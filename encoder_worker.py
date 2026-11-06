"""Encodeur : consomme les projets `.prproj` déposés dans WATCH_IN et les encode via prproj2ffmpeg.

`prproj2ffmpeg.py` (tools/prproj-unpacker/) exporte le projet Premiere en MP4 H.264 via ffmpeg,
avec accélération matérielle NVENC (repli logiciel automatique).

Boucle : surveillance de WATCH_IN (scrutation, insensible aux événements inotify manqués) →
encodage séquentiel (une seule session NVENC à la fois) → promotion du MP4 terminé dans
WATCH_OUT, où le moniteur le déplace vers le dossier projet.

Robustesse :
- la sortie est écrite dans `WATCH_OUT/.staging/` puis renommée atomiquement : le moniteur
  ne voit jamais un fichier partiellement encodé ;
- un job interrompu (crash, redémarrage) est repris automatiquement au démarrage ;
- un projet déposé à la main dans WATCH_IN est « adopté » (enregistré en base) puis encodé.
"""
import argparse
import datetime
import os
import shutil
import threading
import time
from pathlib import Path

from config import (
    BASE_SCAN_FOLDER,
    ENCODER_ABORT_GRACE_SECONDS,
    ENCODER_LOG_FILE,
    ENCODER_POLL_INTERVAL,
    ENCODER_PROGRESS_INTERVAL,
    ENCODER_RECENT_SUBMISSION_GRACE_SECONDS,
    EXPORT_LOG_DIRECTORY,
    INPUT_EXTENSIONS,
    JOBS_DB_FILE,
    OUTPUT_EXTENSION,
    PRPROJ2FFMPEG_EXTRA_BASES,
    QUARANTINE_DIRNAME,
    RESCAN_INTERVAL_SECONDS,
    SOURCE_ARCHIVE_DIRNAME,
    STAGING_DIRNAME,
    STATUS_ABORTED,
    STATUS_DONE,
    STATUS_DONE_WITH_WARNINGS,
    STATUS_ENCODING,
    STATUS_FAILED,
    STATUS_SUBMITTED,
    WATCH_FOLDER_INPUT,
    WATCH_FOLDER_OUTPUT,
    resolve_project_root,
)
from db_helper import get_submission_by_job_id, init_db, update_submission_status_by_job_id
from filename_utils import extract_job_id, is_assist_stem, strip_assist_suffix, strip_job_id
from prproj_engine import (
    EncodeSettings,
    ProcessRegistry,
    expected_output_bytes,
    probe_ffmpeg,
    read_project_info,
    resolve_script,
    run_export,
    summarize_line,
)
from submit_to_watchfolder import build_expected_export_stem, log_submission_action

# Nom du fichier XML temporaire qu'écrivaient les anciennes versions de prproj2ffmpeg
# à côté du projet (résidu possible après un arrêt brutal ; les versions actuelles
# décompressent le .prproj en mémoire, sans aucun fichier temporaire).
_TMP_XML_NAME = '.prproj2ffmpeg-tmp.xml'

# Callback optionnel pour envoyer les logs vers un autre système (ex: Flask SSE)
log_callback = None

_log_lock = threading.Lock()
_state_lock = threading.Lock()
_registry = ProcessRegistry()

# Projets à statut terminal dont l'archivage a échoué : ne pas réessayer (ni journaliser)
# à chaque cycle de scrutation de 2 s. Une entrée est retirée si le fichier disparaît.
_unarchivable_paths: set[Path] = set()
_unarchivable_lock = threading.Lock()

_state = {
    'paused': False,
    'current_job': None,
    'current_file': None,
    'current_display_name': None,
    'current_is_assist': False,
    'current_started_at': None,
    'current_stage': None,
    'current_project': None,
    'current_progress': None,
    'queue_pending': 0,
    'batch': {'completed': 0, 'total': 0, 'active': False},
    'last_error': None,
    'processed': 0,
    'failed': 0,
    'aborted': 0,
    'encoder_available': False,
    'encoder_engine': None,
}


# --- Journalisation ---

def log_encoder_message(message: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] (Encoder) {message}"
    print(log_entry)
    if log_callback:
        try:
            log_callback(log_entry)
        except Exception:
            pass
    with _log_lock:
        try:
            ENCODER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(ENCODER_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(log_entry + "\n")
        except Exception as e:
            print(f"ERREUR: Impossible d'écrire dans le fichier log de l'encodeur '{ENCODER_LOG_FILE}': {e}")


def rotate_encoder_log_if_large():
    """Fait pivoter le journal de l'encodeur au-delà de 5 Mo (thread-safe)."""
    try:
        if ENCODER_LOG_FILE.exists() and ENCODER_LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            with _log_lock:
                backup_log = ENCODER_LOG_FILE.with_name(f"{ENCODER_LOG_FILE.stem}_backup{ENCODER_LOG_FILE.suffix}")
                if backup_log.exists():
                    backup_log.unlink()
                ENCODER_LOG_FILE.rename(backup_log)
                print(f"INFO: Fichier log de l'encodeur archivé sous '{backup_log.name}' et réinitialisé.")
    except Exception as e:
        print(f"AVERTISSEMENT: Impossible de faire tourner le log de l'encodeur: {e}")


# --- État partagé (lu par /api/status) ---

def _set_state(**kwargs):
    with _state_lock:
        _state.update(kwargs)


def _bump(counter: str, delta: int = 1):
    """Incrémente un compteur de l'état partagé (sous verrou, sans cycle lecture/modification)."""
    with _state_lock:
        _state[counter] = _state.get(counter, 0) + delta


def snapshot() -> dict:
    """Photographie de l'état de l'encodeur pour l'API."""
    with _state_lock:
        state = dict(_state)
        state['batch'] = dict(_state.get('batch') or {})
        return state


def _clear_current_job_state() -> None:
    """Efface l'état du job courant (fin normale, abandon ou exception inattendue)."""
    _set_state(
        current_job=None,
        current_file=None,
        current_display_name=None,
        current_is_assist=False,
        current_started_at=None,
        current_stage=None,
        current_project=None,
        current_progress=None,
    )


def _display_name_for(path: Path) -> tuple[str, bool]:
    """Nom lisible d'un projet (sans job_id ni suffixe `_assist`) et indicateur de variante."""
    stem = strip_job_id(path.stem)
    is_assist = is_assist_stem(stem)
    display = strip_assist_suffix(stem) if is_assist else stem
    return display or path.stem, is_assist


def queue_snapshot() -> list[dict]:
    """
    Liste en lecture seule des projets en attente dans WATCH_IN, hors job courant.

    Contrairement à `_pick_next_job`, cette fonction n'adopte ni ne renomme aucun fichier :
    elle peut donc être appelée à chaque sondage de `/api/status`.
    """
    with _state_lock:
        current_file = _state.get('current_file')

    queue = []
    for path in _list_pending_files():
        if current_file and path.name == current_file:
            continue
        display_name, is_assist = _display_name_for(path)
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = 0
        queue.append({
            'job_id': extract_job_id(path.name),
            'file_name': path.name,
            'display_name': display_name,
            'is_assist': is_assist,
            'size_bytes': size_bytes,
        })
    return queue


def _update_batch_state(pending_count: int) -> None:
    """
    Met à jour l'avancement du lot courant.

    `pending_count` inclut le job en cours quand il y en a un. Un nouveau lot démarre dès que
    la file était vide ou que le lot précédent est terminé. Si de nouveaux projets arrivent
    pendant un lot, le total augmente sans remettre à zéro les jobs déjà terminés.
    """
    with _state_lock:
        batch = dict(_state.get('batch') or {})
        current = bool(_state.get('current_job'))
        if pending_count <= 0 and not current:
            batch['active'] = False
            _state['batch'] = batch
            return

        if not batch.get('active') or batch.get('completed', 0) >= batch.get('total', 0):
            batch = {'completed': 0, 'total': max(1, pending_count), 'active': True}
        else:
            in_progress = 1 if current else 0
            waiting = max(0, pending_count - in_progress)
            batch['total'] = max(
                batch.get('total', 0),
                batch.get('completed', 0) + in_progress + waiting,
            )
        _state['batch'] = batch


def _bump_batch_completed() -> None:
    """Comptabilise un job terminé (succès, échec ou abandon) dans le lot courant."""
    with _state_lock:
        batch = dict(_state.get('batch') or {})
        batch['completed'] = batch.get('completed', 0) + 1
        batch['total'] = max(batch.get('total', 0), batch['completed'])
        _state['batch'] = batch


def pause_and_abort():
    """Met l'encodeur en pause et abandonne le job en cours (appelé par /api/stop)."""
    _set_state(paused=True)
    aborted = _registry.abort(ENCODER_ABORT_GRACE_SECONDS)
    if aborted:
        log_encoder_message("Abandon du job en cours demandé (arrêt utilisateur)...")
    else:
        log_encoder_message("Encodeur en pause : les projets en attente seront repris au prochain démarrage.")
    return aborted


def resume():
    """Reprend le traitement de la file d'attente (appelé par /api/start)."""
    _set_state(paused=False)


def shutdown(grace_seconds: float = ENCODER_ABORT_GRACE_SECONDS):
    """Arrêt propre : abandonne le job en cours (appelé à l'extinction de l'application)."""
    _registry.abort(grace_seconds)


# --- File d'attente ---

def _is_input_file(path: Path) -> bool:
    """Vrai si le fichier est un projet candidat (bonne extension, pas un fichier temporaire)."""
    name = path.name
    if name.startswith('.') or name.endswith('.tmp') or name.endswith('.part'):
        return False
    return name.lower().endswith(tuple(INPUT_EXTENSIONS))


def _list_pending_files():
    """Projets en attente dans WATCH_IN, triés par date de modification (FIFO)."""
    if not WATCH_FOLDER_INPUT.exists():
        return []
    files = []
    try:
        for entry in WATCH_FOLDER_INPUT.iterdir():
            if entry.is_file() and _is_input_file(entry):
                try:
                    files.append((entry.stat().st_mtime, entry))
                except OSError:
                    continue
    except Exception as e:
        log_encoder_message(f"ERREUR lors du listage de '{WATCH_FOLDER_INPUT}': {type(e).__name__} - {e}")
        return []
    return [path for _, path in sorted(files, key=lambda item: (item[0], item[1].name))]


def _is_unarchivable(prproj_path: Path) -> bool:
    with _unarchivable_lock:
        return prproj_path in _unarchivable_paths


def _archive_source(prproj_path: Path) -> bool:
    """Déplace le projet consommé dans WATCH_IN/Source/<horodatage>/ (≡ archive des sources d'AME).

    Retourne False si le déplacement a échoué. L'échec n'est journalisé qu'une fois : le
    chemin est mémorisé pour ne pas produire une ligne de log toutes les 2 s.
    """
    if _is_unarchivable(prproj_path):
        return False
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H-%M-%S')
    destination_dir = WATCH_FOLDER_INPUT / SOURCE_ARCHIVE_DIRNAME / timestamp
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(prproj_path), str(destination_dir / prproj_path.name))
        with _unarchivable_lock:
            _unarchivable_paths.discard(prproj_path)
        log_encoder_message(f"  Projet archivé dans '{destination_dir}'")
        return True
    except Exception as e:
        with _unarchivable_lock:
            already_reported = prproj_path in _unarchivable_paths
            _unarchivable_paths.add(prproj_path)
        if not already_reported:
            log_encoder_message(
                f"  AVERTISSEMENT: impossible d'archiver '{prproj_path.name}' : "
                f"{type(e).__name__} - {e} (nouvelle tentative désactivée jusqu'au redémarrage)")
        return False


def _ensure_submission(prproj_path: Path):
    """
    Retourne (job_id, ligne de soumission, chemin réel) pour un projet présent dans WATCH_IN.
    Adopte le projet s'il n'a pas été soumis par l'application (dépôt manuel) :
    enregistrement en base + journal CSV, sous un nom unique contenant un job_id.
    """
    job_id = extract_job_id(prproj_path.name)
    if job_id:
        row = get_submission_by_job_id(JOBS_DB_FILE, job_id)
        if row:
            return job_id, row, prproj_path
        # Un fichier tout juste renommé par submit_to_watchfolder.py peut être visible
        # avant son insertion SQLite : ne pas l'adopter avec le chemin WATCH_IN (C-01).
        try:
            age = time.time() - prproj_path.stat().st_mtime
        except OSError:
            return None, None, prproj_path
        if age < ENCODER_RECENT_SUBMISSION_GRACE_SECONDS:
            return None, None, prproj_path
        expected_stem = build_expected_export_stem(prproj_path)
        log_submission_action(prproj_path, prproj_path.name, expected_stem, job_id)
        return job_id, get_submission_by_job_id(JOBS_DB_FILE, job_id), prproj_path

    # Aucun job_id : dépôt manuel → adoption (renommage en nom unique + enregistrement)
    job_id = f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
    unique_name = f"{prproj_path.stem}_{job_id}{prproj_path.suffix}"
    adopted_path = prproj_path.with_name(unique_name)
    try:
        prproj_path.rename(adopted_path)
    except Exception as e:
        log_encoder_message(f"  ERREUR: impossible de renommer le projet déposé '{prproj_path.name}': {e}")
        return None, None, prproj_path
    expected_stem = build_expected_export_stem(adopted_path)
    log_encoder_message(f"Projet déposé manuellement '{prproj_path.name}' adopté sous '{unique_name}' (JobId: {job_id})")
    log_submission_action(adopted_path, unique_name, expected_stem, job_id)
    return job_id, get_submission_by_job_id(JOBS_DB_FILE, job_id), adopted_path


# --- Encodage d'un job ---

def _media_bases_for(row: dict) -> list:
    """
    Racines de résolution des médias passées à prproj2ffmpeg (`--base`) : racine du projet
    d'origine, son parent, la racine scannée, puis PRPROJ2FFMPEG_EXTRA_BASES.
    """
    bases = []
    original_path = str(row.get('OriginalFilePath') or '')
    if original_path:
        try:
            project_root = resolve_project_root(Path(original_path))
            bases.extend([project_root, project_root.parent])
        except Exception:
            pass
    bases.extend([BASE_SCAN_FOLDER, *PRPROJ2FFMPEG_EXTRA_BASES])
    unique = []
    for base in bases:
        if base not in unique:
            unique.append(base)
    return unique


def _process_job(prproj_path: Path, job_id: str, row: dict, settings: EncodeSettings, script_path: Path) -> str:
    """Encode un projet `.prproj`. Retourne le statut final du job."""
    stem = prproj_path.stem  # contient déjà le job_id (ex: "M6 - LMCR - La potée lorraine_9x16_2_20261010_120000_abcdef")

    staging_dir = WATCH_FOLDER_OUTPUT / STAGING_DIRNAME
    staging_path = staging_dir / f"{stem}{OUTPUT_EXTENSION}"
    published_path = WATCH_FOLDER_OUTPUT / f"{stem}{OUTPUT_EXTENSION}"
    export_log_path = EXPORT_LOG_DIRECTORY / f"{stem}.log"

    for directory in (staging_dir, EXPORT_LOG_DIRECTORY):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log_encoder_message(f"  ERREUR: impossible de créer '{directory}': {e}")

    started_at = time.monotonic()
    display_name, is_assist = _display_name_for(prproj_path)
    _set_state(
        current_job=job_id,
        current_file=prproj_path.name,
        current_display_name=display_name,
        current_is_assist=is_assist,
        current_started_at=started_at,
        current_stage='preparing',
        current_project=None,
        current_progress=None,
    )

    media_bases = _media_bases_for(row)
    project_info = read_project_info(prproj_path, script_path, media_bases=media_bases)
    project_payload = None
    if project_info:
        project_payload = {
            'sequence_name': project_info.sequence_name,
            'duration_sec': project_info.duration_sec,
            'fps': project_info.fps,
            'width': project_info.width,
            'height': project_info.height,
        }
    _set_state(current_project=project_payload)

    log_encoder_message("=" * 70)
    log_encoder_message(f"Encodage du job {job_id} : '{prproj_path.name}'")
    if project_info:
        log_encoder_message(f"  Projet  : séquence '{project_info.sequence_name or '(sans nom)'}' · "
                            f"{project_info.duration_sec if project_info.duration_sec is not None else '?'} s "
                            f"@ {project_info.fps or '?'} fps · "
                            f"{project_info.width or '?'}x{project_info.height or '?'}")
    else:
        log_encoder_message("  Projet  : métadonnées indisponibles (--dump a échoué ; encodage quand même tenté)")
    log_encoder_message(f"  Sortie  : '{published_path.name}' (staging '{staging_path.name}')")
    log_encoder_message(f"  Encodage: prproj2ffmpeg · H.264 {settings.bitrate} · encodeur '{settings.encoder}' (ffmpeg/NVENC)")

    update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_ENCODING)

    expected_bytes = expected_output_bytes(project_info, settings)
    duration_sec = project_info.duration_sec if project_info else None

    def _on_progress(idle_seconds: float, stats: dict | None = None, *, emit_log: bool = True):
        size = 0
        try:
            if staging_path.exists():
                size = staging_path.stat().st_size
        except OSError:
            size = 0
        elapsed = time.monotonic() - started_at

        stats = stats or {}
        encoded_sec = stats.get('encoded_sec')
        speed = stats.get('speed')

        # Priorité au temps encodé remonté par ffmpeg : plus fiable que la taille du MP4.
        percent = None
        if duration_sec and encoded_sec is not None:
            percent = min(99.0, max(0.0, encoded_sec * 100.0 / duration_sec))
        elif expected_bytes:
            percent = min(99.0, size * 100.0 / expected_bytes) if size else 0.0

        eta_seconds = None
        if speed and duration_sec is not None and encoded_sec is not None:
            eta_seconds = max(0.0, (duration_sec - encoded_sec) / speed)
        elif percent and elapsed > 0:
            eta_seconds = max(0.0, elapsed * (100.0 - percent) / percent)

        stage = 'encoding' if (size > 0 or (encoded_sec or 0) > 0) else 'preparing'
        _set_state(current_stage=stage, current_progress={
            'percent': round(percent, 1) if percent is not None else None,
            'elapsed_seconds': round(elapsed, 1),
            'eta_seconds': round(eta_seconds, 1) if eta_seconds is not None else None,
            'speed': speed,
            'written_bytes': size,
            'expected_bytes': expected_bytes,
            'encoded_sec': round(encoded_sec, 2) if encoded_sec is not None else None,
            'duration_sec': duration_sec,
        })

        if emit_log:
            detail = f"{size / 1_000_000:.1f} Mo écrits"
            if percent is not None:
                detail += f" (≈{int(percent)} % estimé)"
            if speed:
                detail += f" · ×{speed:.2f}"
            log_encoder_message(f"  encodage en cours : {detail} — {elapsed:.0f} s écoulées")

    _on_progress(0.0, {}, emit_log=False)

    # Les sorties brutes (script + ffmpeg) sont journalisées ici, les résumés français alimentent l'UI (SSE)
    def _on_line(raw_line: str):
        summary = summarize_line(raw_line)
        if summary:
            log_encoder_message(f"  {summary}")

    result = run_export(
        prproj_path=prproj_path,
        output_path=staging_path,
        log_path=export_log_path,
        settings=settings,
        script_path=script_path,
        media_bases=media_bases,
        on_line=_on_line,
        on_progress=_on_progress,
        registry=_registry,
        progress_interval=ENCODER_PROGRESS_INTERVAL,
    )

    _clear_current_job_state()

    # Nettoyage d'un éventuel fichier partiel
    def _discard_staging():
        try:
            if staging_path.exists():
                staging_path.unlink()
        except Exception as e:
            log_encoder_message(f"  AVERTISSEMENT: impossible de supprimer le fichier partiel '{staging_path}': {e}")

    if result.aborted:
        _discard_staging()
        log_encoder_message(f"Job {job_id} ABANDONNÉ après {result.duration_seconds:.1f} s (arrêt utilisateur).")
        _bump('aborted')
        update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_ABORTED)
        _archive_source(prproj_path)
        return STATUS_ABORTED

    if result.output_ready:
        # Le statut DONE doit être visible AVANT la publication atomique : l'événement
        # inotify du os.replace réveille le moniteur, qui saute alors la stabilisation (C-03).
        status_updated = False
        try:
            status_updated = update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_DONE)
        except Exception as e:
            log_encoder_message(
                f"  AVERTISSEMENT: impossible de passer le job {job_id} à '{STATUS_DONE}' avant publication : {e}")

        # Promotion atomique dans le dossier surveillé par le moniteur (même système de fichiers)
        try:
            os.replace(staging_path, published_path)
        except Exception as e:
            log_encoder_message(f"  ERREUR: impossible de publier '{staging_path.name}' vers '{published_path}': {e}")
            _discard_staging()
            update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_FAILED)
            _set_state(last_error=f"publication impossible: {e}")
            _bump('failed')
            _archive_source(prproj_path)
            return STATUS_FAILED

        log_encoder_message(f"Job {job_id} TERMINÉ en {result.duration_seconds:.1f} s — "
                            f"{result.output_size / 1_000_000:.1f} Mo → dépôt dans '{WATCH_FOLDER_OUTPUT.name}' pour déplacement final.")
        log_encoder_message(f"  Journal complet de l'encodage : '{export_log_path}'")

        if not status_updated:
            # Repli après publication : le fichier est complet, le moniteur le déplacera
            # même si le statut n'a pas pu être écrit avant l'événement inotify.
            try:
                update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_DONE)
            except Exception as e:
                log_encoder_message(
                    f"  AVERTISSEMENT: impossible de passer le job {job_id} à '{STATUS_DONE}' après publication : {e}")
        _bump('processed')
        _archive_source(prproj_path)
        return STATUS_DONE

    # Échec
    _discard_staging()
    detail = result.error_lines[0] if result.error_lines else f"code retour {result.returncode}"
    detail = summarize_line(detail) or detail
    log_encoder_message(f"Job {job_id} EN ÉCHEC après {result.duration_seconds:.1f} s — {detail}")
    log_encoder_message(f"  Journal complet : '{export_log_path}'")
    log_encoder_message("  Le projet n'a pas été encodé : relancer la soumission (option « Forcer l'écrasement ») pour réessayer.")
    update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_FAILED)
    _set_state(last_error=str(detail))
    _bump('failed')
    _archive_source(prproj_path)
    return STATUS_FAILED


def _pick_next_job():
    """Retourne (chemin réel, job_id, ligne) du prochain job à encoder, ou (None, None, None)."""
    with _unarchivable_lock:
        for stale_path in [path for path in _unarchivable_paths if not path.exists()]:
            _unarchivable_paths.discard(stale_path)

    for prproj_path in _list_pending_files():
        job_id, row, job_path = _ensure_submission(prproj_path)
        if not job_id or not row:
            continue
        status = row.get('Status')
        if status in (STATUS_DONE, STATUS_DONE_WITH_WARNINGS):
            # Statut déjà avancé : soit la sortie existe (le moniteur va la déplacer),
            # soit l'encodage s'est achevé sans laisser de fichier → on refait le job.
            if (WATCH_FOLDER_OUTPUT / f"{job_path.stem}{OUTPUT_EXTENSION}").exists():
                if not _is_unarchivable(job_path):
                    log_encoder_message(f"Job {job_id} déjà encodé (sortie déjà dans '{WATCH_FOLDER_OUTPUT.name}') : projet archivé sans réencodage.")
                _archive_source(job_path)
                continue
            log_encoder_message(f"Job {job_id} marqué '{status}' mais aucune sortie trouvée : réencodage.")
        elif status not in (STATUS_SUBMITTED, STATUS_ENCODING):
            # Statut terminal (échec, abandon, déplacement effectué) : ne jamais réencoder en boucle.
            # Une nouvelle soumission (option « Forcer l'écrasement ») crée un nouveau job_id.
            if not _is_unarchivable(job_path):
                log_encoder_message(f"Job {job_id} au statut '{status}' : projet archivé sans encodage.")
            _archive_source(job_path)
            continue
        return job_path, job_id, row
    return None, None, None


def clean_staging_leftovers():
    """Supprime les fichiers partiels laissés dans `.staging` par un arrêt brutal."""
    staging_dir = WATCH_FOLDER_OUTPUT / STAGING_DIRNAME
    if not staging_dir.is_dir():
        return
    try:
        for item in staging_dir.iterdir():
            if item.is_file():
                try:
                    item.unlink()
                    log_encoder_message(f"Nettoyage au démarrage : fichier partiel '{item.name}' supprimé.")
                except Exception as e:
                    log_encoder_message(f"AVERTISSEMENT: impossible de supprimer '{item}': {e}")
    except Exception as e:
        log_encoder_message(f"AVERTISSEMENT: impossible de nettoyer '{staging_dir}': {e}")


def clean_input_leftovers():
    """Supprime les XML temporaires laissés par d'anciennes versions de prproj2ffmpeg.

    Le script actuel décompresse le `.prproj` en mémoire et n'écrit plus aucun
    fichier temporaire ; ce nettoyage ne traite que les résidus historiques.
    """
    if not WATCH_FOLDER_INPUT.is_dir():
        return
    try:
        for item in WATCH_FOLDER_INPUT.iterdir():
            if item.is_file() and item.name == _TMP_XML_NAME:
                try:
                    item.unlink()
                    log_encoder_message(f"Nettoyage au démarrage : fichier temporaire '{item.name}' supprimé.")
                except Exception as e:
                    log_encoder_message(f"AVERTISSEMENT: impossible de supprimer '{item}': {e}")
    except Exception as e:
        log_encoder_message(f"AVERTISSEMENT: impossible de nettoyer '{WATCH_FOLDER_INPUT}': {e}")


def start_encoder(stop_event=None, once: bool = False):
    """Boucle principale de l'encodeur (thread daemon). `once=True` : vide la file puis s'arrête."""
    rotate_encoder_log_if_large()
    log_encoder_message("--- Démarrage de l'encodeur prproj2ffmpeg (ffmpeg/NVENC) ---")
    log_encoder_message(f"             Watchfolder entrée: '{WATCH_FOLDER_INPUT}'")
    log_encoder_message(f"             Watchfolder sortie: '{WATCH_FOLDER_OUTPUT}' (staging '{STAGING_DIRNAME}', quarantaine '{QUARANTINE_DIRNAME}')")
    log_encoder_message(f"             Scrutation de la file toutes les {ENCODER_POLL_INTERVAL}s · progression toutes les {ENCODER_PROGRESS_INTERVAL}s")
    log_encoder_message(f"             Base de données: '{JOBS_DB_FILE}' · journaux d'encodage: '{EXPORT_LOG_DIRECTORY}'")

    try:
        init_db(JOBS_DB_FILE)
    except Exception as e:
        log_encoder_message(f"ERREUR CRITIQUE: impossible d'initialiser la base SQLite '{JOBS_DB_FILE}': {e}")
        return

    if not WATCH_FOLDER_INPUT.exists():
        try:
            WATCH_FOLDER_INPUT.mkdir(parents=True, exist_ok=True)
            log_encoder_message(f"Dossier d'entrée créé: '{WATCH_FOLDER_INPUT}'")
        except Exception as e:
            log_encoder_message(f"ERREUR CRITIQUE: impossible de créer '{WATCH_FOLDER_INPUT}': {e}")
            return

    clean_staging_leftovers()
    clean_input_leftovers()

    settings = EncodeSettings.from_config()
    script_path = None
    script_warning_logged = False
    last_rescan = time.monotonic()

    while stop_event is None or not stop_event.is_set():
        # Résolution (et re-résolution) du script prproj2ffmpeg et de l'environnement ffmpeg
        if script_path is None:
            script_path = resolve_script()
            if script_path:
                probe = probe_ffmpeg()
                _set_state(encoder_available=probe.ready, encoder_engine=f"prproj2ffmpeg · {probe.describe()}")
                log_encoder_message(f"Script d'encodage détecté : '{script_path}'")
                log_encoder_message(f"             Environnement : {probe.describe()}")
                if not probe.ready:
                    log_encoder_message("AVERTISSEMENT: ffmpeg/ffprobe introuvable(s) dans le PATH — l'encodage échouera.")
                script_warning_logged = False
            else:
                _set_state(encoder_available=False, encoder_engine=None)
                if not script_warning_logged:
                    log_encoder_message("ERREUR: script 'prproj2ffmpeg.py' introuvable — aucun encodage ne peut démarrer. "
                                        "Vérifiez PRPROJ2FFMPEG_SCRIPT dans le fichier .env.")
                    script_warning_logged = True

        pending = _list_pending_files()
        _set_state(queue_pending=len(pending))
        _update_batch_state(len(pending))

        if _state_snapshot_paused():
            time.sleep(ENCODER_POLL_INTERVAL)
            continue

        if script_path and pending:
            prproj_path, job_id, row = _pick_next_job()
            if prproj_path:
                try:
                    _process_job(prproj_path, job_id, row, settings, script_path)
                except Exception as e:
                    _clear_current_job_state()
                    log_encoder_message(f"ERREUR INATTENDUE pendant l'encodage de '{prproj_path.name}': {type(e).__name__} - {e}")
                    _archive_source(prproj_path)
                finally:
                    _bump_batch_completed()
                continue  # enchaîne immédiatement sur le job suivant

        if once and not _list_pending_files():
            break

        if RESCAN_INTERVAL_SECONDS and (time.monotonic() - last_rescan) >= RESCAN_INTERVAL_SECONDS:
            last_rescan = time.monotonic()
            if pending:
                log_encoder_message(f"{len(pending)} projet(s) en attente dans '{WATCH_FOLDER_INPUT.name}'.")

        time.sleep(ENCODER_POLL_INTERVAL)

    log_encoder_message("Encodeur arrêté proprement.")


def _state_snapshot_paused() -> bool:
    with _state_lock:
        return bool(_state['paused'])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Encodeur prproj2ffmpeg : consomme les projets .prproj de WATCH_IN et les encode en MP4.")
    parser.add_argument("--once", action="store_true", help="vide la file d'attente puis s'arrête (diagnostic)")
    args = parser.parse_args()

    stop = threading.Event()
    if args.once:
        start_encoder(stop_event=stop, once=True)
    else:
        try:
            start_encoder(stop_event=stop)
        except KeyboardInterrupt:
            shutdown()

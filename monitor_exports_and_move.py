"""Surveillance du watchfolder de sortie et déplacement des exports vers leur destination finale.

L'encodeur (`encoder_worker.py`) dépose les MP4 terminés dans `WATCH_FOLDER_OUTPUT` sous le nom
`{stem}_{job_id}.mp4`. Ce moniteur les rapproche de leur soumission (job_id, puis heuristique
en filet de sécurité) et les déplace/renomme vers le dossier projet d'origine, avec versionnage
en cas de collision, quarantaine si aucun projet ne correspond, et réessais en cas de verrou.
"""
import csv
import datetime
import os
import re
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from config import (
    BASE_SCAN_FOLDER,
    ENABLE_FALLBACK_MOVE,
    ENABLE_HEURISTIC_MATCHING,
    FOLDER_KEYWORD,
    JOBS_DB_FILE,
    LOGS_DIRECTORY,
    MONITOR_LOG_FILE,
    MOVE_RETRY_COUNT,
    MOVE_RETRY_DELAY,
    OUTPUT_EXTENSION,
    QUARANTINE_DIRNAME,
    RESCAN_INTERVAL_SECONDS,
    STABLE_FILE_CHECKS,
    STABLE_FILE_INTERVAL,
    STAGING_DIRNAME,
    ALLOW_OVERWRITE,
    STATUS_DONE,
    STATUS_DONE_WITH_WARNINGS,
    STATUS_MOVE_FAILED,
    STATUS_MOVED,
    SUBMISSION_LOG_FILE,
    WATCH_FOLDER_OUTPUT,
    is_matchable_status,
    resolve_project_root,
)
from db_helper import (
    get_matchable_submissions,
    get_submission_by_job_id,
    init_db,
    update_submission_status,
    update_submission_status_by_job_id,
)
from filename_utils import (
    get_base_export_stem,
    normalize_for_matching,
    sanitize_filename_stem,
    slugify_like_ame,
    strip_assist_suffix,
)

try:
    LOGS_DIRECTORY.mkdir(parents=True, exist_ok=True)
except Exception as e_mkdir_main_monitor:
    print(f"AVERTISSEMENT: Impossible de créer le dossier de logs principal '{LOGS_DIRECTORY}' au démarrage du moniteur: {e_mkdir_main_monitor}")


# --- Fonctions ---
log_lock = threading.Lock()

# Callback optionnel pour envoyer les logs vers un autre système (ex: Flask SSE)
log_callback = None

# Nombre maximal de threads de traitement simultanés (protection contre les pics d'événements Watchdog)
MAX_PROCESSING_THREADS = 8


def log_monitor_message(message: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] (Monitor) {message}"
    print(log_entry)
    if log_callback:
        try:
            log_callback(log_entry)
        except Exception:
            pass
    with log_lock:
        try:
            MONITOR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(MONITOR_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(log_entry + "\n")
        except Exception as e:
            print(f"ERREUR: Impossible d'écrire dans le fichier log du moniteur '{MONITOR_LOG_FILE}': {e}")


def get_versioned_path(target_path: Path) -> Path:
    """
    Si target_path existe déjà, ajoute un suffixe de version comme _v1, _v2, etc., pour éviter l'écrasement.
    """
    if not target_path.exists():
        return target_path

    stem = target_path.stem
    suffix = target_path.suffix
    parent = target_path.parent

    # Vérifier s'il y a déjà un suffixe de version _v\d+
    match = re.search(r'_v(\d+)$', stem)
    if match:
        version = int(match.group(1))
        base_stem = stem[:match.start()]
    else:
        version = 0
        base_stem = stem

    while True:
        version += 1
        new_path = parent / f"{base_stem}_v{version}{suffix}"
        if not new_path.exists():
            return new_path


def _is_same_filesystem(path_a: Path, path_b: Path) -> bool:
    """Vrai si les deux chemins sont sur le même système de fichiers (renommage atomique possible)."""
    try:
        return path_a.stat().st_dev == path_b.stat().st_dev
    except OSError:
        return False


def move_to_final_verified(source_path: Path, destination_path: Path) -> None:
    """
    Déplace un fichier exporté vers sa destination finale sans jamais exposer un fichier partiel :
    - même système de fichiers : renommage atomique direct ;
    - systèmes de fichiers différents (ext4 → NTFS/FUSE) : copie vers un `.part` local à la
      destination, vérification de la taille, puis remplacement atomique et suppression de la source.

    Lève une exception (OSError) en cas d'échec ; le fichier `.part` est nettoyé au passage.
    """
    if _is_same_filesystem(source_path, destination_path.parent):
        os.replace(source_path, destination_path)
        return

    temp_path = destination_path.parent / f".{destination_path.name}.part"
    try:
        shutil.copy2(source_path, temp_path)
        if temp_path.stat().st_size != source_path.stat().st_size:
            raise OSError(
                f"Taille incohérente après copie (source: {source_path.stat().st_size} octets, "
                f"copie: {temp_path.stat().st_size} octets)"
            )
        os.replace(temp_path, destination_path)
        try:
            source_path.unlink()
        except OSError:
            pass
    except Exception:
        try:
            if temp_path.exists():
                temp_path.unlink()
        except OSError:
            pass
        raise


def update_submission_log_status(matched_submission_row: dict, new_status: str, final_target_filename: str):
    watchfolder_file_name = matched_submission_row.get('WatchfolderFileName', '<inconnu>')
    matched_timestamp = matched_submission_row.get('Timestamp', '')
    matched_original_file_path = matched_submission_row.get('OriginalFilePath', '')
    matched_expected_export_name = matched_submission_row.get('ExpectedExportName', '')
    job_id = matched_submission_row.get('JobId')

    log_monitor_message(
        f"INFO: Statut pour le projet via '{watchfolder_file_name}' (OriginalFilePath='{matched_original_file_path}', export final: '{final_target_filename}') devrait être mis à jour à '{new_status}'.")

    try:
        updated = False
        # Essayer d'abord la mise à jour via l'identifiant unique job_id
        if job_id:
            updated = update_submission_status_by_job_id(JOBS_DB_FILE, job_id, new_status)

        if not updated:
            # Fallback historique
            updated = update_submission_status(
                JOBS_DB_FILE,
                matched_timestamp,
                matched_original_file_path,
                watchfolder_file_name,
                matched_expected_export_name,
                new_status
            )

        if updated:
            log_monitor_message(f"    Mise à jour du statut dans SQLite pour '{watchfolder_file_name}' à '{new_status}'.")
        else:
            log_monitor_message(
                "    AVERTISSEMENT: Aucune entrée correspondante en attente trouvée dans SQLite pour mise à jour.")
    except Exception as e_update_db:
        log_monitor_message(
            f"ERREUR CRITIQUE lors de la mise à jour du statut dans SQLite: {type(e_update_db).__name__} - {e_update_db}")


class ExportHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self.processing_lock = threading.Lock()
        self.processing_paths = set()
        self.executor = ThreadPoolExecutor(max_workers=MAX_PROCESSING_THREADS, thread_name_prefix="export-process")

    def _is_temporary_file(self, filename: str) -> bool:
        if re.match(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", Path(filename).stem):
            log_monitor_message(f"  DEBUG: File '{filename}' matches GUID pattern, likely temporary.")
            return True
        if "._" in filename or filename.endswith("~") or filename.startswith(".tmp") or filename.endswith(".part"):
            log_monitor_message(f"  DEBUG: File '{filename}' matches common temporary pattern. Ignored.")
            return True
        if filename.startswith("._") or Path(filename).stem.endswith("._"):
            log_monitor_message(f"  DEBUG: File '{filename}' matches '._' prefix/suffix pattern, likely temporary.")
            return True
        if not Path(filename).stem.isascii() or len(Path(filename).stem) > 100:
            if not filename.lower().endswith(OUTPUT_EXTENSION.lower()):
                log_monitor_message(
                    f"  DEBUG: File '{filename}' has unusual stem characteristics, potentially temporary.")
                return True
        return False

    def process_file(self, src_path_str: str):
        try:
            norm_path = str(Path(src_path_str).resolve()).lower()
        except Exception:
            norm_path = str(Path(src_path_str).absolute()).lower()

        with self.processing_lock:
            if norm_path in self.processing_paths:
                log_monitor_message(f"  INFO: Fichier '{src_path_str}' est déjà en cours de traitement par un autre thread. Événement ignoré pour éviter les conflits.")
                return
            self.processing_paths.add(norm_path)

        try:
            self._process_file_internal(src_path_str)
        finally:
            with self.processing_lock:
                self.processing_paths.discard(norm_path)

    def _is_published_by_encoder(self, exported_file_path: Path) -> bool:
        """
        Vrai si le fichier correspond à un export publié par l'encodeur : son nom contient un job_id
        et la base le donne au statut `EXPORT_DONE*`, c'est-à-dire après publication atomique
        (`os.replace` depuis `.staging`) et vérification de taille. Le fichier est donc complet
        dès sa première apparition, sans attendre la vérification de stabilité.
        """
        match = re.search(r'(\d{8}_\d{6}_[a-f0-9]{6})', exported_file_path.name)
        if not match:
            return False
        job_id = match.group(1)
        try:
            row = get_submission_by_job_id(JOBS_DB_FILE, job_id)
        except Exception as e_db:
            log_monitor_message(f"  AVERTISSEMENT: lecture de la base impossible pour '{job_id}' : {e_db}")
            return False
        if row and row.get('Status') in (STATUS_DONE, STATUS_DONE_WITH_WARNINGS):
            log_monitor_message(f"  Job {job_id} publié par l'encodeur (statut '{row.get('Status')}') : fichier réputé complet, vérification de stabilité ignorée.")
            return True
        return False

    def _wait_for_stability(self, exported_file_path: Path) -> bool:
        """
        Attend que la taille du fichier soit stable (STABLE_FILE_CHECKS × STABLE_FILE_INTERVAL).
        Retourne False si le traitement doit être abandonné (fichier disparu, taille nulle).
        """
        last_size = -1
        stable_checks_done = 0
        # Utilisation des constantes configurées pour la stabilité
        for i in range(STABLE_FILE_CHECKS + 5):  # max_stabilization_attempts
            if not exported_file_path.exists():
                log_monitor_message(
                    f"  INFO: Fichier '{exported_file_path.name}' a disparu pendant la vérification de stabilité.")
                return False
            try:
                current_size = exported_file_path.stat().st_size
                if current_size == last_size and current_size > 0:
                    stable_checks_done += 1
                    log_monitor_message(
                        f"  Vérification stabilité {stable_checks_done}/{STABLE_FILE_CHECKS} pour '{exported_file_path.name}', taille: {current_size}")
                    if stable_checks_done >= STABLE_FILE_CHECKS:
                        log_monitor_message(
                            f"  SUCCÈS: Fichier '{exported_file_path.name}' semble stable (taille: {current_size}).")
                        break
                else:
                    stable_checks_done = 0
                    if current_size == 0 and last_size <= 0:
                        log_monitor_message(f"  Fichier '{exported_file_path.name}' taille 0. Attente...")
                    else:
                        log_monitor_message(
                            f"  Fichier '{exported_file_path.name}' taille changeante (préc: {last_size}, nouv: {current_size}). Réinitialisation compteur stabilité.")
                last_size = current_size
            except FileNotFoundError:
                log_monitor_message(
                    f"  INFO: Fichier '{exported_file_path.name}' non trouvé pendant la vérification de stabilité.")
                return False
            except Exception as e_stat:
                log_monitor_message(f"  ERREUR pendant stat() sur '{exported_file_path.name}': {type(e_stat).__name__} - {e_stat}")

            if i < (STABLE_FILE_CHECKS + 5 - 1):
                time.sleep(STABLE_FILE_INTERVAL)
        else:
            if last_size == 0 and exported_file_path.exists():
                log_monitor_message(
                    f"  ERREUR: Fichier '{exported_file_path.name}' a une taille de 0 après les vérifications de stabilité. Ignoré.")
                return False
            if exported_file_path.exists():
                log_monitor_message(
                    f"  AVERTISSEMENT: Fichier '{exported_file_path.name}' (taille: {last_size}) n'a pas pu être confirmé comme stable. Tentative de déplacement quand même.")
            else:
                log_monitor_message(
                    f"  INFO: Fichier '{exported_file_path.name}' a disparu avant la fin des vérifications de stabilité.")
                return False
        return True

    def _process_file_internal(self, src_path_str: str):
        """
        Traite un fichier exporté (chemin en chaîne), qu'il soit issu d'un événement watchdog
        ou d'un balayage du dossier de sortie.
        """
        exported_file_path = Path(src_path_str)

        if not exported_file_path.exists():
            log_monitor_message(
                f"  INFO: Fichier '{src_path_str}' n'existe pas/plus. Probablement temporaire, déplacé ou renommé.")
            return

        # Garde défensif : le staging de l'encodeur ne doit jamais être traité (observer non récursif, mais bon)
        try:
            if STAGING_DIRNAME in exported_file_path.resolve().parts or QUARANTINE_DIRNAME in exported_file_path.resolve().parts:
                log_monitor_message(f"  INFO: Fichier '{exported_file_path.name}' situé dans un sous-dossier technique. Ignoré.")
                return
        except Exception:
            pass

        if self._is_temporary_file(exported_file_path.name) or \
                not exported_file_path.name.lower().endswith(OUTPUT_EXTENSION.lower()):
            log_monitor_message(
                f"  INFO: Fichier '{exported_file_path.name}' est temporaire ou n'a pas la bonne extension ('{OUTPUT_EXTENSION}'). Ignoré.")
            return

        log_monitor_message(f"Fichier final potentiel détecté: '{exported_file_path.name}'")

        # Un export publié par l'encodeur est complet dès sa première apparition : inutile d'attendre
        # 2×5 s. Les fichiers arrivés par un autre moyen (dépôt manuel, copie réseau) sont, eux,
        # toujours soumis à la vérification de stabilité.
        if not self._is_published_by_encoder(exported_file_path):
            if not self._wait_for_stability(exported_file_path):
                return

        if not exported_file_path.exists():
            log_monitor_message(
                f"  INFO: Fichier '{exported_file_path.name}' n'existe plus avant la recherche de correspondance. Ignoré.")
            return

        stem_of_exported_file_cleaned = get_base_export_stem(exported_file_path.name)
        stem_of_exported_file_cleaned = sanitize_filename_stem(stem_of_exported_file_cleaned)
        log_monitor_message(f"  Stem exporté (nettoyé et sanitizé): '{stem_of_exported_file_cleaned}'")

        found_match_in_log = False
        matched_row = None

        # 1) Rapprochement par ID de job unique
        # L'ID de job a le format : YYYYMMDD_HHMMSS_ffffff
        job_id_match = re.search(r'(\d{8}_\d{6}_[a-f0-9]{6})', exported_file_path.name)
        if job_id_match:
            job_id = job_id_match.group(1)
            log_monitor_message(f"  ID de job détecté dans le nom du fichier exporté : '{job_id}'")
            try:
                row = get_submission_by_job_id(JOBS_DB_FILE, job_id)
                if row:
                    if is_matchable_status(row.get('Status')):
                        matched_row = row
                        log_monitor_message(f"    Correspondance de job unique trouvée dans SQLite pour '{job_id}'")
                    else:
                        log_monitor_message(f"    INFO: Job '{job_id}' trouvé mais statut déjà traité ('{row.get('Status')}'). Ignoré.")
                        found_match_in_log = True  # Marquer pour éviter le repli heuristique
                else:
                    log_monitor_message(f"    AVERTISSEMENT: Aucun job correspondant à '{job_id}' dans SQLite.")
            except Exception as e_job_query:
                log_monitor_message(f"    ERREUR lors du rapprochement par ID de job : {e_job_query}")

        # 2) Fallback heuristique si pas de match par ID de job (ou si pas d'ID détecté)
        if not matched_row and not found_match_in_log and ENABLE_HEURISTIC_MATCHING:
            log_monitor_message("  Tentative de rapprochement heuristique par nom de fichier...")
            try:
                pending_entries = get_matchable_submissions(JOBS_DB_FILE)
                if pending_entries:
                    for row_idx, row in enumerate(pending_entries):
                        if is_matchable_status(row.get('Status')):
                            final_target_stem_from_log = row['ExpectedExportName']
                            watchfolder_file_name_stem = Path(row['WatchfolderFileName']).stem

                            # Éliminer l'ID de job unique éventuel du nom pour la comparaison heuristique
                            legacy_watchfolder_stem = re.sub(r'_\d{8}_\d{6}_[a-f0-9]{6}$', '', watchfolder_file_name_stem)

                            log_monitor_message(
                                f"    Vérification entrée SQLite [{row_idx}]: OriginalFilePath='{row['OriginalFilePath']}', WatchfolderFileName='{row['WatchfolderFileName']}', ExpectedFinalStem='{final_target_stem_from_log}'")

                            candidate_stems = []

                            candidate_1 = f"{legacy_watchfolder_stem}_export"
                            candidate_stems.append(candidate_1)

                            candidate_2 = slugify_like_ame(final_target_stem_from_log)
                            if candidate_2 not in candidate_stems:
                                candidate_stems.append(candidate_2)

                            candidate_3 = legacy_watchfolder_stem
                            if candidate_3 not in candidate_stems:
                                candidate_stems.append(candidate_3)

                            candidate_4 = final_target_stem_from_log
                            if candidate_4 not in candidate_stems:
                                candidate_stems.append(candidate_4)

                            matched_candidate = None
                            for ame_candidate_stem in candidate_stems:
                                if stem_of_exported_file_cleaned == ame_candidate_stem:
                                    matched_candidate = ame_candidate_stem
                                    break

                                # Fallback matching normalisé
                                if normalize_for_matching(stem_of_exported_file_cleaned) == normalize_for_matching(ame_candidate_stem):
                                    matched_candidate = ame_candidate_stem
                                    break

                            if matched_candidate:
                                matched_row = row
                                log_monitor_message(f"    CORRESPONDANCE HEURISTIQUE TROUVÉE via candidat '{matched_candidate}'")
                                break
            except Exception as e_read_db:
                log_monitor_message(f"    ERREUR lors du scan des entrées en attente : {e_read_db}")

        # 3) Traitement si correspondance trouvée
        if matched_row:
            found_match_in_log = True
            original_file_full_path = Path(matched_row['OriginalFilePath'])
            final_destination_folder = resolve_project_root(original_file_full_path)

            if not final_destination_folder.exists():
                log_monitor_message(f"  Création du dossier de destination: '{final_destination_folder}'")
                try:
                    final_destination_folder.mkdir(parents=True, exist_ok=True)
                except Exception as e_mkdir:
                    log_monitor_message(f"  ERREUR CRITIQUE en créant le dossier '{final_destination_folder}': {e_mkdir}. Abandon.")
                    return

            # Nom final : la variante _assist ne doit jamais apparaître dans le MP4 livré, y compris
            # pour une ligne de soumission créée avant l'introduction de cette règle.
            final_target_filename = f"{sanitize_filename_stem(strip_assist_suffix(matched_row['ExpectedExportName']))}{OUTPUT_EXTENSION}"
            final_destination_path = final_destination_folder / final_target_filename

            # Gestion des collisions à destination
            if final_destination_path.exists():
                if ALLOW_OVERWRITE:
                    log_monitor_message(f"  AVERTISSEMENT: Fichier '{final_destination_path}' existe déjà. ALLOW_OVERWRITE=True -> Écrasement.")
                else:
                    new_dest_path = get_versioned_path(final_destination_path)
                    log_monitor_message(f"  COLLISION: Fichier de destination existe. ALLOW_OVERWRITE=False -> Versionnage: '{final_target_filename}' -> '{new_dest_path.name}'")
                    final_destination_path = new_dest_path
                    final_target_filename = final_destination_path.name

            matched_submission_row_for_log_update = {
                'Timestamp': matched_row.get('Timestamp', ''),
                'OriginalFilePath': matched_row.get('OriginalFilePath', ''),
                'WatchfolderFileName': matched_row.get('WatchfolderFileName', ''),
                'ExpectedExportName': matched_row.get('ExpectedExportName', ''),
                'JobId': matched_row.get('JobId')
            }

            moved_successfully = False
            for attempt in range(MOVE_RETRY_COUNT):
                if not exported_file_path.exists():
                    log_monitor_message(f"  INFO: Fichier source '{exported_file_path.name}' n'existe plus.")
                    if final_destination_path.exists():
                        log_monitor_message("  INFO: Fichier déjà présent à destination.")
                        update_submission_log_status(matched_submission_row_for_log_update, STATUS_MOVED, final_target_filename)
                        moved_successfully = True
                    break
                try:
                    log_monitor_message(f"  Tentative de déplacement ({attempt + 1}/{MOVE_RETRY_COUNT}) vers '{final_destination_path}'...")
                    move_to_final_verified(exported_file_path, final_destination_path)
                    log_monitor_message("  SUCCÈS: Fichier déplacé.")
                    update_submission_log_status(matched_submission_row_for_log_update, STATUS_MOVED, final_target_filename)
                    moved_successfully = True
                    break
                except (PermissionError, OSError) as e_lock:
                    log_monitor_message(f"  ERREUR déplacement: {e_lock}. Réessai dans {MOVE_RETRY_DELAY}s...")
                    time.sleep(MOVE_RETRY_DELAY)
                except Exception as e_move:
                    log_monitor_message(f"  ERREUR inattendue: {e_move}. Réessai dans {MOVE_RETRY_DELAY}s...")
                    time.sleep(MOVE_RETRY_DELAY)

            if not moved_successfully and exported_file_path.exists():
                log_monitor_message(f"  ÉCHEC DÉFINITIF après {MOVE_RETRY_COUNT} essais.")
                update_submission_log_status(matched_submission_row_for_log_update, STATUS_MOVE_FAILED, final_target_filename)

        # 4) Repli ou quarantaine si aucun projet ne correspond dans la base
        if not found_match_in_log and exported_file_path.exists():
            if not ENABLE_FALLBACK_MOVE:
                # Comportement sécurisé par défaut : déplacer en quarantaine
                quarantine_folder = WATCH_FOLDER_OUTPUT / QUARANTINE_DIRNAME
                try:
                    quarantine_folder.mkdir(parents=True, exist_ok=True)
                except Exception as e_quar_mkdir:
                    log_monitor_message(f"  ERREUR lors de la création du dossier de quarantaine: {e_quar_mkdir}")
                    return

                quarantine_path = quarantine_folder / exported_file_path.name
                quarantine_path = get_versioned_path(quarantine_path)

                log_monitor_message(f"  AVERTISSEMENT: Pas de correspondance. ENABLE_FALLBACK_MOVE=False -> Quarantaine : '{quarantine_path}'")

                moved_successfully = False
                for attempt in range(MOVE_RETRY_COUNT):
                    if not exported_file_path.exists():
                        break
                    try:
                        move_to_final_verified(exported_file_path, quarantine_path)
                        log_monitor_message("  SUCCÈS: Fichier déplacé en quarantaine.")
                        moved_successfully = True
                        break
                    except Exception as e_quar_move:
                        log_monitor_message(f"  ERREUR quarantaine: {e_quar_move}. Réessai...")
                        time.sleep(MOVE_RETRY_DELAY)
            else:
                # Comportement d'origine (avec versionnage obligatoire) : dossier projet le plus récent
                log_monitor_message("  AVERTISSEMENT: Pas de correspondance. ENABLE_FALLBACK_MOVE=True -> Déplacement via fallback...")
                try:
                    candidates = [
                        p for p in BASE_SCAN_FOLDER.iterdir()
                        if p.is_dir() and FOLDER_KEYWORD.lower() in p.name.lower()
                    ]
                    if not candidates:
                        log_monitor_message(f"    AVERTISSEMENT: Aucun dossier avec le mot-clé '{FOLDER_KEYWORD}' sous '{BASE_SCAN_FOLDER}'. Abandon.")
                        return

                    def _numeric_prefix(name: str) -> int:
                        m = re.match(r'^(\d+)', name)
                        return int(m.group(1)) if m else -1

                    candidates_sorted = sorted(
                        candidates,
                        key=lambda p: (_numeric_prefix(p.name), p.stat().st_mtime),
                        reverse=True
                    )

                    chosen = candidates_sorted[0]
                    final_destination_folder = chosen

                    final_target_filename = f"{stem_of_exported_file_cleaned}{OUTPUT_EXTENSION}"
                    final_destination_path = final_destination_folder / final_target_filename

                    # Versionnage sécurisé obligatoire en mode fallback
                    if final_destination_path.exists():
                        new_dest_path = get_versioned_path(final_destination_path)
                        log_monitor_message(f"    Fallback collision: '{final_destination_path.name}' existe -> Versionnage: '{new_dest_path.name}'")
                        final_destination_path = new_dest_path

                    moved_successfully = False
                    for attempt in range(MOVE_RETRY_COUNT):
                        if not exported_file_path.exists():
                            break
                        try:
                            log_monitor_message(f"    Fallback: Déplacement ({attempt + 1}/{MOVE_RETRY_COUNT}) vers '{final_destination_path}'...")
                            move_to_final_verified(exported_file_path, final_destination_path)
                            log_monitor_message("    Fallback: SUCCÈS: Fichier déplacé.")
                            moved_successfully = True
                            break
                        except Exception as e_fb_move:
                            log_monitor_message(f"    Fallback ERREUR: {e_fb_move}. Réessai...")
                            time.sleep(MOVE_RETRY_DELAY)
                except Exception as e_fallback:
                    log_monitor_message(f"    ERREUR lors de l'exécution du fallback: {e_fallback}")

    def _process_file_async(self, src_path_str: str):
        """Délègue le traitement à un pool de threads borné (anti-surcharge d'événements Watchdog)."""
        time.sleep(1)  # bref délai pour laisser le handle du fichier se stabiliser
        self.process_file(src_path_str)

    def on_created(self, event):
        if event.is_directory:
            return

        src_path_str = event.src_path
        log_monitor_message(f"Événement 'on_created' détecté pour: {src_path_str} (délégué au pool de stabilité)")
        self.executor.submit(self._process_file_async, src_path_str)

    def on_moved(self, event):
        if event.is_directory:
            return

        src_path_str = event.dest_path
        log_monitor_message(f"Événement 'on_moved' détecté de {event.src_path} vers {src_path_str} (délégué au pool de stabilité)")
        self.executor.submit(self._process_file_async, src_path_str)


def sweep_existing_exports(event_handler: ExportHandler, label: str):
    """Traite les fichiers *extension déjà présents à la racine du dossier de sortie."""
    try:
        existing_files = [
            p for p in WATCH_FOLDER_OUTPUT.iterdir()
            if p.is_file() and p.name.lower().endswith(OUTPUT_EXTENSION.lower())
        ]
    except Exception as e_scan:
        log_monitor_message(f"AVERTISSEMENT: Erreur lors du balayage ({label}) de '{WATCH_FOLDER_OUTPUT}': {type(e_scan).__name__} - {e_scan}")
        return

    if existing_files:
        log_monitor_message(f"{label}: {len(existing_files)} fichier(s) *{OUTPUT_EXTENSION} présents à traiter dans '{WATCH_FOLDER_OUTPUT}'.")
    for existing in existing_files:
        log_monitor_message(f"{label} -> traitement de: '{existing.name}'")
        event_handler.process_file(str(existing))


def rotate_monitor_log_if_large():
    """
    Fait pivoter le fichier log du moniteur s'il dépasse 5 Mo pour éviter
    une consommation excessive d'espace disque. Thread-safe (verrou partagé avec l'écriture des logs).
    """
    try:
        if MONITOR_LOG_FILE.exists() and MONITOR_LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            with log_lock:
                backup_log = MONITOR_LOG_FILE.with_name(f"{MONITOR_LOG_FILE.stem}_backup{MONITOR_LOG_FILE.suffix}")
                if backup_log.exists():
                    backup_log.unlink()
                MONITOR_LOG_FILE.rename(backup_log)
                print(f"INFO: Fichier log du moniteur archivé sous '{backup_log.name}' et réinitialisé.")
    except Exception as e:
        print(f"AVERTISSEMENT: Impossible de faire tourner le log du moniteur: {e}")


# --- Script principal ---
def start_monitor(stop_event=None):
    rotate_monitor_log_if_large()
    log_monitor_message("--- Démarrage du script de monitoring des exports ---")
    log_monitor_message(f"Configuration: Dossier de sortie à surveiller: '{WATCH_FOLDER_OUTPUT}'")
    log_monitor_message(f"             Extension attendue: '{OUTPUT_EXTENSION}'")
    log_monitor_message(f"             Vérification de stabilité: {STABLE_FILE_CHECKS}x{STABLE_FILE_INTERVAL}s")
    log_monitor_message(f"             Réessais de déplacement: {MOVE_RETRY_COUNT}x{MOVE_RETRY_DELAY}s")
    log_monitor_message(f"             Rescan périodique: {RESCAN_INTERVAL_SECONDS}s (0 = désactivé)")
    log_monitor_message(f"             Fichier log de soumission: '{SUBMISSION_LOG_FILE}'")

    if not MONITOR_LOG_FILE.exists():
        try:
            MONITOR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            MONITOR_LOG_FILE.touch()
            log_monitor_message(f"Fichier log du moniteur créé: {MONITOR_LOG_FILE}")
        except Exception as e_touch_monitor_log:
            print(
                f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] AVERTISSEMENT: Impossible de créer le fichier log du moniteur '{MONITOR_LOG_FILE}': {e_touch_monitor_log}")

    if not WATCH_FOLDER_OUTPUT.exists() or not WATCH_FOLDER_OUTPUT.is_dir():
        log_monitor_message(
            f"ERREUR CRITIQUE: Le dossier de sortie à surveiller '{WATCH_FOLDER_OUTPUT}' n'existe pas ou n'est pas un dossier. Arrêt.")
        return

    # Initialiser la base de données
    try:
        init_db(JOBS_DB_FILE)
        log_monitor_message(f"Base de données SQLite initialisée/vérifiée: '{JOBS_DB_FILE}'")
    except Exception as e_init_db:
        log_monitor_message(f"ERREUR CRITIQUE: Impossible d'initialiser la base de données SQLite: {e_init_db}")
        return

    log_monitor_message(f"Démarrage du moniteur pour le dossier: {WATCH_FOLDER_OUTPUT}")

    event_handler = ExportHandler()
    observer = Observer()
    observer.schedule(event_handler, str(WATCH_FOLDER_OUTPUT), recursive=False)

    try:
        # Balayage initial: traiter les fichiers déjà présents à la racine du dossier de sortie
        sweep_existing_exports(event_handler, "Balayage initial")

        observer.start()
        log_monitor_message("Moniteur démarré.")

        last_rescan = time.monotonic()
        while stop_event is None or not stop_event.is_set():
            time.sleep(2)
            if not observer.is_alive():
                log_monitor_message(
                    "ERREUR CRITIQUE: L'observateur Watchdog n'est plus actif! Tentative de redémarrage...")
                try:
                    observer.stop()
                    observer.join(timeout=5)
                except Exception as e_obs_stop:
                    log_monitor_message(f"  Erreur lors de l'arrêt de l'ancien observer: {e_obs_stop}")

                observer = Observer()
                event_handler = ExportHandler()
                observer.schedule(event_handler, str(WATCH_FOLDER_OUTPUT), recursive=False)
                observer.start()
                log_monitor_message("Observateur Watchdog redémarré.")

            # Filet de sécurité : un événement inotify peut être manqué sur certains montages réseau/FUSE
            if RESCAN_INTERVAL_SECONDS and (time.monotonic() - last_rescan) >= RESCAN_INTERVAL_SECONDS:
                last_rescan = time.monotonic()
                sweep_existing_exports(event_handler, "Rescan périodique")
    except KeyboardInterrupt:
        log_monitor_message("Arrêt du moniteur demandé par l'utilisateur (Ctrl+C).")
    except Exception as e_main_loop:
        log_monitor_message(
            f"Erreur inattendue dans la boucle principale du moniteur: {type(e_main_loop).__name__} - {e_main_loop}")
    finally:
        log_monitor_message("Arrêt de l'observateur Watchdog...")
        if observer.is_alive():
            observer.stop()
            observer.join(timeout=5)
        # Arrêt propre du pool de threads de traitement
        try:
            event_handler.executor.shutdown(wait=False)
        except Exception as e_executor_shutdown:
            log_monitor_message(f"  AVERTISSEMENT: Erreur lors de l'arrêt du pool de threads: {e_executor_shutdown}")
        log_monitor_message("Moniteur arrêté proprement.")

if __name__ == "__main__":
    try:
        start_monitor()
    except KeyboardInterrupt:
        pass

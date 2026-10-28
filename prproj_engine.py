"""Pilotage de `prproj2ffmpeg.py` (export d'un projet Premiere `.prproj` en MP4 H.264).

Le script vendorisé (`tools/prproj-unpacker/`, voir PROVENANCE.md) est invoqué en
sous-processus — jamais importé : il lit le `.prproj` (gzip interne), construit la
commande ffmpeg (h264_nvenc si disponible, repli libx264) et l'exécute lui-même.
Il termine par « done: <chemin> » (code 0) ou « error: <message> » (code 1).

Ce module remplace l'ancien adaptateur `filmcraft-cli` avec la même philosophie :
- sortie lue en flux (stdout+stderr fusionnés) et journalisée au fil de l'eau ;
- le tampon est découpé sur `\\r` ET `\\n` (ffmpeg rafraîchit ses statistiques avec `\\r`) ;
- la progression est signalée périodiquement (ffmpeg émet des statistiques en continu,
  un déclenchement sur simple inactivité ne se produirait jamais) ;
- l'abandon passe par `ProcessRegistry` (SIGTERM puis SIGKILL sur le groupe de process :
  ffmpeg hérite du groupe du script et est arrêté avec lui).
"""
import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from config import (
    APP_ROOT,
    BASE_SCAN_FOLDER,
    PRPROJ2FFMPEG_BITRATE,
    PRPROJ2FFMPEG_ENCODER,
    PRPROJ2FFMPEG_PYTHON,
    resolve_ffmpeg_binaries,
    resolve_prproj2ffmpeg_script,
)

# Taille maximale du tampon de lecture d'une ligne de sortie (protection mémoire)
_MAX_LINE_BYTES = 1_000_000
# Longueur maximale d'une ligne remontée à l'UI (la commande ffmpeg générée peut être très longue)
_UI_LINE_MAX_CHARS = 300
# Débit audio AAC du script (estimation de progression uniquement)
_AUDIO_KBPS = 320
# Séparateurs de « lignes » : ffmpeg rafraîchit ses statistiques avec \\r
_RECORD_SEP_RE = re.compile(rb'[\r\n]')
# Ligne de statistiques ffmpeg (frame=…, size=…, time=…) : journalisée, jamais envoyée à l'UI
_FFMPEG_STATS_RE = re.compile(
    r'^(frame|fps|size|time|bitrate|speed|out_time|progress|dup_frames|drop_frames|total_size)=')


@dataclass(frozen=True)
class EncodeSettings:
    """Réglages d'encodage passés à prproj2ffmpeg (--bitrate / --encoder)."""
    bitrate: str   # format ffmpeg : "10M", "8000k"…
    encoder: str   # "auto" (= choix du script) | "h264_nvenc" | "libx264" | …

    @classmethod
    def from_config(cls) -> "EncodeSettings":
        return cls(bitrate=PRPROJ2FFMPEG_BITRATE, encoder=PRPROJ2FFMPEG_ENCODER)

    @property
    def encoder_argument(self) -> str | None:
        """Valeur de `--encoder` à passer au script (None = laisser le script choisir)."""
        value = str(self.encoder or '').strip()
        return None if not value or value.lower() == 'auto' else value


class FfmpegProbe(NamedTuple):
    """État de l'environnement ffmpeg détecté au démarrage."""
    ffmpeg: str | None
    ffprobe: str | None
    version: str | None
    has_nvenc: bool

    @property
    def ready(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def describe(self) -> str:
        if not self.ready:
            return "ffmpeg/ffprobe introuvable(s) dans le PATH"
        hardware = "h264_nvenc disponible" if self.has_nvenc else "h264_nvenc absent (repli libx264)"
        return f"ffmpeg {self.version or '?'} · {hardware}"


@dataclass
class ProjectInfo:
    """Métadonnées d'un projet, lues via `prproj2ffmpeg.py --dump` (best effort)."""
    sequence_name: str | None = None
    duration_sec: float | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None


@dataclass
class ExportResult:
    """Résultat d'une exécution de prproj2ffmpeg pour un job."""
    returncode: int | None
    aborted: bool
    duration_seconds: float
    output_path: Path
    output_size: int
    error_lines: list = field(default_factory=list)

    @property
    def output_ready(self) -> bool:
        """Vrai si l'export s'est terminé correctement et que le fichier de sortie est exploitable."""
        return not self.aborted and self.returncode == 0 and self.output_size > 0


class ProcessRegistry:
    """Conserve la référence du process prproj2ffmpeg en cours pour pouvoir l'abandonner depuis /api/stop."""

    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._abort_requested = False

    def start(self, process) -> None:
        with self._lock:
            self._process = process
            self._abort_requested = False

    def finish(self, process) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    @property
    def abort_requested(self) -> bool:
        with self._lock:
            return self._abort_requested

    def abort(self, grace_seconds: float = 5.0) -> bool:
        """
        Demande l'abandon du job en cours : SIGTERM sur le groupe de process, puis SIGKILL
        après le délai de grâce. Retourne True si un process était en cours.
        """
        with self._lock:
            process = self._process
            self._abort_requested = True
        if process is None or process.poll() is not None:
            return False
        _terminate_process_group(process, grace_seconds)
        return True


def _terminate_process_group(process, grace_seconds: float) -> None:
    """Arrête un process lancé avec start_new_session=True (SIGTERM puis SIGKILL sur le groupe)."""
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError):
        return

    def _signal_group(sig) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    _signal_group(signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_group(signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _worker_cwd() -> str:
    """Répertoire de travail du script : la racine scannée (base de repli des médias), sinon l'app."""
    if BASE_SCAN_FOLDER.is_dir():
        return str(BASE_SCAN_FOLDER)
    return str(APP_ROOT)


def resolve_script() -> Path | None:
    """Chemin du script `prproj2ffmpeg.py` (None s'il est introuvable)."""
    return resolve_prproj2ffmpeg_script()


def _nvenc_session_ok(ffmpeg_path: str, timeout: float = 15.0) -> bool:
    """
    Vérifie qu'une session NVENC s'initialise réellement (GPU accessible et libre).

    La seule présence de `h264_nvenc` dans `ffmpeg -encoders` ne prouve rien : le
    wrapper est compilé sur les paquets standards même sans GPU. Un échec de ce
    préflight est volontairement conservateur (repli libx264, export correct mais
    plus lent). Ne lève jamais d'exception.
    """
    try:
        completed = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-v", "error", "-f", "lavfi",
             "-i", "nullsrc=s=64x64:d=0.1", "-frames:v", "1", "-pix_fmt", "yuv420p",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return False
    return completed.returncode == 0


def probe_ffmpeg(timeout: float = 15.0) -> FfmpegProbe:
    """
    Détecte ffmpeg/ffprobe, leur version et la disponibilité réelle de NVENC.

    NVENC n'est déclaré disponible que si le préflight d'initialisation réussit
    (même test que le script d'encodage), et non sur la seule présence de
    l'encodeur compilé. Ne lève jamais d'exception.
    """
    ffmpeg_path, ffprobe_path = resolve_ffmpeg_binaries()
    if not ffmpeg_path:
        return FfmpegProbe(None, ffprobe_path, None, False)

    version = None
    try:
        completed = subprocess.run([ffmpeg_path, "-version"], capture_output=True, text=True, timeout=timeout)
        first_line = (completed.stdout or '').splitlines()[0] if completed.stdout else ''
        match = re.search(r'ffmpeg version (\S+)', first_line)
        version = match.group(1) if match else None
    except Exception:
        pass

    has_nvenc = False
    try:
        completed = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"],
                                   capture_output=True, text=True, timeout=timeout)
        if 'h264_nvenc' in (completed.stdout or ''):
            has_nvenc = _nvenc_session_ok(ffmpeg_path, timeout)
    except Exception:
        pass

    return FfmpegProbe(ffmpeg_path, ffprobe_path, version, has_nvenc)


def parse_bitrate(value: str) -> int:
    """Convertit un débit ffmpeg ("10M", "8000k", "1200000") en bits/s (0 si illisible)."""
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([kKmM]?)$', str(value or '').strip())
    if not match:
        return 0
    factor = {'': 1, 'k': 1_000, 'm': 1_000_000}[match.group(2).lower()]
    return int(float(match.group(1)) * factor)


def build_command(prproj_path: Path, output_path: Path, settings: EncodeSettings,
                  media_bases=(), script_path: Path | None = None) -> list:
    """
    Construit la commande complète d'export d'un `.prproj` vers un MP4.

    `media_bases` : racines supplémentaires pour résoudre les chemins médias du XML
    (le script ajoute lui-même dossier d'entrée, parents et répertoire courant).
    """
    script = script_path or resolve_prproj2ffmpeg_script()
    if script is None:
        raise FileNotFoundError("script prproj2ffmpeg.py introuvable (PRPROJ2FFMPEG_SCRIPT)")

    command = [str(PRPROJ2FFMPEG_PYTHON), str(script), str(prproj_path),
               "--out", str(output_path), "--bitrate", str(settings.bitrate)]
    encoder = settings.encoder_argument
    if encoder:
        command += ["--encoder", encoder]

    seen = set()
    for base in media_bases:
        base = Path(base)
        key = str(base)
        if key in seen or not base.is_dir():
            continue
        seen.add(key)
        command += ["--base", key]
    return command


def read_project_info(prproj_path: Path, script_path: Path, media_bases=(),
                      timeout: float = 120.0) -> ProjectInfo | None:
    """
    Lit les métadonnées du projet (nom de séquence, durée, cadence, taille) via `--dump`.

    Best effort : retourne None si le dump échoue (nommage et encodage n'en dépendent pas ;
    le dump exige lui aussi que le média principal soit résoluble).
    """
    if script_path is None:
        return None
    command = [str(PRPROJ2FFMPEG_PYTHON), str(script_path), str(prproj_path), "--dump"]
    for base in media_bases:
        base = Path(base)
        if base.is_dir():
            command += ["--base", str(base)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=timeout, cwd=_worker_cwd())
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    try:
        model = json.loads(completed.stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(model, dict):
        return None

    info = ProjectInfo()
    if isinstance(model.get('sequence'), str):
        info.sequence_name = model['sequence']
    if isinstance(model.get('duration'), (int, float)):
        info.duration_sec = float(model['duration'])
    if isinstance(model.get('fps'), (int, float)):
        info.fps = float(model['fps'])
    size = model.get('sequence_size')
    if isinstance(size, (list, tuple)) and len(size) == 2:
        try:
            info.width, info.height = int(size[0]), int(size[1])
        except (TypeError, ValueError):
            pass
    return info


def expected_output_bytes(info: ProjectInfo | None, settings: EncodeSettings) -> int | None:
    """Estimation de la taille finale du MP4 (débit vidéo CBR + audio AAC) pour la progression."""
    if info is None or not info.duration_sec:
        return None
    video_bps = parse_bitrate(settings.bitrate) or parse_bitrate(PRPROJ2FFMPEG_BITRATE)
    audio_bps = _AUDIO_KBPS * 1_000
    return int((video_bps + audio_bps) / 8 * info.duration_sec * 0.98) or None


def run_export(prproj_path: Path, output_path: Path, log_path: Path, settings: EncodeSettings,
               script_path: Path, media_bases=(), on_line=None, on_progress=None,
               registry: ProcessRegistry | None = None, progress_interval: float = 5.0) -> ExportResult:
    """
    Exécute un export prproj2ffmpeg pour un `.prproj`.

    - `on_line`     : appelé pour chaque ligne brute du script/ffmpeg (texte décodé) ;
    - `on_progress`: appelé toutes les `progress_interval` secondes (estimation) ;
    - `registry`    : permet d'abandonner le job depuis un autre thread.

    Toutes les lignes brutes sont écrites dans `log_path` (journal complet du job).
    """
    started = time.monotonic()

    try:
        command = build_command(prproj_path, output_path, settings, media_bases, script_path)
    except Exception as exc:
        return ExportResult(returncode=None, aborted=False, duration_seconds=time.monotonic() - started,
                            output_path=output_path, output_size=0,
                            error_lines=[f"commande d'export impossible: {exc}"])

    log_path.parent.mkdir(parents=True, exist_ok=True)
    aborted = False
    lines: list = []
    returncode = None

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            text=False,
            cwd=_worker_cwd(),
            start_new_session=True,
        )
    except Exception as exc:
        return ExportResult(returncode=None, aborted=False, duration_seconds=time.monotonic() - started,
                            output_path=output_path, output_size=0,
                            error_lines=[f"lancement impossible: {exc}"])

    if registry is not None:
        registry.start(process)

    # Journal écrit en flux : une coupure brutale (crash, kill -9) laisse le diagnostic exploitable
    log_handle = None
    try:
        log_handle = open(log_path, 'w', encoding='utf-8', errors='replace')
    except Exception:
        log_handle = None

    def _persist(line: str) -> None:
        if log_handle is not None:
            try:
                log_handle.write(line if line.endswith('\n') else line + '\n')
                log_handle.flush()
            except Exception:
                pass

    try:
        lines.extend(_stream_output(process, on_line, on_progress, progress_interval,
                                    registry if registry is not None else ProcessRegistry(), _persist))
        returncode = process.wait()
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass
        if registry is not None:
            aborted = registry.abort_requested
            registry.finish(process)

    output_size = 0
    try:
        if output_path.exists():
            output_size = output_path.stat().st_size
    except OSError:
        output_size = 0

    error_lines = [line for line in lines if is_error_line(line)]

    return ExportResult(
        returncode=returncode,
        aborted=aborted,
        duration_seconds=time.monotonic() - started,
        output_path=output_path,
        output_size=output_size,
        error_lines=error_lines,
    )


def _stream_output(process, on_line, on_progress, progress_interval: float, registry: ProcessRegistry,
                   on_persist=None) -> list:
    """
    Lit la sortie du script sans bloquer : sélecteur sur le pipe + tampon manuel.

    Le tampon est découpé sur `\\r` et `\\n` (statistiques ffmpeg), et `on_progress` est
    appelé au plus toutes les `progress_interval` secondes pour l'affichage de progression.
    """
    collected: list = []
    stdout = process.stdout
    if stdout is None:
        return collected

    buffered = b""
    fd = stdout.fileno()
    selector = selectors.DefaultSelector()
    last_activity = time.monotonic()
    last_progress = time.monotonic()
    interval = progress_interval if progress_interval and progress_interval > 0 else None
    try:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            events = selector.select(timeout=interval)
            if events:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buffered += chunk
                last_activity = time.monotonic()
                parts = _RECORD_SEP_RE.split(buffered)
                buffered = parts.pop()  # dernier fragment potentiellement incomplet
                for raw_line in parts:
                    _emit_line(raw_line, collected, on_line, on_persist)
                # Protection mémoire : ligne anormalement longue, on la remonte telle quelle
                if len(buffered) > _MAX_LINE_BYTES:
                    _emit_line(buffered, collected, on_line, on_persist)
                    buffered = b""
            if on_progress is not None and interval is not None:
                now = time.monotonic()
                if now - last_progress >= interval:
                    last_progress = now
                    on_progress(now - last_activity)
            if registry.abort_requested and process.poll() is not None:
                break
        if buffered:
            _emit_line(buffered, collected, on_line, on_persist)
    finally:
        try:
            selector.close()
        except Exception:
            pass
        try:
            stdout.close()
        except Exception:
            pass
    return collected


def _emit_line(raw_line: bytes, collected: list, on_line, on_persist=None) -> None:
    text = raw_line.decode('utf-8', errors='replace').strip()
    if not text:
        return
    collected.append(text)
    if on_persist is not None:
        try:
            on_persist(text)
        except Exception:
            pass
    if on_line is not None:
        try:
            on_line(text)
        except Exception:
            pass


def _truncate(text: str, limit: int = _UI_LINE_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + '…'


# Motifs d'erreurs ffmpeg qui remontent jusqu'à l'UI (le reste du bruit technique reste au journal)
_FFMPEG_ERROR_HINTS = (
    'no such file or directory',
    'invalid data found',
    'error while opening',
    'failed to open',
    'could not find codec',
    'conversion failed',
    'unable to find a suitable output format',
    'does not contain any stream',
)


def is_error_line(raw_line: str) -> bool:
    """Vrai si la ligne décrit une erreur (script prproj2ffmpeg ou ffmpeg)."""
    text = str(raw_line).strip().lower()
    if text.startswith('error:'):
        return True
    return any(hint in text for hint in _FFMPEG_ERROR_HINTS)


def summarize_line(raw_line: str) -> str | None:
    """
    Traduit une ligne de sortie de prproj2ffmpeg en résumé français lisible dans l'UI.

    Le script et ffmpeg produisent beaucoup de bruit technique (bannière, flux, statistiques) :
    seules les lignes signifiantes sont remontées en SSE, tout est journalisé dans le fichier.
    Retourne None si la ligne doit rester au journal uniquement.
    """
    text = str(raw_line).strip()
    if not text:
        return None
    lower = text.lower()

    # Lignes du script prproj2ffmpeg
    if text.startswith('error:'):
        detail = text[len('error:'):].strip()
        if detail.startswith('media not found'):
            return (f"ERREUR: média introuvable — {detail} "
                    "(ajoutez la racine des médias à PRPROJ2FFMPEG_EXTRA_BASES)")
        if detail.startswith('ffmpeg failed'):
            return f"ERREUR: ffmpeg a échoué — {detail} (voir le journal complet du job)"
        return f"ERREUR: prproj2ffmpeg — {detail}"
    if text.startswith('done:'):
        return f"✓ Export terminé : {text[len('done:'):].strip()}"
    if text.startswith(('[cache]', '[bbox]', '[encoder]')):
        return f"prproj2ffmpeg: {text}"
    # Ligne de commande générée par le script (elle commence toujours par « ffmpeg -… ») ;
    # la bannière « ffmpeg version … » reste au journal.
    if re.match(r"^(?:[^\s]*/)?ffmpeg(?:\.exe)? -", text):
        return "prproj2ffmpeg: commande ffmpeg générée (détail complet dans le journal du job)"

    # Statistiques de progression ffmpeg : journalisées, masquées de l'UI
    if _FFMPEG_STATS_RE.match(text):
        return None
    if text.startswith('video:'):
        return None

    # Erreurs ffmpeg notables
    if any(hint in lower for hint in _FFMPEG_ERROR_HINTS):
        return f"ERREUR: ffmpeg — {_truncate(text)}"

    # Bruit technique ffmpeg (bannière, flux, configuration…) : journal uniquement
    return None

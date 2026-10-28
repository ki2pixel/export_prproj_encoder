"""Configuration centrale de l'application (version Linux / prproj2ffmpeg).

Toutes les valeurs sont surchargeables par un fichier `.env` situé à la racine du projet.
Cette version remplace Adobe Media Encoder / FilmCraft par `prproj2ffmpeg.py`
(tools/prproj-unpacker/) : les projets Premiere `.prproj` sont exportés en MP4 H.264
via ffmpeg, avec accélération matérielle NVENC (repli logiciel automatique).
"""
import os
import shutil
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore
    # Charge automatiquement un fichier .env s'il est présent à la racine du projet.
    # Le chemin est explicite (racine du projet) pour que le chargement fonctionne
    # quel que soit le répertoire courant d'exécution (ex: lancement depuis un autre dossier).
    _PROJECT_ROOT = Path(__file__).parent
    _ENV_PATH = _PROJECT_ROOT / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=False)
except Exception:
    # python-dotenv non installé ou autre problème: on ignore silencieusement
    pass

APP_ROOT = Path(__file__).parent.resolve()


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in ("true", "1", "yes", "oui")


def _env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else Path(default).expanduser()


def _env_extensions(name: str, default: str) -> tuple:
    """Liste d'extensions (séparées par des virgules) normalisée en tuple, pour `str.endswith()`."""
    raw = os.environ.get(name, default)
    exts = tuple(e.strip().lower() for e in raw.split(",") if e.strip())
    return exts or tuple(e.strip().lower() for e in default.split(",") if e.strip())


def _env_paths(name: str, default: str = "") -> tuple:
    """Liste de chemins séparés par `os.pathsep` (vide = tuple vide)."""
    raw = os.environ.get(name, default)
    return tuple(Path(p.strip()).expanduser() for p in raw.split(os.pathsep) if p.strip())


# --- Chemins principaux ---
# /mnt/cache correspond au disque F: de la version Windows (scanné, jamais écrit par l'app).
BASE_SCAN_FOLDER = _env_path("BASE_SCAN_FOLDER", Path("/mnt/cache"))
FOLDER_KEYWORD = _env_str("FOLDER_KEYWORD", "Camille")
PROJECTS_SUBFOLDER = _env_str("PROJECTS_SUBFOLDER", "projets")
INPUT_EXTENSIONS = _env_extensions("INPUT_EXTENSIONS", ".prproj")

# Watch Folders locaux à l'application (remplacent les watch folders d'AME)
WATCH_FOLDER_INPUT = _env_path("WATCH_FOLDER_INPUT", APP_ROOT / "WATCH_IN")
WATCH_FOLDER_OUTPUT = _env_path("WATCH_FOLDER_OUTPUT", APP_ROOT / "WATCH_OUT")
SOURCE_ARCHIVE_DIRNAME = _env_str("SOURCE_ARCHIVE_DIRNAME", "Source")
STAGING_DIRNAME = _env_str("STAGING_DIRNAME", ".staging")
QUARANTINE_DIRNAME = _env_str("QUARANTINE_DIRNAME", "quarantine")

# Logs, base SQLite, journaux d'export
LOGS_DIRECTORY = _env_path("LOGS_DIRECTORY", APP_ROOT / "logs_watchfolder")
# Journal structuré (une ligne CSV par soumission, jamais tronqué : c'est le registre exploitable)
SUBMISSION_LOG_FILE = _env_path("SUBMISSION_LOG_FILE", LOGS_DIRECTORY / "submission_log.csv")
# Journal texte lisible (messages de déroulement, archivé/roté à chaque soumission)
SUBMISSION_TEXT_LOG_FILE = _env_path("SUBMISSION_TEXT_LOG_FILE", LOGS_DIRECTORY / "submission_log.txt")
MONITOR_LOG_FILE = _env_path("MONITOR_LOG_FILE", LOGS_DIRECTORY / "monitor_log.txt")
ENCODER_LOG_FILE = _env_path("ENCODER_LOG_FILE", LOGS_DIRECTORY / "encoder_log.txt")
JOBS_DB_FILE = _env_path("JOBS_DB_FILE", LOGS_DIRECTORY / "jobs.db")
EXPORT_LOG_DIRECTORY = _env_path("EXPORT_LOG_DIRECTORY", LOGS_DIRECTORY / "exports")

# Sortie
OUTPUT_EXTENSION = _env_str("OUTPUT_EXTENSION", ".mp4")
# Placeholder disponible : {project_file_stem} (nom du fichier .prproj, préfixe numérique,
# suffixes « copy N » et « _assist » retirés ; c'est aussi le nom produit par défaut par
# prproj2ffmpeg).
OUTPUT_FILENAME_FORMAT = _env_str("OUTPUT_FILENAME_FORMAT", "{project_file_stem}")

# --- prproj2ffmpeg (.prproj → MP4 H.264 via ffmpeg) ---
# Script vendorisé (voir tools/prproj-unpacker/PROVENANCE.md) ; chemin surchargeable dans .env.
PRPROJ2FFMPEG_SCRIPT = _env_path("PRPROJ2FFMPEG_SCRIPT", APP_ROOT / "tools" / "prproj-unpacker" / "prproj2ffmpeg.py")
# Interpréteur Python utilisé pour lancer le script (stdlib uniquement) : celui de l'app par défaut.
PRPROJ2FFMPEG_PYTHON = _env_str("PRPROJ2FFMPEG_PYTHON", sys.executable or "python3")
# auto = h264_nvenc si disponible (repli libx264 géré par le script) · ou encodage forcé (h264_nvenc, libx264…)
PRPROJ2FFMPEG_ENCODER = _env_str("PRPROJ2FFMPEG_ENCODER", "auto")
# Débit vidéo CBR (format ffmpeg : 10M, 8000k…)
PRPROJ2FFMPEG_BITRATE = _env_str("PRPROJ2FFMPEG_BITRATE", "10M")
# Bases supplémentaires pour résoudre les chemins médias du XML (séparées par « : »).
# Les bases automatiques sont : racine du projet d'origine, son parent, BASE_SCAN_FOLDER,
# puis dossier du .prproj copié, ses parents et le répertoire courant (ajoutés par le script).
PRPROJ2FFMPEG_EXTRA_BASES = _env_paths("PRPROJ2FFMPEG_EXTRA_BASES", "")

# --- Encodeur (worker) ---
ENCODER_POLL_INTERVAL = _env_int("ENCODER_POLL_INTERVAL", 2)          # secondes
ENCODER_PROGRESS_INTERVAL = _env_int("ENCODER_PROGRESS_INTERVAL", 5)  # secondes
ENCODER_ABORT_GRACE_SECONDS = _env_int("ENCODER_ABORT_GRACE_SECONDS", 5)

# --- Surveillance / déplacement ---
STABLE_FILE_CHECKS = _env_int("STABLE_FILE_CHECKS", 2)
STABLE_FILE_INTERVAL = _env_int("STABLE_FILE_INTERVAL", 5)  # secondes
MOVE_RETRY_COUNT = _env_int("MOVE_RETRY_COUNT", 12)
MOVE_RETRY_DELAY = _env_int("MOVE_RETRY_DELAY", 10)  # secondes
# Rescan périodique du dossier de sortie (filet de sécurité si un événement inotify est perdu) - 0 = désactivé
RESCAN_INTERVAL_SECONDS = _env_int("RESCAN_INTERVAL_SECONDS", 60)
# Délai de protection : un fichier de sortie plus récent que ce délai n'est jamais supprimé
# par l'option « Nettoyer les sorties » (publication/déplacement en cours).
CLEAN_OUTPUT_GUARD_SECONDS = _env_int("CLEAN_OUTPUT_GUARD_SECONDS", 60)

# --- Règles de déplacement et de renommage ---
ALLOW_OVERWRITE = _env_bool("ALLOW_OVERWRITE", False)
ENABLE_FALLBACK_MOVE = _env_bool("ENABLE_FALLBACK_MOVE", False)
ENABLE_HEURISTIC_MATCHING = _env_bool("ENABLE_HEURISTIC_MATCHING", True)

# --- Paramètres de Sécurité et API ---
API_USER = _env_str("API_USER", "admin")
API_PASSWORD = _env_str("API_PASSWORD", "")  # Mot de passe pour l'API (généré au démarrage si vide)
SECRET_KEY = _env_str("SECRET_KEY", "")  # Clef secrète pour signer les cookies de session (générée si vide)
# Remarque : si FLASK_HOST est configuré pour écouter sur le réseau local ou public (ex: 0.0.0.0),
# il est impératif d'utiliser un proxy inverse (reverse proxy) type Nginx pour chiffrer les flux en HTTPS (TLS).
FLASK_HOST = _env_str("FLASK_HOST", "127.0.0.1")
FLASK_PORT = _env_int("FLASK_PORT", 5000)
FLASK_DEBUG = _env_bool("FLASK_DEBUG", False)

# --- Statuts de soumission / d'export (base SQLite + CSV) ---
STATUS_SUBMITTED = "SUBMITTED_TO_WATCHFOLDER"
STATUS_ENCODING = "EXPORT_ENCODING"
STATUS_DONE = "EXPORT_DONE"
# Conservé pour l'historique de la base (l'encodeur actuel ne l'émet plus) et pour le rapprochement.
STATUS_DONE_WITH_WARNINGS = "EXPORT_DONE_WITH_WARNINGS"
STATUS_FAILED = "EXPORT_FAILED"
STATUS_ABORTED = "EXPORT_ABORTED"
# Soumission normale remplacée par la variante `_assist` du même projet : terminal, jamais réencodé.
STATUS_SUPERSEDED = "EXPORT_SUPERSEDED"
STATUS_MOVED = "EXPORT_MOVED_TO_FINAL"
STATUS_MOVE_FAILED = "EXPORT_FAILED_TO_MOVE"

# Statuts pour lesquels un fichier exporté peut encore être rapproché d'un job
# (l'encodeur fait progresser le statut avant que le moniteur ne déplace le fichier).
MATCHABLE_STATUSES = frozenset({
    STATUS_SUBMITTED,
    STATUS_ENCODING,
    STATUS_DONE,
    STATUS_DONE_WITH_WARNINGS,
})


def is_matchable_status(status: str) -> bool:
    """Vrai si un fichier exporté portant ce statut doit encore être déplacé vers sa destination finale."""
    return str(status) in MATCHABLE_STATUSES


def resolve_project_root(file_path: Path) -> Path:
    """
    Détermine de manière robuste le dossier projet racine (ex: /mnt/cache/117 Camille)
    pour un chemin de fichier donné en remontant l'arborescence des parents.
    """
    abs_path = file_path.resolve()
    abs_base = BASE_SCAN_FOLDER.resolve()

    # 1. Remonter jusqu'au dossier situé directement sous BASE_SCAN_FOLDER
    for parent in abs_path.parents:
        if parent.parent == abs_base:
            return parent

    # 2. Fallback 1: Si le dossier 'projets' est présent dans le chemin, prendre son parent
    for parent in abs_path.parents:
        if parent.name == PROJECTS_SUBFOLDER:
            return parent.parent

    # 3. Fallback 2: Comportement d'origine (grand-parent du fichier)
    return file_path.parent.parent


def resolve_prproj2ffmpeg_script() -> Path | None:
    """
    Résout le chemin du script `prproj2ffmpeg.py` (PRPROJ2FFMPEG_SCRIPT ou emplacement vendorisé).
    Retourne None s'il est introuvable.
    """
    candidate = PRPROJ2FFMPEG_SCRIPT
    if not candidate.is_absolute():
        candidate = APP_ROOT / candidate
    if candidate.is_file():
        return candidate
    return None


def resolve_ffmpeg_binaries() -> tuple:
    """
    Résout les binaires `ffmpeg` et `ffprobe` dans le PATH.
    Retourne (chemin ffmpeg | None, chemin ffprobe | None).
    """
    return shutil.which("ffmpeg"), shutil.which("ffprobe")

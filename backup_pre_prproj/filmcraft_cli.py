"""Pilotage du binaire headless `filmcraft-cli` (FilmCraft / FilmScript).

Contrat utilisé (validé sur le test isolé `_xml2mp4`) :

    filmcraft-cli run - --compact --keep-going --save-as <projet.fcproj>

`run -` lit un script JSONL sur stdin (une commande par ligne) ; le résultat de chaque commande
est écrit en JSONL sur stdout. Le projet `.fcproj` est écrit même en cas d'échec d'export.
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

from config import (
    APP_ROOT,
    FILMCRAFT_BITRATE_KBPS,
    FILMCRAFT_BITRATE_MODE,
    FILMCRAFT_FORMAT,
    FILMCRAFT_FPS,
    FILMCRAFT_HW_ENCODING,
    FILMCRAFT_MAX_BITRATE_KBPS,
    FILMCRAFT_PRESET,
    resolve_filmcraft_cli,
)

_HW_DISABLED_VALUES = ("off", "false", "0", "no", "non", "none", "disabled")
# Taille maximale du tampon de lecture d'une ligne de sortie du CLI (protection mémoire)
_MAX_LINE_BYTES = 1_000_000


@dataclass(frozen=True)
class ExportSettings:
    """Réglages d'encodage passés à `file.exportMedia`."""
    preset: str
    format: str
    bitrate_kbps: int
    max_bitrate_kbps: int
    bitrate_mode: str
    fps: int                 # 0 = ne pas forcer la cadence (paramètre omis du script JSONL)
    hardware_encoding: str   # "auto" | "off"

    @classmethod
    def from_config(cls) -> "ExportSettings":
        return cls(
            preset=FILMCRAFT_PRESET,
            format=FILMCRAFT_FORMAT,
            bitrate_kbps=FILMCRAFT_BITRATE_KBPS,
            max_bitrate_kbps=FILMCRAFT_MAX_BITRATE_KBPS,
            bitrate_mode=FILMCRAFT_BITRATE_MODE,
            fps=FILMCRAFT_FPS,
            hardware_encoding=FILMCRAFT_HW_ENCODING,
        )

    @property
    def hardware_enabled(self) -> bool:
        return str(self.hardware_encoding).strip().lower() not in _HW_DISABLED_VALUES


def build_script(xml_path: Path, out_path: Path, settings: ExportSettings) -> str:
    """
    Construit le script JSONL attendu par `filmcraft-cli run -` :
    import du XML → export de la séquence active → compteurs matériels.
    """
    export_params = {
        "path": str(out_path),
        "preset": settings.preset,
        "format": settings.format,
        "bitrateKbps": settings.bitrate_kbps,
        "maxBitrateKbps": settings.max_bitrate_kbps,
        "bitrateMode": settings.bitrate_mode,
    }
    if settings.fps:
        export_params["fps"] = settings.fps
    export_params["hardwareEncoding"] = settings.hardware_encoding
    export_params["wait"] = True

    commands = [
        {"id": "file.import", "params": {"paths": [str(xml_path)]}},
        {"id": "file.exportMedia", "params": export_params},
        {"id": "perf.stats", "params": {}},
    ]
    return "".join(json.dumps(command, ensure_ascii=False) + "\n" for command in commands)


@dataclass
class CommandLogSummary:
    """Synthèse du log JSONL produit par une exécution de filmcraft-cli."""
    commands: list = field(default_factory=list)   # [{'id':…, 'ok':…, 'error':…}]
    import_ok: bool = False
    export_ok: bool = False
    frames: int | None = None
    seconds: float | None = None
    linked_media: int | None = None
    offline_media: list = field(default_factory=list)
    hardware: dict | None = None
    parse_errors: int = 0

    @property
    def errors(self) -> list:
        return [c['error'] for c in self.commands if not c.get('ok') and c.get('error')]

    @property
    def has_offline_media(self) -> bool:
        return bool(self.offline_media)


@dataclass
class ExportResult:
    """Résultat d'une exécution de filmcraft-cli pour un job."""
    returncode: int | None
    aborted: bool
    duration_seconds: float
    output_path: Path
    output_size: int
    summary: CommandLogSummary

    @property
    def output_ready(self) -> bool:
        """Vrai si l'export s'est terminé correctement et que le fichier de sortie est exploitable."""
        return (
            not self.aborted
            and self.returncode == 0
            and self.summary.export_ok
            and self.output_size > 0
        )


class ProcessRegistry:
    """Conserve la référence du process filmcraft-cli en cours pour pouvoir l'abandonner depuis /api/stop."""

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


def resolve_cli() -> str | None:
    """Chemin du binaire headless `filmcraft-cli` (None s'il est introuvable)."""
    return resolve_filmcraft_cli()


def run_export(xml_path: Path, output_path: Path, fcproj_path: Path, log_path: Path,
               settings: ExportSettings, cli_path: str, on_line=None, on_progress=None,
               registry: ProcessRegistry | None = None, progress_interval: float = 5.0) -> ExportResult:
    """
    Exécute un export FilmCraft pour un XML.

    - `on_line`      : appelé pour chaque ligne brute produite par le CLI (log JSONL) ;
    - `on_progress`  : appelé toutes les `progress_interval` secondes sans nouvelle ligne (estimation) ;
    - `registry`     : permet d'abandonner le job depuis un autre thread.

    Les lignes brutes sont écrites dans `log_path` (journal complet), comme le faisait le script bash.
    """
    started = time.monotonic()
    script = build_script(xml_path, output_path, settings)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    aborted = False
    lines: list = []
    returncode = None

    command = [cli_path, "run", "-", "--compact", "--keep-going", "--save-as", str(fcproj_path)]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            text=False,
            cwd=str(APP_ROOT),
            start_new_session=True,
        )
    except Exception as exc:
        return ExportResult(
            returncode=None, aborted=False, duration_seconds=time.monotonic() - started,
            output_path=output_path, output_size=0,
            summary=CommandLogSummary(commands=[{'id': 'process', 'ok': False, 'error': str(exc)}]),
        )

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
        payload = script.encode('utf-8')
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except (BrokenPipeError, OSError) as exc:
            broken = json.dumps({'id': 'stdin', 'ok': False, 'error': f'écriture du script impossible: {exc}'},
                                ensure_ascii=False)
            lines.append(broken)
            _persist(broken)

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

    summary = parse_command_lines(lines)
    output_size = 0
    try:
        if output_path.exists():
            output_size = output_path.stat().st_size
    except OSError:
        output_size = 0

    return ExportResult(
        returncode=returncode,
        aborted=aborted,
        duration_seconds=time.monotonic() - started,
        output_path=output_path,
        output_size=output_size,
        summary=summary,
    )


def _stream_output(process, on_line, on_progress, progress_interval: float, registry: ProcessRegistry,
                   on_persist=None) -> list:
    """
    Lit la sortie du CLI sans bloquer : sélecteur sur le pipe + tampon manuel
    (permet d'appeler on_progress quand l'encodeur n'émet rien pendant plusieurs secondes).

    Le tampon est borné : une ligne unique démesurée est découpée pour ne pas consommer la mémoire.
    """
    collected: list = []
    stdout = process.stdout
    if stdout is None:
        return collected

    buffered = b""
    fd = stdout.fileno()
    selector = selectors.DefaultSelector()
    last_activity = time.monotonic()
    try:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            events = selector.select(timeout=progress_interval if progress_interval > 0 else None)
            if events:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buffered += chunk
                last_activity = time.monotonic()
                while b"\n" in buffered:
                    raw_line, buffered = buffered.split(b"\n", 1)
                    _emit_line(raw_line, collected, on_line, on_persist)
                # Protection mémoire : ligne anormalement longue, on la remonte telle quelle
                if len(buffered) > _MAX_LINE_BYTES:
                    _emit_line(buffered, collected, on_line, on_persist)
                    buffered = b""
            elif on_progress is not None:
                on_progress(time.monotonic() - last_activity)
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
    text = raw_line.decode('utf-8', errors='replace').rstrip('\r')
    if not text.strip():
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


def _import_details(result: dict) -> tuple:
    """
    Extrait (séquences, médias liés, médias hors-ligne) d'un résultat `file.import`.

    Selon le format importé, ces compteurs sont soit à la racine du résultat, soit dans
    `result.documents[]` (cas d'un XML Final Cut Pro : `{"documents":[{"linkedMedia":2,…}]}`).
    """
    documents = result.get('documents')
    if isinstance(documents, list) and documents:
        sequences = 0
        linked = 0
        offline: list = []
        for document in documents:
            if not isinstance(document, dict):
                continue
            if isinstance(document.get('sequences'), int):
                sequences += document['sequences']
            if isinstance(document.get('linkedMedia'), int):
                linked += document['linkedMedia']
            if isinstance(document.get('offlineMedia'), list):
                offline.extend(document['offlineMedia'])
        if sequences or linked or offline:
            return sequences or result.get('sequences'), linked, offline

    linked = result.get('linkedMedia')
    offline = result.get('offlineMedia')
    return result.get('sequences'), linked if isinstance(linked, int) else None, offline if isinstance(offline, list) else []


def parse_command_log(log_path: Path) -> CommandLogSummary:
    """Synthèse d'un journal JSONL écrit par `run_export`."""
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as handle:
            return parse_command_lines(handle.readlines())
    except Exception:
        return CommandLogSummary(parse_errors=1)


def parse_command_lines(lines) -> CommandLogSummary:
    """Synthèse d'un flux de lignes JSONL de filmcraft-cli."""
    summary = CommandLogSummary()
    for line in lines:
        text = str(line).strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            summary.parse_errors += 1
            continue
        if not isinstance(payload, dict):
            summary.parse_errors += 1
            continue

        command_id = payload.get('id')
        summary.commands.append({
            'id': command_id,
            'ok': bool(payload.get('ok')),
            'error': payload.get('error'),
        })
        result = payload.get('result') or {}

        if command_id == 'file.import' and payload.get('ok'):
            summary.import_ok = True
            sequences, linked, offline = _import_details(result)
            if isinstance(linked, int):
                summary.linked_media = linked
            summary.offline_media = offline
        elif command_id == 'file.exportMedia' and payload.get('ok'):
            summary.export_ok = True
            inner = result.get('result') or {}
            if isinstance(inner.get('frames'), int):
                summary.frames = inner['frames']
            if isinstance(inner.get('seconds'), (int, float)):
                summary.seconds = float(inner['seconds'])
        elif command_id == 'perf.stats':
            inner_export = (result.get('export') or {})
            hardware = inner_export.get('hardware')
            if isinstance(hardware, dict):
                summary.hardware = hardware
    return summary


def summarize_command(raw_line: str) -> str | None:
    """
    Traduit une ligne JSONL de filmcraft-cli en résumé français lisible dans l'UI.
    Retourne None si la ligne n'est pas une réponse JSON exploitable.
    """
    text = str(raw_line).strip()
    if not text.startswith('{'):
        return text or None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return text
    if not isinstance(payload, dict):
        return text

    command_id = payload.get('id')
    ok = bool(payload.get('ok'))
    error = payload.get('error')
    result = payload.get('result') or {}

    if command_id == 'file.import':
        if not ok:
            return f"filmcraft: ÉCHEC de l'import XML — {error or 'erreur inconnue'}"
        sequences, linked, offline = _import_details(result)
        message = f"filmcraft: import XML OK — {sequences if sequences is not None else '?'} séquence(s), {linked if linked is not None else '?'} média(s) lié(s)"
        if offline:
            message += f" — ⚠ {len(offline)} média(s) introuvable(s) : {', '.join(str(m) for m in offline[:3])}"
        return message
    if command_id == 'file.exportMedia':
        if not ok:
            return f"filmcraft: ÉCHEC de l'export — {error or 'erreur inconnue'}"
        inner = result.get('result') or {}
        frames = inner.get('frames')
        seconds = inner.get('seconds')
        details = []
        if frames is not None:
            details.append(f"{frames} images")
        if isinstance(seconds, (int, float)):
            details.append(f"en {float(seconds):.1f} s")
        return "filmcraft: export terminé — " + (", ".join(details) if details else str(result.get('path') or 'OK'))
    if command_id == 'perf.stats':
        hardware = ((result.get('export') or {}).get('hardware')) or {}
        if not ok:
            return f"filmcraft: statistiques indisponibles — {error or 'erreur inconnue'}"
        sessions = hardware.get('sessions')
        declined = hardware.get('declined')
        frames = hardware.get('frames')
        if sessions:
            return f"filmcraft: encodage matériel (NVENC) — {sessions} session(s), {frames} images, {declined} refus"
        if declined:
            return f"filmcraft: encodage logiciel — NVENC a refusé {declined} fois ({frames} images)"
        return f"filmcraft: encodage terminé ({frames if frames is not None else '?'} images)"
    if command_id is None:
        return text
    return f"filmcraft: {command_id} — {'OK' if ok else 'ÉCHEC'}{'' if ok else f' ({error})'}"


def list_presets(cli_path: str, timeout: float = 10.0) -> list | None:
    """
    Liste les presets disponibles (`filmcraft-cli export --list-presets`).
    Le CLI répond soit un tableau JSON d'objets (`{"name": …}`), soit une liste texte.
    Retourne None si la commande n'est pas disponible (l'appelant se contente alors d'un avertissement).
    """
    try:
        completed = subprocess.run(
            [cli_path, "export", "--list-presets"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
            start_new_session=True,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None

    output = completed.stdout or ''
    try:
        payload = json.loads(output)
    except (ValueError, TypeError):
        payload = None

    if isinstance(payload, dict):
        payload = payload.get('presets') if isinstance(payload.get('presets'), list) else None
    if isinstance(payload, list):
        names = []
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get('name'), str):
                names.append(item['name'])
            elif isinstance(item, str):
                names.append(item)
        if names:
            return names

    presets = [line.strip() for line in output.splitlines() if line.strip()]
    return presets or None


def _normalize_preset(value: str) -> str:
    text = str(value or '').strip().strip('"\'').strip()
    text = re.sub(r'^\d+[.)]\s*', '', text)          # "1. Nom" -> "Nom"
    text = re.sub(r'\s+', ' ', text)
    return text.casefold()


def preset_is_known(presets, preset: str) -> bool:
    """Vrai si `preset` figure dans la liste retournée par `list_presets` (comparaison tolérante)."""
    if not presets:
        return False
    wanted = _normalize_preset(preset)
    return any(_normalize_preset(candidate) == wanted for candidate in presets)

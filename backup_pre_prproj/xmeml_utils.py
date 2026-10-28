"""Lecture tolérante des timelines XML (xmeml Final Cut Pro 7 / FCPXML) via la bibliothèque standard.

Le nom de la séquence sert à nommer le MP4 final (cf. OUTPUT_FILENAME_FORMAT), la durée estimée
sert uniquement à afficher une progression pendant l'encodage.

ElementTree n'ouvre jamais les DTD externes : la ligne `<!DOCTYPE xmeml>` du format
Final Cut Pro 7 est ignorée sans requête réseau.
"""
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple


class SequenceInfo(NamedTuple):
    """Informations extraites de la timeline XML."""
    name: str | None           # nom de la séquence (ex: "M6 - LMCR - La potée lorraine_9x16")
    duration_frames: int | None  # durée de la séquence en images
    timebase: float | None     # cadence de la timeline (ex: 25.0)


def _localname(tag) -> str:
    """Nom de balise sans son namespace : '{ns}sequence' -> 'sequence'."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit('}', 1)[-1]


def _child_text(parent, wanted_localname: str) -> str | None:
    for child in list(parent):
        if _localname(child.tag) == wanted_localname:
            text = (child.text or "").strip()
            if text:
                return text
    return None


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.match(r'^(\d+)', value.strip())
    return int(match.group(1)) if match else None


def read_sequence_info(xml_path: Path) -> SequenceInfo:
    """
    Extrait (nom, durée en images, cadence) de la première séquence du XML.
    Tolérant : toute erreur de lecture/parsing, ou XML sans séquence, retourne des champs à None
    (l'appelant retombe alors sur le nom de fichier).
    """
    try:
        root = ET.parse(str(xml_path)).getroot()
    except Exception:
        return SequenceInfo(None, None, None)

    sequence = None
    for element in root.iter():
        if _localname(element.tag) == 'sequence':
            sequence = element
            break
    if sequence is None:
        return SequenceInfo(None, None, None)

    # --- Nom : enfant <name> (xmeml), attribut name, puis plus proche ancêtre nommé (FCPXML <project name=…>) ---
    name = _child_text(sequence, 'name')
    if not name:
        attribute = (sequence.get('name') or '').strip()
        if attribute:
            name = attribute
    if not name:
        parents = {child: parent for parent in root.iter() for child in parent}
        current = parents.get(sequence)
        while current is not None:
            attribute = (current.get('name') or '').strip()
            if attribute:
                name = attribute
                break
            current = parents.get(current)

    # --- Durée : enfant <duration> en images (xmeml) ou attribut rationnel (fcpxml "1819/25s") ---
    duration_frames = _parse_int(_child_text(sequence, 'duration'))
    if duration_frames is None:
        attribute = (sequence.get('duration') or '').strip()
        if attribute.endswith('s') and '/' in attribute:
            duration_frames = _parse_int(attribute.split('/', 1)[0])

    # --- Cadence : <rate><timebase> (xmeml) ou attribut frameDuration (fcpxml, ex "1/25s") ---
    timebase = None
    rate = None
    for child in list(sequence):
        if _localname(child.tag) == 'rate':
            rate = child
            break
    if rate is not None:
        timebase = _parse_int(_child_text(rate, 'timebase'))
    if timebase is None:
        frame_duration = (sequence.get('frameDuration') or '').strip()
        match = re.match(r'^\d+/(\d+)s?$', frame_duration)
        if match and int(match.group(1)) > 0:
            timebase = int(match.group(1))

    return SequenceInfo(name, duration_frames, float(timebase) if timebase else None)

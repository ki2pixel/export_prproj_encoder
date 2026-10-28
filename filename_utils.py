"""Utilitaires communs de nettoyage et normalisation de noms de fichiers.

Centralise les heuristiques partagées entre submit_to_watchfolder.py,
encoder_worker.py et monitor_exports_and_move.py pour éviter leur divergence.
"""
import os
import re
import unicodedata

# Caractères interdits par NTFS (et par la plupart des systèmes de fichiers) dans un nom de fichier
_FORBIDDEN_FS_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Suffixe d'une variante technique « assist » d'un projet Premiere : cette variante est prioritaire
# sur la version normale lors du scan, mais ne doit jamais apparaître dans le nom du MP4 final.
ASSIST_SUFFIX = "_assist"
_ASSIST_SUFFIX_RE = re.compile(re.escape(ASSIST_SUFFIX) + r"$", re.IGNORECASE)


def is_assist_stem(stem: str) -> bool:
    """Vrai si le stem (sans extension) se termine par le suffixe `_assist` (insensible à la casse)."""
    return bool(_ASSIST_SUFFIX_RE.search(str(stem or "")))


def strip_assist_suffix(stem: str) -> str:
    """
    Retire le suffixe `_assist` terminal d'un stem (sans extension).
    Ex : "Mon Projet_9x16_assist" -> "Mon Projet_9x16".
    """
    if not stem:
        return ""
    return _ASSIST_SUFFIX_RE.sub("", str(stem))


def sanitize_filename_stem(stem: str) -> str:
    """Nettoie un nom de fichier : espaces/dots superflus, espaces multiples."""
    if not stem:
        return ""
    # Remove trailing/leading spaces and dots
    stem = stem.strip('. ')
    # Replace multiple consecutive dots with a single dot
    stem = re.sub(r'\.+', '.', stem)
    # Remove spaces around dots
    stem = re.sub(r'\s*\.\s*', '.', stem)
    # Replace multiple consecutive spaces with a single space
    stem = re.sub(r'\s+', ' ', stem)
    # Strip again just in case
    stem = stem.strip('. ')
    return stem


def sanitize_for_filesystem(name: str) -> str:
    """
    Rend un nom utilisable comme nom de fichier sur le système de fichiers de destination
    (NTFS en l'occurrence) : remplace les caractères interdits, retire espaces et points finaux.
    Retourne une chaîne vide si plus rien d'exploitable (l'appelant doit alors prévoir un repli).
    """
    if not name:
        return ""
    cleaned = _FORBIDDEN_FS_CHARS_RE.sub('_', str(name))
    cleaned = cleaned.strip(' .')
    return cleaned


def get_base_export_stem(exported_filename_with_ext: str) -> str:
    """
    Retire un éventuel suffixe numérique de version ajouté au nom de fichier
    (ex: "M6 - LMCR - La potée lorraine_9x16_2" -> "M6 - LMCR - La potée lorraine_9x16"),
    sans casser les suffixes qui font partie d'un ratio d'image (9x16, 16x9, 4x5, 1x1).
    """
    stem = os.path.splitext(os.fspath(exported_filename_with_ext))[0]
    match_numeric_suffix = re.match(r"^(.*?)_(\d+)$", stem)
    if match_numeric_suffix:
        base_part = match_numeric_suffix.group(1)
        # Ne pas retirer le suffixe s'il fait partie d'un ratio d'image (ex: 9x16, 16x9, 4x5, 1x1)
        # Dans ce cas, base_part se termine par des chiffres suivis d'un 'x' (ex: '9x' ou '16x').
        if re.search(r"\d+x$", base_part):
            return stem
        return base_part
    return stem


# Identifiant de job de l'application (format "YYYYMMDD_HHMMSS_<6 hex>"), pivot du rapprochement
# entre la soumission, l'encodage et le déplacement final.
JOB_ID_PATTERN = r"\d{8}_\d{6}_[a-f0-9]{6}"
_JOB_ID_RE = re.compile(JOB_ID_PATTERN)
# Job_id en suffixe d'un nom, éventuellement suivi de l'extension : le séparateur qui le précède
# est retiré avec lui ("Mon Projet_20261013_120000_abcdef.prproj" -> "Mon Projet.prproj").
_SUFFIX_JOB_ID_RE = re.compile(rf"[ _-]*{JOB_ID_PATTERN}(?=\.[^./\\]*$|$)")


def strip_job_id(name: str) -> str:
    """
    Retire un éventuel suffixe de job_id d'un nom de fichier ou d'un stem.
    Ex : "Mon Projet_9x16_20261013_120000_abcdef" -> "Mon Projet_9x16"
         "Mon Projet_20261013_120000_abcdef.prproj" -> "Mon Projet.prproj"
    """
    if not name:
        return ""
    return _SUFFIX_JOB_ID_RE.sub('', str(name)).rstrip(' _-')


def extract_job_id(name: str) -> str | None:
    """Retourne le job_id présent dans un nom de fichier, ou None s'il est absent."""
    match = _JOB_ID_RE.search(str(name or ''))
    return match.group(0) if match else None


def slugify_like_ame(text_to_slug: str) -> str:
    """
    Tente d'imiter la "slugification" d'Adobe Media Encoder pour les noms de fichiers.
    Conservé comme filet de sécurité pour le rapprochement heuristique.
    """
    if not text_to_slug:  # Gérer le cas d'une chaîne vide
        return ""

    # 1. Remplacer les caractères accentués courants
    text_to_slug = text_to_slug.replace('é', 'e').replace('ê', 'e').replace('ë', 'e')
    text_to_slug = text_to_slug.replace('É', 'E').replace('Ê', 'E').replace('Ë', 'E')
    text_to_slug = text_to_slug.replace('à', 'a').replace('â', 'a').replace('ä', 'a')
    text_to_slug = text_to_slug.replace('À', 'A').replace('Â', 'A').replace('Ä', 'A')
    text_to_slug = text_to_slug.replace('ô', 'o').replace('ö', 'o')
    text_to_slug = text_to_slug.replace('Ô', 'O').replace('Ö', 'O')
    text_to_slug = text_to_slug.replace('ù', 'u').replace('û', 'u').replace('ü', 'u')
    text_to_slug = text_to_slug.replace('Ù', 'U').replace('Û', 'U').replace('Ü', 'U')
    text_to_slug = text_to_slug.replace('î', 'i').replace('ï', 'i')
    text_to_slug = text_to_slug.replace('Î', 'I').replace('Ï', 'I')
    text_to_slug = text_to_slug.replace('ç', 'c')
    text_to_slug = text_to_slug.replace('Ç', 'C')
    text_to_slug = text_to_slug.replace('œ', 'oe')
    text_to_slug = text_to_slug.replace('Œ', 'OE')

    # 2. Remplacer les tirets spéciaux et apostrophes
    text_to_slug = text_to_slug.replace('–', '-')  # Tiret demi-cadratin en tiret normal
    text_to_slug = text_to_slug.replace('—', '-')  # Tiret cadratin en tiret normal
    text_to_slug = text_to_slug.replace('’', '_')  # Apostrophe typographique devient underscore
    text_to_slug = text_to_slug.replace("'", '_')  # Apostrophe droite devient underscore

    # 3. Remplacer tout ce qui n'est pas alphanumérique ou underscore par un underscore
    #    \w inclut les lettres, chiffres, et l'underscore.
    #    Le + s'assure que plusieurs caractères non valides consécutifs deviennent un seul underscore.
    text_to_slug = re.sub(r'[^\w_]+', '_', text_to_slug, flags=re.UNICODE)

    # 4. Supprimer les underscores multiples (si le sub précédent en a laissé par ex. "_-_" -> "__")
    text_to_slug = re.sub(r'_+', '_', text_to_slug)

    # 5. Supprimer les underscores en début ou fin de chaîne
    text_to_slug = text_to_slug.strip('_')

    return text_to_slug


def normalize_for_matching(name: str) -> str:
    """Normalise un nom de fichier pour la comparaison heuristique (accentuation, casse, séparateurs)."""
    if name is None:
        return ""
    s = str(name).lower()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace('–', '-').replace('—', '-')
    s = s.replace('’', '_').replace("'", '_')
    s = s.replace('-', '_')
    s = re.sub(r'[^\w_]+', '_', s, flags=re.UNICODE)
    s = re.sub(r'_+', '_', s).strip('_')
    return s

"""Soumission des projets Premiere `.prproj` au watchfolder local (version Linux / prproj2ffmpeg).

Le script scanne `BASE_SCAN_FOLDER` à la recherche des dossiers correspondant à `FOLDER_KEYWORD`,
puis copie chaque `*.prproj` de leur sous-dossier `projets` vers `WATCH_FOLDER_INPUT` sous un nom
unique `{stem}_{job_id}.prproj` (copie atomique .tmp → rename, vérifiée). L'encodeur
(`encoder_worker.py`) prend ensuite le relais et lance `prproj2ffmpeg.py` (ffmpeg / NVENC).

Options CLI :
    --force                  : soumet même si l'export final existe déjà à destination
    --clean_source_archive   : vide l'archive des sources traitées (WATCH_IN/Source)
    --clean_outputs          : vide les sorties en attente, le staging et la quarantaine
"""
import argparse
import csv
import datetime
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from config import (
    BASE_SCAN_FOLDER,
    FOLDER_KEYWORD,
    INPUT_EXTENSIONS,
    JOBS_DB_FILE,
    LOGS_DIRECTORY,
    OUTPUT_EXTENSION,
    OUTPUT_FILENAME_FORMAT,
    PRPROJ2FFMPEG_BITRATE,
    PRPROJ2FFMPEG_ENCODER,
    PRPROJ2FFMPEG_SCRIPT,
    PROJECTS_SUBFOLDER,
    CLEAN_OUTPUT_GUARD_SECONDS,
    SOURCE_ARCHIVE_DIRNAME,
    STABLE_FILE_CHECKS,
    STABLE_FILE_INTERVAL,
    STATUS_DONE,
    STATUS_DONE_WITH_WARNINGS,
    STATUS_ENCODING,
    STATUS_SUBMITTED,
    STATUS_SUPERSEDED,
    SUBMISSION_LOG_FILE,
    SUBMISSION_TEXT_LOG_FILE,
    WATCH_FOLDER_INPUT,
    WATCH_FOLDER_OUTPUT,
    resolve_project_root,
)
from db_helper import (
    add_submission,
    get_matchable_submissions,
    init_db,
    update_submission_status_if_current,
)
from filename_utils import (
    is_assist_stem,
    sanitize_filename_stem,
    sanitize_for_filesystem,
    strip_assist_suffix,
    strip_job_id,
)

try:
    LOGS_DIRECTORY.mkdir(parents=True, exist_ok=True)
except Exception as e_mkdir_main:
    print(f"AVERTISSEMENT: Impossible de créer le dossier de logs principal '{LOGS_DIRECTORY}' au démarrage: {e_mkdir_main}")


# --- Fonctions ---

def log_message(message: str, log_file_path: Path = None):
    """Écrit un message dans le journal texte de soumission (et sur la sortie standard)."""
    log_file_path = log_file_path or SUBMISSION_TEXT_LOG_FILE
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    print(log_entry)
    try:
        log_file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file_path, "a", encoding="utf-8", errors='replace') as f:
            f.write(log_entry + "\n")
    except Exception as e:
        print(f"ERREUR: Impossible d'écrire dans le fichier log '{log_file_path}': {e}")


def archive_log_file(log_file_path: Path):
    """
    Archive un fichier de log textuel en le copiant (puis en tronquant l'original)
    afin de ne jamais perdre les données en cas d'échec de l'opération.
    """
    if not log_file_path.exists() or log_file_path.stat().st_size == 0:
        return
    archive_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_log_path = log_file_path.with_name(
        f"{log_file_path.stem}_archive_{archive_timestamp}{log_file_path.suffix}"
    )
    try:
        # Copie d'abord (aucune perte si la copie échoue), puis suppression de l'original.
        shutil.copy2(log_file_path, archive_log_path)
        log_file_path.unlink()
        print(f"INFO: Ancien log de soumission archivé sous: {archive_log_path}")
    except Exception as e_archive:
        print(f"AVERTISSEMENT: Impossible d'archiver l'ancien log '{log_file_path}': {type(e_archive).__name__} - {e_archive}")


def initialize_submission_log_if_needed():
    # 1. Initialisation de la base SQLite
    try:
        init_db(JOBS_DB_FILE)
        print(f"[{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO: Base de données SQLite initialisée à: {JOBS_DB_FILE}")
    except Exception as e:
        print(f"ERREUR CRITIQUE: Impossible d'initialiser la base de données SQLite '{JOBS_DB_FILE}': {e}")

    # 2. Création du dossier du fichier log
    try:
        SUBMISSION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"ERREUR CRITIQUE: Impossible de créer le dossier pour le fichier log '{SUBMISSION_LOG_FILE}': {e}")

    # 3. En-tête CSV si le fichier est nouveau ou vide
    try:
        if SUBMISSION_LOG_FILE.suffix.lower() == ".csv" and (not SUBMISSION_LOG_FILE.exists() or SUBMISSION_LOG_FILE.stat().st_size == 0):
            with open(SUBMISSION_LOG_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "status", "original_file_path", "watchfolder_file_name", "expected_export_name", "job_id"])
    except Exception as e:
        print(f"ERREUR CRITIQUE: Impossible d'initialiser le fichier CSV de soumission '{SUBMISSION_LOG_FILE}': {e}")


def log_submission_action(original_file_path: Path, watchfolder_file_name: str, expected_export_name: str,
                          job_id: str, status: str = STATUS_SUBMITTED):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        add_submission(JOBS_DB_FILE, timestamp, str(original_file_path), watchfolder_file_name, expected_export_name, job_id, status)
        log_message(f"DATABASE: Enregistré dans SQLite - {status} - {original_file_path.name} -> {watchfolder_file_name} (Expected Final Stem: {expected_export_name}, JobId: {job_id})")
    except Exception as e:
        log_message(f"ERREUR: Impossible d'enregistrer la soumission dans SQLite: {e}")

    # Écriture d'une ligne CSV structurée (journal lisible par tableur)
    try:
        SUBMISSION_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(SUBMISSION_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([timestamp, status, str(original_file_path), watchfolder_file_name, expected_export_name, job_id])
    except Exception as e:
        print(f"ERREUR: Impossible d'écrire la ligne CSV dans '{SUBMISSION_LOG_FILE}': {e}")


def get_project_stem_without_prefix_and_copy_info(input_filename_with_ext: str) -> str:
    """
    Retourne le stem d'un nom de projet, sans préfixe numérique, suffixe de copie ni variante `_assist`.
    Ex : "156 Mon Projet copy 22.x.prproj" -> "Mon Projet" · "01 Autre.prproj" -> "Autre"

    Les suffixes de version (`_2`, `_3`…) sont volontairement préservés : ce sont des variantes
    distinctes du projet qui doivent produire des MP4 distincts
    (ex : "M6 - _9x16_2.prproj" -> "M6 - _9x16_2").

    Le suffixe `_assist` est retiré avant puis après le nettoyage `copy N` : la variante assist
    doit toujours produire le même nom final que la version classique
    (ex : "Mon Projet_9x16_assist.prproj" -> "Mon Projet_9x16").
    """
    stem_no_ext = os.path.splitext(os.fspath(input_filename_with_ext))[0]
    # 0. Variante technique « assist » (ex: "Mon Projet_9x16_assist" -> "Mon Projet_9x16")
    stem_no_ext = strip_assist_suffix(stem_no_ext)

    # 1. Suppression du préfixe numérique (ex: "156 ")
    match_prefix = re.match(r"^\d+\s+(.*)", stem_no_ext)
    if match_prefix:
        stem_no_prefix = match_prefix.group(1)
    else:
        stem_no_prefix = stem_no_ext

    # 2. Suppression d'un suffixe " copy [chiffres/point]" (insensible à la casse)
    stem_cleaned = re.sub(r"\s*copy\s*\d*\.?\w?$", "", stem_no_prefix, flags=re.IGNORECASE).strip()

    # 3. Suffixe « assist » placé après le suffixe de copie (ex: "Mon Projet_assist copy 2")
    return strip_assist_suffix(stem_cleaned)


def build_expected_export_stem(source_path: Path) -> str:
    """
    Détermine le stem du fichier final attendu à partir du nom du projet `.prproj`
    (préfixe numérique, suffixe « copy N » et suffixe `_assist` retirés ; suffixes de version
    préservés).

    Aucune lecture du contenu du fichier : c'est le même nom que celui produit par défaut
    par prproj2ffmpeg, et un éventuel suffixe de job_id (dépôt manuel / adoption) est retiré.
    """
    stem_without_job = strip_job_id(source_path.name)
    fallback_stem = get_project_stem_without_prefix_and_copy_info(stem_without_job)

    try:
        raw = OUTPUT_FILENAME_FORMAT.format(project_file_stem=fallback_stem)
    except Exception:
        raw = ''
    if not raw.strip():
        # Repli : nom de fichier nettoyé (jamais de stem vide)
        raw = fallback_stem or strip_assist_suffix(strip_job_id(source_path.stem))

    cleaned = sanitize_for_filesystem(sanitize_filename_stem(raw))
    if not cleaned:
        fallback = strip_assist_suffix(strip_job_id(source_path.stem))
        cleaned = sanitize_for_filesystem(sanitize_filename_stem(fallback)) or fallback or source_path.stem
    return cleaned


def is_safe_path(path: Path, base_dir: Path) -> bool:
    """
    Vérifie de manière sécurisée si le chemin résolu se situe bien sous le dossier de base.
    Exclut également explicitement les liens symboliques.
    """
    try:
        resolved_path = path.resolve()
        resolved_base = base_dir.resolve()

        # Vérifier si c'est un lien symbolique
        if path.is_symlink() or resolved_path.is_symlink():
            return False

        # Vérifier que le chemin résolu est situé sous le chemin de base
        return os.path.commonpath([str(resolved_base), str(resolved_path)]) == str(resolved_base)
    except Exception:
        return False


def select_assist_priority(project_paths: list[Path]) -> tuple[list[Path], list[tuple[Path, Path]]]:
    """
    Applique la priorité de la variante `_assist` d'un projet.

    Retourne `(projets_retenus, projets_ignorés)` ; chaque élément ignoré est un couple
    `(chemin_ignoré, chemin_assist_retenu)`. Le regroupement se fait dans un même dossier :
    `X_assist.prproj` remplace `X.prproj`, mais la variante assist reste exportée sous le nom
    final de `X`. Une assist orpheline (sans version classique) est conservée.
    """
    groups = {}
    for path in project_paths:
        stem_without_job = strip_job_id(path.stem)
        base_stem = (strip_assist_suffix(stem_without_job)
                     if is_assist_stem(stem_without_job) else stem_without_job)
        key = (str(path.parent), base_stem.casefold())
        groups.setdefault(key, []).append(path)

    selected = []
    superseded = []
    for group in groups.values():
        assists = [path for path in group if is_assist_stem(strip_job_id(path.stem))]
        if assists:
            winner = sorted(assists, key=lambda path: path.name.casefold())[0]
            selected.append(winner)
            superseded.extend((path, winner) for path in group if path != winner)
        else:
            selected.extend(group)

    selected.sort()
    superseded.sort(key=lambda pair: pair[0].name.casefold())
    return selected, superseded


def find_projects_to_process(base_scan_folder: Path, folder_keyword: str, force_overwrite: bool):
    """Scanne les dossiers '*<keyword>*' et retourne les projets `.prproj` à soumettre."""
    log_message(f"INFO: Début du scan de '{base_scan_folder}' pour les dossiers '*{folder_keyword}*' (Force: {force_overwrite})")
    prproj_to_submit_list = []
    keyword_lower = folder_keyword.lower()

    log_message(f"DEBUG: Utilisation du scan natif Python pour lister les dossiers à la racine de '{base_scan_folder}'")
    try:
        if not base_scan_folder.exists() or not base_scan_folder.is_dir():
            log_message(f"ERREUR: Le dossier de base '{base_scan_folder}' n'existe pas ou n'est pas un dossier.")
            return []

        found_folders_count = 0

        for entry in base_scan_folder.iterdir():
            try:
                if not (entry.is_dir() and keyword_lower in entry.name.lower()):
                    continue

                # Sécurisation des chemins : ignorer symlinks et traverses
                if not is_safe_path(entry, base_scan_folder):
                    log_message(f"  AVERTISSEMENT: Dossier '{entry.name}' ignoré par sécurité (lien symbolique ou hors limites).")
                    continue

                log_message(f"  Dossier '{entry.name}' correspond. Traitement...")
                found_folders_count += 1
                processed_in_this_folder = False

                projets_dir = entry / PROJECTS_SUBFOLDER
                if projets_dir.exists() and projets_dir.is_dir():
                    if not is_safe_path(projets_dir, base_scan_folder):
                        log_message(f"    AVERTISSEMENT: Dossier '{PROJECTS_SUBFOLDER}' '{projets_dir}' ignoré par sécurité.")
                        continue

                    # Scan non récursif volontaire : seuls les projets du premier niveau de
                    # 'projets' sont des sources finales (les sous-dossiers type
                    # 'Adobe Premiere Pro Auto-Save' contiennent des artefacts de travail ;
                    # les fichiers temporaires de Premiere (*.prproj~, *.prproj.lck)
                    # ne correspondent pas au motif *.prproj).
                    prproj_files = []
                    for extension in INPUT_EXTENSIONS:
                        prproj_files.extend(projets_dir.glob(f"*{extension}"))
                    prproj_files = sorted(set(prproj_files))

                    if prproj_files:
                        log_message(f"    DEBUG: Python a trouvé {len(prproj_files)} projet(s) {'/'.join(INPUT_EXTENSIONS)} dans '{projets_dir}' (scan non récursif, premier niveau uniquement):")

                        # 1. Valider les fichiers avant d'appliquer la priorité _assist : une
                        #    variante assist ignorée par sécurité ne doit jamais masquer la normale.
                        valid_prproj_files = []
                        for prproj_path in prproj_files:
                            if not prproj_path.is_file():
                                log_message(f"      AVERTISSEMENT: Le fichier n'existe plus ou n'est pas un fichier: '{prproj_path}'. Ignoré.")
                                continue

                            if not is_safe_path(prproj_path, base_scan_folder):
                                log_message(f"      AVERTISSEMENT: Fichier '{prproj_path}' ignoré par sécurité (lien symbolique ou hors limites).")
                                continue

                            log_message(f"      DEBUG: Python trouve le fichier via Path: '{prproj_path}'")
                            valid_prproj_files.append(prproj_path)

                        # 2. Priorité au projet `_assist` sur sa version normale.
                        selected_prproj_files, superseded_prproj_files = select_assist_priority(valid_prproj_files)
                        for ignored_path, assist_path in superseded_prproj_files:
                            log_message(f"      INFO: Variante _assist prioritaire : '{assist_path.name}' retenue ; '{ignored_path.name}' ignorée (même projet).")

                        for prproj_path in selected_prproj_files:
                            expected_export_stem = build_expected_export_stem(prproj_path)
                            expected_final_filename = f"{expected_export_stem}{OUTPUT_EXTENSION}"
                            final_destination_folder = resolve_project_root(prproj_path)
                            expected_final_output_file_at_dest = final_destination_folder / expected_final_filename
                            log_message(f"      Vérification de l'export existant (nom final: '{expected_final_filename}') à: '{expected_final_output_file_at_dest}'")

                            if not force_overwrite and expected_final_output_file_at_dest.exists():
                                log_message(f"        SKIP: Export final '{expected_final_output_file_at_dest.name}' existe déjà dans la destination finale.")
                            else:
                                if force_overwrite and expected_final_output_file_at_dest.exists():
                                    log_message(f"        INFO: --force. '{expected_final_output_file_at_dest.name}' sera soumis (existant à la destination).")
                                else:
                                    log_message(f"        À SOUMETTRE: {prproj_path.name}")
                                prproj_to_submit_list.append(prproj_path)
                            processed_in_this_folder = True
                    else:
                        log_message(f"    DEBUG: Aucun projet {'/'.join(INPUT_EXTENSIONS)} dans '{projets_dir}'.")
                else:
                    log_message(f"    DEBUG: Le sous-dossier '{PROJECTS_SUBFOLDER}' n'existe pas dans '{entry}'.")

                if not processed_in_this_folder:
                    log_message(f"    Aucun projet (ou aucun ne nécessitant un export) trouvé/traité dans '{entry}'.")
            except Exception as e_entry:
                log_message(f"    ERREUR lors du traitement de l'entrée '{entry.name}': {e_entry}")

        if found_folders_count == 0:
            log_message(f"AVERTISSEMENT: Aucun dossier contenant '{folder_keyword}' à la racine de '{base_scan_folder}'.")
    except Exception as e_global_scan:
        log_message(f"ERREUR inattendue lors du scan natif de '{base_scan_folder}': {e_global_scan}")

    if not prproj_to_submit_list:
        log_message(f"INFO: Aucun projet à soumettre après le scan de '{base_scan_folder}'.")
    return list(set(prproj_to_submit_list))


def copy_to_watchfolder_root(source_path: Path, watch_folder_input: Path, job_id: str) -> bool:
    """Copie atomique (et vérifiée) d'un projet vers le watchfolder d'entrée sous un nom unique."""
    unique_name = f"{source_path.stem}_{job_id}{source_path.suffix}"
    destination_path = watch_folder_input / unique_name
    tmp_destination_path = destination_path.with_name(destination_path.name + ".tmp")
    try:
        if destination_path.exists():
            log_message(f"  INFO: Fichier '{unique_name}' existe déjà dans '{watch_folder_input}'. Il sera écrasé.")
        # Copie temporaire pour éviter que l'encodeur ne prenne le fichier avant la fin du transfert
        shutil.copy2(source_path, tmp_destination_path)
        # Vérification d'intégrité de la copie (taille) avant le renommage atomique :
        # une copie incomplète (disque plein) ne doit jamais être encodée.
        if not tmp_destination_path.exists():
            raise OSError("La copie temporaire n'a pas été créée")
        source_size = source_path.stat().st_size
        if tmp_destination_path.stat().st_size != source_size:
            raise OSError(
                f"Taille incohérente après copie (source: {source_size} octets, "
                f"copie: {tmp_destination_path.stat().st_size} octets)"
            )
        # Remplacement atomique
        if destination_path.exists():
            destination_path.unlink()
        tmp_destination_path.rename(destination_path)
        log_message(f"  COPIÉ : '{source_path.name}' vers '{watch_folder_input}' sous le nom '{unique_name}' (copie sécurisée et vérifiée)")
        return True
    except Exception as e:
        log_message(f"ERREUR lors de la copie de '{source_path.name}' vers '{watch_folder_input}' : {type(e).__name__} - {e}")
        if tmp_destination_path.exists():
            try:
                tmp_destination_path.unlink()
            except Exception:
                pass
        return False


def supersede_pending_normal_jobs(assist_path: Path):
    """
    Marque comme remplacées les soumissions de la version normale d'un projet dont la variante
    `_assist` vient d'être copiée dans le watchfolder d'entrée.

    L'encodeur archive sans encodage tout job dont le statut est terminal : la normale ne produira
    donc jamais son MP4 et ne pourra pas provoquer une collision `X.mp4` / `X_v1.mp4`.
    Les jobs déjà en cours d'encodage ou déjà encodés ne sont pas modifiés (avertissement seul).
    """
    assist_stem = strip_assist_suffix(strip_job_id(assist_path.stem))
    assist_folder = assist_path.parent.resolve()
    try:
        rows = get_matchable_submissions(JOBS_DB_FILE)
    except Exception as e:
        log_message(f"AVERTISSEMENT: impossible de vérifier les soumissions normales à remplacer : {e}")
        return

    for row in rows:
        status = row.get('Status')
        original_path_value = row.get('OriginalFilePath') or ''
        if not original_path_value:
            continue

        original_path = Path(original_path_value)
        original_stem = strip_job_id(original_path.stem)
        if is_assist_stem(original_stem):
            continue
        if original_stem.casefold() != assist_stem.casefold():
            continue
        try:
            if original_path.parent.resolve() != assist_folder:
                continue
        except Exception:
            continue

        job_id = row.get('JobId')
        if not job_id:
            continue

        if status == STATUS_SUBMITTED:
            # Sécurité : un export normal déjà publié ne doit jamais être bloqué.
            watchfolder_name = row.get('WatchfolderFileName') or ''
            if watchfolder_name:
                output_name = f"{Path(watchfolder_name).stem}{OUTPUT_EXTENSION}"
                if (WATCH_FOLDER_OUTPUT / output_name).exists():
                    log_message(f"AVERTISSEMENT: Sortie normale déjà publiée pour '{original_path.name}' : elle ne sera pas marquée remplacée.")
                    continue
            if update_submission_status_if_current(JOBS_DB_FILE, job_id, STATUS_SUBMITTED, STATUS_SUPERSEDED):
                log_message(f"INFO: Soumission normale '{original_path.name}' (JobId: {job_id}) marquée '{STATUS_SUPERSEDED}' : la variante _assist '{assist_path.name}' est prioritaire.")
            else:
                log_message(f"AVERTISSEMENT: La soumission normale '{original_path.name}' (JobId: {job_id}) a changé de statut entre-temps ; elle n'a pas été remplacée.")
        elif status == STATUS_ENCODING:
            log_message(f"AVERTISSEMENT: La version normale '{original_path.name}' est déjà en cours d'encodage ; la variante _assist ne peut pas la remplacer rétroactivement.")
        elif status in (STATUS_DONE, STATUS_DONE_WITH_WARNINGS):
            log_message(f"AVERTISSEMENT: Un export normal de '{original_path.name}' est déjà prêt/déjà produit ; la variante _assist peut provoquer un versionnage à destination.")


def cleanup_source_archive(watch_folder_input: Path):
    """Vide l'archive des sources traitées (sous-dossier 'Source' du watchfolder d'entrée)."""
    source_archive_dir = watch_folder_input / SOURCE_ARCHIVE_DIRNAME
    if source_archive_dir.exists() and source_archive_dir.is_dir():
        log_message(f"INFO: Nettoyage du dossier d'archive des sources: '{source_archive_dir}'")
        try:
            for item in source_archive_dir.iterdir():
                if item.is_dir():
                    log_message(f"  Suppression du sous-dossier d'archive: '{item}'")
                    shutil.rmtree(item)
                elif item.is_file():
                    log_message(f"  Suppression du fichier d'archive: '{item}'")
                    item.unlink()
            log_message(f"INFO: Nettoyage de '{source_archive_dir}' terminé.")
            return True
        except Exception as e:
            log_message(f"ERREUR lors du nettoyage de '{source_archive_dir}': {e}")
            return False
    else:
        log_message(f"INFO: Dossier d'archive des sources '{source_archive_dir}' non trouvé.")
        return True


def cleanup_output_folder(watch_folder_output: Path):
    """
    Vide les exports publiés (*{OUTPUT_EXTENSION}) à la racine du watchfolder de sortie.

    Ne touche jamais au staging de l'encodeur (job en cours) ni à la quarantaine : un export non
    identifié y est conservé volontairement pour inspection. Les fichiers plus récents que
    CLEAN_OUTPUT_GUARD_SECONDS sont également épargnés (publication/déplacement en cours).
    """
    if not (watch_folder_output.exists() and watch_folder_output.is_dir()):
        log_message(f"INFO: Dossier de sortie '{watch_folder_output}' non trouvé. Aucun nettoyage effectué.")
        return True

    log_message(f"INFO: Nettoyage du dossier de sortie: '{watch_folder_output}' (fichiers *{OUTPUT_EXTENSION} publiés ; staging et quarantaine préservés)")
    cleaned_count = 0
    skipped_count = 0
    error_count = 0
    now = time.time()
    try:
        for item in sorted(watch_folder_output.iterdir()):
            if not (item.is_file() and item.name.lower().endswith(OUTPUT_EXTENSION.lower())):
                continue
            if not is_safe_path(item, watch_folder_output):
                log_message(f"  AVERTISSEMENT: '{item}' ignoré par sécurité.")
                continue
            try:
                age_seconds = now - item.stat().st_mtime
            except OSError:
                age_seconds = 0
            if age_seconds < CLEAN_OUTPUT_GUARD_SECONDS:
                log_message(f"  Conservé (récent, déplacement en cours) : '{item.name}'")
                skipped_count += 1
                continue
            try:
                log_message(f"  Suppression de '{item.name}'...")
                item.unlink()
                cleaned_count += 1
            except Exception as e_unlink:
                log_message(f"  ERREUR lors de la suppression de '{item.name}': {e_unlink}")
                error_count += 1

        if error_count > 0:
            log_message(f"AVERTISSEMENT: {error_count} erreur(s) lors du nettoyage de '{watch_folder_output}'.")
        log_message(f"INFO: Nettoyage de '{watch_folder_output}' terminé. {cleaned_count} fichier(s) supprimé(s), {skipped_count} conservé(s).")
        return True
    except Exception as e:
        log_message(f"ERREUR lors du listage des fichiers dans '{watch_folder_output}' pour nettoyage: {e}")
        return False


# --- Script principal ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Soumet les projets Premiere (.prproj) au watchfolder local encodé par prproj2ffmpeg (ffmpeg/NVENC).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Soumet les projets pour encodage même si un export final existe déjà."
    )
    parser.add_argument(
        "--clean_source_archive",
        action="store_true",
        help=f"Nettoie le dossier d'archive '{SOURCE_ARCHIVE_DIRNAME}' du watchfolder d'entrée avant la copie."
    )
    parser.add_argument(
        "--clean_outputs",
        action="store_true",
        help=f"Nettoie le watchfolder de sortie ({WATCH_FOLDER_OUTPUT}) des fichiers *{OUTPUT_EXTENSION}, du staging et de la quarantaine."
    )
    args = parser.parse_args()

    try:
        LOGS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    except Exception as e_mkdir_main_startup:
        print(f"AVERTISSEMENT: Impossible de garantir la création du dossier de logs '{LOGS_DIRECTORY}' au démarrage pour l'archivage: {e_mkdir_main_startup}")

    # Archivage sûr de l'ancien log de soumission (copie puis suppression, sans perte en cas d'échec)
    archive_log_file(SUBMISSION_TEXT_LOG_FILE)

    initialize_submission_log_if_needed()

    log_message("--- Démarrage du script de soumission au watchfolder (prproj2ffmpeg) ---")
    log_message(f"Configuration: scan de '{BASE_SCAN_FOLDER}' (mot-clé '{FOLDER_KEYWORD}'), extensions {', '.join(INPUT_EXTENSIONS)}")
    log_message(f"             Watchfolder entrée: '{WATCH_FOLDER_INPUT}' / sortie: '{WATCH_FOLDER_OUTPUT}'")
    log_message(f"             Encodage: prproj2ffmpeg '{PRPROJ2FFMPEG_SCRIPT}' → H.264 {PRPROJ2FFMPEG_BITRATE}, encodeur '{PRPROJ2FFMPEG_ENCODER}' (ffmpeg/NVENC)")

    if not WATCH_FOLDER_INPUT.exists() or not WATCH_FOLDER_INPUT.is_dir():
        log_message(f"ERREUR CRITIQUE: Le dossier d'entrée '{WATCH_FOLDER_INPUT}' n'existe pas.")
        sys.exit(1)

    if args.clean_outputs:
        if not WATCH_FOLDER_OUTPUT.exists() or not WATCH_FOLDER_OUTPUT.is_dir():
            log_message(f"AVERTISSEMENT: Le dossier de sortie '{WATCH_FOLDER_OUTPUT}' spécifié pour nettoyage n'existe pas.")

    if args.clean_source_archive:
        cleanup_source_archive(WATCH_FOLDER_INPUT)

    if args.clean_outputs:
        cleanup_output_folder(WATCH_FOLDER_OUTPUT)

    prproj_files_to_submit = find_projects_to_process(BASE_SCAN_FOLDER, FOLDER_KEYWORD, args.force)

    if not prproj_files_to_submit:
        log_message("Aucun projet .prproj à soumettre.")
    else:
        log_message(f"\n{len(prproj_files_to_submit)} projet(s) seront copiés vers le watchfolder d'entrée.")
        projects_successfully_submitted_count = 0
        for prproj_path in prproj_files_to_submit:
            log_message(f"Préparation de la soumission pour : {prproj_path.name}")

            job_id = f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
            success = copy_to_watchfolder_root(prproj_path, WATCH_FOLDER_INPUT, job_id)
            if success:
                projects_successfully_submitted_count += 1
                expected_export_stem_for_log = build_expected_export_stem(prproj_path)
                unique_watchfolder_name = f"{prproj_path.stem}_{job_id}{prproj_path.suffix}"
                log_submission_action(prproj_path, unique_watchfolder_name, expected_export_stem_for_log, job_id)
                if is_assist_stem(strip_job_id(prproj_path.stem)):
                    supersede_pending_normal_jobs(prproj_path)

        log_message("\n--- Soumission terminée ---")
        if projects_successfully_submitted_count > 0:
            log_message(f"{projects_successfully_submitted_count} projet(s) copié(s) vers '{WATCH_FOLDER_INPUT}'.")
        else:
            log_message("Aucun nouveau projet n'a été copié (soit déjà exportés/destination existe, soit une erreur de copie).")
        log_message(f"L'encodeur prproj2ffmpeg devrait maintenant traiter les fichiers depuis '{WATCH_FOLDER_INPUT}'.")

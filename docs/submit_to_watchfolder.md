# submit_to_watchfolder.py – soumission des projets Premiere (.prproj)

Script lancé en sous-processus par l'application (et utilisable seul en ligne de commande).

```bash
.venv/bin/python submit_to_watchfolder.py [--force] [--clean_source_archive] [--clean_outputs]
```

## Déroulement

1. **Archivage du journal texte** : `submission_log.txt` est copié puis supprimé
   (`submission_log_archive_AAAAMMJJ_HHMMSS.txt`) — copie d'abord, suppression ensuite, pour ne rien
   perdre en cas d'échec. Le registre `submission_log.csv` n'est jamais tronqué.
2. **Initialisation** : base SQLite (`init_db`) et en-tête CSV si le fichier est absent/vide.
3. **Nettoyages optionnels** :
   - `--clean_source_archive` vide `WATCH_IN/Source/` (projets déjà traités) ;
   - `--clean_outputs` vide les `*.mp4` **publiés** à la racine de `WATCH_OUT`, en préservant le
     staging de l'encodeur, la quarantaine et tout fichier plus récent que
     `CLEAN_OUTPUT_GUARD_SECONDS` (protection d'une publication en cours).
4. **Scan** de `BASE_SCAN_FOLDER` : dossiers dont le nom contient `FOLDER_KEYWORD`
   (insensible à la casse), sous-dossier `PROJECTS_SUBFOLDER` (`projets`), **non récursif**, pour
   chaque extension de `INPUT_EXTENSIONS` (`.prproj` par défaut). Les sous-dossiers de travail
   (`Adobe Premiere Pro Auto-Save`… ) sont donc ignorés par construction, tout comme les fichiers
   temporaires de Premiere (`*.prproj~`, `*.prproj.lck`), qui ne correspondent pas au motif.
   Chaque chemin est validé par `is_safe_path()` (rejet des liens symboliques et de toute sortie de
   l'arborescence scannée).
   - **Priorité `_assist`** : dans un même dossier, si `X_assist.prproj` et `X.prproj` coexistent,
     seule la variante `_assist` est retenue (le fichier normal ignoré est journalisé). Une assist
     orpheline, sans version normale, est conservée et exportée normalement.
5. **Nom final attendu** (`build_expected_export_stem`) — aucune lecture du contenu du fichier :
   - le nom du fichier `.prproj` est nettoyé par `strip_job_id()` (retrait d'un éventuel suffixe
     `_{job_id}`, ex. après adoption) puis `get_project_stem_without_prefix_and_copy_info()`
     (préfixe numérique, suffixe `copy N` et suffixe technique `_assist` retirés ; les suffixes de
     version `_2`, `_3`… et les ratios d'image `_9x16` sont **conservés**) ;
   - la valeur est passée à `OUTPUT_FILENAME_FORMAT` (placeholder `{project_file_stem}`), puis
     nettoyée par `sanitize_filename_stem()` et `sanitize_for_filesystem()` (caractères interdits
     par NTFS, points/espaces finaux) ;
   - repli : stem brut du fichier si le nettoyage donne un résultat vide.
   C'est le même nom que celui produit par défaut par `prproj2ffmpeg` (`--out` à côté du projet).
6. **Décision** : si le MP4 final existe déjà à destination
   (`resolve_project_root(prproj) / nom_attendu.mp4`) le projet est ignoré, sauf `--force`.
7. **Copie atomique** (`copy_to_watchfolder_root`) vers `WATCH_IN` sous le nom
   `{stem}_{job_id}.prproj` : copie en `.tmp`, vérification de la taille, puis `rename()`.
   Le `job_id` a le format `AAAAMMJJ_HHMMSS_ffffff` (6 caractères hexadécimaux) — c'est la clé de
   rapprochement utilisée ensuite par l'encodeur et le moniteur.
8. **Garde-fou file d'attente** : après la copie d'une variante `_assist`, les soumissions de la
   version normale encore au statut `SUBMITTED_TO_WATCHFOLDER` sont marquées `EXPORT_SUPERSEDED`
   (transition conditionnelle en base). L'encodeur les archive sans les encoder, ce qui évite un
   `X.mp4` suivi de `X_v1.mp4`. Une normale déjà `EXPORT_ENCODING` ou `EXPORT_DONE` n'est jamais
   modifiée : un avertissement est journalisé.
9. **Journalisation** : `add_submission()` en base + ligne CSV structurée + messages dans le journal
   texte.

## Journaux produits

| Fichier | Contenu |
|---|---|
| `logs_watchfolder/submission_log.csv` | registre structuré : `timestamp,status,original_file_path,watchfolder_file_name,expected_export_name,job_id` |
| `logs_watchfolder/submission_log.txt` | journal texte de déroulement (scanné par `/api/logs/history`) |
| `logs_watchfolder/jobs.db` | table `submissions` (source de vérité des statuts) |

## Messages clés

- `SKIP: Export final '…' existe déjà dans la destination finale.` — rien à faire.
- `INFO: Variante _assist prioritaire : '…_assist.prproj' retenue ; '….prproj' ignorée` — priorité appliquée.
- `INFO: Soumission normale '…' (JobId: …) marquée 'EXPORT_SUPERSEDED'` — l'ancienne normale ne sera pas encodée.
- `COPIÉ : '…' vers '…' sous le nom '…' (copie sécurisée et vérifiée)` — soumission réussie.
- `AVERTISSEMENT: … ignoré par sécurité` — lien symbolique ou chemin hors limites.
- `ERREUR CRITIQUE: Le dossier d'entrée '…' n'existe pas.` — code de sortie 1.

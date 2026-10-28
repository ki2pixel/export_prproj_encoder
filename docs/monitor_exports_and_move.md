# monitor_exports_and_move.py – déplacement des exports vers leur destination finale

Thread daemon lancé par `app.py` (utilisable seul : `.venv/bin/python monitor_exports_and_move.py`).
Il surveille `WATCH_OUT` et rapproche chaque MP4 de sa soumission.

## Surveillance

- **Watchdog** asynchrone (`on_created` + `on_moved`, observer non récursif) : la détection ne bloque
  jamais ; chaque événement est délégué à un pool de threads borné (`MAX_PROCESSING_THREADS = 8`)
  avec déduplication par chemin (`processing_paths`).
- **Balayage initial** au démarrage : les `*.mp4` déjà présents à la racine de `WATCH_OUT` sont
  traités (reprise après un arrêt entre la publication et le déplacement).
- **Rescan périodique** (`RESCAN_INTERVAL_SECONDS`, 60 s par défaut, 0 = désactivé) : filet de
  sécurité si un événement inotify est perdu (montages FUSE/réseau).
- **Stabilisation** avant tout traitement : `STABLE_FILE_CHECKS` × `STABLE_FILE_INTERVAL` (2 × 5 s) de
  taille inchangée, plafonné à 7 tentatives. Elle est **sautée pour les exports publiés par
  l'encodeur** : leur nom contient un `job_id` et la base les donne au statut `EXPORT_DONE*`, ce qui
  prouve qu'ils ont été publiés par `os.replace` depuis `.staging` après vérification de taille — le
  fichier est donc complet dès sa première apparition (mesuré : **~1,2 s** au lieu de ~12 s avant
  déplacement). Tout autre fichier (dépôt manuel, copie réseau, job encore en cours) suit le chemin
  normal avec stabilisation, puis quarantaine s'il n'est rapproché d'aucune soumission.
- Les sous-dossiers techniques `.staging/` et `quarantine/` sont explicitement ignorés, tout comme
  les fichiers temporaires (`.tmp`, `.part`, `~`, préfixe `._`, nom en GUID).

## Rapprochement

1. **Par `job_id`** (déterministe) : le nom du fichier contient
   `AAAAMMJJ_HHMMSS_ffffff` ; `get_submission_by_job_id()` retrouve la ligne SQLite. Le fichier est
   retenu si le statut est *appariable* (`is_matchable_status()` :
   `SUBMITTED_TO_WATCHFOLDER`, `EXPORT_ENCODING`, `EXPORT_DONE`, `EXPORT_DONE_WITH_WARNINGS`) — un
   export déjà déplacé (`EXPORT_MOVED_TO_FINAL`) n'est pas retraité.
2. **Heuristique** (filet de sécurité, `ENABLE_HEURISTIC_MATCHING=True`) : comparaison du stem du
   fichier aux candidats issus des soumissions en attente (`{stem}_export`, variante « slugifiée »,
   stem du fichier déposé dans `WATCH_IN`, stem attendu) avec normalisation (accents, casse,
   séparateurs). Une correspondance heuristique est journalisée en majuscules : c'est le signe d'un
   nommage inattendu, à vérifier.
3. **Aucune correspondance** :
   - `ENABLE_FALLBACK_MOVE=False` (défaut) : le fichier part en **quarantaine**
     (`WATCH_OUT/quarantine/`, versionné s'il y a collision) pour inspection ;
   - `ENABLE_FALLBACK_MOVE=True` : déplacement vers le dossier projet le plus récent (préfixe
     numérique décroissant puis date de modification), avec versionnage obligatoire.

## Déplacement vers la destination finale

- Destination : `resolve_project_root(row['OriginalFilePath'])` (dossier du projet, ex.
  `/mnt/cache/117 Camille`), nom final `{ExpectedExportName}{OUTPUT_EXTENSION}` (le suffixe
  technique `_assist` est retiré du nom final).
- **Collision** : `ALLOW_OVERWRITE=False` (défaut) ⇒ versionnage `_v1`, `_v2`… ; `ALLOW_OVERWRITE=True`
  ⇒ écrasement atomique (le remplacement se fait par `os.replace`, la cible n'est jamais absente).
- **`move_to_final_verified()`** :
  - même système de fichiers ⇒ `os.replace()` (atomique, instantané) ;
  - systèmes de fichiers différents (ext4 → NTFS/FUSE) ⇒ copie vers `.<nom>.part` **sur la
    destination**, vérification de la taille, `os.replace()` (donc jamais de fichier partiel visible
    sous son nom final), puis suppression de la source. Le `.part` est nettoyé en cas d'échec.
- **Réessais** : `MOVE_RETRY_COUNT` × `MOVE_RETRY_DELAY` (12 × 10 s) sur `PermissionError`/`OSError`
  (fichier verrouillé, disque lent).
- **Statuts** : `EXPORT_MOVED_TO_FINAL` en cas de succès, `EXPORT_FAILED_TO_MOVE` après épuisement
  des réessais (mise à jour par `job_id`, avec repli sur la ligne complète).

## Journal

`logs_watchfolder/monitor_log.txt` (rotation au-delà de 5 Mo, préfixe `(Monitor)` dans l'UI).

## Points vérifiés en exploitation

- Un MP4 publié pendant que le moniteur est arrêté est bien déplacé au redémarrage (balayage initial).
- La collision avec un export manuel existant produit `…_v1.mp4` sans écraser l'original.
- Le déplacement inter-systèmes (ext4 → `/mnt/cache`) n'expose jamais un fichier tronqué : en cas de
  coupure, seul un `.<nom>.part` peut subsister (supprimé automatiquement).

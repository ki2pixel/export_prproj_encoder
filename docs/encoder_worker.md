# encoder_worker.py – encodage prproj2ffmpeg (remplace Adobe Media Encoder)

Thread daemon lancé par `app.py` (testable seul : `.venv/bin/python encoder_worker.py [--once]`).
Il joue le rôle qu'occupait AME entre le watchfolder d'entrée et le watchfolder de sortie.

## Boucle de fonctionnement

1. **Résolution du script et de ffmpeg** : `resolve_script()` localise
   `tools/prproj-unpacker/prproj2ffmpeg.py` (surchargeable via `PRPROJ2FFMPEG_SCRIPT`), puis
   `probe_ffmpeg()` vérifie ffmpeg/ffprobe dans le PATH, la version, la présence de `h264_nvenc`
   et son initialisation réelle (préflight sur une trame nulle).
   Tant que le script est introuvable, la file n'est pas traitée (erreur journalisée une seule fois,
   état exposé dans `/api/status` sous `encoder_available` / `encoder_engine`).
2. **File d'attente par scrutation** de `WATCH_IN` (`ENCODER_POLL_INTERVAL`, 2 s par défaut) :
   choix délibéré pour une file de travail, insensible aux événements manqués et sans dépendance à
   inotify sur un montage réseau/FUSE. Les fichiers sont triés par date de modification (FIFO), les
   `.tmp`/`.part`/fichiers cachés sont ignorés.
3. **Adoption** (`_ensure_submission`) : un projet déposé à la main dans `WATCH_IN` est enregistré
   en base sous un nom unique contenant un `job_id`, puis encodé — équivalent du dépôt manuel d'un
   `.proj` dans le watchfolder AME.
4. **Reprise** : au démarrage (et à chaque tour), un job dont le statut est
   `SUBMITTED_TO_WATCHFOLDER` ou `EXPORT_ENCODING` (orphelin d'un crash) est réencodé. Un job dont la
   sortie existe déjà dans `WATCH_OUT` est simplement archivé (pas de réencodage), et un job à
   statut terminal (`EXPORT_FAILED`, `EXPORT_ABORTED`, `EXPORT_SUPERSEDED`,
   `EXPORT_MOVED_TO_FINAL`…) n'est **jamais** réencodé en boucle : son projet est archivé, une
   nouvelle soumission créant un nouveau `job_id`.
5. **Encodage séquentiel** (une seule session NVENC à la fois).
6. **Nettoyages au démarrage** : `clean_staging_leftovers()` (fichiers partiels de `.staging`) et
   `clean_input_leftovers()` (XML temporaire `.prproj2ffmpeg-tmp.xml` laissé dans `WATCH_IN` par
   une **ancienne** version de prproj2ffmpeg). Le script actuel décompresse le `.prproj` en
   mémoire et n'écrit plus aucun fichier temporaire ; ce nettoyage ne traite que les résidus
   historiques d'un arrêt brutal.

## Encodage d'un job (`_process_job`)

- Statut → `EXPORT_ENCODING` ; métadonnées lues en best effort (`prproj_engine.read_project_info`,
  mode `--dump` du script) : nom de séquence, durée, cadence, taille — utilisées pour les logs et
  l'estimation de progression.
- Sortie écrite dans `WATCH_OUT/.staging/{stem}_{job_id}.mp4` : le moniteur ne voit donc jamais un
  fichier partiellement encodé.
- Commande exécutée (via `prproj_engine.run_export`, `start_new_session=True`) :

  ```bash
  <python> tools/prproj-unpacker/prproj2ffmpeg.py \
      WATCH_IN/<stem>_<job_id>.prproj \
      --out WATCH_OUT/.staging/<stem>_<job_id>.mp4 \
      --bitrate 10M \
      --base <racine du projet d'origine> --base <son parent> --base /mnt/cache
      # + --encoder <h264_nvenc|libx264> uniquement si forcé dans .env
  ```

  Le script construit lui-même la commande ffmpeg (H.264 CBR, audio AAC 320 kbps, `+faststart`) et
  la journalise ; il replie automatiquement sur `libx264` si `h264_nvenc` est indisponible.
- La sortie (script + ffmpeg fusionnés) est lue sans bloquer (sélecteur + tampon borné, découpé sur
  `\r` **et** `\n` car ffmpeg rafraîchit ses statistiques avec `\r`) : tout est écrit **en flux**
  dans `logs_watchfolder/exports/<stem>_<job_id>.log`, et les lignes signifiantes (erreurs, phases
  de cache, fin d'export) sont traduites en résumé français dans l'UI (`summarize_line`) — les
  statistiques `frame=…` et le bruit ffmpeg restent au journal.
- Toutes les `ENCODER_PROGRESS_INTERVAL` secondes, une ligne de progression est émise (taille
  écrite et pourcentage estimé d'après le débit CBR et la durée du projet).
- **Succès** (`rc=0` et fichier non vide) : `os.replace()` du staging vers
  `WATCH_OUT/{stem}_{job_id}.mp4` (même système de fichiers, donc atomique), puis statut
  `EXPORT_DONE`.
- **Échec** : statut `EXPORT_FAILED`, fichier partiel supprimé, journal complet conservé pour
  diagnostic (message d'erreur du script — ex. « média introuvable » — repris dans `last_error`).
- **Dans tous les cas** : le projet est déplacé vers `WATCH_IN/Source/<horodatage>/` (équivalent de
  l'archive source d'AME) — c'est ce qui évite toute boucle de réencodage.

## Pause, abandon, arrêt

- `/api/stop` appelle `pause_and_abort()` : l'encodeur passe en pause (les projets restent dans
  `WATCH_IN`) et le job en cours est abandonné via `ProcessRegistry.abort()` — `SIGTERM` sur le
  groupe de process (le script est lancé dans sa propre session, ffmpeg en hérite), puis `SIGKILL`
  après `ENCODER_ABORT_GRACE_SECONDS` (5 s). `clean_staging_leftovers()` purge les fichiers partiels
  au démarrage suivant.
- `/api/start` appelle `resume()`.
- À l'extinction de l'application, `atexit` appelle `shutdown()` (même abandon) : aucun process
  ffmpeg orphelin.

## État exposé (`snapshot()`)

`paused`, `current_job`, `current_file`, `current_started_at`, `queue_pending`, `processed`,
`failed`, `aborted`, `last_error`, `encoder_available`, `encoder_engine` — consommé par
`/api/status` (ligne « Encodeur » de l'UI ; `encoder_engine` détaille ffmpeg/version/NVENC au survol).

## Journal

`logs_watchfolder/encoder_log.txt` (rotation au-delà de 5 Mo, préfixe `(Encoder)` conservé dans l'UI
pour le coloriage).

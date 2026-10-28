# 🎬 Application Flask - Submit to Watchfolder (prproj2ffmpeg / Linux)

Version Linux de l'application d'export automatisé : elle scanne les projets **Adobe Premiere Pro**
(`.prproj`) des dossiers projets, les soumet à un watchfolder local encodé par **prproj2ffmpeg**
(ffmpeg, accélération matérielle NVENC avec repli logiciel) et déplace les MP4 terminés dans le
dossier du projet d'origine.

Elle remplace Adobe Media Encoder (version Windows, `F:\export_media_encoder`) : mêmes fonctions,
mêmes statuts, même interface — l'encodage passe désormais par `tools/prproj-unpacker/prproj2ffmpeg.py`
(voir `PROVENANCE.md`), qui lit le `.prproj` directement. La version Windows reste utilisable telle
quelle (ne jamais faire tourner les deux sur les mêmes projets en même temps).

## Fonctionnalités

- **Interface web** : panneau de contrôle, options de soumission, console de logs temps réel (SSE).
- **Soumission** : scan de `/mnt/cache/*Camille*/projets/*.prproj` ; la variante `_assist.prproj`
  est prioritaire sur la version normale et produit le même MP4 (`X_assist.prproj` → `X.mp4`).
  Le nom final est déduit du fichier `.prproj` (préfixe numérique, « copy N » et `_assist` retirés,
  suffixes de version conservés), copie atomique vers le watchfolder, journalisation SQLite + CSV.
- **Encodeur prproj2ffmpeg** : file d'attente séquentielle (une session NVENC à la fois), sortie
  H.264 CBR 10 Mbps + audio AAC 320 kbps, à la cadence de la séquence, animations/calques alpha du
  projet fidèlement reproduits (keyframes Motion, overlays, **masques rectangle/ellipse**,
  **Gaussian Blur** par clip), repli logiciel `libx264` automatique, progression estimée dans les
  logs.
- **Moniteur d'exports** : surveillance du dossier de sortie, rapprochement déterministe par
  `job_id`, déplacement/renommage vers le dossier projet, versionnage des collisions
  (`_v1`, `_v2`), quarantaine si aucune correspondance, réessais en cas de verrou.
- **Robustesse** : sortie en staging puis publication atomique (jamais de fichier partiel exposé),
  reprise automatique des jobs interrompus (crash/redémarrage), rescan périodique (filet de
  sécurité inotify), déplacement inter-systèmes de fichiers vérifié (`.part` + renommage atomique).
- **Sécurité** : authentification par session, protection CSRF, rate-limiting du login, CSP,
  `/api/health` limité à localhost.

## Installation

```bash
cd ~/export_prproj_encoder
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env    # puis ajuster BASE_SCAN_FOLDER, PRPROJ2FFMPEG_*, secrets…
```

Prérequis : Python 3.10+, `ffmpeg` ≥ 4.2 et `ffprobe` dans le PATH (un GPU NVIDIA est recommandé :
l'encodage NVENC est bien plus rapide, sinon repli automatique `libx264`) et une arborescence de
projets `/mnt/cache/<NNN Camille>/projets/*.prproj`.

## Lancement

```bash
./run_app.sh                # premier plan (Ctrl+C pour arrêter)
./run_app.sh --background   # arrière-plan + attente de /api/health + ouverture du navigateur
./stop_app.sh               # arrêt propre (encodeur, moniteur, serveur) — --force si nécessaire
```

Si l'application tourne déjà, `run_app.sh` se contente de rouvrir la page (aucun doublon de serveur) ;
en cas d'échec de démarrage en arrière-plan, une notification de bureau est affichée. `stop_app.sh`
cible l'instance qui écoute sur `FLASK_PORT` (et vérifie que c'est bien cette application) ;
`stop_app.sh --all` arrête en plus toute autre instance.

### Raccourci de bureau

```bash
./install_shortcut.sh              # crée « Export MP4 (Camille) » sur le Bureau + dans le menu Applications
./install_shortcut.sh --uninstall  # retire les raccourcis
```

Le raccourci (icône `static/icon.svg`) lance `run_app.sh --background` : double-clic ⇒ serveur en
arrière-plan et interface ouverte dans le navigateur. Sur GNOME, autoriser le lancement au premier
double-clic si la boîte de dialogue le demande.

Puis http://localhost:5050 (ou le port configuré dans `.env` ; utilisateur `admin`, mot de passe `API_PASSWORD` du `.env`).
Au démarrage, l'encodeur et le moniteur d'exports sont lancés automatiquement (threads daemon) ;
les jobs encore en attente dans `WATCH_IN` sont repris.

## Utilisation

1. Exporter/sauvegarder le projet Premiere `.prproj` dans `/mnt/cache/<projet>/projets/`.
2. Cocher les options si nécessaire :
   - **Forcer l'écrasement** : soumet même si un MP4 du même nom existe déjà à destination ;
   - **Nettoyer l'archive des sources** : vide `WATCH_IN/Source/` (projets traités) ;
   - **Nettoyer les sorties** : vide les MP4 publiés en attente de déplacement (le staging, la
     quarantaine et les fichiers en cours de traitement sont préservés).
3. « Démarrer le script » : la soumission copie les `.prproj` dans `WATCH_IN`, l'encodeur les
   traite, le moniteur déplace chaque MP4 dans `/mnt/cache/<projet>/`.
4. « Arrêter » : stoppe la soumission et met l'encodeur en pause (le job en cours est abandonné,
   les projets en attente restent dans `WATCH_IN` et seront repris au prochain démarrage).

Il est aussi possible de déposer directement un `.prproj` dans `WATCH_IN` (avec ou sans `job_id`) :
l'encodeur l'« adopte » (enregistrement en base) puis l'encode.

## Structure du projet

```
export_prproj_encoder/
├── app.py                        # UI Flask + SSE + démarrage de l'encodeur et du moniteur
├── config.py                     # configuration (.env), statuts, résolution du script/ffmpeg
├── db_helper.py                  # SQLite : soumissions et statuts d'export
├── filename_utils.py             # nettoyage/normalisation des noms, job_id
├── prproj_engine.py              # pilotage de prproj2ffmpeg (commande, progression, abandon)
├── submit_to_watchfolder.py      # scan + soumission (sous-processus)
├── encoder_worker.py             # file d'attente + encodage (thread)
├── monitor_exports_and_move.py   # surveillance du dossier de sortie + déplacement final
├── tools/prproj-unpacker/        # prproj2ffmpeg.py vendorisé (+ PROVENANCE.md, LICENSE)
├── templates/ static/            # interface web
├── WATCH_IN/  WATCH_OUT/         # watch folders locaux (Source/, .staging/, quarantine/)
├── logs_watchfolder/             # jobs.db, submission_log.csv/.txt, monitor_log.txt, encoder_log.txt, exports/
├── fixtures/                     # fixtures réelles pour les tests (projet .prproj)
├── test_app.py test_watchdog.py  # tests unitaires (python -m unittest discover)
├── run_app.sh                    # lanceur Linux
├── stop_app.sh                   # arrêt propre de l'application
├── install_shortcut.sh           # raccourci de bureau « Export MP4 (Camille) »
└── static/icon.svg               # icône (raccourci de bureau + favicon)
```

Les fichiers supprimés lors de la migration depuis FilmCraft/`.xml` sont conservés dans
`backup_pre_prproj/` (non utilisé par l'application).

## Configuration (`.env`)

| Variable | Rôle |
|---|---|
| `BASE_SCAN_FOLDER` / `FOLDER_KEYWORD` / `PROJECTS_SUBFOLDER` | Racine scannée et filtre de dossiers (ex. `/mnt/cache` + `Camille` + `projets`) |
| `INPUT_EXTENSIONS` | Extensions soumises (défaut `.prproj`) |
| `WATCH_FOLDER_INPUT` / `WATCH_FOLDER_OUTPUT` | Watch folders locaux (entrée / sortie) |
| `LOGS_DIRECTORY` + `SUBMISSION_LOG_FILE`/`SUBMISSION_TEXT_LOG_FILE`/`MONITOR_LOG_FILE`/`ENCODER_LOG_FILE` | Journaux (registre CSV structuré, journal texte lisible, moniteur, encodeur) |
| `JOBS_DB_FILE` / `EXPORT_LOG_DIRECTORY` | Base SQLite, journal complet par encodage |
| `OUTPUT_EXTENSION` / `OUTPUT_FILENAME_FORMAT` | Sortie et nom final (placeholder `{project_file_stem}`) |
| `PRPROJ2FFMPEG_SCRIPT` | Script d'encodage (défaut : `tools/prproj-unpacker/prproj2ffmpeg.py`, surchargeable) |
| `PRPROJ2FFMPEG_ENCODER` / `PRPROJ2FFMPEG_BITRATE` | Réglages d'encodage (défaut : `auto` → NVENC si disponible, CBR `10M`) |
| `PRPROJ2FFMPEG_EXTRA_BASES` | Bases supplémentaires de résolution des médias (séparées par `:`) |
| `PRPROJ2FFMPEG_CACHE_MAX_MB` | Plafond du cache disque alpha/bbox (Mo, défaut 2048 ; 0 = illimité ; purge LRU) |
| `ENCODER_POLL_INTERVAL` / `ENCODER_PROGRESS_INTERVAL` / `ENCODER_ABORT_GRACE_SECONDS` | Comportement de l'encodeur |
| `STABLE_FILE_*` / `MOVE_RETRY_*` / `RESCAN_INTERVAL_SECONDS` | Surveillance et déplacement |
| `ALLOW_OVERWRITE` / `ENABLE_FALLBACK_MOVE` / `ENABLE_HEURISTIC_MATCHING` | Règles de collision et de repli |
| `API_USER` / `API_PASSWORD` / `SECRET_KEY` / `FLASK_HOST` / `FLASK_PORT` / `FLASK_DEBUG` | Sécurité et serveur |

## API

| Route | Description |
|---|---|
| `GET /` , `GET|POST /login` , `GET /logout` | Interface et session |
| `GET /api/health` | Santé du serveur (localhost uniquement) |
| `GET /api/status` | `{running, monitor_running, encoder:{…}}` (état encodeur, file d'attente, job courant) |
| `POST /api/start` | Lance la soumission (`{force, clean_source_archive, clean_outputs}`) et reprend l'encodeur |
| `POST /api/stop` | Arrête la soumission et met l'encodeur en pause (abandon du job en cours) |
| `GET /api/logs/stream` | Logs temps réel (SSE) |
| `GET /api/logs/history` | 100 dernières lignes du journal de soumission |
| `POST /api/logs/clear` | Vide l'affichage des logs |

## Statuts (SQLite + CSV)

`SUBMITTED_TO_WATCHFOLDER` → `EXPORT_ENCODING` → `EXPORT_DONE` / `EXPORT_FAILED` /
`EXPORT_ABORTED` → `EXPORT_MOVED_TO_FINAL` / `EXPORT_FAILED_TO_MOVE`.
(`EXPORT_DONE_WITH_WARNINGS` reste accepté pour l'historique de la base ; il n'est plus émis.)

## Tests et vérifications

```bash
./.venv/bin/python -m unittest discover -v          # tests unitaires + smoke Flask
./.venv/bin/python encoder_worker.py --once         # vide la file d'attente puis s'arrête (diagnostic)
```

## Documentation

Voir [`docs/`](./docs/) : architecture, détail de chaque module, arborescence des dossiers.

## Dépannage

- **`script prproj2ffmpeg ou ffmpeg introuvable`** — le script vendorisé ou ffmpeg manque :
  vérifier `PRPROJ2FFMPEG_SCRIPT` dans `.env` et que `ffmpeg`/`ffprobe` sont dans le PATH.
- **`ERREUR: média introuvable`** — le XML interne du `.prproj` référence des médias absents ou sur
  un autre volume : ajouter la racine des médias à `PRPROJ2FFMPEG_EXTRA_BASES` (séparateur `:`),
  puis relancer avec « Forcer l'écrasement ».
- **Premier export d'un projet avec overlays alpha plus lent** — un intermédiaire 8 bits de la
  plage réellement utilisée est mis en cache dans `~/.cache/prproj2ffmpeg/` (plafond
  `PRPROJ2FFMPEG_CACHE_MAX_MB`, défaut 2048 Mo) ; les exports suivants du même média sont rapides.
- **Fichier resté dans `WATCH_OUT/quarantine/`** — l'export n'a été rapproché d'aucune soumission :
  l'examiner puis le remettre dans `WATCH_IN` (il sera adopté) ou le supprimer.
- **Export lent** — pas de GPU NVIDIA (repli `libx264`) : visible dans la console de l'UI et dans le
  journal du job (`logs_watchfolder/exports/`).

⚠️ Ne pas faire tourner la version Windows et la version Linux en même temps sur les mêmes projets :
les deux écrivent le MP4 final au même endroit (le versionnage `_v1`, `_v2` éviterait la perte, mais
les exports seraient dupliqués).

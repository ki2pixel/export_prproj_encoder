# Repository Guidelines

Application Flask (Linux) qui pilote un workflow d'export par watchfolder avec **prproj2ffmpeg** :
elle scanne les projets Premiere (fichiers `.prproj`) des dossiers projets, les copie dans un
watchfolder d'entrée, les encode en H.264 10 Mbps via `tools/prproj-unpacker/prproj2ffmpeg.py`
(ffmpeg, accélération matérielle NVENC avec repli logiciel), puis déplace les MP4 terminés dans le
dossier du projet d'origine. UI, commentaires et logs en français ; usage interne.

C'est le portage Linux de `F:\export_media_encoder` (version Windows/Adobe Media Encoder), qui doit
rester fonctionnel : ne jamais modifier l'autre projet depuis ce dépôt.

## Project Structure & Module Organization

`app.py` est le point d'entrée : Flask multi-threadé qui embarque **deux** tâches de fond en threads
daemon — l'encodeur (`encoder_worker.py`) et le moniteur (`monitor_exports_and_move.py`) — et lance
le script de soumission en sous-processus (`submit_to_watchfolder.py`, sous `process_lock`, sortie
diffusée en SSE). `LogPublisher` (max 50 clients, files de 500 messages) alimente l'UI.

- **`config.py`** — source unique de configuration (`.env` via `python-dotenv`, helpers
  `_env_str/_env_int/_env_bool/_env_path/_env_extensions/_env_paths`), statuts d'export,
  `MATCHABLE_STATUSES`, `resolve_project_root()`, `resolve_prproj2ffmpeg_script()`,
  `resolve_ffmpeg_binaries()`.
- **`db_helper.py`** — SQLite (`jobs.db`) : `submissions` (timestamp, status, original_file_path,
  watchfolder_file_name, expected_export_name, job_id), accès via `contextlib.closing` + `with conn:`.
- **`prproj_engine.py`** — pilotage du script d'encodage : `EncodeSettings`, `build_command()`
  (`<python> prproj2ffmpeg.py <projet> --out … --bitrate … --base …`), `run_export()` (Popen +
  sélecteur, progression périodique, abandon), `read_project_info()` (mode `--dump` : séquence,
  durée, fps), `summarize_line()` (résumés français pour l'UI), `probe_ffmpeg()`, `ProcessRegistry`
  (SIGTERM/SIGKILL sur le groupe de process).
- **`tools/prproj-unpacker/`** — `prproj2ffmpeg.py` vendorisé (voir `PROVENANCE.md` et
  `UPSTREAM-SYNC.md`), `unpack.pl` et LICENSE. La source d'origine est introuvable depuis le
  2026-10-13 : cette copie est désormais **la copie de référence** ; les correctifs locaux y sont
  documentés et la re-synchronisation est décrite dans `UPSTREAM-SYNC.md`.
- **`encoder_worker.py`** — file d'attente par scrutation de `WATCH_IN`, encodage séquentiel,
  sortie en `WATCH_OUT/.staging/` puis publication atomique, adoption des projets déposés à la main,
  reprise des jobs orphelins, pause/abandon.
- **`submit_to_watchfolder.py`** — scan non récursif de `<projets>/*.prproj`, priorité à la variante
  `_assist` d'un projet, nom final attendu (nom du fichier `.prproj` nettoyé, sans préfixe numérique,
  « copy N » ni `_assist`), copie atomique vérifiée vers `WATCH_IN`, garde-fou `EXPORT_SUPERSEDED`
  des soumissions normales déjà en file, journalisation SQLite + CSV.
- **`monitor_exports_and_move.py`** — watchdog sur `WATCH_OUT` (async, non bloquant), rapprochement
  par `job_id` puis heuristique, `move_to_final_verified()` (renommage atomique ou copie `.part`
  + `os.replace` inter-systèmes), versionnage, quarantaine, rescan périodique.
- **`filename_utils.py`** — nettoyage/normalisation des noms, `strip_job_id()` / `extract_job_id()`,
  suffixe `_assist` (`is_assist_stem()` / `strip_assist_suffix()`).
- **`templates/`, `static/`** — UI responsive (thème clair/sombre, SSE, statuts Soumission/Moniteur/Encodeur).
- **`fixtures/`** — fixtures réelles (projet `.prproj`) utilisées par les tests.
- **`docs/`** — documentation par module ; `perf_export.md` couvre le diagnostic des exports lents.

## Build, Test, and Development Commands

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env              # renseigner BASE_SCAN_FOLDER, PRPROJ2FFMPEG_*, secrets
./run_app.sh                      # premier plan (Ctrl+C pour arrêter)
./run_app.sh --background         # arrière-plan + /api/health + navigateur
./stop_app.sh [--all] [--force]   # arrêt propre (SIGTERM) de l'instance du port configuré
./install_shortcut.sh             # (ré)installe le raccourci de bureau « Export MP4 (Camille) »
./install_shortcut.sh --uninstall
./.venv/bin/python -m unittest discover -v
./.venv/bin/python -m unittest test_app.TestAMEWatchfolder.test_path_safety
./.venv/bin/python encoder_worker.py --once   # vide la file d'attente puis s'arrête
./.venv/bin/python tools/benchmark_export.py --prproj <projet> --base <racine>  # A/B sans MP4
```

`run_app.sh` ne démarre jamais deux serveurs : si `/api/health` répond, il se contente d'ouvrir la
page. Le raccourci de bureau s'appuie sur `static/icon.svg` (également utilisé comme favicon).
Toute page HTML est servie en `Cache-Control: no-store` : un jeton CSRF mis en cache provoquerait un
403 à la soumission du formulaire.

Prérequis : Python 3.10+, `ffmpeg` ≥ 4.2 et `ffprobe` dans le PATH, watch folders créés dans le
projet. L'encodage matériel utilise NVENC si un GPU NVIDIA est disponible (sinon repli automatique
`libx264`, plus lent) ; le profil encode H.264 CBR 10 Mbps + audio AAC 320 kbps, à la cadence de la
séquence.

## Coding Style & Naming Conventions

Pas de linter ni de formatter configuré. Suivre les conventions établies : type hints sur les
signatures, `pathlib.Path` pour tous les chemins (jamais de concaténation de chaînes),
`threading.Lock` pour l'état mutable partagé, `contextlib.closing` pour SQLite, docstrings et
messages utilisateur en français. Les chemins et constantes de comportement passent par `config.py` ;
tout ce qui touche au moteur d'encodage passe par `prproj_engine.py` (jamais d'import du script
vendorisé).

## Testing Guidelines

`unittest` uniquement, découvert par `python -m unittest discover`. Couverture actuelle : sécurité
des chemins, versionnage, `job_id` (`strip_job_id` / `extract_job_id`), scan non récursif, priorité
`_assist` (scan, nom final, garde-fou `EXPORT_SUPERSEDED`), contrat prproj2ffmpeg (`build_command`,
`parse_bitrate`, `read_project_info`, `probe_ffmpeg`), résumés de lignes (`summarize_line`), plage
alpha/cache (bornage `-t`, clé de contenu, cache négatif bbox `[]`, purge LRU), statuts appariables,
déplacement vérifié (même FS et inter-FS), plus un smoke test Flask (`test_client` : CSP, auth,
CSRF, `/api/status`).
Les fixtures réelles sont dans `fixtures/` ; les tests n'écrivent jamais hors de leurs bacs à sable
(`test_sandbox*`). Aucun seuil de couverture imposé.

## Sécurité et exploitation

- Auth par session + CSRF sur toute mutation ; `/api/health` refuse tout client non loopback ;
  `FLASK_DEBUG` interdit hors loopback ; CSP et en-têtes de sécurité sur toutes les réponses.
- Ne jamais laisser un job d'encodage orphelin : l'abandon passe par `ProcessRegistry.abort()`
  (SIGTERM sur le groupe de process — ffmpeg hérite du groupe du script — puis SIGKILL après
  `ENCODER_ABORT_GRACE_SECONDS`).
- Les chemins médias du XML interne sont résolus par `prproj2ffmpeg --base` (racine du projet
  d'origine, son parent, `BASE_SCAN_FOLDER`, plus `PRPROJ2FFMPEG_EXTRA_BASES` si besoin).
- La préparation alpha (intermédiaire qtrle et bbox `cropdetect`) est bornée au préfixe réellement
  utilisé ; le cache disque `~/.cache/prproj2ffmpeg` est plafonné par
  `PRPROJ2FFMPEG_CACHE_MAX_MB` (défaut 2048 Mo, purge LRU au démarrage).
- Les intermédiaires qtrle portent un sidecar `.qtrle.json` : une plage plus courte réutilise un
  intermédiaire plus long du même contenu (et sa bbox pleine). `PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX`
  (défaut 15 s) tolère une queue inutilisée avant conversion pleine ; l'augmenter (ex. 60) pour un
  lot de séquences partageant un média.
- La version Windows et la version Linux ne doivent pas tourner simultanément sur les mêmes
  projets (même destination finale).

# app.py – interface et orchestration

`app.py` est le point d'entrée : un process Flask multi-threadé qui embarque les deux tâches de
fond et lance la soumission en sous-processus.

## Rôles

1. **Interface web** : page `/` (panneau de contrôle + console de logs), `/login`, `/logout`.
   - Authentification par session : `API_USER` / `API_PASSWORD` du `.env` (mot de passe temporaire
     fort généré et affiché en console si `API_PASSWORD` est vide).
   - Rate-limiting du login : 5 échecs par IP sur 5 minutes glissantes, blocage 5 minutes,
     délai constant de 0,5 s sur tout échec (anti-énumération), réponses 401 uniformes.
   - Jeton CSRF de session (`_csrf_token`) exigé sur toute requête mutative (`csrf_protect`),
     comparé en temps constant (`hmac.compare_digest`) ; rotation de session à la connexion.
   - Cookies `HttpOnly`, `SameSite=Lax`, `Secure` dès que le serveur n'écoute pas en loopback,
     session de 8 h. En-têtes `Content-Security-Policy`, `X-Content-Type-Options`,
     `Referrer-Policy`, `X-Frame-Options` sur toutes les réponses.
   - `/api/health` est la seule route non authentifiée, et elle refuse tout client non loopback.

2. **Console de logs temps réel (SSE)** : `LogPublisher` (Pub/Sub thread-safe, 50 clients maximum,
   file de 500 messages avec écartement des plus anciens) ; `/api/logs/stream` diffuse, heartbeat
   toutes les secondes ; `/api/logs/history` sert les 100 dernières lignes du journal texte de
   soumission ; `/api/logs/clear` vide les files.

3. **Orchestration** :
   - le **moniteur d'exports** (`monitor_exports_and_move.start_monitor`) tourne dans un thread
     daemon et pousse ses lignes vers l'UI (`log_callback`) ;
   - l'**encodeur** (`encoder_worker.start_encoder`) tourne dans un second thread daemon ;
   - la **soumission** est lancée par `subprocess.Popen([sys.executable, submit_to_watchfolder.py])`
     sous `process_lock` (anti-double démarrage) ; la sortie est lue en binaire par
     `monitor_process_output` (décodage UTF-8 tolérant) et diffusée en SSE ;
   - `atexit` arrête proprement l'encodeur (abandon du job en cours) et le moniteur ;
     un gestionnaire `SIGTERM`/`SIGINT` déclenche le même arrêt propre.

## API

| Route | Méthode | Réponse |
|---|---|---|
| `/` | GET | page principale (auth requise) |
| `/login` | GET, POST | formulaire / connexion (CSRF requis en POST) |
| `/logout` | GET | destruction de session |
| `/api/health` | GET | `{"status": "ok"}` (loopback uniquement, 403 sinon) |
| `/api/status` | GET | `{running, monitor_running, encoder:{running, paused, queue_pending, current_job, current_file, processed, failed, aborted, encoder_available, encoder_engine, last_error}}` |
| `/api/start` | POST | `{force, clean_source_archive, clean_outputs}` (booléens stricts) → lance la soumission et reprend l'encodeur |
| `/api/stop` | POST | arrête la soumission (`terminate()` puis `kill()` après 5 s) et met l'encodeur en pause en abandonnant le job en cours |
| `/api/logs/stream` | GET | flux SSE (`text/event-stream`) |
| `/api/logs/history` | GET | 100 dernières lignes du journal texte + `encoding_used` |
| `/api/logs/clear` | POST | purge les files SSE |

## Journalisation au démarrage

`_log_boot_configuration()` écrit (dans `encoder_log.txt` **et** vers l'UI) :
la racine scannée et le mot-clé, les extensions, les watch folders, le format de nom final, la base
SQLite, les réglages d'encodage (`PRPROJ2FFMPEG_BITRATE`, `PRPROJ2FFMPEG_ENCODER`), le chemin résolu
du script `prproj2ffmpeg.py`, puis la vérification best-effort de l'environnement ffmpeg
(`probe_ffmpeg()` : version, présence de `h264_nvenc` **et test réel d'initialisation NVENC** sur
une trame nulle 64x64). Le préflight évite d'annoncer NVENC disponible sur la seule présence de
l'encodeur compilé : sans GPU ou si la session NVENC est occupée, l'UI annonce le repli
`libx264`. Aucune de ces vérifications n'est bloquante : un script introuvable ou un NVENC
indisponible ne produit qu'une erreur/avertissement journalisé.

## Garanties de sécurité au démarrage

- `FLASK_DEBUG=True` avec `FLASK_HOST` non loopback ⇒ démarrage refusé (le débogueur Werkzeug
  permettrait l'exécution de code à distance).
- Écoute sur une interface externe ⇒ avertissement explicite (proxy inverse TLS recommandé).
- `FLASK_HOST` non loopback ⇒ cookie `Secure` et message d'avertissement sur les identifiants.
- `use_reloader=False` obligatoire (sinon le moniteur et l'encodeur seraient dupliqués).

# app.py – interface et orchestration

`app.py` est le point d'entrée : un process Flask multi-threadé qui embarque les deux tâches de
fond et lance la soumission en sous-processus.

## Rôles

1. **Interface web** : page `/` (panneau de contrôle, suivi des encodages + console de logs),
   `/login`, `/logout`.
   - Authentification par session : `API_USER` / `API_PASSWORD` du `.env` (mot de passe temporaire
     fort généré et affiché en console si `API_PASSWORD` est vide).
   - Rate-limiting du login : 5 échecs par IP sur 5 minutes glissantes, blocage 5 minutes,
     délai constant de 0,5 s sur tout échec (anti-énumération), réponses 401 uniformes.
     Le dictionnaire `_login_failures` est purgé sous verrou et plafonné à
     `MAX_TRACKED_LOGIN_IPS` (4096) : au-delà, l'IP la plus ancienne est évincée (FIFO), ce qui
     borne la mémoire face à une rafale d'IP distinctes.
   - Identifiants comparés en temps constant (`hmac.compare_digest`) ; jeton CSRF de session
     (`_csrf_token`) exigé sur toute requête mutative (`csrf_protect`). À la connexion réussie :
     `session.clear()`, régénération explicite du jeton CSRF et session permanente (anti-fixation).
   - Cookies `HttpOnly`, `SameSite=Lax`, `Secure` dès que le serveur n'écoute pas en loopback,
     session de 8 h. En-têtes `Content-Security-Policy` (`default-src 'self'`, `base-uri 'self'`,
     `form-action 'self'`, `object-src 'none'`, `style-src 'self'`, `font-src 'self'`,
     `script-src 'self'`, `connect-src 'self'`, `img-src 'self' data:`),
     `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options` sur toutes les réponses.
     Aucune ressource externe n'est chargée : les polices utilisent la pile système et
     l'interface reste pleinement fonctionnelle en environnement isolé (air-gap).
   - `/logout` accepte `GET` et `POST` : un `POST` exige le jeton CSRF, un `GET` portant
     `Sec-Fetch-Site: cross-site` est refusé (une balise `<img>` tierce ne peut pas déconnecter).
   - `/api/health` est la seule route non authentifiée, et elle refuse tout client non loopback.
     Elle refuse aussi les en-têtes de proxy (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`) sans
     session authentifiée : derrière un reverse proxy local, `remote_addr` vaut toujours
     `127.0.0.1` et la restriction loopback serait sinon contournée.

2. **Console de logs temps réel (SSE)** : `LogPublisher` (Pub/Sub thread-safe, 50 clients maximum,
   file de 500 messages avec écartement des plus anciens) ; `/api/logs/stream` diffuse, heartbeat
   toutes les secondes ; `/api/logs/history` sert les 100 dernières lignes du journal texte de
   soumission ; `/api/logs/clear` vide les files.

3. **Interface web et robustesse frontend** (`static/script.js`, `static/style.css`) :
   - **Suivi visuel des encodages** : panneau dédié au-dessus de la console de logs avec
     progression globale du lot (`(terminés + fraction en cours) / total`), carte de l'encodage en cours
     (nom nettoyé, badge `assist`, métadonnées de séquence, pourcentage, vitesse, ETA, taille
     écrite / attendue), file d'attente FIFO (hors job courant) et barres via `<progress>` natif
     sans style inline (conforme CSP `style-src 'self'`).
   - **Polling unique** de `/api/status` toutes les 2 s, avec timeout de requête de 8 s
     (`AbortController`), verrou anti-exécution concurrente et suspension lorsque l'onglet est
     masqué (`visibilitychange`). Le retour sur l'onglet déclenche un rafraîchissement immédiat.
   - **SSE** : historique chargé avant l'ouverture du flux ; reconnexion avec backoff exponentiel
     de 5 s à 30 s, timer annulable à l'arrêt de la session.
   - **Console de logs** plafonnée à 500 lignes, insertion par `DocumentFragment` (un seul reflow),
     auto-scroll mesuré avant mutation, compteur synchronisé sur le DOM réel.
   - **Purge des logs** : confirmation via un `<dialog>` non bloquant, effacement local puis
     `POST /api/logs/clear`, sans réinjection de l'historique fichier. Une reconnexion réseau
     conserve les lignes déjà affichées.
   - **Stockage** : les accès `localStorage` (thème, annonces vocales) sont encapsulés et
     dégradent silencieusement si le navigateur les interdit ou si le quota est plein.
   - **Accessibilité** : classes de contraste WCAG AA pour les statuts, pastilles différenciées
     (`running`, `paused`, `error`, `offline`), `role="switch"` sur le thème, console focalisable,
     regroupement `<fieldset>/<legend>`, landmark `<main>` sur `/login`, focus visible et respect
     de `prefers-reduced-motion` (texte des boutons conservé, spinner masqué).
   - **Thème** : pile de polices système (aucun appel à Google Fonts) et thème sombre natif sur
     la page de connexion via `prefers-color-scheme`.

4. **Orchestration** :
   - le **moniteur d'exports** (`monitor_exports_and_move.start_monitor`) tourne dans un thread
     daemon et pousse ses lignes vers l'UI (`log_callback`) ;
   - l'**encodeur** (`encoder_worker.start_encoder`) tourne dans un second thread daemon ;
   - la **soumission** est lancée par `subprocess.Popen([sys.executable, submit_to_watchfolder.py])`
     sous `process_lock` (anti-double démarrage) ; la sortie est lue en binaire par
     `monitor_process_output` (décodage UTF-8 tolérant, `stdout` fermé dans `finally`) et
     diffusée en SSE ;
   - `/api/stop` récupère la référence du sous-processus sous `process_lock` puis exécute
     `terminate()`/`wait()`/`kill()` **hors verrou** : `/api/status` et le thread de lecture des
     logs ne sont jamais bloqués pendant l'attente ;
   - `atexit` termine le sous-processus de soumission s'il est encore actif, puis arrête
     proprement l'encodeur (abandon du job en cours) et le moniteur ; un gestionnaire
     `SIGTERM`/`SIGINT` déclenche le même arrêt propre.

## API

| Route | Méthode | Réponse |
|---|---|---|
| `/` | GET | page principale (auth requise) |
| `/login` | GET, POST | formulaire / connexion (CSRF requis en POST) |
| `/logout` | GET, POST | destruction de session (l'UI utilise un formulaire POST avec CSRF ; GET cross-site refusé) |
| `/api/health` | GET | `{"status": "ok"}` (loopback uniquement, sans proxy non authentifié ; 403 sinon) |
| `/api/status` | GET | `{running, monitor_running, encoder:{running, paused, queue_pending, queue, current_job, current_file, current_display_name, current_is_assist, current_stage, current_project, current_progress, batch, current_started_at, processed, failed, aborted, encoder_available, encoder_engine, last_error}}` |
| `/api/start` | POST | `{force, clean_source_archive, clean_outputs}` (booléens stricts) → lance la soumission et reprend l'encodeur |
| `/api/stop` | POST | arrête la soumission (`terminate()` puis `kill()` après 5 s) et met l'encodeur en pause en abandonnant le job en cours |
| `/api/logs/stream` | GET | flux SSE (`text/event-stream`) |
| `/api/logs/history` | GET | 100 dernières lignes du journal texte + `encoding_used` |
| `/api/logs/clear` | POST | purge les files SSE (l'historique fichier n'est pas réaffiché côté client) |

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

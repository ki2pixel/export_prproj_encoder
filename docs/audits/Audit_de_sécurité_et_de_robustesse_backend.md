L'analyse du dépôt `export_prproj_encoder` et de ses spécifications architecturales met en évidence une architecture globalement robuste (isolation du moteur FFmpeg en sous-processus, staging atomique, sécurité HTTP/CSRF/CSP, supervision hybride `watchdog` + polling) `[cite: 1]`, mais exposée à plusieurs risques critiques et élevés en production sous forte concurrence ou lors de défaillances système/stockage.

**Note méthodologique sur l'archive Repomix fournie** : Le fichier joint `repomix-output-ki2pixel-export_prproj_encoder.md` a été tronqué lors de sa génération au milieu de `docs/archive/Optimisation-De-Pipeline-FFmpeg-NVENC.md` en raison des 70 images Base64 intégrées dans ce document d'archive `[cite: 1]`. Le présent audit s'appuie sur la cartographie du dépôt, les workflows d'ingénierie (`.agents/workflows/*.md`), le journal de correctifs d'audit (`docs/archive/audit-prproj2ffmpeg-correctifs.md`) `[cite: 1]` et les spécifications techniques détaillées de chaque module. Pour obtenir les numéros de lignes exacts dans les patchs Python, régénérez l'archive en excluant les archives documentaires (`npx repomix --ignore "docs/archive/**"`).

## 1. Synthèse Managériale et Matrice des Risques

Le backend repose sur un processus Python 3.10+ unique exécutant Flask (HTTP + SSE `LogPublisher`), deux threads daemons (`encoder_worker.py`, `monitor_exports_and_move.py` doté d'un `ThreadPoolExecutor` à 8 workers) et des sous-processus (`submit_to_watchfolder.py`, `prproj2ffmpeg.py` / `ffmpeg`) partageant une base SQLite (`jobs.db`), un journal CSV (`submission_log.csv`) et trois espaces de stockage (`WATCH_IN`, `WATCH_OUT`, `/mnt/cache`) `[cite: 1]`.

Les zones de vulnérabilité principales se concentrent à la frontière entre ces acteurs concurrents : verrouillage SQLite multi-écrivains, collisions de nommage final entre les 8 threads du moniteur, survie des sous-processus enfants `ffmpeg` lors d'un arrêt précoce du parent `prproj2ffmpeg.py`, et épuisement mémoire (DoS) sur les structures en RAM ou lors de la décompression `.prproj` `[cite: 1]`.

| ID | Domaine | Constat | Composant affecté | Sévérité |
| --- | --- | --- | --- | --- |
| **C-01** | Concurrence | Deadlock SQLite et `OperationalError: database is locked` sous accès multi-acteurs | `db_helper.py` | **Critique** |
| **C-02** | Concurrence | Condition de course TOCTOU sur le versionnage `_v1`/`_v2` entre les 8 threads | `monitor_exports_and_move.py` | **Élevé** |
| **P-01** | Processus | Survie des processus `ffmpeg` enfants lors d'un `abort()` ou pendant le pré-calcul alpha | `prproj_engine.py` | **Élevé** |
| **S-01** | Sécurité | Épuisement mémoire (DoS) sur le dictionnaire du rate-limiter et aveuglement derrière proxy | `app.py` | **Élevé** |
| **S-02** | Sécurité | Bombe de décompression Gzip en mémoire (`MemoryError` / OOM) sur `.prproj` | `prproj2ffmpeg.py` | **Élevé** |
| **R-01** | Résilience | Boucle infinie de reprise post-crash (Poison Pill) sur les jobs `EXPORT_ENCODING` | `encoder_worker.py` | **Élevé** |
| **R-02** | Stockage | Court-circuit d'`inotify` (`on_created` vs `on_moved`) lors du `os.replace` depuis `.staging` | `monitor_exports_and_move.py` | **Moyen** |
| **S-03** | Sécurité | Traversée de répertoire par liens symboliques dans `resolve_media()` et `is_safe_path()` | `filename_utils.py` | **Moyen** |
| **S-04** | Sécurité | Injection de filtres dans `-filter_complex` via caractères spéciaux de chemins médias | `prproj2ffmpeg.py` | **Moyen** |
| **P-02** | Processus | Conflit de session NVENC lors des sondes runtime `probe_ffmpeg()` concurrentes | `prproj_engine.py` | **Moyen** |
| **P-03** | Ressources | Découpage UTF-8 multi-octets et gestion des `\r` dans `selectors.DefaultSelector` | `prproj_engine.py` | **Moyen** |
| **P-04** | Ressources | Saturation des 50 slots SSE par des connexions TCP semi-ouvertes sans keepalive | `app.py` | **Moyen** |
| **R-03** | Stockage | Absence de `fsync` avant `os.replace` et fichiers `.<nom>.part` orphelins sur `/mnt/cache` | `monitor_exports_and_move.py` | **Moyen** |
| **C-03** | Concurrence | Adoption prématurée d'un `.prproj` déposé manuellement sans écriture atomique `.tmp` | `encoder_worker.py` | **Moyen** |
| **S-05** | Sécurité | Invalidation des sessions/CSRF au redémarrage si `SECRET_KEY` est éphémère | `app.py` | **Faible** |
| **C-04** | Concurrence | Absence de verrouillage fichier (`flock`) sur `submission_log.csv` | `submit_to_watchfolder.py` | **Faible** |

## 2. Constats Détaillés et Recommandations Correctives

### Domaine A — Sécurité Applicative et Réseau (OWASP & Hardening)

#### [S-01] Épuisement mémoire (DoS) sur `_check_login_rate_limit()` et comportement derrière un reverse proxy (Élevé)

* **Composant affecté** : `app.py` (`_check_login_rate_limit`, `/login`, `/api/health`).
* **Description technique** : Le limiteur de tentatives de connexion (5 échecs / 5 minutes par IP) stocke l'historique des échecs dans une structure en mémoire indexée par `request.remote_addr`. Deux vulnérabilités se combinent :
1. Si la purge des entrées expirées n'est effectuée que pour l'IP courante et que le dictionnaire n'a pas de taille maximale bornée, une rafale de requêtes avec des IP sources variées fait croître le dictionnaire indéfiniment en RAM.
2. Si l'application est exposée derrière un reverse proxy local (Nginx, Caddy, tunnel Cloudflare), `request.remote_addr` vaut `127.0.0.1` pour tous les clients : 5 échecs d'un attaquant bloquent l'authentification pour l'ensemble des utilisateurs légitimes, et la vérification loopback de `/api/health` est contournée par toute requête externe transitant par le proxy.


* **Scénario d'occurrence / PoC** : Envoi de 5 POST `/login` erronés via un proxy inverse local -> blocage global de `127.0.0.1` ; requête externe `GET /api/health` via le même proxy -> accès accordé car `remote_addr == "127.0.0.1"`.
* **Impact système** : Déni de service sur l'interface d'administration ou exposition non désirée de `/api/health`.
* **Recommandation de remédiation** :
* Protéger le dictionnaire des tentatives par un `threading.Lock`, purger les clés expirées (> 300 s) lorsque `len(attempts) > 1024`, et plafonner la structure via un `collections.OrderedDict` borné (éviction FIFO/LRU au-delà de 4096 IP).
* Sur `/api/health`, vérifier non seulement que `request.remote_addr in ("127.0.0.1", "::1")`, mais aussi qu'aucun en-tête de proxy (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`) n'est présent, sauf si l'authentification de session est valide.



#### [S-02] Bombe de décompression Gzip en mémoire dans `prproj2ffmpeg.py` (Élevé)

* **Composant affecté** : `tools/prproj-unpacker/prproj2ffmpeg.py` (`main()`, `Project.__init__`) `[cite: 1]`.
* **Description technique** : Suite au correctif d'audit 1.5, le fichier `.prproj` est lu intégralement en mémoire puis décompressé via `gzip.decompress(raw)` si les deux premiers octets valent `\x1f\x8b`, avant d'être parsé par `ET.fromstring(xml_bytes)` `[cite: 1]`. `gzip.decompress()` décompresse le flux complet sans plafond de taille.
* **Scénario d'occurrence / PoC** : Un fichier `.prproj` corrompu ou malveillant de 10 Mo contenant des zéros compressés (ratio 1000:1) déposé dans `WATCH_IN` génère un tampon `xml_bytes` de 10 Go + un arbre DOM `ElementTree` de plusieurs dizaines de gigaoctets.
* **Impact système** : Déclenchement de l'OOM Killer Linux, tuant brutalement le sous-processus ou le processus principal Flask.
* **Recommandation de remédiation** :
* Utiliser `gzip.GzipFile(fileobj=io.BytesIO(raw)).read(MAX_PRPROJ_XML_BYTES + 1)` avec un plafond strict (ex. `MAX_PRPROJ_XML_BYTES = 250 * 1024 * 1024` soit 250 Mo) et lever une erreur propre (`die("projet .prproj trop volumineux après décompression")`) si la limite est dépassée.



#### [S-03] Traversée de répertoire par liens symboliques dans `is_safe_path()` et `resolve_media()` (Moyen)

* **Composant affecté** : `filename_utils.py`, `config.py`, `tools/prproj-unpacker/prproj2ffmpeg.py` (`resolve_media`) `[cite: 1]`.
* **Description technique** : `resolve_media(win_path, bases)` découpe le chemin Windows enregistré dans le `.prproj` et teste toutes les profondeurs de suffixes `for skip in range(0, len(parts))` sous les répertoires `bases` (`/mnt/cache`) `[cite: 1]`. Si un sous-dossier de `/mnt/cache` contient un lien symbolique pointant vers `/` ou `/home`, ou si `parts` contient des segments relatifs non normalisés avant jointure, le fichier résolu sort de l'arborescence `/mnt/cache`.
* **Impact système** : Lecture ou sonde (`ffprobe` / `ffmpeg`) de fichiers hors de l'arborescence autorisée `/mnt/cache`.
* **Recommandation de remédiation** :
* Dans `is_safe_path(path, allowed_base)` et dans `resolve_media()`, résoudre systématiquement les chemins canoniques avec `Path(candidate).resolve()` (ou `os.path.realpath` dans `prproj2ffmpeg.py` pour conserver son style `os.path` `[cite: 1]`) et vérifier `os.path.commonpath([real_candidate, real_base]) == real_base`.



#### [S-04] Injection de directives libavfilter via les chemins ou métadonnées dans `-filter_complex` (Moyen)

* **Composant affecté** : `tools/prproj-unpacker/prproj2ffmpeg.py`, `prproj_engine.py` `[cite: 1]`.
* **Description technique** : Les appels `subprocess.Popen` n'utilisent pas `shell=True`, ce qui bloque l'injection de commandes shell. En revanche, `prproj2ffmpeg.py` construit dynamiquement des expressions `-filter_complex` (`scale`, `crop`, `gblur`, `rotate`, `colorchannelmixer`) `[cite: 1]` et des arguments `-i <path>`. Un nom de fichier commençant par `-` (interprété comme une option par `ffmpeg`/`ffprobe` s'il n'est pas précédé de `file:` ou d'un chemin absolu) ou un paramètre numérique non borné issu du XML pourrait corrompre la commande.
* **Recommandation de remédiation** :
* S'assurer que tous les chemins passés à `-i` et aux sorties dans `prproj_engine.py` et `prproj2ffmpeg.py` sont des chemins absolus (`os.path.abspath` / `Path.resolve()`), ce qui garantit qu'ils commencent par `/` sous Linux et ne peuvent jamais être interprétés comme des flags CLI par `ffmpeg`.



#### [S-05] Persistance de `SECRET_KEY` et rotation CSRF à la connexion (Faible)

* **Composant affecté** : `app.py`, `config.py`.
* **Description technique** : Si `SECRET_KEY` n'est pas définie dans `.env` et utilise une valeur aléatoire générée à l'import (`secrets.token_hex(32)`), chaque redémarrage du service invalide les sessions existantes. À l'inverse, lors d'un `POST /login` réussi, si la session pré-authentification n'est pas purgée (`session.clear()`) avant de régénérer le jeton CSRF, l'application est exposée à la fixation de session si le cookie avait été pré-injecté.
* **Recommandation de remédiation** :
* Appeler `session.clear()` immédiatement après validation de `API_USER` / `API_PASSWORD` (comparés via `hmac.compare_digest`), puis définir `session['authenticated'] = True`, `session.permanent = True` et générer un nouveau `session['csrf_token'] = secrets.token_hex(32)`.



---

### Domaine B — Concurrence, Threading et Synchronisation

#### [C-01] Contention SQLite et Deadlock sur les transactions conditionnelles (Critique)

* **Composant affecté** : `db_helper.py` (`jobs.db`) `[cite: 1]`.
* **Description technique** : Quatre composants accèdent concurremment à `jobs.db` : le serveur web Flask, `encoder_worker.py`, les 8 threads de `monitor_exports_and_move.py`, et le processus externe `submit_to_watchfolder.py` `[cite: 1]`.
Sous Python `sqlite3`, l'utilisation de `with conn:` ouvre par défaut une transaction `DEFERRED`. Si une fonction comme `update_submission_status_if_current()` exécute un `SELECT status FROM submissions WHERE job_id = ?` suivi d'un `UPDATE submissions SET status = ? WHERE job_id = ?` à l'intérieur du même bloc `with conn:`, la connexion acquiert d'abord un verrou `SHARED`, puis tente de le promouvoir en verrou `RESERVED`/`EXCLUSIVE`. Si une deuxième connexion fait de même au même instant, SQLite détecte un **deadlock irréconciliable** et lève **immédiatement** `sqlite3.OperationalError: database is locked` sans respecter le `timeout=30.0` !
* **Scénario d'occurrence / PoC** : `monitor_exports_and_move.py` (8 threads) met à jour le statut de fin de déplacement pendant que `encoder_worker.py` publie un nouveau `EXPORT_DONE` ou que `submit_to_watchfolder.py` marque des jobs `EXPORT_SUPERSEDED` `[cite: 1]`.
* **Impact système** : Échec d'enregistrement de statut (`EXPORT_DONE` ou `EXPORT_MOVED_TO_FINAL` perdu), entraînant un retraitement du fichier au cycle suivant ou une mise en quarantaine injustifiée.
* **Recommandation de remédiation** :
1. Configurer systématiquement chaque connexion SQLite dans `db_helper.py` avec :
```python
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("PRAGMA synchronous=NORMAL;")
conn.execute("PRAGMA busy_timeout=30000;")
conn.execute("PRAGMA foreign_keys=ON;")

```


2. Dans `update_submission_status_if_current()`, remplacer tout couple `SELECT` + `UPDATE` par une unique requête atomique `UPDATE` qui acquiert directement le verrou d'écriture sans risque de promotion bloquante :
```python
with conn:
    cur = conn.execute(
        "UPDATE submissions SET status = ?, updated_at = ? "
        "WHERE job_id = ? AND status IN (...)",
        (new_status, now_iso, job_id, *expected_statuses),
    )
    return cur.rowcount > 0

```





#### [C-02] Condition de course TOCTOU sur le versionnage `_v1`, `_v2` dans `monitor_exports_and_move.py` (Élevé)

* **Composant affecté** : `monitor_exports_and_move.py` (`ThreadPoolExecutor(max_workers=8)`, `move_to_final_verified`) `[cite: 1]`.
* **Description technique** : Le verrou de déduplication `processing_paths` protège uniquement le chemin *source* dans `WATCH_OUT/` `[cite: 1]`. Cependant, deux fichiers sources différents dans `WATCH_OUT/` (ex. `Projet_9x16_20261023_110000_aaaaaa.mp4` et `Projet_9x16_20261023_110500_bbbbbb.mp4`, ou deux fichiers appariés par le fallback heuristique `[cite: 1]`) sont tous deux destinés à être renommés vers `/mnt/cache/<projet>/Projet_9x16.mp4` (ou `_v1.mp4`).
* **Scénario d'occurrence / PoC** :
1. Thread 1 et Thread 2 du `ThreadPoolExecutor` traitent simultanément deux MP4 du même projet.
2. Thread 1 et Thread 2 testent `final_path.exists()` -> `False` pour tous les deux (ou trouvent tous deux `_v1.mp4` libre).
3. Thread 1 copie vers `.Projet_9x16.mp4.part` et appelle `os.replace()`.
4. Thread 2 écrit dans le **même** fichier temporaire `.Projet_9x16.mp4.part` (corrompant la copie de Thread 1 ou levant `FileNotFoundError` au moment du second `os.replace`) ou écrase silencieusement le fichier final de Thread 1 sans incrémenter en `_v1` !


* **Impact système** : Écrasement silencieux d'une version exportée ou corruption du fichier `.part` lors du transfert inter-FS.
* **Recommandation de remédiation** :
* Protéger la résolution du nom de destination final et la réservation de la cible par un verrou global de destination (`_dest_lock = threading.Lock()` + ensemble `_active_destinations: set[Path]`), et inclure un identifiant unique (ou le `job_id` / `threading.get_ident()`) dans le nom du fichier temporaire `.part` : `.<nom>.<job_id>.part` avant le `os.replace()` final.



#### [C-03] Adoption d'un `.prproj` incomplet déposé manuellement dans `WATCH_IN` (Moyen)

* **Composant affecté** : `encoder_worker.py` `[cite: 1]`.
* **Description technique** : Les fichiers soumis par `submit_to_watchfolder.py` sont copiés de manière atomique via `.tmp` puis `rename` `[cite: 1]`. En revanche, `encoder_worker.py` prend également en charge l'adoption des projets `.prproj` déposés manuellement dans `WATCH_IN` `[cite: 1]`. Si un opérateur copie un gros fichier `.prproj` manuellement dans `WATCH_IN`, le worker FIFO peut le détecter et lancer `prproj_engine.py` avant la fin de l'écriture disque.
* **Impact système** : Échec immédiat du job adopté (`EXPORT_FAILED` sur `BadGzipFile` ou `ParseError` `[cite: 1]`).
* **Recommandation de remédiation** :
* Pour tout fichier `.prproj` découvert dans `WATCH_IN` qui n'est pas déjà enregistré avec le statut `SUBMITTED_TO_WATCHFOLDER` dans `jobs.db` (adoption manuelle), vérifier que sa taille et son `mtime` sont stables sur deux mesures espacées (ex. 1,0 s) et que son descripteur n'est pas ouvert en écriture avant de l'adopter.



#### [C-04] Accès concurrent sans verrouillage à `submission_log.csv` (Faible)

* **Composant affecté** : `submit_to_watchfolder.py` `[cite: 1]`.
* **Description technique** : Si `submit_to_watchfolder.py` est déclenché depuis l'UI (`/api/start`) alors qu'une exécution précédente ou un lancement CLI est encore en cours, deux processus peuvent écrire simultanément dans `submission_log.csv`.
* **Recommandation de remédiation** :
* Utiliser `fcntl.flock(csv_file.fileno(), fcntl.LOCK_EX)` autour de l'écriture des lignes CSV, et empêcher dans `app.py` (sous `process_lock`) le lancement simultané de deux sous-processus `submit_to_watchfolder.py`.



---

### Domaine C — Cycle de Vie des Processus et Gestion des Ressources

#### [P-01] Risque de processus `ffmpeg` orphelins lors de `ProcessRegistry.abort()` (Élevé)

* **Composant affecté** : `prproj_engine.py` (`ProcessRegistry`), `tools/prproj-unpacker/prproj2ffmpeg.py` `[cite: 1]`.
* **Description technique** : `prproj_engine.py` isole `prproj2ffmpeg.py` dans une nouvelle session (`start_new_session=True`, donc `pgid = proc.pid`) et `ProcessRegistry.abort()` envoie `SIGTERM` au groupe de processus (`os.killpg(pgid, signal.SIGTERM)`), attend `ENCODER_ABORT_GRACE_SECONDS`, puis envoie `SIGKILL` `[cite: 1]`.
Or, `prproj2ffmpeg.py` est un script Python qui lance lui-même plusieurs sous-processus `ffmpeg`/`ffprobe` enfants (génération des intermédiaires alpha `.mov`, calcul `alpha_bbox`, puis encodage final) `[cite: 1]`. Lorsque `os.killpg(pgid, signal.SIGTERM)` est envoyé :
1. Le processus Python parent `prproj2ffmpeg.py` meurt quasi instantanément sur `SIGTERM` (interruption Python par défaut).
2. Si `ProcessRegistry.abort()` attend uniquement `proc.wait(timeout=ENCODER_ABORT_GRACE_SECONDS)` sur l'objet `Popen` parent, `proc.wait()` retourne immédiatement dès la mort de `prproj2ffmpeg.py`, **alors que le processus enfant `ffmpeg` peut encore être en vie** (par exemple bloqué sur une lecture `/mnt/cache` ou en train de vider ses buffers) et **ne recevra jamais le `SIGKILL` d'escalade** !
3. De plus, si `prproj2ffmpeg.py` est interrompu pendant `alpha_intermediate`, un fichier `.tmp.mov` ou `.bbox.json.tmp` reste en cours d'écriture si `ffmpeg` survit `[cite: 1]`.


* **Scénario d'occurrence / PoC** : Clic sur `/api/stop` pendant un pré-rendu ProRes alpha ou un encodage lourd -> `prproj2ffmpeg.py` se termine dès `SIGTERM`, `proc.wait()` rend la main immédiatement, mais `ffmpeg` continue de tourner en arrière-plan jusqu'à épuisement de la tâche.
* **Impact système** : Processus `ffmpeg` orphelins saturant le CPU/GPU NVENC et verrouillant les fichiers dans `WATCH_OUT/.staging/`.
* **Recommandation de remédiation** :
* Dans `ProcessRegistry.abort()`, après `proc.wait()`, vérifier que **tous** les processus du groupe `pgid` sont effectivement terminés en sondant `os.killpg(pgid, 0)` dans une boucle jusqu'à l'expiration de `ENCODER_ABORT_GRACE_SECONDS`. Tant que `os.killpg(pgid, 0)` ne lève pas `ProcessLookupError`, le groupe contient encore des processus enfants vivants ; à l'expiration du délai de grâce, envoyer systématiquement `os.killpg(pgid, signal.SIGKILL)` :
```python
deadline = time.monotonic() + grace_seconds
while time.monotonic() < deadline:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        break
    time.sleep(0.05)
else:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass

```





#### [P-02] Contention de sessions matérielles NVENC lors des appels `probe_ffmpeg()` (Moyen)

* **Composant affecté** : `prproj_engine.py` (`probe_ffmpeg`, `_nvenc_session_ok`) `[cite: 1]`.
* **Description technique** : Suite au correctif d'audit 1.6, `probe_ffmpeg()` exécute un test réel d'initialisation NVENC (`ffmpeg -f lavfi -i nullsrc=s=64x64:d=0.1 -frames:v 1 -c:v h264_nvenc -f null -`) `[cite: 1]`. Sur les cartes NVIDIA GeForce grand public, le pilote limite le nombre de sessions NVENC simultanées (3 à 5 selon la génération du pilote), et l'initialisation CUDA/NVENC prend 150 à 400 ms. Si `probe_ffmpeg()` est appelé à chaque chargement d'état UI ou `/api/health` pendant qu'un encodage NVENC est déjà en cours, cela ralentit l'API et peut saturer les sessions NVENC ou échouer faussement (`has_nvenc=False`) `[cite: 1]`.
* **Recommandation de remédiation** :
* Mettre en cache le résultat de `probe_ffmpeg()` dans `prproj_engine.py` avec un TTL (ex. 300 secondes) ou tant que le chemin binaire `ffmpeg` ne change pas.



#### [P-03] Lecture non bloquante `selectors.DefaultSelector` : césure UTF-8 et retours chariot `\r` (Moyen)

* **Composant affecté** : `prproj_engine.py` `[cite: 1]`.
* **Description technique** : La lecture de la sortie de `prproj2ffmpeg.py` / `ffmpeg` via `selectors.DefaultSelector` `[cite: 1]` lit des blocs d'octets bruts (`os.read(fd, 4096)`). Deux écueils surviennent en production :
1. Les messages en français de `prproj2ffmpeg.py` (`é`, `è`, `à`, `…`) `[cite: 1]` utilisent des séquences UTF-8 sur 2 ou 3 octets. Si `os.read()` coupe au milieu d'un caractère multi-octets et appelle `.decode('utf-8')` sur le bloc brut, cela peut produire des caractères de remplacement `` (avec `errors='replace'`) ou lever `UnicodeDecodeError`.
2. `ffmpeg` sépare ses lignes de progression par `\r` et non `\n`. Si le découpage de lignes ne traite que `\n`, toute la progression FFmpeg s'accumule dans une seule ligne géante en mémoire jusqu'à la fin de l'encodage.


* **Recommandation de remédiation** :
* Utiliser `codecs.getincrementaldecoder("utf-8")(errors="replace")` sur le flux d'octets et normaliser les fins de ligne en découpant à la fois sur `\r` et `\n`.



#### [P-04] Épuisement des 50 connexions SSE dans `LogPublisher` par des sockets semi-ouverts (Moyen)

* **Composant affecté** : `app.py` (`LogPublisher`) `[cite: 1]`.
* **Description technique** : `LogPublisher` limite le nombre de clients SSE simultanés à 50 et maintient un buffer borné à 500 messages `[cite: 1]`. Lorsque l'application est au repos (aucun log émis), le générateur SSE de chaque client connecté reste bloqué sur `q.get()`. Si un client ferme son ordinateur portable ou perd le Wi-Fi sans paquet TCP `FIN`, Flask ne détecte jamais la fermeture du socket tant qu'il n'essaie pas d'y écrire un octet. Au bout de 50 déconnexions silencieuses, le serveur refuse tout nouveau flux SSE.
* **Recommandation de remédiation** :
* Dans le générateur SSE de `app.py`, utiliser `q.get(timeout=15.0)` et, sur `queue.Empty`, envoyer un commentaire SSE de battement de cœur `yield ": keepalive\n\n"`. Cela force l'écriture régulière sur le socket TCP, lève `GeneratorExit` / `BrokenPipeError` sur les clients morts et libère immédiatement la file dans le bloc `finally`.



---

### Domaine D — Robustesse, Intégrité du Stockage et Reprise Post-Crash

#### [R-01] Boucle infinie de reprise post-crash (Poison Pill) sur les jobs `EXPORT_ENCODING` (Élevé)

* **Composant affecté** : `encoder_worker.py`, `db_helper.py` `[cite: 1]`.
* **Description technique** : Au démarrage, `encoder_worker.py` réinitialise ou reprend les jobs trouvés à l'état `EXPORT_ENCODING` (reprise post-crash) `[cite: 1]`. Si un projet `.prproj` spécifique déclenche un crash fatal du processus (ex. OOM Killer Linux ou segfault FFmpeg), le statut en base reste `EXPORT_ENCODING` et le fichier `.prproj` reste dans `WATCH_IN`. Au redémarrage du service, le worker reprendra indéfiniment ce même job en boucle.
* **Impact système** : Paralysie complète de la file d'encodage FIFO (`WATCH_IN` bloqué derrière le job poison).
* **Recommandation de remédiation** :
* Lors de la reprise post-crash au démarrage de `encoder_worker.py`, vérifier si un marqueur de tentative de reprise existe déjà pour ce `job_id` (par exemple via un fichier témoin `.Attempted_<job_id>` dans `WATCH_OUT/.staging/` ou via l'historique des logs). Si un job `EXPORT_ENCODING` a déjà fait l'objet d'une reprise post-crash sans aboutir, le passer en `EXPORT_FAILED` et déplacer le `.prproj` hors de la file active plutôt que de le relancer une troisième fois.



#### [R-02] Latence de détection dans `monitor_exports_and_move.py` due à l'événement `on_moved` d'`inotify` (Moyen)

* **Composant affecté** : `monitor_exports_and_move.py` `[cite: 1]`.
* **Description technique** : `encoder_worker.py` encode le fichier MP4 dans `WATCH_OUT/.staging/`, puis le publie de manière atomique dans `WATCH_OUT/` via `os.replace()` `[cite: 1]`. Au niveau du noyau Linux (`inotify`), un `os.replace()` entre `WATCH_OUT/.staging/fichier.mp4` et `WATCH_OUT/fichier.mp4` (même système de fichiers) génère un événement **`IN_MOVED_TO`** (traduit par `watchdog` en appel à `on_moved(event)` où `event.dest_path` est dans `WATCH_OUT/`), et **non** un événement `on_created(event)` ! Si le `FileSystemEventHandler` de `monitor_exports_and_move.py` ne traite que `on_created` (ou ne filtre pas correctement `on_moved` vers `WATCH_OUT/`), la notification instantanée `watchdog` est ignorée et le fichier attend systématiquement le rescan périodique de 60 secondes `[cite: 1]`.
* **Impact système** : Délai supplémentaire pouvant atteindre 60 secondes avant le déplacement final de chaque vidéo exportée.
* **Recommandation de remédiation** :
* S'assurer que le handler `watchdog` implémente à la fois `on_created(self, event)` (sur `event.src_path`) et `on_moved(self, event)` (sur `event.dest_path`), en ignorant dans les deux cas les chemins situés dans `.staging/` ou `quarantine/` et les fichiers cachés commençant par `.`.



#### [R-03] Absence de `fsync` avant `os.replace` et nettoyage des fichiers `.<nom>.part` résiduels (Moyen)

* **Composant affecté** : `submit_to_watchfolder.py`, `encoder_worker.py`, `monitor_exports_and_move.py` (`move_to_final_verified`) `[cite: 1]`.
* **Description technique** :
1. Lors d'une copie inter-systèmes de fichiers (de `WATCH_OUT/` vers `/mnt/cache/<projet>/.<nom>.part` puis `os.replace()` `[cite: 1]`), si `os.fsync()` n'est pas appelé sur le fichier `.part` avant `os.replace()`, une coupure électrique ou une déconnexion du partage `/mnt/cache` peut laisser un fichier final tronqué ou de 0 octet.
2. Si le processus est interrompu pendant la copie vers `.<nom>.part`, ce fichier caché reste orphelin dans `/mnt/cache/<projet>/`. Si une tentative ultérieure réutilise le même nom `.<nom>.part` ou si ces fichiers s'accumulent sur le disque de cache, l'espace de stockage se dégrade.


* **Recommandation de remédiation** :
* Dans `move_to_final_verified()`, après la copie vers `part_path`, vérifier `part_path.stat().st_size == src_path.stat().st_size and part_path.stat().st_size > 0`, ouvrir brièvement `part_path` pour exécuter `os.fsync(fd)`, puis exécuter `os.replace(part_path, final_path)` avant de supprimer `src_path`. En cas d'exception dans le bloc `try`, supprimer `part_path` (`part_path.unlink(missing_ok=True)`) dans le bloc `except`.



---

## 3. Feuille de Route Priorisée des Correctifs

### Priorité 1 — Immédiate (Stabilité & Intégrité Critique)

1. **`db_helper.py` (C-01)** : Activer `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`, `PRAGMA busy_timeout=30000;` à l'ouverture des connexions SQLite, et rendre `update_submission_status_if_current()` purement atomique via une unique instruction `UPDATE ... WHERE job_id = ? AND status = ?` sans `SELECT` préalable dans la même transaction.
2. **`prproj_engine.py` (P-01)** : Renforcer `ProcessRegistry.abort()` pour sonder `os.killpg(pgid, 0)` jusqu'à disparition complète du groupe de processus (et pas seulement du processus parent `prproj2ffmpeg.py`) avant d'omettre le `SIGKILL` final `[cite: 1]`.
3. **`monitor_exports_and_move.py` (C-02 & R-03)** : Sécuriser l'allocation du nom de fichier final (`_v1`, `_v2`) sous un verrou de destination inter-threads, utiliser un suffixe unique pour le fichier `.part`, et garantir le `fsync` + nettoyage du `.part` en cas d'erreur I/O.

### Priorité 2 — Court Terme (Sécurité & Robustesse Opérationnelle)

4. **`app.py` (S-01, S-05 & P-04)** : Plafonner la taille du dictionnaire de `_check_login_rate_limit()` (max 4096 entrées avec purge sous verrou), rejeter `/api/health` non authentifié si des en-têtes `X-Forwarded-For` sont présents, régénérer la session au login, et ajouter un commentaire `: keepalive\n\n` toutes les 15 s dans le flux SSE `LogPublisher`.
5. **`tools/prproj-unpacker/prproj2ffmpeg.py` (S-02 & S-03)** : Plafonner la taille décompressée des `.prproj` en mémoire et vérifier `os.path.realpath` dans `resolve_media()` (avec mise à jour de `PROVENANCE.md` et `UPSTREAM-SYNC.md` `[cite: 1]`).
6. **`encoder_worker.py` (R-01 & C-03)** : Protéger la reprise post-crash contre les boucles infinies sur un `.prproj` défectueux et vérifier la stabilité des `.prproj` déposés manuellement dans `WATCH_IN`.

### Priorité 3 — Moyen Terme (Optimisation & Observabilité)

7. **`monitor_exports_and_move.py` (R-02)** : Prendre en charge l'événement `on_moved` de `watchdog` pour détecter instantanément les `os.replace()` issus de `WATCH_OUT/.staging/` sans attendre le rescan de 60 s `[cite: 1]`.
8. **`prproj_engine.py` (P-02 & P-03)** : Mettre en cache le résultat de `probe_ffmpeg()` et utiliser un décodeur UTF-8 incrémental découpant sur `\r` et `\n` dans la boucle `selectors.DefaultSelector`.

---

## 4. Protocole de Vérification et Non-Régression

Conformément aux contraintes du dépôt `[cite: 1]`, toute implémentation de ces correctifs devra être validée dans des répertoires `test_sandbox*` isolés par la séquence suivante :

```sh
./.venv/bin/python -m unittest discover -v
./.venv/bin/python -m py_compile app.py config.py db_helper.py \
  filename_utils.py prproj_engine.py submit_to_watchfolder.py \
  encoder_worker.py monitor_exports_and_move.py test_app.py test_watchdog.py
git diff --check

```

------

L'analyse du nouvel export Repomix (`repomix-output-export_prproj_encoder-main.zip.md`) permet d'auditer ligne par ligne le code source complet de `app.py`, `config.py`, `db_helper.py`, `encoder_worker.py`, `tools/prproj-unpacker/prproj2ffmpeg.py`, `tools/benchmark_export.py`, ainsi que le frontend (`static/script.js`, `templates/*.html`) et la documentation active.

(Note : l'export s'arrête à l'entête de `filename_utils.py` en raison de la taille de `prproj2ffmpeg.py` et `style.css` ; si vous souhaitez également l'inspection ligne à ligne des 4 derniers fichiers, vous pouvez générer un mini-bundle ciblé via `npx repomix --include "filename_utils.py,prproj_engine.py,submit_to_watchfolder.py,monitor_exports_and_move.py,test_*.py"`).

Voici le rapport d'audit consolidé et enrichi des vulnérabilités et conditions de course découvertes directement dans le code source inspecté, avec leurs numéros de lignes exacts et leurs correctifs prêts à l'emploi.

---

## 1. Synthèse Managériale et Matrice des Risques Révisée

L'inspection du code source réel révèle **3 nouvelles failles majeures** qui n'étaient pas visibles depuis la seule documentation :

1. **Une condition de course TOCTOU critique (`C-01`)** dans `encoder_worker.py` (lignes 185-191) où le worker peut intercepter un fichier `.prproj` fraîchement renommé par `submit_to_watchfolder.py` avant que sa ligne SQLite ne soit insérée, écrasant `OriginalFilePath` par le chemin `WATCH_IN/` et provoquant à la fois un `sqlite3.IntegrityError` dans le script de soumission et un déplacement final erroné du MP4 à la racine de l'application (`APP_ROOT`).


2. **Une fuite de sous-processus orphelin (`P-01`)** dans `app.py` (lignes 515-524) où le gestionnaire `atexit` (`stop_background_threads`) arrête les deux threads daemons mais oublie de terminer `current_process` (`submit_to_watchfolder.py`) lors d'un `SIGTERM`/`SIGINT`.


3. **Une inversion d'ordre atomique (`C-02`)** dans `encoder_worker.py` (lignes 299-311) où `os.replace(staging_path, published_path)` est exécuté **avant** `update_submission_status_by_job_id(..., STATUS_DONE)`, ce qui réveille `watchdog` alors que le job est encore au statut `EXPORT_ENCODING` en base et force une stabilisation inutile de 10 secondes.



| ID | Domaine | Constat technique | Fichier & Lignes | Sévérité |
| --- | --- | --- | --- | --- |
| **C-01** | Concurrence | Course TOCTOU entre `rename()` de soumission et `_ensure_submission()` (`UNIQUE constraint failed` + perte de `OriginalFilePath`) | `encoder_worker.py:185-191` | **Critique** |
| **C-02** | Concurrence | Publication `os.replace()` exécutée **avant** le passage du statut SQLite à `EXPORT_DONE` | `encoder_worker.py:296-312` | **Élevé** |
| **C-03** | Concurrence | Absence du mode WAL (`PRAGMA journal_mode=WAL`) dans SQLite sous accès multi-processus/multi-threads | `db_helper.py:22-27` | **Élevé** |
| **P-01** | Processus | Sous-processus `submit_to_watchfolder.py` (`current_process`) orphelin à l'arrêt de Flask (`atexit` / `SIGTERM`) | `app.py:515-524` | **Élevé** |
| **P-02** | Concurrence / UI | Contention de `process_lock` pendant `wait(timeout=5)` dans `/api/stop` bloquant `/api/status` et `monitor_process_output` | `app.py:178-182, 397-425` | **Élevé** |
| **S-01** | Sécurité | Traversée de répertoire (`..`) et suivi de liens symboliques dans `resolve_media()` | `prproj2ffmpeg.py:456-475` | **Élevé** |
| **S-02** | Sécurité | Déni de service mémoire (Memory DoS) sur `_login_failures` et comparaison non constante des identifiants | `app.py:185-221, 274` | **Élevé** |
| **S-03** | Sécurité | Décompression Gzip `.prproj` sans limite de taille en RAM (`gzip.decompress`) | `prproj2ffmpeg.py:1136-1141` | **Moyen** |
| **R-01** | Résilience | Mauvaise résolution de destination (`APP_ROOT`) pour les projets adoptés manuellement dans `WATCH_IN` | `encoder_worker.py:194-203` + `config.py:176-195` | **Moyen** |
| **R-02** | Résilience | Boucle infinie de logs (toutes les 2 s) si `_archive_source()` échoue sur un job au statut terminal | `encoder_worker.py:168-176, 340-344` | **Moyen** |
| **P-03** | Ressources | Fichiers `.tmp.mov` déterministes non purgés immédiatement sur `SIGTERM` dans `alpha_intermediate()` | `prproj2ffmpeg.py:795-826` | **Moyen** |
| **S-04** | Sécurité | Déconnexion forcée par simple requête GET sur `/logout` (absence de POST + CSRF) | `app.py:293-297` | **Faible** |

---

## 2. Constats Détaillés et Correctifs Ciblés

### Domaine A — Concurrence, Synchronisation et SQLite

#### [C-01] Condition de course TOCTOU entre `submit_to_watchfolder.py` et `_ensure_submission()` (Critique)

* **Localisation** : `encoder_worker.py`, lignes 180-203 (`_ensure_submission`).


* **Analyse du code** :
```python
def _ensure_submission(prproj_path: Path):
    job_id = extract_job_id(prproj_path.name)
    if job_id:
        row = get_submission_by_job_id(JOBS_DB_FILE, job_id)
        if row:
            return job_id, row, prproj_path
        expected_stem = build_expected_export_stem(prproj_path)
        log_submission_action(prproj_path, prproj_path.name, expected_stem, job_id)
        return job_id, get_submission_by_job_id(JOBS_DB_FILE, job_id), prproj_path

```


Dans `submit_to_watchfolder.py`, la copie atomique vers `WATCH_IN/<stem>_<job_id>.prproj` (étape 7) a lieu **avant** l'enregistrement dans SQLite via `add_submission()` (étape 9).
Si la boucle de scrutation de `encoder_worker.py` (toutes les 2 s) liste `WATCH_IN` dans la fenêtre temporelle située entre le `rename()` du fichier `.tmp` et l'insertion SQLite :


1. `extract_job_id(prproj_path.name)` trouve le `job_id`, mais `get_submission_by_job_id()` renvoie `None`.
2. `encoder_worker.py` appelle alors `log_submission_action(prproj_path, ...)` en passant **`prproj_path` (situé dans `WATCH_IN/`) comme `original_file_path**` au lieu du chemin réel `/mnt/cache/<NNN Camille>/projets/...` !


3. Lorsque `submit_to_watchfolder.py` tente d'insérer sa ligne quelques millisecondes plus tard, SQLite lève `sqlite3.IntegrityError: UNIQUE constraint failed: submissions.job_id` à cause de l'index unique `idx_submissions_job_id` (`db_helper.py`, ligne 56).


4. Pire : `OriginalFilePath` valant désormais `WATCH_IN/<stem>_<job_id>.prproj`, `resolve_project_root()` (`config.py`, ligne 195) retombe sur le fallback `file_path.parent.parent`, soit la racine du dépôt `~/export_prproj_encoder` (`APP_ROOT`) au lieu de `/mnt/cache/<NNN Camille>` !




* **Correctif recommandé** :
1. Dans `submit_to_watchfolder.py`, enregistrer ou préparer la soumission (ou ignorer dans `encoder_worker.py` tout fichier portant déjà un `job_id` mais absent de la base depuis moins de 5 secondes pour laisser la transaction de soumission se terminer).
2. Dans `encoder_worker.py` (`_ensure_submission`), si `job_id` est présent mais `row is None`, attendre brièvement ou sauter le fichier si son `mtime` est très récent (< 5 s) :


```python
job_id = extract_job_id(prproj_path.name)
if job_id:
    row = get_submission_by_job_id(JOBS_DB_FILE, job_id)
    if row:
        return job_id, row, prproj_path
    try:
        if (time.time() - prproj_path.stat().st_mtime) < 5.0:
            # Copie atomique de submit_to_watchfolder.py tout juste renommée,
            # dont l'insertion SQLite est encore en cours : on attend le prochain tour.
            return None, None, prproj_path
    except OSError:
        return None, None, prproj_path
    expected_stem = build_expected_export_stem(prproj_path)
    log_submission_action(prproj_path, prproj_path.name, expected_stem, job_id)
    return job_id, get_submission_by_job_id(JOBS_DB_FILE, job_id), prproj_path

```



#### [C-02] Inversion d'ordre entre `os.replace()` et `update_submission_status_by_job_id(..., STATUS_DONE)` (Élevé)

* **Localisation** : `encoder_worker.py`, lignes 296-312 (`_process_job`).


* **Analyse du code** :
```python
if result.output_ready:
    # Promotion atomique dans le dossier surveillé par le moniteur (même système de fichiers)
    try:
        os.replace(staging_path, published_path)
    except Exception as e:
        ...
    log_encoder_message(f"Job {job_id} TERMINÉ en {result.duration_seconds:.1f} s — ...")
    log_encoder_message(f"  Journal complet de l'encodage : '{export_log_path}'")

    update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_DONE)

```


Dès que `os.replace(staging_path, published_path)` s'exécute (ligne 299), le noyau Linux émet immédiatement l'événement `inotify` `IN_MOVED_TO` vers `monitor_exports_and_move.py`. Or le statut SQLite n'est mis à jour à `EXPORT_DONE` qu'à la ligne 311, après deux écritures disque dans `encoder_log.txt` et deux diffusions SSE.
Lorsque le thread du moniteur interroge `jobs.db` dès réception de l'événement `watchdog`, il lit encore le statut `EXPORT_ENCODING` et ne saute donc pas la boucle de stabilisation (`2 × 5 s = 10 s`) pourtant prévue pour les fichiers `EXPORT_DONE` !


* **Correctif recommandé** :
Passer le statut à `STATUS_DONE` juste avant `os.replace(staging_path, published_path)` (et le repasser à `STATUS_FAILED` dans le bloc `except` si `os.replace` échoue) :
```python
if result.output_ready:
    update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_DONE)
    try:
        os.replace(staging_path, published_path)
    except Exception as e:
        log_encoder_message(f"  ERREUR: impossible de publier '{staging_path.name}' vers '{published_path}': {e}")
        _discard_staging()
        update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_FAILED)
        _set_state(last_error=f"publication impossible: {e}")
        _bump('failed')
        _archive_source(prproj_path)
        return STATUS_FAILED

```



#### [C-03] Absence du mode WAL (`PRAGMA journal_mode=WAL`) dans `db_helper.py` (Élevé)

* **Localisation** : `db_helper.py`, lignes 22-27 (`get_db_connection`).


* **Analyse du code** :
```python
def get_db_connection(db_path: Path):
    """Retourne une connexion à la base SQLite (timeout large pour absorber les accès concurrents)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

```


Sans `PRAGMA journal_mode=WAL`, SQLite utilise le journal de rollback par défaut (`DELETE`), qui bloque toutes les lectures pendant une écriture et inversement. De plus, au démarrage (`app.py`, lignes 504 et 513), les threads `prproj-encoder` et `export-monitor` appellent `init_db(JOBS_DB_FILE)` exactement au même instant.


* **Correctif recommandé** :
Activer `WAL` et `synchronous=NORMAL` dans `get_db_connection()` :
```python
def get_db_connection(db_path: Path):
    """Retourne une connexion à la base SQLite (mode WAL + timeout large pour les accès concurrents)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn

```



---

### Domaine B — Cycle de Vie des Processus et Gestion des Ressources

#### [P-01] Fuite du sous-processus `submit_to_watchfolder.py` lors de l'arrêt de l'application (Élevé)

* **Localisation** : `app.py`, lignes 515-524 (`stop_background_threads`).


* **Analyse du code** :
```python
@atexit.register
def stop_background_threads():
    print("Arrêt de l'encodeur et du moniteur d'exports...")
    encoder_worker.shutdown()
    encoder_stop_event.set()
    if encoder_thread is not None:
        encoder_thread.join(timeout=10)
    monitor_stop_event.set()
    if monitor_thread is not None:
        monitor_thread.join(timeout=5)

```


Lorsque `./stop_app.sh` envoie `SIGTERM` au serveur Flask pendant qu'un scan/copie `submit_to_watchfolder.py` (`current_process`) est en cours d'exécution, `stop_background_threads()` arrête `encoder_worker` et `monitor_thread`, mais **ne termine pas `current_process**`. Le sous-processus `submit_to_watchfolder.py` survit comme processus orphelin et continue d'écrire dans `WATCH_IN/` et `jobs.db`.


* **Correctif recommandé** :
Terminer proprement `current_process` dans `stop_background_threads()` :
```python
@atexit.register
def stop_background_threads():
    print("Arrêt de la soumission, de l'encodeur et du moniteur d'exports...")
    with process_lock:
        proc = current_process
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
    encoder_worker.shutdown()
    encoder_stop_event.set()
    if encoder_thread is not None:
        encoder_thread.join(timeout=10)
    monitor_stop_event.set()
    if monitor_thread is not None:
        monitor_thread.join(timeout=5)

```



#### [P-02] Blocage de `process_lock` pendant `wait(timeout=5)` dans `/api/stop` et désynchronisation d'état (Élevé)

* **Localisation** : `app.py`, lignes 154-182 (`monitor_process_output`) et lignes 397-425 (`stop_script`).


* **Analyse du code** :
Dans `stop_script()` (ligne 401), le thread HTTP acquiert `with process_lock:` et appelle `current_process.wait(timeout=5)` (puis éventuellement `kill()` + `wait(timeout=2)`) **sans relâcher `process_lock**`.
Pendant ces 5 à 7 secondes :


1. Toutes les requêtes `GET /api/status` (émises toutes les 2 s par `static/script.js`) restent bloquées à la ligne 330 (`with process_lock:`).


2. Le thread `monitor_process_output` qui détecte la fin du processus reste bloqué à la ligne 178 (`with process_lock:`) et ne peut pas remettre `script_running = False` tant que `stop_script()` n'est pas sorti du bloc `with`.


3. De plus, `process.stdout` n'est jamais fermé explicitement (`process.stdout.close()`) dans le bloc `finally:` de `monitor_process_output`.




* **Correctif recommandé** :
Dans `stop_script()`, récupérer la référence `proc = current_process` sous `process_lock`, exécuter `terminate()` / `wait()` / `kill()` **hors** du verrou (ou mettre à jour `script_running = False` et `current_process = None` sous verrou dès la mort confirmée du processus), et fermer `process.stdout.close()` dans le `finally` de `monitor_process_output`.

#### [P-03] Fichiers `.tmp.mov` déterministes et résidus sur `SIGTERM` dans `prproj2ffmpeg.py` (Moyen)

* **Localisation** : `tools/prproj-unpacker/prproj2ffmpeg.py`, lignes 795-826 (`alpha_intermediate`) et lignes 884-898 (`_bbox_cache_write`).


* **Analyse du code** :
1. `tmp = cached + '.tmp.mov'` (ligne 797) et `tmp = fp + '.tmp'` (ligne 889) utilisent un nom fixe par clé de cache. Si deux processus préparent le même média simultanément (ex. benchmark CLI + worker), ils écrivent dans le même fichier temporaire.


2. Lors d'un abandon (`ProcessRegistry.abort()`), `SIGTERM` interrompt `prproj2ffmpeg.py` pendant `subprocess.run(..., timeout=1800)` (ligne 808) sans passer par le bloc `if cached is None and os.path.exists(tmp): os.remove(tmp)` (ligne 825), laissant un fichier `.tmp.mov` volumineux sur le disque jusqu'à l'expiration de 24 h de `_prune_cache()`.




* **Correctif recommandé** :
Inclure `os.getpid()` dans le préfixe temporaire (`tmp = f"{cached}.{os.getpid()}.tmp.mov"` et `tmp = f"{fp}.{os.getpid()}.tmp"`) pour éviter toute collision inter-processus, et nettoyer les `.tmp.mov` orphelins plus récents si aucun processus ne les détient.

------------------------------

### Domaine C — Sécurité Applicative et Réseau (OWASP & Hardening)

#### [S-01] Traversée de répertoire (`..`) et suivi de liens symboliques dans `resolve_media()` (Élevé)

* **Localisation** : `tools/prproj-unpacker/prproj2ffmpeg.py`, lignes 456-475 (`resolve_media`).


* **Analyse du code** :
```python
def resolve_media(win_path, bases):
    if not win_path:
        return None
    cache_key = (win_path, tuple(str(base) for base in bases))
    if cache_key in _resolve_cache:
        return _resolve_cache[cache_key]
    norm = win_path.replace('\\', '/').replace(':', '')
    parts = [p for p in norm.split('/') if p]
    for base in bases:
        for skip in range(0, len(parts)):
            cand = os.path.join(str(base), *parts[skip:])
            if os.path.isfile(cand):
                _resolve_cache[cache_key] = cand
                return cand

```


Deux failles de traversée de chemin sont présentes :
1. `parts = [p for p in norm.split('/') if p]` **ne filtre pas les segments `'..'**` ! Si un fichier `.prproj` contient `<FilePath>F:\Projet\..\..\..\..\etc\shadow</FilePath>` ou `..\..\autre_dossier\secret.mp4`, `os.path.join(str(base), *parts[skip:])` remonte au-dessus de `base`.


2. `os.path.isfile(cand)` suit les liens symboliques sans vérifier que la cible résolue (`os.path.realpath(cand)`) se trouve bien à l'intérieur de `os.path.realpath(base)` (contrairement à `submit_to_watchfolder.py` qui rejette les liens symboliques via `is_safe_path()`).




* **Correctif recommandé** :
Filtrer `.` et `..` lors du découpage et vérifier le confinement canonique sous `base` :
```python
parts = [p for p in norm.split('/') if p and p not in ('.', '..')]
for base in bases:
    real_base = os.path.realpath(str(base))
    for skip in range(0, len(parts)):
        cand = os.path.join(real_base, *parts[skip:])
        if os.path.islink(cand):
            continue
        real_cand = os.path.realpath(cand)
        try:
            if os.path.commonpath([real_cand, real_base]) != real_base:
                continue
        except ValueError:
            continue
        if os.path.isfile(real_cand):
            _resolve_cache[cache_key] = real_cand
            return real_cand

```



#### [S-02] Déni de service mémoire sur `_login_failures`, expiration de blocage et comparaison des identifiants (Élevé)

* **Localisation** : `app.py`, lignes 185-221 (`_is_login_blocked`, `_record_login_failure`) et ligne 274 (`login_route`).


* **Analyse du code** :
1. **Fuite mémoire sur `_login_failures**` : les entrées du dictionnaire `_login_failures` ne sont supprimées que si la *même* IP retente une connexion après `LOGIN_ATTEMPT_WINDOW_SECONDS`. Il n'existe ni purge globale des clés expirées ni plafond sur `len(_login_failures)`.


2. **Logique de fin de blocage dans `_is_login_blocked` (lignes 199-205)** :
```python
if entry.get("blocked_until") and now < entry["blocked_until"]:
    return True
if now - entry["first_ts"] > LOGIN_ATTEMPT_WINDOW_SECONDS:
    del _login_failures[client_ip]
    return False
return entry["count"] >= LOGIN_MAX_ATTEMPTS

```


Si `entry["blocked_until"]` est défini et que `now >= entry["blocked_until"]` (la punition de 5 minutes est écoulée), l'entrée devrait être purgée immédiatement (`del _login_failures[client_ip]; return False`) au lieu de dépendre de `first_ts`.


3. **Comparaison non constante des identifiants (ligne 274)** :
```python
if username == API_USER and password == current_api_password:

```


L'opérateur `==` court-circuite dès le premier octet différent (et n'évalue pas `password` si `username` est faux), créant un canal auxiliaire temporel, alors que `hmac.compare_digest` est déjà importé dans `app.py` (ligne 11).




* **Correctif recommandé** :
```python
MAX_TRACKED_LOGIN_IPS = 4096

def _prune_login_failures_unlocked(now: float):
    expired = [
        ip for ip, data in _login_failures.items()
        if (data.get("blocked_until") and now >= data["blocked_until"])
        or (not data.get("blocked_until") and now - data["first_ts"] > LOGIN_ATTEMPT_WINDOW_SECONDS)
    ]
    for ip in expired:
        del _login_failures[ip]
    while len(_login_failures) >= MAX_TRACKED_LOGIN_IPS:
        oldest_ip = next(iter(_login_failures))
        del _login_failures[oldest_ip]

```


Et dans `login_route()` (ligne 274) :
```python
user_ok = hmac.compare_digest((username or '').encode('utf-8'), API_USER.encode('utf-8'))
pass_ok = hmac.compare_digest((password or '').encode('utf-8'), current_api_password.encode('utf-8'))
if user_ok and pass_ok:

```



#### [S-03] Décompression Gzip `.prproj` sans plafond de taille en mémoire (Moyen)

* **Localisation** : `tools/prproj-unpacker/prproj2ffmpeg.py`, lignes 1136-1141 (`main`).


* **Analyse du code** :
```python
with open(src, 'rb') as f:
    raw = f.read()
xml_bytes = gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw
parsed = Project(src, xml_bytes=xml_bytes)

```


`gzip.decompress(raw)` décompresse l'intégralité du flux en RAM sans limite. Un fichier `.prproj` corrompu ou malformé (bombe Gzip) peut provoquer un `MemoryError` ou déclencher l'OOM Killer Linux.
* **Correctif recommandé** :
Plafonner la lecture décompressée (ex. `_MAX_PRPROJ_XML_BYTES = 256 * 1024 * 1024`) via `gzip.GzipFile` :
```python
_MAX_PRPROJ_XML_BYTES = 256 * 1024 * 1024  # 256 Mio max

def _read_prproj_xml_bytes(src_path):
    with open(src_path, 'rb') as f:
        magic = f.read(2)
        f.seek(0)
        if magic == b'\x1f\x8b':
            with gzip.GzipFile(fileobj=f, mode='rb') as gz:
                data = gz.read(_MAX_PRPROJ_XML_BYTES + 1)
        else:
            data = f.read(_MAX_PRPROJ_XML_BYTES + 1)
    if len(data) > _MAX_PRPROJ_XML_BYTES:
        raise ValueError(f"projet XML trop volumineux (> {_MAX_PRPROJ_XML_BYTES // (1024 * 1024)} Mio)")
    return data

```



---

### Domaine D — Robustesse, Intégrité du Stockage et Reprise Post-Crash

#### [R-01] Mauvaise destination finale (`APP_ROOT`) pour les projets adoptés manuellement dans `WATCH_IN` (Moyen)

* **Localisation** : `encoder_worker.py`, lignes 194-203 (`_ensure_submission`) et `config.py`, lignes 176-195 (`resolve_project_root`).


* **Analyse du code** :
Lorsqu'un fichier `.prproj` est déposé manuellement dans `WATCH_IN/`, `_ensure_submission()` l'adopte et appelle :
```python
log_submission_action(adopted_path, unique_name, expected_stem, job_id)

```


La colonne `original_file_path` dans `jobs.db` reçoit donc `/home/kidpixel/export_prproj_encoder/WATCH_IN/<stem>_<job_id>.prproj`.
Lorsque l'encodage se termine, `monitor_exports_and_move.py` retrouve ce `job_id` dans `jobs.db` et calcule le dossier de destination final en appelant `resolve_project_root(Path(row['OriginalFilePath']))` (`config.py`, ligne 176).
Or, `WATCH_IN/<stem>_<job_id>.prproj` n'est ni sous `BASE_SCAN_FOLDER` (`/mnt/cache`), ni dans un sous-dossier `projets` : `resolve_project_root()` exécute donc son Fallback 2 (ligne 195) :


```python
return file_path.parent.parent

```


Le parent de `WATCH_IN` étant la racine du dépôt (`APP_ROOT = /home/kidpixel/export_prproj_encoder`), **le moniteur déplace le fichier MP4 final au milieu du code source Python de l'application** !
* **Correctif recommandé** :
Dans `monitor_exports_and_move.py` (ou `resolve_project_root`), vérifier si `OriginalFilePath` est situé dans `WATCH_FOLDER_INPUT`. Si tel est le cas (projet déposé manuellement dans `WATCH_IN` sans dossier projet d'origine connu sous `/mnt/cache`), ne pas déplacer le MP4 dans `APP_ROOT`, mais appliquer la politique des fichiers sans projet connu (`quarantine/` ou `ENABLE_FALLBACK_MOVE`).

#### [R-02] Boucle infinie de journalisation si `_archive_source()` échoue sur un statut terminal (Moyen)

* **Localisation** : `encoder_worker.py`, lignes 168-176 (`_archive_source`) et lignes 327-345 (`_pick_next_job`).


* **Analyse du code** :
Si `_archive_source(job_path)` échoue (ex. problème de permissions sur `WATCH_IN/Source/`), l'exception est capturée et le fichier `.prproj` reste dans `WATCH_IN/` alors que son statut en base est terminal (`EXPORT_FAILED`, `EXPORT_ABORTED`, `EXPORT_SUPERSEDED`).
À chaque cycle de `start_encoder()` (toutes les 2 s), `_pick_next_job()` retrouve ce fichier, journalise `Job ... au statut '...' : projet archivé sans encodage`, rappelle `_archive_source(job_path)` qui échoue à nouveau, et boucle indéfiniment.


* **Correctif recommandé** :
Conserver en mémoire dans `encoder_worker.py` un ensemble `_unarchivable_paths: set[Path]` (purgé si le fichier disparaît) afin de ne tenter l'archivage d'un job terminal récalcitrant qu'une seule fois et de l'ignorer lors des tours suivants tant qu'il n'a pas été déplacé.

---

## 3. Feuille de Route Priorisée

1. **Priorité 1 (Immédiate — `db_helper.py` & `encoder_worker.py`)** :
* Activer `PRAGMA journal_mode=WAL` et `PRAGMA synchronous=NORMAL` dans `db_helper.get_db_connection()` (**C-03**).
* Protéger `_ensure_submission()` contre la course TOCTOU avec `submit_to_watchfolder.py` (**C-01**).
* Mettre à jour le statut SQLite à `STATUS_DONE` *avant* `os.replace(staging_path, published_path)` dans `_process_job()` (**C-02**).


2. **Priorité 2 (Court terme — `app.py` & `prproj2ffmpeg.py`)** :
* Terminer `current_process` dans `stop_background_threads()` (`atexit`) et relâcher `process_lock` avant `wait()` dans `/api/stop` (**P-01**, **P-02**).
* Filtrer `'..'` et vérifier `os.path.realpath` dans `resolve_media()`, et plafonner la décompression Gzip à 256 Mio dans `prproj2ffmpeg.py` (**S-01**, **S-03** — avec mise à jour de `PROVENANCE.md` et `UPSTREAM-SYNC.md`).


* Plafonner `_login_failures` et utiliser `hmac.compare_digest` sur les identifiants dans `app.py` (**S-02**).


3. **Priorité 3 (Moyen terme — Résilience)** :
* Éviter le repli de `resolve_project_root()` sur `APP_ROOT` pour les projets adoptés depuis `WATCH_IN` (**R-01**) et éviter la boucle de logs en cas d'échec d'`_archive_source()` (**R-02**).

------------------------------

L'intégralité des fichiers sources du dépôt étant désormais réunie (`app.py`, `config.py`, `db_helper.py`, `encoder_worker.py`, `tools/prproj-unpacker/prproj2ffmpeg.py`, `filename_utils.py`, `prproj_engine.py`, `submit_to_watchfolder.py`, `monitor_exports_and_move.py`, `test_app.py` et `test_watchdog.py`), voici le **rapport d'audit backend complet et définitif**, référencé ligne par ligne sur le code réel.

---

## 1. Synthèse Managériale et Matrice Complète des Risques

L'architecture de `export_prproj_encoder` est bien découpée et respecte de bonnes pratiques fondamentales (absence de `shell=True`, staging d'encodage, vérification de taille avant renommage, protection CSRF en temps constant, CSP stricte).

Cependant, l'inspection croisée des 8 modules backend met en lumière **16 constats techniques** (1 Critique, 6 Élevés, 6 Moyens, 3 Faibles), dont plusieurs conditions de course inter-processus/inter-threads et fuites de processus qui ne peuvent être détectées qu'en croisant le code de `submit_to_watchfolder.py`, `encoder_worker.py`, `prproj_engine.py` et `monitor_exports_and_move.py`.

| ID | Domaine | Constat technique | Fichier & Lignes exactes | Sévérité |
| --- | --- | --- | --- | --- |
| **C-01** | Concurrence / Intégrité | Course TOCTOU entre la copie `WATCH_IN` et l'insertion SQLite : adoption parasite par l'encodeur, `IntegrityError` et corruption de `OriginalFilePath` | `submit_to_watchfolder.py:476-483` / `encoder_worker.py:180-191`

 | **Critique** |
| **P-01** | Cycle de vie Processus | Survie du processus enfant `ffmpeg` lors d'un `abort()` : `process.wait()` retourne dès la mort du parent `prproj2ffmpeg.py` sans jamais envoyer `SIGKILL` au groupe | `prproj_engine.py:139-159`

 | **Élevé** |
| **C-02** | Concurrence / Stockage | Condition de course TOCTOU entre les 8 threads du moniteur sur `get_versioned_path()` et collision sur le fichier temporaire unique `.{nom}.part` | `monitor_exports_and_move.py:137, 412-442`

 | **Élevé** |
| **C-03** | Concurrence / Perf | Publication `os.replace()` exécutée **avant** `update_submission_status_by_job_id(..., STATUS_DONE)` : `watchdog` lit `EXPORT_ENCODING` et impose 10 s d'attente | `encoder_worker.py:296-312` / `monitor_exports_and_move.py:242-259`

 | **Élevé** |
| **C-04** | Concurrence / SQLite | Absence du mode `WAL` (`PRAGMA journal_mode=WAL`) sous accès concurrent (Flask + encodeur + 8 threads moniteur + sous-processus soumission) | `db_helper.py:22-27`

 | **Élevé** |
| **P-02** | Processus / Verrous | Sous-processus `current_process` orphelin sur `SIGTERM`/`atexit` et blocage de `process_lock` pendant `wait(timeout=5)` dans `/api/stop` | `app.py:397-425, 515-524`

 | **Élevé** |
| **S-01** | Sécurité (Path Traversal) | Traversée de répertoire (`..`) et suivi de liens symboliques dans `resolve_media()` lors de la résolution des chemins du XML `.prproj` | `prproj2ffmpeg.py:456-475`

 | **Élevé** |
| **S-02** | Sécurité (DoS / Auth) | Dictionnaire `_login_failures` non borné en RAM (Memory DoS) et comparaison `==` non constante des identifiants | `app.py:185-221, 274`

 | **Moyen** |
| **S-03** | Sécurité (DoS RAM) | Bombe de décompression Gzip en mémoire (`gzip.decompress(raw)` sans limite) sur un fichier `.prproj` | `prproj2ffmpeg.py:1136-1141`

 | **Moyen** |
| **R-01** | Résilience / Stockage | Destination finale erronée (`APP_ROOT`) pour tout `.prproj` déposé manuellement dans `WATCH_IN` (`resolve_project_root` fallback) | `encoder_worker.py:194-203` / `config.py:176-195`

 | **Moyen** |
| **R-02** | Résilience / Moniteur | `sweep_existing_exports()` synchrone bloquant le thread principal du moniteur et fuite de `ThreadPoolExecutor` au redémarrage de `Observer` | `monitor_exports_and_move.py:536-550, 604-617`

 | **Moyen** |
| **R-03** | Résilience / Matching | Oubli de `strip_job_id()` avant `get_base_export_stem()` dans le moniteur, mettant en échec le rapprochement heuristique sur les fichiers avec `job_id` | `monitor_exports_and_move.py:333-398`

 | **Moyen** |
| **R-04** | Résilience / Encodeur | Boucle infinie de journalisation (toutes les 2 s) si `_archive_source()` échoue sur un job à statut terminal | `encoder_worker.py:168-176, 327-345`

 | **Moyen** |
| **P-03** | Ressources / Cache | Fichiers temporaires `.tmp.mov` déterministes non nettoyés immédiatement lors d'un `SIGTERM` pendant `alpha_intermediate()` | `prproj2ffmpeg.py:795-826`

 | **Faible** |
| **C-05** | Déterminisme / CSV | Perte de l'ordre déterministe du scan via `list(set(...))` et écritures CSV concurrentes sans `flock` | `submit_to_watchfolder.py:137-143, 322`

 | **Faible** |
| **S-04** | Sécurité HTTP | Route `/logout` en `GET` sans protection CSRF et absence de vérification `X-Forwarded-For` sur `/api/health` | `app.py:293-297, 306-312`

 | **Faible** |

---

## 2. Cartographie et Analyse Statique des Flux Backend (Étape 1)

```
[submit_to_watchfolder.py] (sous-processus lancé par app.py sous process_lock)
   ├── Scanne /mnt/cache/*Camille*/projets/*.prproj (is_safe_path + select_assist_priority)
   ├── Copie .tmp -> rename vers WATCH_IN/{stem}_{job_id}.prproj
   ├── Insère SUBMITTED_TO_WATCHFOLDER dans jobs.db + submission_log.csv
   └── Marque les versions normales en attente en EXPORT_SUPERSEDED

[encoder_worker.py] (thread daemon "prproj-encoder")
   ├── Scrute WATCH_IN toutes les 2 s (_list_pending_files, FIFO par mtime)
   ├── Adopte ou vérifie la soumission dans jobs.db (_ensure_submission)
   ├── Lit les métadonnées via prproj_engine.read_project_info (--dump)
   ├── Passe le statut à EXPORT_ENCODING
   ├── Exécute prproj_engine.run_export -> prproj2ffmpeg.py -> ffmpeg (session isolée)
   │     └── Sortie dans WATCH_OUT/.staging/{stem}_{job_id}.mp4
   ├── os.replace() vers WATCH_OUT/{stem}_{job_id}.mp4 + statut EXPORT_DONE
   └── Archive le .prproj dans WATCH_IN/Source/<horodatage>/

[monitor_exports_and_move.py] (thread daemon "export-monitor" + ThreadPoolExecutor(8))
   ├── Écoute WATCH_OUT via watchdog (on_created, on_moved) + rescan 60 s
   ├── Saute _wait_for_stability si statut SQLite == EXPORT_DONE*
   ├── Apparie par job_id (ou fallback heuristique) dans jobs.db
   └── Déplace via move_to_final_verified (os.replace ou copie .part inter-FS)
         vers resolve_project_root(OriginalFilePath) avec versionnage _v1, _v2
         et passe le statut à EXPORT_MOVED_TO_FINAL (ou quarantaine)

```

---

## 3. Constats Détaillés par Domaine (Étapes 2 à 5)

### Domaine A — Concurrence, Threading et Synchronisation

#### [C-01] Condition de course TOCTOU entre `copy_to_watchfolder_root()` et `_ensure_submission()` (Critique)

* **Composants & Lignes** : `submit_to_watchfolder.py` lignes 476-483 et `encoder_worker.py` lignes 180-191.


* **Description technique** :
Dans `submit_to_watchfolder.py` (lignes 476-483) :


```python
job_id = f"{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
success = copy_to_watchfolder_root(prproj_path, WATCH_FOLDER_INPUT, job_id)
if success:
    projects_successfully_submitted_count += 1
    expected_export_stem_for_log = build_expected_export_stem(prproj_path)
    unique_watchfolder_name = f"{prproj_path.stem}_{job_id}{prproj_path.suffix}"
    log_submission_action(prproj_path, unique_watchfolder_name, expected_export_stem_for_log, job_id)

```


Le fichier `.prproj` est renommé à son emplacement final `WATCH_IN/{stem}_{job_id}.prproj` **avant** que `log_submission_action()` n'insère la ligne correspondante dans `jobs.db`.
Or, `encoder_worker.py` scrute `WATCH_IN` toutes les 2 secondes en parallèle et exécute `_ensure_submission()` (lignes 180-191) :


```python
job_id = extract_job_id(prproj_path.name)
if job_id:
    row = get_submission_by_job_id(JOBS_DB_FILE, job_id)
    if row:
        return job_id, row, prproj_path
    expected_stem = build_expected_export_stem(prproj_path)
    log_submission_action(prproj_path, prproj_path.name, expected_stem, job_id)
    return job_id, get_submission_by_job_id(JOBS_DB_FILE, job_id), prproj_path

```


* **Scénario d'occurrence / PoC** :
1. `submit_to_watchfolder.py` termine `tmp_destination_path.rename(destination_path)` (ligne 350).


2. Au même instant, `encoder_worker.py` liste `WATCH_IN`, voit le fichier avec son `job_id`, interroge `jobs.db` (`row` vaut `None`), et appelle immédiatement `log_submission_action(prproj_path, ...)` où `prproj_path` est **`WATCH_IN/{stem}_{job_id}.prproj`** !


3. `encoder_worker.py` insère donc dans `jobs.db` `original_file_path = ".../WATCH_IN/{stem}_{job_id}.prproj"`.
4. Quelques millisecondes plus tard, `submit_to_watchfolder.py` exécute `log_submission_action()` (ligne 482) avec le vrai chemin `/mnt/cache/.../projets/...`, mais SQLite rejette l'insertion avec `IntegrityError: UNIQUE constraint failed: submissions.job_id` (`db_helper.py` ligne 56) !


5. En fin d'encodage, `monitor_exports_and_move.py` lit `OriginalFilePath = ".../WATCH_IN/..."`, appelle `resolve_project_root()` (`config.py` ligne 195 : `file_path.parent.parent`), et déplace le MP4 final dans la racine de l'application (`APP_ROOT`) au lieu du dossier client `/mnt/cache/<NNN Camille>/` !




* **Recommandation de remédiation** :
Dans `submit_to_watchfolder.py` (lignes 476-485), enregistrer la soumission en base **avant** le `rename` final (ou supprimer la ligne si la copie échoue), **et** dans `encoder_worker.py` (`_ensure_submission`, ligne 185), ne pas ré-enregistrer à la volée un fichier portant déjà un `job_id` dont le `mtime` a moins de 10 secondes si la ligne SQLite n'est pas encore visible.



#### [C-02] Condition de course TOCTOU entre les 8 threads du moniteur sur `get_versioned_path()` et `.part` (Élevé)

* **Composant & Lignes** : `monitor_exports_and_move.py` lignes 69, 125-156, 194-196 et 412-442.


* **Description technique** :
`ExportHandler` utilise un `ThreadPoolExecutor(max_workers=8)` (ligne 196). Le verrou `self.processing_lock` (ligne 218) ne déduplique que le chemin **source** (`norm_path` dans `WATCH_OUT/`).
Si deux fichiers sources distincts dans `WATCH_OUT/` ciblent le même projet de destination (ex. deux exports successifs du même projet traités lors d'un `sweep_existing_exports` ou deux variantes produisant le même `ExpectedExportName`) :


1. Les deux threads calculent `final_destination_path` et appellent `get_versioned_path(final_destination_path)` (ligne 420) simultanément hors verrou : ils obtiennent **tous les deux la même cible** (ex. `Projet_9x16_v1.mp4`).


2. Les deux threads appellent `move_to_final_verified(exported_file_path, final_destination_path)` (ligne 440).


3. À la ligne 137 (`temp_path = destination_path.parent / f".{destination_path.name}.part"`), le nom du fichier `.part` ne dépend que de `destination_path.name` : les deux threads copient en parallèle dans le **même** fichier `.part`, puis le premier `os.replace(temp_path, destination_path)` supprime `temp_path`, faisant échouer le second thread en `FileNotFoundError` ou écrasant silencieusement le premier export sans incrémenter en `_v2` !




* **Recommandation de remédiation** :
1. Ajouter un verrou de réservation des destinations (`_dest_lock = threading.Lock()` et `_reserved_destinations: set[Path] = set()`) dans `ExportHandler` pour rendre atomique le bloc « choix de `get_versioned_path` + réservation du chemin cible » jusqu'à la fin du déplacement.
2. Dans `move_to_final_verified()` (ligne 137), inclure un suffixe unique par opération dans le nom du fichier temporaire :


```python
temp_path = destination_path.parent / f".{destination_path.name}.{os.getpid()}_{threading.get_ident()}.part"

```





#### [C-03] Publication `os.replace()` effectuée avant la mise à jour SQLite `EXPORT_DONE` (Élevé)

* **Composants & Lignes** : `encoder_worker.py` lignes 296-312 et `monitor_exports_and_move.py` lignes 242-259, 323-325.


* **Description technique** :
Dans `encoder_worker.py` (`_process_job`, lignes 296-312) :


1. Ligne 299 : `os.replace(staging_path, published_path)` déplace le MP4 dans `WATCH_OUT/`.


2. Lignes 308-309 : `log_encoder_message(...)` écrit deux fois sur disque et pousse vers SSE.


3. Ligne 311 : `update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_DONE)` met enfin à jour SQLite.
Or, dès la ligne 299, `watchdog` déclenche `on_moved` dans `monitor_exports_and_move.py` (ligne 530). Si `log_encoder_message` ou le disque prend du temps (ex. rotation ou flush), ou lors d'un `sweep_existing_exports` concurrent (qui n'a pas le `time.sleep(1)` de `_process_file_async` !), `_is_published_by_encoder()` (ligne 254) lit encore le statut `EXPORT_ENCODING` dans `jobs.db`, renvoie `False`, et force le fichier à subir inutilement `_wait_for_stability()` (`2 × 5 s = 10 s`).




* **Recommandation de remédiation** :
Dans `encoder_worker.py` (ligne 296), appeler `update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_DONE)` **avant** `os.replace(staging_path, published_path)` (et repasser à `STATUS_FAILED` dans le `except` si `os.replace` échoue).



#### [C-04] Absence du mode `WAL` sur SQLite (`jobs.db`) (Élevé)

* **Composant & Lignes** : `db_helper.py` lignes 22-27.


* **Description technique** :
`get_db_connection()` ouvre `sqlite3.connect(str(db_path), timeout=30.0)` en mode journalisation par défaut (`DELETE`). Avec 8 threads dans le moniteur, 1 thread encodeur, Flask et le sous-processus `submit_to_watchfolder.py`, les lectures bloquent les écritures et inversement.


* **Recommandation de remédiation** :
Ajouter `conn.execute("PRAGMA journal_mode=WAL")` et `conn.execute("PRAGMA synchronous=NORMAL")` dans `get_db_connection()` (`db_helper.py` ligne 26).



---

### Domaine B — Cycle de Vie des Processus et Gestion des Ressources

#### [P-01] Processus `ffmpeg` orphelins lors d'un `ProcessRegistry.abort()` (Élevé)

* **Composant & Lignes** : `prproj_engine.py` lignes 139-159 (`_terminate_process_group`) et lignes 369-398 (`_stream_output`).


* **Description technique** :
Regardons `_terminate_process_group` dans `prproj_engine.py` (lignes 139-159) :


```python
_signal_group(signal.SIGTERM)
try:
    process.wait(timeout=grace_seconds)
    return
except subprocess.TimeoutExpired:
    pass
_signal_group(signal.SIGKILL)

```


`process` est l'interpréteur Python exécutant `prproj2ffmpeg.py` (lancé avec `start_new_session=True`), qui a lui-même lancé un sous-processus enfant `ffmpeg` (`subprocess.call(cmd)` à la ligne 1468 de `prproj2ffmpeg.py`, ou `subprocess.run(cmd)` aux lignes 808 et 939).
Lorsque `os.killpg(pgid, signal.SIGTERM)` est appelé :


1. Le processus parent `prproj2ffmpeg.py` n'a pas de gestionnaire `SIGTERM` : il est interrompu par Python et se termine en quelques millisecondes.
2. `process.wait(timeout=grace_seconds)` (qui n'attend que le PID de `prproj2ffmpeg.py`, pas le groupe de processus !) réussit immédiatement et exécute **`return` à la ligne 153** !


3. **Le `SIGKILL` de la ligne 156 n'est donc jamais envoyé au groupe `pgid**`, même si le processus enfant `ffmpeg` est bloqué sur un appel système I/O (`/mnt/cache`) ou ignore le premier `SIGTERM` !


* **Scénario d'occurrence / PoC** : Clic sur « Arrêter » (`/api/stop`) pendant un transcodage alpha `qtrle` ou un rendu complexe -> `prproj2ffmpeg.py` meurt immédiatement, `process.wait()` retourne sans erreur, `_terminate_process_group` quitte à la ligne 153, et `ffmpeg` continue de tourner en arrière-plan.
* **Recommandation de remédiation** :
Dans `_terminate_process_group()` (`prproj_engine.py` lignes 150-159), après `process.wait()`, attendre l'extinction effective de **tous** les membres du groupe `pgid` via `os.killpg(pgid, 0)` jusqu'à `deadline = time.monotonic() + grace_seconds`, et envoyer `os.killpg(pgid, signal.SIGKILL)` si le groupe existe encore à l'échéance :


```python
deadline = time.monotonic() + max(0.1, float(grace_seconds))
_signal_group(signal.SIGTERM)
try:
    remaining = max(0.0, deadline - time.monotonic())
    process.wait(timeout=remaining)
except subprocess.TimeoutExpired:
    pass

while time.monotonic() < deadline:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return
    except PermissionError:
        break
    time.sleep(0.05)

_signal_group(signal.SIGKILL)
try:
    process.wait(timeout=2)
except subprocess.TimeoutExpired:
    pass

```



#### [P-02] Sous-processus `submit_to_watchfolder.py` orphelin à l'extinction et contention sur `process_lock` (Élevé)

* **Composant & Lignes** : `app.py` lignes 154-182, 397-425 et 515-524.


* **Description technique** :
1. Dans `stop_background_threads()` (`app.py` lignes 515-524, enregistré via `@atexit.register`), seuls `encoder_worker` et `monitor_thread` sont arrêtés. Si l'application reçoit `SIGTERM` (`./stop_app.sh`) pendant que `current_process` (`submit_to_watchfolder.py`) copie des fichiers, `current_process` n'est pas arrêté et survit en orphelin.


2. Dans `stop_script()` (`app.py` lignes 401-425), `current_process.wait(timeout=5)` (puis `kill()` + `wait(timeout=2)`) est exécuté **à l'intérieur** du bloc `with process_lock:`. Cela bloque pendant jusqu'à 7 secondes toutes les requêtes `GET /api/status` (ligne 330) ainsi que le bloc `finally:` de `monitor_process_output` (ligne 178).


3. Dans `monitor_process_output()` (`app.py` ligne 154), le tube `process.stdout` n'est pas fermé explicitement dans le bloc `finally:`.




* **Recommandation de remédiation** :
* Fermer `process.stdout.close()` dans le `finally:` de `monitor_process_output()`.


* Dans `stop_script()`, lire `proc = current_process` sous `process_lock`, puis effectuer `proc.terminate()` et `proc.wait(timeout=5)` **hors** du verrou `process_lock`.
* Dans `stop_background_threads()` (ligne 515), terminer `current_process` s'il est actif.





---

### Domaine C — Sécurité Applicative et Réseau (OWASP & Hardening)

#### [S-01] Traversée de répertoire (`..`) et suivi de liens symboliques dans `resolve_media()` (Élevé)

* **Composant & Lignes** : `tools/prproj-unpacker/prproj2ffmpeg.py` lignes 456-475.


* **Description technique** :
```python
norm = win_path.replace('\\', '/').replace(':', '')
parts = [p for p in norm.split('/') if p]
for base in bases:
    for skip in range(0, len(parts)):
        cand = os.path.join(str(base), *parts[skip:])
        if os.path.isfile(cand):
            _resolve_cache[cache_key] = cand
            return cand

```


Contrairement à `submit_to_watchfolder.py` qui valide chaque fichier avec `is_safe_path()` (lignes 192-207), `resolve_media()` dans `prproj2ffmpeg.py` :


1. Ne filtre pas les segments `'..'` dans `parts` : un chemin XML contenant `..\..\etc\passwd` ou remontant hors de `base` est joint tel quel par `os.path.join`.
2. Ne vérifie pas les liens symboliques (`os.path.realpath`), permettant à un lien symbolique posé dans un dossier projet de pointer vers n'importe quel fichier du système hôte pour l'inclure dans le rendu vidéo ou le sonder via `ffprobe`.


* **Recommandation de remédiation** :
Filtrer `'.'` et `'..'` dans `parts`, résoudre `real_base = os.path.realpath(str(base))` et `real_cand = os.path.realpath(cand)`, refuser `os.path.islink(cand)` et vérifier `os.path.commonpath([real_base, real_cand]) == real_base`.

#### [S-02] Dictionnaire `_login_failures` non borné en mémoire et comparaison `==` des identifiants (Moyen)

* **Composant & Lignes** : `app.py` lignes 185-221 et ligne 274.


* **Description technique** :
1. `_login_failures` (ligne 190) stocke une entrée par `client_ip` sans taille maximale ni purge globale des IP expirées.


2. À la ligne 274 (`if username == API_USER and password == current_api_password:`), la comparaison utilise `==` au lieu de `hmac.compare_digest` (pourtant déjà utilisé à la ligne 233 pour le jeton CSRF).




* **Recommandation de remédiation** :
Purger les entrées expirées de `_login_failures` sous `_login_failures_lock` dès que `len(_login_failures) >= 1024` (avec éviction FIFO au-delà de 4096 entrées) et comparer `username` et `password` avec `hmac.compare_digest`.

#### [S-03] Décompression `.prproj` sans limite de taille en mémoire dans `prproj2ffmpeg.py` (Moyen)

* **Composant & Lignes** : `tools/prproj-unpacker/prproj2ffmpeg.py` lignes 1136-1141.


* **Description technique** :
`xml_bytes = gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw` (ligne 1140) décompresse la totalité du flux Gzip sans plafond. Un fichier `.prproj` corrompu ou malveillant formant une bombe Gzip saturera la RAM.


* **Recommandation de remédiation** :
Lire via `gzip.GzipFile(fileobj=io.BytesIO(raw)).read(MAX_XML_BYTES + 1)` avec un plafond de 256 Mio (`256 * 1024 * 1024`).

#### [S-04] Déconnexion `/logout` en `GET` et filtrage proxy sur `/api/health` (Faible)

* **Composant & Lignes** : `app.py` lignes 293-297 et 306-312.


* **Description technique** :
* `/logout` accepte `GET` sans jeton CSRF : une simple balise `<img src="[http://127.0.0.1:5000/logout](http://127.0.0.1:5000/logout)">` sur une page tierce déconnecte l'utilisateur. (Pour préserver le contrat `GET /logout` testé par `TestFlaskSmoke` dans `test_app.py` lignes 938, 976, 1003 et 1016, conserver `GET` mais accepter aussi `POST` et vérifier `Sec-Fetch-Site` s'il est présent : refuser si `request.headers.get('Sec-Fetch-Site') == 'cross-site'`).


* Sur `/api/health` (ligne 306), si un reverse proxy local relaie une requête externe, `request.remote_addr` vaut `127.0.0.1`. Vérifier en outre qu'aucun en-tête `X-Forwarded-For` / `X-Real-IP` n'est présent.





---

### Domaine D — Robustesse, Intégrité du Stockage et Reprise Post-Crash

#### [R-01] Mauvaise résolution de destination (`APP_ROOT`) pour les projets adoptés manuellement (Moyen)

* **Composants & Lignes** : `encoder_worker.py` lignes 194-203, `config.py` lignes 176-195, `monitor_exports_and_move.py` ligne 403.


* **Description technique** :
Lorsqu'un projet `.prproj` est déposé à la main dans `WATCH_IN/`, `_ensure_submission()` (`encoder_worker.py` ligne 202) enregistre `OriginalFilePath = WATCH_IN/<stem>_<job_id>.prproj`.
Une fois encodé, `monitor_exports_and_move.py` (ligne 403) appelle `resolve_project_root(Path(matched_row['OriginalFilePath']))`.
Dans `config.py` (lignes 176-195), comme `WATCH_IN/...` n'est ni sous `BASE_SCAN_FOLDER` (`/mnt/cache`) ni dans un sous-dossier `projets`, `resolve_project_root` tombe dans le Fallback 2 :


```python
return file_path.parent.parent

```


Le grand-parent d'un fichier situé dans `WATCH_IN/` est **`APP_ROOT` (le dossier du code source de l'application)** ! Le fichier MP4 est donc déplacé à la racine du dépôt Python au lieu d'aller en quarantaine (ou en fallback `/mnt/cache`).
* **Recommandation de remédiation** :
Dans `monitor_exports_and_move.py` (ligne 402), si `original_file_full_path.resolve().is_relative_to(WATCH_FOLDER_INPUT.resolve())`, traiter ce job adopté manuellement (sans dossier projet d'origine connu sous `/mnt/cache`) via la branche de repli/quarantaine (étape 4, ligne 458) au lieu d'écrire dans `APP_ROOT`.



#### [R-02] Blocage synchrone dans `sweep_existing_exports()` et fuite de `ThreadPoolExecutor` au redémarrage Watchdog (Moyen)

* **Composant & Lignes** : `monitor_exports_and_move.py` lignes 536-550 et 604-617.


* **Description technique** :
1. `sweep_existing_exports()` (ligne 549) appelle `event_handler.process_file(str(existing))` **de manière synchrone** dans le thread principal du moniteur, au lieu de le soumettre au `ThreadPoolExecutor` (`event_handler.executor.submit(event_handler.process_file, str(existing))`). Si un fichier nécessite la stabilisation (10 s) ou les 12 réessais de déplacement (`12 × 10 s = 120 s`), le thread principal du moniteur est figé pendant plusieurs minutes.


2. Aux lignes 613-615, si `observer.is_alive()` devient faux, la boucle recrée `event_handler = ExportHandler()` sans appeler `event_handler.executor.shutdown(wait=False)` sur l'ancien handler, fuyant l'ancien `ThreadPoolExecutor(max_workers=8)` et perdant l'ensemble `processing_paths` en cours.




* **Recommandation de remédiation** :
* Dans `sweep_existing_exports()`, déléguer chaque fichier à `event_handler.executor.submit(event_handler.process_file, str(existing))`.


* Lors du redémarrage de `observer` (ligne 613), réutiliser l'instance `event_handler` existante (`observer.schedule(event_handler, ...)`).





#### [R-03] Oubli de `strip_job_id()` avant `get_base_export_stem()` dans le moniteur (Moyen)

* **Composant & Lignes** : `monitor_exports_and_move.py` lignes 333-334 et 360-398.


* **Description technique** :
À la ligne 333 de `monitor_exports_and_move.py` :


```python
stem_of_exported_file_cleaned = get_base_export_stem(exported_file_path.name)
stem_of_exported_file_cleaned = sanitize_filename_stem(stem_of_exported_file_cleaned)

```


`get_base_export_stem()` (`filename_utils.py` ligne 66) ne retire qu'un suffixe numérique `_\d+$` (ex. `_2`), mais **ne retire pas un `job_id**` (`_20261013_120000_abcdef`).
Si la recherche par `job_id` en base ne renvoie rien (ex. base réinitialisée) et que le moniteur passe à l'étape 2 (rapprochement heuristique, ligne 360), il compare `stem_of_exported_file_cleaned` (qui contient encore `_20261013_120000_abcdef`) aux candidats `candidate_1..4` (dont le `job_id` a été retiré à la ligne 369). La comparaison échoue donc systématiquement !


* **Recommandation de remédiation** :
Importer `strip_job_id` depuis `filename_utils` dans `monitor_exports_and_move.py` et remplacer la ligne 333 par :
```python
stem_of_exported_file_cleaned = get_base_export_stem(strip_job_id(exported_file_path.name))

```



#### [R-04] Boucle infinie de logs si `_archive_source()` échoue sur un statut terminal (Moyen)

* **Composant & Lignes** : `encoder_worker.py` lignes 168-176 et 327-345.


* **Description technique** :
Dans `_pick_next_job()` (lignes 332-344), lorsqu'un fichier `.prproj` dans `WATCH_IN/` a un statut terminal (`EXPORT_FAILED`, `EXPORT_ABORTED`, `EXPORT_SUPERSEDED`), le worker appelle `_archive_source(job_path)`. Si le déplacement vers `WATCH_IN/Source/<horodatage>/` échoue (`OSError` / permission), `_archive_source` capture l'exception et laisse le fichier dans `WATCH_IN/`. Deux secondes plus tard, `_pick_next_job()` retrouve le même fichier et génère les mêmes lignes de log en boucle infinie.


* **Recommandation de remédiation** :
Si `_archive_source(prproj_path)` échoue à déplacer le fichier vers `WATCH_IN/Source/<horodatage>/`, tenter en repli de le renommer sur place avec un suffixe `.archived_err` (ignoré par `_is_input_file()`) ou mémoriser son chemin dans un `set` `_archive_failed_paths` pour ne plus le retraiter à chaque cycle de 2 secondes.

---

## 4. Correctifs Concrets par Fichier (Prêts à Appliquer)

### 4.1. `db_helper.py` (Constat C-04 : Mode WAL + `busy_timeout`)

```python
def get_db_connection(db_path: Path):
    """Retourne une connexion à la base SQLite (mode WAL + timeout large pour absorber les accès concurrents)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    return conn

```

### 4.2. `prproj_engine.py` (Constat P-01 : Extinction garantie du groupe de processus `ffmpeg`)

Dans `_terminate_process_group(process, grace_seconds: float)` (lignes 139-159) :

```python
def _terminate_process_group(process, grace_seconds: float) -> None:
    """Arrête un process lancé avec start_new_session=True (SIGTERM puis SIGKILL sur tout le groupe)."""
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError):
        return

    def _signal_group(sig) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    deadline = time.monotonic() + max(0.1, float(grace_seconds))
    _signal_group(signal.SIGTERM)
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        pass

    # Même si le parent Python (prproj2ffmpeg.py) est mort dès SIGTERM, un sous-processus
    # enfant (ffmpeg) du même groupe peut encore être en vie : on sonde le groupe pgid.
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            break
        time.sleep(0.05)

    _signal_group(signal.SIGKILL)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass

```

### 4.3. `submit_to_watchfolder.py` et `encoder_worker.py` (Constats C-01, C-03, C-05, R-04)

1. **Dans `submit_to_watchfolder.py**` :
* Ligne 138 : verrouiller l'écriture CSV avec `fcntl.flock(f.fileno(), fcntl.LOCK_EX)`.


* Ligne 322 : remplacer `return list(set(prproj_to_submit_list))` par `return sorted(set(prproj_to_submit_list))`.


* Lignes 476-484 : enregistrer la soumission ou protéger la fenêtre TOCTOU avec `_ensure_submission`.




2. **Dans `encoder_worker.py**` :
* Dans `_ensure_submission()` (lignes 180-191) :


```python
job_id = extract_job_id(prproj_path.name)
if job_id:
    row = get_submission_by_job_id(JOBS_DB_FILE, job_id)
    if row:
        return job_id, row, prproj_path
    # Si le fichier vient d'être renommé par submit_to_watchfolder.py (< 5 s),
    # son insertion SQLite est en cours : on attend le prochain cycle de scrutation.
    try:
        if (time.time() - prproj_path.stat().st_mtime) < 5.0:
            return None, None, prproj_path
    except OSError:
        return None, None, prproj_path
    expected_stem = build_expected_export_stem(prproj_path)
    log_submission_action(prproj_path, prproj_path.name, expected_stem, job_id)
    return job_id, get_submission_by_job_id(JOBS_DB_FILE, job_id), prproj_path

```


* Dans `_process_job()` (lignes 296-314) : mettre à jour `STATUS_DONE` **avant** `os.replace(staging_path, published_path)` :


```python
if result.output_ready:
    update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_DONE)
    try:
        os.replace(staging_path, published_path)
    except Exception as e:
        log_encoder_message(f"  ERREUR: impossible de publier '{staging_path.name}' vers '{published_path}': {e}")
        _discard_staging()
        update_submission_status_by_job_id(JOBS_DB_FILE, job_id, STATUS_FAILED)
        _set_state(last_error=f"publication impossible: {e}")
        _bump('failed')
        _archive_source(prproj_path)
        return STATUS_FAILED

    log_encoder_message(f"Job {job_id} TERMINÉ en {result.duration_seconds:.1f} s — "
                        f"{result.output_size / 1_000_000:.1f} Mo → dépôt dans '{WATCH_FOLDER_OUTPUT.name}' pour déplacement final.")
    log_encoder_message(f"  Journal complet de l'encodage : '{export_log_path}'")
    _bump('processed')
    _archive_source(prproj_path)
    return STATUS_DONE

```





### 4.4. `monitor_exports_and_move.py` (Constats C-02, R-01, R-02, R-03)

* Importer `strip_job_id` depuis `filename_utils` (ligne 47) et `WATCH_FOLDER_INPUT` depuis `config` (ligne 20).


* Dans `move_to_final_verified()` (ligne 137), utiliser un nom `.part` unique par thread/processus et forcer `os.fsync` :


```python
temp_path = destination_path.parent / f".{destination_path.name}.{os.getpid()}_{threading.get_ident()}.part"
try:
    shutil.copy2(source_path, temp_path)
    if temp_path.stat().st_size != source_path.stat().st_size:
        raise OSError(
            f"Taille incohérente après copie (source: {source_path.stat().st_size} octets, "
            f"copie: {temp_path.stat().st_size} octets)"
        )
    with open(temp_path, "rb") as fh:
        os.fsync(fh.fileno())
    os.replace(temp_path, destination_path)

```


* Dans `ExportHandler.__init__()` (ligne 193), ajouter `self.destination_lock = threading.Lock()` et `self.reserved_destinations = set()` pour protéger le calcul de `get_versioned_path(final_destination_path)` contre les collisions entre les 8 threads du pool.


* À la ligne 333, retirer le `job_id` avant de calculer le stem de base :


```python
stem_of_exported_file_cleaned = get_base_export_stem(strip_job_id(exported_file_path.name))

```


* À la ligne 402, si `original_file_full_path` provient de `WATCH_FOLDER_INPUT` (projet déposé manuellement dans `WATCH_IN` sans dossier d'origine connu sous `/mnt/cache`), ne pas utiliser `resolve_project_root` (qui pointerait vers `APP_ROOT`), mais laisser l'étape 4 (quarantaine ou fallback `/mnt/cache`) gérer le fichier tout en marquant le statut SQLite.


* À la ligne 613, conserver l'instance `event_handler` existante lors de la recréation de `Observer()`.



### 4.5. `app.py` (Constats P-02, S-02, S-04)

* Plafonner `_login_failures` à 4096 entrées avec purge des entrées expirées sous `_login_failures_lock` (lignes 193-216).


* Utiliser `hmac.compare_digest` pour vérifier `username` et `password` (ligne 274).


* Fermer `process.stdout.close()` dans `monitor_process_output` (ligne 177), relâcher `process_lock` pendant `wait()` dans `stop_script()` (ligne 401), et terminer `current_process` dans `stop_background_threads()` (ligne 515).



### 4.6. `tools/prproj-unpacker/prproj2ffmpeg.py` (Constats S-01, S-03, P-03)

* Dans `resolve_media()` (lignes 456-475) : filtrer `'.'` et `'..'`, rejeter les liens symboliques et vérifier `os.path.commonpath([real_base, real_cand]) == real_base`.


* Dans `main()` (lignes 1136-1141) : plafonner la décompression Gzip en mémoire à 256 Mio via `gzip.GzipFile`.


* (Conformément à `AGENTS.md`, documenter cette mise à jour dans `tools/prproj-unpacker/PROVENANCE.md` et `UPSTREAM-SYNC.md` avec la nouvelle empreinte SHA-256).



---

## 5. Nouveaux Tests Unitaires de Non-Régression (`test_app.py` / `test_watchdog.py`)

Pour couvrir l'ensemble des correctifs ci-dessus dans le bac à sable `test_sandbox` sans toucher aux données de production :

1. `test_ensure_submission_ignore_fichier_en_cours_de_soumission` : vérifie qu'un `.prproj` portant un `job_id` récent (< 5 s) non encore présent dans `jobs.db` n'est pas écrasé avec `OriginalFilePath = WATCH_IN/...` (**C-01**).
2. `test_terminate_process_group_tue_les_enfants_survivants` : lance un processus parent Python qui spawn un sous-processus enfant ignorant `SIGTERM` puis meurt immédiatement ; vérifie que `_terminate_process_group()` détecte l'enfant survivant via `os.killpg(pgid, 0)` et le tue par `SIGKILL` (**P-01**).
3. `test_concurrent_versioned_moves_sans_collision` : lance 4 threads simultanés déplaçant 4 fichiers vers la même cible avec `ALLOW_OVERWRITE=False` ; vérifie que `dest.mp4`, `dest_v1.mp4`, `dest_v2.mp4` et `dest_v3.mp4` sont tous créés sans perte ni collision `.part` (**C-02**).
4. `test_resolve_media_bloque_path_traversal_et_symlinks` : vérifie que `resolve_media(r"F:\..\..\secret.mp4", [base])` et les liens symboliques sortant de `base` retournent `None` (**S-01**).
5. `test_heuristic_matching_avec_job_id_orphelin` : vérifie que `ExportHandler.process_file()` rapproche par heuristique un fichier `Projet_9x16_20261013_120000_abcdef.mp4` dont le `job_id` a changé en base grâce à `strip_job_id()` (**R-03**).
"""Interface web de pilotage (Flask) — version Linux / prproj2ffmpeg.

Le process Flask embarque les deux tâches de fond :
- le moniteur d'exports (`monitor_exports_and_move.py`) : déplace les MP4 terminés vers le dossier projet ;
- l'encodeur (`encoder_worker.py`) : encode les projets Premiere `.prproj` via `prproj2ffmpeg.py`
  (ffmpeg, accélération NVENC avec repli logiciel).

Le script de soumission (`submit_to_watchfolder.py`) est lancé en sous-processus (comme sur Windows),
ce qui permet de l'arrêter et de suivre sa sortie en temps réel via SSE.
"""
import atexit
import hmac
import queue
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

import encoder_worker
import monitor_exports_and_move
from config import (
    API_PASSWORD,
    API_USER,
    BASE_SCAN_FOLDER,
    FLASK_DEBUG,
    FLASK_HOST,
    FLASK_PORT,
    FOLDER_KEYWORD,
    INPUT_EXTENSIONS,
    JOBS_DB_FILE,
    LOGS_DIRECTORY,
    OUTPUT_EXTENSION,
    OUTPUT_FILENAME_FORMAT,
    PRPROJ2FFMPEG_BITRATE,
    PRPROJ2FFMPEG_ENCODER,
    SECRET_KEY,
    SUBMISSION_TEXT_LOG_FILE,
    WATCH_FOLDER_INPUT,
    WATCH_FOLDER_OUTPUT,
)
from prproj_engine import probe_ffmpeg, resolve_script

app = Flask(__name__)

# --- En-têtes de sécurité (CSP) ---
# `unsafe-inline` est requis pour la balise <script> qui injecte window.CSRF_TOKEN.
# style-src https://fonts.googleapis.com et font-src https://fonts.gstatic.com :
#   la feuille de style Google Fonts reste utilisable (self = localhost:5000).
# Pas de 'unsafe-inline' sur script-src : les seuls scripts sont statiques + le jeton inline.
_CSP_HEADER = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:"
)


@app.after_request
def _set_security_headers(response):
    """Applique la Content-Security-Policy et les en-têtes de sécurité de base à toutes les réponses."""
    response.headers.setdefault('Content-Security-Policy', _CSP_HEADER)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    # Une page HTML mise en cache contiendrait un jeton CSRF périmé (session tournée, déconnexion…)
    if response.mimetype == 'text/html':
        response.headers.setdefault('Cache-Control', 'no-store')
    return response


def _is_loopback() -> bool:
    """Vrai si le serveur écoute uniquement sur une interface locale (127.0.0.1, ::1)."""
    return FLASK_HOST in ('127.0.0.1', 'localhost', '::1')


if SECRET_KEY:
    app.secret_key = SECRET_KEY
else:
    app.secret_key = secrets.token_hex(32)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=not _is_loopback(),
    SESSION_COOKIE_MAX_AGE=timedelta(hours=8),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8)
)

# Configuration
SCRIPT_PATH = Path(__file__).parent / "submit_to_watchfolder.py"
# L'UI affiche le journal texte lisible (le CSV reste le registre structuré)
LOG_FILE = SUBMISSION_TEXT_LOG_FILE

# Vérification que le fichier .env est bien configuré (secrets manquants)
_ENV_FILE = Path(__file__).parent / ".env"
if not _ENV_FILE.exists():
    print("AVERTISSEMENT: Fichier .env introuvable à la racine du projet. "
          "Les valeurs par défaut de config.py seront utilisées (défauts non sécurisés).")
elif not SECRET_KEY or not API_PASSWORD:
    print("AVERTISSEMENT: SECRET_KEY ou API_PASSWORD absent(e) du fichier .env. "
          "Un secret/mot de passe temporaire sera généré pour cette session.")

# Gestion du mot de passe de l'API (génération d'un mot de passe fort si non configuré)
current_api_password = API_PASSWORD
if not current_api_password:
    current_api_password = secrets.token_hex(8)  # 16 caractères hexadécimaux


# Pub/Sub Publisher pour les logs SSE
class LogPublisher:
    """Gère un système Pub/Sub thread-safe pour diffuser les logs à de multiples clients SSE avec protection mémoire"""

    def __init__(self):
        self.listeners = []
        self.lock = threading.Lock()

    def listen(self):
        with self.lock:
            # Limiter le nombre de clients simultanés à 50
            if len(self.listeners) >= 50:
                raise RuntimeError("Nombre maximum de connexions SSE actives atteint (50)")
            # Limite de taille de queue de 500 pour protéger la mémoire
            q = queue.Queue(maxsize=500)
            self.listeners.append(q)
        return q

    def announce(self, message):
        with self.lock:
            # Travailler sur une copie pour éviter les effets de bord
            for q in list(self.listeners):
                try:
                    q.put_nowait(message)
                except queue.Full:
                    # En cas de file d'attente pleine (client très lent),
                    # on retire le message le plus ancien (tête de file) pour insérer le nouveau.
                    try:
                        q.get_nowait()
                        q.put_nowait(message)
                    except Exception:
                        pass

    def remove(self, q):
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def clear_all(self):
        with self.lock:
            for q in self.listeners:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break


publisher = LogPublisher()

script_running = False
current_process = None
process_lock = threading.Lock()

monitor_thread = None
encoder_thread = None


def monitor_process_output(process):
    """Lit la sortie du script de soumission en temps réel et attend la fin du processus."""
    global script_running, current_process
    try:
        # Lire la sortie en temps réel (binaire -> décodage UTF-8, les erreurs sont remplacées)
        for raw in iter(process.stdout.readline, b''):
            if raw:
                decoded = raw.decode('utf-8', errors='replace')
                publisher.announce(decoded.strip())

        process.wait()

        if process.returncode == 0:
            publisher.announce(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✓ Script terminé avec succès")
        else:
            publisher.announce(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ✗ Script terminé avec erreur (code: {process.returncode})")

    except Exception as e:
        publisher.announce(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERREUR lors du monitoring: {str(e)}")
    finally:
        with process_lock:
            # Réinitialiser seulement si le processus en cours d'affichage est toujours celui-ci
            if current_process == process:
                script_running = False
                current_process = None


# --- Sécurité : rate-limiting du login (anti-bruteforce) ---
LOGIN_ATTEMPT_WINDOW_SECONDS = 300      # Fenêtre glissante de 5 minutes
LOGIN_MAX_ATTEMPTS = 5                  # Maximum de tentatives échouées dans la fenêtre
LOGIN_FAILED_DELAY_SECONDS = 0.5        # Délai constant imposé sur tout échec (uniformise le temps de réponse)
LOGIN_BLOCK_DURATION_SECONDS = 300      # Durée de blocage après dépassement du maximum

_login_failures = {}                    # ip -> {"count": int, "first_ts": float, "blocked_until": float}
_login_failures_lock = threading.Lock()


def _is_login_blocked(client_ip: str) -> bool:
    """Vrai si l'IP dépasse le nombre de tentatives autorisées dans la fenêtre glissante."""
    with _login_failures_lock:
        now = time.time()
        entry = _login_failures.get(client_ip)
        if not entry:
            return False
        if entry.get("blocked_until") and now < entry["blocked_until"]:
            return True
        if now - entry["first_ts"] > LOGIN_ATTEMPT_WINDOW_SECONDS:
            del _login_failures[client_ip]
            return False
        return entry["count"] >= LOGIN_MAX_ATTEMPTS


def _record_login_failure(client_ip: str):
    with _login_failures_lock:
        now = time.time()
        entry = _login_failures.get(client_ip)
        if not entry or now - entry["first_ts"] > LOGIN_ATTEMPT_WINDOW_SECONDS:
            entry = {"count": 0, "first_ts": now, "blocked_until": None}
            _login_failures[client_ip] = entry
        entry["count"] += 1
        if entry["count"] >= LOGIN_MAX_ATTEMPTS:
            entry["blocked_until"] = now + LOGIN_BLOCK_DURATION_SECONDS


def _clear_login_failures(client_ip: str):
    with _login_failures_lock:
        _login_failures.pop(client_ip, None)


# --- Protection CSRF par jeton de session ---
def _generate_csrf_token() -> str:
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(32)
    return session['_csrf_token']


def _validate_csrf_token(token):
    """Compare le jeton reçu avec celui de la session en temps constant (anti-timing)."""
    expected = session.get('_csrf_token', '')
    return bool(token) and bool(expected) and hmac.compare_digest(str(token), expected)


def csrf_protect(f):
    """Exige un jeton CSRF valide sur les requêtes mutatives (POST, PUT, PATCH, DELETE)."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            if not _validate_csrf_token(request.headers.get('X-CSRF-Token', '')):
                return jsonify({'error': 'Jeton CSRF invalide ou manquant'}), 403
        return f(*args, **kwargs)

    return decorated


# --- Décorateur pour l'Authentification par Session ---
def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            # Si c'est une requête API (commençant par /api/), on renvoie une erreur JSON 401
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized', 'message': 'Veuillez vous connecter'}), 401
            # Sinon, on redirige vers la page de connexion
            return redirect(url_for('login_route'))
        return f(*args, **kwargs)

    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login_route():
    """Page de connexion"""
    if session.get('logged_in'):
        return redirect(url_for('index'))

    if request.method == 'POST':
        client_ip = request.remote_addr or 'inconnu'

        if _is_login_blocked(client_ip):
            return render_template('login.html', error="Trop de tentatives échouées. Réessayez dans quelques minutes.",
                                   csrf_token=_generate_csrf_token()), 429

        username = request.form.get('username')
        password = request.form.get('password')

        # Vérification du jeton CSRF (POST /login est mutatif)
        if not _validate_csrf_token(request.form.get('csrf_token', '')):
            return render_template('login.html', error="Jeton de sécurité invalide. Rechargez la page et réessayez.",
                                   csrf_token=_generate_csrf_token()), 403

        if username == API_USER and password == current_api_password:
            _clear_login_failures(client_ip)
            session.clear()  # Rotation de session : invalide toute session antérieure (anti-session-fixation)
            session['logged_in'] = True
            session.permanent = True
            return redirect(url_for('index'))
        else:
            # Délai constant sur échec : uniformise le temps de réponse (anti-énumération)
            time.sleep(LOGIN_FAILED_DELAY_SECONDS)
            _record_login_failure(client_ip)
            # La page est réaffichée avec un nouveau jeton CSRF : sans cela, le champ caché serait vide
            # et toute nouvelle tentative échouerait en 403 jusqu'à un rechargement manuel.
            return render_template('login.html',
                                   error="Identifiants invalides (voir API_USER et API_PASSWORD dans le fichier .env)",
                                   csrf_token=_generate_csrf_token()), 401

    return render_template('login.html', error=None, csrf_token=_generate_csrf_token())


@app.route('/logout')
def logout_route():
    """Déconnexion"""
    session.clear()
    return redirect(url_for('login_route'))


@app.route('/')
@requires_auth
def index():
    """Page principale"""
    return render_template('index.html', csrf_token=_generate_csrf_token())


@app.route('/api/health')
def health_check():
    """Route de santé non sensible, accessible uniquement depuis localhost (127.0.0.1 ou ::1)"""
    client_ip = request.remote_addr
    if client_ip not in ('127.0.0.1', '::1', 'localhost'):
        return jsonify({'error': 'Access restricted to localhost'}), 403
    return jsonify({'status': 'ok'}), 200


@app.route('/api/status')
@requires_auth
def get_status():
    """Retourne le statut actuel : soumission, moniteur et encodeur."""
    monitor_active = False
    try:
        if monitor_thread is not None:
            monitor_active = monitor_thread.is_alive()
    except NameError:
        monitor_active = False

    encoder_alive = False
    try:
        if encoder_thread is not None:
            encoder_alive = encoder_thread.is_alive()
    except NameError:
        encoder_alive = False

    encoder_state = encoder_worker.snapshot()
    encoder_state['running'] = encoder_alive

    with process_lock:
        running = script_running

    return jsonify({
        'running': running,
        'monitor_running': monitor_active,
        'encoder': encoder_state
    })


@app.route('/api/start', methods=['POST'])
@requires_auth
@csrf_protect
def start_script():
    """Démarre le script de soumission et réactive l'encodeur, de manière atomique sous verrou"""
    global script_running, current_process

    with process_lock:
        if script_running:
            return jsonify({'success': False, 'message': 'Le script est déjà en cours d\'exécution'}), 400

        data = request.json or {}

        # Validation stricte des types de paramètres
        def validate_bool(val, field_name):
            if not isinstance(val, bool):
                raise ValueError(f"Le champ '{field_name}' doit être un booléen.")
            return val

        try:
            force = validate_bool(data.get('force', False), 'force')
            clean_source_archive = validate_bool(data.get('clean_source_archive', False), 'clean_source_archive')
            clean_outputs = validate_bool(data.get('clean_outputs', False), 'clean_outputs')
        except ValueError as ve:
            return jsonify({'success': False, 'message': str(ve)}), 400

        # Construire la commande
        cmd = [sys.executable, str(SCRIPT_PATH)]
        if force:
            cmd.append("--force")
        if clean_source_archive:
            cmd.append("--clean_source_archive")
        if clean_outputs:
            cmd.append("--clean_outputs")

        publisher.announce(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Démarrage du script avec les options: {' '.join(cmd)}")

        try:
            # Lancer le script de manière atomique sous le verrou
            current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,  # lire en binaire
                bufsize=0
            )
            script_running = True
        except Exception as e:
            current_process = None
            script_running = False
            publisher.announce(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERREUR: Impossible de lancer le processus : {str(e)}")
            return jsonify({'success': False, 'message': f'Erreur lors du lancement du processus : {str(e)}'}), 500

        # Lancer le thread de lecture de logs en arrière-plan
        try:
            thread = threading.Thread(target=monitor_process_output, args=(current_process,))
            thread.daemon = True
            thread.start()
        except Exception as e:
            # Si le thread ne peut pas être démarré, on tue le processus et on réinitialise l'état
            publisher.announce(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERREUR: Impossible de démarrer le thread de suivi : {str(e)}")
            current_process.kill()
            current_process.wait()
            current_process = None
            script_running = False
            return jsonify({'success': False, 'message': f'Erreur lors du lancement du thread de suivi : {str(e)}'}), 500

    # Reprise du traitement de la file d'attente de l'encodeur
    encoder_worker.resume()

    return jsonify({'success': True, 'message': 'Script démarré'})


@app.route('/api/stop', methods=['POST'])
@requires_auth
@csrf_protect
def stop_script():
    """Arrête le script de soumission et met l'encodeur en pause (abandon du job en cours)"""
    global current_process, script_running

    aborted_job = False
    with process_lock:
        if script_running:
            if current_process is None:
                # Le thread a été lancé mais le sous-processus n'a pas encore été créé (phase d'initialisation)
                publisher.announce(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Script arrêté pendant la phase d'initialisation.")
            else:
                try:
                    current_process.terminate()
                    try:
                        current_process.wait(timeout=5)
                        publisher.announce(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Script de soumission arrêté proprement par l'utilisateur")
                    except subprocess.TimeoutExpired:
                        publisher.announce(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] AVERTISSEMENT: Le script n'a pas répondu à terminate(). Forçage de l'arrêt (kill)...")
                        current_process.kill()
                        current_process.wait(timeout=2)
                        publisher.announce(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Script arrêté de force (kill)")
                except Exception as e:
                    return jsonify({'success': False, 'message': f'Erreur lors de l\'arrêt: {str(e)}'}), 500
        else:
            publisher.announce(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Aucun script de soumission en cours : mise en pause de l'encodeur uniquement.")

    # Mise en pause de l'encodeur (abandon propre du job en cours d'encodage, s'il y en a un)
    aborted_job = encoder_worker.pause_and_abort()

    if aborted_job:
        return jsonify({'success': True, 'message': 'Soumission arrêtée et encodage du job en cours abandonné'})
    return jsonify({'success': True, 'message': 'Soumission arrêtée, encodeur en pause'})


@app.route('/api/logs/stream')
@requires_auth
def stream_logs():
    """Stream des logs en temps réel via Server-Sent Events avec limitation de débit et protection mémoire"""
    try:
        q = publisher.listen()
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 429

    def generate():
        try:
            while True:
                try:
                    # Attendre un nouveau message dans la queue
                    message = q.get(timeout=1)
                    yield f"data: {message}\n\n"
                except queue.Empty:
                    # Envoyer un heartbeat pour garder la connexion active
                    yield ": heartbeat\n\n"
        finally:
            # S'assurer que la queue est retirée quand le client se déconnecte
            publisher.remove(q)

    return Response(generate(), mimetype='text/event-stream')


@app.route('/api/logs/history')
@requires_auth
def get_log_history():
    """Retourne l'historique des logs depuis le fichier"""
    if not LOG_FILE.exists():
        return jsonify({'logs': []})

    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            lines = [line.strip() for line in f.readlines()]
            # Retourner les 100 dernières lignes
            return jsonify({'logs': lines[-100:], 'encoding_used': 'utf-8'})
    except Exception as e:
        return jsonify({'error': f'Erreur lors de la lecture du fichier: {str(e)}'}), 500


@app.route('/api/logs/clear', methods=['POST'])
@requires_auth
@csrf_protect
def clear_logs():
    """Vide l'affichage des logs (ne supprime pas le fichier)"""
    # Vider toutes les queues actives
    publisher.clear_all()

    return jsonify({'success': True, 'message': 'Logs effacés de l\'affichage'})


def _log_boot_configuration():
    """
    Journalise la configuration effective et l'état de l'environnement d'encodage.
    Passe par le journal de l'encodeur (fichier + SSE), pour que l'information reste consultable
    même si aucun client n'était connecté au démarrage.
    """
    def announce(message: str):
        encoder_worker.log_encoder_message(message)

    announce("--- Configuration ---")
    announce(f"Scan: '{BASE_SCAN_FOLDER}' (mot-clé '{FOLDER_KEYWORD}', extensions {', '.join(INPUT_EXTENSIONS)})")
    announce(f"Watch folders: entrée '{WATCH_FOLDER_INPUT}' · sortie '{WATCH_FOLDER_OUTPUT}'")
    announce(f"Sortie: '{OUTPUT_FILENAME_FORMAT}{OUTPUT_EXTENSION}' · base '{JOBS_DB_FILE}' · logs '{LOGS_DIRECTORY}'")
    announce(f"Encodage: prproj2ffmpeg · H.264 {PRPROJ2FFMPEG_BITRATE} CBR · encodeur '{PRPROJ2FFMPEG_ENCODER}' (ffmpeg/NVENC)")

    script_path = resolve_script()
    if script_path is None:
        announce("ERREUR: script 'prproj2ffmpeg.py' introuvable — aucun encodage ne peut démarrer. "
                 "Vérifiez PRPROJ2FFMPEG_SCRIPT dans le fichier .env.")
        return
    announce(f"Script d'encodage: '{script_path}'")

    probe = probe_ffmpeg()
    if not probe.ready:
        announce("AVERTISSEMENT: ffmpeg/ffprobe introuvable(s) dans le PATH — prproj2ffmpeg ne pourra pas encoder.")
        return
    announce(f"Environnement ffmpeg: {probe.describe()}")
    if not probe.has_nvenc:
        announce("AVERTISSEMENT: encodeur h264_nvenc absent — repli logiciel libx264 (plus lent).")


if __name__ == '__main__':
    print("=" * 60)
    print("Application Flask - Submit to Watchfolder (prproj2ffmpeg)")
    print("=" * 60)
    print(f"Script cible: {SCRIPT_PATH}")
    print(f"Fichier log: {LOG_FILE}")
    print(f"Démarrage du serveur sur http://{FLASK_HOST}:{FLASK_PORT}")
    print("=" * 60)

    is_loopback = FLASK_HOST in ('127.0.0.1', 'localhost', '::1')
    if FLASK_DEBUG and not is_loopback:
        print("\n" + "!" * 80)
        print(" ERREUR CRITIQUE DE SÉCURITÉ :")
        print(f" Le mode DEBUG de Flask est activé sur une interface externe : {FLASK_HOST}")
        print(" Le débogueur Werkzeug permet l'exécution de code à distance (RCE).")
        print(" Démarrage refusé pour des raisons de sécurité.")
        print("!" * 80 + "\n")
        sys.exit(1)

    if not is_loopback:
        print("\n" + "!" * 80)
        print(" AVERTISSEMENT DE SÉCURITÉ :")
        print(f" Le serveur écoute sur une interface externe : {FLASK_HOST}")
        print(" Les identifiants de session transiteraient en clair (HTTP).")
        print(" Il est FORTEMENT RECOMMANDÉ d'utiliser un proxy inverse (Nginx, etc.)")
        print(" avec HTTPS (TLS) pour sécuriser l'accès depuis le réseau externe.")
        print("!" * 80 + "\n")

    if not API_PASSWORD:
        print("\n" + "*" * 80)
        print(" ATTENTION : Aucun mot de passe n'est configuré dans le fichier .env (API_PASSWORD)")
        print(" Un mot de passe temporaire fort a été généré pour cette session :")
        print(f" Nom d'utilisateur : {API_USER}")
        print(f" Mot de passe      : {current_api_password}")
        print(" Pour utiliser un mot de passe persistant, veuillez configurer API_PASSWORD dans le .env")
        print("*" * 80 + "\n")

    # Callbacks de logs vers l'UI (SSE)
    monitor_exports_and_move.log_callback = publisher.announce
    encoder_worker.log_callback = publisher.announce

    # Configuration effective + vérification de l'environnement d'encodage
    _log_boot_configuration()

    # --- Démarrage de l'encodeur (thread daemon) ---
    encoder_stop_event = threading.Event()
    encoder_thread = threading.Thread(
        target=encoder_worker.start_encoder,
        args=(encoder_stop_event,),
        daemon=True,
        name="prproj-encoder",
    )
    encoder_thread.start()

    # --- Démarrage du moniteur d'exports (thread daemon) ---
    monitor_stop_event = threading.Event()
    monitor_thread = threading.Thread(
        target=monitor_exports_and_move.start_monitor,
        args=(monitor_stop_event,),
        daemon=True,
        name="export-monitor",
    )
    monitor_thread.start()

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

    def _install_signal_handlers():
        """SIGTERM (arrêt système / kill) : déclenche le même arrêt propre que Ctrl+C (via atexit)."""
        import signal

        def _handle_termination(signum, frame):
            raise SystemExit(0)

        try:
            signal.signal(signal.SIGTERM, _handle_termination)
        except (ValueError, OSError):
            # Hors thread principal : les gestionnaires de signaux n'y sont pas installables
            pass

    _install_signal_handlers()

    app.run(
        debug=FLASK_DEBUG,
        host=FLASK_HOST,
        port=FLASK_PORT,
        threaded=True,
        use_reloader=False
    )

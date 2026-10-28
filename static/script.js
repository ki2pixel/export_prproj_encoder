// Centralisation des sélecteurs DOM pour améliorer la performance et la maintenance
const DOM = {
    logContent: document.getElementById('logContent'),
    startBtn: document.getElementById('startBtn'),
    stopBtn: document.getElementById('stopBtn'),
    clearLogsBtn: document.getElementById('clearLogsBtn'),
    statusIndicator: document.getElementById('statusIndicator'),
    statusText: document.getElementById('statusText'),
    logCount: document.getElementById('logCount'),
    themeSwitch: document.getElementById('themeSwitch'),
    themeLabel: document.getElementById('themeLabel'),
    scrollToBottomBtn: document.getElementById('scrollToBottomBtn'),
    offlineBadge: document.getElementById('offlineBadge'),
    monitorStatusIndicator: document.getElementById('monitorStatusIndicator'),
    monitorStatusText: document.getElementById('monitorStatusText'),
    encoderStatusIndicator: document.getElementById('encoderStatusIndicator'),
    encoderStatusText: document.getElementById('encoderStatusText'),
    forceOverwrite: document.getElementById('forceOverwrite'),
    cleanArchive: document.getElementById('cleanArchive'),
    cleanOutput: document.getElementById('cleanOutput'),
    speechToggle: document.getElementById('speechToggle')
};

// Variables d'état globales
let logLines = 0;
let eventSource = null;
let statusTimeoutId = null;
let failedRequests = 0;
let isOffline = false;
let isActionPending = false; // Verrou pour éviter le double-clic
let isAuthenticated = true; // Passe à false après un 401 : stoppe le polling et le SSE (F6/F7)

// Jeton CSRF injecté par le serveur via la balise <meta> (F3 : plus de script inline)
const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const csrfToken = csrfMeta ? csrfMeta.content : '';

// Constantes de configuration
const MAX_LOG_LINES = 500; // Limite du DOM pour éviter la saturation
const STATUS_POLL_INTERVAL_MS = 2000;
const SSE_RETRY_DELAY_MS = 5000;
const OFFLINE_THRESHOLD = 3; // Échecs consécutifs avant de passer hors-ligne

// Formate la date/heure locale de façon stable et française (recommandation audit R11)
function nowFr() {
    return new Date().toLocaleString('fr-FR');
}

// Normalise une ligne de log brute (F1 : anti log-injection / spoofing visuel)
// - remplace les retours chariot (\r) qui écrasent l'affichage des terminaux
// - coupe les lignes excessivement longues pour protéger le DOM
function normalizeLogLine(line) {
    let text = String(line).replace(/\r/g, ' ').replace(/\n/g, ' ');
    if (text.length > 2000) {
        text = text.slice(0, 2000) + '…';
    }
    return text;
}

// Stoppe le polling de statut, le flux SSE et le badge offline (F6/F7)
function stopBackgroundNetwork() {
    if (statusTimeoutId) {
        clearTimeout(statusTimeoutId);
        statusTimeoutId = null;
    }
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
    isOffline = false;
    failedRequests = 0;
    if (DOM.offlineBadge) DOM.offlineBadge.style.display = 'none';
}

// Fonction utilitaire pour exécuter des requêtes réseau de manière sécurisée (gestion robuste des erreurs)
async function safeFetchJson(url, options = {}) {
    // Ajout automatique du jeton CSRF aux requêtes mutatives
    const method = (options.method || 'GET').toUpperCase();
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method) && csrfToken) {
        options.headers = Object.assign({}, options.headers, { 'X-CSRF-Token': csrfToken });
    }

    let response;
    try {
        response = await fetch(url, options);
    } catch (networkError) {
        // F5 : message utilisateur plus précis en cas de panne réseau
        throw new Error('Serveur injoignable (réseau)');
    }

    if (response.status === 401) {
        // Session expirée : on stoppe polling + SSE avant la redirection
        // pour éviter les requêtes en boucle sur la page de connexion (F6/F7)
        isAuthenticated = false;
        stopBackgroundNetwork();
        window.location.href = '/login';
        return null;
    }
    if (response.status === 403) {
        throw new Error('Jeton de sécurité invalide (403). Rechargez la page.');
    }
    if (!response.ok) {
        // F5 : le backend renvoie {message} en français sur les erreurs 400/500
        let serverMessage = null;
        try {
            const body = await response.json();
            if (body && body.message) serverMessage = body.message;
        } catch (e) {
            // Corps non-JSON : on garde le message générique
        }
        throw new Error(serverMessage || `Erreur serveur (${response.status})`);
    }
    const contentType = response.headers.get("content-type");
    if (!contentType || !contentType.includes("application/json")) {
        throw new Error("Format de réponse invalide (non-JSON)");
    }
    return await response.json();
}

// Fonction pour ajouter une ligne de log dans la console
function addLogLine(line) {
    if (!DOM.logContent) return;

    // Supprimer l'état vide s'il existe
    const emptyState = DOM.logContent.querySelector('.empty-state');
    if (emptyState) {
        emptyState.remove();
    }

    const logLine = document.createElement('div');
    logLine.className = 'log-line';

    // Normalisation avant affichage (F1)
    const safeLine = normalizeLogLine(line);

    // Colorer selon le type de message
    const upperLine = safeLine.toUpperCase();
    if (upperLine.includes('ERREUR') || upperLine.includes('ERROR')) {
        logLine.classList.add('error');
    } else if (upperLine.includes('SUCCESS') || safeLine.includes('✓') || safeLine.includes('terminé avec succès')) {
        logLine.classList.add('success');
    } else if (upperLine.includes('AVERTISSEMENT') || upperLine.includes('WARNING')) {
        logLine.classList.add('warning');
    } else if (upperLine.includes('INFO') || upperLine.includes('DEBUG')) {
        logLine.classList.add('info');
    }

    logLine.textContent = safeLine;
    DOM.logContent.appendChild(logLine);

    // Limiter à MAX_LOG_LINES lignes pour éviter la saturation du DOM
    while (DOM.logContent.children.length > MAX_LOG_LINES) {
        DOM.logContent.removeChild(DOM.logContent.firstChild);
    }

    // Auto-scroll intelligent
    const isAtBottom = DOM.logContent.scrollHeight - DOM.logContent.clientHeight - DOM.logContent.scrollTop <= 80;
    if (isAtBottom) {
        DOM.logContent.scrollTop = DOM.logContent.scrollHeight;
        if (DOM.scrollToBottomBtn) DOM.scrollToBottomBtn.style.display = 'none';
    } else {
        if (DOM.scrollToBottomBtn) DOM.scrollToBottomBtn.style.display = 'inline-block';
    }

    logLines++;
    updateLogCount();
}

// Met à jour le compteur de lignes affiché
function updateLogCount() {
    if (!DOM.logCount) return;
    DOM.logCount.textContent = `${logLines} ligne${logLines > 1 ? 's' : ''}`;
}

// Vide l'affichage de la console locale
function clearConsole(confirmFirst = true) {
    if (confirmFirst && !confirm('Êtes-vous sûr de vouloir effacer l\'affichage des logs à l\'écran ?')) {
        return;
    }
    if (DOM.logContent) {
        DOM.logContent.innerHTML = `
            <div class="empty-state">
                <svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path>
                </svg>
                <p>Aucun log pour le moment</p>
            </div>
        `;
    }
    logLines = 0;
    updateLogCount();
}

// Effacement complet : local + files SSE côté serveur + rechargement de l'historique (F8)
async function clearLogsFull() {
    clearConsole(false);
    try {
        await safeFetchJson('/api/logs/clear', { method: 'POST' });
    } catch (error) {
        addLogLine(`[${nowFr()}] ❌ Effacement serveur impossible: ${error.message}`);
    }
    // Recharge l'historique pour un état cohérent (les messages en file ont été purgés)
    loadLogHistory();
}

// Met à jour l'UI en fonction de l'état hors-ligne / en-ligne du serveur
function setOfflineStatus(offline) {
    const wasOffline = isOffline;
    isOffline = offline;

    if (DOM.offlineBadge) {
        DOM.offlineBadge.style.display = isOffline ? 'inline-block' : 'none';
    }

    if (isOffline) {
        if (DOM.statusIndicator) DOM.statusIndicator.className = 'status-indicator offline';
        if (DOM.statusText) DOM.statusText.textContent = 'Serveur injoignable';

        if (DOM.monitorStatusIndicator) DOM.monitorStatusIndicator.className = 'status-indicator offline';
        if (DOM.monitorStatusText) {
            DOM.monitorStatusText.textContent = 'Inconnu';
            DOM.monitorStatusText.style.color = '#6b7280';
        }

        if (DOM.encoderStatusIndicator) DOM.encoderStatusIndicator.className = 'status-indicator offline';
        if (DOM.encoderStatusText) {
            DOM.encoderStatusText.textContent = 'Inconnu';
            DOM.encoderStatusText.style.color = '#6b7280';
        }

        if (DOM.startBtn) DOM.startBtn.disabled = true;
        if (DOM.stopBtn) DOM.stopBtn.disabled = true;
    } else if (wasOffline) {
        // Retour en ligne : on avertit et resynchronise les logs récents pour éviter les coupures
        addLogLine(`[${nowFr()}] 🔄 Connexion rétablie. Resynchronisation...`);
        clearConsole(false);
        loadLogHistory();
    }
}

// Fonction pour démarrer le stream de logs SSE
function startLogStream() {
    if (!isAuthenticated) return;
    if (eventSource) {
        eventSource.close();
    }

    eventSource = new EventSource('/api/logs/stream');

    eventSource.onmessage = function(event) {
        if (event.data && event.data.trim()) {
            addLogLine(event.data);
        }
    };

    eventSource.onerror = function(error) {
        console.error('Erreur EventSource:', error);
        // Tente de rétablir manuellement si fermé définitivement
        if (isAuthenticated && eventSource && eventSource.readyState === EventSource.CLOSED) {
            setTimeout(() => {
                // Vérifie que la session est toujours valide avant de relancer (F7)
                if (isAuthenticated) startLogStream();
            }, SSE_RETRY_DELAY_MS);
        }
    };
}

// Gère l'activation et la désactivation des boutons en évitant les surcharges visuelles
function updateActionButtonsState(isRunning) {
    if (isActionPending || isOffline) return; // Ne pas écraser pendant une action réseau active

    if (DOM.startBtn) {
        DOM.startBtn.disabled = isRunning;
        DOM.startBtn.classList.remove('btn-loading');
    }
    if (DOM.stopBtn) {
        DOM.stopBtn.disabled = !isRunning;
        DOM.stopBtn.classList.remove('btn-loading');
    }
}

// Vérifie le statut du script et du moniteur de façon cyclique
async function checkStatus() {
    try {
        const data = await safeFetchJson('/api/status');

        failedRequests = 0;
        setOfflineStatus(false);

        const isRunning = !!data.running;
        updateActionButtonsState(isRunning);

        if (isRunning) {
            if (DOM.statusIndicator) DOM.statusIndicator.className = 'status-indicator running';
            if (DOM.statusText) DOM.statusText.textContent = 'En cours...';
        } else {
            if (DOM.statusIndicator) DOM.statusIndicator.className = 'status-indicator';
            if (DOM.statusText) DOM.statusText.textContent = 'Prêt';
        }

        if (DOM.monitorStatusIndicator && DOM.monitorStatusText) {
            if (data.monitor_running) {
                DOM.monitorStatusIndicator.className = 'status-indicator';
                DOM.monitorStatusText.textContent = 'Actif';
                DOM.monitorStatusText.style.color = '#10b981';
            } else {
                DOM.monitorStatusIndicator.className = 'status-indicator running';
                DOM.monitorStatusText.textContent = 'Arrêté';
                DOM.monitorStatusText.style.color = '#ef4444';
            }
        }

        // Encodeur (prproj2ffmpeg) : état, taille de la file d'attente et job en cours
        const encoder = data.encoder || {};
        if (DOM.encoderStatusIndicator && DOM.encoderStatusText) {
            DOM.encoderStatusText.title = encoder.encoder_engine || '';
            if (!encoder.encoder_available) {
                DOM.encoderStatusIndicator.className = 'status-indicator running';
                DOM.encoderStatusText.textContent = 'script prproj2ffmpeg ou ffmpeg introuvable';
                DOM.encoderStatusText.style.color = '#ef4444';
            } else if (!encoder.running) {
                DOM.encoderStatusIndicator.className = 'status-indicator running';
                DOM.encoderStatusText.textContent = 'Arrêté';
                DOM.encoderStatusText.style.color = '#ef4444';
            } else if (encoder.paused) {
                DOM.encoderStatusIndicator.className = 'status-indicator';
                DOM.encoderStatusText.textContent = `En pause${encoder.queue_pending ? ` (${encoder.queue_pending} en file)` : ''}`;
                DOM.encoderStatusText.style.color = '#f59e0b';
            } else {
                DOM.encoderStatusIndicator.className = 'status-indicator';
                const pending = encoder.queue_pending ? `${encoder.queue_pending} en file` : 'file vide';
                DOM.encoderStatusText.textContent = encoder.current_file
                    ? `Encodage en cours · ${pending}`
                    : (encoder.queue_pending ? `Prêt · ${pending}` : 'Prêt');
                DOM.encoderStatusText.style.color = '#10b981';
            }
        }
    } catch (error) {
        console.error('Erreur lors de la vérification du statut:', error);
        failedRequests++;
        if (failedRequests >= OFFLINE_THRESHOLD) {
            setOfflineStatus(true);
        }
    } finally {
        // F6 : ne relance le polling que si la session est toujours valide
        if (isAuthenticated) {
            statusTimeoutId = setTimeout(checkStatus, STATUS_POLL_INTERVAL_MS);
        }
    }
}

// Action de démarrage du script (anti double-clic)
async function handleStart() {
    if (isActionPending || isOffline || !isAuthenticated) return;

    // Verrouillage instantané de l'UI
    isActionPending = true;
    if (DOM.startBtn) {
        DOM.startBtn.disabled = true;
        DOM.startBtn.classList.add('btn-loading');
    }
    if (DOM.stopBtn) DOM.stopBtn.disabled = true;

    const options = {
        force: DOM.forceOverwrite ? DOM.forceOverwrite.checked : false,
        clean_source_archive: DOM.cleanArchive ? DOM.cleanArchive.checked : false,
        clean_outputs: DOM.cleanOutput ? DOM.cleanOutput.checked : false
    };

    try {
        const data = await safeFetchJson('/api/start', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(options)
        });

        if (data.success) {
            addLogLine(`[${nowFr()}] 🚀 ${data.message}`);
        } else {
            addLogLine(`[${nowFr()}] ❌ ${data.message}`);
        }
    } catch (error) {
        addLogLine(`[${nowFr()}] ❌ Erreur: ${error.message}`);
    } finally {
        isActionPending = false;
        // Force un rafraîchissement immédiat de l'UI
        if (statusTimeoutId) clearTimeout(statusTimeoutId);
        await checkStatus();
    }
}

// Action d'arrêt du script (anti double-clic avec confirmation)
async function handleStop() {
    if (isActionPending || isOffline || !isAuthenticated) return;

    if (!confirm('Êtes-vous sûr de vouloir arrêter la soumission et mettre l\'encodeur en pause (le job en cours sera abandonné) ?')) {
        return;
    }

    // Verrouillage instantané de l'UI
    isActionPending = true;
    if (DOM.stopBtn) {
        DOM.stopBtn.disabled = true;
        DOM.stopBtn.classList.add('btn-loading');
    }
    if (DOM.startBtn) DOM.startBtn.disabled = true;

    try {
        const data = await safeFetchJson('/api/stop', {
            method: 'POST'
        });

        if (data.success) {
            addLogLine(`[${nowFr()}] ⏹️ ${data.message}`);
        } else {
            addLogLine(`[${nowFr()}] ❌ ${data.message}`);
        }
    } catch (error) {
        addLogLine(`[${nowFr()}] ❌ Erreur: ${error.message}`);
    } finally {
        isActionPending = false;
        if (statusTimeoutId) clearTimeout(statusTimeoutId);
        await checkStatus();
    }
}

// Charge l'historique initial des logs
async function loadLogHistory() {
    try {
        const data = await safeFetchJson('/api/logs/history');
        if (data.logs && data.logs.length > 0) {
            if (DOM.logContent) {
                const emptyState = DOM.logContent.querySelector('.empty-state');
                if (emptyState) emptyState.remove();
            }
            data.logs.forEach(line => addLogLine(line));
        }
    } catch (error) {
        // F10 : message visible si l'historique ne peut pas être chargé
        addLogLine(`[${nowFr()}] ❌ Historique des logs indisponible: ${error.message}`);
    }
}

// Thème de l'interface
function applyTheme(mode) {
    const isDark = mode === 'dark';
    document.body.classList.toggle('dark', isDark);
    if (DOM.themeSwitch) DOM.themeSwitch.checked = isDark;
    if (DOM.themeSwitch) DOM.themeSwitch.setAttribute('aria-label', isDark ? 'Activer le mode clair' : 'Activer le mode sombre');
    if (DOM.themeLabel) DOM.themeLabel.textContent = isDark ? 'Mode clair' : 'Mode sombre';
}

function initTheme() {
    const stored = localStorage.getItem('theme');
    let mode = stored;
    if (!mode) {
        const prefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
        mode = prefersDark ? 'dark' : 'light';
    }
    applyTheme(mode);
}

// Liaison des événements UI
function bindEvents() {
    if (DOM.themeSwitch) {
        DOM.themeSwitch.addEventListener('change', () => {
            const mode = DOM.themeSwitch.checked ? 'dark' : 'light';
            localStorage.setItem('theme', mode);
            applyTheme(mode);
        });
    }

    if (DOM.logContent && DOM.scrollToBottomBtn) {
        DOM.logContent.addEventListener('scroll', () => {
            const isAtBottom = DOM.logContent.scrollHeight - DOM.logContent.clientHeight - DOM.logContent.scrollTop <= 50;
            if (isAtBottom) {
                DOM.scrollToBottomBtn.style.display = 'none';
            }
        });

        DOM.scrollToBottomBtn.addEventListener('click', () => {
            DOM.logContent.scrollTo({
                top: DOM.logContent.scrollHeight,
                behavior: 'smooth'
            });
            DOM.scrollToBottomBtn.style.display = 'none';
        });
    }

    if (DOM.startBtn) DOM.startBtn.addEventListener('click', handleStart);
    if (DOM.stopBtn) DOM.stopBtn.addEventListener('click', handleStop);
    // F8 : effacement local + purge des files SSE côté serveur + resynchronisation
    if (DOM.clearLogsBtn) DOM.clearLogsBtn.addEventListener('click', clearLogsFull);

    if (DOM.speechToggle) {
        DOM.speechToggle.addEventListener('change', () => {
            const announce = DOM.speechToggle.checked;
            localStorage.setItem('announceLogs', announce);
            if (DOM.logContent) {
                DOM.logContent.setAttribute('aria-live', announce ? 'polite' : 'off');
            }
        });
    }
}

function initSpeech() {
    if (DOM.speechToggle) {
        const saved = localStorage.getItem('announceLogs') === 'true';
        DOM.speechToggle.checked = saved;
        if (DOM.logContent) {
            DOM.logContent.setAttribute('aria-live', saved ? 'polite' : 'off');
        }
    }
}

// Initialisation globale
function init() {
    initTheme();
    initSpeech();
    loadLogHistory();
    startLogStream();
    checkStatus();
    bindEvents();
}

// Lancement au chargement du DOM
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
} else {
    init();
}

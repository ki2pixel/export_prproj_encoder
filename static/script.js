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
    encoderEngineInfo: document.getElementById('encoderEngineInfo'),
    forceOverwrite: document.getElementById('forceOverwrite'),
    cleanArchive: document.getElementById('cleanArchive'),
    cleanOutput: document.getElementById('cleanOutput'),
    speechToggle: document.getElementById('speechToggle'),
    confirmDialog: document.getElementById('confirmDialog'),
    confirmDialogMessage: document.getElementById('confirmDialogMessage'),
    confirmDialogConfirm: document.getElementById('confirmDialogConfirm')
};

// Variables d'état globales
let logLines = 0;
let eventSource = null;
let statusTimeoutId = null;
let sseRetryTimeoutId = null;
let sseRetryDelayMs = 5000;
let failedRequests = 0;
let isOffline = false;
let isActionPending = false; // Verrou pour éviter le double-clic
let isAuthenticated = true; // Passe à false après un 401 : stoppe le polling et le SSE (F6/F7)
let isCheckingStatus = false;
let statusRefreshQueued = false;

// Jeton CSRF injecté par le serveur via la balise <meta> (F3 : plus de script inline)
const csrfMeta = document.querySelector('meta[name="csrf-token"]');
const csrfToken = csrfMeta ? csrfMeta.content : '';

// Constantes de configuration
const MAX_LOG_LINES = 500; // Limite du DOM pour éviter la saturation
const STATUS_POLL_INTERVAL_MS = 2000;
const REQUEST_TIMEOUT_MS = 8000;
const SSE_RETRY_INITIAL_MS = 5000;
const SSE_RETRY_MAX_MS = 30000;
const OFFLINE_THRESHOLD = 3; // Échecs consécutifs avant de passer hors-ligne
const STATUS_TEXT_CLASSES = [
    'status-text--ok',
    'status-text--warning',
    'status-text--danger',
    'status-text--muted'
];

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

// Protection des accès localStorage (SEC-F01) : navigation restreinte, mode privé ou quota plein.
function safeGetStorage(key) {
    try {
        return window.localStorage.getItem(key);
    } catch (error) {
        return null;
    }
}

function safeSetStorage(key, value) {
    try {
        window.localStorage.setItem(key, String(value));
    } catch (error) {
        // Stockage désactivé ou plein : dégradation gracieuse, l'application reste utilisable.
    }
}

// Applique un état visuel à un texte de statut sans style inline (SEC-F02 / A11Y-F01).
function setStatusText(element, text, tone) {
    if (!element) return;
    element.textContent = text;
    element.classList.remove(...STATUS_TEXT_CLASSES);
    if (tone) {
        element.classList.add(`status-text--${tone}`);
    }
}

// Applique la classe exacte d'une pastille d'état (UX-F01).
function setStatusIndicator(element, state) {
    if (!element) return;
    element.className = state ? `status-indicator ${state}` : 'status-indicator';
}

// Active ou désactive les options de soumission (RES-F06).
function setOptionsDisabled(disabled) {
    [DOM.forceOverwrite, DOM.cleanArchive, DOM.cleanOutput].forEach((checkbox) => {
        if (checkbox) checkbox.disabled = disabled;
    });
}

// Stoppe le polling de statut, le flux SSE, le timer de reconnexion et la modale (F6/F7).
function stopBackgroundNetwork() {
    if (statusTimeoutId) {
        clearTimeout(statusTimeoutId);
        statusTimeoutId = null;
    }
    if (sseRetryTimeoutId) {
        clearTimeout(sseRetryTimeoutId);
        sseRetryTimeoutId = null;
    }
    sseRetryDelayMs = SSE_RETRY_INITIAL_MS;
    if (eventSource) {
        eventSource.close();
        eventSource = null;
    }
    if (DOM.confirmDialog && DOM.confirmDialog.open) {
        DOM.confirmDialog.close('cancel');
    }
    isOffline = false;
    failedRequests = 0;
    statusRefreshQueued = false;
    if (DOM.offlineBadge) DOM.offlineBadge.hidden = true;
    setOptionsDisabled(true);
}

// Fonction utilitaire pour exécuter des requêtes réseau de manière sécurisée (gestion robuste des erreurs)
async function safeFetchJson(url, options = {}) {
    // Ajout automatique du jeton CSRF aux requêtes mutatives
    const method = (options.method || 'GET').toUpperCase();
    const fetchOptions = Object.assign({}, options);
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method) && csrfToken) {
        fetchOptions.headers = Object.assign({}, fetchOptions.headers, { 'X-CSRF-Token': csrfToken });
    }

    // Timeout de requête (RES-F03) : une connexion figée ne doit pas bloquer le polling.
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const timeoutId = controller
        ? setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
        : null;
    const requestOptions = controller
        ? Object.assign({}, fetchOptions, { signal: controller.signal })
        : fetchOptions;

    try {
        let response;
        try {
            response = await fetch(url, requestOptions);
        } catch (networkError) {
            if (networkError && networkError.name === 'AbortError') {
                throw new Error('Délai de réponse dépassé (8 s)');
            }
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
            } catch (error) {
                // Corps non-JSON : on garde le message générique
            }
            throw new Error(serverMessage || `Erreur serveur (${response.status})`);
        }
        const contentType = response.headers.get('content-type');
        if (!contentType || !contentType.includes('application/json')) {
            throw new Error('Format de réponse invalide (non-JSON)');
        }
        try {
            return await response.json();
        } catch (error) {
            if (error && error.name === 'AbortError') {
                throw new Error('Délai de réponse dépassé (8 s)');
            }
            throw new Error('Réponse JSON invalide');
        }
    } finally {
        if (timeoutId) clearTimeout(timeoutId);
    }
}

// Construit une ligne de log normalisée et colorée.
function createLogLine(rawLine) {
    const logLine = document.createElement('div');
    logLine.className = 'log-line';

    const safeLine = normalizeLogLine(rawLine);
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
    return logLine;
}

// Insère plusieurs lignes en un seul reflow (PERF-F01) et respecte le plafond DOM.
function addLogLines(lines) {
    if (!DOM.logContent || !Array.isArray(lines) || lines.length === 0) return;

    // Mesure AVANT toute mutation : sinon la hauteur des nouvelles lignes fausse le calcul.
    const isAtBottom = DOM.logContent.scrollHeight - DOM.logContent.clientHeight - DOM.logContent.scrollTop <= 80;

    const emptyState = DOM.logContent.querySelector('.empty-state');
    if (emptyState) emptyState.remove();

    const fragment = document.createDocumentFragment();
    lines.forEach((rawLine) => fragment.appendChild(createLogLine(rawLine)));
    DOM.logContent.appendChild(fragment);

    // Limiter à MAX_LOG_LINES lignes pour éviter la saturation du DOM
    while (DOM.logContent.children.length > MAX_LOG_LINES) {
        DOM.logContent.removeChild(DOM.logContent.firstElementChild);
    }

    logLines = DOM.logContent.children.length;
    updateLogCount();

    if (isAtBottom) {
        DOM.logContent.scrollTop = DOM.logContent.scrollHeight;
        if (DOM.scrollToBottomBtn) DOM.scrollToBottomBtn.hidden = true;
    } else if (DOM.scrollToBottomBtn) {
        DOM.scrollToBottomBtn.hidden = false;
    }
}

// Fonction historique conservée pour les appels unitaires.
function addLogLine(line) {
    addLogLines([line]);
}

// Met à jour le compteur de lignes affiché (PERF-F02).
function updateLogCount() {
    if (!DOM.logCount) return;
    if (logLines >= MAX_LOG_LINES) {
        DOM.logCount.textContent = `${MAX_LOG_LINES} lignes (max)`;
    } else {
        DOM.logCount.textContent = `${logLines} ligne${logLines > 1 ? 's' : ''}`;
    }
}

// Crée l'état vide sans innerHTML (SEC-F04).
function createEmptyState() {
    const emptyState = document.createElement('div');
    emptyState.className = 'empty-state';

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');

    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('d', 'M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z');
    svg.appendChild(path);

    const text = document.createElement('p');
    text.textContent = 'Aucun log pour le moment';

    emptyState.appendChild(svg);
    emptyState.appendChild(text);
    return emptyState;
}

// Vide l'affichage de la console locale. La confirmation est portée par clearLogsFull().
function clearConsole() {
    if (!DOM.logContent) return;
    DOM.logContent.textContent = '';
    DOM.logContent.appendChild(createEmptyState());
    DOM.logContent.scrollTop = 0;
    logLines = 0;
    updateLogCount();
    if (DOM.scrollToBottomBtn) DOM.scrollToBottomBtn.hidden = true;
}

// Effacement complet : confirmation non bloquante, affichage local + files SSE côté serveur.
async function clearLogsFull() {
    if (!isAuthenticated) return;

    const confirmed = await askConfirmation(
        'Effacer l’affichage et purger les files de logs du serveur ?',
        'Effacer les logs'
    );
    if (!confirmed || !isAuthenticated) return;

    clearConsole();
    try {
        const data = await safeFetchJson('/api/logs/clear', { method: 'POST' });
        if (!data) return;
        if (data.success) {
            addLogLine(`[${nowFr()}] 🗑️ Logs effacés de l'affichage et des files serveur.`);
        } else {
            addLogLine(`[${nowFr()}] ❌ ${data.message || 'Effacement serveur impossible'}`);
        }
    } catch (error) {
        addLogLine(`[${nowFr()}] ❌ Effacement serveur impossible: ${error.message}`);
    }
}

// Met à jour l'UI en fonction de l'état hors-ligne / en-ligne du serveur
function setOfflineStatus(offline) {
    const wasOffline = isOffline;
    isOffline = offline;

    if (DOM.offlineBadge) DOM.offlineBadge.hidden = !isOffline;

    if (isOffline) {
        setStatusIndicator(DOM.statusIndicator, 'offline');
        setStatusText(DOM.statusText, 'Serveur injoignable', 'muted');

        setStatusIndicator(DOM.monitorStatusIndicator, 'offline');
        setStatusText(DOM.monitorStatusText, 'Inconnu', 'muted');

        setStatusIndicator(DOM.encoderStatusIndicator, 'offline');
        setStatusText(DOM.encoderStatusText, 'Inconnu', 'muted');
        if (DOM.encoderStatusText) DOM.encoderStatusText.title = '';
        if (DOM.encoderEngineInfo) DOM.encoderEngineInfo.textContent = '';

        if (DOM.startBtn) {
            DOM.startBtn.disabled = true;
            DOM.startBtn.classList.remove('btn-loading');
        }
        if (DOM.stopBtn) {
            DOM.stopBtn.disabled = true;
            DOM.stopBtn.classList.remove('btn-loading');
        }
        setOptionsDisabled(true);
    } else if (wasOffline) {
        // Retour en ligne : conserver les logs accumulés et relancer le SSE si nécessaire.
        addLogLine(`[${nowFr()}] 🔄 Connexion rétablie.`);
        if (typeof EventSource !== 'undefined' &&
            (!eventSource || eventSource.readyState === EventSource.CLOSED)) {
            startLogStream();
        }
    }
}

// Fonction pour démarrer le stream de logs SSE avec backoff exponentiel (RES-F05).
function startLogStream() {
    if (!isAuthenticated || typeof EventSource === 'undefined') return;

    if (sseRetryTimeoutId) {
        clearTimeout(sseRetryTimeoutId);
        sseRetryTimeoutId = null;
    }
    if (eventSource) {
        eventSource.close();
    }

    try {
        eventSource = new EventSource('/api/logs/stream');
    } catch (error) {
        eventSource = null;
        console.error('Impossible de démarrer le flux SSE:', error);
        return;
    }

    eventSource.onopen = function() {
        sseRetryDelayMs = SSE_RETRY_INITIAL_MS;
    };

    eventSource.onmessage = function(event) {
        sseRetryDelayMs = SSE_RETRY_INITIAL_MS;
        if (event.data && event.data.trim()) {
            addLogLine(event.data);
        }
    };

    eventSource.onerror = function(error) {
        console.error('Erreur EventSource:', error);
        if (!isAuthenticated || !eventSource || eventSource.readyState !== EventSource.CLOSED) {
            return;
        }
        if (sseRetryTimeoutId) {
            clearTimeout(sseRetryTimeoutId);
            sseRetryTimeoutId = null;
        }
        const delay = sseRetryDelayMs;
        sseRetryDelayMs = Math.min(sseRetryDelayMs * 2, SSE_RETRY_MAX_MS);
        sseRetryTimeoutId = setTimeout(() => {
            sseRetryTimeoutId = null;
            // Vérifie que la session est toujours valide avant de relancer (F7)
            if (isAuthenticated) startLogStream();
        }, delay);
    };
}

// Gère l'activation et la désactivation des boutons en évitant les surcharges visuelles
function updateActionButtonsState(isSubmitting, isEncoderBusy = false) {
    setOptionsDisabled(isActionPending || isSubmitting || isOffline);

    if (isActionPending || isOffline) return; // Ne pas écraser pendant une action réseau active

    if (DOM.startBtn) {
        DOM.startBtn.disabled = isSubmitting;
        DOM.startBtn.classList.remove('btn-loading');
    }
    if (DOM.stopBtn) {
        DOM.stopBtn.disabled = !(isSubmitting || isEncoderBusy);
        DOM.stopBtn.classList.remove('btn-loading');
    }
}

// Planifie un unique prochain cycle de polling, en annulant tout timer existant (RES-F03).
function scheduleNextStatusPoll() {
    if (statusTimeoutId) {
        clearTimeout(statusTimeoutId);
        statusTimeoutId = null;
    }
    if (isAuthenticated && !document.hidden) {
        statusTimeoutId = setTimeout(checkStatus, STATUS_POLL_INTERVAL_MS);
    }
}

// Vérifie le statut du script, du moniteur et de l'encodeur de façon cyclique
async function checkStatus() {
    if (!isAuthenticated) return;
    if (isCheckingStatus) {
        statusRefreshQueued = true;
        return;
    }

    isCheckingStatus = true;
    try {
        const data = await safeFetchJson('/api/status');
        if (!data || !isAuthenticated) return;

        failedRequests = 0;
        setOfflineStatus(false);

        const isSubmitting = Boolean(data.running);
        const encoder = data.encoder || {};
        const isEncoderBusy = Boolean(
            encoder.running && !encoder.paused &&
            (encoder.current_job || encoder.current_file || (encoder.queue_pending && encoder.queue_pending > 0))
        );
        updateActionButtonsState(isSubmitting, isEncoderBusy);

        if (isSubmitting) {
            setStatusIndicator(DOM.statusIndicator, 'running');
            setStatusText(DOM.statusText, 'En cours...', 'ok');
        } else {
            setStatusIndicator(DOM.statusIndicator, '');
            setStatusText(DOM.statusText, 'Prêt', 'ok');
        }

        if (data.monitor_running) {
            setStatusIndicator(DOM.monitorStatusIndicator, '');
            setStatusText(DOM.monitorStatusText, 'Actif', 'ok');
        } else {
            setStatusIndicator(DOM.monitorStatusIndicator, 'error');
            setStatusText(DOM.monitorStatusText, 'Arrêté', 'danger');
        }

        // Encodeur (prproj2ffmpeg) : état, moteur, taille de la file d'attente et job en cours
        if (DOM.encoderStatusText) DOM.encoderStatusText.title = encoder.encoder_engine || '';
        if (DOM.encoderEngineInfo) DOM.encoderEngineInfo.textContent = encoder.encoder_engine || '';

        if (!encoder.encoder_available) {
            setStatusIndicator(DOM.encoderStatusIndicator, 'error');
            setStatusText(DOM.encoderStatusText, 'script prproj2ffmpeg ou ffmpeg introuvable', 'danger');
        } else if (!encoder.running) {
            setStatusIndicator(DOM.encoderStatusIndicator, 'error');
            setStatusText(DOM.encoderStatusText, 'Arrêté', 'danger');
        } else if (encoder.paused) {
            setStatusIndicator(DOM.encoderStatusIndicator, 'paused');
            setStatusText(
                DOM.encoderStatusText,
                `En pause${encoder.queue_pending ? ` (${encoder.queue_pending} en file)` : ''}`,
                'warning'
            );
        } else {
            const pending = encoder.queue_pending ? `${encoder.queue_pending} en file` : 'file vide';
            const encoderLabel = encoder.current_file
                ? `Encodage en cours · ${pending}`
                : (encoder.queue_pending ? `Prêt · ${pending}` : 'Prêt');
            setStatusIndicator(DOM.encoderStatusIndicator, encoder.current_file ? 'running' : '');
            setStatusText(DOM.encoderStatusText, encoderLabel, 'ok');
        }
    } catch (error) {
        console.error('Erreur lors de la vérification du statut:', error);
        failedRequests++;
        if (failedRequests >= OFFLINE_THRESHOLD) {
            setOfflineStatus(true);
        }
    } finally {
        isCheckingStatus = false;
        if (statusRefreshQueued && isAuthenticated && !document.hidden) {
            statusRefreshQueued = false;
            checkStatus();
        } else {
            statusRefreshQueued = false;
            scheduleNextStatusPoll();
        }
    }
}

// Dialogue de confirmation non bloquant (UX-F02) : le SSE et les timers continuent de tourner.
function askConfirmation(message, confirmLabel = 'Confirmer') {
    return new Promise((resolve) => {
        if (!DOM.confirmDialog || typeof DOM.confirmDialog.showModal !== 'function') {
            console.warn('Dialogue de confirmation indisponible dans ce navigateur.');
            resolve(false);
            return;
        }
        if (DOM.confirmDialog.open) {
            resolve(false);
            return;
        }

        DOM.confirmDialog.returnValue = '';
        if (DOM.confirmDialogMessage) {
            DOM.confirmDialogMessage.textContent = message;
        }
        if (DOM.confirmDialogConfirm) {
            DOM.confirmDialogConfirm.textContent = confirmLabel;
        }

        const handleClose = () => {
            DOM.confirmDialog.removeEventListener('close', handleClose);
            resolve(DOM.confirmDialog.returnValue === 'confirm');
        };
        DOM.confirmDialog.addEventListener('close', handleClose);

        try {
            DOM.confirmDialog.showModal();
        } catch (error) {
            DOM.confirmDialog.removeEventListener('close', handleClose);
            console.error('Impossible d’ouvrir le dialogue de confirmation:', error);
            resolve(false);
            return;
        }

        const cancelBtn = DOM.confirmDialog.querySelector('button[value="cancel"]');
        if (cancelBtn) cancelBtn.focus();
    });
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
    setOptionsDisabled(true);

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
        if (!data) return;

        if (data.success) {
            addLogLine(`[${nowFr()}] 🚀 ${data.message}`);
            // Les options destructives ne doivent pas rester armées pour une prochaine soumission.
            if (DOM.cleanArchive) DOM.cleanArchive.checked = false;
            if (DOM.cleanOutput) DOM.cleanOutput.checked = false;
        } else {
            addLogLine(`[${nowFr()}] ❌ ${data.message}`);
        }
    } catch (error) {
        addLogLine(`[${nowFr()}] ❌ Erreur: ${error.message}`);
    } finally {
        isActionPending = false;
        // Force un rafraîchissement immédiat de l'UI, sans créer de seconde boucle de polling.
        if (isAuthenticated) {
            if (statusTimeoutId) {
                clearTimeout(statusTimeoutId);
                statusTimeoutId = null;
            }
            await checkStatus();
        } else {
            setOptionsDisabled(true);
        }
    }
}

// Action d'arrêt du script (anti double-clic avec confirmation non bloquante)
async function handleStop() {
    if (isActionPending || isOffline || !isAuthenticated) return;

    const confirmed = await askConfirmation(
        'Voulez-vous arrêter la soumission et abandonner l’encodage en cours ?',
        'Arrêter et abandonner'
    );
    if (!confirmed || isActionPending || isOffline || !isAuthenticated) return;

    // Verrouillage instantané de l'UI
    isActionPending = true;
    if (DOM.stopBtn) {
        DOM.stopBtn.disabled = true;
        DOM.stopBtn.classList.add('btn-loading');
    }
    if (DOM.startBtn) DOM.startBtn.disabled = true;
    setOptionsDisabled(true);

    try {
        const data = await safeFetchJson('/api/stop', {
            method: 'POST'
        });
        if (!data) return;

        if (data.success) {
            addLogLine(`[${nowFr()}] ⏹️ ${data.message}`);
        } else {
            addLogLine(`[${nowFr()}] ❌ ${data.message}`);
        }
    } catch (error) {
        addLogLine(`[${nowFr()}] ❌ Erreur: ${error.message}`);
    } finally {
        isActionPending = false;
        if (isAuthenticated) {
            if (statusTimeoutId) {
                clearTimeout(statusTimeoutId);
                statusTimeoutId = null;
            }
            await checkStatus();
        } else {
            setOptionsDisabled(true);
        }
    }
}

// Charge l'historique initial des logs en une seule insertion (PERF-F01)
async function loadLogHistory() {
    try {
        const data = await safeFetchJson('/api/logs/history');
        if (!data || !Array.isArray(data.logs) || data.logs.length === 0) return;
        addLogLines(data.logs);
    } catch (error) {
        // F10 : message visible si l'historique ne peut pas être chargé
        addLogLine(`[${nowFr()}] ❌ Historique des logs indisponible: ${error.message}`);
    }
}

// Thème de l'interface (A11Y-F02 : libellé constant et état aria-checked explicite)
function applyTheme(mode) {
    const isDark = mode === 'dark';
    document.body.classList.toggle('dark', isDark);
    if (DOM.themeSwitch) {
        DOM.themeSwitch.checked = isDark;
        DOM.themeSwitch.setAttribute('aria-checked', String(isDark));
        DOM.themeSwitch.setAttribute('aria-label', 'Mode sombre');
    }
    if (DOM.themeLabel) DOM.themeLabel.textContent = 'Mode sombre';
}

function initTheme() {
    const stored = safeGetStorage('theme');
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
            safeSetStorage('theme', mode);
            applyTheme(mode);
        });
    }

    if (DOM.logContent && DOM.scrollToBottomBtn) {
        DOM.logContent.addEventListener('scroll', () => {
            const isAtBottom = DOM.logContent.scrollHeight - DOM.logContent.clientHeight - DOM.logContent.scrollTop <= 50;
            DOM.scrollToBottomBtn.hidden = isAtBottom;
        }, { passive: true });

        DOM.scrollToBottomBtn.addEventListener('click', () => {
            DOM.logContent.scrollTo({
                top: DOM.logContent.scrollHeight,
                behavior: 'smooth'
            });
            DOM.scrollToBottomBtn.hidden = true;
        });
    }

    if (DOM.startBtn) DOM.startBtn.addEventListener('click', handleStart);
    if (DOM.stopBtn) DOM.stopBtn.addEventListener('click', handleStop);
    if (DOM.clearLogsBtn) DOM.clearLogsBtn.addEventListener('click', clearLogsFull);

    if (DOM.speechToggle) {
        DOM.speechToggle.addEventListener('change', () => {
            const announce = DOM.speechToggle.checked;
            safeSetStorage('announceLogs', announce);
            if (DOM.logContent) {
                DOM.logContent.setAttribute('aria-live', announce ? 'polite' : 'off');
            }
        });
    }

    // PERF-F02 : suspendre le polling quand l'onglet est masqué, rafraîchir au retour.
    document.addEventListener('visibilitychange', () => {
        if (!isAuthenticated) return;
        if (document.hidden) {
            if (statusTimeoutId) {
                clearTimeout(statusTimeoutId);
                statusTimeoutId = null;
            }
            return;
        }
        if (statusTimeoutId) {
            clearTimeout(statusTimeoutId);
            statusTimeoutId = null;
        }
        checkStatus();
    });
}

function initSpeech() {
    if (DOM.speechToggle) {
        const saved = safeGetStorage('announceLogs') === 'true';
        DOM.speechToggle.checked = saved;
        if (DOM.logContent) {
            DOM.logContent.setAttribute('aria-live', saved ? 'polite' : 'off');
        }
    }
}

// Initialisation globale
async function init() {
    initTheme();
    initSpeech();
    bindEvents();
    checkStatus();
    // L'historique doit être chargé avant l'ouverture du flux SSE pour éviter tout entrelacement.
    await loadLogHistory();
    if (isAuthenticated) {
        startLogStream();
    }
}

// Lancement au chargement du DOM
function boot() {
    init().catch((error) => {
        console.error('Erreur lors de l’initialisation de l’interface:', error);
    });
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
} else {
    boot();
}

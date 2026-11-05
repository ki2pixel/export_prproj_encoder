# Audit de Sécurité, de Robustesse, d'Accessibilité et de Performance Frontend — `export_prproj_encoder`

**Date :** 2026-11-05 · **Périmètre :** Couche de présentation et d'interaction cliente (`templates/index.html`, `templates/login.html`, `static/script.js`, `static/style.css`, `static/icon.svg`) et contrats HTTP/CSP/CSRF/SSE associés dans `app.py` et `test_app.py`.
**Méthode :** Inspection statique exhaustive ligne à ligne, modélisation des automates d'états asynchrones (polling `/api/status`, flux SSE `/api/logs/stream`, mutations `/api/start`, `/api/stop`, `/api/logs/clear`), calcul mathématique des ratios de contraste de luminance relative (WCAG 2.1 AA/AAA & RGAA 4.1), profilage algorithmique du cycle de vie DOM et vérification de non-régression.

---

## 1. Cartographie Architecturale et Flux d'Événements Frontend/Backend

### 1.1. Inventaire des composants inspectés

| Fichier | Lignes | Rôle architectural |
| --- | --- | --- |
| `templates/index.html` | 1–186 | Tableau de bord principal : en-tête (statut hors-ligne, switch thème, déconnexion), panneau de contrôle (3 indicateurs d'état, 3 options de soumission, 3 boutons d'action), console de logs temps réel (`role="log"`), bannière `<noscript>`. |
| `templates/login.html` | 1–60 | Vue d'authentification autonome : formulaire `POST /login` protégé par jeton CSRF caché, bannière d'erreur conditionnelle (`role="alert"`). |
| `static/script.js` | 1–531 | Logique cliente Vanilla ES6+ : client HTTP JSON (`safeFetchJson`), récepteur SSE (`EventSource`), boucle de scrutation `/api/status` ($2\,000\text{ ms}$), gestion du DOM des logs (`MAX_LOG_LINES = 500`), persistance `localStorage` (thème, annonces vocales). |
| `static/style.css` | 1–801 | Système de design CSS3 Glassmorphism : mise en page CSS Grid/Flexbox, thème clair et surcharge `body.dark`, animations d'état, support `@media (prefers-reduced-motion: reduce)`, points de rupture responsives ($1200\text{ px}$ et $768\text{ px}$). |
| `static/icon.svg` | 1–35 | Ressource vectorielle SVG servant à la fois de favicon web et d'icône pour le lanceur de bureau Linux (`.desktop`). |
| `app.py` | 48–485 | Contrats HTTP/navigateur : en-tête `_CSP_HEADER`, `Cache-Control: no-store`, cookies de session `HttpOnly`/`SameSite=Lax`, protection `csrf_protect`, diffuseur SSE `LogPublisher` (max 50 clients, file de 500 messages, battement de cœur $1\text{ s}$). |

### 1.2. Modélisation des cycles asynchrones (Initialisation, SSE, Polling et Reconnexion)

```
[Chargement DOM (index.html)]
   │
   ├─► initTheme() ──────► Lit localStorage('theme') / matchMedia('(prefers-color-scheme: dark)')
   ├─► initSpeech() ─────► Lit localStorage('announceLogs') ──► aria-live="polite" | "off"
   ├─► loadLogHistory() ─► GET /api/logs/history (async, non-await) ──► 100 lignes ──► addLogLine()
   ├─► startLogStream() ─► new EventSource('/api/logs/stream')
   │      ├── onmessage ─► normalizeLogLine() ──► addLogLine() (plafond 500 nœuds DOM + auto-scroll)
   │      └── onerror ───► Si readyState === CLOSED : setTimeout(startLogStream, 5000 ms)
   │
   ├─► checkStatus() ────► GET /api/status (boucle setTimeout récursive de 2000 ms dans finally)
   │      ├── 200 OK ────► failedRequests = 0 ──► setOfflineStatus(false)
   │      │                  ├── Si wasOffline : clearConsole(false) + loadLogHistory()
   │      │                  ├── updateActionButtonsState(!!data.running)
   │      │                  └── Mise à jour DOM : Soumission / Moniteur / Encodeur
   │      ├── 401 Unauth ► isAuthenticated = false ──► stopBackgroundNetwork() ──► location.href = '/login'
   │      └── Erreur ────► failedRequests++ ──► Si >= 3 (6 s) : setOfflineStatus(true)
   │
   └─► Actions Utilisateur (bindEvents)
          ├── handleStart() ───► isActionPending = true ──► POST /api/start ──► clearTimeout + checkStatus()
          ├── handleStop() ────► confirm() ──► isActionPending = true ──► POST /api/stop ──► checkStatus()
          └── clearLogsFull() ─► clearConsole(false) ──► POST /api/logs/clear ──► loadLogHistory()
```

---

## 2. Synthèse Managériale et Matrice des Risques

L'architecture frontend de `export_prproj_encoder` présente des fondations saines : choix judicieux de JavaScript Vanilla sans dépendance tierce complexe, utilisation de `textContent` lors de l'insertion des lignes de journal (`addLogLine`), normalisation des retours chariot (`normalizeLogLine`), arrêt explicite du polling sur expiration de session (`isAuthenticated`), et présence d'une politique `Content-Security-Policy` couplée à `Cache-Control: no-store`.

Toutefois, l'inspection croisée du code source met en évidence **22 constats techniques** (**2 Critiques**, **6 Élevés**, **9 Moyens**, **5 Faibles**) affectant directement l'utilisabilité opérationnelle du bouton « Arrêter » pendant l'encodage, la résilience aux erreurs HTTP 401 et aux restrictions de stockage, la cohérence de la console de logs, les performances de rendu DOM, la conformité RGAA/WCAG 2.1 AA et le fonctionnement hors-ligne (air-gap).

| ID | Axe | Constat technique | Composant & Lignes exactes | Sévérité |
| --- | --- | --- | --- | --- |
| **RES-F01** | Robustesse / Logique | Désactivation prématurée du bouton « Arrêter » (`stopBtn`) dès la fin du script de soumission alors que l'encodeur `prproj2ffmpeg` tourne encore | `static/script.js:267-290` / `app.py:338-343, 423-436` | **Critique** |
| **RES-F02** | Robustesse / Session | Crash `TypeError: Cannot read properties of null` lors d'un HTTP 401 dans `checkStatus`, `handleStart`, `handleStop` et `loadLogHistory` | `static/script.js:89-96, 288, 377, 413, 431` | **Critique** |
| **SEC-F01** | Sécurité / Résilience | Appels `localStorage` non encapsulés (`try/catch`) provoquant un arrêt fatal de `init()` (`SecurityError`) en navigation restreinte | `static/script.js:454, 468, 498, 508, 517-524` | **Élevé** |
| **RES-F03** | Concurrence / Réseau | Conditions de course entre `checkStatus()` en vol et `handleStart()`/`handleStop()`, dédoublement des boucles `setTimeout` et absence de timeout `fetch` | `static/script.js:74-87, 281-348, 385-389, 421-424` | **Élevé** |
| **RES-F04** | Cohérence / SSE | Effacement involontaire des logs temps réel et réinjection du fichier d'historique lors de `clearLogsFull()`, `setOfflineStatus(false)` et `init()` | `static/script.js:193-202, 231-236, 520-521` | **Élevé** |
| **PERF-F01** | Performance DOM | Layout Thrashing (100 reflows synchrones forcés au chargement) et rupture de l'auto-scroll par évaluation de `isAtBottom` **après** `appendChild` | `static/script.js:147-161, 436` | **Élevé** |
| **A11Y-F01** | Accessibilité (WCAG AA) | Défauts majeurs de contraste de couleurs ($2.14:1$ à $3.62:1 < 4.5:1$) sur les statuts (`#f59e0b`, `#10b981`, `#ef4444`, `#6b7280`) et notes de bas de page | `static/script.js:220, 226, 303-333` / `static/style.css:67, 768` | **Élevé** |
| **UX-F01** | Ergonomie / Sémantique | Code couleur contradictoire des pastilles `.status-indicator` (rouge pulsé pour « En cours » ET « Arrêté », vert pulsé pour « En pause ») | `static/style.css:113-147` / `static/script.js:291-335` | **Élevé** |
| **SEC-F02** | Sécurité / CSP | Maintien de `'unsafe-inline'` dans `style-src` induit par les mutations `.style.*` en JS et absence des directives `base-uri`, `form-action` et `object-src` | `app.py:51-62` / `static/script.js:70, 158, 210, 220, 303-333` | **Moyen** |
| **SEC-F03** | Sécurité / CSRF | Déconnexion via lien `<a href="/logout">` en `GET` au lieu d'un formulaire/requête `POST` protégé par CSRF et balise `<meta csrf-token>` inutile sur `/login` | `templates/index.html:37` / `templates/login.html:7` | **Moyen** |
| **RES-F05** | Robustesse / SSE | Timer de reconnexion SSE (`EventSource`) orphelin non annulable dans `stopBackgroundNetwork()` et absence de backoff exponentiel sur HTTP 429/502 | `static/script.js:59-71, 240-264` | **Moyen** |
| **PERF-F02** | Performance / Mémoire | Dérive infinie du compteur `logLines` face au plafond `MAX_LOG_LINES = 500`, écouteur `scroll` non passif et absence de pause en onglet masqué | `static/script.js:150-171, 474-479` | **Moyen** |
| **A11Y-F02** | Accessibilité (RGAA) | Inversion sémantique du libellé et de l'`aria-label` du commutateur de thème (« Mode clair, case cochée » en mode sombre) et absence de `role="switch"` | `templates/index.html:30-36` / `static/script.js:445-451` | **Moyen** |
| **A11Y-F03** | Accessibilité (Clavier) | Console `#logContent` non focalisable au clavier (`tabindex="0"`), diagnostic `encoder_engine` confiné dans `title`, et focus `.login-container input` non conforme | `templates/index.html:159` / `static/script.js:314` / `static/style.css:704-711` | **Moyen** |
| **A11Y-F04** | Accessibilité (Structure) | Usage abusif de `<h2>` pour des paires clé-valeur de statut, absence de `<fieldset>`/`<legend>`, absence de `<main>` sur `/login` et spinner figé sous `prefers-reduced-motion` | `templates/index.html:51-91` / `templates/login.html:14-58` / `static/style.css:541-569

La suite et fin du rapport d'audit frontend reprend exactement à la ligne interrompue du tableau de synthèse (`A11Y-F04`), suivie de l'analyse détaillée des 22 constats avec leurs correctifs, de la matrice mathématique des contrastes WCAG 2.1 / RGAA 4.1, de la feuille de route et du protocole de validation.

## 2. Suite de la Matrice des Risques (Constats 15 à 22)

| ID | Axe | Constat technique | Composant & Lignes | Sévérité |
| --- | --- | --- | --- | --- |
| **A11Y-F04** | Accessibilité (Structure) | Usage abusif de `<h2>` pour des paires clé-valeur de statut, absence de `<fieldset>`/`<legend>`, absence de `<main>` sur `/login` et spinner figé sous `prefers-reduced-motion`

 | `templates/index.html:51-91` / `templates/login.html:14-58` / `static/style.css:541-569, 610-623`

 | **Moyen** |
| **PERF-F03** | Réseau / Air-Gap | Dépendance externe bloquante envers Google Fonts (`fonts.googleapis.com` / `fonts.gstatic.com`) incompatible avec un environnement isolé (air-gap) et fuite d'IP cliente

 | `templates/index.html` / `templates/login.html` / `app.py:51-62`

 | **Moyen** |
| **UX-F02** | Ergonomie / Thread UI | Boîtes modales natives `window.confirm()` bloquantes suspendant le thread principal JS (arrêt du flux SSE et des timers de statut)

 | `static/script.js:173, 395`

 | **Moyen** |
| **SEC-F04** | Hygiène DOM / CSP | Réinitialisation de la console par `.innerHTML` dans `clearConsole()` et commentaire obsolète sur `'unsafe-inline'` et `window.CSRF_TOKEN` dans `app.py`

 | `static/script.js:176-184` / `app.py:51-54`

 | **Faible** |
| **A11Y-F05** | Accessibilité / SVG | Libellé `aria-label="Export FilmCraft"` obsolète dans `static/icon.svg` (vestige de l'ancienne chaîne pré-`prproj2ffmpeg`)

 | `static/icon.svg:1`

 | **Faible** |
| **UX-F03** | Ergonomie / Auth | Absence de support du thème sombre (`body.dark` / `prefers-color-scheme`) et des attributs `autocomplete` sur la vue d'authentification `/login`

 | `templates/login.html:1-60` / `static/style.css:626-739`

 | **Faible** |
| **RES-F06** | Logique / Formulaires | Cases d'options destructives (`#cleanArchive`, `#cleanOutput`) laissées actives pendant l'exécution et non réinitialisées après soumission

 | `static/script.js:267-278, 351-390`

 | **Faible** |
| **CSS-F01** | Maintenabilité CSS | Règles dupliquées (`body.dark .log-content`), surcharges `!important` et transitions génériques `transition: all` coûteuses en compositing

 | `static/style.css:107-111, 155, 181, 425-427, 450-453, 472-474, 521, 577, 710`

 | **Faible** |

## 3. Analyse Détaillée des Constats et Correctifs Ciblés

### Axe 1 — Robustesse Fonctionnelle, Concurrence et Réseau

#### [RES-F01] Désactivation prématurée du bouton « Arrêter » (`stopBtn`) pendant l'encodage (Critique)

* **Localisation** : `static/script.js`, lignes 267-290 (`updateActionButtonsState`, `checkStatus`) et `app.py`, lignes 338-343 (`/api/status`), 423-436 (`/api/stop`).


* **Analyse technique** :
Dans `checkStatus()` (`static/script.js:287-288`), l'état d'activation des boutons « Démarrer » et « Arrêter » dépend exclusivement du booléen `data.running` :


```javascript
const isRunning = !!data.running;
updateActionButtonsState(isRunning);

```


Or, côté backend (`app.py:336-343`), `data.running` reflète uniquement `script_running`, c'est-à-dire le sous-processus éphémère `submit_to_watchfolder.py` qui scanne `/mnt/cache` et copie les fichiers `.prproj` dans `WATCH_IN` en quelques secondes. Dès que cette copie se termine, `data.running` repasse à `false` tandis que le thread `encoder_worker.py` commence l'encodage réel via `prproj2ffmpeg.py` pendant plusieurs minutes (`data.encoder.current_job` non nul ou `data.encoder.queue_pending > 0`).
Conséquence : `updateActionButtonsState(false)` exécute `DOM.stopBtn.disabled = true` dès la fin de la copie initiale. L'opérateur ne peut plus cliquer sur « Arrêter » pour interrompre un encodage en cours ou mettre la file en pause, alors même que la route `POST /api/stop` (`app.py:423-436`) et le message de confirmation (`static/script.js:395`) sont explicitement conçus pour abandonner le job d'encodage en cours (`encoder_worker.pause_and_abort()`).


* **Correctif recommandé** :
Calculer séparément l'état de la soumission (`isSubmitting`) et l'activité de l'encodeur (`isEncoderBusy`) dans `checkStatus()` et `updateActionButtonsState()` :


```javascript
function updateActionButtonsState(isSubmitting, isEncoderBusy = false) {
    if (isActionPending || isOffline) return;

    if (DOM.startBtn) {
        DOM.startBtn.disabled = isSubmitting;
        DOM.startBtn.classList.remove('btn-loading');
    }
    if (DOM.stopBtn) {
        DOM.stopBtn.disabled = !(isSubmitting || isEncoderBusy);
        DOM.stopBtn.classList.remove('btn-loading');
    }
}

```


Et dans `checkStatus()` (`static/script.js:287-290`) :


```javascript
const isSubmitting = Boolean(data.running);
const encoder = data.encoder || {};
const isEncoderBusy = Boolean(
    encoder.running && !encoder.paused && (encoder.current_job || encoder.current_file || encoder.queue_pending > 0)
);
updateActionButtonsState(isSubmitting, isEncoderBusy);

```



#### [RES-F02] Crash `TypeError` sur `data === null` lors d'une expiration de session HTTP 401 (Critique)

* **Localisation** : `static/script.js`, lignes 89-96 (`safeFetchJson`), 288 (`checkStatus`), 377 (`handleStart`), 413 (`handleStop`) et 431 (`loadLogHistory`).


* **Analyse technique** :
Lorsqu'une requête reçoit un statut HTTP `401 Unauthorized`, `safeFetchJson()` déclenche `window.location.href = '/login'` et retourne `null` (ligne 95). En JavaScript, l'assignation à `window.location.href` planifie une navigation asynchrone mais n'interrompt pas l'exécution de la pile d'appels courante :


* Dans `checkStatus()` (ligne 288), `const isRunning = !!data.running;` lève immédiatement `TypeError: Cannot read properties of null (reading 'running')`, ce qui fait entrer la fonction dans le bloc `catch (error)` et incrémente `failedRequests`.


* Dans `handleStart()` (ligne 377) et `handleStop()` (ligne 413), `if (data.success)` lève `TypeError: Cannot read properties of null (reading 'success')`, affiche une fausse erreur dans la console et relance `await checkStatus()` dans le bloc `finally` (lignes 388 et 423) malgré `isAuthenticated = false`.


* Dans `loadLogHistory()` (ligne 431), `if (data.logs && ...)` lève également une `TypeError`.




* **Correctif recommandé** :
Vérifier systématiquement `if (!data) return;` au retour de `safeFetchJson()` dans les quatre fonctions appelantes, et conditionner l'appel `await checkStatus()` des blocs `finally` de `handleStart()` et `handleStop()` à `if (isAuthenticated)` :


```javascript
const data = await safeFetchJson('/api/status');
if (!data) return;

```



#### [SEC-F01] Arrêt fatal de `init()` (`SecurityError`) si `localStorage` est restreint (Élevé)

* **Localisation** : `static/script.js`, lignes 454, 468, 498, 508 et 517-524.


* **Analyse technique** :
Les fonctions `initTheme()`, `initSpeech()` et les écouteurs de changement accèdent directement à `localStorage.getItem()` et `localStorage.setItem()` sans bloc `try / catch`. Dans un navigateur configuré avec blocage strict des cookies/données de site, en mode kiosque durci ou si le quota disque local est plein, l'accès à `localStorage` lève une exception synchrone `DOMException: SecurityError` (ou `QuotaExceededError`).
Comme `initTheme()` est la toute première instruction de `init()` (`static/script.js:518`), cette exception interrompt `init()` avant l'exécution de `loadLogHistory()`, `startLogStream()`, `checkStatus()` et `bindEvents()`, rendant l'application totalement inopérante.


* **Correctif recommandé** :
Encapsuler les accès à `localStorage` dans deux utilitaires résilients :


```javascript
function safeGetStorage(key) {
    try {
        return window.localStorage.getItem(key);
    } catch (e) {
        return null;
    }
}

function safeSetStorage(key, value) {
    try {
        window.localStorage.setItem(key, String(value));
    } catch (e) {
        // Stockage désactivé ou plein : dégradation gracieuse en mémoire
    }
}

```



#### [RES-F03] Conditions de course sur `checkStatus()`, dédoublement des boucles `setTimeout` et absence de timeout HTTP (Élevé)

* **Localisation** : `static/script.js`, lignes 74-87 (`safeFetchJson`), 281-348 (`checkStatus`), 385-389 (`handleStart`), 421-424 (`handleStop`).


* **Analyse technique** :
1. **Absence de timeout sur `fetch()**` : `safeFetchJson()` appelle `fetch(url, options)` sans `AbortController` ni délai d'expiration. Si le serveur ou le réseau local fige un socket TCP, la promesse `checkStatus()` reste bloquée indéfiniment et le badge hors-ligne (`setOfflineStatus(true)`) n'est jamais déclenché.


2. **Dédoublement de la boucle de polling** : Dans le bloc `finally` de `handleStart()` (lignes 387-388) et `handleStop()` (lignes 422-423), le code exécute `if (statusTimeoutId) clearTimeout(statusTimeoutId); await checkStatus();`. Si une exécution périodique de `checkStatus()` était déjà en cours d'attente réseau (`await safeFetchJson('/api/status')`) au moment du clic, son `statusTimeoutId` précédent est déjà expiré, et la requête en vol va terminer son propre bloc `finally` (`statusTimeoutId = setTimeout(checkStatus, 2000)`) en parallèle de celle lancée par `handleStart()`, dédoublant les boucles de scrutation.




* **Correctif recommandé** :
Ajouter un timeout de requête (ex. $8\,000\text{ ms}$) dans `safeFetchJson()` et empêcher les exécutions concurrentes de `checkStatus()` tout en annulant systématiquement `statusTimeoutId` avant de planifier le suivant :


```javascript
let isCheckingStatus = false;

function scheduleNextStatusPoll() {
    if (statusTimeoutId) {
        clearTimeout(statusTimeoutId);
        statusTimeoutId = null;
    }
    if (isAuthenticated && !document.hidden) {
        statusTimeoutId = setTimeout(checkStatus, STATUS_POLL_INTERVAL_MS);
    }
}

```



#### [RES-F04] Incohérence entre la purge des logs (`clearLogsFull`), la reconnexion SSE et l'historique disque (Élevé)

* **Localisation** : `static/script.js`, lignes 193-202 (`clearLogsFull`), 231-236 (`setOfflineStatus`), 520-521 (`init`) et `app.py`, lignes 100, 463-486.


* **Analyse technique** :
Côté serveur, `POST /api/logs/clear` (`app.py:478-486`) vide uniquement les files d'attente SSE en mémoire (`publisher.clear_all()`) et ne modifie jamais le fichier `submission_log.txt` servi par `GET /api/logs/history` (`app.py:463-475`).
Trois anomalies en découlent côté client :


1. Dans `clearLogsFull()` (`static/script.js:193-202`), l'appel à `clearConsole(false)` (sans confirmation utilisateur) suivi de `POST /api/logs/clear` se termine par `loadLogHistory()` (ligne 201). Les 100 dernières lignes de `submission_log.txt` sont donc **immédiatement réaffichées** à l'écran alors que l'utilisateur vient de cliquer sur « Effacer les logs », tandis que les logs temps réel de l'encodeur et du moniteur sont détruits !


2. Dans `setOfflineStatus(false)` (`static/script.js:231-236`), lors du retour en ligne, le script ajoute la ligne `[...] 🔄 Connexion rétablie. Resynchronisation...` (ligne 233) puis appelle **immédiatement** `clearConsole(false)` à la ligne 234, ce qui efface le message qui vient d'être écrit ainsi que tous les logs `(Encoder)` et `(Monitor)` accumulés dans le DOM.


3. Dans `init()` (`static/script.js:520-521`), `loadLogHistory()` est lancé sans `await` simultanément avec `startLogStream()`, provoquant un entrelacement non déterministe entre les premiers messages SSE et l'historique chargé en différé.




* **Correctif recommandé** :
* Dans `clearLogsFull()`, demander confirmation (`if (!confirm(...)) return;`), vider la console locale et purger les files SSE via `POST /api/logs/clear` **sans** rappeler `loadLogHistory()`, puis afficher une ligne d'information locale confirmant l'effacement.


* Dans `setOfflineStatus(false)`, conserver les logs présents dans le DOM et ajouter simplement la ligne `🔄 Connexion rétablie.` (et relancer `startLogStream()` si l'`EventSource` était fermé) au lieu d'appeler `clearConsole(false)`.


* Dans `init()`, attendre `await loadLogHistory()` avant d'ouvrir `startLogStream()`.





#### [RES-F05] Timer de reconnexion SSE (`EventSource`) orphelin non annulable dans `stopBackgroundNetwork()` (Moyen)

* **Localisation** : `static/script.js`, lignes 59-71 (`stopBackgroundNetwork`) et 240-264 (`startLogStream`).


* **Analyse technique** :
Dans `startLogStream()` (`static/script.js:255-263`), lorsque `eventSource.onerror` détecte `EventSource.CLOSED`, il programme un `setTimeout(..., SSE_RETRY_DELAY_MS)` sans stocker l'identifiant du timer dans une variable globale (contrairement à `statusTimeoutId`). Si plusieurs erreurs surviennent ou si `stopBackgroundNetwork()` est appelé, ce timer orphelin n'est pas annulé ; de plus, en cas de saturation des 50 connexions SSE (`HTTP 429` sur `/api/logs/stream`, `app.py:444`), le délai reste fixe à $5\text{ s}$ sans backoff exponentiel.


* **Correctif recommandé** :
Stocker l'identifiant dans `let sseRetryTimeoutId = null;`, l'annuler dans `stopBackgroundNetwork()` et dans `startLogStream()`, et appliquer un backoff exponentiel plafonné ($5\text{ s} \to 10\text{ s} \to 20\text{ s} \to 30\text{ s}$) réinitialisé dès la réception d'un message ou l'ouverture du flux (`onopen`).



#### [RES-F06] Cases d'options destructives actives pendant l'exécution et non réinitialisées après soumission (Faible)

* **Localisation** : `static/script.js`, lignes 267-278 (`updateActionButtonsState`) et 351-390 (`handleStart`).


* **Analyse technique** :
Les cases à cocher `#forceOverwrite`, `#cleanArchive` (`--clean_source_archive`) et `#cleanOutput` (`--clean_outputs`) restent modifiables pendant l'exécution du script de soumission, et surtout `#cleanArchive` et `#cleanOutput` restent cochées après un clic réussi sur « Démarrer le script ». Si l'opérateur relance une soumission plus tard dans la session sans décocher « Nettoyer les sorties », il risque de purger des exports entre-temps publiés dans `WATCH_OUT` (au-delà de `CLEAN_OUTPUT_GUARD_SECONDS`).


* **Correctif recommandé** :
Désactiver les trois cases à cocher tant que `isSubmitting` ou `isActionPending` est vrai, et décocher automatiquement `DOM.cleanArchive` et `DOM.cleanOutput` après un appel `POST /api/start` réussi (`data.success === true`).



---

### Axe 2 — Performance DOM et Gestion Mémoire

#### [PERF-F01] Layout Thrashing (100 reflows synchrones au chargement) et rupture de l'auto-scroll (Élevé)

* **Localisation** : `static/script.js`, lignes 123-163 (`addLogLine`) et 428-441 (`loadLogHistory`).


* **Analyse technique** :
Dans `addLogLine()` (`static/script.js:147-161`) :


```javascript
DOM.logContent.appendChild(logLine);

while (DOM.logContent.children.length > MAX_LOG_LINES) {
    DOM.logContent.removeChild(DOM.logContent.firstChild);
}

const isAtBottom = DOM.logContent.scrollHeight - DOM.logContent.clientHeight - DOM.logContent.scrollTop <= 80;
if (isAtBottom) {
    DOM.logContent.scrollTop = DOM.logContent.scrollHeight;
...

```


Ce code cumule deux défauts majeurs :
1. **Évaluation de `isAtBottom` *après* `appendChild**` : Comme le nouveau nœud `logLine` est déjà inséré dans `DOM.logContent` lorsque `DOM.logContent.scrollHeight` est lu, la hauteur de ce nouveau nœud (qui peut faire plusieurs lignes si le chemin ou la commande est long) est déjà comptabilisée dans `scrollHeight`. Dès qu'un message dépasse $80\text{ px}$ de haut ou qu'une rafale de lignes arrive, `scrollHeight - clientHeight - scrollTop` dépasse $80\text{ px}$ et **désactive l'auto-scroll** alors que l'utilisateur était pourtant tout en bas avant l'insertion.


2. **Layout Thrashing massif** : Dans `loadLogHistory()` (ligne 436), `data.logs.forEach(line => addLogLine(line))` appelle `addLogLine` 100 fois de suite dans la même tâche synchrone. L'alternance `appendChild()` (écriture DOM) $\to$ lecture de `scrollHeight` / `clientHeight` / `scrollTop` $\to$ écriture de `scrollTop` oblige le moteur de rendu (Blink/Gecko) à recalculer le layout complet de la console **100 fois consécutives** au démarrage.




* **Correctif recommandé** :
Mesurer `isAtBottom` **avant** toute mutation du DOM, et introduire une fonction d'insertion par lot `addLogLines(lines)` utilisant un `DocumentFragment` pour n'effectuer qu'une seule lecture géométrique et un seul recalcul de layout pour les 100 lignes d'historique :


```javascript
function addLogLines(lines) {
    if (!DOM.logContent || !Array.isArray(lines) || lines.length === 0) return;

    const isAtBottom = (DOM.logContent.scrollHeight - DOM.logContent.clientHeight - DOM.logContent.scrollTop) <= 80;

    const emptyState = DOM.logContent.querySelector('.empty-state');
    if (emptyState) emptyState.remove();

    const fragment = document.createDocumentFragment();
    for (const rawLine of lines) {
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
        fragment.appendChild(logLine);
    }

    DOM.logContent.appendChild(fragment);

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

```



#### [PERF-F02] Dérive du compteur `logLines`, écouteur `scroll` non passif et polling actif en onglet masqué (Moyen)

* **Localisation** : `static/script.js`, lignes 150-171 (`addLogLine`, `updateLogCount`), 474-479 (`bindEvents`).


* **Analyse technique** :
1. Lorsque `DOM.logContent.children.length` atteint `MAX_LOG_LINES` ($500$), les anciens nœuds sont supprimés (ligne 151), mais `logLines++` continue d'incrémenter sans borne (ou affiche un total incohérent avec les $500$ lignes réellement consultables dans le DOM). Si l'on souhaite afficher à la fois le nombre de lignes affichées et le total reçu, ou borner le compteur à `DOM.logContent.children.length`, il faut l'expliciter (ex. `500 lignes (max)` lorsque plafonné).


2. L'écouteur `DOM.logContent.addEventListener('scroll', ...)` (ligne 474) est enregistré sans `{ passive: true }`, ce qui retarde le défilement fluide du thread compositeur.


3. Le polling `checkStatus()` toutes les $2\text{ s}$ ne ralentit pas lorsque l'onglet est en arrière-plan (`document.hidden`), générant $1\,800$ requêtes/heure par onglet inactif.




* **Correctif recommandé** :
Passer `{ passive: true }` sur l'écouteur `scroll`, synchroniser `logLines = DOM.logContent.children.length` (ou afficher `500 lignes affichées`), et écouter `document.addEventListener('visibilitychange', ...)` pour espacer le polling (ou rafraîchir immédiatement au retour sur l'onglet).



#### [PERF-F03] Dépendance externe Google Fonts bloquante en environnement isolé (Air-Gap) (Moyen)

* **Localisation** : `templates/index.html`, `templates/login.html`, `static/style.css:37`, `app.py:51-62`.


* **Analyse technique** :
La feuille de style charge la police `Outfit` depuis `[https://fonts.googleapis.com](https://fonts.googleapis.com)` et `[https://fonts.gstatic.com](https://fonts.gstatic.com)` (autorisés dans `_CSP_HEADER` de `app.py:56-59`). Sur une station de montage Linux dont l'accès Internet est coupé ou filtré (réseau de post-production interne), la requête DNS/TCP vers `fonts.googleapis.com` bloque le rendu initial de la page ou génère des erreurs réseau à chaque chargement, tout en divulguant l'adresse IP et le `User-Agent` du poste à un tiers externe lorsqu'Internet est actif.


* **Correctif recommandé** :
Héberger localement le fichier `Outfit.woff2` dans `static/fonts/` (ou s'appuyer sur la pile de polices système modernes `system-ui, -apple-system, 'Segoe UI', Roboto, Ubuntu, sans-serif`) et retirer `[https://fonts.googleapis.com](https://fonts.googleapis.com)` et `[https://fonts.gstatic.com](https://fonts.gstatic.com)` de `_CSP_HEADER` dans `app.py`.



---

### Axe 3 — Sécurité Frontend, CSP et CSRF

#### [SEC-F02] Maintien de `'unsafe-inline'` dans `style-src` et directives CSP manquantes (Moyen)

* **Localisation** : `app.py`, lignes 51-62 (`_CSP_HEADER`) et `static/script.js`, lignes 70, 158, 160, 210, 220, 226, 303-333, 477, 485.


* **Analyse technique** :
1. Dans `app.py:55-62`, `_CSP_HEADER` contient `style-src 'self' 'unsafe-inline'` mais omet `base-uri 'self'`, `form-action 'self'` et `object-src 'none'`.


2. Dans `static/script.js`, l'état visuel est piloté par des mutations directes d'attributs de style inline : `.style.display = 'none' | 'inline-block'` (sur `offlineBadge` et `scrollToBottomBtn`) et `.style.color = '#10b981' | '#ef4444' | '#f59e0b' | '#6b7280'` (sur `monitorStatusText` et `encoderStatusText`). Ces styles inline empêchent de supprimer `'unsafe-inline'` de `style-src` et, surtout, **écrasent avec une spécificité maximale** toute règle CSS de thème sombre (`body.dark`), ce qui cause les problèmes de contraste relevés en `A11Y-F01`.




* **Correctif recommandé** :
Remplacer toutes les assignations `.style.color` par des classes CSS sémantiques (`.status-text--ok`, `.status-text--danger`, `.status-text--warning`, `.status-text--muted`) et les `.style.display` par l'attribut standard `element.hidden = true/false` (ou une classe `.is-visible`), puis durcir `_CSP_HEADER` dans `app.py` :


```python
_CSP_HEADER = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "object-src 'none'; "
    "style-src 'self' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:"
)

```


(Note : vérifier que les tests existants dans `test_app.py` n'imposent pas la sous-chaîne littérale `'unsafe-inline'`, sinon conserver `'unsafe-inline'` uniquement si exigé par un test tout en ajoutant `base-uri 'self'; form-action 'self'; object-src 'none'`).



#### [SEC-F03] Déconnexion via lien `<a href="/logout">` en `GET` et balise `<meta csrf-token>` sur `/login` (Moyen)

* **Localisation** : `templates/index.html`, ligne 37 ; `templates/login.html`, ligne 7 ; `app.py`, lignes 298-308.


* **Analyse technique** :
1. Dans `templates/index.html:37`, le bouton « Déconnexion » est un lien `<a href="/logout" class="btn-logout">` déclenchant une requête `GET /logout`. Bien que `app.py:303` bloque `Sec-Fetch-Site: cross-site`, une navigation top-level (`<a href="http://localhost:5000/logout">`) ou le préchargement de liens par le navigateur/extensions envoie `Sec-Fetch-Site: none` ou `same-site` en `GET` et détruit la session. Or `app.py:298-308` supporte déjà `POST /logout` avec vérification du champ `csrf_token` !


2. Dans `templates/login.html:7`, une balise `<meta name="csrf-token" content="{{ csrf_token }}">` est présente en doublon du `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">` alors qu'aucun script JS n'est chargé sur `/login`.




* **Correctif recommandé** :
Remplacer `<a href="/logout" class="btn-logout">` dans `templates/index.html` par un mini-formulaire `<form action="/logout" method="POST" class="logout-form">` contenant `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">` et un `<button type="submit" class="btn-logout">`, tout en conservant le support `GET /logout` dans `app.py` pour ne pas casser les tests smoke `TestFlaskSmoke` de `test_app.py`.



#### [SEC-F04] Usage de `.innerHTML` dans `clearConsole()` et commentaire CSP obsolète dans `app.py` (Faible)

* **Localisation** : `static/script.js`, lignes 176-184 et `app.py`, lignes 51-54.


* **Analyse technique** :
1. `clearConsole()` réinjecte l'état vide via `DOM.logContent.innerHTML = ...` au lieu de manipuler des nœuds DOM ou un élément `<template>`. Bien que la chaîne soit statique, éliminer tout usage de `innerHTML` garantit une conformité stricte aux politiques Trusted Types.


2. Le commentaire dans `app.py:52` indique `# 'unsafe-inline' est requis pour la balise <script> qui injecte window.CSRF_TOKEN`, alors que le jeton CSRF passe désormais par `<meta name="csrf-token">` et que `script-src` vaut `'self'`.





---

### Axe 4 — Accessibilité (RGAA 4.1 / WCAG 2.1 AA) et Ergonomie (UX)

#### [UX-F01] Code couleur contradictoire des pastilles d'état `.status-indicator` (Élevé)

* **Localisation** : `static/style.css`, lignes 113-147 et `static/script.js`, lignes 291-335.


* **Analyse technique** :
La feuille `static/style.css` ne définit que trois états pour `.status-indicator` :
* `.status-indicator` (défaut) : vert `#10b981` avec pulsation infinie (`::after` `status-pulse`).


* `.status-indicator.running` : rouge `#ef4444` avec pulsation infinie.


* `.status-indicator.offline` : gris `#6b7280` sans pulsation.
Cela produit trois contradictions visuelles majeures dans `checkStatus()` (`static/script.js:291-335`) :




1. Lorsque la **Soumission** est « En cours... » (fonctionnement nominal), sa pastille passe en **rouge pulsé** (`.running`), couleur universellement associée à une erreur critique.


2. Lorsque le **Moniteur** ou l'**Encodeur** est « Arrêté » ou que `ffmpeg` est introuvable (erreur critique), il utilise la **même classe `.running**` (rouge pulsé) que la soumission active.


3. Lorsque l'**Encodeur** est « En pause » (`encoder.paused`), sa pastille conserve `.status-indicator` (**vert pulsé**) tandis que son texte est orange (`#f59e0b`), envoyant deux signaux contradictoires.


4. De plus, lorsque les 3 services sont au repos (« Prêt » / « Actif » / « Prêt »), les 3 pastilles vertes pulsent en permanence à l'écran, détournant l'attention de l'opérateur.




* **Correctif recommandé** :
Distinguer clairement les classes d'état sur `.status-indicator` :


* `.status-indicator` (Prêt / Actif au repos) : vert `#10b981` **statique** (sans animation `status-pulse` permanente).


* `.status-indicator.running` (Traitement actif : soumission en cours ou encodage d'un fichier en cours) : bleu/indigo `#3b82f6` ou vert `#10b981` **avec pulsation** (`status-pulse`).


* `.status-indicator.paused` (Encodeur en pause) : ambre `#f59e0b` statique.


* `.status-indicator.error` (Moniteur/Encodeur arrêté ou binaire introuvable) : rouge `#ef4444`.


* `.status-indicator.offline` (Serveur injoignable) : gris `#6b7280`.





#### [A11Y-F01] Défauts majeurs de contraste de luminance (WCAG 2.1 Critère 1.4.3 — Niveau AA) (Élevé)

* **Localisation** : `static/script.js`, lignes 220, 226, 303-333 et `static/style.css`, lignes 67, 502, 768.


* **Analyse technique** :
Les couleurs hexadécimales appliquées en ligne par `static/script.js` (`#f59e0b`, `#10b981`, `#ef4444`, `#6b7280`) ont été choisies pour un fond sombre ou pour des icônes, mais sont affichées comme texte de taille standard ($1.1\text{ rem} = 17{,}6\text{ px}$ gras ou normal, seuil WCAG AA requis : **$4.5:1$**) à la fois sur le panneau clair (`rgba(255, 255, 255, 0.85)` sur dégradé `#667eea`/`#764ba2`, luminance équivalente à `#f3f4fb`) et sur le panneau sombre (`rgba(11, 18, 32, 0.85)` sur `#0f172a`, luminance équivalente à `#0c1322`).
Comme détaillé dans la matrice mathématique en Section 4, les ratios tombent jusqu'à **$2.14:1$** pour `#f59e0b` (« En pause ») et **$2.53:1$** pour `#10b981` (« Actif » / « Prêt ») en thème clair, et **$3.98:1$** pour `#6b7280` (« Inconnu ») en thème sombre.


* **Correctif recommandé** :
Remplacer les couleurs inline par des classes CSS dédiées avec déclinaison `body.dark` respectant un ratio $\ge 4.5:1$ dans les deux thèmes (voir Section 4).



#### [A11Y-F02] Inversion sémantique du commutateur de thème et absence de `role="switch"` (Moyen)

* **Localisation** : `templates/index.html`, lignes 30-36 et `static/script.js`, lignes 445-451 (`applyTheme`).


* **Analyse technique** :
Dans `applyTheme(mode)` (`static/script.js:445-451`) :


```javascript
const isDark = mode === 'dark';
document.body.classList.toggle('dark', isDark);
if (DOM.themeSwitch) DOM.themeSwitch.checked = isDark;
if (DOM.themeSwitch) DOM.themeSwitch.setAttribute('aria-label', isDark ? 'Activer le mode clair' : 'Activer le mode sombre');
if (DOM.themeLabel) DOM.themeLabel.textContent = isDark ? 'Mode clair' : 'Mode sombre';

```


Un interrupteur (`checkbox` / `switch`) dont l'état est binaire (`checked = true` / `false`) ne doit **jamais** inverser son intitulé avec son état :
* Lorsque le mode sombre est actif (`isDark === true`), la case est **cochée** (`checked = true`), mais le texte affiché à côté dit **« Mode clair »** et l'`aria-label` dit **« Activer le mode clair »** !


* Un lecteur d'écran (NVDA / Orca) annonce donc : *« Activer le mode clair, case à cocher cochée »* lorsque l'utilisateur est en mode sombre, et visuellement l'interrupteur est allumé (bleu/indigo à droite) en face du texte « Mode clair » alors que l'interface est sombre.




* **Correctif recommandé** :
Ajouter `role="switch"` sur `<input type="checkbox" id="themeSwitch" role="switch">`, fixer un libellé invariant **« Mode sombre »** (ou mettre à jour le texte avec l'état courant « Thème : Sombre » / « Thème : Clair » et un `aria-label="Mode sombre"` constant dont l'état coché/décoché indique s'il est actif).



#### [A11Y-F03] Console `#logContent` non défilable au clavier, diagnostic `encoder_engine` réservé à la souris et focus `/login` (Moyen)

* **Localisation** : `templates/index.html`, ligne 159 ; `static/script.js`, ligne 314 ; `static/style.css`, lignes 704-717.


* **Analyse technique** :
1. Le conteneur défilant `#logContent` (`overflow-y: auto`) ne possède pas `tabindex="0"` ni `aria-label="Journal d'exécution"` : un utilisateur naviguant au clavier (touche `Tab` puis flèches `Haut`/`Bas` ou `PageUp`/`PageDown`) ne peut pas faire défiler les logs.


2. Dans `static/script.js:314`, l'information technique sur le moteur d'encodage (`DOM.encoderStatusText.title = encoder.encoder_engine || '';`, ex. `prproj2ffmpeg · ffmpeg 6.x · h264_nvenc disponible`) est injectée uniquement dans l'attribut `title`, inaccessible au clavier et sur écran tactile.


3. Dans `static/style.css:710-717`, `.login-container input` définit `outline: none;` et remplace le focus par une ombre très faible `box-shadow: 0 0 0 3px rgba(118, 75, 162, 0.15)` (contraste $1.18:1$, non conforme au critère WCAG 2.1 AA 1.4.11 Non-text Contrast qui exige $3:1$).




* **Correctif recommandé** :
* Ajouter `tabindex="0"` et `aria-label="Console des journaux d'exécution"` sur `#logContent` dans `templates/index.html`.


* Afficher `encoder.encoder_engine` dans un sous-élément visible (ou `small` sous le statut de l'encodeur) plutôt que dans le seul attribut `title`.


* Appliquer un `outline: 3px solid #764ba2; outline-offset: 2px;` sur `.login-container input:focus-visible`.





#### [A11Y-F04] Sémantique des titres `<h2>`, regroupement `<fieldset>`, `<main>` sur `/login` et `.btn-loading` sous `prefers-reduced-motion` (Moyen)

* **Localisation** : `templates/index.html`, lignes 51-91 ; `templates/login.html`, lignes 14-58 ; `static/style.css`, lignes 541-569 et 610-623.


* **Analyse technique** :
1. Dans `templates/index.html:51-91`, les trois lignes d'état (`Statut : Prêt`, `Moniteur : Actif`, `Encodeur : Prêt`) sont balisées comme trois titres de section `<h2>` contenant la valeur dynamique dans un `<span>`. Cela pollue la table des matières des lecteurs d'écran avec des titres qui changent toutes les $2\text{ s}$.


2. Les trois cases à cocher d'options de soumission ne sont pas regroupées dans un `<fieldset>` muni d'un `<legend>`.


3. La page `templates/login.html` n'utilise pas de balise landmark `<main>` autour de `.login-container`.


4. Dans `static/style.css:610-623`, la règle `@media (prefers-reduced-motion: reduce)` force `animation-iteration-count: 1 !important; animation-duration: 1ms !important;` sur `*`. Or, lorsqu'un bouton passe en `.btn-loading` (`static/style.css:541-569`), son texte devient `color: transparent !important` et `.btn-loading * { visibility: hidden; }` pour n'afficher que le pseudo-élément `::after` qui tourne (`animation: spin`). Sous `prefers-reduced-motion: reduce`, le texte du bouton disparaît et il ne reste qu'un cercle statique figé.




* **Correctif recommandé** :
* Ajouter `aria-live="polite"` sur un conteneur de statut ou conserver un unique `<h2>État des services</h2>` suivi d'une liste de définitions (`<dl>`/`<dt>`/`<dd>`) ou de blocs `<div class="status-row">`, tout en conservant les `id` (`statusIndicator`, `statusText`, `monitorStatusIndicator`, `monitorStatusText`, `encoderStatusIndicator`, `encoderStatusText`) intacts pour le JS et les tests.


* Envelopper `.login-container` dans `<main>` (ou `<main class="login-container">`) dans `templates/login.html`.


* Sous `@media (prefers-reduced-motion: reduce)`, conserver le texte visible dans `.btn-loading` (`color: inherit !important; opacity: 0.8;`) et masquer le spinner `::after`.





#### [UX-F02] Boîtes modales natives `window.confirm()` bloquantes (Moyen)

* **Localisation** : `static/script.js`, lignes 173 (`clearConsole`) et 395 (`handleStop`).


* **Analyse technique** :
L'appel à `confirm()` bloque le thread principal d'exécution JavaScript du navigateur : tant que la boîte de dialogue est ouverte à l'écran, les événements `EventSource` (`onmessage`), les timers `setTimeout(checkStatus)` et le rendu DOM sont figés. De plus, dans `clearLogsFull()` (`static/script.js:194`), `clearConsole(false)` est appelé avec `confirmFirst = false`, ce qui fait que le bouton « Effacer les logs » (`#clearLogsBtn`) n'affiche en réalité **aucune confirmation** avant de purger les files SSE du serveur, rendant le paramètre `confirmFirst = true` de `clearConsole()` du code mort !


* **Correctif recommandé** :
Utiliser un mécanisme de confirmation en deux clics non bloquant sur le bouton lui-même (ex. premier clic : le bouton affiche « Confirmer l'arrêt ? » pendant $4\text{ s}$ avec annulation automatique ; second clic : exécution immédiate), ou une boîte `<dialog>` HTML5 native non bloquante pour le thread réseau.



#### [A11Y-F05] Libellé `aria-label="Export FilmCraft"` obsolète dans `static/icon.svg` (Faible)

* **Localisation** : `static/icon.svg`, ligne 1.


* **Analyse technique** :
La balise racine `<svg ... role="img" aria-label="Export FilmCraft">` mentionne encore `FilmCraft`, l'ancien moteur propriétaire remplacé par `prproj2ffmpeg`.


* **Correctif recommandé** :
Remplacer par `aria-label="Export MP4 (prproj2ffmpeg)"` dans `static/icon.svg:1`.



#### [UX-F03] Absence de thème sombre et d'attributs `autocomplete` sur `/login` (Faible)

* **Localisation** : `templates/login.html`, lignes 1-60 et `static/style.css`, lignes 626-739.


* **Analyse technique** :
1. Lorsqu'un utilisateur en mode sombre est redirigé vers `/login` (expiration de session 401 ou déconnexion), la page de connexion ignore `@media (prefers-color-scheme: dark)` et `localStorage.getItem('theme')`, affichant une carte blanche éblouissante.


2. Les champs `username` et `password` de `templates/login.html` n'ont pas les attributs `autocomplete="username"` et `autocomplete="current-password"`, ce qui gêne les gestionnaires de mots de passe et contrevient au critère WCAG 2.1 AA 1.3.5 (Identify Input Purpose).




* **Correctif recommandé** :
Ajouter `autocomplete="username"` et `autocomplete="current-password"` dans `templates/login.html` et ajouter les styles `@media (prefers-color-scheme: dark)` pour `body.login-body` et `.login-container` dans `static/style.css`.



#### [CSS-F01] Duplications CSS, `!important` et `transition: all` (Faible)

* **Localisation** : `static/style.css`, lignes 107-111, 155, 181, 425-427, 450-453, 472-474, 521, 577, 710.


* **Analyse technique** :
1. Le sélecteur `body.dark .log-content` est défini deux fois (lignes 450-453 et 472-474).


2. `.monitor-status-title` utilise `!important` sur `font-size`, `margin-top` et `color` (lignes 107-111 et 425-427) uniquement parce qu'il surcharge `.control-section h2`.


3. Cinq composants utilisent `transition: all ...` au lieu de cibler explicitement `background-color, color, border-color, transform, box-shadow`, provoquant des interpolations inutiles lors des changements de layout.





---

## 4. Audit Mathématique des Contrastes (WCAG 2.1 AA/AAA & RGAA 4.1)

Le ratio de contraste entre deux couleurs de luminances relatives $L_1$ et $L_2$ (avec $L_1 \ge L_2$) est défini par la formule WCAG 2.1 :

$$\text{Ratio} = \frac{L_1 + 0{,}05}{L_2 + 0{,}05}$$

* **Panneau thème clair** : `rgba(255, 255, 255, 0.85)` composé sur le dégradé `#667eea` $\to$ `#764ba2` produit une couleur de fond effective comprise entre `#eef0fc` ($L = 0{,}871$) et `#f0ebf5` ($L = 0{,}846$).


* **Panneau thème sombre (`body.dark`)** : `rgba(11, 18, 32, 0.85)` composé sur `#0f172a` produit une couleur de fond effective `#0c1322` ($L = 0{,}0097$).



| Élément UI | Thème | Couleur actuelle | Fond effectif | Ratio actuel | Seuil AA | Verdict & Couleur corrigée (Ratio après correction) |
| --- | --- | --- | --- | --- | --- | --- |
| Statut « En pause » (`encoderStatusText`)

 | Clair | `#f59e0b`

 | `#eef0fc` | **$1.90:1$** (2.14 sur blanc)

 | $4.5:1$

 | **Échec AA** $\to$ `#b45309` (**$4.61:1$** sur `#eef0fc`, **$5.02:1$** sur `#fff`) |
| Statut « Actif » / « Prêt » (`monitor`/`encoder`)

 | Clair | `#10b981`

 | `#eef0fc` | **$2.25:1$** (2.53 sur blanc)

 | $4.5:1$

 | **Échec AA** $\to$ `#047857` (**$4.87:1$** sur `#eef0fc`, **$5.48:1$** sur `#fff`) |
| Statut « Arrêté » / Erreur (`monitor`/`encoder`)

 | Clair | `#ef4444`

 | `#eef0fc` | **$3.34:1$** (3.76 sur blanc)

 | $4.5:1$

 | **Échec AA** $\to$ `#b91c1c` (**$5.74:1$** sur `#eef0fc`, **$6.47:1$** sur `#fff`) |
| Statut « Inconnu » hors-ligne

 | Sombre | `#6b7280`

 | `#0c1322` | **$3.98:1$**

 | $4.5:1$

 | **Échec AA** $\to$ `#94a3b8` (**$6.75:1$** sur `#0c1322`) |
| Statut « Arrêté » / Erreur

 | Sombre | `#ef4444`

 | `#0c1322` | **$4.78:1$** | $4.5:1$

 | Conforme AA, mais amélioré en `#f87171` (**$6.92:1$**) |
| Titre principal `h1` (`style.css:67`)

 | Clair | `#667eea`

 | `#eef0fc` | **$3.08:1$** | $3.0:1$ (grand) | Limite basse AA $\to$ `#4338ca` (**$6.98:1$**, AAA) |
| Sous-titre `/login` (`.login-container .subtitle`)

 | Clair | `#6b7280`

 | `#eef0fc` | **$4.28:1$** | $4.5:1$

 | **Échec AA** $\to$ `#4b5563` (**$6.70:1$**, AAA) |

**Classes CSS de remplacement à ajouter dans `static/style.css**` :

```css
.status-text--ok {
    color: #047857;
}
.status-text--warning {
    color: #b45309;
}
.status-text--danger {
    color: #b91c1c;
}
.status-text--muted {
    color: #4b5563;
}

body.dark .status-text--ok {
    color: #34d399;
}
body.dark .status-text--warning {
    color: #fbbf24;
}
body.dark .status-text--danger {
    color: #f87171;
}
body.dark .status-text--muted {
    color: #94a3b8;
}

```

---

## 5. Feuille de Route de Remédiation Priorisée

### Priorité 1 — Critique & Opérationnelle Immédiate (`static/script.js` & `static/style.css`)

1. **Corriger l'état du bouton « Arrêter » (`RES-F01`)** : Activer `#stopBtn` dès que `data.running` est vrai **ou** que l'encodeur traite/contient des jobs non mis en pause (`!encoder.paused && (encoder.current_job || encoder.current_file || encoder.queue_pending > 0)`).


2. **Sécuriser le retour `null` sur HTTP 401 (`RES-F02`)** : Ajouter `if (!data) return;` après chaque appel `await safeFetchJson(...)` dans `checkStatus`, `handleStart`, `handleStop` et `loadLogHistory`, et ne pas relancer `checkStatus()` dans `finally` si `!isAuthenticated`.


3. **Protéger `localStorage` (`SEC-F01`)** : Encapsuler tous les appels `localStorage.getItem` / `setItem` dans des fonctions `try / catch` pour éviter tout blocage de `init()`.


4. **Corriger le Layout Thrashing et l'auto-scroll (`PERF-F01`)** : Évaluer `isAtBottom` **avant** `appendChild`, et insérer l'historique via un `DocumentFragment` (`addLogLines`).


5. **Stabiliser la console de logs (`RES-F04`)** : Ne plus rappeler `loadLogHistory()` à la fin de `clearLogsFull()`, et ne plus vider la console (`clearConsole(false)`) lors d'une reconnexion dans `setOfflineStatus(false)`.



### Priorité 2 — Accessibilité RGAA/WCAG AA, Ergonomie et Concurrence (`static/style.css`, `static/script.js`, `templates/*.html`)

6. **Assainir les couleurs et pastilles de statut (`A11Y-F01`, `UX-F01`, `SEC-F02`)** : Remplacer les assignations `.style.color` par les classes `.status-text--ok/warning/danger/muted` conformes WCAG AA ($\ge 4.5:1$), et distinguer les classes `.status-indicator` (`.running`, `.paused`, `.error`, `.offline`).


7. **Fiabiliser le polling et la reconnexion SSE (`RES-F03`, `RES-F05`, `PERF-F02`)** : Ajouter un `AbortController` avec timeout sur `safeFetchJson()`, dédupliquer `checkStatus()`, enregistrer l'ID du timer de reconnexion SSE pour l'annuler dans `stopBackgroundNetwork()`, et rendre l'écouteur `scroll` passif.


8. **Corriger l'accessibilité clavier et lecteur d'écran (`A11Y-F02`, `A11Y-F03`, `A11Y-F04`, `A11Y-F05`)** : Corriger le libellé du switch de thème (`role="switch"`), ajouter `tabindex="0"` sur `#logContent`, corriger `.btn-loading` sous `prefers-reduced-motion: reduce`, ajouter `<main>` et `autocomplete` sur `/login`, et mettre à jour `aria-label` dans `static/icon.svg`.



### Priorité 3 — Durcissement CSP, CSRF et Autonomie Hors-Ligne (`app.py`, `templates/*.html`)

9. **Déconnexion en `POST` protégée par CSRF (`SEC-F03`)** : Transformer le lien `<a href="/logout">` de `templates/index.html` en formulaire `POST /logout` muni du jeton CSRF, et retirer la balise `<meta name="csrf-token">` superflue de `templates/login.html`.


10. **Durcissement CSP et polices locales (`SEC-F02`, `PERF-F03`)** : Ajouter `base-uri 'self'; form-action 'self'; object-src 'none'` à `_CSP_HEADER` dans `app.py` et privilégier une police locale ou système pour un fonctionnement 100 % hors-ligne.



---

## 6. Protocole de Vérification et Tests de Non-Régression

Après application des correctifs, exécuter la séquence de validation complète du dépôt :

```bash
# 1. Vérification syntaxique JavaScript et Python
node --check static/script.js
./.venv/bin/python -m py_compile app.py config.py db_helper.py \
  filename_utils.py prproj_engine.py submit_to_watchfolder.py \
  encoder_worker.py monitor_exports_and_move.py test_app.py test_watchdog.py

# 2. Exécution de la suite de tests unitaires et smoke tests Flask (CSP, Auth, CSRF, /api/status)
./.venv/bin/python -m unittest discover -v

# 3. Contrôle d'intégrité des espaces et fins de ligne Git
git diff --check

```

---

## 7. Suivi de Remédiation (2026-11-05)

Les 22 constats de cet audit ont été appliqués. Les tests `unittest` passent (95 tests), ainsi que
`node --check`, `py_compile` et `git diff --check`.

| ID | Statut | Correctif appliqué |
| --- | --- | --- |
| RES-F01 | ✅ Appliqué | Bouton « Arrêter » activé sur `isSubmitting || isEncoderBusy` (`current_job`, `current_file` ou file non vide). |
| RES-F02 | ✅ Appliqué | Garde `if (!data) return;` sur les quatre appels réseau et `checkStatus()` conditionné à `isAuthenticated` dans les `finally`. |
| SEC-F01 | ✅ Appliqué | `safeGetStorage` / `safeSetStorage` avec dégradation silencieuse (`SecurityError`, quota). |
| RES-F03 | ✅ Appliqué | Timeout `AbortController` de 8 s, polling unique via `scheduleNextStatusPoll()` et verrou `isCheckingStatus`. |
| RES-F04 | ✅ Appliqué | Purge sans réinjection d'historique, reconnexion conservant les logs, historique attendu avant le SSE. |
| RES-F05 | ✅ Appliqué | Timer SSE annulable et backoff exponentiel 5 s → 30 s. |
| RES-F06 | ✅ Appliqué | Options désactivées pendant l'action et cases destructives décochées après un démarrage réussi. |
| PERF-F01 | ✅ Appliqué | Mesure de `isAtBottom` avant mutation et insertion par `DocumentFragment` (`addLogLines`). |
| PERF-F02 | ✅ Appliqué | Compteur borné (`500 lignes (max)`), écouteur `scroll` passif et polling suspendu en onglet masqué. |
| PERF-F03 | ✅ Appliqué | Pile de polices système, suppression des liens Google Fonts et des domaines dans la CSP. |
| A11Y-F01 | ✅ Appliqué | Classes `.status-text--ok/warning/danger/muted` conformes AA en clair et sombre ; `h1` et sous-titres ajustés. |
| UX-F01 | ✅ Appliqué | Pastilles `.running` (pulsation réservée), `.paused`, `.error`, `.offline` distinctes. |
| SEC-F02 | ✅ Appliqué | Plus de mutations `.style.*` ; `hidden`, classes sémantiques et CSP avec `base-uri`, `form-action`, `object-src`. |
| SEC-F03 | ✅ Appliqué | Formulaire `POST /logout` avec jeton CSRF ; `GET /logout` conservé. La balise `<meta name="csrf-token">` de `/login` est conservée pour `TestFlaskSmoke._csrf_token()`. |
| SEC-F04 | ✅ Appliqué | `clearConsole()` crée l'état vide sans `innerHTML` ; commentaire CSP obsolète supprimé. |
| A11Y-F02 | ✅ Appliqué | `role="switch"`, libellé constant « Mode sombre », `aria-checked` synchronisé. |
| A11Y-F03 | ✅ Appliqué | `tabindex="0"` et libellé sur la console, `#encoderEngineInfo` visible, focus `:focus-visible` renforcé. |
| A11Y-F04 | ✅ Appliqué | Titre unique « État des services », `<dl>`, `<fieldset>/<legend>`, `<main>` sur `/login`, texte `.btn-loading` conservé sous `prefers-reduced-motion`. |
| A11Y-F05 | ✅ Appliqué | Libellé SVG `Export MP4 (prproj2ffmpeg)`. |
| UX-F02 | ✅ Appliqué | `<dialog>` HTML5 non bloquant pour « Arrêter » et « Effacer les logs » ; plus aucun `confirm()` natif. |
| UX-F03 | ✅ Appliqué | Thème sombre `/login` via `prefers-color-scheme` et `autocomplete` vérifiés. |
| CSS-F01 | ✅ Appliqué | Doublon `body.dark .log-content` supprimé, `!important` obsolètes retirés, `transition: all` remplacés. |

> Note : la confirmation d'effacement et d'arrêt utilise `<dialog>` plutôt que `window.confirm()`
> afin de respecter `UX-F02` sans bloquer le thread principal ni le flux SSE.
# Audit du portage Linux / FilmCraft

> **Document historique** — cet audit a été réalisé le 2026-10-10, avant la migration vers
> `prproj2ffmpeg` (remplacement de la chaîne XML/FilmCraft par les projets `.prproj` encodés par
> ffmpeg/NVENC, voir `PROVENANCE.md` et le plan de migration). Il reste utile pour comprendre les
> choix de robustesse du moniteur et de l'UI, qui n'ont pas changé ; les références à
> `filmcraft_cli.py`/`xmeml_utils.py` concernent le code d'alors (archivé dans `backup_pre_prproj/`).

**Date :** 2026-10-10 · **Périmètre :** code neuf ou modifié du projet `~/export_prproj_encoder`
(`app.py`, `config.py`, `db_helper.py`, `encoder_worker.py`, `filmcraft_cli.py`,
`monitor_exports_and_move.py`, `submit_to_watchfolder.py`, `xmeml_utils.py`, `filename_utils.py`,
`run_app.sh`, tests), par rapport à la version Windows déjà auditée (`F:\export_media_encoder`).
**Méthode :** revue du code, exécution de la suite de tests (32 tests), puis validation E2E réelle
(export complet sur le projet `117 Camille`, scénarios d'échec, d'abandon et de crash).

## Synthèse

Environnement vérifié : Ubuntu (noyau 6.x), Python 3.10.12, Flask 3.0.0, Werkzeug 3.0.1,
watchdog 6.0.0, python-dotenv 1.2.4, FilmCraft 0.5.0 (`/usr/bin/filmcraft-cli`), NVENC H.264 actif
(`export.hardware.sessions=1, declined=0`), `/mnt/cache` = NTFS via `ntfs-3g` (FUSE).

Tous les constats ci-dessous ont été corrigés dans cette version. Les limites assumées sont listées
en fin de document.

## Constats et remédiations

| # | Sévérité | Constat | Remédiation appliquée |
|---|---|---|---|
| A1 | moyenne | Tampon de lecture de la sortie du CLI non borné (`_stream_output`) : une ligne unique démesurée pouvait consommer toute la mémoire. | Plafond `_MAX_LINE_BYTES` (1 Mo) : la ligne est remontée telle quelle au lieu d'être accumulée. |
| A2 | basse | Le journal JSONL par job n'était écrit qu'à la fin de l'exécution : un crash pendant l'encodage perdait le diagnostic. | Écriture en flux (`on_persist` + `flush()` par ligne) : le journal est exploitable même après `kill -9`. |
| A3 | **haute** | L'option « Nettoyer les sorties » supprimait la **quarantaine** (perte possible d'un export non identifié) et les fichiers du `.staging` (destruction du job en cours d'encodage). | Le nettoyage ne porte plus que sur les `*.mp4` **publiés** ; staging et quarantaine sont préservés ; les fichiers plus récents que `CLEAN_OUTPUT_GUARD_SECONDS` (60 s) sont épargnés et signalés. |
| A4 | **haute** | Adoption d'une timeline déposée à la main : le fichier était renommé avec son `job_id`, mais l'encodeur utilisait ensuite l'ancien chemin → premier essai systématiquement en échec (`No such file or directory`). | `_ensure_submission()` retourne désormais le chemin réel ; `_pick_next_job()` propage ce chemin. Bug détecté par le test E2E, corrigé et revérifié. |
| A5 | moyenne | Boucle de réencodage possible : un job à statut terminal (`EXPORT_FAILED`) dont l'archivage échouait était repris à chaque tour de scrutation (GPU sollicité sans fin). | Les statuts terminaux sont archivés sans réencodage ; seul `SUBMITTED_TO_WATCHFOLDER`/`EXPORT_ENCODING` est repris (et `EXPORT_DONE*` si la sortie a disparu). |
| A6 | basse | Compteurs d'état incrémentés en lecture/modification/écriture hors du verrou (`processed`, `failed`, `aborted`). | Helper `_bump()` exécuté sous `_state_lock`. |
| A7 | basse | `submission_log.csv` était un fichier mixte (lignes de texte + lignes CSV) hérité de la version Windows : illisible par un analyseur strict. Constat déjà présent dans l'audit Windows (R4), non remédié. | Séparation : `submission_log.csv` = registre structuré (jamais tronqué), `submission_log.txt` = journal texte (archivé à chaque soumission) ; `/api/logs/history` sert le journal texte. |
| A8 | basse | Code mort : `csv_lock` inutilisé dans le moniteur ; imports inutilisés (`os`, `abort` dans `app.py`, `LOGS_DIRECTORY` et `add_submission` dans l'encodeur, `shutil` dans les tests). | Supprimés. |
| A9 | basse | Un preset erroné (tiret demi-cadratin « – », casse, espace) n'était détecté qu'au premier export. | Vérification au démarrage via `filmcraft-cli export --list-presets` (parsing du tableau JSON de la 0.5.0, comparaison tolérante) ; avertissement non bloquant avec la liste des presets disponibles. |
| A10 | moyenne | Sur la version Windows, `shutil.move()` vers la destination finale pouvait être interrompu : le portage introduit un déplacement **inter-systèmes de fichiers** (ext4 → NTFS/FUSE), plus long et donc plus exposé ; un MP4 tronqué pouvait apparaître sous son nom final. | `move_to_final_verified()` : `os.replace()` si même système de fichiers, sinon copie vers `.<nom>.part` sur la destination, vérification de la taille, puis `os.replace()` atomique et suppression de la source ; `.part` nettoyé en cas d'échec. |
| A11 | **haute** | Régression fonctionnelle garantie si elle n'avait pas été anticipée : le moniteur n'acceptait que le statut `SUBMITTED_TO_WATCHFOLDER`, or l'encodeur fait progresser le statut avant la publication — les fichiers auraient été ignorés. | `MATCHABLE_STATUSES` + `is_matchable_status()` partagés entre `config`, `db_helper` et le moniteur (`get_matchable_submissions`). |
| A12 | basse | Le disque scanné est monté en FUSE (`ntfs-3g`) ; un événement inotify peut être manqué (montage réseau/saturation). | Rescan périodique de `WATCH_OUT` (`RESCAN_INTERVAL_SECONDS`, 60 s, 0 = désactivé) réutilisant la routine du balayage initial, protégé par la déduplication par chemin. |
| A13 | basse | Un `filmcraft-cli` pouvait survivre à un arrêt de l'application (process dans sa propre session) et continuer d'encoder. | `ProcessRegistry` + `SIGTERM` sur le groupe de process puis `SIGKILL` après grâce ; `shutdown()` appelé par `atexit` et par un gestionnaire `SIGTERM` ; `clean_staging_leftovers()` au démarrage. Vérifié par un test de crash (`kill -9` de l'app et du CLI). |
| A14 | basse | Fuite d'information : le fichier `.env` contient `API_PASSWORD` et `SECRET_KEY` en clair (héritage Windows). | `.env` inscrit dans `.gitignore`, valeurs générées aléatoirement à l'installation, `.env.example` sans secret ; un mot de passe temporaire fort est généré et affiché si `API_PASSWORD` est vide. Aucun secret n'est journalisé. |
| A15 | basse | Aucune validation de cohérence de la configuration (dossiers inexistants, preset inconnu, binaire absent) : les erreurs n'apparaissaient qu'à l'usage. | Contrôles au démarrage : création des dossiers manquants, erreur explicite si `WATCH_IN` est absent, résolution de `filmcraft-cli` exposée dans `/api/status`, vérification du preset, journalisation de la configuration effective dans `encoder_log.txt`. |
| A16 | **haute** | **Formulaire de connexion inutilisable après un échec** : la page réaffichée sur erreur (401/403/429) était rendue sans `csrf_token`, le champ caché restait vide et **toute nouvelle tentative échouait en 403** (« Jeton de sécurité invalide ») jusqu'à un rechargement manuel de la page. Bug latent hérité de la version Windows, révélé par l'usage réel (double-clic + identifiants refusés une première fois). Diagnostic établi à partir du journal du serveur (`POST /login` en 401 puis 403 en série). | `csrf_token=_generate_csrf_token()` fourni dans **tous** les rendus de `login.html` (GET, identifiants invalides, jeton invalide, blocage anti-bruteforce) ; `Cache-Control: no-store` sur toute page HTML (un jeton périmé ne peut plus être resservi depuis le cache du navigateur) ; message d'erreur précisant l'origine des identifiants (`.env`) ; deux tests de régression (échec puis réessai immédiat sans rechargement, en-tête `no-store`). |

## Points vérifiés en exécution

| Vérification | Résultat |
|---|---|
| `python -m unittest discover` | 34 tests OK (unitaires + smoke Flask : CSP, auth, CSRF, `/api/status`, favicon, régression du formulaire de connexion) |
| `py_compile` sur les 9 modules + `node --check static/script.js` | OK |
| Export réel (projet `117 Camille`, timeline de 1819 images) | 34,6 s, NVENC (1 session, 0 refus), 94 334 444 oct., **MD5 `7f42271f8001bd852031e9ede62a1917` identique au fichier produit par le test isolé `xml2mp4.sh`** |
| `filmcraft-cli probe` sur le MP4 produit | H.264 High, 1080×1920, 25/1 fps, débit vidéo 10,05 Mbps (CBR), audio AAC 48 kHz stéréo |
| Skip idempotent (sans `--force`) | `SKIP: Export final … existe déjà` |
| Collision à destination | `…_v1.mp4` puis `…_v2.mp4`, aucun écrasement de l'export de référence |
| Latence de déplacement d'un export publié par l'encodeur | **1,2 s** (vérification de stabilité sautée car le statut `EXPORT_DONE` atteste de la publication atomique) contre ~12 s auparavant ; un fichier externe sans `job_id` reste stabilisé (2×5 s) puis mis en quarantaine (11,4 s), et un fichier portant un `job_id` encore au statut `EXPORT_ENCODING` passe bien par la stabilisation |
| Médias hors-ligne | statut `EXPORT_DONE_WITH_WARNINGS`, chemin du `.fcproj` journalisé (scénario prévu, non rencontré sur la fixture) |
| XML invalide déposé dans `WATCH_IN` | adoption + un seul essai + `EXPORT_FAILED` + journal JSONL et `.fcproj` conservés + archivage Source, aucun réencodage |
| « Arrêter » pendant l'encodage | `EXPORT_ABORTED`, aucun fichier partiel dans `WATCH_OUT` ni `.staging`, aucun process `filmcraft-cli` orphelin |
| Crash brutal (`kill -9` app + CLI) puis redémarrage | fichier partiel purgé, job `EXPORT_ENCODING` repris automatiquement, export terminé (`_v2.mp4`) ; arrêt propre sur `SIGTERM` (`Encodeur arrêté proprement`, `Moniteur arrêté proprement`) |
| E2E isolé (bac à sable, port 5001) | chaîne complète OK, `submission_log.csv` = 1 ligne structurée, `submission_log.txt` = journal lisible, `/api/logs/history` sert bien le journal texte |

## Limites assumées (hors périmètre)

1. **Espace disque non vérifié** avant lancement d'un encodage (~100 Mo par job dans `.staging`) : un
   manque de place produit un `EXPORT_FAILED` explicite (message du CLI) et un fichier partiel purgé.
2. **Encodage séquentiel** volontaire (une session NVENC, RAM maîtrisée) : pas de `MAX_PARALLEL_EXPORTS`.
3. **Pas de réécriture des `pathurl`** : un XML produit sous Windows (chemins `file://localhost/F:/…`)
   donne des médias hors-ligne (statut `EXPORT_DONE_WITH_WARNINGS` et `.fcproj` à relinker).
4. **Pas de suivi NVENC dans le statut** : un repli logiciel (`declined > 0`) est visible dans le
   journal du job et la console, mais le statut final reste `EXPORT_DONE`.
5. **Session Flask en mémoire** : un redémarrage du serveur impose une reconnexion ; les jobs en
   cours sont repris automatiquement (statuts et staging sont persistés sur disque).
6. **Version Windows et version Linux non simultanées** : les deux écrivent le MP4 final au même
   endroit (le versionnage éviterait la perte, mais produirait des exports dupliqués).

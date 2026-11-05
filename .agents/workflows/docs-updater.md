---
description: Mettre à jour la documentation Python/Flask à partir du code, des tests et des mesures
---

# Workflow Docs Updater

> Ce workflow harmonise la documentation de `export_prproj_encoder` à partir
> du code réel, des tests et des mesures. Il ne remplace pas la relecture
> humaine.

## Protocoles critiques

1. **Périmètre** : documentation uniquement (`docs/`, `README.md`, `AGENTS.md`,
   et commentaires de `.env.example` si nécessaire). Ne pas modifier le code,
   les tests, la configuration ou les données.
2. **Outils autorisés** : `read`, `grep`, `glob`, `edit`, `write`, `shell`
   (limité aux audits `git`, `find`, `rg`, `wc`, `unittest`, `py_compile`).
3. **Contexte** : lire `AGENTS.md` et `docs/README.md` au démarrage, puis
   uniquement la documentation du périmètre concerné.
4. **Source de vérité** : code, tests et mesures > documentation existante >
   historique git. `AGENTS.md` fixe les conventions.
5. **Sécurité** : ne jamais lire `.env` (secrets) ni les journaux de production ;
   ne pas modifier `WATCH_IN/`, `WATCH_OUT/`, `logs_watchfolder/`, `*.db`,
   `test_sandbox*` ou les fichiers ignorés.
6. **Documents historiques** : `docs/archive/`, `docs/audits/` et
   `docs/audit_linux_port.md` ne décrivent pas nécessairement l'état courant ;
   ne pas les réécrire comme des docs actives. Une correction factuelle doit
   être datée et limitée.

## Étape 1 — Audit structurel, métrique et contractuel

### Cartographie

```sh
git ls-files '*.py' '*.md' '*.sh' '*.html' '*.css' '*.js' '.env.example'
find docs -maxdepth 2 -type f -name '*.md' | sort
```

### Volumétrie

```sh
wc -l AGENTS.md README.md *.py
find docs -name '*.md' -print0 | xargs -0 wc -l | tail -1
```

### Tests et dette

```sh
./.venv/bin/python -m unittest discover -v
./.venv/bin/python -m py_compile app.py config.py db_helper.py \
  filename_utils.py prproj_engine.py submit_to_watchfolder.py \
  encoder_worker.py monitor_exports_and_move.py test_app.py test_watchdog.py
rg -n 'TODO|FIXME|HACK|XXX' --glob '*.py'
```

Il n'y a pas de linter ni de formatter configuré dans ce dépôt : ne pas lancer
d'outil non prévu par `AGENTS.md`.

### Fichiers récemment modifiés

```sh
git log --since='30 days ago' --name-only --pretty=format: | sort -u
```

## Étape 2 — Diagnostic triangulé

| Source | Rôle | Outil |
| --- | --- | --- |
| Intention | Le « pourquoi » | `AGENTS.md`, `README.md`, demande initiale, audit concerné |
| Réalité | Le « quoi » et le « comment » | code, tests, `config.py`, routes Flask, `rg` |
| Existant | L'état documenté | `docs/*.md`, `.env.example`, `README.md` |

Identifier les divergences :

- un module, une route ou un statut documenté qui n'existe plus ;
- une commande, un chemin ou un nom de journal obsolète ;
- une variable d'environnement ou une valeur par défaut périmée ;
- un chiffre de test ou de performance qui n'est plus reproductible ;
- une décision non consignée ou un contrat HTTP/JSON/SQLite/CSV modifié ;
- une référence à un fichier supprimé ou déplacé.

## Étape 3 — Sélection du standard de rédaction

- **API / routes** : méthodes, authentification, CSRF, entrées/sorties, codes
  HTTP, erreurs, SSE.
- **Pipeline** : flux `.prproj` → soumission → encodage → moniteur, fichiers
  consommés/produits, prérequis ffmpeg/NVENC, reprise et abandon.
- **Architecture** : modules, threads, responsabilités ; diagrammes Mermaid si
  utile.
- **Exploitation** : `run_app.sh`, `stop_app.sh`, `install_shortcut.sh`,
  watch folders, journaux, nettoyages, redémarrage.
- **Configuration** : `config.py` et `.env.example`, valeurs par défaut et
  effets.
- **Performance** : `docs/perf_export.md`, protocole de benchmark, mesures,
  fidélité, cache alpha.
- **Script vendorisé** : `tools/prproj-unpacker/PROVENANCE.md` et
  `UPSTREAM-SYNC.md` ; ne pas documenter une divergence locale comme un
  comportement upstream.

## Étape 4 — Proposition de mise à jour

Produire un plan avant d'appliquer :

```markdown
## Plan de mise à jour

### Audit
- Cible : `docs/encoder_worker.md`
- Constat : la commande d'encodage ne mentionne plus le staging.

### Modifications
- Type : pipeline
- Diagnostic : obsolète
- Correction : ...

### Vérification
- Commandes/test exécutés
- Documents/index à mettre à jour
```

## Étape 5 — Application et finalisation

1. Appliquer avec `edit` (changement ciblé) ou `write` (réécriture).
2. Vérifier les commandes, chemins, noms de variables, statuts et routes cités.
3. Mettre à jour `docs/README.md` si un document est ajouté, renommé ou
   supprimé.
4. Mettre à jour `README.md` et `AGENTS.md` uniquement si les commandes, la
   structure ou les contrats changent.
5. Lancer `./.venv/bin/python -m unittest discover -v` si le code a changé
   pendant la session ; sinon vérifier au minimum les commandes citées.
6. Lancer `git diff --check`.
7. Vérifier `git status` : seuls les documents prévus doivent être modifiés ;
   aucun fichier `WATCH_IN/`, `WATCH_OUT/`, `logs_watchfolder/`, `*.db`,
   `test_sandbox*` ou `.env`.

## Checkpoints

Avant :

- [ ] Résumé en tête si le document est long.
- [ ] Public cible identifié.
- [ ] Sources de vérité lues (`AGENTS.md`, code, tests, docs existantes).

Pendant :

- [ ] Tableau de trade-offs si plusieurs options.
- [ ] Commandes copiables.
- [ ] Limites et rollback.
- [ ] Style factuel, sans remplissage.

Après :

- [ ] Liens et chemins valides.
- [ ] Chiffres et valeurs par défaut à jour.
- [ ] Cohérence avec `AGENTS.md`, `README.md`, `.env.example` et le code.
- [ ] Index `docs/README.md` à jour.
- [ ] Aucune donnée de production modifiée.

## Traçabilité

- Le corps du commit mentionne les documents synchronisés et les décisions
  prises.
- Le workflow ne se termine pas si les checkpoints ne sont pas cochés.
- Les décisions durables sont consignées dans la documentation du module
  concerné, ou dans `AGENTS.md` si elles changent les conventions.
- Les documents historiques restent dans `docs/archive/` et `docs/audits/`.

## Références

- `AGENTS.md`
- `README.md`
- `docs/README.md`
- `docs/app.md`
- `docs/submit_to_watchfolder.md`
- `docs/encoder_worker.md`
- `docs/monitor_exports_and_move.md`
- `docs/folder_structure.md`
- `docs/perf_export.md`
- `.env.example`
- `test_app.py`, `test_watchdog.py`
- `tools/benchmark_export.py`
- `tools/prproj-unpacker/PROVENANCE.md`, `tools/prproj-unpacker/UPSTREAM-SYNC.md`

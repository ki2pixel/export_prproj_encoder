---
description: Exécuter les contrôles Python, committer et pousser la branche courante
---

# Commit & Push (branche courante)

## Prérequis

- Des fichiers modifiés existent.
- Le remote `origin` est configuré.
- Les secrets ne sont pas dans le diff (`.env` ; `.env.example` est versionnable).
- Aucune donnée de production n'est modifiée : `WATCH_IN/`, `WATCH_OUT/`,
  `logs_watchfolder/`, `*.db`, `test_sandbox*`, `/mnt/cache`.
- Le dépôt Windows `export_media_encoder` n'est pas modifié.

## Étapes

1. Vérifier l'état : `git status`, `git diff --stat`.
2. Lancer les contrôles :
   ```sh
   ./.venv/bin/python -m unittest discover -v
   ./.venv/bin/python -m py_compile app.py config.py db_helper.py \
     filename_utils.py prproj_engine.py submit_to_watchfolder.py \
     encoder_worker.py monitor_exports_and_move.py test_app.py test_watchdog.py
   git diff --check
   ```
   Il n'y a pas de linter ni de formatter configuré : ne pas lancer d'outil non
   prévu par `AGENTS.md`.
3. Stager explicitement les fichiers voulus :
   ```sh
   git add <fichiers>
   git diff --cached --check
   ```
   Éviter `git add -A` par défaut, surtout si `.agents/` ou des fichiers non
   suivis ne doivent pas être commités.
4. Committer avec un message Conventional Commits dont le résumé est en
   français, généré depuis le diff :
   ```sh
   git commit -m "<type>: <résumé court en français>"
   ```
   Types usuels : `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `perf`.
5. Pousser :
   ```sh
   git push -u origin "$(git branch --show-current)"
   ```

## Exécution étape par étape

```sh
BRANCH=$(git branch --show-current)

git status
git diff --stat

./.venv/bin/python -m unittest discover -v
./.venv/bin/python -m py_compile app.py config.py db_helper.py \
  filename_utils.py prproj_engine.py submit_to_watchfolder.py \
  encoder_worker.py monitor_exports_and_move.py test_app.py test_watchdog.py
git diff --check

git add <fichiers>
git diff --cached --check
git commit -m "<type>: <résumé court en français>"
git push -u origin "$BRANCH"
```

## Notes

- Il n'y a pas de CI : la suite `unittest` est la vérification obligatoire.
- Ne pas committer `.env`, `.venv/`, `WATCH_IN/`, `WATCH_OUT/`,
  `logs_watchfolder/`, `*.db`, `test_sandbox*`, `__pycache__/` ni les MP4 ou
  médias générés.
- Ne pas mettre de chemins locaux expérimentaux dans `.env.example` ou
  `config.py` ; les valeurs par défaut doivent rester génériques.
- Si `tools/prproj-unpacker/prproj2ffmpeg.py` est modifié, vérifier les tests
  concernés et mentionner `PROVENANCE.md`/`UPSTREAM-SYNC.md` dans le commit.
- Pour un changement de contrat HTTP/JSON, de statuts SQLite/CSV ou de
  nommage/`job_id`, ajouter dans le corps du commit les tests exécutés et les
  contrats modifiés.
- Ne pas redémarrer l'application de production ; signaler si un redémarrage
  est nécessaire.
- En cas d'échec d'un check, corriger avant de committer ; ne pas contourner.

# Arborescence des dossiers

## Côté projets (disque `F:` sous Windows, monté `/mnt/cache` sous Linux)

```
/mnt/cache/
├── 134 Camille/
│   ├── projets/                                  # projets Premiere de travail
│   │   ├── M6 - AVDP - …_9x16.prproj             # projet à exporter
│   │   ├── M6 - AVDP - …_9x16_assist.prproj      # variante prioritaire (MP4 final identique)
│   │   ├── M6 - AVDP - …_9x16_2.prproj           # variante distincte (suffixe de version conservé)
│   │   ├── M6 - AVDP - …_9x16.ecproj             # projet EffectCraft (ignoré par le scan)
│   │   ├── Adobe Premiere Pro Auto-Save/         # artefacts de travail — ignorés par le scan
│   │   └── …_9x16.mp4                            # exports manuels éventuels (non déplacés)
│   └── M6 - AVDP - …_9x16.mp4                    # export final attendu (déplacé ici par le moniteur)
└── 138 Camille/ · …                              # autres projets, même structure
```

Règles :

- le scan ne descend **pas** dans les sous-dossiers de `projets/` : seuls les projets du premier
  niveau sont considérés (les autosaves et caches de travail sont ignorés ; les fichiers temporaires
  de Premiere `*.prproj~`/`*.prproj.lck` ne correspondent pas au motif `*.prproj`) ;
- si `X_assist.prproj` et `X.prproj` coexistent, seule la variante `_assist` est exportée ; une
  assist orpheline reste exportée normalement ;
- le **nom du MP4 final** vient du nom du fichier `.prproj` nettoyé (préfixe numérique, suffixe
  « copy N » et suffixe `_assist` retirés, suffixes de version conservés) :
  `…_9x16_2.prproj` → `…_9x16_2.mp4` et `…_9x16_assist.prproj` → `…_9x16.mp4` ;
- la destination est toujours la racine du dossier projet (`resolve_project_root`), jamais
  `projets/` ;
- une collision de nom produit une version (`…_v1.mp4`, `…_v2.mp4`) plutôt qu'un écrasement
  (`ALLOW_OVERWRITE=False` par défaut) ;
- les chemins médias du XML interne sont résolus par `prproj2ffmpeg --base` : racine du projet
  d'origine, son parent, `BASE_SCAN_FOLDER` (plus `PRPROJ2FFMPEG_EXTRA_BASES` si nécessaire).

## Côté application (`~/export_prproj_encoder`)

```
export_prproj_encoder/
├── WATCH_IN/                     # watchfolder d'entrée (copie des projets à encoder)
│   ├── <stem>_<job_id>.prproj    # en attente de traitement par l'encodeur
│   └── Source/<horodatage>/      # archive des projets déjà traités (--clean_source_archive)
├── WATCH_OUT/                    # watchfolder de sortie
│   ├── <stem>_<job_id>.mp4       # export publié, en attente de déplacement par le moniteur
│   ├── .staging/                 # encodage en cours (jamais exposé au moniteur)
│   └── quarantine/               # exports non rapprochés d'une soumission (inspection manuelle)
├── tools/prproj-unpacker/        # prproj2ffmpeg.py vendorisé (+ PROVENANCE.md, unpack.pl, LICENSE)
├── logs_watchfolder/
│   ├── jobs.db                   # base SQLite (statuts par job_id)
│   ├── submission_log.csv        # registre structuré des soumissions
│   ├── submission_log.txt        # journal texte lisible (+ archives horodatées)
│   ├── monitor_log.txt           # journal du moniteur (+ backup à 5 Mo)
│   ├── encoder_log.txt           # journal de l'encodeur (+ backup à 5 Mo)
│   └── exports/<stem>_<job_id>.log   # journal complet de chaque encodage (commande ffmpeg + sortie)
├── fixtures/                     # fixtures réelles pour les tests (projet .prproj)
└── test_sandbox*/                # bacs à sable des tests unitaires (créés puis supprimés)
```

## Cycle de vie d'un projet

```
projets/<nom>.prproj
    │  soumission (copie atomique)
    ▼
WATCH_IN/<nom>_<job_id>.prproj ──► (prproj2ffmpeg : ffmpeg/NVENC) ──► WATCH_OUT/.staging/…_<job_id>.mp4
    │                                                        │ publication atomique (os.replace)
    ▼ archive Source/<horodatage>/                            ▼
(conservée pour audit)                              WATCH_OUT/…_<job_id>.mp4
                                                             │ rapprochement par job_id
                                                             ▼
                                        /mnt/cache/<projet>/<nom final>.mp4   (versionné si collision)
```

## Nettoyages disponibles

| Option | Effet |
|---|---|
| `--clean_source_archive` | vide `WATCH_IN/Source/` (archives des projets traités) |
| `--clean_outputs` | vide les `*.mp4` **publiés** de `WATCH_OUT` ; ne touche jamais au staging ni à la quarantaine ; épargne les fichiers de moins de `CLEAN_OUTPUT_GUARD_SECONDS` |

`logs_watchfolder/exports/*.log` n'est jamais supprimé automatiquement : il sert au diagnostic
(commande ffmpeg complète, erreurs, phases de cache).

# Prproj Tools

Deux outils pour travailler avec des projets Adobe Premiere Pro sans ouvrir Premiere :

1. **`prproj2ffmpeg.py`** — exporte une séquence d'un projet en MP4 H.264 (GPU si disponible) directement depuis le `.prproj` (ou son XML décompressé), en reproduisant fidèlement les animations et les calques alpha.
2. **`unpack.pl`** — utilitaire Perl qui extrait le XML d'un `.prproj` (archive gzip), pour inspection ou usage manuel.

Le principe directeur de l'export : **le XML du `.prproj` est la seule source de vérité.** Aucune donnée externe (dossier `docs/`, tracking, etc.) n'est lue, aucune hypothèse n'est faite sur le nombre de cuts ou de segments — le script rend ce que contient le XML, rien de plus.

## Prérequis

- **Python 3** (bibliothèque standard uniquement, aucun paquet à installer)
- **`ffmpeg` ≥ 4.2 et `ffprobe`** dans le `PATH`
- Recommandé : encodage matériel NVIDIA (`h264_nvenc`, testé GTX 1650) — repli automatique sur `libx264`
- Pour `unpack.pl` : Perl avec `IO::Uncompress::Gunzip`

## prproj2ffmpeg.py

### Usage

```bash
# Export complet depuis le .prproj (décompression interne, aucun prérequis)
python3 prproj2ffmpeg.py "projets/mon_projet.prproj"

# Options courantes
python3 prproj2ffmpeg.py projet.prproj --out export.mp4 --bitrate 10M
python3 prproj2ffmpeg.py projet.prproj --base "/mnt/cache/134 Camille"   # racine des médias
python3 prproj2ffmpeg.py projet.prproj --dry-run                         # affiche la commande ffmpeg
python3 prproj2ffmpeg.py projet.prproj --dump                            # modèle JSON de la timeline
```

| Option | Défaut | Rôle |
| --- | --- | --- |
| `input` | — | fichier `.prproj` ou `.xml` décompressé |
| `--out` | à côté de l'entrée | chemin du MP4 de sortie |
| `--bitrate` | `10M` | débit vidéo (CBR avec `-maxrate`/`-bufsize`) |
| `--encoder` | `h264_nvenc` si réellement utilisable, sinon `libx264` | encodeur ffmpeg (valeur forcée jamais repliée) |
| `--base` | dossier du XML, son parent, cwd | racine(s) pour résoudre les chemins Windows du XML ; répétable |
| `--sequence` | première séquence | nom de la séquence à exporter |
| `--dry-run` | — | imprime la commande ffmpeg sans l'exécuter |
| `--dump` | — | imprime le modèle (segments, overlays, durée) en JSON et sort |

### Ce qui est reproduit depuis le XML

- **Piste de base** : chaque item est rendu une seule fois, dans l'ordre de la timeline. Les trous deviennent du noir ; la durée totale est la fin du dernier item.
- **Transformations (composant `Motion`)** : Position, Échelle, Rotation, Ancre — statiques **ou** animées.
  - **Keyframes linéaires et Bézier** sont toutes deux gérées, détectées clé par clé : l'encodage linéaire (vitesses + influence 1/6) donne une interpolation droite exacte ; l'encodage Bézier (`5,0,…` + poignées) donne la courbe cubique exacte lue depuis le XML. Les intervalles mixtes sont approximés par échantillonnage.
  - La rotation utilise la convention Premiere (positif = horaire) et peut être animée (rendu par composition avec la formule d'ancre exacte).
  - L'échelle peut varier au cours du temps (zoom animé), y compris avec un lissage Bézier.
- **Pistes overlay** : les médias des pistes supérieures sont composités dans l'ordre des pistes
  (Z-order Premiere : V2 sous V3, couche 0 = images fixes de V1) :
  - images avec alpha (PNG/JPG) et vidéos avec alpha (ProRes 4444 `.mov`) ;
  - fenêtres temporelles respectées (`enable` par item) ;
  - un même élément rejoué consécutivement n'est lu en **boucle** que si sa durée couvre la fin
    réelle du fichier (sinon les replays restent des `trim` séparés, corrects) ;
  - transformations Motion des overlays également supportées ; une image fixe à zoom/rotation
    animés est bouclée (`-loop 1`) pour que les keyframes soient évaluées sur la durée.
- **Audio** : **toutes** les pistes (`A1`, `A2`, `A3`…), silences pour les trous, mixage `amix`
  compensé (`volume=N`), sortie AAC 320 kbps 48 kHz stéréo.
- **Sortie** : H.264 8 bits (`yuv420p`), MP4 avec `+faststart`, à la fréquence de la séquence (25 fps dans nos projets).
  En `libx264`, `-b:v`/`-maxrate` pilotent le débit (pas de `-crf`), `-bufsize = 2 × débit`
  (20M si le débit est illisible).

### Ce que le script ne fait pas

- Seuls les composants **Motion** et **Opacity** sont rendus : pas d'effets vidéo, transitions, fondus, mélanges, remap de vitesse, ni de texte/graphismes générés par Premiere.
- Les pistes autres que vidéo/audio standards (nested sequences, ajustements…) ne sont pas rendues.
- Après une retouche manuelle dans Premiere, il faut **sauvegarder le projet puis relancer le script** (le XML est relu à chaque exécution).

### Optimisations automatiques (transparentes)

- **Intermédiaire 8 bits des médias alpha** : un ProRes 4444 10/12 bits est converti une fois en qtrle RGBA 8 bits (lossless, alpha exact) — décodage et conversion bien plus rapides, différence visuelle imperceptible. Cache dans `~/.cache/prproj2ffmpeg/` (ou `$XDG_CACHE_HOME`), clé = chemin + taille + date → reconstruction automatique si le média change.
- **Crop alpha-bbox** : un overlay n'est composité que sur la zone réellement opaque de son média (calculée sur toutes ses frames), pas sur tout le cadre.
- **Lecture en boucle fusionnée** : un élément rejoué N fois depuis son début devient un seul flux continu (`-stream_loop`), sans décodages ni buffers superflus — uniquement si la durée du replay couvre tout le fichier.
- **Regroupement des segments** : les items contigus d'un même fichier partagent un seul décodeur, découpé en interne.
- **Résolution des médias** : toutes les profondeurs de suffixe du chemin Windows sont essayées (pas seulement les 2 premiers segments), avec cache positif et négatif.
- **Zéro fichier temporaire** : le `.prproj` (gzip ou XML brut) est décompressé et parsé en mémoire ; aucun `.prproj2ffmpeg-tmp.xml` n'est écrit à côté du projet.
- **Cache** : purge LRU après construction complète du graphe (les pulls du job sont protégés), sidecar `.bbox.json` écrit atomiquement, arbre `axis_expr` équilibré (profondeur `log2(N)`).

Ordre de grandeur mesuré sur un projet 9:16 de 79,8 s (1080×1920, overlay animé alpha + rotation + zoom) : **~25 s d'export, ~1,1 Go de RAM** avec `h264_nvenc`.

### Dépannage

- `media not found` : les chemins du XML pointent vers le poste d'origine (`F:\...`). Utiliser `--base <racine>` autant de fois que nécessaire — le script essaie aussi le dossier du projet et le répertoire courant.
- Pas de GPU NVIDIA, pilote absent ou session NVENC occupée : le script teste réellement
  l'initialisation NVENC et bascule sur `libx264` (plus lent) en journalisant
  `[encoder] h264_nvenc indisponible (GPU/session NVENC), repli libx264`. Un encodeur forcé
  avec `--encoder h264_nvenc` n'est jamais replié silencieusement.
- Pour inspecter ce que le script comprend du projet : `--dump` (modèle JSON) ou `--dry-run` (commande ffmpeg complète).

## unpack.pl

Extrait le XML d'un `.prproj` :

```bash
perl unpack.pl "chemin/vers/fichier.prproj"                 # -> fichier.xml à côté
perl unpack.pl "chemin/vers/fichier.prproj" "sortie.xml"    # chemin explicite
```

`prproj2ffmpeg.py` n'en a pas besoin (il lit le `.prproj` directement) — cet outil reste utile pour lire/analyser le XML à la main.

## Licence

Voir [LICENSE](LICENSE).

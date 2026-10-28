# Performance de l'export prproj2ffmpeg

Diagnostic et optimisation du 2026-10-23 sur le projet
`Gulli - Ma Super Ligue 1 - GOAL_9x16` (job `20261023_115105_ce321d`).

## Symptôme

| Job | Durée source | Total | Préparation alpha | Rendu/encodage | Vitesse |
| --- | --- | --- | --- | --- | --- |
| `ce321d` (GOAL) | 91,08 s | **607,2 s** | 33,1 s + 4,7 s = **37,8 s** | **~569 s** | **0,16x / 4 fps** |
| `4f1fc1` (frère, même `MSL1.mov`) | 102,5 s | **60,8 s** | 36,3 s + 5,2 s = **41,5 s** | **~19 s** | **~5x / 125 fps** |

Le job frère utilise le même clip alpha et reste rapide : le codec alpha n'est pas la cause
principale. Les 570 s du job GOAL viennent du graphe de filtres :

- piste de base : 19 branches `scale=3456:1944` (source 1920x1080 à 180 %) + `overlay` sur un
  canvas 1080x1920, puis `concat` ;
- overlay masqué : le même MP4, `alphamerge` avec un rectangle statique, puis
  **`scale=4838:2722`** (252 %) et `overlay` plein durée ;
- clip alpha `MSL1.mov` : recadré en `782x124+108+288` après bbox ; son rendu est minuscule, son
  coût est la conversion qtrle initiale (33 s, en cache ensuite).

## Correctif retenu

Un overlay à transformation statique (position/échelle fixes, rotation nulle) portant un masque
est rendu sur la **bbox transformée du masque**, pas sur le cadre entier :

- `visible_overlay_window()` calcule le crop source et la taille de sortie à partir de la bbox du
  masque (`drawbox` pour un rectangle, union + feather sinon) ;
- un rectangle plein et non inversé est entièrement opaque : `alphamerge` est supprimé ;
- `cx`/`cy` pairs pour les masques non simples et `crop=…:exact=1` partout : la troncature
  silencieuse du chroma yuv420p (qui décalait `cy=53` vers 52) est supprimée ;
- un rectangle plein n'est jamais élargi de 1 px ; `out_w`/`out_h` impairs inclus dans
  `odd_window` imposent le repli RGBA ;
- les overlays animés, tournés, non uniformes, flous ou à masque inversé conservent le chemin
  composition.

Cas GOAL (valeurs corrigées par l'audit du 2026-10-23) : `crop=444:267:740:53:exact=1` →
`scale=1119:673`, overlay en `(-22,-19)`, au lieu de `scale=4838:2722` + alphamerge. La piste de
base est **inchangée** (composition 3456x1944).

## Résultats

| Mesure | Avant | Après |
| --- | --- | --- |
| Export complet 91,08 s (cache alpha chaud) | 607,2 s | **34,8 s** |
| Vitesse ffmpeg | 0,16x / 4 fps | **2,68x / 67 fps** |
| Taille | 114 720 Ko | 114 729 Ko |
| Images | 2277 | 2277 |

À froid, ajouter ~38 s de préparation alpha : ~73 s, soit ~8x plus rapide.

### Fidélité (extrait 0-15 s, H.264 10 Mbps)

| Zone | SSIM | PSNR |
| --- | --- | --- |
| Global | 0,9874 | 35,45 dB |
| Base sous l'overlay (y 654-1920) | **0,9944** | — |
| Overlay masqué (y 0-654) | 0,9754 | 31,23 dB |

Le résidu est localisé dans le zoom 252 % : le crop-avant-scale introduit un déphasage
sous-pixel par rapport au scale plein cadre de la référence. La base est rendue par le même
graphe qu'avant (SSIM 0,9944, écart d'encodage).

### Optimisation écartée : padding de la piste de base

Étendre le crop-window aux items dont le cadrage sort du cadre (padding noir) supprimait le
`scale=3456:1944` des 19 branches. Mesures lossless sur 5 s :

- overlay optimisé (variante d'intersection) : SSIM 0,9876 / PSNR 34,24 dB ;
- overlay + padding de base : SSIM 0,9464 / PSNR 29,67 dB.

Le gain de rendu n'était que de ~1,3x (le scale 4838x2722 étant déjà supprimé), pour une
dégradation visible de toute l'image. Le chemin de base est donc resté en composition ; la
condition `crop_window_fits()` est conservée.

## Métriques après les correctifs d'audit (2026-10-23)

Même protocole que ci-dessus : extrait 15 s, `h264_nvenc`, 3 passages, cache alpha chaud, projet
GOAL et `--base /mnt/cache` ; référence = copie du script avant les correctifs (patchs 1.1 à 3.4).

| Version | Préparation dry-run | Médiane rendu 15 s | Vitesse | Taille extrait |
| --- | --- | --- | --- | --- |
| Référence (avant audit) | 0,49 s | 6,68 s (min 6,32 / max 6,97) | 2,24x | 19 556 704 o |
| Correctifs audit | 0,49 s | 11,82 s (min 11,77 / max 11,97) | 1,27x | 19 568 168 o |

Fidélité référence → correctifs (même encodeur, même débit, 375 images) :

| Mesure | Valeur |
| --- | --- |
| SSIM global (Y/U/V) | 0,9715 (0,9650 / 0,9842 / 0,9852) |
| PSNR moyen (y/u/v) | 31,55 dB (30,11 / 37,95 / 38,38) |

Le graphe vidéo est identique à un pixel près (fenêtre `444x267` au lieu de `444x268`, sortie
`1119x673` au lieu de `1119x675`) : l'écart SSIM/PSNR ci-dessus mesure ce réalignement vertical
d'un pixel entre les deux variantes de fenêtre. Le contrôle par rapport à la **composition pleine
(`visible_overlay_window` désactivé)**, extrait 15 s lossless, confirme la correction :

| Variante de fenêtre | SSIM vs composition pleine | PSNR vs composition pleine |
| --- | --- | --- |
| Avant audit (`444x268` → `1119x675`) | 0,9874 | 35,45 dB |
| Après audit (`444x267` → `1119x673`, `exact=1`) | 0,9835 | **35,97 dB** |

Le PSNR progresse (le décalage chroma `cy=53 → 52` est supprimé) ; le SSIM recule très légèrement
à cause du rééchantillonnage vertical d'un pixel inhérent à la fenêtre corrigée. Hors fenêtre
visible, les deux scripts produisent une vidéo **bit-à-bit identique** (`new_full` vs
`ref_full` : SSIM 1,0 / PSNR infini) : les autres correctifs ne modifient pas le rendu GOAL. Les
coins de rotation sont désormais transparents (`c=none`) et les images fixes animées bouclent sur
toute leur durée.

Le temps de rendu augmente sur cet extrait parce que le correctif audio 3.1 ajoute la piste A2
du projet GOAL : `MSL1.mov` (ProRes 4444 XQ 512 Mo, piste silencieuse) est démuxé pour son PCM,
alors que l'ancienne version ignorait A2. Le graphe vidéo seul est inchangé. Sur l'export complet,
ce coût de lecture est noyé dans les 91 s de rendu ; il est le prix de l'exhaustivité audio
demandée par l'audit.

## Cache alpha inter-séquences (2026-10-23)

Chaque intermédiaire qtrle porte un sidecar `.qtrle.json` (empreinte de contenu, plage `cap`,
durée, source). Une séquence qui demande une plage plus courte réutilise l'intermédiaire le plus
court qui la couvre, et la bbox pleine déjà calculée est réutilisée. Le seuil
`PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX` (défaut 15 s) convertit en pleine durée quand la queue
inutilisée est courte.

Mesures sur le lot GOAL/frère (`MSL1.mov`, source 150 s, plages 91,5 et 102,5 s), cache neuf,
dry-run (préparation alpha + bbox) :

| Ordre / réglage | GOAL | Frère | Total | Cache |
| --- | --- | --- | --- | --- |
| GOAL → frère, défaut 15 s | 37,7 s | 41,5 s | 79,2 s | ~90 Mo (2 fichiers) |
| Frère → GOAL, défaut 15 s | **0,5 s** (réutilise 102,5 s + bbox pleine) | 43,8 s | **44,3 s** | ~46 Mo |
| GOAL → frère, seuil 60 s | 59,7 s (pleine 150 s) | **6,0 s** | **65,7 s** | ~67 Mo (1 fichier) |

Recommandation : pour un lot de séquences partageant un média, exporter la plus longue d'abord
(ou régler `PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX=60` dans `.env`). Le défaut 15 s reste prudent
pour un export isolé.

## Reproduire

```bash
# Benchmark (muxer null, 3 passages, cache alpha chaud après le dry-run)
python3 tools/benchmark_export.py \
  --prproj "WATCH_IN/Source/2026-10-23 12-01-13/Gulli - Ma Super Ligue 1 - GOAL_9x16_20261023_115105_ce321d.prproj" \
  --base /mnt/cache --seconds 15 --runs 3 --encoder h264_nvenc

# A/B avec une copie baseline du script
python3 tools/benchmark_export.py --prproj projet.prproj --base /mnt/cache \
  --script /tmp/prproj2ffmpeg_baseline.py --seconds 15 --runs 3

# Comparaison visuelle / métriques (extraits de 15 s)
ffmpeg -hide_banner -i ref.mp4 -i new.mp4 -lavfi "[0:v][1:v]ssim" -f null -
ffmpeg -hide_banner -i ref.mp4 -i new.mp4 -lavfi "[0:v][1:v]psnr" -f null -

# Mesurer la préparation alpha seule (dry-run, cache neuf)
XDG_CACHE_HOME=/tmp/cache-test python3 tools/prproj-unpacker/prproj2ffmpeg.py \
  projet.prproj --dry-run --base /mnt/cache

# Lot partageant un média : tolérer une queue plus longue avant la conversion pleine
PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX=60 python3 tools/benchmark_export.py \
  --prproj projet.prproj --base /mnt/cache --seconds 15 --runs 1
```

Pour isoler les filtres du bruit d'encodage, produire les extraits en lossless
(`-c:v libx264 -qp 0 -preset ultrafast -an`).

## Limites

- Seuls les overlays à transformation statique uniforme, sans rotation, avec masque non inversé
  sont optimisés.
- La préparation alpha reste le coût dominant à froid ; le cache qtrle et les sidecars évitent
  de la refaire à chaud et entre séquences.
- L'index ne couvre que les intermédiaires créés par cette version (les anciens restent trouvés
  par clé exacte/pleine durée). Le seuil de conversion pleine durée est un compromis : 15 s par
  défaut, 60 s conseillé pour un lot partageant un média.

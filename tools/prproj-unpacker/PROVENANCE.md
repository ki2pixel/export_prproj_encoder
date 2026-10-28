# Provenance — Prproj Tools (prproj2ffmpeg)

Copie vendorisée depuis le dépôt de conception de l'auteur (outil maison) :

- Source initiale : `/mnt/cache/134 Camille/Prproj-Unpacker-main/`
  (emplacement introuvable depuis le 2026-10-13 — dossier déplacé ou supprimé ;
  la copie ci-dessous fait référence tant que la source n'est pas remise en place).
- Copié le : 2026-10-13
- Empreintes SHA-256 :

| Fichier | SHA-256 | État |
| --- | --- | --- |
| `prproj2ffmpeg.py` | `fb840ffbf2e5edc2c5727871f593953d01c60c3bb9ae5c04967251f42bd8d941` | copie **+ correctifs + rendu masques/blur + optimisations perf Niv 1 & 2 + boîte alpha exacte des images fixes + préparation alpha bornée & cache (2026-10-19) + rendu fenêtre visible des overlays masqués (2026-10-23) + index de cache alpha inter-séquences (2026-10-23) + correctifs d'audit 1.1–3.4 (2026-10-23)** (voir ci-dessous) |
| `unpack.pl` | `e8aa19fccb148f86160eda3f3da0d5644cc7b435753010e0ef318e269997fbab` | copie conforme |
| `README.md` | `e3edd2434ce382030347ac097053daff2179a52d2c7c490e2d6c746e2ead3965` | copie **+ documentation des correctifs d'audit (2026-10-23)** |
| `LICENSE` | `09c65776ef866a5f0c184a5c52331807e954ba74c074b62e3617884ef864ad8b` | copie conforme |

Empreinte de l'original (avant modifications) :
`967affb14a5f6028b651814628ec99da3b91c7625b00208d5075339983a6c2b0`.

## Changements appliqués localement (2026-10-13 / 2026-10-19)

1. **Fenêtre de recadrage hors limites** — un item dont l'échelle ne remplit pas la séquence
   (source 16:9 dans une séquence 9:16 à ~101,6 % : `crop window 1063x1890 does not fit source
   1920x1080`) faisait échouer l'export complet. Le rendu bascule désormais sur le chemin
   composition (fond + image mise à l'échelle).
2. **Opacité scalaire** — l'opacité est stockée en `VideoComponentParam` scalaire (float 0-100)
   et non en point : l'accès `static[0]` provoquait un `TypeError` dès qu'un overlay portait une
   opacité. La valeur est lue et normalisée en 0..1 (`_opacity_scalar`).
3. **Rendu des masques `AE.ADBE AEMask2`** — rectangle (Type=2) et ellipse : décodage du blob
   « Path » binaire (en-tête 25 octets, points float32 normalisés), position, feather, opacité,
   expansion, inversion ; alpha appliqué via `alphamerge` (plage normalisée) avant le composant
   Motion. Les masques sont lus dans la chaîne du clip **et** dans les sous-composants d'un
   composant (cas des masques rattachés au composant Opacity, vérifié dans Premiere).
4. **Rendu du Gaussian Blur `AE.Impact_Blur_FX`** — `Amount` → `sigma` ffmpeg = `Amount × 3`
   (`_BLUR_SIGMA_FACTOR`, calibré visuellement : Amount 20 → sigma 60, rendu conforme au moniteur
   Premiere sur le projet de test). Flou appliqué par clip, les pistes supérieures restent nettes.
5. **`--dump` enrichi** — masques et flou listés par segment/overlay (vérification sans encodage).
6. **Cadrage hors limites → composition** (`crop_window_fits`) — un item dont le cadrage demandé
   sort des limites de la source (position bornée par le calcul de fenêtre) était rendu décalé
   (cas des split screens : ~250 px de décalage constaté sur le Mabille). Ces items passent
   désormais par le chemin composition, seul fidèle à la transformation demandée.
7. **Synchronisation des overlays (framesync)** — la composition d'une incrustation consomme la
   première image du flux secondaire avec l'image suivante quand les horodatages sont égaux
   (l'incrustation apparaissait une image trop tard) ; à l'inverse, un décalage d'une image
   complète la fait apparaître une image trop tôt. Le flux et la fenêtre d'activation sont donc
   avancés d'une **demi-image** : l'incrustation apparaît exactement à sa première image
   (vérifié sur les 3 clips masqués du Mabille). Cas image fixe : fenêtre seule.
8. **Optimisation des performances FFmpeg / NVENC (Niveau 1 & 2 - 2026-10-15)** :
   - Flou gaussien multi-échelle pyramidal (`blur_chain`) : pour les flous prononcés ($\sigma \ge 24$,
     ex. arrière-plans flous), décimation bilinéaire $1/4$ puis expansion bilinéaire ($1/16$ de la
     surface mémoire traitée) ; pour les flous moyens ($\sigma \ge 6$), décimation $1/2$ ; flou
     léger conservé en direct. Zéro dégradation visuelle, débit du filtre accéléré de +70% à +130%.
   - Remplacement de `flags=lanczos` par `flags=bicubic` dans `transform_filters` et le recadrage :
     supprime le surcoût inutile de hautes fréquences détruites en aval, gain mesuré de +46% sur les
     opérations de mise à l'échelle.
   - Encodage `h264_nvenc` basculé de `-preset p6` vers `-preset p4 -tune hq -bf 3 -b_ref_mode middle` :
     suppression des passes multipass redondantes de pré-analyse et de la contention CUDA, sans perte
     visible de qualité (écart VMAF < 0,15 à 10 Mbps).
   - Fusion des boucles d'incrustation identiques (`merge_loop_groups`) : les overlays sont triés par
     source et temps de début, permettant de fusionner les répétitions contiguës même si réparties sur
     différentes pistes par le monteur (élimination de plusieurs décodeurs vidéo et passes d'overlay).
   - Masques statiques mono-trame répétés dans `alphamerge` : les masques fixes sont générés en une
     unique trame (`d=1/fps`) répétée automatiquement par `framesync`, supprimant le calcul lourd
     de `drawbox/lutyuv` sur chaque image 1080p.
9. **Boîte alpha exacte des images fixes** (`alpha_bbox_exact`, 2026-10-16) — le recadrage des
   incrustations alpha (`bbox_crop_part`) s'appuyait sur `cropdetect`, dont le seuil `limit`
   s'applique à la **moyenne des lignes et colonnes de bord** (et non pixel à pixel, malgré la
   formulation d'usage) : l'élagage étant itératif, un artwork fin et creux (script, croix) était
   rogné bien au-delà de son contenu. Cas réel : `m6+.png` rendu en 144×86 au lieu de 168×87 et
   `pretty little liars.png` en 116×84 au lieu de 299×93 (logos amputés dans l'export). Les images
   fixes passent désormais par un balayage exact du plan alpha brut
   (`alphaextract,format=gray` + `rawvideo`, seuil `alpha > 2`, marge de 2 px inchangée, cache
   disque dédié `bboxpix1`) ; en cas d'échec de lecture, aucune boîte n'est renvoyée (donc aucun
   recadrage, jamais de découpe à tort). Les vidéos alpha conservent la détection `cropdetect`.
   Une ligne `[bbox] recadrage alpha de '<média>': WxH+X+Y` est journalisée par média recadré.
10. **Préparation alpha bornée et cache (2026-10-19)** :
    - **Plage utilisée** : l'intermédiaire qtrle et l'analyse `cropdetect` ne couvrent plus que
      le préfixe `[0, fin utilisée]` de chaque média, arrondi à 0,5 s (`_alpha_cap`). La fin est
      calculée avant les filtres pour la piste de base et les overlays (`_note_alpha_end`,
      fusion des boucles comprise). Le préfixe est conservé (pas de `-ss`) pour ne décaler aucun
      horodatage ; un intermédiaire pleine durée déjà en cache reste réutilisé, et l'analyse bbox
      est alors bornée à la plage utile. Cas réel `203 Camille` /
      `9_16_RDV_PROGRAMME_EM6_V02_5MIN.mov` : transcode 300 s → 61,5 s, **22,4 s au lieu de
      108,2 s**, bbox **3,6 s au lieu de 18,5 s**.
    - **Clé de cache de contenu** : en plus de la clé chemin + mtime, une empreinte légère
      (taille + premiers/derniers 4 Mio) reconnaît un même média copié dans un autre dossier
      projet ; les nouvelles entrées sont écrites sous cette clé de contenu.
    - **Cache négatif bbox** : une absence de recadrage est stockée `[]` et non `null` (l'ancien
      `null` était relu comme une absence de cache, ce qui relançait `cropdetect` à chaque export).
      L'ancien format reste lu comme absent et sera réécrit une fois.
    - **Purge LRU** : au-delà de `PRPROJ2FFMPEG_CACHE_MAX_MB` (défaut 2048 Mo, 0 = illimité), les
      entrées les moins récemment lues sont supprimées (mtime rafraîchi à chaque hit) ; les
      `.tmp.mov` orphelins de plus de 24 h sont toujours retirés.
    - **Observabilité** : lignes `[cache] conversion alpha … (0-61,5 s)` et
      `[bbox] analyse alpha …` émises dès le démarrage, avec durée réelle de conversion/analyse ;
      `summarize_line` les remonte déjà dans l'UI.
11. **Rendu sur fenêtre visible des overlays masqués (2026-10-23)** — un overlay à
    transformation statique (position/échelle fixes, rotation nulle) portant un masque est
    rastérisé sur la bbox transformée du masque, jamais sur le cadre entier. Le rectangle plein
    non inversé est entièrement opaque : `alphamerge` est supprimé. La fenêtre est arrondie à
    des dimensions paires pour yuv420p (repli RGBA si impossible). Les masques inversés,
    animés, tournés ou non uniformes conservent le chemin composition. Cas réel `Gulli - Ma
    Super Ligue 1 - GOAL_9x16` : `scale=4838:2722` + alphamerge → `crop=444:268:740:53` +
    `scale=1119:675`, overlay `(-22,-19)` ; **607,2 s → 34,8 s** (cache alpha chaud), sortie
    114 729 Ko contre 114 720 Ko, 2277 images. *(Valeurs corrigées par l'audit : fenêtre
    `444:267` et sortie `1119:673`, voir item 13.)* SSIM/PSNR 0-15 s : global 0,9874 / 35,45 dB,
    base 0,9944, zoom masqué 0,9754 / 31,23 dB. Le padding de la piste de base évalué en
    parallèle (crop-window hors cadre) a été écarté : SSIM 0,9464 / PSNR 29,67 dB lossless
    pour un gain de ~1,3x seulement.
12. **Index de cache alpha inter-séquences (2026-10-23)** — chaque intermédiaire qtrle reçoit
    un sidecar `.qtrle.json` (empreinte de contenu, plage convertie `cap`, durée, source).
    Une demande bornée réutilise l'intermédiaire **le plus court dont la plage couvre la
    demande** (ou un intermédiaire pleine durée), y compris via une copie du même média ; la
    bbox pleine déjà calculée est réutilisée pour une plage plus courte. Le seuil
    `PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX` (défaut 15 s) convertit en pleine durée quand la queue
    inutilisée est courte, pour alimenter les séquences suivantes. La purge LRU supprime les
    sidecars orphelins. Mesures sur le lot GOAL/frère (`MSL1.mov` 150 s, plages 91,5 et
    102,5 s) : ordre frère→GOAL 43,8 + 0,5 s = 44,3 s (contre 79,2 s, ~35 s gagnées) ;
    ordre GOAL→frère avec seuil 60 : 59,7 + 6,0 s = 65,7 s (contre 79,2 s), un seul
    intermédiaire pleine durée (cache 67 Mo contre 90 Mo).

13. **Correctifs d'audit (2026-10-23)** — application des 14 constats de
    `docs/audits/audit_complet_prproj2ffmpeg.md` :
    - **1.1 Z-order** : collecte des overlays par couche de piste (`collect_overlay_layers`),
      couche 0 = images fixes de V1, puis V2, V3… ; plus de tri alphabétique par chemin ni de
      `TypeError` sur `source_path` nul (base, overlays et audio filtrés).
    - **1.2 Flou** : `blur_chain(blur, width, height)` décime en dimensions paires
      (`trunc(iw/8)*2` ou `trunc(iw/4)*2`) et restaure la taille exacte ; sans dimensions connues
      (zoom animé), `gblur` direct pour ne jamais dériver.
    - **1.3 Boucles** : `merge_loop_groups(items, bases)` ne fusionne que les replays depuis 0 dont
      la durée égale la durée réelle du média (`probe_duration`, tolérance 0,1 s) ; masque, flou,
      keyframes de position/échelle/rotation et échelle non uniforme exclus.
    - **1.4 Pads** : `PullTable` déduplique par `(source, in, out)` mais recrée une pull si le pad
      vidéo/audio est déjà consommé ; une boucle recréée reste bouclée.
    - **1.5 Décompression** : `.prproj` gzip ou XML brut décompressé/parsé en mémoire
      (`Project(xml_path, xml_bytes=...)`) ; plus aucun `.prproj2ffmpeg-tmp.xml`.
    - **1.6 NVENC** : `_select_encoder` teste l'initialisation réelle sur une trame nulle 64x64 et
      replie sur `libx264` en journalisant `[encoder] … repli libx264` ; un encodeur forcé n'est
      jamais replié.
    - **2.1 Fenêtre visible** : `crop=…:exact=1`, `cx/cy` pairs pour les masques non simples, pas
      d'élargissement d'un rectangle plein, `out_w/out_h` impairs dans `odd_window`, flou exclu.
    - **2.2 Échelle uniforme** : lecture de « Uniform Scale » (`static_bool`), paramètres animés
      sans `StartKeyframe` acceptés, même chemin de keyframes pour les deux axes.
    - **2.3 Rotation** : `format=rgba,rotate=…:c=none` (coins transparents) ; images fixes à
      transformation animée lues en `-loop 1 -framerate …`.
    - **2.4 Piste de base** : masque généré sur une trame (`1/fps`), `format=yuva420p` autour de
      `gblur` avec masque, opacité appliquée en composition, ellipse non bornée avant calcul de
      son centre/rayons (`_ellipse_geometry`).
    - **3.1 Audio** : toutes les pistes non vides (`A1`, `A2`, `A3`…), concat par piste puis
      `amix` compensé `volume=N` ; labels globalement uniques.
    - **3.2 Médias** : profondeur de recherche complète (`range(0, len(parts))`) et cache
      positif/négatif `_resolve_cache`.
    - **3.3 Débit** : `-bufsize = 2 × débit` (`_bitrate_bps`), libx264 sans `-crf`.
    - **3.4 Cache** : purge LRU après construction du graphe avec pulls protégés, `.bbox.json`
      écrit atomiquement, nettoyage des `.bbox.json.tmp` de plus de 24 h, `axis_expr` en arbre
      équilibré.

**Validation** : W9 `mask_blur` (masque sur le 3ᵉ clip + flou sur le 4ᵉ) conforme aux captures
Premiere ; Mabille : 3 masques sur la piste V2, split screens cadrés et synchronisés (apparition à
la première image) ; projet 138 sans masque identique au rendu d'avant la fonctionnalité (SSIM :
0 frame différente sur 2323) ; W9 validé par l'utilisateur inchangé (0 frame sur 1701). Correctif
n° 9 : `/mnt/cache/183 Camille` (M6+ / Pretty Little Liars, 3 extraits 9:16) — boîtes exactes
`172x90+824+292` et `302x96+58+293` (au lieu de `148x90+830+292` et `120x88+150+294`), logos
complets sur les 3 exports de contrôle, et A/B ancien/nouveau comportement : PSNR 51,2 dB hors des
logos (aucune autre zone modifiée) contre SSIM 0,72 / 0,96 dans les zones restaurées. Tests
unitaires 35/35 validés.

**Validation 2026-10-19 (préparation alpha bornée + cache)** : projet `203 Camille` /
`M6_EM6_Short YTB pleurer de rire_TAT_M6_9x16` — dry-run à froid **32,1 s** (contre 132,4 s
avant), à chaud **0,85 s** ; **export complet à froid 45,7 s** (contre 143,8 s, soit ~98 s de
moins) et à chaud **15,0 s**. La sortie est **bit-à-bit identique** à la référence du même jour
(79 430 113 octets, framemd5 vidéo et audio identiques, PSNR infini). Tests unitaires 42/42.

**Validation 2026-10-23 (fenêtre visible des overlays masqués)** : projet `Gulli - Ma Super
Ligue 1 - GOAL_9x16` (job `20261023_115105_ce321d`) — export complet **34,8 s** à cache alpha
chaud (contre 607,2 s), 114 729 Ko contre 114 720 Ko, 2277 images ; extrait 0-15 s SSIM 0,9874 /
PSNR 35,45 dB (base 0,9944 ; zoom masqué 0,9754 / 31,23 dB). Le padding de la piste de base a été
écarté (SSIM 0,9464 / PSNR 29,67 dB lossless pour ~1,3x de gain). Tests unitaires 47/47.

**Validation 2026-10-23 (index de cache alpha)** : lot GOAL/frère, cache neuf — ordre
frère→GOAL : **43,8 s + 0,5 s** (réutilisation de l'intermédiaire 102,5 s et de sa bbox pleine)
contre 79,2 s ; ordre GOAL→frère avec `PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX=60` : **59,7 s + 6,0 s**
= 65,7 s, un seul intermédiaire pleine durée (67 Mo contre 90 Mo). Tests unitaires 51/51.

**Validation 2026-10-23 (correctifs d'audit)** : suite `unittest` **68/68**. Benchmark A/B GOAL,
extrait 15 s, `h264_nvenc`, cache alpha chaud : référence **6,68 s** (médiane, 2,24x) contre
**11,82 s** (1,27x) après correctifs — l'écart vient de l'ajout de la piste audio A2 (`MSL1.mov`,
ProRes 512 Mo silencieux) désormais mixée ; le graphe vidéo est inchangé au pixel près
(`crop=444:267:740:53:exact=1`, `scale=1119:673`). SSIM global **0,9715**, PSNR moyen **31,55 dB**
entre les deux fenêtres (réalignement vertical d'un pixel) ; par rapport à la composition pleine,
PSNR **35,97 dB** contre **35,45 dB** avant (SSIM 0,9835 contre 0,9874). Hors fenêtre, les deux
scripts sont bit-à-bit identiques (SSIM 1,0). `--dump` GOAL OK, `--dry-run` sans
`.prproj2ffmpeg-tmp.xml`.

## Procédure de synchronisation avec la source

Voir `UPSTREAM-SYNC.md` (état, procédure de génération du diff source → copie, liste des
changements, limites).

## Limites connues

- Masques : formes libres et chemins animés ignorés (rendu sans masque) ; blend modes autres que
  l'union (Add) et l'inversion non reproduits.
- Flou : isotrope (`gblur`) ; `Thickness`/`Angle` (flou directionnel) et aberration chromatique non
  reproduits.
- Fenêtre visible : seuls les overlays à transformation statique uniforme, sans rotation, avec
  masque non inversé sont optimisés ; le zoom est rééchantillonné depuis la bbox du masque, d'où
  un possible déphasage sous-pixel par rapport au scale plein cadre. La piste de base reste en
  composition dès que `crop_window_fits()` est faux.
- Préparation alpha : seule la **fin** de la plage utilisée est bornée ; un média utilisé
  uniquement tard dans sa source transcode encore tout son préfixe (`-ss` + remap non implémentés
  pour ne pas décaler les horodatages).
- Index alpha : les sidecars `.qtrle.json` ne couvrent que les intermédiaires créés après cette
  version ; les anciens restent trouvés par leur clé exacte/pleine durée, pas pour une autre
  plage. `PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX` (15 s par défaut) est un compromis : l'augmenter
  (ex. 60) accélère les lots partageant un média mais allonge le premier export.
- Cache disque : plafond `PRPROJ2FFMPEG_CACHE_MAX_MB` (défaut 2048 Mo, 0 = illimité) ; les
  entrées les moins récemment lues sont purgées au démarrage de l'export.

L'application invoque ce script en sous-processus via `PRPROJ2FFMPEG_SCRIPT` (voir `config.py`) —
ne jamais l'importer comme un module Python.

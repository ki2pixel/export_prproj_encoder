# Synchronisation avec la source d'origine

`prproj2ffmpeg.py` de ce dossier est **la copie de référence** : la source d'origine
(`/mnt/cache/134 Camille/Prproj-Unpacker-main/`, SHA-256
`967affb14a5f6028b651814628ec99da3b91c7625b00208d5075339983a6c2b0`) est introuvable depuis le
2026-10-13. C'est ce fichier que l'application exécute (`PRPROJ2FFMPEG_SCRIPT`).

## Procédure de synchronisation (quand la source sera retrouvée)

1. Comparer les empreintes : si la source retrouvée est bien l'original `967affb1…`, elle peut
   être comparée directement.
2. Générer le diff (depuis la racine du dépôt) :

   ```bash
   diff -u --label prproj2ffmpeg.py --label prproj2ffmpeg.py \
       <dossier-source>/prproj2ffmpeg.py \
       tools/prproj-unpacker/prproj2ffmpeg.py > UPSTREAM-FIXES.diff
   ```

3. Appliquer sur la source puis re-copier :

   ```bash
   cd <dossier-source>
   patch -p0 prproj2ffmpeg.py < UPSTREAM-FIXES.diff
   cp prproj2ffmpeg.py <dépôt>/tools/prproj-unpacker/prproj2ffmpeg.py
   ```

   (ou reporter les changements à la main, liste ci-dessous), puis mettre à jour `PROVENANCE.md`
   (nouvelle empreinte, état de synchronisation).

## Changements présents dans cette copie (par rapport à l'origine `967affb1…`)

1. **Fenêtre de recadrage hors limites** : un item dont l'échelle ne remplit pas la séquence
   (source 16:9 dans une séquence 9:16 à ~101,6 %) bascule vers le chemin composition au lieu de
   faire échouer l'export (`crop window … does not fit source …`).
2. **Opacité scalaire** : l'opacité est un paramètre scalaire (0-100) et non un point ; lecture
   robuste (`_opacity_scalar`) pour les overlays (auparavant `TypeError`).
3. **Rendu des masques `AE.ADBE AEMask2`** : rectangle et ellipse (décodage du « Path » binaire,
   position normalisée, feather, opacité, expansion, inversion). Les masques sont lus dans la
   chaîne du clip **et** dans les sous-composants (cas des masques rattachés au composant Opacity).
4. **Rendu du Gaussian Blur `AE.Impact_Blur_FX`** : `Amount` → `sigma` ffmpeg = `Amount × 3`
   (constante `_BLUR_SIGMA_FACTOR`, calibrée visuellement sur le projet de test : Amount 20 →
   sigma 60).
5. **`--dump` enrichi** : masques et flou listés par segment/overlay (outil de vérification).
6. **Cadrage hors limites → composition** (`crop_window_fits`) : un item dont le cadrage demandé
   sort des limites de la source n'est plus rendu par le chemin « fenêtre de crop » (qui bornait
   la position et décalait l'image, cas des split screens) mais par la composition.
7. **Synchronisation des overlays** : flux et fenêtre d'activation avancés d'une demi-image
   (sans quoi l'incrustation apparaît une image trop tard — ou trop tôt avec une image
   complète — à cause de la synchronisation framesync aux horodatages égaux).
8. **Optimisation des performances FFmpeg / NVENC (Niveau 1 & 2)** : flou gaussien pyramidal
   multi-échelle (décimation bilinéaire $1/4$ ou $1/2$ selon $\sigma$, gain mesuré de +70% à +130%
   sur le filtre), mise à l'échelle en `bicubic` au lieu de `lanczos` (+46% de débit), profil `h264_nvenc`
   en `-preset p4 -tune hq -bf 3 -b_ref_mode middle` (suppression de la contention multipass),
   regroupement des boucles contiguës multi-pistes (`merge_loop_groups`) et masques statiques mono-trame
   répétés dans `alphamerge` (gain global mesuré de 88,9s à 52,8s sur projet lourd).
9. **Boîte alpha exacte des images fixes** (`alpha_bbox_exact`) : `cropdetect` rogne selon la moyenne
   des lignes et colonnes de bord (son `limit` n'est pas un seuil par pixel) et sous-estimait donc la
   boîte des logos fins — `bbox_crop_part` recadrait l'incrustation au-delà de son contenu. Les images
   fixes utilisent désormais un balayage pixel à pixel du plan alpha brut (`alphaextract,format=gray`
   + `rawvideo`, seuil `alpha > 2`, marge de 2 px, cache `bboxpix1`) ; toute erreur de lecture renvoie
   « aucune boîte » (donc aucun recadrage). Les vidéos alpha gardent la détection heuristique.
10. **Préparation alpha bornée et cache (2026-10-19)** : l'intermédiaire qtrle et l'analyse
    `cropdetect` sont limités au préfixe réellement utilisé (`-t`, fin calculée sur la piste de
    base et les overlays, boucles fusionnées, arrondie à 0,5 s). Un intermédiaire pleine durée
    déjà en cache reste réutilisé, avec bbox bornée. Ajout d'une clé de cache de contenu
    (taille + premiers/derniers 4 Mio) pour reconnaître les copies, d'un cache négatif bbox
    `[]` (au lieu de `null`), d'une purge LRU (`PRPROJ2FFMPEG_CACHE_MAX_MB`, défaut 2048 Mo)
    et de lignes `[cache]`/`[bbox]` avec durée réelle.
11. **Rendu sur fenêtre visible des overlays masqués (2026-10-23)** : un overlay à
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
12. **Index de cache alpha inter-séquences (2026-10-23)** : sidecar `.qtrle.json` par
    intermédiaire (empreinte de contenu, plage `cap`, durée, source). Une demande bornée
    réutilise l'intermédiaire le plus court dont la plage couvre la demande, ou un
    intermédiaire pleine durée ; la bbox pleine déjà calculée sert pour une plage plus courte.
    Le seuil `PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX` (défaut 15 s) convertit en pleine durée quand
    la queue inutilisée est courte. La purge LRU retire les sidecars orphelins. Mesures sur
    `MSL1.mov` (150 s ; plages 91,5 et 102,5 s) : ordre frère→GOAL 43,8 + 0,5 s = 44,3 s
    contre 79,2 s ; ordre GOAL→frère avec seuil 60 : 59,7 + 6,0 s = 65,7 s, un seul
    intermédiaire (67 Mo contre 90 Mo).

13. **Correctifs d'audit (2026-10-23)** : application des 14 constats de
    `docs/audits/audit_complet_prproj2ffmpeg.md` — Z-order des couches (`collect_overlay_layers`,
    plus de tri par chemin), `blur_chain(blur, width, height)` à dimensions paires et restauration
    exacte, fusion de boucles limitée aux replays de durée pleine (`merge_loop_groups(items, bases)`),
    `PullTable` anti-collision de pads `[p:v]`/`[p:a]`, décompression `.prproj` en mémoire (aucun
    XML temporaire), préflight NVENC réel avec repli `libx264`, fenêtre visible `crop=…:exact=1`
    avec parité chroma et flou exclu, échelle uniforme (lecture de `Uniform Scale`), rotation
    `c=none` et images fixes animées bouclées, alpha/opacité/masque de la piste de base et
    ellipse non bornée, toutes les pistes audio mixées (`amix` compensé), résolution des médias à
    profondeur complète avec cache, `-bufsize = 2 × débit` et libx264 sans `-crf`, purge LRU après
    graphe avec pulls protégés, `.bbox.json` atomique et `axis_expr` équilibré.

## Limites connues de la copie actuelle

- Masques : seuls les types rectangle (`Type=2`) et ellipse sont rendus ; les formes libres et les
  chemins animés sont ignorés (rendu sans masque). Blend modes : union (Add) ; l'inversion est gérée.
- Flou : rendu isotrope (`gblur`) ; `Thickness`/`Angle` (flou directionnel) et l'aberration
  chromatique ne sont pas reproduits.
- Le masque d'un overlay n'est généré que sur sa durée d'affichage (optimisation) ; un overlay
  « bouclé » (rejeu consécutif identique) utilise la durée totale de la séquence.
- Recadrage des incrustations alpha : il est **exact** pour les images fixes ; pour les vidéos alpha
  (Moving Graphics), il reste heuristique (`cropdetect`, union des trames) et peut donc sous-estimer
  une boîte dont le contenu est fin — un média dont le recadrage échoue n'est jamais découpé, et une
  ligne `[bbox]` est journalisée à chaque recadrage retenu.
- Fenêtre visible : seuls les overlays à transformation statique uniforme, sans rotation, avec
  masque non inversé sont optimisés ; le zoom est rééchantillonné depuis la bbox du masque, d'où
  un possible déphasage sous-pixel par rapport au scale plein cadre. La piste de base reste en
  composition dès que `crop_window_fits()` est faux.
- Préparation alpha : seule la fin de la plage utilisée est bornée ; un média utilisé uniquement
  tard dans sa source transcode encore son préfixe (`-ss` + remap non implémentés pour préserver
  les horodatages). `PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX` (défaut 15 s) peut convertir en pleine
  durée pour alimenter les séquences suivantes ; l'augmenter accélère les lots mais allonge le
  premier export. Les intermédiaires antérieurs sans sidecar ne sont pas réutilisables pour une
  autre plage. Le cache disque est plafonné par `PRPROJ2FFMPEG_CACHE_MAX_MB` (défaut 2048 Mo,
  0 = illimité), purge LRU au démarrage.

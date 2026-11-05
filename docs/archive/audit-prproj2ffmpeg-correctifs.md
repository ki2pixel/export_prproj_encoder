# Correctifs de l'audit `docs/audits/audit_complet_prproj2ffmpeg.md`

## Décisions de cadrage

- **Périmètre** : les **14 constats** de l'audit (1.1 → 3.4), pas seulement les patchs A–F.
- **Validation** : suite `unittest` complète + benchmark A/B sur le projet GOAL
  (`WATCH_IN/Source/2026-10-23 12-01-13/Gulli - Ma Super Ligue 1 - GOAL_9x16_20261023_115105_ce321d.prproj`,
  `--base /mnt/cache`), avec extraits 15 s pour SSIM/PSNR. Pas d'export complet de contrôle.
- **Copie de référence** : `tools/prproj-unpacker/prproj2ffmpeg.py` reste la référence ; l'app
  continue de l'exécuter en sous-processus (jamais d'import). Documenter dans `PROVENANCE.md` et
  `UPSTREAM-SYNC.md`, mettre à jour l'empreinte SHA-256.
- **Contraintes** : Python 3.10+ stdlib, aucune dépendance nouvelle, ffmpeg ≥ 4.2, UI/logs en
  français, `pathlib` côté app (le script vendorisé garde son style `os.path`).

## Fichiers à modifier

| Fichier | Rôle |
| --- | --- |
| `tools/prproj-unpacker/prproj2ffmpeg.py` | L'essentiel des correctifs (2233 lignes) |
| `prproj_engine.py` | Statut NVENC réel dans `probe_ffmpeg()` + ligne `[encoder]` dans `summarize_line()` |
| `encoder_worker.py` | Commentaire/docstring du nettoyage `.prproj2ffmpeg-tmp.xml` (résidu d'anciennes versions) |
| `test_app.py` | Nouveaux tests et mise à jour des attentes de `visible_overlay_window` |
| `tools/prproj-unpacker/PROVENANCE.md` | Item 13 + SHA-256 + compte de tests |
| `tools/prproj-unpacker/UPSTREAM-SYNC.md` | Item 13 |
| `tools/prproj-unpacker/README.md` | Comportements corrigés (audio, Z-order, boucles, NVENC, débit, temp XML) |
| `docs/perf_export.md` | Nouvelle section métriques après benchmark + correction des valeurs GOAL |
| `docs/encoder_worker.md` | XML temporaire = résidu historique |
| `docs/app.md` | `probe_ffmpeg()` teste désormais l'initialisation NVENC |
| `docs/audits/audit_complet_prproj2ffmpeg.md` | Section « Suivi des correctifs » (traçabilité) |

---

## 1. Bugs critiques

### 1.1 Z-order des pistes d'incrustation + `source_path` nul

- Remplacer le tri global `overlays.sort(key=lambda it: (it.clip.source_path, it.start))` par une
  collecte **par couche de piste**.
- Nouvelle fonction module `collect_overlay_layers(parsed, video_tracks, base_track_items)` :
  - couche 0 = items `still` **avec** `clip.source_path` de `video_tracks[0]` ;
  - couches suivantes = tous les items avec `clip.source_path` de `video_tracks[1:]`, dans l'ordre
    des pistes (V2, V3, …), chacun déjà trié par `start` par `items_of_track()`.
- Dans `main()` : `base = [i for i in v1_items if i.clip and i.clip.source_path and not i.clip.still]`,
  `layers = collect_overlay_layers(...)`, `overlays = [it for layer in layers for it in layer]`.
- Fusion des boucles **par couche** : `overlay_groups = []` puis, pour chaque couche,
  `overlay_groups.extend(merge_loop_groups(layer, bases))`. L'ordre final est donc
  `(indice de piste, start)` sans tri supplémentaire ; il est utilisé tel quel par la boucle
  `plan` (les derniers composités passent au-dessus).
- Filtrer `clip.source_path` sur `base`, les overlays et l'audio : plus de `TypeError` sur `None`
  ni de `die("media not found for None")`.
- Réutiliser `v1_items` pour `base` et la couche 0 (évite un double parsing de la piste V1).

### 1.2 `blur_chain()` : dimensions paires et restauration exacte

- Nouvelle signature `blur_chain(blur, width=None, height=None)`.
- Décimation uniquement si `width`/`height` sont connus (sinon `gblur` direct, pour ne jamais
  dériver de 1–7 px quand la restoration dynamique `iw*4` ne retombe pas sur la taille exacte) :
  - `sigma >= 24` : `scale=w='max(2,trunc(iw/8)*2)':h='max(2,trunc(ih/8)*2)':flags=fast_bilinear`,
    `gblur=sigma=sigma/4:steps=2`, puis `scale={width}:{height}:flags=bilinear` ;
  - `sigma >= 6` : idem avec `trunc(iw/4)*2` / `trunc(ih/4)*2` et `sigma/2` ;
  - sinon `gblur=sigma=...:steps=2` (aucun `scale`).
- Mettre à jour les 4 appels :
  - piste de base, composition : `scale_dims(item, isw, ish)` si `has_animated_scale(item)` est
    faux, sinon `None` ;
  - piste de base, crop-window : `(seq_w, seq_h)` (le flou suit `scale=seq_w:seq_h`) ;
  - overlay, fenêtre visible : `(out_w, out_h)` ;
  - overlay, composition : `scale_dims(ov, ow, oh)` si échelle statique, sinon `None`.
- Ajouter `has_animated_scale(item)` et `has_animated_transform(item)` (échelle/scale2/rotation
  avec `kfs`) ; `has_animated_transform` sert aussi à 2.3.

### 1.3 `merge_loop_groups()` : tautologie et bouclage erroné

- Signature `merge_loop_groups(items, bases)`.
- `fits(item)` devient un vrai test :
  - refuser `item.clip.still`, `item.masks`, `item.blur`, et tout `position/scale/scale2/rotation`
    avec `kfs` (donc seulement des transformations statiques bouclables) ;
  - `cin < 1e-6` (rejeu depuis le début) ;
  - `resolved = resolve_media(item.clip.source_path, bases)` puis
    `full = probe_duration(resolved)` ; exiger `full > 1e-6` et
    `abs(cdur - full) <= 0.1` : `-stream_loop -1` boucle sur la **fin réelle du fichier**, donc un
    extrait partiel (`out_sec < durée`) ne doit plus être fusionné.
- `sig(item)` complète : `bool(position.kfs)`, `bool(scale.kfs)`, `bool(scale2.kfs)`,
  `bool(rotation.kfs)`, `bool(item.masks)`, `blur.amount`, `uniform_scale`.
- Conséquence : un intermédiaire alpha plus long que la plage utilisée ne permet plus la fusion
  (le probe porte sur le média résolu, et `fits` exige la durée pleine) ; les boucles partielles
  repassent par des `trim`/replays séparés, corrects.

### 1.4 Collisions d'étiquettes `[p:v]` / `[p:a]` dans `get_pull()`

- Extraire une classe module `PullTable` (testable) utilisée par le `get_pull` imbriqué de `main()` :
  - `pulls: list[(path, in_s, out_s, is_loop)]`, `_map: dict`, `_used: set[(idx, stream)]` ;
  - clé normale `(resolved, in_s, out_s)` ; clé `unique` `(resolved, in_s, out_s, len(pulls))` ;
  - clé boucle `(resolved, 0.0, span, 'loop')` ;
  - si la clé existe **et** que `(idx, stream)` est déjà consommé : nouvelle entrée
    (`(resolved, in_s, out_s, len(pulls))` pour une pull normale, **ou**
    `(resolved, 0.0, span, 'loop', len(pulls))` pour une boucle) — le correctif F de l'audit
    recréait une pull **non bouclée** dans ce cas, ce qui rejouait seulement le premier segment :
    ne pas reproduire ce défaut.
  - marquer `(idx, stream)` dans `_used` après chaque retour.
- `get_pull` conserve `resolve_media` + `alpha_intermediate` puis délègue à `PullTable.get`.
- Appels : `stream='v'` pour base/overlays, `stream='a'` pour l'audio ; `pulls = pull_table.pulls`.

### 1.5 Décompression `.prproj` en mémoire (zéro fichier temporaire)

- `Project.__init__(self, xml_path, xml_bytes=None)` : `ET.fromstring(xml_bytes)` si fourni, sinon
  `ET.parse(self.xml_path).getroot()`.
- `main()` : lire `raw` en binaire ; `gzip.decompress(raw)` seulement si `raw[:2] == b'\x1f\x8b'`,
  sinon parser les octets tels quels (XML brut). Plus aucun `.prproj2ffmpeg-tmp.xml`.
- Encadrer lecture/décompression/parsing par `try/except (OSError, gzip.BadGzipFile, EOFError,
  ET.ParseError)` → `die("lecture du projet impossible: ...")`.
- `encoder_worker.py` : garder `clean_input_leftovers()` (utile pour les résidus d'anciennes
  versions) mais mettre à jour le commentaire et la doc `docs/encoder_worker.md`.

### 1.6 Repli NVENC réel à l'exécution

- Extraire `_encoder_works(encoder, timeout=30)` :
  `ffmpeg -hide_banner -v error -f lavfi -i nullsrc=s=64x64:d=0.1 -frames:v 1 -pix_fmt yuv420p
  -c:v h264_nvenc -f null -` ; retourne `returncode == 0` (jamais d'exception).
- Extraire `_select_encoder(forced=None)` :
  - `forced` non vide et différent de `auto` → renvoyé tel quel (pas de repli, l'utilisateur a forcé) ;
  - sinon `ffmpeg -encoders` ; si `h264_nvenc` absent → `libx264` ;
  - si présent mais `_encoder_works('h264_nvenc')` faux → log
    `[encoder] h264_nvenc indisponible (GPU/session NVENC), repli libx264` sur `stderr` puis
    `libx264`.
- Dans `main()`, remplacer le bloc de sélection par `encoder = _select_encoder(args.encoder)`.
- `prproj_engine.probe_ffmpeg()` : même test runtime avec le binaire résolu
  (`_nvenc_session_ok(ffmpeg_path, timeout)`), pour que l'UI ne prétende plus NVENC disponible sur
  la seule présence de l'encodeur compilé. `summarize_line()` remonte les lignes `[encoder]` à l'UI.

---

## 2. Fidélité visuelle et mathématique

### 2.1 `visible_overlay_window()` : parité, `crop exact`, flou

- Retourner `None` si `item.blur is not None` (le crop au ras du masque couperait le flou).
- `cx = max(0, min(floor(mx0), w_int - 2))`, idem `cy` ; si le masque n'est **pas** un rectangle
  plein opaque (`_is_simple_opaque_rect_mask`), faire `cx &= ~1`, `cy &= ~1`.
- Calculer `cw/ch` après l'alignement ; n'augmenter de 1 px que pour les masques non simples
  (un rectangle plein ne doit pas afficher 1 px source hors masque).
- `ow = max(2, round(cw*sx))`, `oh = max(2, round(ch*sy))`.
- Dans `main()` (branche fenêtre) :
  - `crop_src = f"crop={cw}:{ch}:{cx}:{cy}:exact=1"` (même chose pour le crop du masque) ;
  - `odd_window = (cx|cy|cw|ch|out_w|out_h) & 1` → `needs_alpha` si impair (en plus de
    `needs_mask`, `source_has_alpha`, opacité < 0.999) ;
  - condition d'entrée : `bbox_part is None and ov.masks and ov.blur is None`.
- Mettre à jour le test GOAL : la fenêtre devient `(740, 53, 444, 267, -22, -19, 1119, 673)`
  (au lieu de `(740, 53, 444, 268, -22, -19, 1119, 675)`) ; `out_h` impair impose le repli RGBA.
- `bbox_crop_part()` : retourner `(None, 0, 0)` si `item.blur is not None` (même raison : le crop
  alpha couperait le flou). Mettre à jour la docstring.

### 2.2 Échelle uniforme (`AE.ADBE Motion`)

- `Param.__slots__` : ajouter `static_bool`, lu par un helper `_start_bool(el)` qui renvoie
  `True/False` seulement si le `StartKeyframe` contient `true`/`false`, sinon `None`.
- `TrackItem.__init__` : `self.uniform_scale = True` (défaut Premiere).
- `_read_components()` :
  - helper `param_active(p) = p.static is not None or bool(p.kfs)` ; accepter `position`, `scale`,
    `scale2`, `rotation` dès qu'actifs (un paramètre animé sans `StartKeyframe` n'est plus ignoré) ;
  - ne mettre à jour `scale_w/scale_h` que si `static is not None` ;
  - `if len(params) > 3 and params[3].static_bool is not None: item.uniform_scale = params[3].static_bool`.
- `scale_expr_pair()` : si `uniform_scale` et une seule des deux échelles est animée, réutiliser le
  même `build_path` pour les deux axes (le `h_expr` garde `src_h/100`, le `w_expr` `src_w/100`) ;
  l'`anim` retourné est vrai. Si les deux sont animées, garder les deux chemins.
- Vérifier les chemins dérivés : `item_needs_canvas`, `crop_window_fits`,
  `transform_is_identity`, `_static_uniform_transform` testent déjà `scale.kfs`/`scale2.kfs`, donc
  un zoom uniforme animé passe bien par la composition.

### 2.3 Rotation : coins transparents et images fixes animées

- `transform_filters()` : quand `rot` existe, produire
  `format=rgba,rotate=a='{rot}':c=none` (le `format=rgba` doit précéder `rotate`). Sans rotation,
  aucun changement.
- Images fixes animées : dans la collecte `image_inputs`, stocker `(path, has_animated_transform(ov))`.
  À la construction de la commande :
  - animée → `-loop 1 -framerate {fps:.6f} -i path` (flux infini, coupé par le `-t total` final) ;
  - statique → `-i path` (comportement actuel, une seule trame répétée par `overlay`).
- Les expressions `scale`/`rotate` utilisent déjà le temps absolu (`transform_filters(ov, ..., 0.0,
  total)`), donc les keyframes sont évaluées correctement sur le flux bouclé.
- `_static_uniform_transform`/`visible_overlay_window` refusent déjà les transformations animées :
  une image fixe animée reste sur le chemin composition.

### 2.4 Piste de base en composition : alpha, opacité, masque, ellipse

- Masque de la piste de base : passer `1.0 / fps` à `mask_chain()` (au lieu de `cout - cin`), comme
  pour les overlays ; `alphamerge`/framesync répète la trame unique.
- Flou + masque : insérer `format=yuva420p` **avant** `gblur` et `format=rgba` **après** (la
  négociation par défaut choisit `yuv420p` et détruit l'alpha). Faire de même dans la branche
  overlay quand `ov.masks` et `ov.blur` coexistent (la branche fenêtre est déjà exclue par 2.1).
- Opacité de la piste de base : `item_needs_canvas()` renvoie vrai si
  `_opacity_scalar(item.opacity)` est défini et `< 0.999` ; ajouter dans le `tail` de composition
  `format=rgba,colorchannelmixer=aa=<valeur>` (après le crop/bbox). L'opacité n'est donc plus
  ignorée, et le chemin crop-window (opaque) n'est plus utilisé pour ces items.
- `_mask_bbox_px(mask, w, h, clamp=True)` : nouveau paramètre. Pour un rectangle, garder le
  bornage actuel. Pour une **ellipse**, `_single_mask_chain()` utilise `clamp=False` via un helper
  `_ellipse_geometry(mask, w, h)` (centre/rayons calculés sur la bbox non bornée, puis `geq` borne
  naturellement au cadre). `_mask_union_bbox_px()` continue d'utiliser `clamp=True` (la fenêtre
  visible doit rester dans la source).

---

## 3. Fiabilisation / performance

### 3.1 Audio : toutes les pistes (`A1`, `A2`, `A3`…)

- Helper module `collect_audio_tracks(parsed, audio_track_elements)` : pour chaque piste, garder les
  items `i.clip and i.clip.source_path` ; ne conserver que les pistes non vides.
- Remplacer l'accès `seq['tracks']['audio'][0]` par cette liste.
- Construire une chaîne concaténée par piste (`[aout_0]`, `[aout_1]`, …) :
  - labels uniques globalement : `[ar{t}_{run}_{j}]` (asplit), `[ag{m}]`/`[a{m}]` avec `m` global ;
  - `get_pull(..., stream='a')` (corrige aussi 1.4) ;
  - `concat=n=...:v=0:a=1` par piste.
- Si une seule piste : sa sortie s'appelle directement `[aout]`. Si plusieurs :
  `amix=inputs=N:duration=longest:dropout_transition=0,volume=N[aout]` (compense la normalisation
  `1/N` d'`amix`).
- `has_audio = bool(audio_tracks)` remplace `if audio_items:` pour `-map [aout]` et les options
  `-c:a aac -b:a 320k -ar 48000 -ac 2`.
- La durée totale (`total`) reste le maximum vidéo/overlays : la piste audio est coupée par
  `-t total` (comportement actuel, pas de changement de durée de sortie).

### 3.2 `resolve_media()` : profondeur complète + cache

- `_resolve_cache = {}` module ; clé `(win_path, tuple(bases))`.
- Boucle `for skip in range(0, len(parts))` (au lieu de `min(3, len(parts))`) ; mémoriser le chemin
  trouvé **et** `None` (cache négatif).
- Conserver l'ordre `base` → `skip` (du plus spécifique au nom de fichier).

### 3.3 Encodage `libx264` : plus de `-crf`, `-bufsize` proportionnel

- Helper `_bitrate_bps(value)` (même grammaire que `parse_bitrate` de `prproj_engine`).
- `bufsize = str(2 * bps)` si `bps > 0`, sinon `20M` ; passer
  `-b:v <bitrate> -maxrate <bitrate> -bufsize <bufsize>`.
- Branche `libx264` : `-preset medium` **sans** `-crf 18` (le CRF écrasait `-b:v` et faussait
  l'estimation de progression de `encoder_worker.py`). La branche NVENC est inchangée.

### 3.4 Cache : purge après préparation, `.bbox.json` atomique, `axis_expr` équilibré

- Déplacer `_prune_cache()` **après** la construction complète du graphe (après tous les
  `alpha_intermediate`/`alpha_bbox`) et lui ajouter `protect=()` :
  - `_prune_cache(protect=())` convertit en `set` d'abspaths et saute ces fichiers dans la purge
    LRU ;
  - appel : `protect = abspaths(pulls) | abspaths(image_inputs)` ;
  - ajouter aussi le nettoyage des `.bbox.json.tmp` orphelins de plus de 24 h (comme `.tmp.mov`).
- `_bbox_cache_write()` : écrire dans `fp + '.tmp'` puis `os.replace(tmp, fp)` ; supprimer le tmp en
  cas d'échec (même schéma que `_write_alpha_meta`).
- `axis_expr()` : remplacer la chaîne linéaire par un arbre binaire équilibré :
  ```python
  def build(lo, hi):
      if lo == hi:
          return poly_expr(folded[lo])
      mid = (lo + hi) // 2
      return (f"if(lt(t,{intervals[mid][1]:.6f}),"
              f"{build(lo, mid)},{build(mid + 1, hi)})")
  ```
  Profondeur `ceil(log2(N))` au lieu de `N` ; sémantique identique aux bornes (`t == fin` va à
  droite). Retourner `'0.0'` si `folded` est vide.

---

## 4. Tests (`test_app.py`)

Ajouter/mettre à jour les tests (le module vendorisé est chargé par `load_prproj2ffmpeg()`, donc
caches et globals sont neufs à chaque test) :

| Test | Vérifie |
| --- | --- |
| `test_project_parse_en_memoire` | `Project(FIXTURE, xml_bytes=gzip.decompress(raw))` parse les séquences ; aucun `.prproj2ffmpeg-tmp.xml` créé |
| `test_resolve_media_profondeur_et_cache` | chemin `F:\Clients\2026\134 Camille\rush\video.mp4` résolu à `skip=3` ; second appel servi par `_resolve_cache` |
| `test_merge_loop_groups_duree_pleine` | deux replays complets et contigus fusionnent ; un extrait partiel, un masque, un flou ou des keyframes ne fusionnent pas (patch `resolve_media`/`probe_duration`) |
| `test_pull_table_collisions` | deux usages vidéo de la même clé → 2 pulls ; un usage vidéo + audio → même pull ; deux boucles → 2 pulls **toujours bouclées** ; `unique` → pulls distincts |
| `test_blur_chain_dimensions` | `sigma>=24`/`>=6` → `trunc(iw/8)*2`/`trunc(iw/4)*2` + restauration exacte `scale=W:H` ; dims inconnues → `gblur` seul |
| `test_visible_overlay_window_masque_rectangulaire` | nouvelle attente `(740,53,444,267,-22,-19,1119,673)` ; pas de +1 px |
| `test_visible_overlay_window_replis` | ajouter `blur is not None` → `None` ; masque avec feather → `cx/cy/cw/ch` pairs |
| `test_uniform_scale_axes` | `scale.kfs` + `scale2` statique + `uniform_scale=True` → `h_expr` animé (même chemin que `w_expr`) ; `uniform_scale=False` → `h_expr` constant |
| `test_transform_filters_rotation_rgba` | rotation → `format=rgba` avant `rotate` et `c=none` ; sans rotation → aucun `format=rgba` ajouté |
| `test_axis_expr_equilibre` | évaluer l'expression avec `eval` (`if(`→`iff(`, `lt`) sur 16 intervalles et vérifier les valeurs aux bornes/milieux ; profondeur logarithmique |
| `test_bbox_cache_write_atomique` | aucun `.tmp` résiduel après écriture ; échec simulé → tmp supprimé |
| `test_prune_cache_protege_les_pulls` | un fichier protégé (le plus ancien) survit à une purge LRU qui l'aurait supprimé |
| `test_ellipse_geometry_hors_cadre` | ellipse débordante : centre/rayons calculés sans bornage, `geq` couvre la partie visible |
| `test_item_needs_canvas_opacite` | opacité 50 % → composition ; 100 % → chemin crop-window |
| `test_select_encoder` | `_encoders` sans nvenc → libx264 ; nvenc + preflight OK → nvenc ; preflight KO → libx264 + ligne `[encoder]` ; `--encoder h264_nvenc` forcé → renvoyé tel quel |
| `test_read_components_param_anime` | `Param` avec `Keyframes` mais sans `StartKeyframe` → `static is None`, `kfs` non vide ; `static_bool` lit `true/false` |
| `test_collect_overlay_layers` | V1 stills en couche 0, puis V2, V3… ; items sans `source_path` ignorés |
| `test_collect_audio_tracks` | A1/A2 non vides conservées, piste vide ignorée |
| `test_summarize_line` | ligne `[encoder] … repli libx264` remontée à l'UI |
| `test_probe_ffmpeg_nvenc_runtime` | faux ffmpeg dont le preflight échoue → `has_nvenc=False` |

Adapter les faux `ffmpeg` existants (`test_probe_ffmpeg_*`) pour distinguer `-version`, `-encoders`
et la commande preflight `nullsrc`.

---

## 5. Validation

### 5.1 Tests unitaires

```bash
./.venv/bin/python -m unittest discover -v
```

Exécuter aussi les tests ciblés pendant le développement :

```bash
./.venv/bin/python -m unittest test_app.TestAMEWatchfolder.test_pull_table_collisions -v
./.venv/bin/python -m unittest test_app.TestAMEWatchfolder.test_merge_loop_groups_duree_pleine -v
```

### 5.2 Benchmark A/B GOAL

Avant toute modification, copier la référence pour l'A/B :

```bash
cp tools/prproj-unpacker/prproj2ffmpeg.py "$COMMANDCODE_SCRATCHPAD/prproj2ffmpeg_baseline.py"
```

Puis, après implémentation (cache alpha chaud après le premier dry-run) :

```bash
PROJ="WATCH_IN/Source/2026-10-23 12-01-13/Gulli - Ma Super Ligue 1 - GOAL_9x16_20261023_115105_ce321d.prproj"

# Référence
python3 tools/benchmark_export.py --prproj "$PROJ" --base /mnt/cache \
  --seconds 15 --runs 3 --encoder h264_nvenc \
  --script "$COMMANDCODE_SCRATCHPAD/prproj2ffmpeg_baseline.py" \
  --out-dir "$COMMANDCODE_SCRATCHPAD/ref"

# Nouvelle version
python3 tools/benchmark_export.py --prproj "$PROJ" --base /mnt/cache \
  --seconds 15 --runs 3 --encoder h264_nvenc \
  --out-dir "$COMMANDCODE_SCRATCHPAD/new"

# Fidélité (extraits 15 s, même encodeur/débit)
ffmpeg -hide_banner -i "$COMMANDCODE_SCRATCHPAD/ref/benchmark_15s.mp4" \
  -i "$COMMANDCODE_SCRATCHPAD/new/benchmark_15s.mp4" -lavfi "[0:v][1:v]ssim" -f null -
ffmpeg -hide_banner -i "$COMMANDCODE_SCRATCHPAD/ref/benchmark_15s.mp4" \
  -i "$COMMANDCODE_SCRATCHPAD/new/benchmark_15s.mp4" -lavfi "[0:v][1:v]psnr" -f null -
```

Vérifier aussi le `--dry-run` (commande générée, fenêtre `crop=444:267:740:53` + `exact=1`,
`scale=1119:673`, audio multi-pistes, absence de `.prproj2ffmpeg-tmp.xml` dans le dossier du
projet) et un `--dump` sur la fixture.

### 5.3 Documentation

- Reporter les métriques A/B (médiane, SSIM/PSNR) dans `docs/perf_export.md` ; corriger les
  valeurs GOAL de la section « Correctif retenu » (444×267 / 1119×673 au lieu de 444×268 /
  1119×675) et expliquer que `exact=1` + parité `cx/cy` supprime le décalage chroma.
- `PROVENANCE.md` : item 13 « Correctifs d'audit 2026-10-23 » (liste 1.1–3.4), nouvelle empreinte
  SHA-256 de `prproj2ffmpeg.py` (`sha256sum`), validation et compte de tests réel.
- `UPSTREAM-SYNC.md` : item 13 avec la même liste.
- `README.md` du script : audio toutes pistes mixées, Z-order respecté, boucles seulement sur
  durée pleine, repli NVENC runtime, `-bufsize = 2×débit` et libx264 sans CRF, absence de fichier
  temporaire, résolution profonde des médias.
- `docs/audits/audit_complet_prproj2ffmpeg.md` : section finale « 5. Suivi des correctifs » avec un
  tableau constat → fonction/commit → test.

## 6. Risques et points de vigilance

- **Changements visuels assumés** (2.2/2.3/2.4) : comparer les extraits GOAL et, si possible, un
  projet avec rotation/masque (W9 `mask_blur`, Mabille) avant de considérer le rendu validé.
- **Audio multi-pistes** : `amix` normalise puis `volume=N` ; vérifier qu'aucune piste n'est
  dupliquée (`asplit` par run) et que les labels restent uniques entre pistes.
- **NVENC preflight** : un GPU occupé peut faire échouer le test et déclencher le repli libx264 ;
  c'est volontairement conservateur (l'export reste correct, plus lent). Un `--encoder` forcé ne
  replie jamais.
- **`axis_expr`** : l'arbre équilibré doit rester sémantiquement identique ; le test par `eval`
  couvre les bornes et les milieux.
- **`blur_chain`** : ne décimer que si les dimensions cibles sont connues ; sinon `gblur` direct
  (léger coût CPU pour les rares zooms animés flous, mais aucune dérive de taille).
- **Fusion des boucles** : plus conservative qu'avant (durée pleine uniquement) ; certains exports
  peuvent créer plus d'entrées ffmpeg, sans changer le rendu.

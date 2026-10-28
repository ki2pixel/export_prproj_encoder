L'audit complet de `tools/prproj-unpacker/prproj2ffmpeg.py` met en évidence **14 constats** (6 bugs critiques/fonctionnels, 4 problèmes de fidélité visuelle et mathématique, et 4 axes de fiabilisation/performance).

---

## 1. Bugs critiques et risques de crash FFmpeg

### 1.1. Destruction du Z-order (ordre d'empilement des pistes d'incrustation)

* **Localisation** : `main()` (`overlays.sort(key=lambda it: (it.clip.source_path, it.start))`).


* **Problème** : Toutes les incrustations (`still` de `V1`, puis tous les clips de `V2`, `V3`, `V4`…) sont fusionnées dans une unique liste `overlays`, puis triées **par ordre alphabétique de chemin source** avant d'être compositées.


1. Si `V2` contient un bandeau `z_bandeau.png` et `V3` contient un logo `a_logo.png` au même instant, `'a_logo.png' < 'z_bandeau.png'` : le logo de `V3` est rendu **en dessous** du bandeau de `V2` (l'inverse de Premiere).
2. Si un `TrackItem` a `source_path is None`, le tri lève immédiatement `TypeError: '<' not supported between instances of 'NoneType' and 'str'`.




* **Correction** : Conserver l'indice de piste `(track_idx, item)` lors de la collecte. Pour fusionner les boucles (`merge_loop_groups`), regrouper les répétitions contiguës d'une même source sans inverser l'ordre vertical d'empilement des groupes résultants (trier les groupes finaux par `(min_track_idx, group[0].start)`).

### 1.2. Crash FFmpeg dans `blur_chain()` sur les dimensions non divisibles par 8

* **Localisation** : `blur_chain(blur)`.


* **Problème** : Pour $\sigma \ge 24$, le filtre généré est `scale=iw/4:ih/4:flags=fast_bilinear,gblur=...,scale=iw*4:ih*4:flags=bilinear` (et `iw/2:ih/2` pour $\sigma \ge 6$).
Or `blur_chain` est placé **après** `tf` (`transform_filters`) :


1. Si un clip `1920x1080` en `yuv420p` a une échelle de `110 %`, `130 %`, `150 %`, `170 %` ou `190 %`, sa hauteur mise à l'échelle (`1188`, `1404`, `1620`, `1836`, `2052`) divisée par 4 donne un **entier impair** (`297`, `351`, `405`, `459`, `513`). Sur un flux `yuv420p`, `scale=iw/4:ih/4` fait planter FFmpeg immédiatement (`height not divisible by 2`).
2. Si la dimension avant flou n'est pas un multiple exact de 4 (ex. `1242 px`), `int(1242 / 4) * 4 = 1240 != 1242` : le filtre modifie la taille du flux, ce qui décale l'overlay ou fait planter le `bbox_crop_part` placé juste après.




* **Correction** : Utiliser des dimensions paires explicites à la décimation (`trunc(iw/8)*2:trunc(ih/8)*2` pour $1/4$, `trunc(iw/4)*2:trunc(ih/4)*2` pour $1/2$) et restaurer la résolution exacte attendue en sortie de flou.

### 1.3. Condition tautologique dans `merge_loop_groups()` et bouclage erroné

* **Localisation** : `merge_loop_groups(items)` → `fits(item)` et `sig(item)`.


* **Problème** :
```python
def fits(item):
    cin, cdur = clip_times(item)
    avail = item.clip.out_sec - item.clip.in_sec
    return cdur <= avail + 1e-6

```


1. Par définition de `clip_times(item)` et `Clip.duration()`, `cdur` **est égal à** `item.clip.out_sec - item.clip.in_sec`. La condition `cdur <= avail + 1e-6` est donc **toujours vraie** (tautologie).


2. Dans FFmpeg, `-stream_loop -1 -t {span} -i {path}` boucle à la **fin du fichier média sur disque**, et non à `item.clip.out_sec`. Si un monteur coupe les 3 premières secondes (`in=0, out=3s`) d'un média de 60 s et répète cette coupe deux fois de suite, `merge_loop_groups` les fusionne et FFmpeg lit les secondes `0..6s` du fichier au lieu de rejouer `0..3s` deux fois. De même, si `_find_alpha_intermediate` réutilise un intermédiaire `.mov` en cache plus long que `out_sec` (ex. `102,5 s`), `-stream_loop -1` lit la suite de l'intermédiaire au lieu de boucler à `out_sec`.


3. `sig(item)` ne vérifie ni `item.position.kfs`, ni `item.masks`, ni `item.blur` : deux segments contigus dont seul le second porte un masque, un flou ou des keyframes de position sont fusionnés à tort.




* **Correction** : Dans `fits(item)`, vérifier que `cdur` correspond à la durée réelle du fichier source (`abs(cdur - probe_duration(resolved)) <= 0.1`) **et** ne pas réutiliser un intermédiaire alpha de durée différente pour un pull `loop` (ou boucler via le filtre `loop` après `trim`). Ajouter `bool(item.position and item.position.kfs)`, `item.masks` et `item.blur` dans `sig(item)` (ou exclure de la fusion tout item portant des keyframes/masques/flou).

### 1.4. Collision d'étiquettes FFmpeg dans `get_pull()` (`unique=False`)

* **Localisation** : `get_pull(path, in_s, out_s, unique=False, ...)`.


* **Problème** : Pour la piste de base et la piste audio, `get_pull` est appelé avec `unique=False`, ce qui déduplique les entrées par clé `(resolved, round(in_s, 6), round(out_s, 6))`.
Si un même extrait `(path, in_s, out_s)` apparaît **deux fois non contiguës** dans la timeline sur la piste de base (ex. un plan de coupe repris plus loin avec les mêmes points In/Out) ou deux fois sur la piste audio (ex. un jingle/bruitage répété), `get_pull` renvoie le **même indice `p**` aux deux `run`. Le graphe `-filter_complex` référence alors `[{p}:v]` (ou `[{p}:a]`) à deux endroits sans `split`/`asplit`, ce qui fait échouer FFmpeg immédiatement (`Stream specifier ... matches no streams` / pad déjà consommé).


* **Correction** : Suivre dans deux ensembles (`used_v`, `used_a`) si le flux vidéo ou audio d'un pull `p` a déjà été consommé ; si oui, forcer la création d'un nouveau pull (ou insérer un `split`/`asplit` en amont).

### 1.5. Fichier temporaire `.prproj2ffmpeg-tmp.xml` non isolé et écrit sur le dossier source

* **Localisation** : `main()` (décompression `.prproj`).


* **Problème** : Le script décompresse le `.prproj` dans `os.path.join(os.path.dirname(src), '.prproj2ffmpeg-tmp.xml')`.


1. Lors d'un appel direct sur `/mnt/cache/.../projets/mon_projet.prproj` (ex. via `benchmark_export.py` ou CLI), il écrit sur le montage NTFS `/mnt/cache` (contrairement à la règle d'architecture du dépôt qui interdit toute écriture temporaire sur `/mnt/cache`).


2. Le nom étant fixe par dossier, deux exécutions concurrentes dans le même dossier (ex. `--dump` et un autre process) entrent en collision (`FileNotFoundError` au `os.unlink(tmp)`).




* **Correction** : Parser directement les octets décompressés en mémoire avec `ET.fromstring(xml_bytes)` (sans aucun fichier temporaire sur disque) et vérifier la signature GZIP `\x1f\x8b` pour accepter aussi les `.prproj` déjà enregistrés en XML brut.



### 1.6. Fausse détection de `h264_nvenc` sans repli réel à l'exécution

* **Localisation** : `main()` (`ffmpeg -hide_banner -encoders`).


* **Problème** : `'h264_nvenc' in enc_out` teste uniquement si le binaire `ffmpeg` a été **compilé** avec le wrapper NVENC (ce qui est toujours vrai sur les paquets Ubuntu/Debian standards), et non si un GPU NVIDIA est accessible ou si une session NVENC est libre au runtime. Si l'initialisation NVENC échoue au lancement de `ffmpeg`, le script appelle `die()` sans tenter le repli sur `libx264` promis par la documentation.


* **Correction** : Si `args.encoder` n'a pas été forcé et que l'exécution `ffmpeg` avec `h264_nvenc` échoue (`rc != 0`), relancer automatiquement avec `libx264` (ou tester l'encodeur sur une trame nulle 64x64 `-f lavfi -i nullsrc=s=64x64 -frames:v 1 -c:v h264_nvenc -f null -`).

---

## 2. Fidélité visuelle et exactitude mathématique

### 2.1. Fenêtre visible (`visible_overlay_window`) : parité post-scale, décalage chroma `crop` et flou

* **Localisation** : `visible_overlay_window()`, `main()` (branche `if window is not None:`).


* **Problème** :
1. **Dimensions impaires après `scale**` : La variable `odd_window = bool((cw & 1) or (ch & 1))` teste la parité de la fenêtre **avant** `scale`, mais c'est la fenêtre **après** `scale` (`out_w`, `out_h`) qui est convertie en `format=yuv420p` à la fin de `post` (`scale={out_w}:{out_h},...,format=yuv420p`). Dans le cas `GOAL` documenté dans `docs/perf_export.md`, `scale=1119:675` produit des dimensions impaires suivies de `format=yuv420p`.


2. **Troncature silencieuse de `cx`/`cy` impairs par `crop` en `yuv420p**` : En `yuv420p`, le filtre `crop=cw:ch:cx:cy` de FFmpeg utilise par défaut `exact=0`, ce qui **force silencieusement `cx` et `cy` à l'entier pair inférieur** (`cy = 53` devient `cy = 52` dans le cas `GOAL` !) sans que `ox`/`oy` en tienne compte, créant un décalage de $1 \times \text{scale}$ pixels (soit $2{,}52\text{ px}$ à $252\text{ \%}$ de zoom). C'est la cause principale de la chute de PSNR/SSIM notée dans `docs/perf_export.md` !


3. **Élargissement `cw += 1` / `ch += 1` sur rectangle plein** : Si `simple_rect` est vrai, il n'y a pas d'`alphamerge` ; incrémenter `cw`/`ch` de 1 px affiche 1 px source supplémentaire hors du masque.
4. **Combinaison masque + `ov.blur**` : Si un overlay masqué porte aussi un flou (`ov.blur`), `crop_src` coupe au ras du masque sans marge pour le rayon de flou $3 \times \text{Amount}$, et si `simple_rect` est vrai, le flou ne bave pas dans l'alpha.




* **Correction** :
* Désactiver le raccourci `visible_overlay_window` lorsque `ov.blur is not None`.
* Aligner `cx` et `cy` sur des entiers pairs (`cx &= ~1`, `cy &= ~1`) avant de calculer `cw, ch, ox, oy`, **ou** passer `:exact=1` au filtre `crop` (`crop={cw}:{ch}:{cx}:{cy}:exact=1`).
* Arrondir `out_w` et `out_h` à l'entier pair le plus proche (`max(2, int(round(cw * sx / 2.0)) * 2)`), ou inclure `(out_w & 1) or (out_h & 1)` dans `odd_window`.



### 2.2. Zoom uniforme animé (`AE.ADBE Motion` : `scale` vs `scale2`)

* **Localisation** : `_read_components()` et `scale_expr_pair()`.


* **Problème** : Dans Premiere Pro, lorsque la case « Échelle uniforme » (`Uniform Scale`, paramètre 3 de `AE.ADBE Motion`) est cochée (comportement par défaut), les keyframes de zoom sont enregistrées uniquement sur `params[1]` (`item.scale`), tandis que `params[2]` (`item.scale2`, largeur) reste statique à `100.0` sans keyframes.
Dans `scale_expr_pair()` :


```python
w_path = build_path(item.scale, 'v', start, end, item.scale_w)
h_path = build_path(item.scale2, 'v', start, end, item.scale_h)

```


Si `item.scale.kfs` est peuplé et `item.scale2.kfs` est vide, seule `w_expr` est animée tandis que `h_expr` reste figée à `100 %`, ce qui étire l'image horizontalement au lieu d'effectuer un zoom homothétique ! De plus, si `<StartKeyframe>` est absent sur un paramètre animé (`params[i].static is None`), `_read_components()` ignore le paramètre et ses keyframes.


* **Correction** : Lire le booléen `Uniform Scale` (`params[3]`) dans `_read_components()` (ou, si `item.scale` a des keyframes et `item.scale2` n'en a pas avec `abs(item.scale_w - item.scale_h) < 1e-6`, utiliser `w_path` pour les deux axes dans `scale_expr_pair()`). Accepter un paramètre dans `_read_components()` dès que `p.static is not None or p.kfs`.

### 2.3. Rotation (`rotate`) : coins noirs opaques et animation figée sur les images fixes

* **Localisation** : `transform_filters()`, et `main()` (traitement des `still` et de la piste de base).


* **Problème** :
1. `transform_filters()` génère `rotate=a='{rot}'` sans `c=none` (ou `fillcolor=0x00000000`), et `format=rgba` n'est ajouté qu'à la **fin** de `tail` (après `tf`). Par défaut, `vf_rotate` remplit les coins dégagés par la rotation en **noir opaque** (`fillcolor=black`) au lieu de transparent !


2. Pour une image fixe (`is_still == True`), l'entrée `-i image.png` ne produit **qu'une seule trame** (`t=0`), répétée ensuite par `overlay`. Si une image fixe possède une échelle ou une rotation animée (`scale.kfs` ou `rotation.kfs`), `scale` et `rotate` sont placés en amont de `overlay` et ne sont donc évalués qu'une seule fois à `t=0` : l'animation de zoom/rotation de l'image fixe reste totalement figée.




* **Correction** :
* Forcer `format=rgba` avant `rotate` et passer `rotate=a='{rot}':c=none`.
* Si `is_still` a une échelle ou une rotation animée, passer `-loop 1 -framerate {fps} -t {span_end - ov.start} -i {path}` (ou insérer `loop=loop=-1:size=1:start=0,fps={fps},trim=duration={dur},setpts=...` avant `tf`).



### 2.4. Piste de base en mode composition : perte d'alpha après `gblur`, opacité ignorée et masque recalculé sur chaque trame

* **Localisation** : `main()` (boucle `base`, branche `if item_needs_canvas(item) or item.masks or not crop_window_fits(...)`).


* **Problème** :
1. Contrairement aux overlays qui utilisent `mask_dur = 1.0 / fps` (1 seule trame répétée par `alphamerge`), la piste de base passe `cout - cin` à `mask_chain()` : un masque sur la piste de base recalcule `drawbox`/`geq` + `lutyuv` + `gblur` sur **toutes les trames** du segment.


2. Si un segment de la piste de base combine `item.masks` et `item.blur`, `tail` contient `gblur` sans `format=rgba` final avant `overlay` sur `[cg{n}]` (`yuv420p`) : FFmpeg négocie `yuv420p` en entrée de `gblur` et **détruit le canal alpha du masque**.


3. `item.opacity` n'est jamais appliqué aux segments de la piste de base.


4. Dans `_mask_bbox_px()`, borner `x0, y0, x1, y1` à `[0, w] × [0, h]` avant de calculer le centre `cx, cy` et les rayons `rx, ry` d'un masque `ellipse` déforme toute ellipse qui déborde du cadre.





---

## 3. Pistes d'amélioration (Audio, Cache, Résolution de chemins, Évaluateur FFmpeg)

### 3.1. Pistes audio secondaires (`A2`, `A3`…) ignorées et éléments audio sans source

* **Localisation** : `main()` (`audio_items = parsed.items_of_track(seq['tracks']['audio'][0]) ...`).


* **Problème** :
1. Seule la première piste audio (`audio[0]`, soit `A1`) est lue. Toute musique, ambiance ou habillage sonore placé sur `A2`, `A3`, etc. est absent de l'export final.


2. Contrairement à `base` et `overlays`, `audio_items` ne filtre pas `if i.clip and i.clip.source_path`, ce qui provoque un arrêt si un item vide est présent.




* **Amélioration** : Parcourir toutes les pistes de `seq['tracks']['audio']` contenant au moins un item valide, construire la piste concaténée `[aout_{t}]` de chacune, et s'il y en a plusieurs, les sommer via `amix=inputs={N}:duration=longest:dropout_transition=0,volume={N}[aout]`.

### 3.2. Profondeur de recherche `resolve_media()` bloquée à `skip < 3`

* **Localisation** : `resolve_media(win_path, bases)`.


* **Problème** : `for skip in range(0, min(3, len(parts))):` ne retire au maximum que les **2 premiers segments** du chemin Windows (ex. `F:\` et `DossierParent`). Si le projet sur Windows était rangé plus profondément (ex. `F:\Clients\2026\134 Camille\rush\video.mp4`), `skip` s'arrête à `2` et ne teste jamais `/mnt/cache/134 Camille/rush/video.mp4` (`skip = 3`), provoquant une erreur `media not found` alors que le fichier est bien présent sous `--base`.


* **Amélioration** : Étendre la boucle à `range(0, len(parts))` (du chemin relatif le plus spécifique jusqu'au nom de fichier) et mettre le résultat en cache mémoire `_resolve_cache[(win_path, tuple(bases))]`.

### 3.3. Incohérence `-b:v` + `-crf 18` avec `libx264` et `-bufsize` fixe

* **Localisation** : `main()` (construction de `cmd`).


* **Problème** : En repli `libx264`, le script passe simultanément `-b:v 10M` et `-crf 18`. Avec `libx264`, `-crf 18` écrase la consigne `-b:v`, produisant un débit variable imprévisible (qui fausse l'estimation de progression en Mo de `encoder_worker.py`). Par ailleurs, `-bufsize 20M` est codé en dur au lieu d'être proportionnel à `--bitrate`.


* **Amélioration** : Retirer `-crf 18` lorsque `--bitrate` est utilisé (ou utiliser `-nal-hrd cbr` / `-b:v` + `-maxrate` + `-bufsize`), et calculer `bufsize` à `2 × bitrate`.

### 3.4. Ordre d'exécution de `_prune_cache()`, écriture atomique de `.bbox.json` et arbre `axis_expr()`

* **Localisation** : `_prune_cache()`, `_bbox_cache_write()`, `axis_expr()`.


* **Problème** :
1. `_prune_cache()` est appelé **avant** `alpha_intermediate()` et `alpha_bbox()` : si le cache dépasse tout juste la limite, il peut supprimer l'intermédiaire que l'export courant s'apprêtait à réutiliser.


2. `_bbox_cache_write()` écrit directement dans `.bbox.json` sans passer par un fichier `.tmp` + `os.replace()` (contrairement à `_write_alpha_meta()`).


3. `axis_expr()` génère une chaîne linéaire récursive `if(lt(t,...), ..., if(lt(t,...)))` de profondeur $O(N)$ (jusqu'à $24 \times K$ niveaux avec `segment_pieces`), alors qu'une dichotomie construit un arbre équilibré de profondeur $\lceil \log_2(N) \rceil$.





---

## 4. Correctifs recommandés

Voici les patchs ciblés pour corriger ces points dans `tools/prproj-unpacker/prproj2ffmpeg.py` :

### A. Décompression `.prproj` en mémoire (zéro fichier temporaire)

```python
class Project:
    def __init__(self, xml_path, xml_bytes=None):
        self.xml_path = os.path.abspath(xml_path)
        self.by_id = {}
        self.by_uid = {}
        self.media = {}
        self.sequences = []
        if xml_bytes is not None:
            root = ET.fromstring(xml_bytes)
        else:
            root = ET.parse(self.xml_path).getroot()
        ...

```

Et dans `main()` :

```python
    src = os.path.abspath(args.input)
    if src.lower().endswith('.prproj'):
        import gzip
        with open(src, 'rb') as f:
            raw = f.read()
        xml_bytes = gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw
        parsed = Project(src, xml_bytes=xml_bytes)
    else:
        parsed = Project(src)

```

### B. Résolution des chemins médias (`resolve_media`) avec cache et profondeur complète

```python
_resolve_cache = {}

def resolve_media(win_path, bases):
    if not win_path:
        return None
    cache_key = (win_path, tuple(bases))
    if cache_key in _resolve_cache:
        return _resolve_cache[cache_key]
    norm = win_path.replace('\\', '/').replace(':', '')
    parts = [p for p in norm.split('/') if p]
    for base in bases:
        for skip in range(0, len(parts)):
            cand = os.path.join(base, *parts[skip:])
            if os.path.isfile(cand):
                _resolve_cache[cache_key] = cand
                return cand
    _resolve_cache[cache_key] = None
    return None

```

### C. `blur_chain` sécurisé (dimensions paires garanties et restauration exacte)

```python
def blur_chain(blur):
    sigma = max(0.1, blur.amount * _BLUR_SIGMA_FACTOR)
    if sigma >= 24.0:
        s4 = max(0.1, sigma / 4.0)
        return (f"scale='max(2,trunc(iw/8)*2)':'max(2,trunc(ih/8)*2)':flags=fast_bilinear,"
                f"gblur=sigma={s4:.3f}:steps=2,"
                f"scale=iw*4:ih*4:flags=bilinear")
    elif sigma >= 6.0:
        s2 = max(0.1, sigma / 2.0)
        return (f"scale='max(2,trunc(iw/4)*2)':'max(2,trunc(ih/4)*2)':flags=fast_bilinear,"
                f"gblur=sigma={s2:.3f}:steps=2,"
                f"scale=iw*2:ih*2:flags=bilinear")
    return f"gblur=sigma={sigma:.3f}:steps=2"

```

*(Si les dimensions cibles exactes `(w, h)` sont connues à l'endroit de l'appel, passer `scale={w}:{h}:flags=bilinear` au dernier étage évite toute dérive d'arrondi de 1 à 6 px).*

### D. `visible_overlay_window` : alignement pair `cx`/`cy` et `out_w`/`out_h`

```python
def visible_overlay_window(item, src_w, src_h, seq_w, seq_h):
    if not item.masks or item.blur is not None:
        return None
    transform = _static_uniform_transform(item, src_w, src_h, seq_w, seq_h)
    if transform is None:
        return None
    x0, y0, sx, sy = transform
    bbox = _mask_union_bbox_px(item, src_w, src_h)
    if bbox is None:
        return None
    mx0, my0, mx1, my1 = bbox
    w_int, h_int = int(round(src_w)), int(round(src_h))
    cx = max(0, min(int(math.floor(mx0)), w_int - 2))
    cy = max(0, min(int(math.floor(my0)), h_int - 2))
    # Aligner cx/cy sur 2 pour éviter la troncature silencieuse de vf_crop en yuv420p
    if not _is_simple_opaque_rect_mask(item):
        cx &= ~1
        cy &= ~1
    cw = max(2, min(int(math.ceil(mx1)) - cx, w_int - cx))
    ch = max(2, min(int(math.ceil(my1)) - cy, h_int - cy))
    if not _is_simple_opaque_rect_mask(item):
        if cw & 1 and cx + cw + 1 <= w_int:
            cw += 1
        if ch & 1 and cy + ch + 1 <= h_int:
            ch += 1
    ox = int(round(x0 + cx * sx))
    oy = int(round(y0 + cy * sy))
    ow = max(2, int(round(cw * sx)))
    oh = max(2, int(round(ch * sy)))
    return cx, cy, cw, ch, ox, oy, ow, oh

```

Et dans `main()` lors de l'utilisation de `window` :

```python
            odd_window = bool((cx & 1) or (cy & 1) or (cw & 1) or (ch & 1) or (out_w & 1) or (out_h & 1))
            needs_alpha = needs_mask or source_has_alpha or odd_window or (
                opacity_value is not None and opacity_value < 0.999)
            crop_src = f"crop={cw}:{ch}:{cx}:{cy}:exact=1"

```

### E. Préservation du Z-order et sécurisation de `merge_loop_groups`

```python
def merge_loop_groups(items, bases):
    def sig(item):
        rot = item.rotation
        pos = item.position.static if item.position and item.position.static else None
        return (round(item.scale_w, 6), round(item.scale_h, 6),
                bool(item.scale and item.scale.kfs), bool(item.scale2 and item.scale2.kfs),
                rot.kind if rot else None, bool(rot and rot.kfs),
                round(rot.static, 6) if rot and rot.static is not None else None,
                tuple(round(v, 9) for v in item.anchor),
                tuple(round(v, 9) for v in pos) if pos else None,
                bool(item.position and item.position.kfs),
                _opacity_scalar(item.opacity),
                bool(item.masks),
                item.blur.amount if item.blur is not None else None)

    def fits(item):
        if item.masks or (item.position and item.position.kfs) or (item.scale and item.scale.kfs) or (item.rotation and item.rotation.kfs):
            return False
        cin, cdur = clip_times(item)
        resolved = resolve_media(item.clip.source_path, bases)
        full_dur = probe_duration(resolved) if resolved else 0.0
        # -stream_loop -1 boucle sur la fin réelle du fichier média
        return full_dur > 1e-6 and abs(cdur - full_dur) <= 0.1

```

Et pour la collecte des overlays dans `main()`, conserver l'ordre des pistes (`track_idx`) et ne fusionner les répétitions de boucle qu'au sein d'une même couche logique (ou trier les `overlay_groups` finaux par `(min_track_idx, group[0].start)`).

### F. Déduplication sûre dans `get_pull` (anti-collision de pads `[p:v]` / `[p:a]`)

```python
    pulls = []
    pull_map = {}
    used_stream_pads = set()

    def get_pull(path, in_s, out_s, unique=False, alpha_ok=False, loop_span=None, stream='v'):
        resolved = resolve_media(path, bases)
        if not resolved:
            die(f"media not found for {path} (use --base)")
        if alpha_ok:
            resolved = alpha_intermediate(resolved)
        if loop_span is not None:
            key = (resolved, 0.0, round(loop_span, 6), 'loop')
        elif unique:
            key = (resolved, round(in_s, 6), round(out_s, 6), len(pulls))
        else:
            key = (resolved, round(in_s, 6), round(out_s, 6))
        if key in pull_map and (pull_map[key], stream) in used_stream_pads:
            key = (resolved, round(in_s, 6), round(out_s, 6), len(pulls))
        if key not in pull_map:
            pull_map[key] = len(pulls)
            pulls.append((key[0], key[1], key[2], key[3] == 'loop' if len(key) > 3 else False))
        idx = pull_map[key]
        used_stream_pads.add((idx, stream))
        return idx

```

---

## 5. Suivi des correctifs

Correctifs appliqués le 2026-10-23 dans `tools/prproj-unpacker/prproj2ffmpeg.py` (copie de
référence), `prproj_engine.py` et `encoder_worker.py`. Validation : `unittest` **68/68**,
`--dump` et `--dry-run` GOAL, benchmark A/B 15 s (`docs/perf_export.md`).

| Constat | Fonction / mécanisme corrigé | Test |
| --- | --- | --- |
| 1.1 Z-order + `source_path` nul | `collect_overlay_layers()`, collecte par couche dans `main()` | `test_collect_overlay_layers` |
| 1.2 `blur_chain()` | `blur_chain(blur, width, height)`, décimation paire + restauration exacte | `test_blur_chain_dimensions` |
| 1.3 Boucles erronées | `merge_loop_groups(items, bases)`, `fits()` sur durée réelle, `sig()` complet | `test_merge_loop_groups_duree_pleine` |
| 1.4 Collisions de pads | classe `PullTable` (clé normale/unique/boucle, `_used` par flux) | `test_pull_table_collisions` |
| 1.5 XML temporaire | `Project(xml_path, xml_bytes=...)`, `gzip.decompress` en mémoire | `test_project_parse_en_memoire` |
| 1.6 NVENC runtime | `_encoder_works()`, `_select_encoder()`, `_nvenc_session_ok()` | `test_select_encoder`, `test_probe_ffmpeg_nvenc_runtime` |
| 2.1 Fenêtre visible | `visible_overlay_window()` pair/exact, `crop=…:exact=1`, flou exclu | `test_visible_overlay_window_masque_rectangulaire`, `test_visible_overlay_window_replis` |
| 2.2 Échelle uniforme | `_start_bool()`, `Param.static_bool`, `uniform_scale`, `scale_expr_pair()` | `test_uniform_scale_axes`, `test_read_components_param_anime` |
| 2.3 Rotation / images fixes | `transform_filters()` `format=rgba,rotate=…:c=none`, `-loop 1` si animé | `test_transform_filters_rotation_rgba` |
| 2.4 Piste de base alpha/opacité | `mask_chain(..., 1/fps)`, `format=yuva420p` autour de `gblur`, `item_needs_canvas()`, `_ellipse_geometry()` | `test_item_needs_canvas_opacite`, `test_ellipse_geometry_hors_cadre` |
| 3.1 Audio multi-pistes | `collect_audio_tracks()`, concat par piste, `amix` + `volume=N` | `test_collect_audio_tracks` |
| 3.2 Profondeur médias | `resolve_media()` `range(0, len(parts))` + `_resolve_cache` | `test_resolve_media_profondeur_et_cache` |
| 3.3 Débit libx264 | `_bitrate_bps()`, `-bufsize = 2 × débit`, suppression de `-crf` | `test_parse_bitrate` (grammaire), dry-run GOAL |
| 3.4 Cache / `axis_expr` | `_prune_cache(protect=())` après graphe, `.bbox.json` atomique, arbre équilibré | `test_prune_cache_protege_les_pulls`, `test_bbox_cache_write_atomique`, `test_axis_expr_equilibre` |
| UI / logs | `probe_ffmpeg()` préflight NVENC, `summarize_line()` ligne `[encoder]` | `test_summarize_line`, `test_probe_ffmpeg_nvenc_runtime` |
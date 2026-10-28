#!/usr/bin/env python3
"""prproj2ffmpeg - export a sequence from an unpacked Premiere .prproj XML to MP4 via ffmpeg.

The .prproj XML is the only source of truth. Every track item found in the XML is
rendered exactly once, in timeline order, whatever their number of cuts/segments.
Motion position keyframes are reproduced with their original cubic bezier easing
(the handles are read from the XML). No other data source is ever consulted.

Usage:
  prproj2ffmpeg.py project.prproj [--out out.mp4] [--bitrate 10M] [--dry-run]
                  [--dump] [--encoder h264_nvenc] [--base DIR] [--sequence NAME]
"""
import argparse
import base64
import gzip
import hashlib
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zlib

TICKS = 254016000000
CACHE_ROOT = os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'),
                          'prproj2ffmpeg')


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def text(el, tag, default=None):
    if el is None:
        return default
    child = el.find(tag)
    if child is None or child.text is None:
        return default
    return child.text


def ref(by_id, by_uid, el, tag):
    if el is None:
        return None
    child = el.find(tag)
    if child is None:
        return None
    oid = child.get('ObjectRef')
    if oid is not None:
        return by_id.get(oid)
    uid = child.get('ObjectURef')
    if uid is not None:
        return by_uid.get(uid)
    return None


def to_sec(ticks_str, default=0.0):
    if ticks_str is None:
        return default
    try:
        return int(ticks_str) / TICKS
    except (TypeError, ValueError):
        return default


def first_num(value_str):
    if not value_str or ',' not in value_str:
        return None
    rest = value_str.split(',', 1)[1]
    try:
        return float(re.split(r'[,:]', rest)[0])
    except ValueError:
        return None


def parse_point(value_str):
    if not value_str or ',' not in value_str:
        return None
    rest = value_str.split(',', 1)[1]
    m = re.match(r'([^,]+):([^,]+)', rest)
    if not m:
        return None
    try:
        return float(m.group(1)), float(m.group(2))
    except ValueError:
        return None


class KeyFrame:
    """One Motion position keyframe.

    Times in seconds (absolute). Handles (ix/iy/ox/oy) are value offsets of the
    bezier control points. Two temporal encodings exist in the wild:
      - bezier keys: tail "5,0,0,0,0,0" (controls at 1/3 of the interval)
      - linear keys: tail "0,0,in_speed,in_influence,out_speed,out_influence"
        (controls collinear with the segment -> exact straight motion)
    """
    __slots__ = ('t', 'x', 'y', 'ix', 'iy', 'ox', 'oy',
                 'mode', 'in_speed', 'in_infl', 'out_speed', 'out_infl')

    def __init__(self, t, x, y, ix=0.0, iy=0.0, ox=0.0, oy=0.0,
                 mode='bezier', in_speed=0.0, in_infl=0.0, out_speed=0.0, out_infl=0.0):
        self.t = t
        self.x = x
        self.y = y
        self.ix = ix
        self.iy = iy
        self.ox = ox
        self.oy = oy
        self.mode = mode
        self.in_speed = in_speed
        self.in_infl = in_infl
        self.out_speed = out_speed
        self.out_infl = out_infl


def parse_point_kfs(el):
    if el is None:
        return []
    kfs_el = el.find('Keyframes')
    if kfs_el is None or not kfs_el.text:
        return []
    out = []
    for chunk in kfs_el.text.split(';'):
        chunk = chunk.strip()
        if not chunk:
            continue
        fields = chunk.split(',')
        if len(fields) < 2:
            continue
        try:
            t = int(fields[0]) / TICKS
            xy = fields[1].split(':')
            x = float(xy[0])
            y = float(xy[1]) if len(xy) > 1 else 0.0
        except ValueError:
            continue
        ix = iy = ox = oy = 0.0
        mode = 'bezier'
        in_speed = in_infl = out_speed = out_infl = 0.0
        if len(fields) >= 14:
            try:
                ix, iy, ox, oy = (float(fields[-4]), float(fields[-3]),
                                  float(fields[-2]), float(fields[-1]))
                if fields[2].strip() == '0':
                    mode = 'linear'
                    in_speed = float(fields[4])
                    in_infl = float(fields[5])
                    out_speed = float(fields[6])
                    out_infl = float(fields[7])
            except ValueError:
                pass
        out.append(KeyFrame(t, x, y, ix, iy, ox, oy, mode, in_speed, in_infl, out_speed, out_infl))
    out.sort(key=lambda k: k.t)
    return out


def parse_scalar_kfs(el):
    """Scalar (Scale/Rotation) keyframes: time,value,tail6 (no spatial handles)."""
    if el is None:
        return []
    kfs_el = el.find('Keyframes')
    if kfs_el is None or not kfs_el.text:
        return []
    out = []
    for chunk in kfs_el.text.split(';'):
        chunk = chunk.strip()
        if not chunk:
            continue
        fields = chunk.split(',')
        if len(fields) < 8:
            continue
        try:
            t = int(fields[0]) / TICKS
            v = float(fields[1])
        except ValueError:
            continue
        mode = 'bezier'
        in_speed = in_infl = out_speed = out_infl = 0.0
        try:
            if fields[2].strip() == '0':
                mode = 'linear'
                in_speed = float(fields[4])
                in_infl = float(fields[5])
                out_speed = float(fields[6])
                out_infl = float(fields[7])
        except ValueError:
            pass
        out.append(KeyFrame(t, v, 0.0, 0.0, 0.0, 0.0, 0.0, mode,
                            in_speed, in_infl, out_speed, out_infl))
    out.sort(key=lambda k: k.t)
    return out


def parse_mask_path(data):
    """Points d'ancrage (x, y) normalisés d'un blob « Path » de masque Premiere.

    Format observé (masque rectangle) : en-tête de 25 octets, puis — pour chaque point —
    3 paires float32 (ancre + 2 poignées de tangente, identiques pour un segment droit)
    suivies de 4 octets de séparation (sauf après le dernier point). Les coordonnées
    sont normalisées sur la source du clip (0-1).
    """
    if not data or len(data) < 25 + 24:
        return []
    points = []
    offset = 25
    while offset + 24 <= len(data):
        try:
            x, y = struct.unpack_from('<2f', data, offset)
        except struct.error:
            break
        if not (-3.0 <= x <= 4.0 and -3.0 <= y <= 4.0):
            break
        points.append((x, y))
        offset += 28
    return points


def _stat_float(el):
    """Valeur numérique d'un StartKeyframe (« ticks,value,... »), ou None."""
    if el is None:
        return None
    return first_num(text(el, 'StartKeyframe'))


def _stat_bool(el):
    """Valeur booléenne d'un StartKeyframe (« ticks,true/false,... »)."""
    if el is None:
        return False
    start = text(el, 'StartKeyframe')
    if not start or ',' not in start:
        return False
    return start.split(',', 1)[1].strip().lower().startswith('true')


def _stat_int(el):
    value = _stat_float(el)
    return int(value) if value is not None else None


def _start_bool(el):
    """Valeur booléenne d'un StartKeyframe (« ticks,true/false,... »), sinon None.

    Contrairement a `_stat_bool` (qui retombe sur False), un parametre sans
    StartKeyframe exploitable reste inconnu : utile pour « Uniform Scale ».
    """
    if el is None:
        return None
    start = text(el, 'StartKeyframe')
    if not start or ',' not in start:
        return None
    value = start.split(',', 1)[1].strip().lower()
    if value.startswith('true'):
        return True
    if value.startswith('false'):
        return False
    return None


class Param:
    __slots__ = ('kind', 'static', 'kfs', 'static_bool')

    def __init__(self, el):
        self.kind = 'point' if el.tag == 'PointComponentParam' else 'scalar'
        start = text(el, 'StartKeyframe')
        self.static = parse_point(start) if self.kind == 'point' else first_num(start or '')
        self.kfs = parse_point_kfs(el) if self.kind == 'point' else parse_scalar_kfs(el)
        self.static_bool = _start_bool(el)


class Clip:
    def __init__(self):
        self.source_path = None
        self.source_w = None
        self.source_h = None
        self.still = False
        self.in_sec = 0.0
        self.out_sec = 0.0
        self.name = ''

    def duration(self, fallback):
        d = self.out_sec - self.in_sec
        return d if d > 1e-9 else fallback


class Mask:
    """Masque vectoriel Premiere (composant AE.ADBE AEMask2).

    Coordonnées normalisées (0-1) dans l'espace SOURCE du clip. Les masques sont
    appliqués à l'image source, avant le composant Motion.
    """

    def __init__(self):
        self.shape = None       # 'rect' | 'ellipse' (les autres formes ne sont pas rendues)
        self.bbox = None        # (x0, y0, x1, y1) normalisés
        self.points = []        # points d'ancrage normalisés (diagnostic / --dump)
        self.feather = 0.0      # pixels (bord adouci)
        self.opacity = 100.0    # % (0-100)
        self.expansion = 0.0    # pixels (dilatation/érosion de la forme)
        self.inverted = False
        self.blend = 0          # 0 = Add (observé) ; autres valeurs : union approximée
        self.type = None        # valeur brute du paramètre « Type » (2 = rectangle observé)


class BlurFx:
    """Gaussian Blur Premiere (composant AE.Impact_Blur_FX)."""

    def __init__(self):
        self.amount = 0.0
        self.thickness = 0.0
        self.uniform = True
        self.angle = 0.0


class TrackItem:
    def __init__(self):
        self.start = 0.0
        self.end = 0.0
        self.clip = None
        self.scale_w = 100.0
        self.scale_h = 100.0
        self.scale = None
        self.scale2 = None
        self.rotation = None
        self.anchor = (0.5, 0.5)
        self.position = None
        self.opacity = None
        self.masks = []
        self.blur = None
        # « Echelle uniforme » est cochee par defaut dans Premiere.
        self.uniform_scale = True


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
        for el in root:
            oid = el.get('ObjectID')
            if oid is not None:
                self.by_id[oid] = el
            uid = el.get('ObjectUID')
            if uid is not None:
                self.by_uid[uid] = el
        self._index_media(root)
        self._index_sequences(root)

    def _index_media(self, root):
        for el in root:
            if el.tag != 'Media':
                continue
            streams = []
            for tag in ('VideoStream', 'AudioStream'):
                for s in el.findall(tag):
                    sr = self.by_id.get(s.get('ObjectRef'))
                    if sr is not None:
                        streams.append(sr)
            info = {
                'path': text(el, 'FilePath'),
                'still': any(text(s, 'IsStill') == 'true' for s in streams),
                'w': None,
                'h': None,
            }
            for s in streams:
                fr = text(s, 'FrameRect')
                if fr:
                    parts = fr.split(',')
                    if len(parts) == 4:
                        info['w'] = float(parts[2])
                        info['h'] = float(parts[3])
            self.media[el.get('ObjectUID')] = info

    def _index_sequences(self, root):
        for el in root:
            if el.tag != 'Sequence':
                continue
            tracks = {'video': [], 'audio': []}
            groups = el.find('TrackGroups')
            if groups is not None:
                for tg in groups.findall('TrackGroup'):
                    group = ref(self.by_id, self.by_uid, tg, 'Second')
                    if group is None:
                        continue
                    if group.tag == 'VideoTrackGroup':
                        tracks['video'] = self._tracks_of(group)
                    elif group.tag == 'AudioTrackGroup':
                        tracks['audio'] = self._tracks_of(group)
            self.sequences.append({'name': text(el, 'Name', 'Untitled'), 'el': el, 'tracks': tracks})

    def _tracks_of(self, group):
        tracks = []
        tks = group.find('TrackGroup/Tracks')
        if tks is not None:
            for t_el in tks.findall('Track'):
                track = self.by_uid.get(t_el.get('ObjectURef'))
                if track is not None:
                    tracks.append(track)
        return tracks

    def items_of_track(self, track):
        items = []
        ci = track.find('ClipTrack/ClipItems/TrackItems')
        if ci is None:
            return items
        for t_el in ci.findall('TrackItem'):
            el = self.by_id.get(t_el.get('ObjectRef'))
            if el is None:
                continue
            item = TrackItem()
            ti = el.find('ClipTrackItem/TrackItem')
            item.start = to_sec(text(ti, 'Start'), 0.0)
            item.end = to_sec(text(ti, 'End'), 0.0)
            subclip = ref(self.by_id, self.by_uid, el.find('ClipTrackItem'), 'SubClip')
            item.clip = self._clip_of(subclip)
            chain = ref(self.by_id, self.by_uid, el.find('ClipTrackItem/ComponentOwner'), 'Components')
            self._read_components(item, chain)
            items.append(item)
        items.sort(key=lambda i: i.start)
        return items

    def _clip_of(self, subclip):
        clip = Clip()
        if subclip is None:
            return clip
        outer = ref(self.by_id, self.by_uid, subclip, 'Clip')
        if outer is None:
            return clip
        inner = outer.find('Clip')
        if inner is None:
            return clip
        clip.in_sec = to_sec(text(inner, 'InPoint'), 0.0) or 0.0
        clip.out_sec = to_sec(text(inner, 'OutPoint'), 0.0) or 0.0
        source = ref(self.by_id, self.by_uid, inner, 'Source')
        if source is not None:
            media_el = ref(self.by_id, self.by_uid, source, 'MediaSource/Media') or \
                ref(self.by_id, self.by_uid, source, 'Media')
            media = self.media.get(media_el.get('ObjectUID') if media_el is not None else None, {})
            clip.source_path = media.get('path')
            clip.source_w = media.get('w')
            clip.source_h = media.get('h')
            clip.still = media.get('still', False)
        clip.name = text(subclip, 'Name', '')
        return clip

    def _read_components(self, item, chain):
        if chain is None:
            return
        comps = chain.find('ComponentChain/Components')
        if comps is None:
            return
        for c_el in comps.findall('Component'):
            comp = self.by_id.get(c_el.get('ObjectRef'))
            if comp is None:
                continue
            match = text(comp, 'MatchName', '')
            params = []
            params_el = comp.find('Component/Params')
            if params_el is not None:
                for p_el in params_el.findall('Param'):
                    p = self.by_id.get(p_el.get('ObjectRef'))
                    if p is not None:
                        params.append(Param(p))
            if match == 'AE.ADBE Motion' and params:
                def param_active(p):
                    return p.static is not None or bool(p.kfs)

                if params[0].kind == 'point' and param_active(params[0]):
                    item.position = params[0]
                if len(params) > 1 and param_active(params[1]):
                    item.scale = params[1]
                    if params[1].static is not None:
                        item.scale_w = params[1].static
                if len(params) > 2 and param_active(params[2]):
                    item.scale2 = params[2]
                    if params[2].static is not None:
                        item.scale_h = params[2].static
                if len(params) > 4 and param_active(params[4]):
                    item.rotation = params[4]
                if len(params) > 3 and params[3].static_bool is not None:
                    item.uniform_scale = params[3].static_bool
                points = [p for p in params if p.kind == 'point' and p.static]
                if points:
                    item.anchor = points[-1].static
            elif 'Opacity' in match and params:
                item.opacity = params[0]
            elif match == 'AE.ADBE AEMask2':
                self._append_mask(item, comp)
            elif match == 'AE.Impact_Blur_FX':
                blur = self._blur_of(comp)
                if blur is not None:
                    item.blur = blur
            # Les masques peuvent aussi être rattachés comme sous-composants d'un composant
            # (cas observé dans Premiere : masque listé sous le composant Opacity).
            self._read_subcomponents(item, comp)

    def _comp_params(self, comp):
        """Paramètres d'un composant, sous forme [{name, id, el}] (ordre du fichier conservé)."""
        out = []
        params_el = comp.find('Component/Params')
        if params_el is None:
            return out
        for p_el in params_el.findall('Param'):
            p = self.by_id.get(p_el.get('ObjectRef'))
            if p is None:
                continue
            out.append({
                'el': p,
                'name': text(p, 'Name') or '',
                'id': text(p, 'ParameterID') or '',
            })
        return out

    def _read_subcomponents(self, item, comp):
        subs = comp.find('SubComponents')
        if subs is None:
            return
        for s_el in subs.findall('SubComponent'):
            sub = self.by_id.get(s_el.get('ObjectRef'))
            if sub is None:
                continue
            if text(sub, 'MatchName', '') == 'AE.ADBE AEMask2':
                self._append_mask(item, sub)

    def _append_mask(self, item, comp):
        mask = self._mask_of(comp)
        if mask is not None:
            item.masks.append(mask)

    def _mask_of(self, comp):
        """Construit un Mask depuis un composant AEMask2 (None si la géométrie est illisible)."""
        by_name, by_id = {}, {}
        for p in self._comp_params(comp):
            if p['name']:
                by_name[p['name']] = p['el']
            if p['id']:
                by_id[p['id']] = p['el']

        points = []
        path_el = by_name.get('Path')
        if path_el is not None:
            blob = path_el.find('StartKeyframeValue')
            if blob is not None and blob.text:
                try:
                    points = parse_mask_path(base64.b64decode(blob.text.strip()))
                except Exception:
                    points = []
        if len(points) < 3:
            return None

        mask = Mask()
        mask.points = points
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        mask.bbox = (min(xs), min(ys), max(xs), max(ys))
        mask.type = _stat_int(by_id.get('22'))
        mask.feather = _stat_float(by_name.get('Feather')) or 0.0
        opacity = _stat_float(by_name.get('Opacity'))
        mask.opacity = 100.0 if opacity is None else opacity
        mask.expansion = _stat_float(by_name.get('Expansion')) or 0.0
        mask.blend = _stat_int(by_name.get('Blend Mode')) or 0
        mask.inverted = _stat_bool(by_id.get('17'))
        # « Type » 2 = rectangle (observé) ; toute autre valeur est traitée comme ellipse.
        mask.shape = 'rect' if mask.type == 2 else 'ellipse'
        return mask

    def _blur_of(self, comp):
        """Construit un BlurFx depuis un composant AE.Impact_Blur_FX."""
        by_name = {}
        for p in self._comp_params(comp):
            if p['name']:
                by_name[p['name']] = p['el']
        amount = _stat_float(by_name.get('Amount'))
        if amount is None:
            return None
        blur = BlurFx()
        blur.amount = amount
        blur.thickness = _stat_float(by_name.get('Thickness')) or 0.0
        blur.uniform = _stat_bool(by_name.get('Uniform Blur'))
        blur.angle = _stat_float(by_name.get('Angle')) or 0.0
        return blur


_resolve_cache = {}


def resolve_media(win_path, bases):
    """Chemin local d'un média Windows, en testant toutes les profondeurs de suffixe.

    Les bases sont essayées dans l'ordre, du chemin relatif le plus spécifique au nom
    de fichier seul. Les résultats positifs et négatifs sont mémorisés : un export
    résout chaque média une seule fois.
    """
    if not win_path:
        return None
    cache_key = (win_path, tuple(str(base) for base in bases))
    if cache_key in _resolve_cache:
        return _resolve_cache[cache_key]
    norm = win_path.replace('\\', '/').replace(':', '')
    parts = [p for p in norm.split('/') if p]
    for base in bases:
        for skip in range(0, len(parts)):
            cand = os.path.join(str(base), *parts[skip:])
            if os.path.isfile(cand):
                _resolve_cache[cache_key] = cand
                return cand
    _resolve_cache[cache_key] = None
    return None


def collect_overlay_layers(parsed, video_tracks, base_track_items):
    """Couches d'incrustation dans l'ordre d'empilement Premiere (V1 still, V2, V3...).

    La couche 0 contient les images fixes de la piste de base ; chaque piste suivante
    forme une couche. Les items sans source sont ignores. L'ordre final des couches est
    l'ordre vertical : les derniers composites passent au-dessus.
    """
    layers = [[it for it in base_track_items
               if it.clip is not None and it.clip.source_path and it.clip.still]]
    for track in video_tracks[1:]:
        layers.append([it for it in parsed.items_of_track(track)
                       if it.clip is not None and it.clip.source_path])
    return layers


def collect_audio_tracks(parsed, audio_track_elements):
    """Items valides de chaque piste audio non vide (A1, A2, A3...)."""
    tracks = []
    for track in audio_track_elements:
        items = [it for it in parsed.items_of_track(track)
                 if it.clip is not None and it.clip.source_path]
        if items:
            tracks.append(items)
    return tracks


def bezier_poly(v0, o0, v1, o1, t0, dt):
    """Cubic bezier (control points at 1/3 and 2/3 of the interval) as a t-polynomial.

    o0 = outgoing handle offset of the left keyframe, o1 = incoming handle of the
    right keyframe, both relative to their keyframe value (as stored in the XML).
    """
    p0, p1, p2, p3 = v0, v0 + o0, v1 + o1, v1
    c0 = p0
    c1 = 3.0 * (p1 - p0)
    c2 = 3.0 * (p0 - 2.0 * p1 + p2)
    c3 = -p0 + 3.0 * p1 - 3.0 * p2 + p3
    inv = 1.0 / dt
    d3 = c3 * inv ** 3
    d2 = c2 * inv ** 2 - 3.0 * c3 * inv ** 3 * t0
    d1 = c1 * inv - 2.0 * c2 * inv ** 2 * t0 + 3.0 * c3 * inv ** 3 * t0 * t0
    d0 = c0 - c1 * inv * t0 + c2 * inv ** 2 * t0 * t0 - c3 * inv ** 3 * t0 ** 3
    return (d0, d1, d2, d3)


def side_handles(kf, other_v, own_v, dt, axis, side):
    """(time_fraction, value_offset) of a keyframe handle for one axis.

    Linear keys carry (speed, influence) computed from the segment delta, which
    yields collinear control points (exact straight motion). Bezier keys carry
    the spatial tangents directly; when absent, the AE speed x influence formula
    is used.
    """
    if kf.mode == 'linear':
        infl = kf.out_infl if side == 'out' else kf.in_infl
        if not (0.0 < infl <= 0.5):
            infl = 1.0 / 6.0
        return infl, (other_v - own_v) * infl
    infl = kf.out_infl if side == 'out' else kf.in_infl
    if not (0.0 < infl <= 0.5):
        infl = 1.0 / 3.0
    spat = 0.0
    if axis == 'x':
        spat = kf.ox if side == 'out' else kf.ix
    elif axis == 'y':
        spat = kf.oy if side == 'out' else kf.iy
    if spat != 0.0:
        return infl, spat
    speed = kf.out_speed if side == 'out' else kf.in_speed
    if speed:
        direction = 1.0 if other_v >= own_v else -1.0
        off = abs(speed) * infl * dt * direction
        return infl, off if side == 'out' else -off
    return infl, 0.0


def segment_pieces(k0, k1, axis, v0, v1, t0, t1):
    """Polynomial pieces (t0, t1, coeffs) of the eased/linear transition k0 -> k1."""
    dt = t1 - t0
    if dt <= 1e-9:
        return []
    a, o0 = side_handles(k0, v1, v0, dt, axis, 'out')
    b, o1 = side_handles(k1, v0, v1, dt, axis, 'in')
    dv = v1 - v0
    tol = 1e-9 * max(abs(dv), 1e-9) + 1e-12
    if abs(o0 - dv * a) <= tol and abs(o1 + dv * b) <= tol:
        slope = dv / dt
        return [(t0, t1, (v0 - slope * t0, slope, 0.0, 0.0))]
    if abs(a - 1.0 / 3.0) < 1e-12 and abs(b - 1.0 / 3.0) < 1e-12:
        return [(t0, t1, bezier_poly(v0, o0, v1, o1, t0, dt))]
    pieces = []
    prev = None
    n = 24
    for j in range(n + 1):
        u = j / n
        tu = ((1 - u) ** 3) * t0 + 3 * ((1 - u) ** 2) * u * (t0 + a * dt) \
            + 3 * (1 - u) * (u ** 2) * (t1 - b * dt) + (u ** 3) * t1
        vu = ((1 - u) ** 3) * v0 + 3 * ((1 - u) ** 2) * u * (v0 + o0) \
            + 3 * (1 - u) * (u ** 2) * (v1 + o1) + (u ** 3) * v1
        if prev is not None and tu > prev[0] + 1e-9:
            slope = (vu - prev[1]) / (tu - prev[0])
            pieces.append((prev[0], tu, (prev[1] - slope * prev[0], slope, 0.0, 0.0)))
        prev = (tu, vu)
    return pieces


def build_path(param, comp, start, end, default_v):
    """Value path v(t) for one component, as polynomial pieces covering [0, duration].

    comp: 'x'/'y' for point params (Motion Position), 'v' for scalar params
    (Scale, Rotation). Keyframe times are absolute in the XML, times returned are
    item-local.
    """
    dur = end - start
    static_v = default_v
    if param is not None:
        if param.kind == 'point':
            if param.static:
                static_v = param.static[0] if comp == 'x' else param.static[1]
        elif param.static is not None:
            static_v = param.static
    if comp == 'x' or comp == 'v':
        comp_v = (lambda k: k.x)
    else:
        comp_v = (lambda k: k.y)
    kfs = []
    if param is not None:
        for kf in param.kfs:
            if start - 1e-6 <= kf.t <= end + 1e-6:
                kfs.append((kf.t - start, kf))
    kfs.sort(key=lambda k: k[0])
    if not kfs:
        return [(0.0, dur, (static_v, 0.0, 0.0, 0.0))]
    intervals = []
    if kfs[0][0] > 1e-6:
        intervals.append((0.0, kfs[0][0], (static_v, 0.0, 0.0, 0.0)))
    for i in range(len(kfs) - 1):
        t0, k0 = kfs[i]
        t1, k1 = kfs[i + 1]
        intervals.extend(segment_pieces(k0, k1, comp, comp_v(k0), comp_v(k1), t0, t1))
    if dur - kfs[-1][0] > 1e-6:
        intervals.append((kfs[-1][0], dur, (comp_v(kfs[-1][1]), 0.0, 0.0, 0.0)))
    return intervals


def axis_path(item, axis):
    """Position path for the item's x or y axis (local times)."""
    return build_path(item.position, axis, item.start, item.end,
                      0.5 if axis == 'x' else 0.5)


def scalar_path(item, param, default_v):
    """Scalar param path (Scale, Rotation) for the item (local times)."""
    return build_path(param, 'v', item.start, item.end, default_v)


def has_animated_scale(item):
    """True si l'echelle (largeur ou hauteur) porte des keyframes."""
    return ((item.scale is not None and bool(item.scale.kfs))
            or (item.scale2 is not None and bool(item.scale2.kfs)))


def has_animated_transform(item):
    """True si l'echelle ou la rotation porte des keyframes."""
    return has_animated_scale(item) or (
        item.rotation is not None and bool(item.rotation.kfs))


def item_needs_canvas(item):
    """True when the crop-window path cannot render the item faithfully.

    Rotation, animated scale, non-uniform scale, opacity < 100 % or alpha-bearing
    media (.mov) require the compositing path (scaled/rotated clip overlaid onto
    the frame).
    """
    if has_animated_scale(item):
        return True
    if item.rotation is not None:
        if item.rotation.kfs:
            return True
        if item.rotation.static and abs(item.rotation.static) > 1e-9:
            return True
    if abs(item.scale_w - item.scale_h) > 1e-9:
        return True
    opacity = _opacity_scalar(item.opacity)
    if opacity is not None and opacity < 0.999:
        return True
    source_path = item.clip.source_path if item.clip is not None else ''
    return (source_path or '').lower().endswith('.mov')


def scale_dims(item, src_w, src_h):
    return (max(1, int(round(src_w * item.scale_w / 100.0))),
            max(1, int(round(src_h * item.scale_h / 100.0))))


def scale_expr_pair(item, src_w, src_h, start, end):
    anim_w = item.scale is not None and bool(item.scale.kfs)
    anim_h = item.scale2 is not None and bool(item.scale2.kfs)
    if not (anim_w or anim_h):
        return None, None, False
    if item.uniform_scale and anim_w != anim_h:
        # Premiere n'enregistre les keyframes que sur un axe quand « Echelle
        # uniforme » est cochee : le meme chemin pilote alors les deux axes.
        param = item.scale if anim_w else item.scale2
        default = item.scale_w if anim_w else item.scale_h
        path = build_path(param, 'v', start, end, default)
        w_path = h_path = path
    else:
        w_path = build_path(item.scale, 'v', start, end, item.scale_w)
        h_path = build_path(item.scale2, 'v', start, end, item.scale_h)
    w_expr = axis_expr(w_path, src_w / 100.0, 0.0, -1e12, 1e12)
    h_expr = axis_expr(h_path, src_h / 100.0, 0.0, -1e12, 1e12)
    return w_expr, h_expr, True


def rotation_expr(item, start, end):
    """Rotation angle expression in radians (or None when no rotation)."""
    if item.rotation is None:
        return None
    if item.rotation.kfs:
        rp = build_path(item.rotation, 'v', start, end, item.rotation.static or 0.0)
        return axis_expr(rp, math.pi / 180.0, 0.0, -1e9, 1e9)
    deg = item.rotation.static or 0.0
    if abs(deg) > 1e-9:
        return f"{deg * math.pi / 180.0:.9f}"
    return None


def transform_filters(item, src_w, src_h, start, end):
    """scale (and rotate when needed) filter string for the item's transform."""
    parts = []
    w_expr, h_expr, anim = scale_expr_pair(item, src_w, src_h, start, end)
    sw, sh = scale_dims(item, src_w, src_h)
    rot = rotation_expr(item, start, end)
    if anim:
        parts.append(f"scale=w='{w_expr}':h='{h_expr}':eval=frame:flags=bicubic")
    elif (sw, sh) != (int(src_w), int(src_h)):
        parts.append(f"scale={sw}:{sh}:flags=bicubic")
    if rot:
        # c=none : les coins degages par la rotation doivent etre transparents,
        # pas noirs opaques (format=rgba doit preceder rotate).
        parts.append(f"format=rgba,rotate=a='{rot}':c=none")
    return ','.join(parts)


def overlay_xy_exprs(item, src_w, src_h, seq_w, seq_h, start, end):
    """Overlay x/y expressions placing the transformed clip so the anchor lands at Position."""
    px = axis_expr(build_path(item.position, 'x', start, end, 0.5), seq_w, 0.0, -1e12, 1e12)
    py = axis_expr(build_path(item.position, 'y', start, end, 0.5), seq_h, 0.0, -1e12, 1e12)
    w_expr, h_expr, anim = scale_expr_pair(item, src_w, src_h, start, end)
    if anim:
        sw_s, sh_s = f"({w_expr})", f"({h_expr})"
    else:
        sw, sh = scale_dims(item, src_w, src_h)
        sw_s, sh_s = f"{sw}", f"{sh}"
    rot = rotation_expr(item, start, end)
    a_x, a_y = item.anchor
    if rot:
        cx = f"({sw_s}/2)"
        cy = f"({sh_s}/2)"
        axs = f"({a_x:.9f}*{sw_s})"
        ays = f"({a_y:.9f}*{sh_s})"
        x = f"({px}-({cx}+cos({rot})*({axs}-{cx})-sin({rot})*({ays}-{cy})))"
        y = f"({py}-({cy}+sin({rot})*({axs}-{cx})+cos({rot})*({ays}-{cy})))"
    else:
        x = f"({px}-{a_x:.9f}*{sw_s})"
        y = f"({py}-{a_y:.9f}*{sh_s})"
    return x, y


def poly_expr(coeffs):
    d0, d1, d2, d3 = coeffs
    if d1 == 0.0 and d2 == 0.0 and d3 == 0.0:
        return f"{d0:.6f}"
    if d2 == 0.0 and d3 == 0.0:
        return f"({d0:.6f}{d1:+.6f}*t)"
    s = f"{d3:.6f}"
    s = f"({s}*t{d2:+.6f})"
    s = f"({s}*t{d1:+.6f})"
    return f"({s}*t{d0:+.6f})"


def axis_expr(intervals, alpha, beta, lo, hi):
    """Pixel coordinate expression: alpha * v(t) + beta, constants clamped to [lo, hi].

    La selection d'intervalle est un arbre binaire equilibre : profondeur
    ceil(log2(N)) au lieu de N, pour une semantique identique aux bornes
    (t == fin d'intervalle bascule a droite, comme la chaine lineaire d'origine).
    """
    folded = []
    for _t0, _t1, coeffs in intervals:
        c = (alpha * coeffs[0] + beta, alpha * coeffs[1], alpha * coeffs[2], alpha * coeffs[3])
        if c[1] == 0.0 and c[2] == 0.0 and c[3] == 0.0:
            c = (max(lo, min(hi, c[0])), 0.0, 0.0, 0.0)
        folded.append(c)
    if not folded:
        return '0.0'

    def build(lo_i, hi_i):
        if lo_i == hi_i:
            return poly_expr(folded[lo_i])
        mid = (lo_i + hi_i) // 2
        return (f"if(lt(t,{intervals[mid][1]:.6f}),"
                f"{build(lo_i, mid)},{build(mid + 1, hi_i)})")
    return build(0, len(folded) - 1)


def clip_times(item):
    cin = item.clip.in_sec if item.clip.in_sec else 0.0
    dur = item.clip.duration(item.end - item.start)
    return cin, dur


# ------------------------------------------------------- media cache / analysis

_probe_cache = {}
_alpha_cache = {}
_bbox_cache = {}
_alpha_limits = {}
_duration_cache = {}
_content_key_cache = {}

# L'intermédiaire alpha et l'analyse bbox ne couvrent que le préfixe réellement affiché,
# arrondi à la demi-seconde (au plus 0,5 s de transcodage supplémentaire par média).
_ALPHA_CAP_STEP = 0.5
# Empreinte de contenu : tout le fichier jusqu'à 4 Mio, sinon premiers + derniers 4 Mio.
_CONTENT_HASH_CHUNK = 4 << 20
_CACHE_MAX_MB_ENV = 'PRPROJ2FFMPEG_CACHE_MAX_MB'
_CACHE_DEFAULT_MAX_MB = 2048
# Métadonnées des intermédiaires qtrle : plage convertie, empreinte de contenu, durée.
_ALPHA_META_SUFFIX = '.qtrle.json'
# Une queue inutilisée plus courte que ce seuil est convertie avec le préfixe : le
# fichier pleine durée pourra servir aux séquences suivantes du même média.
_ALPHA_FULL_TAIL_MAX = 15.0
_ALPHA_FULL_TAIL_MAX_ENV = 'PRPROJ2FFMPEG_ALPHA_FULL_TAIL_MAX'


def _alpha_full_tail_max():
    """Seuil (secondes) au-delà duquel la conversion reste bornée à la plage utilisée."""
    raw = os.environ.get(_ALPHA_FULL_TAIL_MAX_ENV, '').strip()
    if not raw:
        return _ALPHA_FULL_TAIL_MAX
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _ALPHA_FULL_TAIL_MAX


def _cache_key(path, tag):
    try:
        st = os.stat(path)
    except OSError:
        return None
    raw = f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{tag}"
    return hashlib.sha1(raw.encode('utf-8', 'surrogateescape')).hexdigest()[:20]


def _content_signature(path):
    """Empreinte légère du contenu (taille + premiers/derniers 4 Mio), ou None.

    Elle reconnaît un même média copié dans un autre dossier projet, dont le chemin
    et la date de modification diffèrent : le cache alpha/bbox peut alors être réutilisé.
    """
    if path in _content_key_cache:
        return _content_key_cache[path]
    try:
        st = os.stat(path)
        digest = hashlib.sha1()
        digest.update(str(st.st_size).encode('ascii'))
        with open(path, 'rb') as fh:
            digest.update(fh.read(_CONTENT_HASH_CHUNK))
            if st.st_size > _CONTENT_HASH_CHUNK:
                fh.seek(max(_CONTENT_HASH_CHUNK, st.st_size - _CONTENT_HASH_CHUNK))
                digest.update(fh.read(_CONTENT_HASH_CHUNK))
        signature = digest.hexdigest()
    except OSError:
        return None
    _content_key_cache[path] = signature
    return signature


def _cache_keys(path, tag):
    """Clés de cache candidates : rapide (chemin + mtime) puis contenu (copies).

    La première répond aux exports répétés sans copie ; la seconde permet de partager
    un intermédiaire entre plusieurs dossiers projet. L'écriture utilise la dernière
    (contenu) afin que les copies suivantes la retrouvent.
    """
    keys = []
    fast = _cache_key(path, tag)
    if fast:
        keys.append(fast)
    signature = _content_signature(path)
    if signature:
        content = hashlib.sha1(f"{signature}|{tag}".encode('utf-8', 'surrogateescape')).hexdigest()[:20]
        if content not in keys:
            keys.append(content)
    return keys


def probe_duration(path):
    """Durée du média en secondes, ou 0.0 si inconnue (cache mémoire)."""
    if path in _duration_cache:
        return _duration_cache[path]
    duration = 0.0
    try:
        out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                              '-of', 'default=noprint_wrappers=1:nokey=1', path],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        duration = float(out)
    except Exception:
        duration = 0.0
    _duration_cache[path] = duration
    return duration


def _alpha_cap(path):
    """Fin (arrondie) du préfixe source réellement utilisé, ou None pour tout le fichier."""
    end = _alpha_limits.get(os.path.abspath(path))
    if end is None:
        return None
    duration = probe_duration(path)
    if duration and end >= duration - 0.1:
        return None
    return max(_ALPHA_CAP_STEP, math.ceil(end / _ALPHA_CAP_STEP) * _ALPHA_CAP_STEP)


def _note_alpha_end(path, end, bases):
    """Mémorise la fin de plage utilisée d'un média (maximum de tous ses usages)."""
    resolved = resolve_media(path, bases)
    if not resolved or end is None:
        return
    key = os.path.abspath(resolved)
    _alpha_limits[key] = max(_alpha_limits.get(key, 0.0), float(end))


def _alpha_meta_path(mov_path):
    return mov_path + _ALPHA_META_SUFFIX


def _read_alpha_meta(meta_path):
    try:
        with open(meta_path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _write_alpha_meta(mov_path, source, cap, duration):
    """Écrit le sidecar d'un intermédiaire qtrle (best effort).

    `cap` vaut None pour un intermédiaire pleine durée ; la durée enregistrée est
    alors celle de la source. Les copies du même média sont reconnues par l'empreinte
    de contenu, comme les clés de cache existantes.
    """
    data = {
        'file': os.path.basename(mov_path),
        'source': os.path.abspath(source),
        'content': _content_signature(source),
        'cap': cap,
        'duration': duration,
    }
    meta_path = _alpha_meta_path(mov_path)
    tmp = meta_path + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh)
        os.replace(tmp, meta_path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _find_alpha_intermediate(path, cap):
    """(chemin, cap, durée) d'un intermédiaire au moins aussi long que `cap`, ou None.

    Un intermédiaire pleine durée convient à toute plage ; sinon on prend le plus
    court des intermédiaires bornés dont la plage couvre la demande. Le sidecar
    permet de retrouver les entrées créées avec une autre plage (même contenu).
    """
    if not os.path.isdir(CACHE_ROOT):
        return None
    signature = _content_signature(path)
    source = os.path.abspath(path)
    best = None
    for name in os.listdir(CACHE_ROOT):
        if not name.endswith(_ALPHA_META_SUFFIX):
            continue
        meta_path = os.path.join(CACHE_ROOT, name)
        meta = _read_alpha_meta(meta_path)
        if not meta:
            continue
        same = ((signature is not None and meta.get('content') == signature)
                or (signature is None and meta.get('source') == source))
        if not same:
            continue
        raw_cap = meta.get('cap')
        if raw_cap is None:
            entry_cap = None
        elif isinstance(raw_cap, (int, float)):
            entry_cap = float(raw_cap)
        else:
            continue
        if cap is None:
            if entry_cap is not None:
                continue
            score = float('inf')
        elif entry_cap is not None and entry_cap + 1e-6 >= cap:
            score = entry_cap
        elif entry_cap is None:
            score = float('inf')
        else:
            continue
        file_name = meta.get('file')
        if not isinstance(file_name, str) or not file_name:
            file_name = name[:-len(_ALPHA_META_SUFFIX)] + '.mov'
        candidate = os.path.join(CACHE_ROOT, file_name)
        if not os.path.isfile(candidate):
            try:
                os.remove(meta_path)
            except OSError:
                pass
            continue
        if best is None or score < best[0]:
            best = (score, candidate, entry_cap, meta.get('duration'))
    if best is None:
        return None
    try:
        os.utime(best[1])
    except OSError:
        pass
    return best[1], best[2], best[3]


def _prune_cache(protect=()):
    """Purge LRU du cache disque au-delà de PRPROJ2FFMPEG_CACHE_MAX_MB (défaut 2048 Mo).

    Le mtime est rafraîchi à chaque lecture : les entrées inutilisées partent d'abord.
    `protect` contient les fichiers du graphe courant, jamais supprimés. Les `.tmp.mov`
    et `.bbox.json.tmp` orphelins de plus de 24 h sont toujours supprimés.
    """
    protected = {os.path.abspath(path) for path in protect}
    raw = os.environ.get(_CACHE_MAX_MB_ENV, '').strip()
    try:
        max_mb = float(raw) if raw else _CACHE_DEFAULT_MAX_MB
    except ValueError:
        max_mb = _CACHE_DEFAULT_MAX_MB
    if max_mb <= 0 or not os.path.isdir(CACHE_ROOT):
        return
    limit = int(max_mb * 1024 * 1024)
    now = time.time()
    entries = []
    total = 0
    freed = 0
    for name in os.listdir(CACHE_ROOT):
        if not (name.endswith('.mov') or name.endswith('.bbox.json')
                or name.endswith('.tmp.mov') or name.endswith('.bbox.json.tmp')):
            continue
        fp = os.path.join(CACHE_ROOT, name)
        try:
            st = os.stat(fp)
        except OSError:
            continue
        if not os.path.isfile(fp):
            continue
        if name.endswith('.tmp.mov') or name.endswith('.bbox.json.tmp'):
            if now - st.st_mtime > 86400:
                try:
                    os.remove(fp)
                    freed += st.st_size
                except OSError:
                    pass
            continue
        entries.append((st.st_mtime, st.st_size, fp))
        total += st.st_size
    if total > limit:
        entries.sort()
        for _mtime, size, fp in entries:
            if total <= limit:
                break
            if os.path.abspath(fp) in protected:
                continue
            try:
                os.remove(fp)
                if fp.endswith('.mov'):
                    try:
                        os.remove(_alpha_meta_path(fp))
                    except OSError:
                        pass
                total -= size
                freed += size
            except OSError:
                pass
    # Sidecars orphelins (intermédiaire supprimé ou déplacé).
    try:
        names = os.listdir(CACHE_ROOT)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(_ALPHA_META_SUFFIX):
            continue
        meta_path = os.path.join(CACHE_ROOT, name)
        meta = _read_alpha_meta(meta_path)
        file_name = meta.get('file') if meta else None
        if not isinstance(file_name, str) or not file_name:
            file_name = name[:-len(_ALPHA_META_SUFFIX)] + '.mov'
        if not os.path.exists(os.path.join(CACHE_ROOT, file_name)):
            try:
                os.remove(meta_path)
            except OSError:
                pass
    if freed:
        print(f"[cache] purge LRU : {freed / (1024 * 1024):.0f} Mo libérés "
              f"(plafond {max_mb:.0f} Mo)", file=sys.stderr)


def probe_video(path):
    """(codec_name, pix_fmt, width, height) of the first video stream, or Nones."""
    if path in _probe_cache:
        return _probe_cache[path]
    result = (None, None, 0, 0)
    try:
        out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                              '-show_entries', 'stream=codec_name,pix_fmt,width,height',
                              '-of', 'default=noprint_wrappers=1', path],
                             capture_output=True, text=True, timeout=60).stdout
        vals = dict(line.split('=', 1) for line in out.strip().splitlines() if '=' in line)
        result = (vals.get('codec_name'), vals.get('pix_fmt'),
                  int(vals.get('width') or 0), int(vals.get('height') or 0))
    except Exception:
        pass
    _probe_cache[path] = result
    return result


def _pix_has_alpha(pix):
    if not pix:
        return False
    return any(tok in pix for tok in ('yuva', 'rgba', 'argb', 'bgra', 'abgr', 'gbrap', 'ya8', 'ya16'))


def alpha_intermediate(path):
    """8-bit qtrle copy of an alpha-bearing video (cached), else the original path.

    ProRes 4444 sources are 10/12-bit; an intra-only 8-bit qtrle intermediate decodes
    and converts several times faster while staying visually identical for overlays
    (alpha preserved bit-exactly, colour quantised 12->8 bit). Seul le préfixe
    réellement utilisé de la source est converti (`_alpha_cap`), jamais les minutes
    de rushes qui ne sont pas montées.
    """
    codec, pix, _w, _h = probe_video(path)
    if not pix or not _pix_has_alpha(pix):
        _alpha_cache[(path, None)] = path
        return path
    cap = _alpha_cap(path)
    memo_key = (path, cap)
    if memo_key in _alpha_cache:
        return _alpha_cache[memo_key]
    if codec == 'qtrle':
        _alpha_cache[memo_key] = path
        if cap is not None:
            _alpha_limits[os.path.abspath(path)] = cap
        return path
    full_duration = probe_duration(path)
    tail_max = _alpha_full_tail_max()
    convert_full = cap is None or (
        full_duration > 0 and 0 < full_duration - cap <= tail_max)
    tag = 'qtrle8' if convert_full else f'qtrle8:{cap:.3f}'
    keys = _cache_keys(path, tag)
    cached = None
    if keys:
        os.makedirs(CACHE_ROOT, exist_ok=True)
        # Un intermédiaire pleine durée déjà présent reste valable (et plus riche) :
        # l'analyse bbox sera simplement bornée par _alpha_cap.
        lookup = [] if convert_full else _cache_keys(path, 'qtrle8')
        for key in dict.fromkeys(lookup + keys):
            candidate = os.path.join(CACHE_ROOT, key + '.mov')
            if os.path.exists(candidate):
                cached = candidate
                try:
                    os.utime(cached)
                except OSError:
                    pass
                break
        if cached is None and cap is not None:
            found = _find_alpha_intermediate(path, cap)
            if found is not None:
                cached, found_cap, found_duration = found
                label = (found_duration if isinstance(found_duration, (int, float))
                         and found_duration > 0 else found_cap)
                label = 'durée complète' if label is None else f'{label:.1f} s'
                print(f"[cache] réutilisation de l'intermédiaire alpha de "
                      f"'{os.path.basename(path)}' ({label} pour {cap:.1f} s demandés)",
                      file=sys.stderr)
                sys.stderr.flush()
        if cached is None:
            key = keys[-1]
            cached = os.path.join(CACHE_ROOT, key + '.mov')
            tmp = cached + '.tmp.mov'
            coverage = 'durée complète' if convert_full else f'0-{cap:.1f} s'
            started = time.monotonic()
            print(f"[cache] conversion alpha 8 bits de '{os.path.basename(path)}' "
                  f"({coverage})…", file=sys.stderr)
            sys.stderr.flush()
            try:
                cmd = ['ffmpeg', '-y', '-hide_banner', '-v', 'error',
                       '-i', path, '-map', '0:v:0', '-an']
                if not convert_full:
                    cmd += ['-t', f'{cap:.6f}']
                cmd += ['-c:v', 'qtrle', tmp]
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
                for line in proc.stderr.splitlines():
                    if 'out of range for mov' not in line and line.strip():
                        print(line, file=sys.stderr)
                if proc.returncode == 0 and os.path.exists(tmp):
                    os.replace(tmp, cached)
                    _write_alpha_meta(cached, path,
                                      None if convert_full else cap,
                                      full_duration if convert_full else cap)
                    print(f"[cache] 8-bit alpha intermediate for '{os.path.basename(path)}' "
                          f"({coverage} en {time.monotonic() - started:.1f} s)",
                          file=sys.stderr)
                else:
                    cached = None
            except Exception:
                cached = None
            if cached is None and os.path.exists(tmp):
                os.remove(tmp)
    effective = cached or path
    if cap is not None:
        _alpha_limits[os.path.abspath(effective)] = cap
    _alpha_cache[memo_key] = effective
    return effective


_ALPHA_MIN = 2  # alpha <= _ALPHA_MIN : pixel considere transparent


def _pad_box(raw, w, h):
    """Boite (x, y, w, h) elargie de 2 px de marge, alignee sur 2 et bornee a l'image.

    Retourne None si la boite est vide ou couvre deja toute l'image (recadrage inutile).
    """
    x, y, bw, bh = raw
    px = max(0, x - 2)
    py = max(0, y - 2)
    pw = min(w - px, bw + 4) // 2 * 2
    ph = min(h - py, bh + 4) // 2 * 2
    if pw <= 0 or ph <= 0 or (pw >= w and ph >= h):
        return None
    return [px, py, pw, ph]


def _bbox_scan(plane, w, h, min_alpha=_ALPHA_MIN):
    """Boite brute (x, y, w, h) des pixels d'alpha > min_alpha d'un plan gray, ou None.

    Balayage exact, ligne a ligne : pas de seuil sur la moyenne des lignes (c'est ce qui
    fait sous-estimer la boite par cropdetect sur un artwork fin et creux).
    """
    table = bytes(0 if value <= min_alpha else 255 for value in range(256))
    masked = plane[:w * h].translate(table)
    x0, x1, y0, y1 = w, -1, None, None
    for y in range(h):
        row = masked[y * w:(y + 1) * w]
        first = row.find(b'\xff')
        if first < 0:
            continue
        last = row.rfind(b'\xff')
        if y0 is None:
            y0 = y
        y1 = y
        x0 = min(x0, first)
        x1 = max(x1, last)
    if y0 is None:
        return None
    return (x0, y0, x1 - x0 + 1, y1 - y0 + 1)


def _bbox_cache_read(path, tag):
    """Boîte en cache (liste, éventuellement vide), ou None si absente.

    Une liste vide est un résultat négatif valide (« aucun recadrage ») : elle évite de
    relancer cropdetect à chaque export. L'ancien format `null` est traité comme absent
    et sera réécrit en `[]`.
    """
    for key in _cache_keys(path, tag):
        fp = os.path.join(CACHE_ROOT, key + '.bbox.json')
        try:
            with open(fp) as fh:
                data = json.load(fh)
        except Exception:
            continue
        if data is None:
            continue
        try:
            os.utime(fp)
        except OSError:
            pass
        return data
    return None


def _bbox_cache_write(path, tag, box):
    keys = _cache_keys(path, tag)
    if not keys:
        return
    fp = os.path.join(CACHE_ROOT, keys[-1] + '.bbox.json')
    tmp = fp + '.tmp'
    try:
        os.makedirs(CACHE_ROOT, exist_ok=True)
        with open(tmp, 'w') as fh:
            json.dump([] if box is None else box, fh)
        os.replace(tmp, fp)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


def alpha_bbox(path):
    """Union alpha bounding box (x, y, w, h) of a media's frames (cached), or None.

    Heuristique adaptee aux videos : cropdetect rogne selon la *moyenne* des lignes et
    colonnes de bord (et non pixel a pixel, malgre le nom du seuil), donc un artwork fin
    est rogne au-dela de son contenu. Les images fixes passent par `alpha_bbox_exact`.
    2px of padding is added. Seul le préfixe utilisé est analysé (`_alpha_cap`).
    """
    cap = _alpha_cap(path)
    tag = 'bbox2' if cap is None else f'bbox2:{cap:.3f}'
    memo_key = (path, tag)
    if memo_key in _bbox_cache:
        return _bbox_cache[memo_key]
    box = None
    found = False
    # Une boîte calculée sur tout le fichier reste valable (sur-ensemble de la plage).
    for candidate_tag in dict.fromkeys([tag, 'bbox2'] if cap is not None else [tag]):
        cached = _bbox_cache_read(path, candidate_tag)
        if cached is not None:
            box = cached
            found = True
            break
    if not found:
        _c, _p, w, h = probe_video(path)
        if w and h:
            coverage = 'durée complète' if cap is None else f'0-{cap:.1f} s'
            started = time.monotonic()
            print(f"[bbox] analyse alpha de '{os.path.basename(path)}' ({coverage})…",
                  file=sys.stderr)
            sys.stderr.flush()
            cmd = ['ffmpeg', '-hide_banner']
            if cap is not None:
                cmd += ['-t', f'{cap:.6f}']
            cmd += ['-i', path, '-vf',
                    f'alphaextract,format=gray,cropdetect=limit={_ALPHA_MIN}:round=2:skip=0',
                    '-f', 'null', '-']
            try:
                out = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=1800).stderr
            except Exception:
                out = ''
            minx = miny = 10 ** 9
            maxx = maxy = -1
            for m in re.finditer(r'crop=(\d+):(\d+):(\d+):(\d+)', out):
                cw, ch, cx, cy = (int(g) for g in m.groups())
                minx = min(minx, cx)
                miny = min(miny, cy)
                maxx = max(maxx, cx + cw)
                maxy = max(maxy, cy + ch)
            if maxx > minx and maxy > miny:
                box = _pad_box((minx, miny, maxx - minx, maxy - miny), w, h)
            print(f"[bbox] analyse alpha terminée pour '{os.path.basename(path)}' "
                  f"({time.monotonic() - started:.1f} s)"
                  f"{'' if box else ' : aucun recadrage'}", file=sys.stderr)
            _bbox_cache_write(path, tag, box)
    _bbox_cache[memo_key] = box
    return box


def alpha_bbox_exact(path):
    """Boite alpha exacte (x, y, w, h) d'une image fixe (cached), ou None.

    Chaque pixel d'alpha > _ALPHA_MIN est pris en compte (plan alpha brut, balaye en
    Python) : aucune sous-detection possible, contrairement a `alpha_bbox`. En cas
    d'echec de lecture, retourne None (aucun recadrage plutot qu'un recadrage a tort).
    """
    if (path, 'bboxpix1') in _bbox_cache:
        return _bbox_cache[(path, 'bboxpix1')]
    box = _bbox_cache_read(path, 'bboxpix1')
    if box is None:
        _c, _p, w, h = probe_video(path)
        if w and h:
            plane = b''
            try:
                plane = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vf',
                                        'alphaextract,format=gray', '-frames:v', '1',
                                        '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                                       capture_output=True, timeout=1800).stdout
            except Exception:
                plane = b''
            if len(plane) >= w * h:
                raw = _bbox_scan(plane, w, h)
                if raw:
                    box = _pad_box(raw, w, h)
            _bbox_cache_write(path, 'bboxpix1', box)
    _bbox_cache[(path, 'bboxpix1')] = box
    return box


def transform_is_identity(item):
    if item.scale is not None and item.scale.kfs:
        return False
    if item.scale2 is not None and item.scale2.kfs:
        return False
    if item.rotation is not None and (item.rotation.kfs or abs(item.rotation.static or 0.0) > 1e-9):
        return False
    return abs(item.scale_w - 100.0) < 1e-9 and abs(item.scale_h - 100.0) < 1e-9


def bbox_crop_part(item, media_path, src_w, src_h, exact=False):
    """(crop filter, dx, dy) trimming an identity-transformed alpha overlay to its bbox.

    Only alpha-bearing media are cropped (opaque black borders must composite as
    black, not become transparent), and only when the item has no scale/rotation
    transform, so the crop offset maps straight onto the overlay position.
    `exact` (images fixes) mesure la boite au pixel ; sinon l'union heuristique de
    cropdetect est conservee (videos alpha). Un flou est exclu : le crop au ras de
    la boite alpha couperait sa diffusion hors du masque.
    """
    if item.blur is not None or not transform_is_identity(item):
        return None, 0, 0
    _c, pix, _w, _h = probe_video(media_path)
    if not _pix_has_alpha(pix):
        return None, 0, 0
    box = alpha_bbox_exact(media_path) if exact else alpha_bbox(media_path)
    if not box:
        return None, 0, 0
    x, y, w, h = box
    print(f"[bbox] recadrage alpha de '{os.path.basename(media_path)}': {w}x{h}+{x}+{y}",
          file=sys.stderr)
    return f"crop={w}:{h}:{x}:{y}", x, y


def _opacity_scalar(param):
    """Valeur normalisee 0..1 d'un composant Opacity (scalaire float stocke en 0..100,
    ou ancienne forme point). Retourne None si absent."""
    if param is None or param.static is None:
        return None
    value = param.static
    if isinstance(value, (tuple, list)):
        value = value[0]
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value > 1.0:
        value = value / 100.0
    return value


# --------------------------------------------------------------- masques / flou

# Conversion « Amount » (Gaussian Blur Premiere) -> sigma ffmpeg (gblur), calibrée visuellement
# sur le projet de test (Amount 20 -> sigma 60, silhouette méconnaissable comme dans Premiere).
_BLUR_SIGMA_FACTOR = 3.0


def crop_window_fits(item, seq_w, seq_h, isw, ish):
    """Vrai si le chemin « fenêtre de crop » peut rendre l'item fidèlement.

    La fenêtre doit tenir dans la source ET le cadrage demandé (position, éventuellement
    animée) doit rester dans les limites : sinon le calcul de la fenêtre est borné
    (clamp) et le rendu se décale par rapport à Premiere (cas des split screens).
    """
    if item.scale is not None and item.scale.kfs:
        return False
    if item.scale2 is not None and item.scale2.kfs:
        return False
    scale = abs(item.scale_w) / 100.0
    if scale <= 1e-9:
        return False
    w_win = int(round(seq_w / scale))
    h_win = int(round(seq_h / scale))
    if w_win > isw or h_win > ish:
        return False

    xs, ys = [0.5], [0.5]
    if item.position is not None:
        if item.position.static:
            xs, ys = [item.position.static[0]], [item.position.static[1]]
        for kf in item.position.kfs:
            xs.append(kf.x)
            ys.append(kf.y)

    a_x = item.anchor[0] * isw
    a_y = item.anchor[1] * ish
    beta_x = a_x + seq_w / (2.0 * scale) - w_win / 2.0
    beta_y = a_y + seq_h / (2.0 * scale) - h_win / 2.0
    hi_x = isw - w_win
    hi_y = ish - h_win
    tol = 1.0
    for v in xs:
        cx = -seq_w / scale * v + beta_x
        if cx < -tol or cx > hi_x + tol:
            return False
    for v in ys:
        cy = -seq_h / scale * v + beta_y
        if cy < -tol or cy > hi_y + tol:
            return False
    return True


def blur_chain(blur, width=None, height=None):
    """Filtre ffmpeg approximant le Gaussian Blur de Premiere (Amount/Thickness).

    Pour les flous prononcés (ex. habillage / arrière-plan flou), une décimation
    pyramidale bilinéaire préalable (k=4 ou k=2) réduit considérablement l'empreinte
    mémoire et le coût CPU sans perte visuelle (les hautes fréquences étant éliminées
    par le flou lui-même). La résolution exacte de sortie n'est restauration que si
    `width`/`height` sont connus ; sinon le gblur est appliqué directement pour ne
    jamais dériver de quelques pixels (cas des zooms animés).
    """
    sigma = max(0.1, blur.amount * _BLUR_SIGMA_FACTOR)
    if width is None or height is None:
        return f"gblur=sigma={sigma:.3f}:steps=2"
    width, height = int(width), int(height)
    if sigma >= 24.0:
        # Flou fort : décimation 1/4 (surface divisée par 16)
        s4 = max(0.1, sigma / 4.0)
        return (f"scale=w='max(2,trunc(iw/8)*2)':h='max(2,trunc(ih/8)*2)':"
                f"flags=fast_bilinear,"
                f"gblur=sigma={s4:.3f}:steps=2,"
                f"scale={width}:{height}:flags=bilinear")
    elif sigma >= 6.0:
        # Flou moyen : décimation 1/2 (surface divisée par 4)
        s2 = max(0.1, sigma / 2.0)
        return (f"scale=w='max(2,trunc(iw/4)*2)':h='max(2,trunc(ih/4)*2)':"
                f"flags=fast_bilinear,"
                f"gblur=sigma={s2:.3f}:steps=2,"
                f"scale={width}:{height}:flags=bilinear")
    return f"gblur=sigma={sigma:.3f}:steps=2"


def _mask_bbox_px(mask, w, h, clamp=True):
    """Bounding box d'un masque en pixels source (expansion comprise).

    `clamp=True` borne la boîte au cadre (rectangle, union de masques, fenêtre
    visible). `clamp=False` conserve la géométrie réelle d'une ellipse qui déborde :
    borner avant de calculer son centre et ses rayons la déformerait.
    """
    x0, y0, x1, y1 = mask.bbox
    x0, y0 = x0 * w - mask.expansion, y0 * h - mask.expansion
    x1, y1 = x1 * w + mask.expansion, y1 * h + mask.expansion
    if not clamp:
        return x0, y0, x1, y1
    x0 = max(0.0, min(x0, x1 - 2.0))
    y0 = max(0.0, min(y0, y1 - 2.0))
    x1 = min(float(w), max(x1, x0 + 2.0))
    y1 = min(float(h), max(y1, y0 + 2.0))
    return x0, y0, x1, y1


def _ellipse_geometry(mask, w, h):
    """(cx, cy, rx, ry) d'une ellipse, calculés sans bornage au cadre.

    `geq` borne naturellement le rendu au cadre : une ellipse qui déborde garde son
    centre et ses rayons réels, seules les parties visibles sont rastérisées.
    """
    x0, y0, x1, y1 = _mask_bbox_px(mask, w, h, clamp=False)
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    rx = max(1.0, (x1 - x0) / 2.0)
    ry = max(1.0, (y1 - y0) / 2.0)
    return cx, cy, rx, ry


def _mask_union_bbox_px(item, w, h):
    """Union of mask boxes in source pixels, or None (no mask or inverted mask).

    Rectangle masks use the same integer box as drawbox (the rendered mask), and
    the feather spread is included so a cropped mask never clips its blurred edge.
    """
    if not item.masks or any(mask.inverted for mask in item.masks):
        return None
    boxes = []
    feather = 0.0
    for mask in item.masks:
        x0, y0, x1, y1 = _mask_bbox_px(mask, w, h)
        if mask.shape == 'rect':
            x = int(round(x0))
            y = int(round(y0))
            bw = max(2, int(round(x1 - x0)))
            bh = max(2, int(round(y1 - y0)))
            x0, y0, x1, y1 = float(x), float(y), float(x + bw), float(y + bh)
        boxes.append((x0, y0, x1, y1))
        feather = max(feather, mask.feather)
    pad = 2.0 * feather
    return (max(0.0, min(box[0] for box in boxes) - pad),
            max(0.0, min(box[1] for box in boxes) - pad),
            min(float(w), max(box[2] for box in boxes) + pad),
            min(float(h), max(box[3] for box in boxes) + pad))


def _is_simple_opaque_rect_mask(item):
    """True for one solid rectangle: its whole bounding box is opaque."""
    if len(item.masks) != 1:
        return False
    mask = item.masks[0]
    return (mask.shape == 'rect' and not mask.inverted
            and mask.feather <= 1e-9 and mask.opacity >= 99.9)


def _static_uniform_transform(item, src_w, src_h, seq_w, seq_h):
    """(x0, y0, sx, sy) for a static, uniform, unrotated transform, else None.

    x0/y0 is the top-left of the scaled source in sequence coordinates, using the
    same rounded dimensions as scale_dims()/overlay_xy_exprs().
    """
    if item.scale is not None and item.scale.kfs:
        return None
    if item.scale2 is not None and item.scale2.kfs:
        return None
    if abs(item.scale_w - item.scale_h) > 1e-9:
        return None
    if item.rotation is not None and (item.rotation.kfs or abs(item.rotation.static or 0.0) > 1e-9):
        return None
    if item.position is not None and item.position.kfs:
        return None
    if item.scale_w <= 1e-9:
        return None
    px, py = (0.5, 0.5)
    if item.position is not None and item.position.static:
        px, py = item.position.static
    sw, sh = scale_dims(item, src_w, src_h)
    x0 = px * seq_w - item.anchor[0] * sw
    y0 = py * seq_h - item.anchor[1] * sh
    return x0, y0, sw / float(src_w), sh / float(src_h)


def visible_overlay_window(item, src_w, src_h, seq_w, seq_h):
    """(cx, cy, cw, ch, ox, oy, ow, oh) for a static masked overlay, or None.

    The window is the transformed mask box itself, not the canvas intersection:
    the scale factor stays as close as possible to the full-frame transform and
    overlay clips the parts outside the frame. `cx`/`cy` are aligned on even
    pixels for chroma (with `crop=…:exact=1` in the caller); a window that stays
    odd falls back to RGBA. A blurred overlay never takes this shortcut (the crop
    would clip the blur spread).
    """
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
    simple_rect = _is_simple_opaque_rect_mask(item)
    if not simple_rect:
        cx &= ~1
        cy &= ~1
    cw = max(2, min(int(math.ceil(mx1)) - cx, w_int - cx))
    ch = max(2, min(int(math.ceil(my1)) - cy, h_int - cy))
    if not simple_rect:
        # Un rectangle plein ne doit jamais afficher 1 px source hors masque.
        if cw & 1 and cx + cw + 1 <= w_int:
            cw += 1
        if ch & 1 and cy + ch + 1 <= h_int:
            ch += 1
    ox = int(round(x0 + cx * sx))
    oy = int(round(y0 + cy * sy))
    ow = max(2, int(round(cw * sx)))
    oh = max(2, int(round(ch * sy)))
    return cx, cy, cw, ch, ox, oy, ow, oh


def _single_mask_chain(mask, w, h, fps, dur, out_label):
    """Chaîne de filtres produisant un flux gray (255 = zone conservée) pour un masque."""
    x0, y0, x1, y1 = _mask_bbox_px(mask, w, h)
    parts = [f"color=c=black:s={int(w)}x{int(h)}:r={fps:.6f}:d={dur:.6f}", "format=gray"]
    if mask.shape == 'rect':
        bw = max(2, int(round(x1 - x0)))
        bh = max(2, int(round(y1 - y0)))
        parts.append(f"drawbox=x={int(round(x0))}:y={int(round(y0))}:w={bw}:h={bh}:color=white:t=fill")
    else:
        cx, cy, rx, ry = _ellipse_geometry(mask, w, h)
        parts.append(
            "geq=lum='if(lte(pow((X-{cx})/{rx},2)+pow((Y-{cy})/{ry},2),1),255,0)'".format(
                cx=f"{cx:.3f}", cy=f"{cy:.3f}", rx=f"{rx:.3f}", ry=f"{ry:.3f}"))
    # Le filtre color travaille en plage limitée (Y=16 pour le noir) : l'alpha doit être en
    # pleine échelle (0 = transparent, 255 = opaque), d'où l'expansion de plage.
    parts.append("lutyuv=y='(val-16)*255/219'")
    if mask.feather > 0:
        parts.append(f"gblur=sigma={mask.feather / 2.0:.3f}")
    if mask.opacity < 99.9:
        factor = max(0.0, min(1.0, mask.opacity / 100.0))
        parts.append(f"lutyuv=y='val*{factor:.4f}'")
    if mask.inverted:
        parts.append("negate")
    return ",".join(parts) + out_label


def mask_chain(masks, w, h, fps, dur, tag):
    """Filtres produisant `[tag]` (gray, 255 = zone conservée) pour un ou plusieurs masques.

    Un seul masque : chaîne directe (feather, opacité, inversion pris en compte).
    Plusieurs masques : union approximée (max) de leurs flux individuels.
    """
    if len(masks) == 1:
        return _single_mask_chain(masks[0], w, h, fps, dur, f"[{tag}]")
    chains = []
    labels = []
    for i, mask in enumerate(masks):
        label = f"[{tag}_{i}]"
        chains.append(_single_mask_chain(mask, w, h, fps, dur, label))
        labels.append(label)
    current = labels[0]
    for i, nxt in enumerate(labels[1:], start=1):
        out = f"[{tag}]" if i == len(labels) - 1 else f"[{tag}_u{i}]"
        chains.append(f"{current}{nxt}blend=all_mode=lighten{out}")
        current = out
    return ";".join(chains)


def merge_loop_groups(items, bases):
    """Merge consecutive identical replays of one source into a single looped overlay.

    An overlay element that is replayed back to back from the start of its source
    (same path, same in-point, contiguous in time, same static transform) produces
    exactly the same picture as one looped input spanning the whole run, without the
    extra decoders and frame buffering of separate replay inputs.

    `-stream_loop -1` loops on the real end of the media file: only a replay whose
    clip duration equals the full probed duration is mergeable. An item with masks,
    a blur or animated transforms stays on the separate trim/replay path.
    """

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
                item.blur.amount if item.blur is not None else None,
                bool(item.uniform_scale))

    def fits(item):
        """True only for a replay of the whole source file from its start."""
        cin, cdur = clip_times(item)
        if item.clip.still or item.masks or item.blur is not None:
            return False
        if cin >= 1e-6:
            return False
        if item.position is not None and item.position.kfs:
            return False
        if has_animated_scale(item):
            return False
        if item.rotation is not None and item.rotation.kfs:
            return False
        resolved = resolve_media(item.clip.source_path, bases)
        full = probe_duration(resolved) if resolved else 0.0
        return full > 1e-6 and abs(cdur - full) <= 0.1

    groups = []
    i = 0
    while i < len(items):
        first = items[i]
        cin, _cdur = clip_times(first)
        group = [first]
        span_end = first.end
        can_merge = fits(first)
        j = i + 1
        while can_merge and j < len(items):
            nxt = items[j]
            ncin, _ncdur = clip_times(nxt)
            if (nxt.clip.source_path == first.clip.source_path
                    and abs(ncin - cin) < 1e-6
                    and abs(nxt.start - span_end) < 1e-6
                    and fits(nxt)
                    and sig(nxt) == sig(first)):
                group.append(nxt)
                span_end = nxt.end
                j += 1
            else:
                break
        groups.append((group, cin, span_end))
        i = j
    return groups


class PullTable:
    """Table des entrées ffmpeg (`pulls`) et de leurs pads deja consommes.

    Une pull normale est partagee par le video et l'audio (pads distincts) ; en
    revanche un meme couple (source, in, out) utilise deux fois sur le meme flux
    doit creer une nouvelle pull, sinon le graphe reference deux fois le meme pad
    (`[{p}:v]`), ce qui fait echouer ffmpeg. Une pull de boucle recreee reste
    toujours une boucle (sinon elle ne rejouerait que le premier segment).
    """

    def __init__(self):
        self.pulls = []
        self._map = {}
        self._used = set()

    def get(self, resolved, in_s, out_s, unique=False, loop_span=None, stream='v'):
        is_loop = loop_span is not None
        if is_loop:
            key = (resolved, 0.0, round(loop_span, 6), 'loop')
        elif unique:
            key = (resolved, round(in_s, 6), round(out_s, 6), len(self.pulls))
        else:
            key = (resolved, round(in_s, 6), round(out_s, 6))
        idx = self._map.get(key)
        if idx is not None and (idx, stream) in self._used:
            if is_loop:
                key = (resolved, 0.0, round(loop_span, 6), 'loop', len(self.pulls))
            else:
                key = (resolved, round(in_s, 6), round(out_s, 6), len(self.pulls))
            idx = None
        if idx is None:
            idx = len(self.pulls)
            self._map[key] = idx
            if is_loop:
                self.pulls.append((resolved, 0.0, round(loop_span, 6), True))
            else:
                self.pulls.append((resolved, round(in_s, 6), round(out_s, 6), False))
        self._used.add((idx, stream))
        return idx


def _abspaths(items):
    """Chemins absolus d'une liste de chemins ou de tuples (chemin, ...)."""
    paths = set()
    for item in items:
        path = item[0] if isinstance(item, tuple) else item
        if path:
            paths.add(os.path.abspath(path))
    return paths


def _bitrate_bps(value):
    """Convertit un debit ffmpeg ("10M", "8000k", "1200000") en bits/s (0 si illisible)."""
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([kKmM]?)$', str(value or '').strip())
    if not match:
        return 0
    factor = {'': 1, 'k': 1_000, 'm': 1_000_000}[match.group(2).lower()]
    return int(float(match.group(1)) * factor)


def _encoder_works(encoder, timeout=30):
    """Teste reellement l'initialisation d'un encodeur sur une trame nulle 64x64."""
    try:
        completed = subprocess.run(
            ['ffmpeg', '-hide_banner', '-v', 'error', '-f', 'lavfi',
             '-i', 'nullsrc=s=64x64:d=0.1', '-frames:v', '1', '-pix_fmt', 'yuv420p',
             '-c:v', encoder, '-f', 'null', '-'],
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return False
    return completed.returncode == 0


def _select_encoder(forced=None):
    """Encodeur effectif : force par l'utilisateur, sinon NVENC si reellement utilisable."""
    forced = str(forced or '').strip()
    if forced and forced.lower() != 'auto':
        return forced
    try:
        enc_out = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'],
                                 capture_output=True, text=True, timeout=30).stdout
    except Exception:
        enc_out = ''
    if 'h264_nvenc' not in (enc_out or ''):
        return 'libx264'
    if not _encoder_works('h264_nvenc'):
        print('[encoder] h264_nvenc indisponible (GPU/session NVENC), repli libx264',
              file=sys.stderr)
        sys.stderr.flush()
        return 'libx264'
    return 'h264_nvenc'


def main():
    ap = argparse.ArgumentParser(description='Export a Premiere .prproj XML sequence to MP4 with ffmpeg.')
    ap.add_argument('input', help='.prproj or unpacked .xml file')
    ap.add_argument('--out', help='output mp4 (default: alongside the input)')
    ap.add_argument('--bitrate', default='10M', help='video bitrate (default 10M)')
    ap.add_argument('--encoder', default=None, help='ffmpeg video encoder (default: h264_nvenc if available)')
    ap.add_argument('--base', action='append', default=[], help='extra base directory to resolve media paths')
    ap.add_argument('--sequence', help='sequence name to export (default: first sequence)')
    ap.add_argument('--dry-run', action='store_true', help='only print the ffmpeg command')
    ap.add_argument('--dump', action='store_true', help='print the parsed timeline model as JSON and exit')
    args = ap.parse_args()

    src = os.path.abspath(args.input)
    try:
        if src.lower().endswith('.prproj'):
            # Decompression en memoire : aucun fichier temporaire sur le dossier source.
            with open(src, 'rb') as f:
                raw = f.read()
            xml_bytes = gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw
            parsed = Project(src, xml_bytes=xml_bytes)
        else:
            parsed = Project(src)
    except (OSError, gzip.BadGzipFile, EOFError, ET.ParseError, zlib.error) as exc:
        die(f"lecture du projet impossible: {exc}")

    if not parsed.sequences:
        die('no sequence found in project')
    seq = None
    if args.sequence:
        for s in parsed.sequences:
            if s['name'] == args.sequence:
                seq = s
                break
        if seq is None:
            die(f"sequence '{args.sequence}' not found (available: {[s['name'] for s in parsed.sequences]})")
    else:
        seq = parsed.sequences[0]

    video_tracks = seq['tracks']['video']
    if not video_tracks:
        die('sequence has no video track')
    v1_items = parsed.items_of_track(video_tracks[0])
    base = [i for i in v1_items
            if i.clip is not None and i.clip.source_path and not i.clip.still]
    # L'ordre des couches est l'ordre vertical Premiere (V1 still, V2, V3...) ; il
    # n'est jamais retrie : les derniers composites passent au-dessus.
    layers = collect_overlay_layers(parsed, video_tracks, v1_items)
    overlays = [it for layer in layers for it in layer]
    if not base:
        die('no video segments found on the base track')

    xml_dir = os.path.dirname(src)
    bases = args.base + [xml_dir, os.path.dirname(xml_dir),
                         os.path.dirname(os.path.dirname(xml_dir)), os.getcwd()]
    src_path_main = resolve_media(base[0].clip.source_path, bases)
    if not src_path_main:
        die(f"media not found for {base[0].clip.source_path} (use --base)")

    seq_w, seq_h = 1080.0, 1920.0
    fps = 25.0
    groups = seq['el'].find('TrackGroups')
    if groups is not None:
        for tg in groups.findall('TrackGroup'):
            g = ref(parsed.by_id, parsed.by_uid, tg, 'Second')
            if g is not None and g.tag == 'VideoTrackGroup':
                fr = text(g, 'FrameRect')
                if fr:
                    p = fr.split(',')
                    seq_w, seq_h = float(p[2]), float(p[3])
                frate = text(g, 'FrameRate')
                if frate:
                    fps = TICKS / float(frate)
                break

    src_w, src_h = base[0].clip.source_w, base[0].clip.source_h
    if not src_w or not src_h:
        die('source media dimensions missing in XML')

    total = max([i.end for i in base] + [i.end for i in overlays] + [0.0])

    if args.dump:
        model = {
            'sequence': seq['name'],
            'sequence_size': [seq_w, seq_h],
            'fps': fps,
            'duration': total,
            'source': {'path': src_path_main, 'size': [src_w, src_h]},
            'segments': [
                {
                    'start': i.start, 'end': i.end,
                    'in': i.clip.in_sec, 'out': i.clip.out_sec,
                    'scale': i.scale_w / 100.0,
                    'window': [int(round(seq_w / (i.scale_w / 100.0))),
                               int(round(seq_h / (i.scale_w / 100.0)))],
                    'anchor': list(i.anchor),
                    'position_static': list(i.position.static) if i.position and i.position.static else None,
                    'keyframes': [
                        {'t': k.t, 'x': k.x, 'y': k.y,
                         'in': [k.ix, k.iy], 'out': [k.ox, k.oy]}
                        for k in (i.position.kfs if i.position else [])
                    ],
                    'masks': [
                        {'shape': m.shape, 'type': m.type,
                         'bbox': [round(v, 6) for v in m.bbox], 'points': len(m.points),
                         'feather': m.feather, 'opacity': m.opacity, 'expansion': m.expansion,
                         'inverted': m.inverted, 'blend': m.blend}
                        for m in i.masks
                    ],
                    'blur': ({'amount': i.blur.amount, 'thickness': i.blur.thickness,
                              'uniform': i.blur.uniform, 'angle': i.blur.angle}
                             if i.blur is not None else None),
                }
                for i in base
            ],
            'overlays': [
                {
                    'start': o.start, 'end': o.end,
                    'path': o.clip.source_path, 'size': [o.clip.source_w, o.clip.source_h],
                    'scale': [o.scale_w / 100.0, o.scale_h / 100.0],
                    'anchor': list(o.anchor),
                    'position_static': list(o.position.static) if o.position and o.position.static else None,
                    'masks': [
                        {'shape': m.shape, 'type': m.type,
                         'bbox': [round(v, 6) for v in m.bbox], 'points': len(m.points),
                         'feather': m.feather, 'opacity': m.opacity, 'expansion': m.expansion,
                         'inverted': m.inverted, 'blend': m.blend}
                        for m in o.masks
                    ],
                    'blur': ({'amount': o.blur.amount, 'thickness': o.blur.thickness,
                              'uniform': o.blur.uniform, 'angle': o.blur.angle}
                             if o.blur is not None else None),
                }
                for o in overlays
            ],
        }
        print(json.dumps(model, indent=2))
        return

    # Plage source réellement utilisée par média : l'intermédiaire alpha et l'analyse
    # bbox ne couvrent que le préfixe [0, fin utilisé], jamais les rushes non montés.
    # On conserve le préfixe (pas de -ss) pour ne décaler aucun horodatage.
    _alpha_limits.clear()
    for item in base:
        cin, cdur = clip_times(item)
        _note_alpha_end(item.clip.source_path, cin + cdur, bases)
    overlay_groups = []
    for layer in layers:
        overlay_groups.extend(merge_loop_groups(layer, bases))
    for group, _cin, span_end in overlay_groups:
        ov = group[0]
        if ov.clip.still:
            continue
        if len(group) > 1:
            _note_alpha_end(ov.clip.source_path, span_end - ov.start, bases)
        else:
            cin, cdur = clip_times(ov)
            _note_alpha_end(ov.clip.source_path, cin + cdur, bases)

    pull_table = PullTable()
    pulls = pull_table.pulls

    def get_pull(path, in_s, out_s, unique=False, alpha_ok=False, loop_span=None,
                 stream='v'):
        resolved = resolve_media(path, bases)
        if not resolved:
            die(f"media not found for {path} (use --base)")
        if alpha_ok:
            resolved = alpha_intermediate(resolved)
        return pull_table.get(resolved, in_s, out_s, unique=unique,
                              loop_span=loop_span, stream=stream)

    def group_runs(items):
        """Group consecutive items that are contiguous within the same source file.

        A run is served by a single decoder input; each item is carved out of it
        with trim inside the filter graph, which keeps seeking/decoding cost at
        one pass over the material.
        """
        runs = []
        for item in items:
            cin, cdur = clip_times(item)
            cout = cin + cdur
            if runs and runs[-1]['path'] == item.clip.source_path and abs(cin - runs[-1]['out']) < 1e-6:
                runs[-1]['out'] = cout
                runs[-1]['items'].append((item, cin, cout))
            else:
                runs.append({'path': item.clip.source_path, 'in': cin, 'out': cout,
                             'items': [(item, cin, cout)]})
        return runs

    filters = []
    video_labels = []
    cursor = 0.0
    n = 0
    for r, run in enumerate(group_runs(base)):
        p = get_pull(run['path'], run['in'], run['out'], alpha_ok=True)
        k = len(run['items'])
        if k > 1:
            outs = ''.join(f"[vr{r}_{j}]" for j in range(k))
            filters.append(f"[{p}:v]split={k}{outs}")
        for j, (item, cin, cout) in enumerate(run['items']):
            if item.start > cursor + 1e-6:
                gap = item.start - cursor
                filters.append(f"color=c=black:s={int(seq_w)}x{int(seq_h)}:r={fps:.6f}:d={gap:.6f},"
                               f"setsar=1,format=yuv420p[vg{n}]")
                video_labels.append(f"[vg{n}]")
            src_label = f"[vr{r}_{j}]" if k > 1 else f"[{p}:v]"
            trim = (f"trim=start={cin - run['in']:.6f}:end={cout - run['in']:.6f},"
                    f"setpts=PTS-STARTPTS")
            isw = item.clip.source_w or src_w
            ish = item.clip.source_h or src_h
            # Le chemin « fenêtre de crop » ne peut pas rendre fidèlement un item dont la
            # fenêtre ne tient pas dans la source, dont le cadrage demandé sort des limites
            # (position bornée), ni un item masqué (transparence requise) : dans ces cas,
            # l'item passe par la composition (fond + overlay).
            if item_needs_canvas(item) or item.masks or not crop_window_fits(item, seq_w, seq_h, isw, ish):
                media_path = alpha_intermediate(resolve_media(item.clip.source_path, bases) or '')
                bbox_part, bdx, bdy = bbox_crop_part(item, media_path, isw, ish)
                tf = transform_filters(item, isw, ish, item.start, item.end)
                pending = []
                if tf:
                    pending.append(tf)
                if item.blur is not None:
                    dims = None if has_animated_scale(item) else scale_dims(item, isw, ish)
                    chain = (blur_chain(item.blur, dims[0], dims[1]) if dims
                             else blur_chain(item.blur))
                    if item.masks:
                        # gblur negocie yuv420p par defaut et detruit l'alpha du masque.
                        chain = f"format=yuva420p,{chain},format=rgba"
                    pending.append(chain)
                if bbox_part:
                    pending.append(bbox_part)
                opacity_value = _opacity_scalar(item.opacity)
                if opacity_value is not None and opacity_value < 0.999:
                    pending.extend(['format=rgba',
                                    f'colorchannelmixer=aa={opacity_value:.4f}'])
                tail = ("," + ",".join(pending)) if pending else ""
                if item.masks:
                    filters.append(mask_chain(item.masks, isw, ish, fps, 1.0 / fps, f"mkn{n}"))
                    filters.append(f"{src_label}{trim},format=rgba[mki{n}]")
                    # alphamerge : la vidéo d'abord, le masque (gray) ensuite
                    filters.append(f"[mki{n}][mkn{n}]alphamerge{tail}[xf{n}]")
                else:
                    filters.append(f"{src_label}{trim}{tail}[xf{n}]")
                ox, oy = overlay_xy_exprs(item, isw, ish, seq_w, seq_h, item.start, item.end)
                if bdx or bdy:
                    ox = f"({ox}+{bdx})"
                    oy = f"({oy}+{bdy})"
                filters.append(f"color=c=black:s={int(seq_w)}x{int(seq_h)}:r={fps:.6f}:d={(cout - cin):.6f},"
                               f"setsar=1,format=yuv420p[cg{n}]")
                filters.append(f"[cg{n}][xf{n}]overlay=x='{ox}':y='{oy}':shortest=1,"
                               f"setsar=1,format=yuv420p[v{n}]")
            else:
                scale = item.scale_w / 100.0
                w_win = max(1, int(round(seq_w / scale)))
                h_win = max(1, int(round(seq_h / scale)))
                if w_win > isw or h_win > ish:
                    die(f'crop window {w_win}x{h_win} does not fit source {isw}x{ish}')
                a_x = item.anchor[0] * isw
                a_y = item.anchor[1] * ish
                x_expr = axis_expr(axis_path(item, 'x'), -seq_w / scale,
                                   a_x + seq_w / (2.0 * scale) - w_win / 2.0, 0.0, isw - w_win)
                y_expr = axis_expr(axis_path(item, 'y'), -seq_h / scale,
                                   a_y + seq_h / (2.0 * scale) - h_win / 2.0, 0.0, ish - h_win)
                blur_part = (f"{blur_chain(item.blur, int(seq_w), int(seq_h))},"
                             if item.blur is not None else "")
                filters.append(f"{src_label}{trim},"
                               f"crop={w_win}:{h_win}:x='{x_expr}':y='{y_expr}',"
                               f"scale={int(seq_w)}:{int(seq_h)}:flags=bicubic,"
                               f"{blur_part}"
                               f"setsar=1,format=yuv420p[v{n}]")
            video_labels.append(f"[v{n}]")
            cursor = item.end
            n += 1
    filters.append(f"{''.join(video_labels)}concat=n={len(video_labels)}:v=1:a=0[vcat]")
    last = '[vcat]'

    image_inputs = []
    plan = []
    for group, cin, span_end in overlay_groups:
        ov = group[0]
        ov_path = resolve_media(ov.clip.source_path, bases)
        if not ov_path:
            die(f"overlay media not found for {ov.clip.source_path}")
        if ov.clip.still:
            image_inputs.append((ov_path, has_animated_transform(ov)))
            plan.append((group, cin, span_end, 'still', len(image_inputs) - 1, ov_path))
        elif len(group) > 1:
            span = span_end - ov.start
            p = get_pull(ov.clip.source_path, 0.0, span, alpha_ok=True, loop_span=span)
            plan.append((group, cin, span_end, 'loop', p, alpha_intermediate(ov_path)))
        else:
            cdur = clip_times(ov)[1]
            p = get_pull(ov.clip.source_path, cin, cin + cdur, unique=True, alpha_ok=True)
            plan.append((group, cin, span_end, 'single', p, alpha_intermediate(ov_path)))
    for k, (group, cin, span_end, kind, idx, effective) in enumerate(plan):
        ov = group[0]
        looped = kind == 'loop'
        is_still = kind == 'still'
        src_label = f"[IMG{idx}:v]" if is_still else f"[{idx}:v]"
        ow = ov.clip.source_w or seq_w
        oh = ov.clip.source_h or seq_h
        bbox_part, bdx, bdy = bbox_crop_part(ov, effective, ow, oh, exact=is_still)
        # La composition d'un overlay (framesync) s'applique avec un décalage (le flux
        # secondaire est consommé par rapport à l'image suivante quand les horodatages sont
        # égaux). On avance flux et fenêtre d'activation d'une DEMI-image : l'incrustation
        # apparaît exactement à sa première image, sans ni avance ni retard (vérifié).
        shift = 0.5 / fps
        head = []
        if not is_still:
            start_pts = max(0.0, ov.start - shift)
            if looped:
                head.append(f"setpts=N/({fps:.6f}*TB)+{start_pts:.6f}/TB")
            else:
                head.append(f"setpts=PTS-STARTPTS+{start_pts:.6f}/TB")
        opacity_value = _opacity_scalar(ov.opacity)
        # Fenêtre visible d'un overlay à transformation statique : évite de rastériser
        # des plans entiers (ex. zoom 252 % -> 4838x2722) qui sont ensuite recadrés.
        window = None
        if bbox_part is None and ov.masks and ov.blur is None:
            window = visible_overlay_window(ov, ow, oh, seq_w, seq_h)
        if window is not None:
            cx, cy, cw, ch, ox, oy, out_w, out_h = window
            source_has_alpha = _pix_has_alpha(probe_video(effective)[1])
            simple_rect = _is_simple_opaque_rect_mask(ov)
            needs_mask = bool(ov.masks) and not simple_rect
            odd_window = bool((cx | cy | cw | ch | out_w | out_h) & 1)
            needs_alpha = needs_mask or source_has_alpha or odd_window or (
                opacity_value is not None and opacity_value < 0.999)
            # exact=1 : evite la troncature silencieuse de cx/cy impairs par crop
            # en yuv420p (decalage chroma compensé par la fenêtre).
            crop_src = f"crop={cw}:{ch}:{cx}:{cy}:exact=1"
            post = [f"scale={out_w}:{out_h}:flags=bicubic"]
            if ov.blur is not None:
                post.append(blur_chain(ov.blur, out_w, out_h))
            if opacity_value is not None and opacity_value < 0.999:
                post.append(f"colorchannelmixer=aa={opacity_value:.4f}")
            post.extend(['format=rgba' if needs_alpha else 'format=yuv420p', 'setsar=1'])
            if needs_mask:
                # Le masque est généré à la taille source puis recadré avec le flux :
                # l'alphamerge ne traite que la fenêtre utile.
                mask_dur = 1.0 / fps
                filters.append(mask_chain(ov.masks, ow, oh, fps, mask_dur, f"omkn{k}"))
                filters.append(f"[omkn{k}]{crop_src}[omknw{k}]")
                filters.append(f"{src_label}{crop_src},format=rgba[omki{k}]")
                filters.append(f"[omki{k}][omknw{k}]alphamerge,"
                               + ','.join(head + post) + f"[ov{k}]")
            else:
                pre = [crop_src]
                if needs_alpha:
                    pre.append("format=rgba")
                filters.append(src_label + ','.join(pre + head + post) + f"[ov{k}]")
        else:
            tail = []
            tf = transform_filters(ov, ow, oh, 0.0, total)
            if tf:
                tail.append(tf)
            if ov.blur is not None:
                dims = None if has_animated_scale(ov) else scale_dims(ov, ow, oh)
                chain = (blur_chain(ov.blur, dims[0], dims[1]) if dims
                         else blur_chain(ov.blur))
                if ov.masks:
                    chain = f"format=yuva420p,{chain},format=rgba"
                tail.append(chain)
            if bbox_part:
                tail.append(bbox_part)
            if opacity_value is not None and opacity_value < 0.999:
                tail.extend(['format=rgba', f'colorchannelmixer=aa={opacity_value:.4f}'])
            tail.extend(['format=rgba', 'setsar=1'])
            if ov.masks:
                # Durée minimale nécessaire : le masque étant statique, une seule trame
                # suffit ; alphamerge la répète automatiquement (framesync eof_action=repeat),
                # ce qui élimine le recalcul coûteux de drawbox/lutyuv sur chaque trame.
                mask_dur = 1.0 / fps
                filters.append(mask_chain(ov.masks, ow, oh, fps, mask_dur, f"omkn{k}"))
                # Le masque est appliqué AVANT le décalage temporel (setpts) : les deux flux ont
                # ainsi les mêmes timestamps locaux (sinon la synchronisation d'alphamerge et de
                # l'overlay décale l'apparition de l'incrustation d'une image).
                filters.append(f"{src_label}format=rgba[omki{k}]")
                filters.append(f"[omki{k}][omkn{k}]alphamerge," + ','.join(head + tail) + f"[ov{k}]")
            else:
                filters.append(src_label + ','.join(head + tail) + f"[ov{k}]")
            ox, oy = overlay_xy_exprs(ov, ow, oh, seq_w, seq_h, 0.0, total)
            if bdx or bdy:
                ox = f"({ox}+{bdx})"
                oy = f"({oy}+{bdy})"
        enable = ''
        if ov.start > 1e-6 or abs(span_end - total) > 1e-6:
            enable = f":enable='between(t,{max(0.0, ov.start - shift):.6f},{span_end:.6f})'"
        filters.append(f"{last}[ov{k}]overlay=x='{ox}':y='{oy}'{enable}[mix{k}]")
        last = f'[mix{k}]'

    audio_tracks = collect_audio_tracks(parsed, seq['tracks']['audio'])
    has_audio = bool(audio_tracks)
    audio_track_labels = []
    m = 0
    for t, audio_items in enumerate(audio_tracks):
        acursor = 0.0
        audio_labels = []
        for r, run in enumerate(group_runs(audio_items)):
            p = get_pull(run['path'], run['in'], run['out'], stream='a')
            k = len(run['items'])
            if k > 1:
                outs = ''.join(f"[ar{t}_{r}_{j}]" for j in range(k))
                filters.append(f"[{p}:a]asplit={k}{outs}")
            for j, (item, cin, cout) in enumerate(run['items']):
                if item.start > acursor + 1e-6:
                    gap = item.start - acursor
                    filters.append(f"aevalsrc=0:c=stereo:s=48000:d={gap:.6f},asetpts=PTS-STARTPTS[ag{m}]")
                    audio_labels.append(f"[ag{m}]")
                    m += 1
                src_label = f"[ar{t}_{r}_{j}]" if k > 1 else f"[{p}:a]"
                filters.append(f"{src_label}atrim=start={cin - run['in']:.6f}:end={cout - run['in']:.6f},"
                               f"asetpts=PTS-STARTPTS,aresample=48000,"
                               f"aformat=channel_layouts=stereo:sample_rates=48000[a{m}]")
                audio_labels.append(f"[a{m}]")
                acursor = item.end
                m += 1
        out_label = '[aout]' if len(audio_tracks) == 1 else f'[aout_{t}]'
        filters.append(f"{''.join(audio_labels)}concat=n={len(audio_labels)}:v=0:a=1{out_label}")
        audio_track_labels.append(out_label)
    if len(audio_track_labels) > 1:
        # amix normalise chaque entree par 1/N : volume=N restaure le niveau.
        filters.append(f"{''.join(audio_track_labels)}amix=inputs={len(audio_track_labels)}:"
                       f"duration=longest:dropout_transition=0,"
                       f"volume={len(audio_track_labels)}[aout]")

    # every pull exists now: resolve still-media input indices (they sit after the pulls)
    image_base = len(pulls)
    for i in range(len(image_inputs)):
        token = f"[IMG{i}:v]"
        if any(token in f for f in filters):
            filters = [f.replace(token, f"[{image_base + i}:v]") for f in filters]

    graph = ';'.join(filters)

    # Toutes les preparations alpha/bbox sont terminees : la purge LRU peut passer
    # sans risquer de supprimer un intermediaire que ce graphe vient d'utiliser.
    _prune_cache(protect=_abspaths(pulls) | _abspaths(image_inputs))

    encoder = _select_encoder(args.encoder)

    bitrate_bps = _bitrate_bps(args.bitrate)
    bufsize = str(2 * bitrate_bps) if bitrate_bps > 0 else '20M'
    cmd = ['ffmpeg', '-y', '-hide_banner']
    for path, in_s, out_s, is_loop in pulls:
        if is_loop:
            cmd += ['-stream_loop', '-1', '-t', f'{out_s:.6f}', '-i', path]
            continue
        if in_s > 1e-6:
            cmd += ['-ss', f'{in_s:.6f}']
        dur = out_s - in_s
        if dur > 1e-6:
            cmd += ['-t', f'{dur:.6f}']
        cmd += ['-i', path]
    for path, animated in image_inputs:
        if animated:
            # Une image fixe animee (zoom/rotation a keyframes) doit produire un flux
            # continu : sinon scale/rotate ne sont evalues qu'a t=0. Le -t total final
            # coupe le flux infini.
            cmd += ['-loop', '1', '-framerate', f'{fps:.6f}', '-i', path]
        else:
            cmd += ['-i', path]
    cmd += ['-filter_complex', graph, '-map', last]
    if has_audio:
        cmd += ['-map', '[aout]']
    cmd += ['-c:v', encoder, '-b:v', args.bitrate, '-maxrate', args.bitrate,
            '-bufsize', bufsize]
    if encoder == 'h264_nvenc':
        cmd += ['-preset', 'p4', '-tune', 'hq', '-bf', '3', '-b_ref_mode', 'middle']
    else:
        # Pas de -crf : il ecraserait -b:v et fausserait l'estimation de progression.
        cmd += ['-preset', 'medium']
    cmd += ['-pix_fmt', 'yuv420p']
    if has_audio:
        cmd += ['-c:a', 'aac', '-b:a', '320k', '-ar', '48000', '-ac', '2']
    cmd += ['-movflags', '+faststart', '-t', f'{total:.6f}']
    out_path = args.out or os.path.splitext(src)[0] + '.mp4'
    cmd += [out_path]

    if args.dry_run:
        print(' '.join(shell_quote(c) for c in cmd))
        return
    print(' '.join(shell_quote(c) for c in cmd))
    sys.stdout.flush()
    rc = subprocess.call(cmd)
    if rc != 0:
        die(f'ffmpeg failed with exit code {rc}')
    print(f"done: {out_path}")


def shell_quote(s):
    if re.search(r"[^A-Za-z0-9_@%+=:,./-]", s):
        return "'" + s.replace("'", "'\\''") + "'"
    return s


if __name__ == '__main__':
    main()

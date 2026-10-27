"""
Hueface core — adds color axes to a COLR color font.

Runs in the browser through Pyodide (and in any Python with fontTools >= 4.56).
Public API, called from index.html:

    analyze(font_bytes)              -> JSON string describing the font
    build(font_bytes, config_json)   -> bytes of the new font

How it works
------------
Every distinct solid color used by the font's COLR layers is a *group*. A group
can stay fixed or get a color axis (0–100). Two modes:

  solid     The whole part changes color uniformly. Built as stacked copies of the
            layer, one per extra color, painted with a two-stop gradient of a single
            color whose opacity varies along the axis. (Illustrator ignores varying
            PaintVarSolid, but applies varying gradient stops.)
  gradient  A multi-color gradient travels across the part as the axis moves.

All new variation data lives in one 16-bit ItemVariationStore. Variable transforms
exported by font editors (32-bit translations) are rewritten as 16-bit translations.
"""

import copy
import io
import json
import re

from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables import otTables as ot
from fontTools.ttLib.tables.otTables import PaintFormat as PF
from fontTools.ttLib.tables._f_v_a_r import Axis
from fontTools.colorLib.builder import buildCOLR
from fontTools.colorLib.unbuilder import unbuildColrV1
from fontTools.varLib.builder import buildVarRegionList, buildVarData, buildVarStore

VERSION = "1.0.0"
NO_VAR = 0xFFFFFFFF
FOREGROUND = 0xFFFF
F2DOT14_ONE = 16384
INT16 = range(-32768, 32768)

# number of consecutive variation indices used by each variable paint format
VAR_COUNT = {
    PF.PaintVarSolid: 1, PF.PaintVarLinearGradient: 6, PF.PaintVarRadialGradient: 6,
    PF.PaintVarSweepGradient: 4, PF.PaintVarTranslate: 2, PF.PaintVarScale: 2,
    PF.PaintVarScaleAroundCenter: 4, PF.PaintVarScaleUniform: 1,
    PF.PaintVarScaleUniformAroundCenter: 3, PF.PaintVarRotate: 1,
    PF.PaintVarRotateAroundCenter: 3, PF.PaintVarSkew: 2, PF.PaintVarSkewAroundCenter: 4,
}
SOLIDS = (PF.PaintSolid, PF.PaintVarSolid)
GRADIENTS = (PF.PaintLinearGradient, PF.PaintVarLinearGradient, PF.PaintRadialGradient,
             PF.PaintVarRadialGradient, PF.PaintSweepGradient, PF.PaintVarSweepGradient)


class HuefaceError(Exception):
    pass


# --------------------------------------------------------------------- loading

def _load(data):
    if hasattr(data, "to_bytes"):          # Pyodide: JavaScript Uint8Array
        data = data.to_bytes()
    try:
        font = TTFont(io.BytesIO(bytes(data)), lazy=False)
        font.ensureDecompiled()
    except Exception as e:
        raise HuefaceError(f"This file couldn't be read as a font ({e}).")
    if "glyf" not in font and "CFF " not in font and "CFF2" not in font:
        raise HuefaceError("This file has no outlines. Load a .ttf or .otf font.")
    if "COLR" not in font or "CPAL" not in font:
        if "SVG " in font:
            raise HuefaceError("This font uses SVG color, which can't be variable. "
                               "Export it with COLR color instead.")
        raise HuefaceError("This font has no COLR color. Give your glyphs colored "
                           "elements and export with COLR enabled.")
    return font


def _hex(c):
    return "#%02X%02X%02X" % (c.red, c.green, c.blue)


def _key(c):
    return "%02X%02X%02X%02X" % (c.red, c.green, c.blue, c.alpha)


def _colr_paints(font):
    """Every color glyph as a COLRv1 paint dict, whatever the COLR version."""
    colr = font["COLR"]
    paints = {}
    if colr.version == 0:
        for g, layers in colr.ColorLayers.items():
            paints[g] = {"Format": PF.PaintColrLayers, "Layers": [
                {"Format": PF.PaintGlyph, "Glyph": l.name,
                 "Paint": {"Format": PF.PaintSolid, "PaletteIndex": l.colorID, "Alpha": 1.0}}
                for l in layers]}
        return paints, None, None
    t = colr.table
    if t.BaseGlyphRecordArray and t.BaseGlyphRecordCount:
        recs = t.LayerRecordArray.LayerRecord
        for b in t.BaseGlyphRecordArray.BaseGlyphRecord:
            paints[b.BaseGlyph] = {"Format": PF.PaintColrLayers, "Layers": [
                {"Format": PF.PaintGlyph, "Glyph": r.LayerGlyph,
                 "Paint": {"Format": PF.PaintSolid, "PaletteIndex": r.PaletteIndex, "Alpha": 1.0}}
                for r in recs[b.FirstLayerIndex:b.FirstLayerIndex + b.NumLayers]]}
    if t.BaseGlyphList:
        paints.update(unbuildColrV1(t.LayerList, t.BaseGlyphList))
    vmap = t.VarIndexMap.mapping if t.VarIndexMap else None
    return paints, t.VarStore, vmap


def _walk(p, fn):
    """Depth-first over every dict in a paint tree (paints, transforms, color stops)."""
    if isinstance(p, dict):
        fn(p)
        for v in p.values():
            _walk(v, fn)
    elif isinstance(p, list):
        for v in p:
            _walk(v, fn)


def _layers(root):
    return root["Layers"] if root.get("Format") == PF.PaintColrLayers else [root]


def _first_solid(p):
    found = []
    _walk(p, lambda d: found.append(d) if d.get("Format") in SOLIDS else None)
    return found[0] if found else None


# --------------------------------------------------------------------- analyze

def _analysis(font):
    paints, _, _ = _colr_paints(font)
    pal = font["CPAL"].palettes[0]
    groups, warnings = {}, []
    flags = {"gradient": False, "foreground": False, "transform": False}

    for g, root in paints.items():
        seen = set()
        for layer in _layers(root):
            def visit(d):
                f = d.get("Format")
                if f in SOLIDS:
                    idx = d["PaletteIndex"]
                    if idx == FOREGROUND:
                        flags["foreground"] = True
                        return
                    c = pal[idx]
                    k = _key(c)
                    grp = groups.setdefault(k, {"key": k, "hex": _hex(c), "alpha": round(c.alpha / 255, 3),
                                                "glyphs": 0, "layers": 0})
                    grp["layers"] += 1
                    seen.add(k)
                elif f in GRADIENTS:
                    flags["gradient"] = True
                elif f == PF.PaintVarTransform:
                    flags["transform"] = True
            _walk(layer, visit)
        for k in seen:
            groups[k]["glyphs"] += 1

    if "SVG " in font:
        warnings.append({"level": "info", "text": "The SVG color table will be removed so apps use the COLR colors."})
    if flags["gradient"]:
        warnings.append({"level": "info", "text": "Gradient fills in your font are kept as they are."})
    if flags["foreground"]:
        warnings.append({"level": "info", "text": "Layers drawn in the text color are kept as they are."})
    if flags["transform"]:
        warnings.append({"level": "warn", "text": "Some components are rotated or scaled differently in each master. "
                         "Hueface rewrites their position, but they may still look wrong in Illustrator. "
                         "Decomposing them in your font editor is the safest option."})
    if "fvar" not in font:
        warnings.append({"level": "info", "text": "This font isn't variable: the export will be variable with the color axes only."})

    if ";hueface-" in (font["name"].getDebugName(3) or ""):
        raise HuefaceError("This font was exported by Hueface, so its colors are already turned into "
                           "axes. Load the original font exported from your font editor instead, and "
                           "use Load settings to bring back the colors you chose.")
    if not groups:
        raise HuefaceError("Hueface found no solid colors to work with. It needs parts filled with "
                           "solid colors" + (" (this font only uses gradients)." if flags["gradient"] else "."))

    cmap = font.getBestCmap() or {}
    colored = set(paints)
    sample = "".join(chr(u) for u, g in sorted(cmap.items()) if g in colored and chr(u).isprintable()
                     and not chr(u).isspace())
    letters = "".join(ch for ch in sample if ch.isalpha())
    name = font["name"]
    axes = []
    if "fvar" in font:
        for a in font["fvar"].axes:
            axes.append({"tag": a.axisTag, "name": name.getDebugName(a.axisNameID) or a.axisTag,
                         "min": a.minValue, "default": a.defaultValue, "max": a.maxValue})
    return {
        "family": name.getDebugName(16) or name.getDebugName(1) or "Untitled",
        "axes": axes,
        "groups": sorted(groups.values(), key=lambda x: -x["glyphs"]),
        "colorGlyphs": len(paints),
        "sample": (letters or sample)[:40],
        "warnings": warnings,
    }


def analyze(data):
    try:
        return json.dumps({"ok": True, **_analysis(_load(data))})
    except HuefaceError as e:
        return json.dumps({"ok": False, "error": str(e)})


# --------------------------------------------------------------------- variation store

class _Store:
    """Collects variation rows (dict region -> raw delta) and regions (dict tag -> triple)."""

    def __init__(self):
        self.regions, self.rows = [], []
        self.too_big = False

    def region(self, spec):
        spec = {k: tuple(float(x) for x in v) for k, v in spec.items()}
        if spec not in self.regions:
            self.regions.append(spec)
        return self.regions.index(spec)

    def add(self, rows):
        base = len(self.rows)
        self.rows.extend(rows)
        return base

    def build(self, axis_tags):
        if not self.rows:
            return None
        n = len(self.regions)
        items = [[int(round(r.get(i, 0))) for i in range(n)] for r in self.rows]
        return buildVarStore(buildVarRegionList(self.regions, axis_tags),
                             [buildVarData(list(range(n)), items, optimize=False)])


def _old_rows(old_vs, old_map, old_tags):
    """Function returning the old deltas for a variation index, keyed by region spec."""
    if old_vs is None:
        return lambda idx: {}
    regions = []
    for r in old_vs.VarRegionList.Region:
        regions.append({old_tags[i]: (a.StartCoord, a.PeakCoord, a.EndCoord)
                        for i, a in enumerate(r.VarRegionAxis) if a.PeakCoord != 0})

    def get(idx):
        if idx == NO_VAR:
            return {}
        if old_map is not None:
            if not old_map:
                return {}
            idx = old_map[min(idx, len(old_map) - 1)]
        outer, inner = idx >> 16, idx & 0xFFFF
        if outer == 0xFFFF or outer >= len(old_vs.VarData):
            return {}
        vd = old_vs.VarData[outer]
        if inner >= len(vd.Item):
            return {}
        return {tuple(sorted(regions[r].items())): vd.Item[inner][col]
                for col, r in enumerate(vd.VarRegionIndex) if vd.Item[inner][col]}
    return get


# --------------------------------------------------------------------- axes

def _add_name(font, text):
    return font["name"].addMultilingualName({"en": text}, ttFont=font, mac=False, minNameID=256)


def _other_var_stores(font):
    stores = []
    for tag in ("HVAR", "VVAR", "MVAR", "GDEF", "BASE"):
        if tag in font:
            vs = getattr(font[tag].table, "VarStore", None)
            if vs is not None:
                stores.append(vs)
    if "avar" in font and getattr(font["avar"], "majorVersion", 1) >= 2:
        vs = getattr(font["avar"].table, "VarStore", None)
        if vs is not None:
            stores.append(vs)
    if "CFF2" in font:
        vs = getattr(font["CFF2"].cff.topDictIndex[0], "VarStore", None)
        if vs is not None:
            stores.append(vs.otVarStore)
    return stores


def _axis_tag(name, taken):
    base = re.sub(r"[^A-Za-z0-9]", "", name).upper()
    if not base or not base[0].isalpha():
        base = "C" + base
    base = (base + "XXXX")[:4]
    tag, n = base, 1
    while tag in taken:
        tag = base[:3] + str(n) if n < 10 else base[:2] + str(n)
        n += 1
    taken.add(tag)
    return tag


def _add_axes(font, axes):
    """axes: list of (tag, name). Range 0–100, default 0."""
    if "fvar" not in font:
        fvar = font["fvar"] = newTable("fvar")
        fvar.axes, fvar.instances = [], []
    fvar = font["fvar"]
    for tag, name in axes:
        a = Axis()
        a.axisTag, a.minValue, a.defaultValue, a.maxValue, a.flags = tag, 0.0, 0.0, 100.0, 0
        a.axisNameID = _add_name(font, name)
        fvar.axes.append(a)
        for inst in fvar.instances:
            inst.coordinates[tag] = 0.0
        if "avar" in font:
            font["avar"].segments[tag] = {-1.0: -1.0, 0.0: 0.0, 1.0: 1.0}
    n = len(fvar.axes)
    for vs in _other_var_stores(font):
        for region in vs.VarRegionList.Region:
            while len(region.VarRegionAxis) < n:
                ra = ot.VarRegionAxis()
                ra.StartCoord = ra.PeakCoord = ra.EndCoord = 0.0
                region.VarRegionAxis.append(ra)
        vs.VarRegionList.RegionAxisCount = n
    _update_stat(font, axes)


def _update_stat(font, new_axes):
    fvar = font["fvar"]
    if "STAT" not in font:
        from fontTools.otlLib.builder import buildStatTable
        buildStatTable(font, [{"tag": a.axisTag, "name": font["name"].getDebugName(a.axisNameID),
                               "values": [{"value": a.defaultValue, "name": "Regular" if a.axisTag not in
                                           dict(new_axes) else "Normal", "flags": 0x2}]}
                              for a in fvar.axes])
        return
    stat = font["STAT"].table
    if stat.DesignAxisRecord is None:
        stat.DesignAxisRecord = ot.AxisRecordArray()
        stat.DesignAxisRecord.Axis = []
    if stat.AxisValueArray is None:
        stat.AxisValueArray = ot.AxisValueArray()
        stat.AxisValueArray.AxisValue = []
    normal = _add_name(font, "Normal")
    names = {a.axisTag: a.axisNameID for a in fvar.axes}
    for tag, _ in new_axes:
        rec = ot.AxisRecord()
        rec.AxisTag, rec.AxisNameID = tag, names[tag]
        rec.AxisOrdering = len(stat.DesignAxisRecord.Axis)
        stat.DesignAxisRecord.Axis.append(rec)
        val = ot.AxisValue()
        val.Format, val.AxisIndex, val.Flags = 1, rec.AxisOrdering, 0x2
        val.ValueNameID, val.Value = normal, 0.0
        stat.AxisValueArray.AxisValue.append(val)
    stat.DesignAxisCount = len(stat.DesignAxisRecord.Axis)
    stat.AxisValueCount = len(stat.AxisValueArray.AxisValue)
    if not getattr(stat, "ElidedFallbackNameID", None):
        stat.ElidedFallbackNameID = 2


# --------------------------------------------------------------------- naming

def _rename(font, family, uid):
    name = font["name"]
    old_family = name.getDebugName(16) or name.getDebugName(1)
    old_ps = (name.getDebugName(6) or "").split("-")[0]
    new_ps = "".join(ch for ch in family if ch.isalnum())[:40] or "Hueface"
    ps_ids = {6} | {i.postscriptNameID for i in font["fvar"].instances
                    if getattr(i, "postscriptNameID", 0xFFFF) != 0xFFFF}
    for rec in name.names:
        if rec.nameID in (0, 3, 7, 8, 9, 10, 11, 12, 13, 14):
            continue
        s = rec.toUnicode()
        if rec.nameID == 25:
            s = new_ps
        elif rec.nameID in ps_ids:
            s = s.replace(old_ps, new_ps) if old_ps else s
        elif old_family:
            s = s.replace(old_family, family)
        rec.string = s
    ps = name.getDebugName(6) or new_ps
    version = (name.getDebugName(5) or "Version 1.000").split(";")[0]
    name.setName(f"{version};{ps};hueface-{uid}", 3, 3, 1, 0x409)
    name.removeNames(nameID=3, platformID=1)


# --------------------------------------------------------------------- build

def _resolve_axes(groups):
    """Each variable group gets an axis id: its own key, or the key of the group it shares with."""
    by_key = {g["key"]: g for g in groups}
    owner = {}
    for g in groups:
        a = g.get("axis", "own")
        if a == "fixed" or len(g.get("stops", [])) < 2:
            continue
        if a != "own" and a in by_key and by_key[a].get("axis", "own") == "own" \
                and len(by_key[a].get("stops", [])) >= 2:
            owner[g["key"]] = a
        else:
            owner[g["key"]] = g["key"]
    return owner


def _palette_index(cpal, hexcolor, alpha, cache):
    k = (hexcolor.upper(), alpha)
    if k not in cache:
        h = hexcolor.lstrip("#")
        Color = type(cpal.palettes[0][0])
        for pal in cpal.palettes:
            pal.append(Color(red=int(h[0:2], 16), green=int(h[2:4], 16), blue=int(h[4:6], 16),
                             alpha=alpha))
        if getattr(cpal, "paletteEntryLabels", None):
            cpal.paletteEntryLabels.append(FOREGROUND)
        cpal.numPaletteEntries = len(cpal.palettes[0])
        cache[k] = len(cpal.palettes[0]) - 1
    return cache[k]


def build(data, config_json):
    """Returns the bytes of the new font."""
    return _build(data, config_json)[0]


def _build(data, config_json):
    cfg = json.loads(config_json)
    font = _load(data)
    upm = font["head"].unitsPerEm
    cpal = font["CPAL"]
    pal = cpal.palettes[0]
    old_tags = [a.axisTag for a in font["fvar"].axes] if "fvar" in font else []
    paints, old_vs, old_map = _colr_paints(font)
    old = _old_rows(old_vs, old_map, old_tags)

    groups = [g for g in cfg.get("groups", []) if g.get("key")]
    by_key = {g["key"]: g for g in groups}
    owner = _resolve_axes(groups)

    # axes to add, in group order
    taken = set(old_tags)
    axis_of = {}
    new_axes = []
    for g in groups:
        if owner.get(g["key"]) == g["key"]:
            name = (g.get("name") or "Color").strip()[:40] or "Color"
            tag = _axis_tag(name, taken)
            axis_of[g["key"]] = tag
            new_axes.append((tag, name))
    if new_axes:
        _add_axes(font, new_axes)
    tags = [a.axisTag for a in font["fvar"].axes] if "fvar" in font else []

    store = _Store()
    region_specs = {}

    def rows_from_old(idx, count):
        out = []
        for i in range(count):
            r = {}
            for spec, delta in old(idx + i if idx != NO_VAR else NO_VAR).items():
                r[store.region(dict(spec))] = delta
            out.append(r)
        return out

    # 1. re-home existing variations into the new store; split variable transforms
    def rehome(p):
        if isinstance(p, list):
            return [rehome(x) for x in p]
        if not isinstance(p, dict):
            return p
        p = {k: rehome(v) for k, v in p.items()}
        f = p.get("Format")
        if f == PF.PaintVarTransform:
            t = dict(p["Transform"])
            b = t.get("VarIndexBase", NO_VAR)
            rows = rows_from_old(b, 6)
            tr = [{k: d / 65536 for k, d in rows[i].items()} for i in (4, 5)]   # Fixed → units
            for r in rows[:4]:
                if any(int(v) not in INT16 for v in r.values()):
                    store.too_big = True
            t["VarIndexBase"] = store.add(rows[:4] + [{}, {}])
            inner = {"Format": PF.PaintVarTransform, "Paint": p["Paint"], "Transform": t}
            if any(tr[0].values()) or any(tr[1].values()):
                dx, dy = t["dx"], t["dy"]
                t["dx"] = t["dy"] = 0
                return {"Format": PF.PaintVarTranslate, "dx": dx, "dy": dy,
                        "VarIndexBase": store.add(tr), "Paint": inner}
            return inner
        if f in VAR_COUNT and p.get("VarIndexBase", NO_VAR) != NO_VAR:
            p["VarIndexBase"] = store.add(rows_from_old(p["VarIndexBase"], VAR_COUNT[f]))
        if "StopOffset" in p and p.get("VarIndexBase", NO_VAR) != NO_VAR:
            p["VarIndexBase"] = store.add(rows_from_old(p["VarIndexBase"], 2))
        return p

    paints = {g: rehome(p) for g, p in paints.items()}

    # 2. recolor
    cache = {}
    os2 = font["OS/2"] if "OS/2" in font else None
    desc = (os2.sTypoDescender if os2 else 0) or font["hhea"].descent
    asc = (os2.sTypoAscender if os2 else 0) or font["hhea"].ascent
    band = max(asc - desc, upm // 2)

    def alpha_rows(tag, k, n):
        """opacity of overlay k (1..n-1): 0 before segment k, ramps 0→1 inside it, 1 after."""
        # tent (a,b,1) rises on [a,b] and falls on [b,1]; ramp (b,1,1) rises on [b,1]:
        # their sum is exactly 1 on [b,1].
        a, b = (k - 1) / (n - 1), k / (n - 1)
        if b >= 1:
            return {store.region({tag: (a, 1.0, 1.0)}): F2DOT14_ONE}
        return {store.region({tag: (a, b, 1.0)}): F2DOT14_ONE,
                store.region({tag: (b, 1.0, 1.0)}): F2DOT14_ONE}

    def hide_after(tag, b, n):
        """opacity delta -1 from axis position b on (falls over a short step just before b)."""
        eps = min(0.02, 0.25 / (n - 1))
        if b >= 1:
            return {store.region({tag: (1.0 - eps, 1.0, 1.0)}): -F2DOT14_ONE}
        return {store.region({tag: (b - eps, b, 1.0)}): -F2DOT14_ONE,
                store.region({tag: (b, 1.0, 1.0)}): -F2DOT14_ONE}

    def merged(*dicts, scale=1.0):
        out = {}
        for d in dicts:
            for r, v in d.items():
                out[r] = out.get(r, 0) + v * scale
        return out

    def flat_gradient(index, alpha_var, alpha=0.0):
        """A gradient that paints one color, with variable opacity (Illustrator-friendly solid)."""
        stops = [{"StopOffset": o, "PaletteIndex": index, "Alpha": alpha,
                  "VarIndexBase": store.add([{}, alpha_var])} for o in (0.0, 1.0)]
        return {"Format": PF.PaintVarLinearGradient,
                "ColorLine": {"Extend": "pad", "ColorStop": stops},
                "x0": 0, "y0": 0, "x1": 0, "y1": upm, "x2": upm, "y2": 0, "VarIndexBase": NO_VAR}

    def travelling_gradient(stops_idx, alpha, tag):
        n = len(stops_idx)
        T = band // 2
        L = n * band + (n - 1) * T
        D = -(n - 1) * (band + T)
        cl = []
        for i, idx in enumerate(stops_idx):
            s = i * (band + T)
            for off in (s, s + band):
                cl.append({"StopOffset": off / L, "PaletteIndex": idx, "Alpha": alpha,
                           "VarIndexBase": NO_VAR})
        reg = store.region({tag: (0.0, 1.0, 1.0)})
        base = store.add([{}, {reg: D}, {}, {reg: D}, {}, {reg: D}])
        return {"Format": PF.PaintVarLinearGradient,
                "ColorLine": {"Extend": "pad", "ColorStop": cl},
                "x0": 0, "y0": desc, "x1": 0, "y1": desc + L, "x2": upm, "y2": desc,
                "VarIndexBase": base}

    def replace_solid(layer, make):
        """Copy of layer with its first solid paint replaced by make(solid_dict)."""
        layer = copy.deepcopy(layer)
        target = _first_solid(layer)

        def swap(p):
            if isinstance(p, dict):
                for k, v in list(p.items()):
                    if v is target:
                        p[k] = make(v)
                    else:
                        swap(v)
            elif isinstance(p, list):
                for v in p:
                    swap(v)
        if layer is target:
            return make(layer)
        swap(layer)
        return layer

    for gname, root in paints.items():
        out = []
        for layer in _layers(root):
            solid = _first_solid(layer)
            if not solid or solid["PaletteIndex"] == FOREGROUND:
                out.append(layer)
                continue
            c = pal[solid["PaletteIndex"]]
            g = by_key.get(_key(c))
            if not g:
                out.append(layer)
                continue
            stops = [s.upper() for s in g.get("stops", [])] or [_hex(c)]
            alpha = solid.get("Alpha", 1.0)
            start_idx = (solid["PaletteIndex"] if stops[0] == _hex(c)
                         else _palette_index(cpal, stops[0], c.alpha, cache))

            def base(s, idx=start_idx):
                s = dict(s)
                s["PaletteIndex"] = idx
                return s

            if g["key"] not in owner:                       # fixed (maybe recolored)
                out.append(replace_solid(layer, base))
                continue
            tag = axis_of[owner[g["key"]]]
            idxs = [start_idx] + [_palette_index(cpal, h, c.alpha, cache) for h in stops[1:]]
            if g.get("mode") == "gradient":
                out.append(replace_solid(layer, lambda s: travelling_gradient(idxs, alpha, tag)))
                continue
            # Each color is a copy of the layer stacked on top of the previous one. A copy
            # fades in across its stretch of the axis; the copy below it is hidden once the
            # one above is fully opaque, so no edge of an older color shows through.
            n = len(idxs)
            stops_at = [k / (n - 1) for k in range(n)]
            for k in range(n):
                fade_in = alpha_rows(tag, k, n) if k else {}
                hide = hide_after(tag, stops_at[k + 1], n) if k < n - 1 else {}
                av = merged(fade_in, hide, scale=alpha)
                start = alpha if k == 0 else 0.0
                out.append(replace_solid(layer, lambda s, k=k, av=av, start=start:
                                         flat_gradient(idxs[k], av, start)))
        paints[gname] = {"Format": PF.PaintColrLayers, "Layers": out}

    # 3. write COLR
    vs = store.build(tags)
    clips = {}
    for g in paints:
        adv = font["hmtx"][g][0] if g in font["hmtx"].metrics else upm
        clips[g] = (-upm, -upm, adv + upm, 2 * upm)
    font["COLR"] = buildCOLR(paints, version=1, glyphMap=font.getReverseGlyphMap(),
                             varStore=vs, clipBoxes=clips, allowLayerReuse=False)
    if "SVG " in font:
        del font["SVG "]

    family = (cfg.get("family") or "").strip()
    if family and "fvar" in font:
        _rename(font, family, cfg.get("uid", "0"))

    buf = io.BytesIO()
    font.save(buf)
    group_of = {axis_of[k]: k for k in axis_of}
    axes = [{"tag": a.axisTag, "name": font["name"].getDebugName(a.axisNameID),
             "min": a.minValue, "default": a.defaultValue, "max": a.maxValue,
             "group": group_of.get(a.axisTag)}
            for a in font["fvar"].axes] if "fvar" in font else []
    return buf.getvalue(), axes


def build_report(data, config_json):
    """For the page: (ok, font bytes or error message, info JSON)."""
    try:
        out, axes = _build(data, config_json)
        return True, out, json.dumps({"axes": axes, "size": len(out)})
    except HuefaceError as e:
        return False, str(e), "{}"
    except Exception as e:
        return False, f"Something went wrong building the font ({type(e).__name__}: {e}).", "{}"

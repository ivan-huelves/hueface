# Hueface

**Color axes for variable fonts.**

Hueface takes a color font made in FontLab, Glyphs or any other editor and adds new
variation axes that change its colors. The color variation lives inside the font file,
so anyone who installs the font can use it in Illustrator, InDesign or a browser by moving
a slider. No code is needed.

**Use it here:** https://ivan-huelves.github.io/hueface/

Everything runs in your browser. Your font is never uploaded.

## How to use it

1. Draw your font with colored parts and export it as a color font (see below).
2. Drop the `.ttf` or `.otf` file into Hueface.
3. For each color in your font, choose:
   - **Fixed**: it doesn't change.
   - **Own axis**: it gets a new axis, named after the row.
   - **Same axis as…**: it changes together with another color.
4. Add the colors it should travel through. Click a swatch to edit it.
5. Choose how it changes:
   - **Solid**: the whole part changes color at once.
   - **Gradient**: the new colors travel across the part.
6. Check the preview with the sliders, name the font, and export it.

**Save settings** writes your colors and axes to a small `.json` file. After you edit and
re-export your font, **Load settings** applies them again, matched by color.

## Preparing your font

In any editor:

- Draw each part you want to color as its own element, with a solid fill.
- Use one exact color per role. Hueface groups parts by color, so if two parts must change
  independently, give them slightly different colors (for example `#E6007E` and `#E6007F`).
- Avoid components that are rotated or scaled differently in each master. Decompose them
  before exporting.
- Export a variable TrueType font with COLR color, not SVG. Static color fonts also work:
  Hueface makes them variable.

**FontLab 8:** set each element's Fill in the Color panel, use the same elements and colors
in every master, then go to *File › Export Font As… › Variable TT › Customize* and turn on
*OT+COLR*.

**Glyphs 3:** add a *Color Palettes* parameter in *Font Info › Masters*, add Color layers to
every master of each glyph, and export as a variable font.

## Where the fonts work

| | Color | Color axes |
|---|---|---|
| Illustrator (2025 and later) | ✓ | ✓ |
| Chrome, Edge, Firefox | ✓ | ✓ |
| Safari, macOS apps using Core Text | not yet (COLRv1) | — |

Use Chrome, Edge or Firefox to preview in Hueface.

## How it works

Hueface writes a COLRv1 table with its own variation store:

- **Solid mode** stacks one copy of each colored layer per color. Each copy fades in over
  its stretch of the axis. The copy below is hidden once the one above is fully opaque,
  so no edge of an older color shows through.
  - Each copy is painted with a two-stop gradient of a single color, not a plain fill.
    Illustrator ignores variable opacity on plain fills (`PaintVarSolid`), but it does apply
    it to gradient stops.
- **Gradient mode** paints the layer with a multi-color linear gradient and moves it along
  the axis.
- All variation deltas are written in 16 bits. Variable transforms exported by font editors
  use 32-bit translations, which Illustrator doesn't render correctly. Hueface rewrites them
  as 16-bit translations.

The core is [`hueface.py`](hueface.py). It's plain Python with
[fontTools](https://github.com/fonttools/fonttools), run in the browser by
[Pyodide](https://pyodide.org) inside a web worker ([`worker.js`](worker.js)). You can also
use it from Python:

```python
import hueface, json
config = {"family": "My Font Color", "uid": "1", "groups": [
    {"key": "E6007EFF", "name": "Stem", "stops": ["#E6007E", "#0062FF"],
     "mode": "solid", "axis": "own"}]}
open("out.ttf", "wb").write(hueface.build(open("in.ttf", "rb").read(), json.dumps(config)))
```

## Running it locally

The page needs to be served over HTTP, not opened as a file. Run this from the folder that
contains `index.html` (the `--bind` option keeps the server private to your computer):

```sh
cd path/to/hueface
python3 -m http.server 8000 --bind 127.0.0.1
# then open http://localhost:8000
```

It loads Pyodide from the jsDelivr CDN, so the first visit needs an internet connection.

## License

Hueface is © Iván Huelves Illas and licensed under
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/). You may use, share
and adapt it if you:

- credit the original author;
- don't use it commercially;
- share your adaptations under the same license.

**The fonts you make with Hueface are yours.** This license covers the Hueface tool only,
not the fonts it produces. You can use, sell or license your fonts however you like.

Hueface uses fontTools (MIT license) and Pyodide (MPL-2.0 license).

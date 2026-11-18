# HUD library (`sdk/lib/hud.c`)

Status: image widgets on the GUI's info panel (section 6), simulator-tested
(`tests/test_hud_sim.py`); the mechanism ran on the camera with one widget (2026-11-17),
`projects/main` with it not yet. Images and colours are the ones the GUI draws [camera OSD dumps].
Linked into every plugin, like all of `sdk/lib/`; a plugin that does not call it gets no byte of it
(`--gc-sections`).

## 1. What it does

Text elements in the live view, in one of two styles: `FENYKL_HUD_PLAIN`, the loaded language's
32 px UI font, or `FENYKL_HUD_LV`, the look of the stock live-view info items (section 3).
`lib/hud.c` holds the elements and the rendering, `lib/hud_em10ii.c` puts them on the screen as GUI
widgets (section 6).

Rendering (glyph lookup with `gpi_get_glyph`, fmt 0x09 RLE decode, the GUI's spacing rules; in the LV
style also the stock images, fmt 0x03) runs in the caller's task, into the spare one of two bitmaps per element, which is then published with one 32-bit
word.

## 2. API

Header `sdk/include/hud.h` (the reference). Errors are `FENYKL_HUD_E*` (negative).

| Call | Context | Does |
| --- | --- | --- |
| `fenykl_hud_init(states)` | CPU0 task, once | checks the stock record the widgets are made from, sets the visible INFO screens, replaces the GUI slots all or nothing (section 6) |
| `fenykl_hud_add(e)` | CPU0 task, after init | checks `e`'s rectangle, allocates its two bitmaps from the library's pool (section 6) |
| `fenykl_hud_text(e, s)` | CPU0 task | renders `s` and publishes it; the next tick sends it to the widget; `ENOFONT` / `EGLYPH` leave `e` unchanged |
| `fenykl_hud_draw(e, fn, arg)` | CPU0 task | clears `e`'s spare bitmap to `bg`, calls `fn(box, w, h, arg)` to draw into it, then publishes it as `fenykl_hud_text`; a nonzero return of `fn` is returned and leaves `e` unchanged (section 3.1) |
| `fenykl_hud_image(id, px, max, w, h)` | any | decodes stock image `id` (fmt 0x03) from the firmware into `px` (section 3.1) |
| `fenykl_hud_hide(e)` | CPU0 task | unpublishes `e`; the next tick shows the empty image |
| `fenykl_hud_tick()` | CPU0 task, ~100 ms | encodes the changes and posts one GUI event (section 6) |
| `fenykl_hud_text_width(style, s)` | CPU0 task | width in a style with the current font, for sizing elements |
| `fenykl_hud_font()`, `fenykl_hud_visible()`, `fenykl_hud_get_stats(st)` | any CPU0 context | font id (0 = none yet), the visibility gate, counters |

No hook: the plugin's `plugin.toml` lists nothing for the HUD.

## 3. Parameters

Every placement and style value is a parameter, nothing is hard-coded:

- **Position and size:** `x`, `y`, `w`, `h` per element; `x` and `w` multiples of 4 (word copies),
  `h = FENYKL_HUD_LINE_H` (36) for one line. Text sits `FENYKL_HUD_MARGIN` (2) below the top; `align`
  puts it `FENYKL_HUD_MARGIN` from the left or right edge, or centres it.
- **Lines:** `\n` in the text starts a line `FENYKL_HUD_PITCH` (32) lower, in the LV style
  `FENYKL_HUD_LV_PITCH` (21): there the outline of a line abuts the next one's, as the stock ISO
  item stacks "ISO-A" over its value. n lines need `h = (n - 1) * pitch + line height`. Each line is
  aligned by its own width, a character before `\n` gets the line-end spacing, and
  `fenykl_hud_text_width` returns the widest line. `\n` counts towards `FENYKL_HUD_TEXT_MAX` (12).
  Later lines draw over the earlier ones' outline and shadow where they meet.
- **Background:** `bg` per element. `FENYKL_HUD_CLEAR` (0x00, alpha 0) leaves the rectangle
  transparent, so only glyph pixels show: the native look. `FENYKL_HUD_BLACK` (0x10) or
  `FENYKL_HUD_TRANSLUCENT` (0x11) draw a box.
- **Palette:** `ink[3]` per element maps glyph codes 1, 2 (anti-alias) and 3 (body);
  `FENYKL_HUD_WHITE` is `{0x06, 0x05, 0x04}`, the stock items' white [camera]. Coloured inks are
  the palette's other text ramps (body, 2/3 and 1/3 brightness, as the white one):
  `FENYKL_HUD_GREEN` `{0x09, 0x08, 0x07}` and `FENYKL_HUD_YELLOW` `{0x03, 0x02, 0x01}`. There is no
  fixed red: `fenykl_hud_color(rgb)` picks one (section 6).
- **Style:** `style` per element.
  - `FENYKL_HUD_PLAIN` (0): the UI font as the menus draw it, 19 px caps, 2 px strokes, no outline.
  - `FENYKL_HUD_LV`: the stock live-view info items' look [camera]. `0`-`9`, `+`,
    `-`, `:` and `.` are the GUI's own images (image font 14; `.` from image font 25, section 6): bold condensed white digits, 17 px tall, with the stock 1 px black outline (0x10)
    and shadow (0x11, 0x12), 13 px apart (`:` and `.` 6 px). Their white (body 0x04, anti-alias
    0x05, 0x06) is drawn in `ink[2]`, `ink[1]`, `ink[0]`, so `FENYKL_HUD_WHITE` gives the stock
    look and a coloured ink colours the digits; outline and shadow stay. A space is 11 px. Every other character
    is the UI font glyph scaled to 2/3 (area average; pixels of 1/3 coverage or less stay empty, so
    the gap under the dot of `i` stays open), made bold (each pixel also inks its right neighbour)
    and outlined by rule: 0x10 beside ink, 0x11 diagonal to it, 0x12 up to two pixels right of and
    below it; its baseline is the digits' baseline and its cell 3 px wider than the scaled glyph, so
    two black columns separate neighbouring letters as in the stock "ISO-A". One line is `FENYKL_HUD_LV_LINE_H` (23) tall, no top margin (the digits' ink is rows
    4-20); `align` keeps `FENYKL_HUD_LV_MARGIN` (1) from the edge. Digit-only texts need no font. The
    stock set has no letters, so `m`, `cm`, `km`, `inf`, `FD`, `DoF` come from the font: rounder than
    the stock lettering, caps and ascenders 13 px and `m` 9 px against the digits' 17 px, about the
    ratio of the stock "ISO-A" label (14 px) to its value (full-size bold letters were too large on the
    camera).
- **Visible INFO screens:** the `states` mask of `fenykl_hud_init`, `FENYKL_HUD_INFO(n)` bits;
  `FENYKL_HUD_INFO_SCREENS` covers basic info and Custom1 (section 6). Layer 1 masked always hides.

### 3.1 Drawn elements and stock images

For content that is not text, such as the focus meter's row of stock cells, the plugin draws the element itself: `fenykl_hud_draw` hands the callback the
element's spare bitmap, `w * h` bytes row by row, already filled with `bg`, and publishes it like a
text.
The bytes are layer-1 palette indices; `FENYKL_HUD_CLEAR` is transparent. The callback runs in the
caller's task, before anything is published.

`fenykl_hud_image` decodes a stock image at runtime from the firmware's image table (`gui_image_table`
-> id table -> record +0x10 -> RLE bitmap), the same path as
the LV digits; nothing is copied into the plugin. Only fmt 0x03 (8 bpp, six 10-bit codes per u64)
is decoded: its colours are layer-1 indices used 1:1, 0 is transparent and
stays 0 in `px`. It returns `FENYKL_HUD_EGLYPH` if the table or the image is missing, the image is
another format, or it has more than `max` pixels. Decode once at start into the plugin's own arrays
and draw from those.

The exposure-row cells used by `projects/main`'s focus meter (`feat_fmeter.c`, `main_layout.h`) are
images 1089 (white: fill 0x04, translucent border 0x11), 1090 (yellow) and 1092 (green), all 8 x 9;
main draws ten of them 6 px apart, unlit cells filled with 0x2E. The example below shows only the
callback mechanics.

```c
static int fmeter_paint(uint8_t *box, int w, int h, void *arg)
{
	struct fmeter *m = arg;

	for (int k = 0; k < 10; k++)
		for (int y = 0; y < 9; y++)
			for (int x = 0; x < 8; x++) {
				uint8_t v = m->cell[m->kind[k]][y * 8 + x];

				if (v)
					box[(1 + y) * w + 8 * k + x] = v;
			}
	return 0;
}
```

## 4. Rules and counters

The widget backend's rules and counters are in section 6.

## 5. Tests

`tests/test_hud_sim.py` builds the English glyph table and the LV image table from the stock firmware
with the Python decoder (`fenykl.assets`; the image ids from the firmware's own image-font tables),
runs `tests/sim/hud/test_hud.c` in the GDB simulator and compares every rendered rectangle (centred
black box, transparent left-aligned text with every spacing rule, translucent right-aligned text of
three lines with other ink, a clipped box; in the LV style main's focus and DoF blocks, every image
with letters (and `%`) right-aligned on black in orange ink (the images' white in that ink), a centred clipped box) with the decoder's glyphs and images
placed by the spacing rules and the line pitch, each line aligned by its own width, pixel for pixel.
LV letters are compared with a Python model of the scaling, bold and outline rule.
The image table holds the firmware's own encoded image records and bytes, read from the stock
firmware when the test runs (nothing stored in the repo); the dump is the bitmap the widget's image
is encoded from. `tests/sim/hud/test_hud_em10ii.c` checks the
visibility predicate (GUI state, display request, output device, layer mask, INFO screen from the
property registry and its override table, unregistered property), the output device per
`disp_device` value, the palette pick (opaque entries below 0x80 only) and the widget backend
against mocks of the factory, the child list, the attach method, `gui_image_set_handle`, `gui_post`
and the stock functions behind the slots (`tests/sim/hud/fw_em10ii.S`), entered through the slot
words: the slots all or nothing, the pool's limit, the records the factory gets (id, rects, mode, image list), attach,
the image each widget shows at a create (the newest content even before the panel is on screen),
one post per tick for all changes and none without a panel on screen, the swaps (each record
decoded and compared with the rendered bitmap), the empty image on another INFO screen and after a
hide, the content back without re-encoding, a lost event posted again after 5 ticks, a refused post
retried, another panel's destroy, refused widgets, an unexpected rect count, CPU1 calls and the GUI
heap's free bytes. `tests/sim/hud/test_hud_rle.c` checks the fmt 0x03 encoder (`lib/hud_rle.c`)
with a decoder written from the format: pixels, runs within rows, the stock size rule, the buffer
bounds. The module skips without the stock firmware sample.

## 6. E-M10 II

Rendering, styles and elements are the shared ones; `lib/hud_em10ii.c` puts each element on the
screen as a stock image widget of the GUI and `lib/hud_rle.c` encodes its bitmaps. No hook and no
layer-1 writes: the GUI draws, clips and repaints the widgets with its own items and deletes them
with their panel, so the HUD goes with the info panel (movie live view, the menu, playback) and
comes back with it, with no erase.

- **Panel and slots:** the widgets live on the still live-view info panel, panel id 10 (class
  vtable 0x60C9801C, the only panel of that class), which every still INFO screen, the LCD and the
  EVF keep. `fenykl_hud_init(states)` checks the ISO item's widget record (0x60C95AAC: type 0x8003,
  id 0x91) and replaces three vtable words all or nothing (`target.toml` `sdk_slots`): the panel's
  OnCreate (vt+0x1A8, word 0x60C981C4), its destroy (vt+0x98, 0x60C980B4) and the GUI app's
  OnMessage (vt+0x198, 0x60BD88C0), OnCreate last. They stay replaced for the boot. All three run in
  the GUI task by static analysis; a call on another core is counted (`cpu1`) and passed on (a
  destroy still drops the widget pointers: the panel objects reuse two addresses).
- **Create:** after the stock OnCreate, one widget per element added so far: a copy of the ISO
  record with id 0xF0 + index (above the panel's 0x96-entry id lookup), both rects = the element's
  rect, draw mode 2 and one image-list entry, made by the stock factory with parent = panel (so it
  comes from the panel's GUI heap pool and the panel's OnDestroy deletes it with its own
  destructor), appended to the panel's child list (drawn last, on top of the stock items), attached
  (vt+0x94) and given the image it should show. An element added later gets its widget at the next
  create: at boot `main` adds its elements before the GUI first builds the panel (about 3 s after
  power-on); if the panel is up already, the HUD appears after the next menu, playback or movie
  visit.
- **Destroy:** the wrapper only drops the widget pointers when the panel is the one holding them
  (the next panel is created before the old one is destroyed). The library never frees a widget.
- **Updates:** each element has three stock image records (type 0x0E, 8 bpp, fmt 0x03 bitmap read
  through the uncached alias, key 0): two for its content and an empty, all transparent one.
  `fenykl_hud_text` and `fenykl_hud_draw` render and publish (section 1). `fenykl_hud_tick`
  encodes each changed element that should show its content into the content record the GUI side
  neither holds as newest nor shows, writes the D-cache back once, and posts the private GUI event
  0x7E47 (no operand in the firmware uses it) once for all elements whose widget shows other than
  it should. The OnMessage wrapper takes the new records and sets each such widget's image handle
  (`gui_image_set_handle`), which invalidates its rect; the GUI repaints it with its background
  (type 0x0E is a transparent type), so a transparent element loses its old pixels. Widgets are
  touched only while their panel is the one on screen; without one nothing is posted and the next
  create shows the newest records. An event that has not arrived after 5 ticks is posted again
  (`unstuck`; GESetEvent can report a post it did not queue); a refused post (`post_fail`) is
  retried at the next tick. So at most one event and one D-cache write-back per tick.
- **Visible INFO screens:** the panel carries the widgets on every INFO screen, so the library
  shows the empty image while `fenykl_hud_visible` is false (another INFO screen, display off or
  closed, layer 1 masked) or the element is hidden, and its content again when visible; each tick
  compares.
- **Visibility** (`fenykl_hud_visible`): layer 1 not masked, GUI live view (super state 1, state 8),
  display request 0xF001 (the live-view info display), output device LCD or EVF, and the INFO screen
  in the set given to `fenykl_hud_init` as `FENYKL_HUD_INFO(n)` bits. The INFO screen is property
  0x8321 (`fenykl_hud_info_screen`: 1 basic info, 0 image only, 0x11 Custom1, 0x12 Custom2, the same on
  the LCD and in the EVF; Custom1/2 show the histogram and the level gauge by default), read from
  the property registry like the firmware's lookup 0x60076078 (override table first while it is
  valid; `fenykl_hud_prop` reads any s8 property the same way). `FENYKL_HUD_INFO_SCREENS` = basic
  info and Custom1. Open: the movie live view takes its INFO screen from property 0x8B01 (the
  firmware's INFO selector 0x60913732 switches to it when 0x609136A9 sees setting 0x2C, prop
  0x8551, or setting 0x3B6A at 1 through the settings layer), so there the HUD follows the still
  live view's 0x8321; main logs 0x8321, 0x8B01, 0x8551 and the decision on every change.
- **Placement:** rects are on the panel's 640 x 480 layout (`FENYKL_HUD_CANVAS_W`), where the stock
  items' rects lie: the EVF's screen positions. `fenykl_hud_output` tells the LCD (`disp_device`
  1) from the EVF (6). The camera run placed a widget at (100, 233) on the same spot of the LCD and
  the EVF. The LCD (720 wide) moves stock items further right (right column +80, centre about +40);
  where it puts an element at x >= 165 is not verified, so layouts keep to x < 165 (`main` does).
- **Memory:** per element its two `w * h` bitmaps, two content buffers of the encoding's worst case
  (4 + 8 * ceil(w * h / 6) bytes) and the empty one, from a fixed pool in the library's bss
  (0xD800 bytes; `main`'s five elements take 54108), `ENOMEM` once it is used up. Not `fw_malloc`:
  it gives every request over 0x800 bytes a block of the firmware's large class, shared with the
  firmware. The records sit in the library's state. GUI heap: 416 B per widget on the camera,
  returned with the panel.
- **Counters** (`struct fenykl_hud_stats`): `creates`, `made`, `failed` (factory or child list
  refused, unexpected rect count), `destroys`, `heap_free` (free bytes of the panel's GUI heap pool
  after the last create), `posts`, `post_fail`, `unstuck`, `messages`, `swaps`, `cpu1`, `nofont`,
  `glyph_miss`.
- **Colours:** only indices below 0x80 reach the display (128-entry hardware palette). The stock
  colours sit at the indices of `include/hud.h` in the live-view layer-1 palette (camera dump) and in 19 of
  the 59 stock GUI palettes, about every third (black 0x10, shadows 0x11/0x12, white 4..6, green
  7..9, yellow 1..3);
  `FENYKL_HUD_CLEAR` is 0x00 (alpha 0). There is no fixed red: `fenykl_hud_color(rgb)` picks the
  nearest opaque entry below 0x80 of the live layer-1 palette (`osd_palette`).
- **Font and images:** `gpi_get_glyph` 0x609315E2 and the font resource pointer; the
  image table through the GUI's pointer `gui_image_table`. The LV style's images are image font 14
  ('-' 1120, '1'..'9' 1121..1129, '0' 1130, ':' 1131, '+' 1132) and '.' 1871 of image font 25 (13 x 22, ':' and '.' 6 x 22, white 4/5/6, outline 0x10/0x11/0x12).

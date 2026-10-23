# HUD library (`sdk/lib/hud.c`)

Status: simulator-tested (`tests/test_hud_sim.py`); on the camera (E-PL3) through `projects/main`
(its smoke test). E-M10 II: simulator, a camera probe (2026-10) that drew on the LCD and in the EVF,
and `projects/main`'s battery percentage on the camera (2026-10-23; section 6). Images and colours
are the ones the GUI draws [camera OSD dumps]. Linked into every plugin, like all of `sdk/lib/`; a
plugin that does not call it gets no byte of it (`--gc-sections`).

## 1. What it does

Text elements on OSD layer 1 of the live view, without flicker, in one of two styles:
`FENYKL_HUD_PLAIN`, the loaded language's 32 px UI font, or `FENYKL_HUD_LV`, the look of the stock
live-view info items (section 3). The GUI only queues its drawing during a frame, executes the batch in `gpi_flush` and then
shows the buffer with `cw_updateDisplay(0)`. The plugin hooks `cw_updateDisplay` and calls
`fenykl_hud_frame` before the original; it copies every published element into the layer-1 buffer
about to be shown, after the GUI's batch, so every shown frame contains it.

Rendering (glyph lookup with `gpi_get_glyph`, fmt 0x09 RLE decode, the GUI's spacing rules; in the LV
style also the stock images, fmt 0x03) runs in the caller's task, into the spare one of two bitmaps per element, which is then published with one 32-bit
word. The hook only reads that word and copies rows; it makes no firmware calls.

## 2. API

Header `sdk/include/hud.h` (the reference). Errors are `FENYKL_HUD_E*` (negative).

| Call | Context | Does |
| --- | --- | --- |
| `fenykl_hud_init(states, buf[2])` | CPU0 task, once | checks the layer-1 buffers (handles, plane, size, address, overlap, 8 bpp), fills `buf` for the plugin's log, sets the visible states |
| `fenykl_hud_add(e)` | CPU0 task, after init | checks `e`'s rectangle, allocates its two bitmaps (`fw_malloc`, `w * h` <= 0x3FFC each) |
| `fenykl_hud_text(e, s)` | CPU0 task | renders `s`, publishes it, paints it into both buffers if visible; `ENOFONT` / `EGLYPH` leave `e` unchanged |
| `fenykl_hud_draw(e, fn, arg)` | CPU0 task | clears `e`'s spare bitmap to `bg`, calls `fn(box, w, h, arg)` to draw into it, then publishes and paints it as `fenykl_hud_text`; a nonzero return of `fn` is returned and leaves `e` unchanged (section 3.1) |
| `fenykl_hud_image(id, px, max, w, h)` | any | decodes stock image `id` (fmt 0x03) from the firmware into `px` (section 3.1) |
| `fenykl_hud_hide(e)` | CPU0 task | unpublishes `e`; clears its rectangle to `FENYKL_HUD_CLEAR` now and at the next two ticks (if visible) |
| `fenykl_hud_tick()` | CPU0 task, ~100 ms | repaints an element the hook copied while it changed; repeats hide clears |
| `fenykl_hud_text_width(style, s)` | CPU0 task | width in a style with the current font, for sizing elements |
| `fenykl_hud_font()`, `fenykl_hud_visible()`, `fenykl_hud_get_stats(st)` | any CPU0 context | font id (0 = none yet), the visibility gate, counters |
| `fenykl_hud_frame(show_front)` | the plugin's `cw_updateDisplay` hook | copies the elements for `show_front == 0`; counts and skips CPU1 calls |

Hooks stay in the plugin (`FENYKL_HOOK_*` and the originals, `FENYKL_ORIG(name)`, are generated per
plugin from `plugin.toml`), so the plugin owns the two-line adapter:

```c
static void on_update(int show_front)
{
	fenykl_hud_frame(show_front);
	FENYKL_ORIG(cw_updateDisplay)(show_front);
}
FENYKL_HOOK(cw_updateDisplay, on_update);
```

with `hooks = ["cw_updateDisplay"]` and `cpu1_unproven = ["cw_updateDisplay"]` in `plugin.toml`.

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
- **Background:** `bg` per element. `FENYKL_HUD_CLEAR` (0xFF) writes only glyph pixels over a
  transparent rectangle: the native look, but the rectangle overwrites whatever the GUI had there, so it
  must be free of stock items in every visible state. `FENYKL_HUD_BLACK` (0x10) or
  `FENYKL_HUD_TRANSLUCENT` (0x11) draw a box.
- **Palette:** `ink[3]` per element maps glyph codes 1, 2 (anti-alias) and 3 (body);
  `FENYKL_HUD_WHITE` is `{0x06, 0x05, 0x04}`, the stock items' white [camera]. Coloured inks are
  the live-view L1 palette's other text ramps (body, 2/3 and 1/3 brightness, as the white one; the
  stock palette 4, which matches the camera's L1 copy): `FENYKL_HUD_GREEN` `{0x09, 0x08, 0x07}`,
  `FENYKL_HUD_YELLOW` `{0x03, 0x02, 0x01}` (the stock exposure-compensation yellow [camera]) and
  `FENYKL_HUD_RED` `{0x10, 0x39, 0x39}` (0x39 is the palette's only red and has no ramp, so the
  darkest shade is the outline black). Other screens may load another L1 palette (Multi View
  does), but the HUD is not visible there.
- **Style:** `style` per element.
  - `FENYKL_HUD_PLAIN` (0): the UI font as the menus draw it, 19 px caps, 2 px strokes, no outline.
  - `FENYKL_HUD_LV`: the stock live-view info items' look [camera]. `0`-`9`, `+`,
    `-`, `:` and `.` are the GUI's own images (image font 12 of the remaining-time counter; `.` from
    image font 16): bold condensed white digits, 17 px tall, with the stock 1 px black outline (0x10)
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
- **Visible states:** the `states` mask of `fenykl_hud_init`, bit n = state n of super state 0x02
  (`SS_REC`). `FENYKL_HUD_INFO_SCREENS` covers the info screens Basic 0x03,
  Histogram 0x04, Highlight&Shadow 0x05. Layer
  1 masked (hide mask bit 0x10) always hides.

### 3.1 Drawn elements and stock images

For content that is not text, such as the focus meter's row of stock cells, the plugin draws the element itself: `fenykl_hud_draw` hands the callback the
element's spare bitmap, `w * h` bytes row by row, already filled with `bg`, and publishes it with the
text path's flicker-free scheme (both canvases painted from the task, the hook copies at every flip).
The bytes are layer-1 palette indices; `FENYKL_HUD_CLEAR` is transparent. The callback runs in the
caller's task, before anything is published.

`fenykl_hud_image` decodes a stock image at runtime from the firmware's common resource (`*(u32
*)0xAF966010` -> id table -> record +0x10 -> RLE bitmap), the same path as
the LV digits; nothing is copied into the plugin. Only fmt 0x03 (8 bpp, six 10-bit codes per u64)
is decoded: its colours are layer-1 indices used 1:1, 0 is transparent and
stays 0 in `px`. It returns `FENYKL_HUD_EGLYPH` if the table or the image is missing, the image is
another format, or it has more than `max` pixels. Decode once at start into the plugin's own arrays
and draw from those; the common resource never moves, so re-decoding is never needed.

The exposure-row cells, all 8 x 9 with a 1 px transparent ring (so cells 8 px apart leave the stock
2 px gap): 1095 white (fill 0x04, border 0x7E), 1096 yellow (0x2E, border 0x10), 1097 orange (0x0D,
border 0x7E), 1098 green (0x32, border 0x10). The stock exposure scale itself is one joined bar:
cells at a pitch of 6 px sharing 2 px black separators, unmarked cells palette index 0x35 (grey,
alpha 170). `projects/main` (`feat_fmeter.c`) draws its focus meter that way: the decoded cells with
1095's translucent border made black, unlit cells filled with 0x35. The example below shows only the
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

- Draw at `cw_updateDisplay(0)` only; calls with `show_front != 0` re-show the front, which already
  holds the elements.
- An element's whole rectangle is rewritten at each copy, so a new value erases the old one.
- While hidden by state, the hook does not touch layer 1; the GUI repaints those screens itself.
- Counters (`struct fenykl_hud_stats`): `updates`, `shows`, `drawn`, `hidden` and `overrides` count hook
  calls and element copies; `erased` counts copies that found the GUI had painted over an element
  copied into that buffer before (each is a frame the old `gpi_flush`-entry method showed without it);
  `stale` counts tick repaints after a race; `cpu1` counts hook calls on CPU1 (must stay 0);
  `nofont` and `glyph_miss` count refused renders.

## 5. Tests

`tests/test_hud_sim.py` builds the English glyph table and the LV image table from the stock firmware
with the Python decoder (`fenykl.assets`; the image ids from the firmware's own image-font tables),
runs `tests/sim/hud/test_hud.c` in the GDB simulator and compares every rendered rectangle (centred
black box, transparent left-aligned text with every spacing rule, translucent right-aligned text of
three lines with other ink, a clipped box; in the LV style main's focus and DoF blocks, every image
with letters (and `%`) right-aligned on black in orange ink (the images' white in that ink), a centred clipped box) with the decoder's glyphs and images
placed by the spacing rules and the line pitch, each line aligned by its own width, pixel for pixel.
LV letters are compared with a Python model of the scaling, bold and outline rule. The C side checks the hook: erase counting, no erase after a hidden
state or a value published while masked, the one-layer override, `show_front != 0`, the three hide
clears, refused rectangles and texts, LV digits without a font, a missing image table.
The image table holds the firmware's own encoded image records and bytes, read from the stock
firmware when the test runs (nothing stored in the repo). `tests/sim/hud/test_hud_draw.c` decodes
images 1095-1098 with `fenykl_hud_image` (compared with `fenykl.assets` pixel for pixel), refuses a
too large digit, the fmt 0x02 AF dot, a missing id and a missing table, draws a 10-cell row of every
cell kind and faint cells into the focus meter's element (620, 42, 80, 12) through `fenykl_hud_draw`
(compared with the decoder's cells placed 8 px apart; transparent rows and 2 px gaps checked), and
checks the callback's arguments, both buffers, and that a callback error leaves the element
unchanged. The text cases also run for the E-M10 II (its English font and its live-view images,
read the same way from its stock firmware, at its firmware addresses); `tests/sim/hud/test_hud_em10ii.c`
checks the E-M10 II visibility predicate (GUI state, display request, output device, layer mask, INFO
screen from the property registry and its override table, unregistered property), the output device
per `disp_device` value, the palette pick (opaque entries below 0x80 only) and `fenykl_hud_init` with
the E-M10 II handle layout (window sizes up to the buffer, handle and buffer address rules). The
module skips without the stock firmware sample.

## 6. E-M10 II

The library is the same; `lib/hud_int.h` holds what differs per camera, `lib/hud_em10ii.c` the E-M10
II's own part. `main` draws its battery percentage with it on this camera (camera run 2026-10-23:
shown on the basic and histogram INFO screens, hidden in MENU, the Super Control Panel and
playback).

- **Hook and buffers:** the same `cw_updateDisplay` hook and `show_front == 0` rule, drawing into
  layer 1's `b[gpi_draw_buf]` (or the one-layer override's buffer) before the original; the same
  CPU1 guard (core id != 0, which also covers CPU2). Layer-1 handles are `{pdata, plane, w, h, x, y}`
  in the GUI's bss above the code image; `w`/`h` are the layer window, at most 896 x 600. Buffers
  are 896-byte pitch, 600 lines, 8 bpp, in uncached DRAM (0xBFCEC800 / 0xBFD6FC00 on the camera).
- **Visibility** (`fenykl_hud_visible`): layer 1 not masked, GUI live view (super state 1, state 8),
  display request 0xF001 (the live-view info display), output device LCD or EVF, and the INFO screen
  in the set given to `fenykl_hud_init` as `FENYKL_HUD_INFO(n)` bits. The INFO screen is property
  0x8321 (`fenykl_hud_info_screen`: 1 basic info, 0 image only, 0x11 Custom1, 0x12 Custom2, the same on
  the LCD and in the EVF; Custom1/2 show the histogram and the level gauge by default), read from
  the property registry like the firmware's lookup 0x60076078 (override table first while it is
  valid). `FENYKL_HUD_INFO_SCREENS` = basic info and Custom1.
- **Output and region** (`fenykl_hud_output`): `disp_device` 1 = rear LCD, shows the 720 x 480 canvas
  1:1; 6 = built-in EVF, shows its left 640 x 480 (`FENYKL_HUD_EVF_W`) scaled to 1024 x 768; anything
  else (closed, external) hides the HUD. Elements are fixed rectangles, so a layout for both outputs
  keeps to x < 640 or swaps elements on an output change.
- **Colours:** only indices below 0x80 reach the display (128-entry hardware palette). The stock
  colours sit at the E-PL3's indices in the live-view layer-1 palette (camera dump) and in 19 of
  the 59 stock GUI palettes, about every third (black 0x10, shadows 0x11/0x12, white 4..6, green
  7..9, yellow 1..3);
  `FENYKL_HUD_CLEAR` is 0x00 (alpha 0). There is no fixed red: `fenykl_hud_color(rgb)` picks the
  nearest opaque entry below 0x80 of the live layer-1 palette (`osd_palette`).
- **Font and images:** `gpi_get_glyph` 0x609315E2 and the font resource pointer as on the E-PL3; the
  image table through the GUI's pointer `gui_image_table`. The LV style's images are image font 14
  ('-' 1120, '1'..'9' 1121..1129, '0' 1130, ':' 1131, '+' 1132) and '.' 1871 of image font 25: the
  E-PL3's shapes (13 x 22, ':' and '.' 6 x 22, white 4/5/6, outline 0x10/0x11/0x12).

/*
 * The screenshot's pixel work and rules (shot_pix.c), free of firmware calls so that the simulator
 * tests (tests/sim/shot/) run the real code: live view YUYV and the OSD palettes to RGB, the OSD
 * layers scaled and blended over the live view on the canvas (the panel's w x h), the 16:9 stretch,
 * the PPM header, the layout of the lent buffer and the start and abort rules.
 */
#ifndef _FENYKL_SHOT_INT_H
#define _FENYKL_SHOT_INT_H

#include <stdint.h>
#include <body.h>

#define FENYKL_SHOT_LAYERS 3u
#define FENYKL_SHOT_ALPHA_MAX FENYKL_OSD_ALPHA_MAX	/* opaque palette alpha */
#define FENYKL_SHOT_HDR_MAX 24u

/* One OSD layer: palette indices, 8 bpp or 4 bpp (low nibble = left pixel). */
struct fenykl_shot_layer {
	const uint8_t *pix;	/* row 0; NULL = not drawn (hidden) */
	uint32_t pitch;		/* bytes */
	uint8_t bpp;
	uint16_t entries;	/* palette entries; higher indices are transparent */
	const uint8_t *pal;	/* {R, G, B, alpha 0..FENYKL_SHOT_ALPHA_MAX} per entry */
	uint16_t w, h;		/* source size, scaled to the canvas by nearest pixel */
};

/* The face/AF frame layer: 8 bpp indices, w x h scaled to to_w x to_h from the canvas origin. */
struct fenykl_shot_frames {
	const uint8_t *pix;	/* NULL = none */
	uint32_t pitch;
	uint16_t w, h, to_w, to_h;	/* to_w, to_h within the canvas */
	uint16_t entries;	/* index 0 and indices >= entries are transparent */
	const uint8_t *pal;	/* {R, G, B, pad} per entry, opaque */
};

struct fenykl_shot_frame {
	const uint8_t *lv;	/* YUYV of canvas pixel (lv_x, lv_y); NULL = black */
	uint32_t lv_pitch;
	uint16_t lv_x, lv_w;	/* both even, lv_x + lv_w <= w */
	uint16_t lv_y, lv_h;	/* lv_y + lv_h <= h; black outside */
	struct fenykl_shot_frames frames;	/* over the live view, under the layers */
	struct fenykl_shot_layer layer[FENYKL_SHOT_LAYERS];	/* bottom to top */
	uint16_t w, h;		/* the canvas, w <= FENYKL_DISP_W_MAX */
};

/* Offsets in the lent buffer: the snapshot (summed), then on FENYKL_SHOT_STRIP_LENT the strip. */
struct fenykl_shot_layout {
	uint32_t lv, lv_row;	/* live view: h rows of lv_row bytes */
	uint32_t osd[FENYKL_SHOT_LAYERS], osd_row[FENYKL_SHOT_LAYERS];	/* osd_h rows each */
	uint32_t frames;	/* FENYKL_FRAMES_PITCH x FENYKL_FRAMES_H */
	uint32_t snap;		/* snapshot bytes; the strip starts here */
	uint32_t strip;		/* bytes of one strip: FENYKL_SHOT_STRIP_ROWS file rows */
	uint32_t bytes;		/* to lend */
};

/* Full-range BT.601 YCbCr to RGB. */
void fenykl_shot_ycc_rgb(unsigned int y, unsigned int cb, unsigned int cr, uint8_t *rgb);
/* n palette entries in the body's format (FENYKL_OSD_PAL_YCC) -> {R, G, B, alpha 0..
   FENYKL_SHOT_ALPHA_MAX}, the alpha scaled by the layer's global alpha (0x100 = 1.0). */
void fenykl_shot_palette(const uint8_t *src, unsigned int n, unsigned int global, uint8_t *rgba);
/* Canvas row y of the frame: live view, face/AF frames, then each drawn layer blended in order.
   rgb: f->w * 3. */
void fenykl_shot_compose(const struct fenykl_shot_frame *f, unsigned int y, uint8_t *rgb);
/* Linear horizontal resample of an RGB row from in_w to out_w pixels (pixel centres aligned). */
void fenykl_shot_stretch(const uint8_t *in, unsigned int in_w, uint8_t *out, unsigned int out_w);
/* "P6\n<w> <h>\n255\n" into out (FENYKL_SHOT_HDR_MAX); returns its length. */
unsigned int fenykl_shot_ppm_header(char *out, unsigned int w, unsigned int h);
/* The lent buffer's layout for geometry g. */
void fenykl_shot_layout(const struct fenykl_disp_geom *g, struct fenykl_shot_layout *lay);
/* Why a shot must not start (start NULL) or must stop now (start: the state at its snapshot);
   NULL = go on. */
const char *fenykl_shot_gate(const struct fenykl_shot_cond *c, const struct fenykl_shot_cond *start);

#endif

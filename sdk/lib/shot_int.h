/*
 * The screenshot's pixel work (shot_pix.c), free of firmware calls so that the simulator test
 * (tests/sim/shot/) runs the real code: live view YUYV and the OSD palettes to RGB, the OSD layers
 * blended over the live view on the canvas (FENYKL_DISP_W x FENYKL_DISP_H, the body's panel), the
 * 16:9 stretch and the PPM header.
 */
#ifndef _FENYKL_SHOT_INT_H
#define _FENYKL_SHOT_INT_H

#include <stdint.h>
#include <body.h>

#define FENYKL_SHOT_LAYERS 3u
#define FENYKL_SHOT_ALPHA_MAX 15u		/* OSD palette alpha 0..15 */
#define FENYKL_SHOT_HDR_MAX 24u

/* One OSD layer: palette indices, 8 bpp or 4 bpp (low nibble = left pixel). */
struct fenykl_shot_layer {
	const uint8_t *pix;	/* row 0; NULL = not drawn (hidden) */
	uint32_t pitch;		/* bytes */
	uint8_t bpp;
	uint16_t entries;	/* palette entries; higher indices are transparent */
	const uint8_t *pal;	/* {R, G, B, alpha 0..15} per entry */
};

/* The face/AF frame layer: 8 bpp indices, w x h scaled to to_w x to_h from the canvas origin. */
struct fenykl_shot_frames {
	const uint8_t *pix;	/* NULL = none */
	uint32_t pitch;
	uint16_t w, h, to_w, to_h;	/* to_w <= FENYKL_DISP_W, to_h <= FENYKL_DISP_H */
	uint16_t entries;	/* index 0 and indices >= entries are transparent */
	const uint8_t *pal;	/* {R, G, B, pad} per entry, opaque */
};

struct fenykl_shot_frame {
	const uint8_t *lv;	/* YUYV of canvas pixel (lv_x, lv_y); NULL = black */
	uint32_t lv_pitch;
	uint16_t lv_x, lv_w;	/* both even, lv_x + lv_w <= FENYKL_DISP_W */
	uint16_t lv_y, lv_h;	/* lv_y + lv_h <= FENYKL_DISP_H; black outside */
	struct fenykl_shot_frames frames;	/* over the live view, under the layers */
	struct fenykl_shot_layer layer[FENYKL_SHOT_LAYERS];	/* bottom to top */
};

/* Full-range BT.601 YCbCr to RGB. */
void fenykl_shot_ycc_rgb(unsigned int y, unsigned int cb, unsigned int cr, uint8_t *rgb);
/* n palette entries {Y, Cb, Cr, alpha} -> {R, G, B, alpha} (alpha masked to 0..15). */
void fenykl_shot_palette(const uint8_t *ycca, unsigned int n, uint8_t *rgba);
/* Canvas row y of the frame: live view, face/AF frames, then each drawn layer blended in order. rgb:
   FENYKL_DISP_W * 3. */
void fenykl_shot_compose(const struct fenykl_shot_frame *f, unsigned int y, uint8_t *rgb);
/* Linear horizontal resample of an RGB row from in_w to out_w pixels (pixel centres aligned). */
void fenykl_shot_stretch(const uint8_t *in, unsigned int in_w, uint8_t *out, unsigned int out_w);
/* "P6\n<w> <h>\n255\n" into out (FENYKL_SHOT_HDR_MAX); returns its length. */
unsigned int fenykl_shot_ppm_header(char *out, unsigned int w, unsigned int h);

#endif

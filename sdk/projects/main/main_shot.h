/*
 * main: the screenshot's pixel work, free of firmware calls so that the simulator test
 * (tests/sim/main/) runs the real code: live view YUYV and the OSD palettes to RGB, the OSD layers
 * blended over the live view on the 720x480 canvas, the 16:9 stretch and the PPM header.
 */
#ifndef _MAIN_SHOT_H
#define _MAIN_SHOT_H

#include <stdint.h>

#define MAIN_SHOT_W 720u		/* OSD canvas = shown part of the LV buffer */
#define MAIN_SHOT_H 480u
#define MAIN_SHOT_OUT_W 853u		/* 720 * 32 / 27: the canvas is shown stretched to 16:9 */
#define MAIN_SHOT_LAYERS 3u
#define MAIN_SHOT_ALPHA_MAX 15u		/* OSD palette alpha 0..15 */
#define MAIN_SHOT_HDR_MAX 24u

/* One OSD layer: palette indices, 8 bpp or 4 bpp (low nibble = left pixel). */
struct main_shot_layer {
	const uint8_t *pix;	/* row 0; NULL = not drawn (hidden) */
	uint32_t pitch;		/* bytes */
	uint8_t bpp;
	uint16_t entries;	/* palette entries; higher indices are transparent */
	const uint8_t *pal;	/* {R, G, B, alpha 0..15} per entry */
};

struct main_shot_frame {
	const uint8_t *lv;	/* YUYV of canvas pixel (lv_x, lv_y); NULL = black */
	uint32_t lv_pitch;
	uint16_t lv_x, lv_w;	/* both even, lv_x + lv_w <= MAIN_SHOT_W */
	uint16_t lv_y, lv_h;	/* lv_y + lv_h <= MAIN_SHOT_H; black outside */
	struct main_shot_layer layer[MAIN_SHOT_LAYERS];	/* bottom to top */
};

/* Full-range BT.601 YCbCr to RGB. */
void main_shot_ycc_rgb(unsigned int y, unsigned int cb, unsigned int cr, uint8_t *rgb);
/* n palette entries {Y, Cb, Cr, alpha} -> {R, G, B, alpha} (alpha masked to 0..15). */
void main_shot_palette(const uint8_t *ycca, unsigned int n, uint8_t *rgba);
/* Canvas row y of the frame: live view, then each drawn layer blended in order. rgb: 720 * 3. */
void main_shot_compose(const struct main_shot_frame *f, unsigned int y, uint8_t *rgb);
/* Linear horizontal resample of an RGB row from in_w to out_w pixels (pixel centres aligned). */
void main_shot_stretch(const uint8_t *in, unsigned int in_w, uint8_t *out, unsigned int out_w);
/* "P6\n<w> <h>\n255\n" into out (MAIN_SHOT_HDR_MAX); returns its length. */
unsigned int main_shot_ppm_header(char *out, unsigned int w, unsigned int h);

#endif

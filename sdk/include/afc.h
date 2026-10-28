/*
 * The plugin's own contrast client (sdk/docs/afc.md): AF statistics in manual focus, where the
 * firmware runs no contrast client. Attaches with cam_request a copy of fcs's contrast_setting
 * (snapshot taken in S-AF/C-AF framing) writing into the library's DMA buffer, detaches on any
 * gate. The firmware layouts are the body's (body.h, afc group). Camera-proven in MF on the
 * E-PL3. Built for targets with the afc capability; zero bytes unless fenykl_afc_poll is called.
 */
#ifndef _FENYKL_AFC_H
#define _FENYKL_AFC_H

#include <stdbool.h>
#include <fenykl.h>
#include <body.h>
#include "log.h"

#define FENYKL_AFC_BOOT_MS 5000u	/* passive after fenykl_afc_init */
#define FENYKL_AFC_SETTLE_MS 500u	/* gates clear this long before an attach */
#define FENYKL_AFC_COOLDOWN_MS 1000u	/* after a detach */
#define FENYKL_AFC_FRAME_MS 500u	/* first frame after the attach; longest gap while attached */
#define FENYKL_AFC_DONE_MS 500u		/* done callback after the first frame following a stop */

/* Gates: a set bit blocks an attach and stops an attached client. */
#define FENYKL_AFC_G_SNAP (1u << 0)	/* no contrast_setting snapshot yet */
#define FENYKL_AFC_G_MF (1u << 1)	/* not manual focus */
#define FENYKL_AFC_G_LV (1u << 2)	/* GUI not on a stills live-view screen */
#define FENYKL_AFC_G_MOVIE (1u << 3)	/* movie or another mode to avoid (fenykl_cnt_movie) */
#define FENYKL_AFC_G_HALF (1u << 4)	/* shutter button half or fully pressed */
#define FENYKL_AFC_G_FCS (1u << 5)	/* fcs contrast state not idle */
#define FENYKL_AFC_G_SLOT (1u << 6)	/* a foreign client linked in the driver */
#define FENYKL_AFC_G_DRV (1u << 7)	/* driver debug mode on */
#define FENYKL_AFC_G_OFF (1u << 8)	/* the caller disabled the client */
/* Stop reason only (fenykl_afc_sm.why): frames stopped while attached (live view gone). */
#define FENYKL_AFC_W_STALL (1u << 9)

enum fenykl_afc_state {
	FENYKL_AFC_IDLE,
	FENYKL_AFC_ATTACHING,	/* cam_request sent, no frame yet */
	FENYKL_AFC_ATTACHED,
	FENYKL_AFC_DETACHING,	/* number = 0 written, waiting for the done callback */
	FENYKL_AFC_OFF,		/* passive for the boot */
};

enum fenykl_afc_action { FENYKL_AFC_NONE, FENYKL_AFC_ATTACH, FENYKL_AFC_STOP };

enum fenykl_afc_err {
	FENYKL_AFC_E_NONE,
	FENYKL_AFC_E_REQ,	/* cam_request returned nonzero */
	FENYKL_AFC_E_NOFRAME,	/* no frame callback within FRAME_MS of the attach */
	FENYKL_AFC_E_NODONE,	/* frames flow after the stop, but no done callback within DONE_MS */
	FENYKL_AFC_E_EARLY,	/* done callback while attached, no stop requested */
	FENYKL_AFC_E_END,	/* fenykl_afc_end */
	FENYKL_AFC_E_EVENT,	/* the frame callback got the error event */
	FENYKL_AFC_E_CORE,	/* a callback ran on another core than CPU0 */
};

/* State machine input (afc_sm.c): one poll. */
struct fenykl_afc_in {
	uint32_t now;		/* ms */
	uint32_t gates;		/* FENYKL_AFC_G_* failing */
	uint32_t frames;	/* frame callbacks so far */
	uint32_t done;		/* done callbacks so far */
	uint32_t cb_stops;	/* frames on which the frame callback wrote number = 0 */
};

struct fenykl_afc_sm {
	enum fenykl_afc_state state;
	uint32_t since;		/* ms: entry into the state */
	uint32_t clear_since;	/* ms: gates clear since then */
	bool clear;
	uint32_t frames;	/* frame count at the last new frame */
	uint32_t frame_at;	/* ms of the last new frame */
	uint32_t stop_frames;	/* detaching: frame count at the stop */
	bool flowing;		/* detaching: a frame came after the stop; DONE_MS runs from flow_at */
	uint32_t flow_at;
	uint32_t done;		/* done count seen */
	uint32_t cb_stops;	/* cb_stops count seen */
	uint32_t idle_at;	/* ms of the last return to idle */
	uint32_t why;		/* gates (or W_STALL) at the last stop, 0 on an error stop */
	bool by_cb;		/* the last stop was the frame callback's */
	enum fenykl_afc_err err;	/* first error; any error ends in OFF */
};

/*
 * Written by the callbacks (firmware context, CPU0, interrupt context), read by the task. A call
 * on another core only counts itself in the library's uncached word (fenykl_afc_other_core).
 */
struct fenykl_afc_cb {
	uint32_t calls, st, cp, err;	/* callbacks, frame start, data complete, error events */
	uint32_t irq, bad_req;		/* in interrupt context, A0 not our request */
	uint32_t events, unk, psw;	/* of the last call */
	uint32_t last_us, gap_us;	/* last call, longest gap since the attach */
	uint32_t first_us;		/* first call after the attach, 0 = none yet */
	uint32_t first_nest, first_psw, first_a0;	/* its interrupt nesting byte, PSW and A0 */
	uint32_t stop;			/* the task asks the callback to write number = 0 */
	uint32_t cb_stops;		/* frames on which the callback wrote number = 0 */
	uint32_t cb_stop_us;		/* first such write since the attach, 0 = none */
	uint32_t done, done_status, done_us, done_nest;
};

#define FENYKL_AFC_ROW_WORDS 16u	/* u32 per grid row in a channel */
#define FENYKL_AFC_DMA_FILL 0xA5A5A5A5u	/* the buffer before the first DMA write of an attach */

/* The library's state, read-only for plugins. */
struct fenykl_afc {
	struct fenykl_afc_sm sm;
	volatile struct fenykl_afc_cb cb;
	uint32_t ms;			/* since fenykl_afc_init */
	uint32_t gates;			/* at the last poll */
	bool snapped;
	uint8_t snap_pat, snap_cols, snap_rows;	/* grid of the snapshot */
	uint32_t snaps, torn;		/* snapshots taken, copies that disagreed */
	uint32_t attaches;
	uint32_t req_us, stop_us;	/* timer at the last cam_request, at the last task stop */
	uint32_t attach_ms;
	uint32_t attach_calls, attach_st;	/* callback and AF_ST counts at the last attach */
	uint8_t snap[FENYKL_CNT_SET_SIZE] __attribute__((aligned(4)));
	uint8_t setting[FENYKL_CNT_SET_SIZE] __attribute__((aligned(4)));	/* the attached copy */
	/* private */
	uint8_t tmp[2][FENYKL_CNT_SET_SIZE] __attribute__((aligned(4)));
	uint32_t snap_ms, last_us, rem_us;
	bool pend_logged, nosnap_logged, err_logged;
	uint32_t req[FENYKL_CNT_REQ_SIZE / 4];	/* the cam_request object */
};

/* Module "afc": attach, stop, done, the first snapshot at INFO; errors at ERR; later snapshots at DEBUG. */
extern struct fenykl_log_module fenykl_afc_log;

/* Starts the clock (FENYKL_AFC_BOOT_MS). CPU0 task, once, before the first poll. */
void fenykl_afc_init(void) FENYKL_CPU0_ONLY("fenykl_afc_init");
/* Plugin task, every 10..50 ms: snapshot of fcs's setting, gates (enable false = G_OFF), state machine,
   attach or stop. */
void fenykl_afc_poll(bool enable) FENYKL_CPU0_ONLY("fenykl_afc_poll");
/* Stops for good (err END): number = 0 now and by the callback at every later frame; no poll needed after. */
void fenykl_afc_end(void) FENYKL_CPU0_ONLY("fenykl_afc_end");
const struct fenykl_afc *fenykl_afc_get(void);
/* The statistics buffer (uncached, FENYKL_CNT_STATS_CHANNELS from HPF1): channel c, area i at
   c * rows * ROW_WORDS + i / cols * ROW_WORDS + i % cols; FILL until the DMA wrote it (from the
   second frame of an attach). */
const volatile uint32_t *fenykl_afc_dma(void);
/* Callbacks that ran on another core than CPU0 (and did nothing else). */
uint32_t fenykl_afc_other_core(void);
/* An error stopped the client for the boot (not END). */
bool fenykl_afc_failed(void);

extern const char *const fenykl_afc_state_name[];
extern const char *const fenykl_afc_err_name[];

#endif

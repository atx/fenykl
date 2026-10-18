/* hookprobe input map: the appevent_post hook queues each CPU0 event with the input state that goes
   with it (dial counters, touch record, eye state); a 20 ms poll in the plugin task logs the queue
   and every change of the polled input state, raw. Reads only. Addresses from the v1.4 code: */
#include <fenykl.h>
#include <log.h>

#include "hookprobe.h"

/* UI_IN key kind state bytes, kind table 0x601A90EC: kind 0 0xA1C18030 (1 byte), kind 1
   0xA1C1802C (2), kind 4 0xA1C18025 (2, mode dial ladder), kind 5 0xA1C18024 (1), kind 7 0xA1C18028 (2). */
#define HPI_KIND0 0xA1C18030u
#define HPI_KIND1 0xA1C1802Cu
#define HPI_KIND4 0xA1C18025u
#define HPI_KIND5 0xA1C18024u
#define HPI_KIND7 0xA1C18028u
/* Mode dial ladder index, s8 (-1 none): read by 0x6017F32D. Override u16: api_user_Get_ModeDialState
   0x602E3350. */
#define HPI_MODE_INDEX 0xA1C17C70u
#define HPI_MODE_OVERRIDE 0x618750F8u
/* Dial state 0xA1C17B80 + 0x1C * dial (dial 0 = 0x02031903, dial 1 = 0x02031902, jog table
   0x601A8810): +0x12 last position, +0x14 / +0x16 the two direction counters (ui_jog_read_count
   0x6017E906..0x6017E91F). */
#define HPI_DIAL(n, off) (0xA1C17B80u + 0x1Cu * (n) + (off))
/* Eye sensor state, u8 (per_eye_get_state 0x60084EB4). */
#define HPI_EYE 0xA1BF98FEu
/* Touch: open flag u8 (tp_deliver 0x602FA479), gesture mask (0x602FA5D1), last gesture (stored
   at 0x602FA4BB), record {gesture, u16 *xy, t0, t1} handed to the callback (0x602FA48E), its xy
   pointer initialised to {u16 x, u16 y} at 0x618795FC (0x602FA0BA). */
#define HPI_TP_OPEN 0x618795F0u
#define HPI_TP_MASK 0x618795F4u
#define HPI_TP_LAST 0x6187962Cu
#define HPI_TP_REC 0x61879600u
#define HPI_TP_XY 0x618795FCu

#define HPI_EV_DIAL 0x0203u  /* event class of the dial steps */
#define HPI_EV_TOUCH 0x0416u /* event class of the touch gestures */
#define HPI_EV_EYE 0x02022301u
#define HPI_WORDS 10u
#define HPI_RING 64u            /* events between two 20 ms drains; overflow is counted */
#define HPI_LOG_MAX 6000u       /* poll lines */
#define HPI_EVENT_LOG_MAX 2000u /* events */

#define U8(a) (*(volatile uint8_t *)(a))
#define U16(a) (*(volatile uint16_t *)(a))
#define U32(a) (*(volatile uint32_t *)(a))

/* Codes polled with ui_key_get_state; 0x02021901 returns the mode dial value. */
static const uint32_t hpi_codes[] = {
	0x0004, 0x0005, 0x0006, 0x0007, 0x0008, 0x000B, 0x000C, 0x0084, 0x0087, 0x0089, 0x0100, 0x0101,
	0x1205, 0x1209, 0x120A, 0x1215, 0x121C, 0x1223, 0x1224, 0x2302, 0x2402, 0x0400, 0x1506,
	0x02021901,
};
#define HPI_CODES (sizeof(hpi_codes) / sizeof(hpi_codes[0]))

static const char *const hpi_names[HPI_WORDS] = {
	"kinds 0,1,1,5", "kinds 4,4,7,7", "mode index,override", "dial0 ctr", "dial1 ctr",
	"dial pos 0,1", "eye,tp open", "tp mask", "tp last", "tp yx",
};

/* An appevent_post call on CPU0 as the hook saw it. */
struct hpi_event {
	uint32_t stamp; /* FENYKL_LOG_TIMER_NOW, us */
	uint32_t event, param, flag;
	uint32_t task; /* get_tid result << 16 | task id */
	uint32_t x[4]; /* dial: counters 0, 1, positions; touch: rec, yx (or the xy pointer), t0, t1; eye */
};

static struct {
	struct hpi_event ring[HPI_RING];
	uint32_t head; /* hook */
	uint32_t tail; /* poll */
	uint32_t ev_logged;
	uint32_t ev_dropped; /* ring full */
	uint32_t words[HPI_WORDS];
	uint32_t keys[HPI_CODES];
	uint32_t logged;
	uint32_t suppressed;
	int started;
} hpi;

static uint32_t hpi_dial_counters(unsigned int dial)
{
	return U16(HPI_DIAL(dial, 0x14)) << 16 | U16(HPI_DIAL(dial, 0x16));
}

static uint32_t hpi_dial_positions(void)
{
	return U16(HPI_DIAL(0, 0x12)) << 16 | U16(HPI_DIAL(1, 0x12));
}

static void hpi_capture(struct hpi_event *e)
{
	if (e->event >> 16 == HPI_EV_DIAL) {
		e->x[0] = hpi_dial_counters(0);
		e->x[1] = hpi_dial_counters(1);
		e->x[2] = hpi_dial_positions();
	} else if (e->event >> 16 == HPI_EV_TOUCH) {
		uint32_t xy = U32(HPI_TP_REC + 4);

		e->x[0] = U32(HPI_TP_REC);
		e->x[1] = xy == HPI_TP_XY ? U32(HPI_TP_XY) : xy; /* another pointer is not read */
		e->x[2] = U32(HPI_TP_REC + 8);
		e->x[3] = U32(HPI_TP_REC + 12);
	} else if (e->event == HPI_EV_EYE) {
		e->x[0] = U8(HPI_EYE);
	}
}

/* Hook context, CPU0: queues the event, no log call (small stack: TOUCHPANEL has 1 KiB). */
void hp_input_event(unsigned int event, unsigned int param, unsigned int flag)
{
	uint16_t tid = 0;
	int r = fw_get_tid(&tid);
	uint32_t psw = fw_psw_get();

	fw_irq_disable_all();
	if (hpi.head - hpi.tail >= HPI_RING) {
		hpi.ev_dropped++;
	} else {
		struct hpi_event *e = &hpi.ring[hpi.head % HPI_RING];

		*e = (struct hpi_event){ FENYKL_LOG_TIMER_NOW(), event, param, flag,
					 (uint32_t)r << 16 | tid, { 0 } };
		hpi_capture(e);
		hpi.head++;
	}
	fw_irq_mask_restore(psw);
}

static void hpi_log_event(const struct hpi_event *e)
{
	fenykl_log_info(&hp_log, "ev %08x param %08x flag %04x task %08x", e->event, e->param,
			e->flag & 0xFFFF, e->task);
	if (e->event >> 16 == HPI_EV_DIAL)
		fenykl_log_info(&hp_log, "  at %u dial ctr %08x %08x pos %08x", e->stamp, e->x[0],
				e->x[1], e->x[2]);
	else if (e->event >> 16 == HPI_EV_TOUCH)
		fenykl_log_info(&hp_log, "  at %u touch rec %08x yx %08x t0 %08x", e->stamp, e->x[0],
				e->x[1], e->x[2]);
	else if (e->event == HPI_EV_EYE)
		fenykl_log_info(&hp_log, "  at %u eye %02x", e->stamp, e->x[0]);
	else
		fenykl_log_info(&hp_log, "  at %u", e->stamp);
}

static void hpi_drain(void)
{
	while (hpi.tail != hpi.head) {
		struct hpi_event e;
		uint32_t psw = fw_psw_get();

		fw_irq_disable_all();
		e = hpi.ring[hpi.tail % HPI_RING];
		hpi.tail++;
		fw_irq_mask_restore(psw);
		if (hpi.ev_logged >= HPI_EVENT_LOG_MAX)
			continue;
		hpi.ev_logged++;
		hpi_log_event(&e);
	}
}

static void hpi_read(uint32_t *w)
{
	w[0] = U8(HPI_KIND0) << 24 | U8(HPI_KIND1) << 16 | U8(HPI_KIND1 + 1) << 8 | U8(HPI_KIND5);
	w[1] = U8(HPI_KIND4) << 24 | U8(HPI_KIND4 + 1) << 16 | U8(HPI_KIND7) << 8 | U8(HPI_KIND7 + 1);
	w[2] = U8(HPI_MODE_INDEX) << 16 | U16(HPI_MODE_OVERRIDE);
	w[3] = hpi_dial_counters(0);
	w[4] = hpi_dial_counters(1);
	w[5] = hpi_dial_positions();
	w[6] = U8(HPI_EYE) << 8 | U8(HPI_TP_OPEN);
	w[7] = U32(HPI_TP_MASK);
	w[8] = U32(HPI_TP_LAST);
	w[9] = U32(HPI_TP_XY);
}

static void hpi_line(const char *fmt, uint32_t a, uint32_t b, uint32_t c)
{
	if (hpi.logged >= HPI_LOG_MAX) {
		hpi.suppressed++;
		return;
	}
	hpi.logged++;
	fenykl_log_info(&hp_log, fmt, a, b, c);
}

/* Plugin task, every 20 ms: logs the queued events, then each polled value that changed (all of
   them on the first call). */
void hp_input_poll(void)
{
	uint32_t w[HPI_WORDS];

	hpi_drain();
	hpi_read(w);
	for (unsigned int i = 0; i < HPI_WORDS; i++) {
		if (hpi.started && w[i] == hpi.words[i])
			continue;
		hpi_line("in %s %08x (was %08x)", (uint32_t)hpi_names[i], w[i], hpi.words[i]);
		hpi.words[i] = w[i];
	}
	for (unsigned int i = 0; i < HPI_CODES; i++) {
		uint32_t v = fw_ui_key_get_state(hpi_codes[i]);

		if (hpi.started && v == hpi.keys[i])
			continue;
		hpi_line("key %08x state %x (was %x)", hpi_codes[i], v, hpi.keys[i]);
		hpi.keys[i] = v;
	}
	hpi.started = 1;
}

void hp_input_report(unsigned int period)
{
	fenykl_log_info(&hp_log, "p%u input lines %u suppressed %u", period, hpi.logged,
			hpi.suppressed);
	fenykl_log_info(&hp_log, "p%u events logged %u queued %u dropped %u", period, hpi.ev_logged,
			hpi.head - hpi.tail, hpi.ev_dropped);
}

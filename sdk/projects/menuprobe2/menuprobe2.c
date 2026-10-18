/* E-M10 II menu probe 2: the Eye-Fi row of Custom menu tab K as an entry point. Condition 113 of the
   rule of setting 0x193 answers 0 (allowed), so the row is no longer grayed for lack of an Eye-Fi
   card; the other seven conditions still apply. The `fn` words of the row's OK and RIGHT dispatch
   records get a callback that logs and returns 4, so the stock On/Off pop-up of setting 0x193 still
   opens. Every call also logs usrset_get(0x193); that it reads the store the pop-up writes is not verified. All three slots
   are restored after MP2_RUN_TICKS. Data writes only; card writes only through the log library. */
#include <fenykl.h>
#include <log.h>

#define MP2_RUN_TICKS 300000	/* ~5 min with the slots replaced */
#define MP2_POLL_TICKS 1000
#define MP2_LOG_CALLS 64	/* callback calls logged per kind; the rest are counted */
#define MP2_SETTING_EYEFI 0x193u
#define MP2_ROW_CONTINUE 4	/* ctl_row_dispatch then opens the record's (group, target) controller */
#define MP2_ACT_ROWS 0x54u	/* sub-actuator: the row-list object */
#define MP2_ROWS_COUNT 0x44u	/* row list: u16 row count */
#define MP2_ROWS_INDEX 0x46u	/* row list: u16 cursor index */
#define MP2_ROWS_IDS 0x4Cu	/* row list: pointer to the u16 row ids */
#define MP2_ROW_NONE 0x7D0u	/* what the id getter 0x609ED6E7 returns for a bad index */

static struct {
	volatile uint32_t cond_calls, cond_blocked, cond_other;
	volatile uint32_t row_calls[2], row_other;
} mp2;

static FENYKL_LOG_MODULE(mp2_mod, "mprobe2", FENYKL_LOG_INFO);

/* Runs inside mode_check in the caller's task: records only. */
static unsigned int mp2_cond113(void)
{
	unsigned int r = fw_orig_mode_cond_eyefi();
	uint32_t n = ++mp2.cond_calls;

	if (r)
		mp2.cond_blocked++;
	if (FENYKL_CORE_ID()) {
		mp2.cond_other++;
		return 0;
	}
	if (n <= MP2_LOG_CALLS)
		fenykl_log_info(&mp2_mod, "c113 #%u: stock %x -> 0, usrset_get(193) %x (unverified store)", n, r,
				fw_usrset_get(MP2_SETTING_EYEFI));
	return 0;
}
FENYKL_CALLBACK(mode_cond, mp2_cond113);

/* The row under the cursor, read as the dispatcher does: the row-list index, then the id getter's rule. */
static unsigned int mp2_row(void *act)
{
	const uint8_t *rows = *(uint8_t *const *)((uint8_t *)act + MP2_ACT_ROWS);
	unsigned int n = *(const uint16_t *)(rows + MP2_ROWS_COUNT);
	unsigned int i = *(const uint16_t *)(rows + MP2_ROWS_INDEX);

	if (i >= n)
		return MP2_ROW_NONE;
	return (*(const uint16_t *const *)(rows + MP2_ROWS_IDS))[i];
}

static int mp2_row_cb(void *act, unsigned int kind)
{
	const char *name = kind ? "RIGHT" : "OK";
	uint32_t n = ++mp2.row_calls[kind];

	if (FENYKL_CORE_ID()) {
		mp2.row_other++;
		return MP2_ROW_CONTINUE;
	}
	if (n > MP2_LOG_CALLS)
		return MP2_ROW_CONTINUE;
	fenykl_log_info(&mp2_mod, "%s #%u: a0 %08x row %x", (uint32_t)name, n, (uint32_t)act,
			mp2_row(act));
	fenykl_log_info(&mp2_mod, "%s #%u: core %u, usrset_get(193) %x (unverified store)", (uint32_t)name, n,
			FENYKL_CORE_ID(), fw_usrset_get(MP2_SETTING_EYEFI));
	return MP2_ROW_CONTINUE;
}

static int mp2_ok(void *act)
{
	return mp2_row_cb(act, 0);
}
FENYKL_CALLBACK(menu_row, mp2_ok);

static int mp2_right(void *act)
{
	return mp2_row_cb(act, 1);
}
FENYKL_CALLBACK(menu_row, mp2_right);

static void mp2_restore(void)
{
	int a = fenykl_slot_restore(&fenykl_slot_eyefi_ok_fn);
	int b = fenykl_slot_restore(&fenykl_slot_eyefi_right_fn);
	int c = fenykl_slot_restore(&fenykl_slot_mode_cond_eyefi);

	fenykl_log_info(&mp2_mod, "restored: ok %d, right %d, cond %d", a, b, c);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int init = fenykl_log_init(NULL);
	int task = fenykl_log_task_start();
	uint32_t seen = 0;
	int ok, right, cond;

	fenykl_log_info(&mp2_mod, "menuprobe2: load %08x size %u, log init %d task %d", load_base,
			image_size, init, task);
	/* sec4 (conditions, menu tables) is loaded: the loader starts the payload only after sec4 and sec5. */
	ok = FENYKL_SLOT_REPLACE(eyefi_ok_fn, mp2_ok);
	right = FENYKL_SLOT_REPLACE(eyefi_right_fn, mp2_right);
	cond = FENYKL_SLOT_REPLACE(mode_cond_eyefi, mp2_cond113);
	fenykl_log_info(&mp2_mod, "replaced: ok %d, right %d, cond %d", ok, right, cond);
	if (ok || right || cond) {
		mp2_restore();
		return;
	}
	for (uint32_t t = 0; t < MP2_RUN_TICKS; t += MP2_POLL_TICKS) {
		uint32_t sum;

		fw_OlyTaskSleep(MP2_POLL_TICKS);
		sum = mp2.cond_calls + mp2.row_calls[0] + mp2.row_calls[1];
		if (sum == seen)
			continue;
		seen = sum;
		fenykl_log_info(&mp2_mod, "t %u s: cond %u (stock blocked %u), ok %u",
				t / 1000u, mp2.cond_calls, mp2.cond_blocked, mp2.row_calls[0]);
		fenykl_log_info(&mp2_mod, "t %u s: right %u, not CPU0 %u", t / 1000u, mp2.row_calls[1],
				mp2.cond_other + mp2.row_other);
	}
	mp2_restore();
}

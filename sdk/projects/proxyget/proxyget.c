/* First camera test of reverse thunks and cloned vtables: the settings proxy object
   *(void **)0x60357B50 gets a clone of its vtable 0x6EE3F58C with method +8 (getSetting, entered with
   `calls`) replaced by a C function. The replacement counts calls by core, remembers the last id and
   the number of failed reads, and always returns what the original returned (fw_orig_*), leaving *out
   as the original wrote it. The menu reads every row's value through this method, so browsing the
   menu makes the count climb.

   A worker task waits for the object, attaches the clone, logs a snapshot every LOG_PERIOD ticks,
   detaches after RUN_SNAPSHOTS and logs AFTER_SNAPSHOTS more: the count must then stay still.
   C:\FENYKL.LOG is always LOG_SIZE bytes, padded with spaces ("w" does not truncate). The replacement
   never logs: it runs in the GUI/HFW tasks. */
#include <fenykl.h>

#define PROXY_PTR 0x60357B50u /* hfw_setting_proxy singleton: object = {vtable, usrsetmgr *} */
#define CORE_ID (*(volatile uint32_t *)0x8050u) /* 0 on CPU0, non-zero on CPU1 */
#define STACK_SIZE 0x2000u
#define WAIT_POLL 100 /* ticks between looks for the object */
#define WAIT_POLLS 600 /* ~60 s */
#define LOG_PERIOD 3000 /* ticks between snapshots, about 3 s (projects/tickprobe) */
#define RUN_SNAPSHOTS 30 /* ~90 s with the clone attached */
#define AFTER_SNAPSHOTS 4 /* after detaching: the count must not grow */
#define LOG_SIZE 0x2000u
#define LOG_LINE 64u /* padding line length */

static volatile uint32_t calls_cpu0, calls_cpu1, last_id, failed;

static int counted_get(void *self, unsigned *out, unsigned id)
{
	if (CORE_ID == 0)
		calls_cpu0++;
	else
		calls_cpu1++;
	last_id = id;
	int r = fw_orig_hfw_setting_proxy_get(self, out, id);
	if (r != 0)
		failed++;
	return r;
}
FENYKL_CALLBACK(hfw_getSetting, counted_get);

static char logbuf[LOG_SIZE];
static unsigned loglen;
static char line[128];

/* Appends a line and rewrites the whole log, padded to LOG_SIZE bytes. */
static void log_line(void)
{
	for (unsigned i = 0; line[i] && loglen < LOG_SIZE - LOG_LINE; i++)
		logbuf[loglen++] = line[i];
	for (unsigned i = loglen; i < LOG_SIZE; i++) {
		unsigned col = i % LOG_LINE;
		logbuf[i] = col == LOG_LINE - 2 ? '\r' : col == LOG_LINE - 1 ? '\n' : ' ';
	}
	void *fp = fw_rpc_fopen("C:\\FENYKL.LOG", "w");
	if (!fp)
		return;
	fw_rpc_fwrite(logbuf, 1, LOG_SIZE, fp);
	fw_rpc_fclose(fp);
}

static void snapshot(const char *tag, uint32_t *object)
{
	fw_sprintf(line, "%s calls=%u cpu1=%u last_id=%x failed=%u vptr=%08x\r\n", tag, calls_cpu0,
		   calls_cpu1, last_id, failed, object[0]);
	log_line();
}

static uint32_t *wait_for_proxy(void)
{
	for (int i = 0; i < WAIT_POLLS; i++) {
		uint32_t *object = *(uint32_t *volatile *)PROXY_PTR;
		if (object)
			return object;
		fw_OlyTaskSleep(WAIT_POLL);
	}
	return 0;
}

FENYKL_TASK(proxyget);
void proxyget(void)
{
	struct fenykl_vtable *vt = &fenykl_vtable_hfw_setting_proxy;
	uint32_t *object = wait_for_proxy();

	if (!object) {
		fw_sprintf(line, "no proxy object at %08x, nothing patched\r\nend of log\r\n", PROXY_PTR);
		log_line();
		return;
	}
	fw_sprintf(line, "object %08x vptr %08x mgr %08x (stock vtable %08x, clone %08x)\r\n",
		   (uint32_t)object, object[0], object[1], vt->address, (uint32_t)vt->clone);
	log_line();

	FENYKL_VTABLE_SET(hfw_setting_proxy_get, counted_get);
	int r = fenykl_vtable_attach(vt, object);
	fw_sprintf(line, "attach %d\r\n", r);
	log_line();
	if (r != 0) {
		fw_sprintf(line, "nothing patched\r\nend of log\r\n");
		log_line();
		return;
	}
	snapshot("attached", object);
	for (int i = 0; i < RUN_SNAPSHOTS; i++) {
		fw_OlyTaskSleep(LOG_PERIOD);
		snapshot("run", object);
	}

	r = fenykl_vtable_detach(vt);
	fw_sprintf(line, "detach %d\r\n", r);
	log_line();
	snapshot("detached", object);
	for (int i = 0; i < AFTER_SNAPSHOTS; i++) {
		fw_OlyTaskSleep(LOG_PERIOD);
		snapshot("after", object);
	}
	fw_sprintf(line, "end of log\r\n");
	log_line();
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	fw_sprintf(line, "fenykl proxyget, load %08x size %08x\r\n", load_base, image_size);
	log_line();
	void *stack = fw_malloc(STACK_SIZE);
	if (!stack)
		return;
	fw_OlyTaskCreate("PROXYGET", proxyget_task, 2, 0, 11, STACK_SIZE, stack);
}

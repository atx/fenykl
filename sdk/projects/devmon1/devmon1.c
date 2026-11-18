/* devmon1: the dev monitor library (docs/devmon.md) and the settings page that holds its options;
   nothing else. RAM only: no hook, no slot of its own, no setting written. */
#include <cfg.h>
#include <devmon.h>
#include <log.h>
#include <ui.h>

#define DV_TICK_MS 100
#define DV_UI_TRIES 600

FENYKL_LOG_MODULE(dv_log, "devmon1", FENYKL_LOG_INFO);

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int log = fenykl_log_init(NULL);
	int cfg, mon, ui = FENYKL_UI_EAGAIN;
	unsigned int i;

	fenykl_log_task_start();
	cfg = fenykl_cfg_init();
	fenykl_cfg_task_start();
	mon = fenykl_devmon_start();
	fenykl_log_info(&dv_log, "load %x size %x: log init %d, cfg init %d", load_base,
			image_size, log, cfg);
	fenykl_log_info(&dv_log, "monitor task %d", mon);
	for (i = 0; i < DV_UI_TRIES && ui == FENYKL_UI_EAGAIN; i++) {
		ui = fenykl_ui_install();
		if (ui == FENYKL_UI_EAGAIN)
			fenykl_task_sleep(DV_TICK_MS);
	}
	fenykl_log_info(&dv_log, "ui install %d after %u tries", ui, i);
	for (;;) {
		fenykl_task_sleep(DV_TICK_MS);
		fenykl_ui_tick();
	}
}

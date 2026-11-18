/* Dev monitor (E-M10 II, docs/devmon.md): a headless Wi-Fi access point and a TCP monitor on CPU1
   that a PC drives with `fenykl dev`. Opt-in: a plugin links it by listing [capabilities.devmon]
   in plugin.toml. Its options (devmon.ap, devmon.off) are on the settings page. */
#ifndef FENYKL_DEVMON_H
#define FENYKL_DEVMON_H

/* Starts the monitor's CPU0 task; call once from plugin_main after fenykl_log_init and
   fenykl_cfg_init. The task id or < 0. */
int fenykl_devmon_start(void) FENYKL_CPU0_ONLY("fenykl_devmon_start");

#endif

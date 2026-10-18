/* hookprobe: shared between the hooks (hookprobe.c) and the input map (hp_input.c). */
#ifndef HOOKPROBE_H
#define HOOKPROBE_H

#include <log.h>

extern struct fenykl_log_module hp_log;

void hp_input_event(unsigned int event, unsigned int param, unsigned int flag);
void hp_input_poll(void);
void hp_input_report(unsigned int period);

#endif

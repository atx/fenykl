/* fenykl plugin SDK. See sdk/README.md. */
#ifndef FENYKL_H
#define FENYKL_H

#include <stddef.h>
#include <stdint.h>

#include "fenykl_fw.h"
#include "fenykl_target.h"

/* FENYKL_LOAD_BASE (where the loader puts FENYKL.BIN) and FENYKL_REGION_END (payload incl. .bss ends
   here) are defined by the build from the target's target.toml. */

/* lib/string.c */
void *memcpy(void *restrict dst, const void *restrict src, size_t n);
void *memmove(void *dst, const void *src, size_t n);
void *memset(void *dst, int c, size_t n);
int memcmp(const void *a, const void *b, size_t n);

/* Implemented by the plugin; called once in the loader's FENYKL task (priority 11, 16 KiB stack)
   after .bss is cleared. Returning ends the task. Never busy-wait (watchdog, see README). */
void plugin_main(uint32_t load_base, uint32_t image_size);

/* Firmware services every target has, for plugins that name no firmware symbol themselves. The
   firmware.toml entries of OlyTaskSleep, rpc_fopen, rpc_fwrite, rpc_fclose and sprintf hold their
   exact semantics; the file functions are CPU0-only. */
static inline int fenykl_task_sleep(int ticks)
{
	return fw_OlyTaskSleep(ticks);
}

static inline void *fenykl_fopen(const char *path, const char *mode)
{
	return fw_rpc_fopen(path, mode);
}

static inline int fenykl_fwrite(const void *buf, unsigned int size, unsigned int n, void *fp)
{
	return fw_rpc_fwrite(buf, size, n, fp);
}

static inline int fenykl_fclose(void *fp)
{
	return fw_rpc_fclose(fp);
}

/* fw_sprintf: at most 8 variadic words. */
#define fenykl_sprintf(...) fw_sprintf(__VA_ARGS__)

/* Task and interrupt helpers, implemented per target in fenykl_target.h (E-PL3 only so far). */
/* Masks this core's interrupts; fenykl_irq_restore(the result) undoes it. */
static inline uint32_t fenykl_irq_save(void);
static inline void fenykl_irq_restore(uint32_t psw);
/* A task running entry (FENYKL_TASK) at priority prio (lower runs first), stack from the heap: its
   id, < 0 on failure. */
static inline int fenykl_task_create(const char *name, void (*entry)(void), int prio,
				     uint32_t stack_bytes);
/* Waits for fenykl_task_wake or ticks (ms): 0 woken (also by a wakeup queued while running),
   FENYKL_TASK_ETIMEOUT. fenykl_task_sleep does not take wakeups. */
static inline int fenykl_task_wait(int ticks);
/* Wakes task id from fenykl_task_wait, or queues it for its next wait; any task. 0 or < 0. */
static inline int fenykl_task_wake(int id);

/*
 * FW_TIMER_NOW: free-running 30-bit counter at 1 MHz (counts down; this counts up), both
 * cores. FENYKL_TIMER_ELAPSED is the wrap-safe difference.
 */
#define FENYKL_TIMER_MASK 0x3FFFFFFFu
#define FENYKL_TIMER_NOW() (FENYKL_TIMER_MASK - *(volatile uint32_t *)FW_TIMER_NOW)
#define FENYKL_TIMER_ELAPSED(from, to) (((to) - (from)) & FENYKL_TIMER_MASK)

/* Volatile reads and writes of firmware memory at address a. */
static inline uint32_t fenykl_rd8(uint32_t a)
{
	return *(const volatile uint8_t *)(uintptr_t)a;
}

static inline uint32_t fenykl_rd16(uint32_t a)
{
	return *(const volatile uint16_t *)(uintptr_t)a;
}

static inline uint32_t fenykl_rd32(uint32_t a)
{
	return *(const volatile uint32_t *)(uintptr_t)a;
}

static inline void fenykl_wr8(uint32_t a, uint32_t v)
{
	*(volatile uint8_t *)(uintptr_t)a = (uint8_t)v;
}

static inline void fenykl_wr16(uint32_t a, uint32_t v)
{
	*(volatile uint16_t *)(uintptr_t)a = (uint16_t)v;
}

static inline void fenykl_wr32(uint32_t a, uint32_t v)
{
	*(volatile uint32_t *)(uintptr_t)a = v;
}

/* a lies in the cached DRAM window [FW_DRAM_START, FW_DRAM_END) or its uncached alias
   (FENYKL_UNCACHED) and is a multiple of align, a power of two. */
static inline bool fenykl_dram_ptr(uint32_t a, uint32_t align)
{
	uint32_t size = FW_DRAM_END - FW_DRAM_START;

	return !(a & (align - 1)) && (a - FW_DRAM_START < size ||
				      a - (uint32_t)FENYKL_UNCACHED(FW_DRAM_START) < size);
}

/* Target: nonzero if this boot was started by plugging in the USB cable. The runtime then returns
   without calling plugin_main, unless plugin.toml sets run_on_usb = true (README, Startup). */
int fenykl_usb_attached(void);

/* Target, CPU0 task context (README, Startup and USB boots; unproven on the camera): the USB cable
   is in now (a plugged-in cable switches a running camera to USB in place). */
bool fenykl_usb_cable_present(void) FENYKL_CPU0_ONLY("fenykl_usb_cable_present");
/* Target, CPU0 task context: the camera has the card mounted (the cable is not checked). */
bool fenykl_card_mounted(void) FENYKL_CPU0_ONLY("fenykl_card_mounted");
/* CPU0 task context: no cable seen this boot and the card mounted; once a cable was seen it stays
   false until power-off. Plugins that write files check it before each open/write/close batch
   (README). */
bool fenykl_card_io_allowed(void) FENYKL_CPU0_ONLY("fenykl_card_io_allowed");
/* A cable was seen by fenykl_card_io_allowed this boot: the gate stays closed until power-off. */
bool fenykl_card_usb_seen(void);

/* Hooks (README, Hooks): FENYKL_HOOK(name, fn) makes `fn` the replacement of firmware function
   `name`, which plugin.toml must list in `hooks` (top level or a [capabilities.<cap>] table). `fn`
   has the signature fw_hook_<name>_fn and may call the original as FENYKL_ORIG(name)(...), which
   is fw_orig_<name>. Nothing changes until fenykl_hook_install. */
#define FENYKL_HOOK(name, fn) FENYKL_HOOK_##name(fn)
#define FENYKL_ORIG(name) fw_orig_##name

/* Descriptor of a hook (fenykl_hook_<name>, generated from firmware.toml by fenykl's fwapi.hook_asm,
   which emits this layout). Installed with fenykl_hook_install (arch/<camera>/hooks.c). */
struct fenykl_hook {
	uint32_t entry;       /* firmware function; its first 5 bytes become `jmp stub` */
	uint32_t stub;        /* generated entry stub */
	uint32_t installed;
	uint8_t length;       /* bytes of original[] that must be at the entry */
	uint8_t original[16]; /* stock entry bytes, also the trampoline's relocated instructions */
};

#define FENYKL_HOOK_EBYTES (-1)  /* the entry does not hold the expected bytes: nothing was written */
#define FENYKL_HOOK_EVERIFY (-2) /* the entry did not read back as written */

/* Patch / restore the entry of hook (&fenykl_hook_<name>). 0 on success or if already in that
   state, else FENYKL_HOOK_E*. Call from a task, never from a hook. */
int fenykl_hook_install(struct fenykl_hook *hook);
int fenykl_hook_remove(struct fenykl_hook *hook);

/* Callbacks and slots (README, Callbacks and slots). FENYKL_CALLBACK(sig, fn), at file scope
   after fn, makes `fn` (signature fw_cb_<sig>_fn) callable by the firmware with callback convention
   `sig`; FENYKL_CB(sig, fn) is then its firmware-callable address. fw_call_<sig>(addr, ...) calls a
   firmware function pointer with that convention. */
#define FENYKL_CALLBACK(sig, fn) FENYKL_CALLBACK_##sig(fn)
#define FENYKL_CB(sig, fn) ((uint32_t)fenykl_cb_##sig##_##fn)

/* For a slot plugin.toml lists in `slots`: FENYKL_SLOT_REPLACE(slot, fn) stores FENYKL_CB(<its sig>,
   fn) in the slot (fn must be bound with FENYKL_CALLBACK), fenykl_slot_restore(&fenykl_slot_<slot>)
   puts the stock value back; fw_orig_<slot>(...) calls the stock value. Both return 0 on success
   or if already in that state, else FENYKL_SLOT_E*. Call them from a task. */
#define FENYKL_SLOT_REPLACE(slot, fn) FENYKL_SLOT_REPLACE_##slot(fn)

#define FENYKL_SLOT_EVALUE (-1)  /* the word did not hold the expected value: nothing was written */
#define FENYKL_SLOT_EVERIFY (-2) /* the word did not read back as written */
#define FENYKL_SLOT_EBUSY (-3)   /* already replaced with something else: restore first */

int fenykl_slot_replace(struct fenykl_slot *slot, uint32_t fn);
int fenykl_slot_restore(struct fenykl_slot *slot);

#endif

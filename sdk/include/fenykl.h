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

/* Target: nonzero if this boot was started by plugging in the USB cable. The runtime then returns
   without calling plugin_main, unless plugin.toml sets run_on_usb = true (README, Startup). */
int fenykl_usb_attached(void);

/* Target, CPU0 task context (README, Startup and USB boots; unproven on the camera): the USB cable
   is in now (a plugged-in cable switches a running camera to USB in place). */
bool fenykl_usb_cable_present(void) FENYKL_CPU0_ONLY("fenykl_usb_cable_present");
/* Target, CPU0 task context: no cable and the camera has the card mounted. Plugins that write files
   check it before each open/write/close batch (README). */
bool fenykl_card_io_allowed(void) FENYKL_CPU0_ONLY("fenykl_card_io_allowed");

/* Hooks (README, Hooks): FENYKL_HOOK(name, fn) makes `fn` the replacement of firmware function
   `name`, which plugin.toml must list in `hooks`. `fn` has the signature fw_hook_<name>_fn and may
   call the original as fw_orig_<name>(...). Nothing changes until fenykl_hook_install. */
#define FENYKL_HOOK(name, fn) FENYKL_HOOK_##name(fn)

#define FENYKL_HOOK_EBYTES (-1)  /* the entry does not hold the expected bytes: nothing was written */
#define FENYKL_HOOK_EVERIFY (-2) /* the entry did not read back as written */

/* Patch / restore the entry of hook (&fenykl_hook_<name>). 0 on success or if already in that
   state, else FENYKL_HOOK_E*. Call from a task, never from a hook. */
int fenykl_hook_install(struct fenykl_hook *hook);
int fenykl_hook_remove(struct fenykl_hook *hook);

/* Callbacks, slots and vtables (README, Callbacks and slots). FENYKL_CALLBACK(sig, fn), at file scope
   after fn, makes `fn` (signature fw_cb_<sig>_fn) callable by the firmware with callback convention
   `sig`; FENYKL_CB(sig, fn) is then its firmware-callable address. fw_call_<sig>(addr, ...) calls a
   firmware function pointer with that convention. */
#define FENYKL_CALLBACK(sig, fn) FENYKL_CALLBACK_##sig(fn)
#define FENYKL_CB(sig, fn) ((uint32_t)fenykl_cb_##sig##_##fn)

/* For a slot plugin.toml lists in `slots`: FENYKL_SLOT_REPLACE(slot, fn) stores FENYKL_CB(<its sig>,
   fn) in the slot (fn must be bound with FENYKL_CALLBACK), fenykl_slot_restore(&fenykl_slot_<slot>)
   puts the stock value back; fw_orig_<slot>(...) calls the stock value. FENYKL_VTABLE_SET(slot, fn)
   sets that method in the clone of the listed vtable containing the slot; fenykl_vtable_attach
   gives `object` the clone, fenykl_vtable_detach gives it back the stock table. All return 0 on
   success or if already in that state, else FENYKL_SLOT_E*. Call them from a task. */
#define FENYKL_SLOT_REPLACE(slot, fn) FENYKL_SLOT_REPLACE_##slot(fn)
#define FENYKL_VTABLE_SET(slot, fn) FENYKL_VTABLE_SET_##slot(fn)

#define FENYKL_SLOT_EVALUE (-1)  /* the word did not hold the expected value: nothing was written */
#define FENYKL_SLOT_EVERIFY (-2) /* the word did not read back as written */
#define FENYKL_SLOT_EBUSY (-3)   /* already replaced/attached with something else: restore first */

int fenykl_slot_replace(struct fenykl_slot *slot, uint32_t fn);
int fenykl_slot_restore(struct fenykl_slot *slot);
int fenykl_vtable_attach(struct fenykl_vtable *vt, void *object);
int fenykl_vtable_detach(struct fenykl_vtable *vt);

#endif

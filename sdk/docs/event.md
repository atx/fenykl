# Event layer (`sdk/lib/event.c`)

Status: simulator-tested (`tests/sim/event/`); on the camera through a key probe (895 events:
`nested`, `bypassed`, `cpu1` 0, `injected` as expected; `docs/key.md` 4) and `projects/main`. Its user is
the key layer (`docs/key.md`); the `appevent_post` hook under it is camera-proven. Linked into every
plugin, like all of `sdk/lib/`; a plugin that does not call `fenykl_event_*` gets no byte of it
(`--gc-sections`).

## 1. What it does

Every APPEVENT the firmware posts goes through `appevent_post(event, param, flag)` (48 call sites,
CPU0 only): keys from `ui_key_task` (`0x0200cccc` press, `0x0201cccc` release, `0x0202` mode dial)
and system events from other tasks. The rear dial does not come through here: turned
on the camera, it posted nothing (`docs/key.md` 5). The SDK binds that hook
itself (`sdk_hooks` in `target.toml`): the generator emits its stub for every plugin, and a
`plugin.toml` that lists `appevent_post` in `hooks` is refused at build time. Plugin code registers
handlers instead; the hook runs them in priority order and finally posts what is left through the
stock entry. One binder, any number of features sharing it.

## 2. API

Header `sdk/include/event.h` (the reference). Errors are `FENYKL_EVENT_E*` (negative), each also
logged by module `event` (`fenykl_event_log`).

| Call | Context | Does |
| --- | --- | --- |
| `fenykl_event_add(h)` | any CPU0 context, before or after install | links `h` by `prio` (equal prio: registration order); `EARG` without `fn`, `EBUSY` if already linked |
| `fenykl_event_install()` | CPU0 task, not a handler | patches `appevent_post` (`fenykl_hook_install`); idempotent; `EHOOK` with the hook's code logged |
| `fenykl_event_remove()` | CPU0 task, not a handler | restores the stock entry; handlers stay linked |
| `fenykl_event_inject(event, param, flag)` | CPU0 task or handler, never an ISR | posts through the stock entry: no handler sees it; the firmware's result |
| `fenykl_event_get_stats(st)` | any | counters (section 4) |
| `fenykl_event_dispatch(event, param, flag)` | the hook | the hook's body; tests call it without the patch |

A handler node is the plugin's static storage and stays linked for the boot; `enabled = false`
pauses it (any context). The fields under `private` belong to the SDK (`next`, and the counters
`seen` and `consumed` of this handler).

```c
static enum fenykl_event_action on_event(struct fenykl_event *ev, void *arg)
{
	if (ev->event != (FENYKL_EVENT_KEY_PRESS | 0x000B))
		return FENYKL_EVENT_PASS;
	feat.req++;			/* the plugin task acts on it */
	return FENYKL_EVENT_CONSUME;
}

static struct fenykl_event_handler feat_handler = {
	.name = "feat", .fn = on_event, .prio = 100, .enabled = true,
};

/* plugin_main: */
	r = fenykl_event_add(&feat_handler);
	if (!r)
		r = fenykl_event_install();
```

## 3. Semantics

- **Order.** Ascending `prio`, equal `prio` in registration order. The key layer (`docs/key.md`)
  sits at 128; a handler below it sees raw events (a remap, a logger), above it what the key layer left.
  A probe that logs everything registers at 0 and passes.
- **PASS** hands the event to the next handler and finally to
  `fw_orig_appevent_post(ev->event, ev->param, ev->flag)`; the hook returns the firmware's result.
  A handler may edit the three fields first: later handlers and the firmware see the edit
  (**replace** = edit and pass).
- **CONSUME** drops the event: no later handler runs, the firmware never sees it, the hook returns
  0 (most callers ignore the result).
- **Inject** posts through the stock entry (the trampoline), never through the chain, so it cannot
  re-enter it. A held-back press is replayed by `inject(press)` and `PASS` of the release in one
  handler call (a `long_stock` Fn). The queue write may block like any firmware post: a task or a
  handler, never an ISR.
- **Registration** is safe against the hook at any time: the new node is complete before the one
  pointer store that links it, under the CPU0 interrupt mask, so a dispatch sees the old or the new
  chain and two registering contexts cannot lose a node.
- **CPU1.** On the E-PL3 `appevent_post` is CPU0-only (static and camera, firmware.toml); on the
  E-M10 II two Wi-Fi tasks post from CPU1 (static, `sdk_cpu1_unproven`). A CPU1 entry is counted
  (`cpu1`), the chain skipped and the stock function called. The caches are not coherent, so on both
  targets the CPU1 count lives in its own `FENYKL_SHARED` line, written uncached; nothing else is
  touched. Handlers therefore run on CPU0 only.

## 4. Handler rules and counters

The hook runs on the posting task's stack: `ui_key_task` (priority 7, stack 0x800, at least 1452 bytes free in
the camera probe: key.md 4) for keys, other firmware tasks for system events.

- No blocking firmware calls (file RPC, task create or sleep, queue waits other than inject), no
  `fenykl_hud_text`/`draw`, no `fenykl_cfg_set`, no `fenykl_event_install`/`remove`. Allowed:
  reading memory, `fw_ui_key_get_state`, `fenykl_log_*` (logging.md), `fenykl_event_inject`,
  `fenykl_event_add`, setting words the plugin task polls (e.g. a `req++`).
- Small frames: a handler stays under ~128 bytes of stack and does not recurse.
- A handler runs again when another task posts while a call on a lower-priority task is preempted
  (same core, different stacks). State touched from a handler is per-task-safe only for key-class
  events (all from `ui_key_task`); state shared with the plugin task is written with single word
  stores.
- **Never post through `fw_appevent_post`** from a handler: that enters the hook again. The hook
  counts entries while another dispatch is in progress (`nested`), logs the first one, and skips the
  chain at depth `FENYKL_EVENT_DEPTH_MAX` (2: counted in `bypassed`), so such a loop ends after one
  extra level instead of overflowing the key task's stack. `nested` also counts the preemption
  above, which the hook cannot tell apart without a task id; `bypassed` must stay 0.

Counters (`struct fenykl_event_stats`): `events` (CPU0 entries), `consumed`, `injected`, `nested`,
`bypassed`, `cpu1` (must stay 0). The counters are plain increments (a preempted increment can be
lost); the depth itself is updated under the interrupt mask.

Design notes: there is no observer flag and no `consumed` field in `struct fenykl_event` (an
observer registers at prio 0). `add` is allowed from any CPU0 context, a handler included: the
dispatch cannot tell a handler from a preempted posting task, and masking the insert makes it safe
everywhere. For the same reason `nested` can be non-zero; a depth guard bounds the nesting.

## 5. Tests

`tests/sim/event/test_event.c` links the real library with the `appevent_post` mock at its real
address with its real entry bytes (`tests/sim/hooks/appevent.S`, 3 * event + param + flag) and a
caller that checks the callee-saved registers and SP. Through `fenykl_event_install` and the patched
entry it checks: prio order with equal prio in registration order and a disabled handler skipped;
every handler's edit reaching the mock; CONSUME returning 0 and skipping later handlers, with the
counters; inject from a handler reaching the mock with its own arguments and no handler seeing it;
a handler calling `fw_appevent_post` (nested 2, bypassed 1, the recursion ends); `add` refusing a
missing `fn` and a second registration and ordering a late handler; `enabled = false`; remove
restoring the stock bytes (idempotent) and `fenykl_event_dispatch` without the patch; a CPU1 entry.
`tests/sim/hooks/test_hooks.c` keeps the plain hook mechanism test on the same mock under the
sim-only name `simevent_post`, since the SDK now binds `appevent_post`.

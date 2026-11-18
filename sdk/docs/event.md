# Event layer (`sdk/lib/event.c`)

Status: simulator-tested under the key layer (`tests/sim/key_em10ii/`); its user is the key layer
(`docs/key.md`), whose user, main's Fn override, has not run on the camera yet. The `appevent_post`
entry itself was hooked on the camera by a hook probe (2026-10-19/20: the key codes of
`docs/key.md` 5). Linked into every
plugin, like all of `sdk/lib/`; a plugin that does not call `fenykl_event_*` gets no byte of it
(`--gc-sections`).

## 1. What it does

Every APPEVENT the firmware posts goes through `appevent_post(event, param, flag)` (52 calls, plus 114
through the two-argument entry 0x6004241B): keys from UI_IN (`0x0200cccc` press, `0x0201cccc` release,
`0x0202cccc` values such as the mode dial) and system events from other tasks, two of them on CPU1
(section 3). The SDK binds that hook
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
- **CPU1.** Two Wi-Fi tasks post from CPU1 (static, `sdk_cpu1_unproven`). A CPU1 entry is counted
  (`cpu1`), the chain skipped and the stock function called. The caches are not coherent, so the
  CPU1 count lives in its own `FENYKL_SHARED` line, written uncached; nothing else is
  touched. Handlers therefore run on CPU0 only.

## 4. Handler rules and counters

The hook runs on the posting task's stack: UI_IN (priority 44, stack 0x800; key.md 4) for
most keys, other firmware tasks for system events.

- No blocking firmware calls (file RPC, task create or sleep, queue waits other than inject), no
  `fenykl_hud_text`/`draw`, no `fenykl_cfg_set`, no `fenykl_event_install`/`remove`. Allowed:
  reading memory, `fw_ui_key_get_state`, `fenykl_log_*` (logging.md), `fenykl_event_inject`,
  `fenykl_event_add`, setting words the plugin task polls (e.g. a `req++`).
- Small frames: a handler stays under ~128 bytes of stack and does not recurse.
- A handler runs again when another task posts while a call on a lower-priority task is preempted
  (same core, different stacks). Key-class events come from three tasks (UI_IN, PWR_STANDBY,
  PER_EYE_SENSOR: key.md 4), so handler state is not per-task-safe even for keys; state shared with the plugin task is written with single word
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

`tests/sim/key_em10ii/test_key.c` runs the layer under the key layer through the patched
`appevent_post` entry (`docs/key.md` 6). `tests/sim/hooks_em10ii/test_hooks.c` keeps the plain hook
mechanism test on the same entry span under the sim-only name `simevent_post`, since the SDK binds
`appevent_post`.

/*
 * APPEVENT interception (sdk/docs/event.md). The SDK's appevent_post hook runs every posted event
 * through the plugin's handlers in prio order, then posts what is left through the stock entry.
 * Everything hangs off fenykl_event_install's reference to the hook descriptor, so a plugin that
 * never installs links none of it.
 */
#include <event.h>


FENYKL_LOG_MODULE(fenykl_event_log, "event", FENYKL_LOG_INFO);

static struct {
	struct fenykl_event_handler *head;
	volatile unsigned int depth;	/* dispatches in progress, all CPU0 tasks */
	bool nested_logged;
	volatile struct fenykl_event_stats st;
} evl;

/* No cache coherency: a CPU1 entry writes only this line, uncached, never evl's. */
static FENYKL_SHARED uint32_t evl_cpu1;

static uint32_t irq_save(void)
{
	uint32_t psw = fw_psw_get();

	fw_irq_disable_all();
	return psw;
}

static int run_chain(struct fenykl_event *ev)
{
	for (struct fenykl_event_handler *h = evl.head; h; h = h->private.next) {
		if (!h->enabled)
			continue;
		h->private.seen++;
		if (h->fn(ev, h->arg) == FENYKL_EVENT_PASS)
			continue;
		h->private.consumed++;
		evl.st.consumed++;
		return 0;
	}
	return fw_orig_appevent_post(ev->event, ev->param, ev->flag);
}

int fenykl_event_dispatch(uint32_t event, uint32_t param, uint32_t flag)
{
	struct fenykl_event ev = { event, param, flag };
	unsigned int depth;
	uint32_t psw;
	int r;

	if (FENYKL_CORE_ID() != 0) {
		(*FENYKL_SHARED_PTR(evl_cpu1))++;
		return fw_orig_appevent_post(event, param, flag);
	}
	evl.st.events++;
	psw = irq_save();
	depth = evl.depth++;
	fw_irq_mask_restore(psw);
	if (depth) {
		evl.st.nested++;
		if (!evl.nested_logged) {
			evl.nested_logged = true;
			fenykl_log_warn(&fenykl_event_log, "nested entry, depth %u, event %08x", depth,
					event);
		}
	}
	if (depth >= FENYKL_EVENT_DEPTH_MAX) {
		evl.st.bypassed++;
		r = fw_orig_appevent_post(event, param, flag);
	} else {
		r = run_chain(&ev);
	}
	psw = irq_save();
	evl.depth--;
	fw_irq_mask_restore(psw);
	return r;
}

static int event_hook(unsigned int event, unsigned int param, unsigned int flag)
{
	return fenykl_event_dispatch(event, param, flag);
}
FENYKL_HOOK(appevent_post, event_hook);

int fenykl_event_add(struct fenykl_event_handler *h)
{
	struct fenykl_event_handler **pp;
	uint32_t psw;

	if (!h->fn) {
		fenykl_log_err(&fenykl_event_log, "add %s: no fn", h->name);
		return FENYKL_EVENT_EARG;
	}
	psw = irq_save();
	for (pp = &evl.head; *pp && *pp != h && (*pp)->prio <= h->prio; pp = &(*pp)->private.next)
		;
	if (*pp == h) {
		fw_irq_mask_restore(psw);
		fenykl_log_err(&fenykl_event_log, "add %s: already registered", h->name);
		return FENYKL_EVENT_EBUSY;
	}
	h->private.next = *pp;
	h->private.seen = 0;
	h->private.consumed = 0;
	__asm__ volatile("" ::: "memory");	/* a preempting hook sees the node complete */
	*pp = h;
	fw_irq_mask_restore(psw);
	return 0;
}

int fenykl_event_install(void)
{
	int r = fenykl_hook_install(&fenykl_hook_appevent_post);

	fenykl_log_info(&fenykl_event_log, "install %d", r);
	return r ? FENYKL_EVENT_EHOOK : 0;
}

int fenykl_event_remove(void)
{
	int r = fenykl_hook_remove(&fenykl_hook_appevent_post);

	fenykl_log_info(&fenykl_event_log, "remove %d", r);
	return r ? FENYKL_EVENT_EHOOK : 0;
}

int fenykl_event_inject(uint32_t event, uint32_t param, uint32_t flag)
{
	evl.st.injected++;
	return fw_orig_appevent_post(event, param, flag);
}

void fenykl_event_get_stats(struct fenykl_event_stats *st)
{
	*st = evl.st;
	st->cpu1 = *FENYKL_SHARED_PTR(evl_cpu1);
}

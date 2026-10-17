/*
 * The contrast client's attach/detach state machine (afc_int.h, docs/afc.md 3).
 */
#include "afc_int.h"

static void afc_enter(struct fenykl_afc_sm *sm, enum fenykl_afc_state state, uint32_t now)
{
	sm->state = state;
	sm->since = now;
}

static void afc_detach(struct fenykl_afc_sm *sm, const struct fenykl_afc_in *in, uint32_t why,
		       bool by_cb)
{
	sm->why = why;
	sm->by_cb = by_cb;
	sm->stop_frames = in->frames;
	sm->flowing = false;
	afc_enter(sm, FENYKL_AFC_DETACHING, in->now);
}

static enum fenykl_afc_action afc_stop(struct fenykl_afc_sm *sm, const struct fenykl_afc_in *in,
				       uint32_t why)
{
	afc_detach(sm, in, why, false);
	return FENYKL_AFC_STOP;
}

/*
 * Waiting for the done callback. The driver detaches at the next frame, so the timeout runs only
 * once a frame came after the stop: with live view off the stop stays pending until frames resume.
 */
static void afc_detaching(struct fenykl_afc_sm *sm, const struct fenykl_afc_in *in, bool done)
{
	uint32_t now = in->now;

	if (done) {
		sm->idle_at = now;
		afc_enter(sm, sm->err == FENYKL_AFC_E_NONE ? FENYKL_AFC_IDLE : FENYKL_AFC_OFF, now);
		return;
	}
	if (!sm->flowing) {
		if (in->frames == sm->stop_frames)
			return;
		sm->flowing = true;
		sm->flow_at = now;
	}
	if (now - sm->flow_at < FENYKL_AFC_DONE_MS)
		return;
	if (sm->err == FENYKL_AFC_E_NONE)
		sm->err = FENYKL_AFC_E_NODONE;
	afc_enter(sm, FENYKL_AFC_OFF, now);
}

enum fenykl_afc_action fenykl_afc_sm_fail(struct fenykl_afc_sm *sm, const struct fenykl_afc_in *in,
					  enum fenykl_afc_err err)
{
	if (sm->err == FENYKL_AFC_E_NONE)
		sm->err = err;
	switch (sm->state) {
	case FENYKL_AFC_ATTACHING:
	case FENYKL_AFC_ATTACHED:
		return afc_stop(sm, in, 0);
	case FENYKL_AFC_IDLE:
		afc_enter(sm, FENYKL_AFC_OFF, in->now);
		return FENYKL_AFC_NONE;
	default:
		return FENYKL_AFC_NONE;	/* a detaching client ends in OFF */
	}
}

void fenykl_afc_sm_init(struct fenykl_afc_sm *sm, const struct fenykl_afc_in *in)
{
	*sm = (struct fenykl_afc_sm){ .state = FENYKL_AFC_IDLE, .since = in->now,
				      .frames = in->frames, .frame_at = in->now, .done = in->done,
				      .cb_stops = in->cb_stops, .idle_at = in->now };
}

/* Gates clear for SETTLE_MS, past the boot delay and the cooldown. */
static bool afc_may_attach(const struct fenykl_afc_sm *sm, uint32_t now)
{
	return now >= FENYKL_AFC_BOOT_MS && sm->clear && now - sm->clear_since >= FENYKL_AFC_SETTLE_MS &&
	       now - sm->idle_at >= FENYKL_AFC_COOLDOWN_MS;
}

enum fenykl_afc_action fenykl_afc_sm_step(struct fenykl_afc_sm *sm, const struct fenykl_afc_in *in)
{
	uint32_t now = in->now;
	bool fresh = in->frames != sm->frames;
	bool done = in->done != sm->done;
	bool cb_stop = in->cb_stops != sm->cb_stops;

	if (in->gates)
		sm->clear = false;
	else if (!sm->clear) {
		sm->clear = true;
		sm->clear_since = now;
	}
	if (fresh) {
		sm->frames = in->frames;
		sm->frame_at = now;
	}
	sm->done = in->done;
	sm->cb_stops = in->cb_stops;

	switch (sm->state) {
	case FENYKL_AFC_IDLE:
		if (!afc_may_attach(sm, now))
			return FENYKL_AFC_NONE;
		afc_enter(sm, FENYKL_AFC_ATTACHING, now);
		return FENYKL_AFC_ATTACH;
	case FENYKL_AFC_ATTACHING:
	case FENYKL_AFC_ATTACHED:
		/* The frame callback stopped it; its done callback may already be here. */
		if (cb_stop) {
			afc_detach(sm, in, in->gates, true);
			afc_detaching(sm, in, done);
			return FENYKL_AFC_NONE;
		}
		if (done) {
			if (sm->err == FENYKL_AFC_E_NONE)
				sm->err = FENYKL_AFC_E_EARLY;
			afc_enter(sm, FENYKL_AFC_OFF, now);
			return FENYKL_AFC_NONE;
		}
		if (in->gates)
			return afc_stop(sm, in, in->gates);
		if (sm->state == FENYKL_AFC_ATTACHING) {
			if (fresh)
				afc_enter(sm, FENYKL_AFC_ATTACHED, now);
			else if (now - sm->since >= FENYKL_AFC_FRAME_MS)
				return fenykl_afc_sm_fail(sm, in, FENYKL_AFC_E_NOFRAME);
			return FENYKL_AFC_NONE;
		}
		/* Frames stopped (live view went away before the GUI state did): a stop, pending. */
		if (now - sm->frame_at >= FENYKL_AFC_FRAME_MS)
			return afc_stop(sm, in, FENYKL_AFC_W_STALL);
		return FENYKL_AFC_NONE;
	case FENYKL_AFC_DETACHING:
		afc_detaching(sm, in, done);
		return FENYKL_AFC_NONE;
	default:
		return FENYKL_AFC_NONE;
	}
}

/*
 * The contrast client's attach/detach state machine (afc_sm.c): pure logic, simulator-tested
 * (tests/sim/afc). The caller polls with the failing gates and the callback counters and performs
 * the returned action: ATTACH = cam_request, STOP = contrast_setting.number = 0. The frame callback
 * may write number = 0 itself (cb_stops): that is a stop too, and its done callback a normal detach.
 */
#ifndef _FENYKL_AFC_INT_H
#define _FENYKL_AFC_INT_H

#include <afc.h>

void fenykl_afc_sm_init(struct fenykl_afc_sm *sm, const struct fenykl_afc_in *in);
enum fenykl_afc_action fenykl_afc_sm_step(struct fenykl_afc_sm *sm, const struct fenykl_afc_in *in);
/* An error found by the caller (E_REQ, E_END): stop now if attached; returns the action. */
enum fenykl_afc_action fenykl_afc_sm_fail(struct fenykl_afc_sm *sm, const struct fenykl_afc_in *in,
					  enum fenykl_afc_err err);

#endif

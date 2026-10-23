/* E-M10 II part of body.h (included from there): the GUI and batt groups on the v1.4 firmware's
   data. The other groups are not implemented on this body (target.toml capabilities). */
#ifndef FENYKL_BODY_TARGET_H
#define FENYKL_BODY_TARGET_H

#define FENYKL_GUI_SS_LV 0x01u		/* super state of the live-view screens */
#define FENYKL_GUI_ST_LV 0x08u		/* its state while live view is shown */
#define FENYKL_GUI_SS_PLAY 0x06u	/* single playback, from static analysis only */

static inline uint16_t fenykl_gui_state(void)
{
	return (uint16_t)(fenykl_rd8(FW_GUI_SUPER) << 8 | fenykl_rd8(FW_GUI_STATE));
}

static inline bool fenykl_gui_live_view(void)
{
	return fenykl_gui_state() == (FENYKL_GUI_SS_LV << 8 | FENYKL_GUI_ST_LV);
}

static inline bool fenykl_gui_playback(void)
{
	return FENYKL_GUI_SUPER(fenykl_gui_state()) == FENYKL_GUI_SS_PLAY;
}

#if FENYKL_HAS_BATT
#define FBODY_SRC_BATT 0x200u

/* The value the 1 Hz source check stored: no A/D request, never busy, does not block. */
static inline int fenykl_batt_read(uint32_t *v)
{
	if (!fenykl_rd8(FW_POWER_SRC_CHECK))
		return FENYKL_BATT_ESTANDBY;
	*v = fenykl_rd16(FW_BATT_VBAT);
	return 0;
}

static inline uint32_t fenykl_power_src(void)
{
	return fenykl_rd16(FW_POWER_SRC);
}

static inline bool fenykl_power_src_batt(uint32_t src)
{
	return src == FBODY_SRC_BATT;
}
#endif

#endif

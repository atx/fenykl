/* E-M10 II GUI probe: polls the GUI state machine, the display request and the OSD (GPI) state every
   GP_POLL_TICKS and logs the raw values whenever one changes; logs the VRAM handles and the 16 bytes
   behind each when they change; dumps GUI data slot 0x38 (the Custom list model) while the menu is
   up and its content changed, at most GP_DUMPS times. Reads only: no firmware call but the log
   library's, no firmware RAM write. Stops after GP_RUN_TICKS or GP_EVENTS changes. */
#include <fenykl.h>
#include <log.h>

#define GP_POLL_TICKS 100
#define GP_RUN_TICKS 180000	/* ~3 min */
#define GP_EVENTS 1000		/* logged changes (state or VRAM) before the probe stops */
#define GP_DUMPS 3
#define GP_SUPER_MENU 0x11u	/* SsMenu: the main menu */
#define GP_HANDLES 7		/* 3 layers x 2 buffers, then the work buffer */
#define GP_SLOT38_SIZE 0xC6Eu
#define GP_SLOT38_WORDS ((GP_SLOT38_SIZE + 3) / 4)
#define GP_U8(a) (*(volatile uint8_t *)(a))
#define GP_U16(a) (*(volatile uint16_t *)(a))
#define GP_U32(a) (*(volatile uint32_t *)(a))

struct gp_gui {
	uint32_t sm;		/* super << 8 | state */
	uint32_t disp_req;
	uint32_t out_mode;
	uint32_t draw_buf, hide, one;	/* one: mode << 16 | (u8)layer << 8 | buf */
	uint32_t ext_out, ext_main;
};

struct gp_vram {
	uint32_t handle;
	uint32_t w[4];
};

static struct {
	struct gp_gui gui;
	struct gp_vram vram[GP_HANDLES];
	uint32_t slot[2][GP_SLOT38_WORDS];
	uint32_t slot_sum;
	unsigned int events, dumps;
} gp;

static FENYKL_LOG_MODULE(gp_mod, "guiprb", FENYKL_LOG_INFO);

static void gp_read_gui(struct gp_gui *g)
{
	g->sm = GP_U8(FW_GUI_SUPER) << 8 | GP_U8(FW_GUI_STATE);
	g->disp_req = GP_U32(FW_GUI_DISP_REQ);
	g->out_mode = GP_U16(FW_GPI_OUT_MODE);
	g->draw_buf = GP_U8(FW_GPI_DRAW_BUF);
	g->hide = GP_U16(FW_GPI_HIDE_MASK);
	g->one = GP_U8(FW_GPI_ONE_MODE) << 16 | (GP_U16(FW_GPI_ONE_LAYER) & 0xFFu) << 8 |
		 GP_U8(FW_GPI_ONE_BUF);
	g->ext_out = GP_U32(FW_GPI_EXT_OUT);
	g->ext_main = GP_U32(FW_GPI_EXT_MAIN);
}

/* A handle is dereferenced only if it points into firmware RAM above the image (bss). */
static bool gp_handle_ok(uint32_t h)
{
	return h >= FW_IMAGE_END && h < FW_DRAM_END - 16 && !(h & 3);
}

static void gp_read_vram(unsigned int i, struct gp_vram *v)
{
	v->handle = GP_U32(FW_GPI_LAYER_VRAM + 4 * i);
	for (unsigned int k = 0; k < 4; k++)
		v->w[k] = gp_handle_ok(v->handle) ? GP_U32(v->handle + 4 * k) : 0;
}

static void gp_poll_gui(void)
{
	struct gp_gui g;

	gp_read_gui(&g);
	if (!memcmp(&g, &gp.gui, sizeof(g)))
		return;
	gp.gui = g;
	gp.events++;
	fenykl_log_info(&gp_mod, "gui %04x disp %x out %x ext %x", g.sm, g.disp_req, g.out_mode,
			g.ext_out);
	fenykl_log_info(&gp_mod, "osd draw %x hide %x one %06x ext_main %x", g.draw_buf, g.hide,
			g.one, g.ext_main);
}

static void gp_poll_vram(void)
{
	for (unsigned int i = 0; i < GP_HANDLES; i++) {
		struct gp_vram v;

		gp_read_vram(i, &v);
		if (!memcmp(&v, &gp.vram[i], sizeof(v)))
			continue;
		gp.vram[i] = v;
		gp.events++;
		fenykl_log_info(&gp_mod, "vram %u %08x: %08x %08x", i, v.handle, v.w[0], v.w[1]);
		fenykl_log_info(&gp_mod, "vram %u %08x: %08x %08x", i, v.handle, v.w[2], v.w[3]);
	}
}

static void gp_copy_slot(uint32_t *dst)
{
	const volatile uint32_t *src = (const volatile uint32_t *)FW_GUI_DATA_SLOT38;

	for (unsigned int i = 0; i < GP_SLOT38_WORDS; i++)
		dst[i] = src[i];
}

/* Dumps slot 0x38 when it changed while the menu is up; two equal copies rule out a torn read. */
static void gp_poll_slot(void)
{
	uint32_t sum = 0;

	if (gp.dumps >= GP_DUMPS || GP_U8(FW_GUI_SUPER) != GP_SUPER_MENU)
		return;
	gp_copy_slot(gp.slot[0]);
	for (unsigned int i = 0; i < GP_SLOT38_WORDS; i++)
		sum = (sum << 1 | sum >> 31) ^ gp.slot[0][i];
	if (!sum || sum == gp.slot_sum)
		return;
	gp_copy_slot(gp.slot[1]);
	if (memcmp(gp.slot[0], gp.slot[1], sizeof(gp.slot[0])))
		return;
	gp.slot_sum = sum;
	gp.dumps++;
	fenykl_log_info(&gp_mod, "slot 38 dump %u, gui %04x, sum %08x", gp.dumps, gp.gui.sm, sum);
	for (unsigned int i = 0; i < GP_SLOT38_WORDS; i += 3)
		fenykl_log_info(&gp_mod, "38+%03x: %08x %08x %08x", 4 * i, gp.slot[0][i],
				i + 1 < GP_SLOT38_WORDS ? gp.slot[0][i + 1] : 0,
				i + 2 < GP_SLOT38_WORDS ? gp.slot[0][i + 2] : 0);
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int init = fenykl_log_init(NULL);
	int task = fenykl_log_task_start();
	uint32_t t;

	fenykl_log_info(&gp_mod, "guiprobe: load %08x size %u, log init %d task %d", load_base,
			image_size, init, task);
	gp.gui.sm = ~0u; /* the first poll logs everything */
	for (unsigned int i = 0; i < GP_HANDLES; i++)
		gp.vram[i].handle = ~0u;
	for (t = 0; t < GP_RUN_TICKS && gp.events < GP_EVENTS; t += GP_POLL_TICKS) {
		gp_poll_gui();
		gp_poll_vram();
		gp_poll_slot();
		fw_OlyTaskSleep(GP_POLL_TICKS);
	}
	fenykl_log_info(&gp_mod, "guiprobe end: %u s, %u changes, %u dumps", t / 1000u, gp.events,
			gp.dumps);
}

/*
 * The E-M10 II screenshot (sdk/lib/shot.c with the E-M10 II body) in the GDB simulator, against
 * mocks of the image pool, the file server and the kernel (fw.S) and a fake display in memory (LCD,
 * live view on a live-view buffer, three OSD layers). The PRIM pool, its reserve, the pool lookup
 * table with the pools over PRIM block 0 and prop 0xE951 are fake words at their firmware addresses.
 * plugin_main sets sim_exit_code to the failure bit mask (0 = pass):
 *   1   the body's lend: block 0 not lent from an idle PRIM, a block other than 0 kept or not given
 *       back, an alloc error or an oversized request not refused
 *   2   a start not refused (and the empty PPM not removed, nothing allocated) with a PRIM frame in
 *       flight, prop 0xE951 set or unregistered, a pool over block 0 busy, movie or playback
 *   4   a whole shot with the raw pieces, the imgwork reserve in use, a still frame and the shutter
 *       button during the write: not every byte written, files left open, block 0 not given back
 *       once after the last file
 *   8   a pool over block 0 or prop 0xE951 going busy during the PPM: the shot not aborted, block 0
 *       not given back before the close, the partial PPM not removed; the card gate closing: the
 *       partial PPM removed with the gate closed
 *   16  an abort during the raw pieces: the finished PPM removed, or block 0 not given back
 */
#include <shot.h>

#define T_PRIM_USED (*(volatile uint32_t *)(FW_POOL_PRIM + 0x04))
#define T_PRIM_FREE (*(volatile uint16_t *)(FW_POOL_PRIM + 0x14))
#define T_PRIM_TOTAL (*(volatile uint16_t *)(FW_POOL_PRIM + 0x18))
#define T_PRIM_RESERVE (*(volatile uint32_t *)(FW_POOL_PRIM + 0x40))
#define T_PRIM_BLOCK(n) (0xA33E8C00u + (n) * 0x1895000u)
#define T_PROP_BEAUTY (FW_PROP_TABLE + 8u * (0xE951u >> 4) + 4u)
#define T_POOL_DESC(i) (0x61900C00u + 0x40u * (i))	/* the fake pools over block 0 */
#define T_POOL_2700 7u		/* its index in the body's list */
#define T_LV 0xBE871BC0u	/* the shown live-view buffer */
#define T_FP 0x61900800u
#define T_TF 0x61900400u	/* take_focus, zero: not movie */
#define T_VRAM 0x61900000u	/* gpi_layer_vram's descriptors */
#define T_OSD 0xBFCEC800u	/* the six OSD buffers, 0x83400 bytes apart */
#define T_STEPS 1000u
#define T_STRIPS 30u		/* 480 rows in 16-row strips */
#define T_PPM (15u + 720u * 480u * 3u)
#define T_RAW (720u * 2u * 480u + 3u * 720u * 480u + 0x500u)

uint32_t sim_exit_code = 99;
uint32_t mreg[4];

struct t_state {
	bool unmounted;		/* the card gate closed */
	uint32_t alloc_ret;
	unsigned int allocs, frees;
	uint32_t freed_id;
	unsigned int opens, closes, removes;
	uint32_t written;
	char seq[16];		/* F free, C close, R remove */
	unsigned int nseq;
};

static struct t_state t;
static volatile int8_t t_beauty;

static void t_seq(char c)
{
	if (t.nseq < sizeof(t.seq))
		t.seq[t.nseq++] = c;
}

uint32_t c_psw_get(void) { return 0; }
uint32_t c_irq_disable_all(void) { return 0; }
uint32_t c_irq_mask_restore(void) { return 0; }
uint32_t c_irq_lock(void) { return 0; }
uint32_t c_irq_unlock(void) { return 0; }
uint32_t c_dcache_flush_all(void) { return 0; }
uint32_t c_OlyTaskSleep(void) { return 0; }
uint32_t c_ui_key_get_state(void) { return 1; }		/* no USB cable */
uint32_t c_fileif_get_media_status(void) { return t.unmounted ? 0 : 0x100; }

/* Paths and text come out empty: the REG piece is then skipped. */
uint32_t c_sprintf(void)
{
	*(char *)mreg[0] = 0;
	return 0;
}

/* "r" (does the file exist?): no; "w": the one fake file. */
uint32_t c_rpc_fopen(void)
{
	if (*(const char *)mreg[1] != 'w')
		return 0;
	t.opens++;
	return T_FP;
}

uint32_t c_rpc_fclose(void)
{
	t.closes++;
	t_seq('C');
	return 0;
}

uint32_t c_rpc_fwrite(void)
{
	t.written += mreg[2] * mreg[3];
	return mreg[3];
}

uint32_t c_rpc_fremove(void)
{
	t.removes++;
	t_seq('R');
	return 0;
}

/* PRIM as the firmware's: the lowest free block, id 1 + index. */
uint32_t c_imgbuff_alloc(void)
{
	unsigned int i = 0;

	t.allocs++;
	if (t.alloc_ret)
		return t.alloc_ret;
	if (mreg[2] != 0)
		return 0x9B02;
	while (i < T_PRIM_TOTAL && (T_PRIM_USED >> i & 1u))
		i++;
	if (i == T_PRIM_TOTAL)
		return 0x9B01;
	T_PRIM_USED |= 1u << i;
	T_PRIM_FREE = (uint16_t)(T_PRIM_FREE - 1);
	*(uint16_t *)mreg[0] = (uint16_t)(1 + i);
	return 0;
}

uint32_t c_imgbuff_addr(void)
{
	return T_PRIM_BLOCK(mreg[2] - 1);
}

uint32_t c_imgbuff_free(void)
{
	t.frees++;
	t.freed_id = mreg[2];
	T_PRIM_USED &= ~(1u << (mreg[2] - 1));
	T_PRIM_FREE = (uint16_t)(T_PRIM_FREE + 1);
	t_seq('F');
	return 0;
}

static void t_pool_busy(unsigned int i, bool busy)
{
	*(volatile uint16_t *)(T_POOL_DESC(i) + 0x14) = busy ? 0 : 1;
}

static void t_reset(void)
{
	t = (struct t_state){ 0 };
	T_PRIM_USED = 0;
	T_PRIM_FREE = 12;
	T_PRIM_TOTAL = 12;
	T_PRIM_RESERVE = 0;
	for (unsigned int i = 0; i < FBODY_BLOCK0_POOLS; i++) {
		fenykl_wr32(FW_POOL_TABLE + 8u * (fbody_block0_pools[i] >> 8), T_POOL_DESC(i));
		*(volatile uint16_t *)(T_POOL_DESC(i) + 0x18) = 1;
		t_pool_busy(i, false);
	}
	t_beauty = 0;
	fenykl_wr32(T_PROP_BEAUTY, (uint32_t)&t_beauty);
	fenykl_wr8(FW_GUI_SUPER, 1);
	fenykl_wr8(FW_TRG_HALF, 0);
	fenykl_wr8(T_TF + 8, 0);
}

static bool t_seq_is(const char *s)
{
	for (unsigned int i = 0; i < t.nseq; i++)
		if (s[i] != t.seq[i])
			return false;
	return !s[t.nseq];
}

/* The LCD showing live view and the front buffer b[1] of each OSD layer. */
static void t_display(void)
{
	static const uint8_t slot[3] = { 1, 0, 2 };

	fenykl_wr8(FW_DISP_STATE, 2);
	fenykl_wr32(FW_DISP_DEVICE, 1);
	fenykl_wr32(FW_TAKE_FOCUS_PTR, T_TF);
	fenykl_wr32(FW_DISP_LAYER_EN, 0x27);
	fenykl_wr32(FW_DISP_IMG_LATCHED, T_LV & 0x7FFFFFFFu);
	fenykl_wr32(FW_DISP_IMG_JUMP, 0x3C);
	fenykl_wr32(FW_DISP_IMG1_X, 720);
	fenykl_wr32(FW_DISP_IMG1_H, 480);
	for (unsigned int i = 0; i < 3; i++)
		fenykl_wr32(FW_DISP_OSD_ALPHA + 4 * i, 0x100);
	fenykl_wr8(FW_GPI_LAYER_BPP, 4);
	fenykl_wr8(FW_GPI_LAYER_BPP + 1, 8);
	fenykl_wr8(FW_GPI_LAYER_BPP + 2, 4);
	for (unsigned int i = 0; i < 6; i++) {
		fenykl_wr32(FW_GPI_LAYER_VRAM + 4 * i, T_VRAM + 16 * i);
		fenykl_wr32(T_VRAM + 16 * i, T_OSD + 0x83400u * i);
	}
	for (unsigned int l = 0; l < 3; l++)
		fenykl_wr32(FW_OSD_SHADOW + 4 * slot[l], T_OSD + 0x83400u * (2 * l + 1));
}

static uint32_t t_lend(void)
{
	uint32_t h = 0;

	t_reset();
	if (fenykl_shot_buf_alloc(0x200000, &h) != T_PRIM_BLOCK(0) || h != 1 || T_PRIM_USED != 1)
		return 1;
	if (fenykl_shot_buf_free(h) || T_PRIM_USED || T_PRIM_FREE != 12)
		return 1;
	t_reset();
	T_PRIM_USED = 1;	/* block 0 taken: the alloc returns block 1 */
	T_PRIM_FREE = 11;
	if (fenykl_shot_buf_alloc(0x200000, &h) || t.frees != 1 || t.freed_id != 2 ||
	    T_PRIM_USED != 1 || T_PRIM_FREE != 11)
		return 1;
	t_reset();
	t.alloc_ret = 0x9B01;
	if (fenykl_shot_buf_alloc(0x200000, &h) || t.frees)
		return 1;
	t_reset();
	return fenykl_shot_buf_alloc(0x1895001, &h) || t.allocs ? 1 : 0;
}

/* A start refused before the lend: nothing allocated, the empty PPM closed and removed. */
static bool t_refused(void)
{
	bool ok = !fenykl_shot_start() && !t.allocs && t_seq_is("CR");

	t_reset();
	return ok;
}

static uint32_t t_start_rules(void)
{
	t_reset();
	T_PRIM_USED = 0x2;	/* a still in flight */
	if (!t_refused())
		return 2;
	t_beauty = 1;
	if (!t_refused())
		return 2;
	fenykl_wr32(T_PROP_BEAUTY, 0);
	if (!t_refused())
		return 2;
	t_pool_busy(T_POOL_2700, true);
	if (!t_refused())
		return 2;
	t_pool_busy(FBODY_BLOCK0_POOLS - 1, true);
	if (!t_refused())
		return 2;
	fenykl_wr8(T_TF + 8, 1);
	if (!t_refused())
		return 2;
	fenykl_wr8(FW_GUI_SUPER, 6);
	return t_refused() ? 0 : 2;
}

static unsigned int t_steps(unsigned int max)
{
	unsigned int n = 0;

	while (n < max && fenykl_shot_step())
		n++;
	return n;
}

/* The imgwork reserve marked in use does not refuse; a still and the shutter button while held
   do not abort. */
static uint32_t t_whole(void)
{
	t_reset();
	T_PRIM_USED = 0xE00;
	T_PRIM_RESERVE = 0xE00;
	T_PRIM_FREE = 9;
	fenykl_shot_raw(1);
	if (!fenykl_shot_start() || t_steps(5) != 5)
		return 4;
	T_PRIM_USED |= 0x2;
	fenykl_wr8(FW_TRG_HALF, 1);
	if (t_steps(T_STEPS) >= T_STEPS)
		return 4;
	if (t.written != T_PPM + T_RAW || t.opens != 6 || t.closes != 6 || t.removes)
		return 4;
	return t.frees == 1 && t.freed_id == 1 && t.seq[t.nseq - 1] == 'F' &&
	       T_PRIM_USED == 0xE02 ? 0 : 4;
}

/* Aborted after a few strips: block 0 back first, then the partial PPM closed and removed. */
static bool t_aborted_ppm(void)
{
	return t.frees == 1 && t_seq_is("FCR") && t.written > 15u && t.written < T_PPM &&
	       !T_PRIM_USED;
}

static uint32_t t_abort_ppm(void)
{
	t_reset();
	if (!fenykl_shot_start() || t_steps(3) != 3)
		return 8;
	t_pool_busy(0, true);
	if (fenykl_shot_step() || !t_aborted_ppm())
		return 8;
	t_reset();
	if (!fenykl_shot_start() || t_steps(3) != 3)
		return 8;
	t_beauty = 1;
	if (fenykl_shot_step() || !t_aborted_ppm())
		return 8;
	t_reset();
	if (!fenykl_shot_start() || t_steps(3) != 3)
		return 8;
	t.unmounted = true;
	return !fenykl_shot_step() && t.frees == 1 && t_seq_is("FC") && !T_PRIM_USED ? 0 : 8;
}

/* Aborted in the raw pieces: the PPM is complete and stays. */
static uint32_t t_abort_raw(void)
{
	t_reset();
	fenykl_shot_raw(100);
	if (!fenykl_shot_start() || t_steps(T_STRIPS + 1) != T_STRIPS + 1)
		return 16;
	t_pool_busy(T_POOL_2700, true);
	if (fenykl_shot_step())
		return 16;
	return t.frees == 1 && t_seq_is("CFC") && !t.removes && t.written > T_PPM &&
	       !T_PRIM_USED ? 0 : 16;
}

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	(void)load_base;
	(void)image_size;
	t_display();
	/* in this order: the raw pieces go with the first shot and with the last */
	sim_exit_code = t_lend();
	sim_exit_code |= t_start_rules();
	sim_exit_code |= t_whole();
	sim_exit_code |= t_abort_ppm();
	sim_exit_code |= t_abort_raw();
}

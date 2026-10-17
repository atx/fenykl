/* SDK test payload, run in the GDB simulator with sim_start.S and the mocks of mocks.S (see
   test_sim.py). Checks the crt0 entry contract, .bss clearing, FENYKL_TASK and every generated
   firmware thunk against the shape mocks. test_sim.py includes this file from a generated api.c
   that defines api_calls(): one PRESERVED/EXPECT/RESULT check per firmware.toml function, numbered
   from API_FIRST. Exit status 0 = pass, otherwise the number of the first failed check. */
#include <fenykl.h>

#define API_FIRST 10

volatile uint32_t mock_args[12];
uint32_t sim_exit_code = 99; /* stays 99 if test_task never runs */

static uint32_t bss_probe[3];
static int failed;
static int main_ran;

static void fail(int check)
{
	if (!failed)
		failed = check;
}

static void expect(int check, int n, const uint32_t *want)
{
	for (int i = 0; i < n; i++)
		if (mock_args[i] != want[i])
			fail(check);
}

#define EXPECT(check, ...)                                                                  \
	expect(check, sizeof((const uint32_t[]){__VA_ARGS__}) / sizeof(uint32_t),          \
	       (const uint32_t[]){__VA_ARGS__})
/* The shape mock of the function with id `check` ran (it stores its id in mock_args[11]). */
#define CALLED(check)                                                                       \
	do {                                                                                \
		if (mock_args[11] != (check))                                               \
			fail(check);                                                        \
	} while (0)
/* The value the shape mock of the function with id `check` returns (mocks.S). */
#define RESULT(check, r)                                                                    \
	do {                                                                                \
		if ((r) != 0x7e000000u + (check))                                           \
			fail(check);                                                        \
	} while (0)

/* Runs `call` with sentinels in D2/D3/A2/A3 (the registers GCC expects preserved) and fails
   `check` if any of them changed. The sentinels differ per check, so a register reloaded from a
   stale stack slot is caught too. */
#define SENTINEL(check, reg) (0x5a000000u | (uint32_t)(check) << 16 | (reg))
#define PRESERVED(check, call)                                                              \
	do {                                                                                \
		register uint32_t d2 __asm__("d2") = SENTINEL(check, 0xd2);                  \
		register uint32_t d3 __asm__("d3") = SENTINEL(check, 0xd3);                  \
		register uint32_t a2 __asm__("a2") = SENTINEL(check, 0xa2);                  \
		register uint32_t a3 __asm__("a3") = SENTINEL(check, 0xa3);                  \
		__asm__ volatile("" : "+r"(d2), "+r"(d3), "+r"(a2), "+r"(a3));               \
		call;                                                                       \
		__asm__ volatile("" : "+r"(d2), "+r"(d3), "+r"(a2), "+r"(a3));               \
		if (d2 != SENTINEL(check, 0xd2) || d3 != SENTINEL(check, 0xd3) ||          \
		    a2 != SENTINEL(check, 0xa2) || a3 != SENTINEL(check, 0xa3))            \
			fail(check);                                                        \
	} while (0)

static void api_calls(void);

FENYKL_TASK(test_task);

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	int r = 0;

	if (bss_probe[0] || bss_probe[1] || bss_probe[2])
		fail(1);
	if (load_base != FENYKL_LOAD_BASE || image_size != 0x1234)
		fail(2);
	/* OlyTaskSleep has a hand mock (it ends the run), so its check is here. */
	PRESERVED(3, r = fw_OlyTaskSleep(77));
	EXPECT(3, 77);
	if (r != 78)
		fail(3);
	api_calls();
	main_ran = 1;
}

void test_task(void)
{
	if (!main_ran)
		fail(4);
	sim_exit_code = failed;
}

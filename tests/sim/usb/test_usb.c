/* Startup decision on a USB boot (lib/start.c, arch/epl3/usb.c), run by test_sim.py with
   sim_start.S and the mocks of ../mocks.S. */
#include <fenykl.h>

volatile uint32_t mock_args[12];
uint32_t sim_exit_code = 7; /* stays 7 if plugin_main never runs */

void plugin_main(uint32_t load_base, uint32_t image_size)
{
	sim_exit_code = 0;
}

# Example plugins

Build one with `uv run fenykl plugin build sdk/projects/<name>`. Kept are the product plugin, the
smallest example and the dev monitor.

| project | what it is |
|---|---|
| `main/` | the daily-use plugin (focus distance, depth of field, focus meter, battery percentage, Fn screenshot, R/B swap, pre-capture (settings tab row "Pre-capture", HUD tag `PRE` / `PRE!`), settings tab); reference user of the cfg, log, ui, HUD, fnfunc, shot and precap libraries; names no firmware symbol: camera state through `include/body.h`, features per body capability (`plugin.toml`), screen layout per camera in `main_layout.h`. Logging, the saved settings (cfg), the settings tab "L. Fenykl" in the Custom menu (option "Log level"), the battery percentage on the basic and histogram INFO screens (hidden in MENU, the Super Control Panel and playback) and under it the focus meter (fcs's fc grid read-only in S-AF, C-AF and S-AF+MF, the own contrast client in MF), R/B swap (no hook: three colour-matrix base properties repointed to row-swapped copies, settings tab row "R/B swap"); the focus distance and depth of field blocks in a second column right of the touch button, and the Button Function entry "Screenshot" (help line "(Fenykl) Saves screen to card") in the stock pickers of Fn1, Fn2, Fn3, REC and L-Fn (screenshot: block 0 of the PRIM still-image pool lent for the write, the raw snapshot pieces kept next to the PPM for the first two shots of a boot), built but not yet run on this camera |
| `hello/` | smallest plugin: writes `C:\FENYKL.LOG` and returns; the first plugin to try after flashing (through the log library) |
| `devmon1/` | E-M10 II dev monitor: the dev monitor library (`sdk/lib/devmon_*.c`, opt-in `[capabilities.devmon]`, `sdk/docs/devmon.md`) and the settings tab with its options; nothing else |

# Example and experiment plugins

Build one with `uv run fenykl plugin build sdk/projects/<name>`. Kept are the product plugin, the
minimal examples of SDK features that no other plugin uses, and the probes and prototypes that hold implementation details for features not yet in `main`.

| project | what it is |
|---|---|
| `main/` | the daily-use plugin (focus distance, depth of field, focus meter, battery percentage, Fn screenshot, R/B swap, settings page); reference user of the cfg, log, ui, HUD and key libraries; names no firmware symbol: camera state through `include/body.h`, features per body capability (`plugin.toml`), screen layout per camera in `main_layout.h` |
| `hello/` | smallest plugin: writes `C:\FENYKL.LOG` and returns; the first plugin to try after flashing (E-M10 II: through the log library) |
| `bootprobe/` | E-M10 II diagnostic probe: logs boot gate, loaded sections, card and USB key state, tick and timer rate into `C:\FENYKL.LOG` and `C:\FENYKL.TCK` (deletes and rewrites only those two files); the first plugin for that camera |
| `icprobe/` | E-M10 II I-cache probe: rewrites a tiny function in payload RAM and calls it without and with `fenykl_icache_invalidate`, logging results, CHCTR values and poll counts; patches no firmware code. Must pass before `hookprobe` |
| `hookprobe/` | E-M10 II pass-through hooks on `cw_updateDisplay` and `appevent_post` plus an input map: calls per core, call rate, `cw_updateDisplay` D0, every CPU0 event (code, param, flag, task, and the dial counters, touch record or eye state with it), and a 20 ms poll of the raw key bytes, mode dial, dial counters, eye and touch state and `ui_key_get_state` of every known code, logged on change; removes both hooks after 15 minutes |
| `menuprobe1/` | E-M10 II: why the Eye-Fi row of Custom menu tab K is grayed; condition 113 of its mode-check rule as a logging pass-through (data slot, restored after 5 min) |
| `menuprobe2/` | E-M10 II: the Eye-Fi row as an entry point; condition 113 allowed, the row's OK/RIGHT records get a logging callback that continues to the stock pop-up (data slots, restored after 5 min) |
| `guiprobe/` | E-M10 II: read-only poller of the GUI state machine, display request and OSD state, logged on change, plus dumps of GUI data slot 0x38 in the menu |
| `lvpoll/` | E-M10 II: read-only poller of live-view flips (MVR flip records, img1 shadow, buffer pool, display controller address/format registers behind the driver's own state gate), flips per second |
| `peakpoll/` | E-M10 II: read-only poller of the stock focus peaking (DSP request, MVR gate and frame record, stored peak request, the four peaking settings read as properties) |
| `afpoll/` | E-M10 II: read-only poller of autofocus (take_focus, contrast evaluation state and grid, FCS command log, contrast clients, lens record, centre statistics) |
| `facepoll/` | E-M10 II: read-only poller of face detection (FD rates, frame descriptor, live results, eyes, fcs face selection, AF packet and point) |
| `proxyget/` | cloned vtable on the settings proxy object; the only E-PL3 user of slots and vtable clones |
| `mfstats/` | the plugin's own contrast client in MF: AF statistics without AF, attach/detach safety (focus meter in MF) |
| `caflog/` | per-frame trace of continuous AF hunting (C-AF) |
| `bchklog/` | battery voltage and thermistor A/D once a second with the battery-check state (voltage readout calibration) |
| `trklog/` | does the C-AF+TR window follow the tracker (tracker injection) |
| `framepaths/` | where the live-view frames come from (focus peaking, magnified view, Pro Capture) |
| `cpu1frame/` | live-view frames read on CPU1 (focus peaking on CPU1); minimal CPU1 worker example |
| `nnbench/` | neural-network kernel costs on CPU1 (conv, ternary, depthwise/pointwise, packed and binary weights, pooling, input preparation, memory and cache), checked against C references, plus the multiply timings of the former `macbench` on both cores; `nnbench_report.py` turns the log into a cost table (detector design) |
| `nnbench2/` | the second nnbench run, built from nnbench's sources with its own plan: input preparation on the live face-detection frame, int8 kernels generated with 4 pixels per call, softmax and the hand-written kernels on the shapes that matter most; `nnbench_report.py` merges both logs |
| `birddetect/` | bird detector on CPU1 that steers the face frame; weights trained by the separate fenykl-nn project on the Open Images Dataset V7 bird class (annotations CC BY 4.0, Google LLC; images listed as CC BY 2.0) |
| `birddetect3/` | the second bird detector (wide-k285): colour input from an IPU engine-1 resize of the live view to 96x72 YUYV (skipped during still capture and while engine 1 is locked), the network on CPU1 as generated code (ternary layers split into I-cache-sized blocks of output channels), one fire decision per frame on the top cell and a 3x3 centroid point around it, steers the face frame to that point every frame; `gen_model.py` regenerates every model-dependent source and the simulator vectors from a fenykl-nn handoff directory. Weights trained by the separate fenykl-nn project on the Open Images Dataset V7 bird class (annotations CC BY 4.0, Google LLC; images listed as CC BY 2.0) and iNaturalist Open Data bird photos (licensed per photo: CC0, CC BY or CC BY-NC) |
| `nnbench3/` | does a trained ternary layer of birddetect3 run faster as generated code split into I-cache-sized blocks of output channels than with the offset-table kernel: L5 whole and split into 2 to 7 blocks (warm and cold D-cache), L2 and L4 whole and split, a strided-read sweep of the D-cache size and line size; built from nnbench's sources, `gen_kernels.py nnbench3 --handoff DIR` generates the kernels and weights from a fenykl-nn handoff directory |

# Example and experiment plugins

Build one with `uv run fenykl plugin build sdk/projects/<name>`. Kept are the product plugin, the
minimal examples of SDK features that no other plugin uses, and the probes and prototypes that hold implementation details for features not yet in `main`.

| project | what it is |
|---|---|
| `main/` | the daily-use plugin (focus distance, depth of field, focus meter, battery percentage, Fn screenshot, R/B swap, settings page); reference user of the cfg, log, ui, HUD and key libraries |
| `hello/` | smallest plugin: writes `C:\FENYKL.LOG` and returns; the first plugin to try after flashing |
| `proxyget/` | cloned vtable on the settings proxy object; the only user of slots and vtable clones |
| `mfstats/` | the plugin's own contrast client in MF: AF statistics without AF, attach/detach safety (focus meter in MF) |
| `caflog/` | per-frame trace of continuous AF hunting (C-AF) |
| `bchklog/` | battery voltage and thermistor A/D once a second with the battery-check state (voltage readout calibration) |
| `trklog/` | does the C-AF+TR window follow the tracker (tracker injection) |
| `framepaths/` | where the live-view frames come from (focus peaking, magnified view, Pro Capture) |
| `cpu1frame/` | live-view frames read on CPU1 (focus peaking on CPU1); minimal CPU1 worker example |
| `nnbench/` | neural-network kernel costs on CPU1 (conv, ternary, depthwise/pointwise, packed and binary weights, pooling, input preparation, memory and cache), checked against C references, plus the multiply timings of the former `macbench` on both cores; `nnbench_report.py` turns the log into a cost table (detector design) |
| `nnbench2/` | the second nnbench run, built from nnbench's sources with its own plan: input preparation on the live face-detection frame, int8 kernels generated with 4 pixels per call, softmax and the hand-written kernels on the shapes that matter most; `nnbench_report.py` merges both logs |
| `birddetect/` | bird detector on CPU1 that steers the face frame; weights trained by the separate fenykl-nn project on the Open Images Dataset V7 bird class (annotations CC BY 4.0, Google LLC; images listed as CC BY 2.0) |

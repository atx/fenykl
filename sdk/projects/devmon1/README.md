# devmon1: the dev monitor (E-M10 II)

The dev monitor library (`sdk/lib/devmon_*.c`, reference `sdk/docs/devmon.md`) and the settings page
that holds its options, nothing else. The camera opens the access point `FENYKL-DEV` (WPA2,
passphrase from `FENYKL_DEVMON_KEY` at build time, else the public default `fenykl-devmon`) 15 s
after the start and serves the framed protocol v1 on port 5555; the PC drives it with `fenykl dev`.

```sh
FENYKL_DEVMON_KEY=... uv run fenykl plugin build sdk/projects/devmon1 -t em10ii --isa-check
# FENYKL.BIN to the card root by hand; Sleep Off; lever ON; PC joins FENYKL-DEV (docs/devmon.md 10)
uv run fenykl dev wait --connect wlan0
uv run fenykl dev ping
uv run fenykl dev read 0xA0021000 0x100000 -o dram.bin --timing
uv run fenykl dev log -f
uv run fenykl tp build -t em10ii --slot 0 sdk/tp/examples/peek.c -o build/tp
uv run fenykl dev exec build/tp/peek.bin --arg 0x60021020
uv run fenykl dev put notes.txt FENYKL.DAT && uv run fenykl dev get FENYKL.DAT -o back.txt
uv run fenykl dev put build/em10ii/FENYKL.BIN FENYKL.TST   # a test build: the loader runs it once
uv run fenykl dev keys "MENU 500 DOWN 300 OK HALF:1500"
uv run fenykl dev poweroff      # or, after WRITE or TPRUN: uv run fenykl dev power off (relay)
```

`FENYKL.BIN` is never written or deleted by the monitor: the stable image is installed by hand.
main with the monitor, for a remote-controlled demo: `fenykl plugin build sdk/projects/main -t em10ii
--with devmon`.

Options (Custom menu K, Eye-Fi row): `Wi-Fi monitor` (`devmon.ap`, default on) and `Power-off test`
(`devmon.off`, default off: the power-off sequence at 585 s). Every step goes to `C:\FENYKL.LOG`.

# fenykl

<p align="center">
  <em>Prometheus stole fire from the Olympians in a fennel stalk.</em>
</p>

> [!WARNING]
> Most of the code and research in this repository was produced by a liberal application of frontier LLMs.
>
> But don't worry: I know a thing or two about cybersecurity, so the repository also features a dose of "hacker dude in pyjamas eating a hotdog over his keyboard" type of engineering.
>
> You have been warned. Pick your poison.


fenykl is a project providing a firmware mod for early era Olympus Micro Four Thirds cameras. It consists of a small firmware patch that provides a mod loader to load a second stage binary from the SD card. 


> [!WARNING]
> This is an early stage experimental project. I do not believe it is ready for care-free users.

## Features

At the moment, only relatively elementary modifications have been implemented, enhancing the HUD and adding support for native blue-red channel swap.

![Basic info screen with the main plugin: battery percentage, focus distance and depth of field, focus meter, R/B swap](pics/main-info-screen.png)

## Supported cameras

Only the E-PL3 is supported. Support for the the E-M10 Mark II is underway.

## High level overview of function

This repository provides code that can patch an original firmware image (not distributed here, but the binaries are floating on the internets) to 
include a "mod loader". This mod loader then looks for a `FENYKL.BIN` binary on the SD card and if it finds it, it loads it into memory and executes it. 
This allows safe recovery and updating without having to reflash the entire firmware image every time.

Internally, the mod then works its magic by hooking internal firmware functions and controlling various camera functions.

## How to install on the E-PL3

> [!CAUTION]
> This can brick your camera. Make sure you do not care about it enough to not be sad if it gets fucked by installing random software from the internets.

You need Linux with [uv](https://docs.astral.sh/uv/) (Python 3.12+), and an E-PL3 with body firmware
1.6 (Setup menu > Firmware).

Get the stock firmware `OLY_E_086_1600_0000_0000.BIN` somewhere. You can find the binaries floating on the internet in dumps 
from questionable sources, or they might be somehow extractable from the Olympus updater software. Put it into `firmware/`
in this repository.

Validate it is the correct one:

```sh
sha256sum firmware/OLY_E_086_1600_0000_0000.BIN
# 89b70dd65c6739de1cd762777205953ee15864df73369b84815df7ed8a0eb4b1
```

Build the mn10300-elf cross toolchain (GCC 16.2, binutils 2.47) once. The loader does not need it,
plugins do. It downloads the sources (arguments: download directory, build directory), needs a
host C/C++ compiler and make, about 3.5 GB in the build directory and about 15 minutes:

```sh
toolchain/build.sh /tmp/fenykl-dl /tmp/fenykl-build
```

This then installs the toolchain into `toolchain/install/` in the repo. 
in/README.md)).

Patch the firmware:

```sh
uv sync
uv run fenykl patch apply epl3/fenykl-loader --output E0869999.BIN
# wrote E0869999.BIN (46596160 bytes, sha256 ac1db3e8cc54a1f41c4daba261a5058c228f94640548a53ba4be7d93ad509c32)
```

Flash it from the SD card:

1. Charge the battery fully. Take off the lens and anything on the accessory port.
2. Format a plain SD/SDHC card in the camera (no Eye-Fi or other Wi-Fi card).
3. Create the folder `DCOLYMP` in the card root and copy `E0869999.BIN` into it. The name must be
   exactly `DCOLYMP/E0869999.BIN`, upper case, and it must be the only `E086*.BIN` there.
4. Camera off, card in. Hold **OK** and switch the camera on. Keep holding OK until the orange lamp
   on the front lights up, then let go. The screen stays black.
5. Wait a few minutes until the lamp blinks rapidly. **Do not switch off or open the battery or
   card door while the lamp is lit.** An interrupted flash is the main way to brick the camera.
6. Switch off, delete `DCOLYMP` from the card (otherwise every power-on with OK held reinstalls it),
   switch on.

The camera now behaves like stock. Setup menu > Firmware still shows body 1.6.

If the camera just boots into live view in step 4, the update did not start. Try once more, holding
OK firmly before switching on.

> [!CAUTION]
> **Never run a USB firmware update from a PC with `FENYKL.BIN` on the card**. 

### Building and installing the main plugin

```sh
uv run fenykl plugin build sdk/projects/main
uv run fenykl payload info sdk/projects/main/build/epl3/FENYKL.BIN   # must end with "loader accepts"
```

Copy `sdk/projects/main/build/epl3/FENYKL.BIN` into the **root** of the card, not into `DCOLYMP/`.

The plugin stores its settings in `FENYKL.CF0`/`FENYKL.CF1` and writes a log to `FENYKL.LOG` in the
card root. Delete the `.CF*` files to reset the settings.

If anything goes wrong, switch off (take the battery out if the camera hangs), delete `FENYKL.BIN`
from the card on a PC, and the camera boots like stock again.

## Settings page

To configure fenykl and verify that it got loaded, 
go into MENU > Custom menu, tab **J** > last row **Fenykl**, then OK.

A changed setting takes effect at once and is saved two seconds later.

Note that this overwrote the Eye-fi configuration option, meaning Eye-fi can no longer be configured with this mod.

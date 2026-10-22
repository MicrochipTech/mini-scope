# mini-scope – watch dsPIC33CK variables live

A small C module for the dsPIC33CK and a browser user interface on the PC.
The controller samples any RAM variables (8, 16 or 32 bit) at a fixed rate and
sends them in binary over a UART to the PC. The CPU is never halted, so a motor
control application keeps running undisturbed.

Two modes:

- **Stream:** every sample goes to the PC immediately and appears as a running
  curve. The sample rate is limited by the baud rate; the GUI shows whether the
  rate fits.
- **Capture:** the samples go into a RAM buffer in the controller first
  (4 KB by default). A trigger condition (rising or falling edge of a channel
  through a threshold, with pretrigger) starts the recording. When the buffer
  is full, it is transferred. Any sample rate is possible, up to what the
  interrupt load allows.

![Capture](docs/capture.png)

## Files

| File | Purpose |
|---|---|
| `firmware/scope.c`, `scope.h` | module for the controller (register level, no MCC) |
| `firmware/example_main.c` | minimal example with clock setup and demo variables |
| `gui/scope_gui.py` | PC user interface (NiceGUI in the browser) |
| `start_gui.bat` | starts the user interface, sets up Python on the first start |
| `start_sim.bat` | builds `test.X` for the simulator and starts the user interface |
| `USER_GUIDE.md` | user guide |
| `SIMULATOR.md` | guide for the simulation without hardware |
| `TROUBLESHOOTING.md` | troubleshooting guide: what to check when it does not work |
| `test.X` | MPLAB X test project, configurations `default` (PICkit 5) and `sim` (simulator) |
| `tools/setup_env.bat` | creates the Python environment `.venv` on the first start |
| `tools/mdb_serial.py` | connects the GUI to the firmware in the MPLAB X simulator |
| `tools/doctor.py` | checks the PC and the project settings, writes `doctor_report.txt` |
| `tools/hw_check.py` | checks mini-scope on the board (ping, clock, rates, capture, reset), writes `hw_report.txt` |
| `tools/make_pdfs.py` | builds PDF versions of the three guides (needs pandoc and Edge) |
| `CLAUDE.md` | notes for Claude Code, for help with getting mini-scope running on your board |
| `tests/` | PC model: `scope.c` built as a DLL, plus a protocol test |
| `LICENSE.txt` | license |

## Adding the firmware

1. Add `scope.c` and `scope.h` to the MPLAB X project.
2. Call `scope_init();` in `main()` **after** the clock and PPS setup.
   `scope_init()` configures PPS, UART, timer and interrupts and sets the
   global interrupt enable (`GIE`).
3. Check the defaults in `scope.h`. Change them there or with `-D` in the
   project properties:

| Setting | Default | Meaning |
|---|---|---|
| `SCOPE_FP_HZ` | 32000000 | peripheral clock FP = FOSC/2 |
| `SCOPE_BAUD` | 115200 | baud rate, +0.6 % at 32 MHz. The stream carries about 11 kB/s; a full capture buffer takes about 0.4 s to transfer. |
| `SCOPE_UART` | 1 | which UART: 1, 2 or 3, see below |
| `SCOPE_TIMER` | `SCOPE_T1` | sample clock: `SCOPE_T1`, `SCOPE_CCP1` … `SCOPE_CCP4` or `SCOPE_NONE`, see below |
| `SCOPE_TIMER_IPL` / `SCOPE_UART_IPL` | 2 / 1 | interrupt priorities; sampling must be higher than the UART |
| `SCOPE_MAX_CH` | 16 | maximum number of channels |
| `SCOPE_CAPBUF` | 4096 | capture buffer in bytes, 64 to 65535, in practice limited by RAM. The GUI reads the value from the firmware before every start, so it only lives here; nothing to change in the GUI, even after reprogramming. |
| `SCOPE_TXBUF` | 512 | stream transmit buffer in bytes, power of two |
| `SCOPE_TX_RP` / `SCOPE_RX_RP` | 37 / 38 | UART pins as RP numbers, default RB5/RB6 = ICSP header, see below |

**Selecting the UART and the sample clock:** both are at the top of
`scope.h`. Only the selected modules are used; everything else stays free for
the application:

```c
#define SCOPE_UART   1           /* 1, 2 or 3 */
#define SCOPE_TIMER  SCOPE_T1    /* SCOPE_T1, SCOPE_CCP1 ... SCOPE_CCP4 or SCOPE_NONE */
```

| `SCOPE_TIMER` | Sample clock | Interrupt used |
|---|---|---|
| `SCOPE_T1` | Timer1 | `_T1Interrupt` |
| `SCOPE_CCP1` … `SCOPE_CCP4` | SCCP1 … SCCP4 as 16-bit timer | `_CCT1Interrupt` … `_CCT4Interrupt` |
| `SCOPE_NONE` | no timer: call `scope_sample()` yourself, for example at the end of the control ISR. Sampling is then synchronous to the PWM, and the sample period in the GUI must match the ISR period. | – |

The selected UART uses `_UxTXInterrupt` and `_UxRXInterrupt`. All three UARTs
can be mapped to any remappable pin by PPS; `scope.h` sets the codes (U1TX = 1,
U2TX = 3, U3TX = 27) automatically. An invalid value stops the build with a
clear error message.

**RAM:** the dsPIC33CK64MC103 has only 8 KB of RAM, so all buffers can be
sized in `scope.h`. The library needs

> RAM = `SCOPE_CAPBUF` + `SCOPE_TXBUF` + 7 · `SCOPE_MAX_CH` + 56 bytes

plus 4 · `SCOPE_MAX_CH` bytes of stack while sampling in stream mode.
Measured with XC-DSC 3.30, -O1:

| `SCOPE_CAPBUF` | `SCOPE_TXBUF` | `SCOPE_MAX_CH` | RAM | Capture depth with one 16-bit channel |
|---|---|---|---|---|
| 4096 | 512 | 16 | 4776 B (default) | 2048 samples |
| 2048 | 512 | 16 | 2728 B | 1024 samples |
| 1024 | 256 | 8 | 1392 B | 512 samples |
| 512 | 128 | 4 | 724 B | 256 samples |
| 256 | 64 | 2 | 390 B | 128 samples |

- **`SCOPE_CAPBUF`** sets the length of a capture. The depth in samples is
  `SCOPE_CAPBUF` divided by the bytes of all channels. The GUI picks it up
  automatically.
- **`SCOPE_TXBUF`** buffers the stream. It must be a power of two and hold at
  least one frame (2 + 4 · `SCOPE_MAX_CH` bytes). A larger buffer only
  bridges short peaks; the average data rate is limited by the baud rate.
- **`SCOPE_MAX_CH`** limits the number of channels; each channel costs 7
  bytes.

`scope.h` rejects unsuitable values at compile time. Whether the total still
fits into RAM is only checked by the linker; leave room for the stack and your
own variables. The library needs about 2 KB of flash.

**Pins:** by default TX is on RP37/RB5 (PGD3) and RX on RP38/RB6 (PGC3), that
is on the ICSP header, so no rework is needed:

- Program a release build with the PICkit, then disconnect the PICkit.
- Connect a 3.3 V USB-UART adapter to the header: pin 4 (PGD) to the
  adapter's RX, pin 5 (PGC) to the adapter's TX, pin 3 to GND. Do not connect
  pin 2 (3V3).
- Debugging and mini-scope cannot run at the same time. In a debug build,
  leave out `scope_init()`, for example with `#ifndef __DEBUG`.
- Programming always remains possible, because the PICkit enters via MCLR.

Other pins: set the RP numbers in `scope.h`. RB*n* is RP(32 + *n*), RC*n* is
RP(48 + *n*); RP32 … RP53 are possible on the dsPIC33CK64MC10x.

```c
#define SCOPE_TX_RP  39    /* RB7 */
#define SCOPE_RX_RP  40    /* RB8 */
```

`scope_init()` then sets the PPS registers, the pin directions (TRIS) and
digital mode (ANSEL) for both pins; the last one matters, because a pin left
in analog mode reads 0 and the UART receives nothing. Invalid pin numbers, or
TX and RX on the same pin, stop the build with an error message. For a
completely custom setup, define `SCOPE_PINS_INIT()` yourself before
`#include "scope.h"`.

**Pin table** for the dsPIC33CK64MC103 (36 pins):

| Port pin | `SCOPE_TX_RP` / `SCOPE_RX_RP` | Package pin (36-pin) | Other functions on the pin | Note |
|---|---|---|---|---|
| RB0 | 32 | 17 | OSCI, CLKI, AN5 | not with a crystal or external clock |
| RB1 | 33 | 18 | OSCO, CLKO, AN6 | not with a crystal |
| RB2 | 34 | 19 | AN1, AN7, ANA0, CMP1D, OA2OUT, INT0 | analog pin, set to digital automatically |
| RB3 | 35 | 20 | PGD2, AN8, OA2IN- | ICSP channel 2 |
| RB4 | 36 | 21 | PGC2, OA2IN+ | ICSP channel 2 |
| **RB5** | **37** | **24** | **PGD3** | **default TX: ICSP channel 3** |
| **RB6** | **38** | **25** | **PGC3** | **default RX: ICSP channel 3** |
| RB7 | 39 | 26 | TDO, AN2 | JTAG must be off (`JTAGEN = OFF`) |
| RB8 | 40 | 27 | PGD1, AN10, SCL1 | ICSP channel 1, I²C1 |
| RB9 | 41 | 28 | PGC1, AN11, SDA1 | ICSP channel 1, I²C1 |
| RB10 | 42 | 33 | TMS, PWM3H | PWM output, JTAG |
| RB11 | 43 | 34 | TCK, PWM3L | PWM output, JTAG |
| RB12 | 44 | 35 | TDI, PWM2H | PWM output, JTAG |
| RB13 | 45 | 36 | PWM2L | PWM output |
| RB14 | 46 | 1 | PWM1H | PWM output |
| RB15 | 47 | 2 | PWM1L | PWM output |
| RC0 | 48 | 4 | AN12 | analog pin, set to digital automatically |
| RC1 | 49 | 12 | AN13, CMP1B, OA3IN-, ISRC0 | analog pin, set to digital automatically |
| RC2 | 50 | 13 | AN14, OA3IN+, ISRC1 | analog pin, set to digital automatically |
| RC3 | 51 | 16 | AN15, IBIAS2 | analog pin, set to digital automatically |
| RC4 | 52 | 29 | – | free digital pin |
| RC5 | 53 | 30 | – | free digital pin |

Port A (RA0 … RA4) is not remappable and cannot be used. Source: the device
description in the dsPIC33CK-MC_DFP pack (`DSPIC33CK64MC103.PIC`); the
package pin numbers apply to the 36-pin dsPIC33CK64MC103.

Notes:

- If the debugger uses the same ICSP channel (`ICS` configuration bit), do
  not debug while mini-scope runs on these pins.
- On motor control boards the PWM pins are usually taken by the power stage.


**Adapter:** any 3.3 V USB-UART adapter for 115200 baud will do. On a motor
board it should be **galvanically isolated**, or the laptop should run on
battery, so that there is no ground loop.

## PC user interface

```
start_gui.bat
```

The first start finds Python 3.9 or later, creates the environment `.venv` in
the project folder and installs the packages from `gui\requirements.txt`. This
takes about a minute; later starts take a few seconds. Set `MINISCOPE_VENV`
to put the environment somewhere else. The browser opens at
http://localhost:8080.

1. Select the port and click **Connect**. The status line shows whether the
   target responds; the button then reads **Disconnect**.
2. Click **Browse …** and pick the ELF file, or enter its path and click
   **Load**. The ELF is at
   `<project>.X\dist\default\production\<project>.X.production.elf`. Then pick
   the variables from a list with address and size; typing filters the list,
   address and width are filled in automatically. Without an ELF, enter the
   address by hand, for example from the `.map` file.
3. Set up channels. The type is s8/u8/s16/u16/s32/u32/f32. **Factor** converts
   to physical units, for example Q15 → A, or scales signals of different
   size.
4. Select the mode, enter the sample period in µs and click **Start**. Capture
   mode adds trigger channel (number in the channel list, from 0), edge,
   threshold as a raw value and pretrigger. **Auto re-arm** re-arms after
   every capture.
   In stream mode, an orange warning appears when the sample period is too
   short for the UART, with the shortest period that still fits.
5. **Write variable** (next to the channels): one row per variable, set up
   like a channel, with a value field. Enter or the arrow writes the value
   into the controller at once, also during a measurement. The demo variables
   `sine_gain` (sine amplitude, Q15) and `ramp_step` (slope of the ramp)
   show the effect.
6. **CSV** saves the data shown (comma separated, decimal point).
7. **Save** (next to the title) stores all settings in `mini-scope.json` in
   the project folder, which is loaded at every start. **Save as ...** and
   **Load from ...** use a file of your choice.

Zoom in the chart with the mouse wheel or the slider. Clicking a name in the
legend hides that channel. **Autoscale** sets the factors so that all channels
use the same range (the largest channel keeps factor 1, the others get a round
factor 1/2/5 × 10ⁿ); factors apply at once, also during a measurement.

## Test project and simulation without hardware

`test.X` is an MPLAB X project for the dsPIC33CK64MC103 with
`example_main.c` and `scope.c`. It has two configurations:

- **default:** for the PICkit 5, with target power from the PICkit switched
  off.
- **sim:** for the MPLAB X simulator, with the macro
  `__MPLAB_DEBUGGER_SIMULATOR`.

The demo variables are `sine`, `ramp`, `counter` and `toggle`, plus
`sine_gain` and `ramp_step` to try out writing from the GUI.

There are two ways to run without hardware:

**1. MPLAB X simulator (real firmware, slow):** the firmware compiled for the
dsPIC33CK runs in the simulator, driven by MDB, the command line debugger of
MPLAB X. The simulator's UART cannot receive, so `tools/mdb_serial.py` writes
the commands into a mailbox in `example_main.c` via MDB. The answers come from
the simulator's UART output file.

```
start_sim.bat
```

Select port "MDB" and click **Connect**. Starting takes 40–60 s while MDB
(Java) and the simulator come up; a progress bar shows the phase and the time
so far. Then click **Load**, the ELF path is already filled in. The simulator
runs much slower than real time and data arrives a few seconds late, which is
good enough for functional tests. Details are in `SIMULATOR.md`.

**2. PC model (fast, needs gcc, for example MinGW):** `scope.c` is built with
modelled registers as a DLL. Data arrives in real time.

```
gcc -O2 -shared -Itests/mock -Ifirmware -o tests/sim.dll tests/mock/sim.c firmware/scope.c
start_gui.bat --sim                      (port "SIM", leave the ELF field empty, Load)
.venv\Scripts\python tests\test_protocol.py   (30 automatic checks)
```

The paths to MPLAB X v6.35, XC-DSC v3.30 and the DFP are at the top of
`tools/mdb_serial.py` and in `start_sim.bat`.

## What `scope_init()` configures

`scope_init()` brings everything the library needs; nothing has to be set up
in your own code.

| What | Setting | Source |
|---|---|---|
| PPS | TX pin ← UxTX, UxRX ← RX pin (default RP37/RB5 and RP38/RB6); TX output, RX input, both digital | data sheet DS70005399D p. 113, 117 |
| UART | asynchronous 8N1, clock FP (`BCLKSEL = 00`), `BRGH = 1`, `UxBRG = 68` → 115,942 baud (+0.6 %) | p. 272–279 |
| Interrupts | UART RX/TX priority 1, sample clock priority 2, `GIE = 1` | – |
| Timer1 | clock FP (`TCS = 0`), prescaler and `PR1` from the sample period sent by the GUI; runs only during a measurement | p. 329–331 |
| SCCPx | 16-bit timer (`MOD = 0`), clock FP (`CLKSEL = 0`), prescaler and `CCPxPRL` from the sample period, interrupt `CCTxIF` on period match | p. 340–342 |

Checked in the disassembly of the linked ELF (`xc-dsc-objdump`) against the
data sheet and against MCC-generated code for the dsPIC33CK
(`microchip-pic-avr-examples/dspic33ck-lvmc-x2cscope-blinky` for the UART,
`mclv2-mchv2-dspic33ck256mp508-an1160` for the SCCP timer). All 18
combinations of UART and sample clock compile and link without warnings.
UART1 with Timer1 and UART2 with SCCP1 have run in the MPLAB X simulator.

**Preconditions in the application:**

- FP = FOSC/2 must match `SCOPE_FP_HZ`, default 32 MHz. With a different
  clock the baud rate is wrong and no connection is made.
- The selected UART, the selected timer and the two UART pins must not be
  used by the application. The linker reports interrupts defined twice
  (`multiple definition of _T1Interrupt`); then select a free UART or timer in
  `scope.h`.

## Limitations

- Only addresses in data memory below 0x8000 are read. 16- and 32-bit
  channels need an even address; otherwise the target rejects the
  configuration instead of running into an address error trap.
- 32-bit values are read as two 16-bit accesses. If a higher-priority ISR
  writes in between, a single value can be torn.
- SFRs can be entered as well. Avoid registers with read side effects (for
  example `UxRXREG`).
- Writing a variable sends no answer, because it may arrive in the middle of
  a stream; the GUI checks address and value range before sending. A 32-bit
  value is written in two 16-bit steps, so code that reads it at that moment
  can see a mix of old and new value. Writes to SFRs work, but take effect
  immediately on the hardware.
- The trigger compares raw integer values; an f32 channel cannot be the
  trigger.
- Very short sample periods (a few µs) load the CPU noticeably, because every
  sample is an interrupt. In a motor control application, set
  `SCOPE_TIMER = SCOPE_NONE` and call `scope_sample()` from the control ISR.

## Status

- **Build:** the firmware compiles without warnings with XC-DSC 3.30 for the
  dsPIC33CK64MC103 (`-Wall -Wextra`).
- **PC model:** protocol, trigger, pretrigger, frame loss at too high a
  stream rate, buffer sizes and the GUI are tested with the original
  `scope.c`.
- **MPLAB X simulator:** stream and capture with trigger run with the
  compiled firmware from `test.X`, through the GUI.
- **Hardware:** **the module has not yet run on real hardware.**

Two points only showed up in the simulator:

- `GIE` was 0 after reset in the simulator (on silicon it resets to 1
  according to the data sheet), so `scope_init()` sets it.
- The TX interrupt was not raised again after the FIFO had been filled, so
  the timer also kicks the transmitter while a capture is being transferred.

Whether the hardware behaves the same in these two points is open; the
firmware works either way.

## How this was made

Code, tests and documentation were developed with the help of an AI coding
assistant (Claude by Anthropic). The register settings were checked against the data sheet DS70005399D and against
MCC-generated code that runs on dsPIC33CK silicon, and the firmware was
exercised in the MPLAB X simulator and in a PC model, as described above.
It has not yet run on real hardware; please report any issue.

## License

See [LICENSE.txt](LICENSE.txt). You may use this software exclusively with
Microchip products.

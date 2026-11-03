---
title: "mini-scope – Troubleshooting Guide"
subtitle: "Finding the cause when mini-scope does not work on your board"
date: "November 3, 2026"
---

Two scripts do most of the checking for you. Run them from the project
folder (start `start_gui.bat` once before, it creates `.venv`):

```
.venv\Scripts\python tools\doctor.py                 PC and project settings, no board needed
.venv\Scripts\python tools\hw_check.py --port COM5   the board (disconnect the GUI first)
```

Each FAIL or WARN names the section below to read. `hw_check.py --loopback`
checks the USB-UART adapter alone, with its TX and RX bridged. To ask for help,
`tools\report.py` makes the one file to send (section 9). Problems with tuning
scripts are in section 10.

Without a board, the same check runs against the PC model: `hw_check.py --port SIM`.

## 1. Before the first start

Most problems come from one of these points. Check them first:

| Check | Why |
|---|---|
| Program a **production** build ("Make and Program Device"), not a debug build | With `ICS = PGD3`, the debug executive uses RB5/RB6, the same pins as mini-scope. |
| **Disconnect the PICkit** after programming | Its pull-downs on PGC/PGD and its drivers disturb the UART lines. |
| `scope_init()` is the **last** call before the main loop | Pin and PPS setup done later in your code can overwrite the mini-scope pins. `scope_init()` also enables interrupts globally. |
| `SCOPE_FP_HZ` equals FOSC/2 of your clock setup | The baud rate is derived from it. With FOSC = 64 MHz, FP is 32 MHz. With the PLL, FOSC = FPLLO / 2, so FOSC 64 MHz needs `POST2DIV = 1` (section 4.3). |
| UART and timer selected in `scope.h` are **not used** by your application | `scope_init()` reconfigures them. |
| ICSP header pin 1 (MCLR) and pin 2 (3V3) stay **unconnected** | A data line on MCLR resets the controller with every character. |
| The board has its own supply | The USB-UART adapter does not power the board. |

mini-scope has been checked against the data sheet, in the MPLAB X simulator
and in a PC model, but **not yet on real hardware**. If you find a problem
that is not covered here, please report it (see section 9).

## 2. Build errors

| Message | Cause | Remedy |
|---|---|---|
| `multiple definition of _T1Interrupt` (or `_U1RXInterrupt`, `_U1TXInterrupt`, `_CCT1Interrupt` …) | Your application already uses this timer or UART. | Select a free one in `scope.h`: `SCOPE_UART` 1, 2 or 3; `SCOPE_TIMER` `SCOPE_T1`, `SCOPE_CCP1` … `SCOPE_CCP4` or `SCOPE_NONE`. |
| `#error "SCOPE_UART must be 1, 2 or 3"` / `"SCOPE_TIMER must be …"` | Invalid selection | Use one of the listed values. |
| `#error "SCOPE_CAPBUF must be 64..65535 bytes"` / `"SCOPE_TXBUF …"` / `"SCOPE_MAX_CH …"` | Buffer setting out of range | See the RAM table in `README.md`. |
| Linker: data memory full, or `region 'data' overflowed` | mini-scope (about 4.7 KB by default) plus your variables exceed the 8 KB RAM. | Reduce `SCOPE_CAPBUF`, `SCOPE_TXBUF`, `SCOPE_MAX_CH`. Example: 1024 / 256 / 8 needs about 1.4 KB. |
| `'_U1RXR' undeclared` or similar | Wrong device selected in the project | mini-scope is written for the dsPIC33CK64MC10x family. |

## 3. Starting the user interface

| Symptom | Remedy |
|---|---|
| "Python 3.9 or newer was not found" | Install Python from python.org and tick "Add python.exe to PATH". Then run `start_gui.bat` again. |
| "Package installation failed" | The first start downloads packages. Behind a proxy, run `set HTTPS_PROXY=http://proxy:port` in a console, then `start_gui.bat` from the same console. If the company blocks pip, ask IT to allow pypi.org. |
| Browser does not open | Open http://localhost:8080 by hand. |
| Error "address already in use" | Another program uses port 8080. Start with `start_gui.bat --port 8081` and open http://localhost:8081. |
| Windows Firewall asks whether to allow Python | The user interface only needs local access. Choose **Cancel**/**Deny**; the browser on the same PC still works. |
| **Browse …** shows no dialog | The file dialog may open behind the browser window; check the taskbar. If Python was installed without Tcl/Tk, type the path into **ELF file** and click **Load**. |
| COM port busy when programming or using another terminal | Click **Disconnect** first; it releases the port. |
| The environment seems broken after a Python update | Delete the folder `.venv` in the project folder. The next start recreates it. |

## 4. "port open, but no answer from target"

The PC can open the COM port, but the controller does not answer. Work
through these steps in order.

### 4.1 Port and wiring

1. Is the right COM port selected? Unplug the adapter, reload the page (F5)
   and see which port disappears.
2. Is the PICkit disconnected?
3. Are RX and TX crossed? The controller's TX (ICSP pin 4, PGD) goes to the
   adapter's **RX**, the controller's RX (pin 5, PGC) to the adapter's
   **TX**, pin 3 to GND. If in doubt, swap the two lines; nothing breaks.
4. Is the adapter a 3.3 V type? A 5 V or RS-232 adapter does not work.

### 4.2 Is the firmware running at all?

- Was a **production** build programmed? A debug build waits for the
  debugger, or occupies the pins.
- In the simulator (port MDB) the same cause shows as "the firmware in the simulator sent nothing
  within 90 s" after about 110 s. Also check that the ELF is a `sim` build and that `tx_kick()` is
  called (`SIMULATOR.md` section 9).
- Is `scope_init()` actually reached? `clock_init()` in `example_main.c`
  waits in two loops: until `OSCCONbits.OSWEN` is 0 (switch done) and until
  `OSCCONbits.LOCK` is 1 (PLL locked). If the PLL does not lock, the firmware
  stays there, nothing is sent at any baud rate, and `hw_check.py` reports no
  byte at all. A reset loop looks the same: a watchdog enabled in the
  configuration bits (`FWDTEN = ON`) cannot be turned off by software, and a
  trap without a trap handler resets the controller without any sign. To find out, program a
  debug build with a breakpoint on `scope_init()`: if it is never reached,
  press Pause and see where the program stands. Or toggle an LED or test pin
  before and after `clock_init()`.
- If it stands in `clock_init()`, compare with the data sheet, section 9: the
  configuration bit `FCKSM` must allow clock switching, FVCO must be 400 to
  1600 MHz and the PLL input at least 8 MHz, and the dividers must only be
  changed while the PLL is not the active clock.

### 4.3 Is the clock what `SCOPE_FP_HZ` says?

If FP differs from `SCOPE_FP_HZ`, the controller sends at the wrong baud
rate. You can find out without an oscilloscope: in the user interface, set
**Baud** to the values below one after the other and click **Connect** each
time. The rate that connects tells you the real FP:

| Baud that connects | Real FP | Set in `scope.h` |
|---|---|---|
| 115200 | 32 MHz | nothing to change |
| 57600 | 16 MHz | `SCOPE_FP_HZ 16000000UL` |
| 230400 | 64 MHz, faster than intended: check the PLL setup | `SCOPE_FP_HZ 64000000UL` |
| 14400 | 4 MHz: the PLL is not running, the device runs on FRC (8 MHz / 2) | fix the PLL setup, or `SCOPE_FP_HZ 4000000UL` |

In general, real FP = 32 MHz × (baud that connects / 115200). After changing
`SCOPE_FP_HZ`, rebuild, program, and set **Baud** back to 115200.

`hw_check.py` does this scan by itself. When the target answers at another
baud rate, the ping line says "the real FP is *f* x SCOPE_FP_HZ", and
`fp_factor` in the report is *f*. A factor of 0.5 means FOSC is half of what
the code assumes. The most likely cause is `POST2DIV`: FOSC is FPLLO / 2
(data sheet Figure 9-2), and code that sets `POST2DIV = 2` for FOSC 64 MHz gets
FPLLO 64 MHz and FOSC 32 MHz. Such firmware often seems to work otherwise,
because the PWM can take its clock from FPLLO directly. Correct the dividers
rather than `SCOPE_FP_HZ` if the rest of your code assumes the higher clock.
A factor of 0.125 means the PLL is not running at all (FRC, FP 4 MHz).

With an oscilloscope: probe ICSP pin 4 while clicking **Connect**. At
115200 baud one bit lasts 8.7 µs. A bit time of 17.4 µs means FP is half of
`SCOPE_FP_HZ`.

Note on the PLL: the dividers (`PLLPRE`, `PLLFBDIV`, `POST1DIV`, `POST2DIV`) must not be
changed while the PLL is the active clock (data sheet page 152 and Register 9-5).
Start on FRC (`FNOSC = FRC`), set the dividers,
then switch to FRCPLL and wait for `OSCCONbits.LOCK`. `example_main.c` shows
the sequence. If the configuration bits already start on the PLL
(`FNOSC = FRCPLL`), the code must switch to FRC first before it changes them. With a PLL, FOSC is half the PLL output (data sheet Figure 9-2):
FP = FOSC/2 = 8 MHz / `PLLPRE` × `PLLFBDIV` / `POST1DIV` / `POST2DIV` / 4.

### 4.4 Something else uses the pins or the UART

- Does your code write the whole `TRISB`, `ANSELB` or a PPS register
  (`RPORx`, `RPINR18/19/27`) **after** `scope_init()`? Then the UART pins
  are reconfigured. Move `scope_init()` to the end, or exclude bits 5 and 6.
- Does your code use the same UART without interrupts (polled)? There is no
  link error in this case, but both parts disturb each other. Select another
  UART with `SCOPE_UART`.
- With other pins than RB5/RB6: do `SCOPE_TX_RP`/`SCOPE_RX_RP` match the
  wiring? RB*n* is RP(32 + *n*), RC*n* is RP(48 + *n*).
- Are there other loads on the UART pins on your board (LEDs, pull resistors,
  filter capacitors)? Strong loads or large capacitors distort the signal.

### 4.5 Interrupts

mini-scope works entirely in interrupts (UART priority 1, sampling
priority 2).

- Does your code disable interrupts for long periods, or run the CPU at an
  interrupt priority level above 2 (`SRbits.IPL`)? Then the mini-scope
  interrupts are blocked.
- Does your code clear `INTCON2bits.GIE` after `scope_init()`?

## 5. Connected, but the values are wrong

| Symptom | Cause | Remedy |
|---|---|---|
| Curve is a flat line | wrong address, or the variable is not updated | **Load** the ELF again after every rebuild; addresses move. Check that the variable really changes, for example in the debugger. |
| Values make no sense | wrong type | The ELF only provides the size, not the type. Select `u16` for `uint16_t`, `f32` for `float` and so on. |
| Single values are wrong now and then | 32-bit value read while it is being written, or a variable updated in several steps | A 32-bit value is read in two 16-bit steps. Write the value in one assignment from a local variable. |
| Value lags behind | variable kept in a CPU register by the optimizer | Declare it `volatile`. |
| Variable is not in the list | local variable, struct member, array, or larger than 4 bytes | Only global and static variables of 1, 2 or 4 bytes are listed. For a struct member, enter the address by hand: struct address + offset. |
| "Target rejected the configuration" | too many channels, or an odd address on a 16- or 32-bit channel | The maximum number of channels is `SCOPE_MAX_CH`. 16- and 32-bit values must be at even addresses. |
| "lost frames" counts up, orange "⚠ Too fast for the UART" warning | stream rate too high for 115200 baud (about 11 kB/s) | Use at least the sample period named in the warning, fewer channels, or Capture. |
| Time axis is wrong | `SCOPE_TIMER = SCOPE_NONE`, and the sample period in the GUI does not match the rate at which `scope_sample()` is called | Enter the period of the ISR that calls `scope_sample()`. |

## 6. Capture does not work

| Symptom | Cause | Remedy |
|---|---|---|
| Status stays at "captures: 0" | the trigger condition never occurs | The threshold is a **raw value**, without the factor. Check the trigger channel number (the first channel is 0) and the edge. Try edge **none**, which records immediately. |
| No answer from the target after a capture, only a reset helps | a capture period of 1 to 2 µs: the sampling interrupt leaves no time, the capture never ends | The Scope tab and the scripts refuse periods below 10 µs. If the firmware was started with such a period by another tool, reset the target and use 10 µs or more. |
| Capture looks valid, but rise times or frequencies are wrong | the period is shorter than mini-scope can hold, so the samples are not spaced by the period, without any error | Measured limits: 15 µs (32 MIPS) and 5 µs (100 MIPS) without load, 50 µs and 100 µs under a 60 % control interrupt. Use a longer period, or capture a free running timer of your firmware as an extra channel to see the real spacing (USER_GUIDE.md section 6.4). |
| Trigger point not where expected | edge direction, or the signal crosses the threshold several times | The trigger fires at the first crossing after the pretrigger part is filled. |
| A recording takes about half a second to appear | normal: a full 4 KB buffer needs about 0.4 s at 115200 baud | Use a smaller `SCOPE_CAPBUF` for faster updates. |
| Recording is too short | buffer divided by the bytes of all channels | Fewer or smaller channels, or a larger `SCOPE_CAPBUF` if RAM allows. |

## 7. The application behaves differently with mini-scope

| Symptom | Cause | Remedy |
|---|---|---|
| Controller resets during a measurement | the watchdog: with very short sample periods the sampling interrupt leaves too little time for the main loop | Use a longer sample period (see section 6: 50 µs or more under interrupt load), or `SCOPE_TIMER = SCOPE_NONE` and call `scope_sample()` from your control ISR. |
| Your UART or timer stops working | conflict with the module selected in `scope.h` | Select another UART or timer. |
| Interrupts fire earlier than before | `scope_init()` sets `GIE = 1` | Call `scope_init()` after all other initialisation. |
| Motor control jitters | mini-scope interrupts at priority 1/2 delay lower-priority code | Keep your time-critical ISRs above priority 2, or sample from your own ISR with `SCOPE_NONE`. |
| Noise, the connection drops when the motor runs | ground loop or interference through the USB-UART adapter | Use a galvanically isolated adapter, or run the laptop on battery, without docking station, mains adapter or LAN. |

## 8. Simulator

Problems with `start_sim.bat` and the MPLAB X simulator are covered in
`SIMULATOR.md`, section 9.

## 9. Asking for help

Your project data stays with you. Do not send build output, CSV exports, variable
names or types, scripts, ELF files, traces, tuning values, screenshots of your
code or file paths. One file is enough:

```
.venv\Scripts\python tools\report.py
```

It reads `doctor_report.txt`, `hw_report.json` and `run_report.txt` (whichever
exist; run `doctor.py` and `hw_check.py` first) and writes
`mini-scope_report.txt`. The file holds only versions, the result of each check
(OK, WARN or FAIL) with measured numbers (clock deviation, lost frames, abort
time) and rule codes with counts. It is built from a fixed list of names and
number formats, so no path, user name, project name or variable name can get in.
A line that does not fit the list is left out, and the file says how many.

**Open the file and read it.** Then decide yourself whether to send it. The
script sends nothing.

What the file does not say is where in your firmware the fault is. That part you
solve at your desk with the messages of the GUI and the sections above. If you
have a question the file cannot answer, describe it in your own words in the
mail, and leave out names you do not want to share.

## 10. Tuning scripts

The tuning scripts and the Script tab are described in `SCRIPTING.md`. They have run
in the PC model and in the simulator only, never on a real drive.

| Symptom | Cause | Remedy |
|---|---|---|
| A problem with a code such as `[E201]` | a rule of Check or Probe | `SCRIPTING.md` section 9 lists every code with what to do. |
| Run is refused with E402 | the ELF is not the program on the target | Build clean, program, and use that ELF (`SCRIPTING.md` section 6). |
| E401 on the first ping | the target does not answer | As in section 4. On MDB it was seen once with no known cause; press Probe again. |
| The run ends with "stream stopped: no sample for 500 ms" | cable, port, or the target stalled | The safe end ran. Check the stream with `hw_check.py`. |
| The drive stops about 1 s after every run | the heartbeat stops at the end of a run, and `tune_block_ms()` returns 1 | This is intended: the firmware stops the drive when the PC stops talking. It starts again with the next run or a reset. |
| A run ended while nobody looked | no browser page was connected for 5 s | Keep the GUI page open during a run. A reload within 5 s keeps the run. |
| A run on MDB ended after the PC slept | Modern Standby stops the simulator | Keep the PC awake (`SIMULATOR.md` section 6). |
| `tuning.h` was not written | the run did not end normally, or a trace after the last row failed | Look at the status line and the log. `tuning.h` is written only for a confirmed result. |

# M7b: load test in MDB

Base 31d0321. MPLAB X v6.35, XC-DSC 3.30, DFP dsPIC33CK-MC 1.10.386, device dsPIC33CK64MC103, MDB simulator.
Power throttling was off for 2 processes in every session (`full_speed()` of `tools/mdb_serial.py`).
Simulation only: instruction counts are real, timing on a chip, pins and the UART line are not tested.

Raw output: `m7b/run32.txt`, `m7b/run32b.txt`, `m7b/run100.txt` (the per-interval idle lines are left out).

## Files

- `firmware/load_main.c`: replaces `example_main.c` in the load build only (everything is inside `#ifdef LOAD_TEST`).
  SCCP1 interrupt at 20 kHz, IPL 3 (mini-scope: sample timer 2, UART 1), burns `load_burn` cycles with `repeat`.
  The main loop counts `idle`. SCCP2 is a free running 32 bit time base at FP, SCCP3 a 16 bit timer at FP/4 that is
  channel 0 of every stream and capture (so the spacing of samples is measured in target time).
  Snapshots: the PC writes `snap_req`, the main loop copies `idle` and the time base in one pass.
- `docs/evidence/m7b/make_load.py`: build. `m7b/m7b_load.py`: measurement.
- `scope.c`, the protocol, `gui/`, `tools/` and `test.X` are unchanged.

## Build

The IDE project is untouched. `make_load.py` copies `firmware/` and `test.X/` to the temp folder, adds a configuration
`load` (a copy of `sim` with `LOAD_TEST=1`, `load_main.c` instead of `example_main.c`) to the copied
`configurations.xml`, generates the makefiles with `prjMakefilesGenerator.bat` (as `start_sim.bat` does) and runs
`make -f Makefile CONF=load build MP_EXTRA_CC_PRE="-DLOAD_MIPS=n -DSCOPE_FP_HZ=n000000UL -DLOAD_SHARE_PCT=60 -DLOAD_OVERHEAD=51"`.

```
python docs/evidence/m7b/make_load.py m32 32 60 51
python docs/evidence/m7b/make_load.py m100 100 60 51
python docs/evidence/m7b/m7b_load.py --elf <m32 elf> --mips 32                 # run32.txt
python docs/evidence/m7b/m7b_load.py --elf <m32 elf> --mips 32 --phases aDF --periods 40tn,30tn,25tn,20tn,15tn,10tn,40t,30t,25t,20t,10t   # run32b.txt
python docs/evidence/m7b/m7b_load.py --elf <m100 elf> --mips 100 --phases AaBCDF --periods 100t,40t,20t,10t,40tn,20tn,10tn,5tn   # run100.txt
```

Clock for 100 MIPS (DS70005399D Table 31-5, 100 MIPS row, and Equation 9-2): PLLPRE 1, PLLFBDIV 50, POST1DIV 1,
POST2DIV 1: FVCO 400 MHz, FPLLO 400 MHz, FOSC = FPLLO / 2 = 200 MHz, FP = 100 MHz. FVCO 400 MHz is the lower limit
(400 to 1600 MHz, DS 9.1), POST1DIV >= POST2DIV holds. 32 MIPS is the setting of `example_main.c`. `SCOPE_FP_HZ` is
set with it and `#error` checks the pair. The simulator needs `Set oscillator.frequency` = FOSC; the script
sets 64 and 200 by a subclass of `MdbSerial`. Check: 2.009 s of the time base (FP ticks) against 200873649 Stopwatch
cycles at 100 MIPS.

## ISR share (measured)

The share is the loss of idle passes per cycle against a run with the interrupt off (phase A against B).
The REPEAT count is `FP / 20000 * 60 / 100 - 51`: the first build without the 51 gave 63.19 % (1011 cycles per pass
of 1600), so 51 cycles are the cost of entry, the time base reads, exit and the counters. Disassembly of
`_CCT1Interrupt` agrees (about 20 instructions around the `repeat`; the exact cycles of interrupt entry are not given by
the objdump, so the idle counter is the figure).

| MIPS | pass period | REPEAT | idle/cycle off | idle/cycle on | share | isr_len (time base ticks, last pass) |
|---|---|---|---|---|---|---|
| 32 | 1600 cycles | 909 | 0.029412 | 0.011764 | 60.00 % | 930 |
| 100 | 5000 cycles | 2949 | 0.029412 | 0.011763 | 60.00 % | 2970 |

`isr_len` is the span between the first and the last read of the time base inside the ISR, so it leaves out the entry
and exit: 960 cycles per pass less about 30.

## Load figures from the idle counter

Idle passes per second of target time (time base), windows of 2 s (A, a, B), 4 s (D), 20 s (C).
"Used" is the loss of idle/cycle against the row named; "all" is against A (no interrupt at all).
The row `kick` is the control interrupt with a one cycle body: it must run for any stream in the simulator
because it sets `U1TXIF` every 50 us / 100 us at 20 kHz (see `example_main.c`), so it is part of every stream
run. On a real chip it would not exist.

| MIPS | case | idle / s | used against A | used by mini-scope |
|---|---|---|---|---|
| 32 | A, no interrupt, mini-scope idle | 941170 | 0 | |
| 32 | a, kick only, mini-scope idle | 910577 | 3.25 % | |
| 32 | B, 60 % load, mini-scope idle | 376464 | 60.00 % | |
| 32 | C, 60 % load, stream 1 ms, 4 ch | 339190 | 63.96 % | 9.9 % of what B leaves = 3.96 % of all cycles |
| 32 | D, kick only, stream 1 ms, 4 ch | 872105 | 7.35 % | 4.23 % of all cycles (D against a) |
| 100 | A | 2941156 | 0 | |
| 100 | a, kick only | 2910565 | 1.04 % | |
| 100 | B | 1176332 | 60.00 % | |
| 100 | C, 60 % load, stream 1 ms, 4 ch | 1137917 | 61.31 % | 3.27 % of what B leaves = 1.31 % of all cycles |
| 100 | D, kick only, stream 1 ms, 4 ch | 2870914 | 2.39 % | 1.36 % of all cycles (D against a) |

The stream costs about 1.3 to 4 % of the cycles at 1 ms with 4 channels; it is mostly the TX interrupt per byte, so
it scales with the baud rate, not with FP. The idle rate in 30 intervals of C at 32 MIPS stayed within 0.010585 to
0.010615 per cycle (0.3 %).

## Stream with the load

Stream 1 ms, 4 channels of 16 bit (10 bytes per frame), 115200 baud, load 60 %.

| MIPS | target time | frames | lost | resyncs | scope_overflows | spacing of neighbours (FP/4 ticks, nominal 8000 / 25000) |
|---|---|---|---|---|---|---|
| 32 | 20.06 s | 20098 | 0 | 0 | 0 | 7999 / 8000.00 / 8001 |
| 100 | 20.01 s | 20021 | 0 | 0 | 0 | 271 / 24995.00 / 25003 |

At 32 MIPS the frames expected from the time stamps equal the frames received (20098). At 100 MIPS the time stamps
give 20017 and the sample index span is 20021, a difference of 4 frames; the spacing has one pair at 271 ticks
(a sample delayed by the control interrupt, followed by the next one soon after) and the mean is still 24995. Not
resolved: whether the 4 frames come from the 16 bit time stamp arithmetic of the script (neighbours with a delayed
sample) or from the target; no frame is missing in the sequence numbers (lost 0).

Without the load (D, kick only), 4 s: 32 MIPS 4065 frames, 0 lost; 100 MIPS 4023 frames, 0 lost.

### Phase D, first run: 354 lost (test script, not scope.c)

The first run of D turned the control interrupt completely off and lost 354 of 4034 frames, `scope_overflows` 360.
That interrupt also sets `U1TXIF` every 100 us (the simulator workaround), so without it the TX interrupt is not raised
again and the ring fills. It was a leftover of the script, not of phase C: the stream stops draining when the
workaround is gone, `scope.c` counted the drops correctly (354 against 360, the 6 are frames in the ring when the
window ended). Fixed by a mode `kick` (interrupt on, body of one cycle) and the baseline a. Rerun once:
32 MIPS 0 lost, see above. The same applies to the stream at the end of an idle phase: idle phases (A, B) run with no stream.

### Stream at 500 us (E, 32 MIPS, load on)

10 bytes per frame at 500 us = 20000 bytes/s needed; 115200 baud 8N1 carries 11520 bytes/s (about 1150 frames/s).
Sent: 2354 frames in 2.01 s = 11710 bytes/s, so the line was full; 1618 of 3972 lost (41 %), `scope_overflows` 1657.
Expected, and counted, not hidden. The GUI warns before this (USER_GUIDE.md 6.3).

## Capture

4096 byte buffer, 4 channels of 16 bit = 512 samples, trigger rising through 30000 on channel 0 (the FP/4 timer),
pretrigger 20 %. Spacing = difference of the time stamp, nominal ticks of FP/4 = period x FP / 4.

| MIPS | load | period | spacing min / mean / max | nominal | trigger |
|---|---|---|---|---|---|
| 32 | on | 100 us | 800 / 800.00 / 800 | 800 | ok, 102 pre |
| 32 | on | 100 us, no trigger | 799 / 800.00 / 801 | 800 | |
| 32 | on | 50 us | 399 / 400.00 / 401 | 400 | ok |
| 32 | on | 40 us | 92 / 320.15 / 400 | 320 | ok |
| 32 | on | 30 us | 95 / 252.61 / 377 | 240 | ok |
| 32 | on | 25 us | 92 / 245.65 / 363 | 200 | ok |
| 32 | on | 20 us | 105 / 269.93 / 373 (first run 95 / 273.23 / 377) | 160 | ok |
| 32 | on | 10 us | 95 / 251.27 / 361 | 80 | ok |
| 32 | on | 5 us | 95 / 252.03 / 361 | 40 | ok |
| 32 | on | 2 us, 1 us | no capture within 300 s of wall clock | | |
| 32 | off | 40 us | 320 / 320.00 / 320 | 320 | ok |
| 32 | off | 30 us | 239 / 240.00 / 241 | 240 | ok |
| 32 | off | 25 us | 200 / 200.00 / 200 | 200 | ok |
| 32 | off | 20 us | 159 / 160.00 / 161 | 160 | ok |
| 32 | off | 15 us | 119 / 120.00 / 121 | 120 | ok |
| 32 | off | 10 us | 95 / 100.52 / 124 | 80 | ok |
| 32 | off | 1 us | no capture within 300 s of wall clock | | |
| 100 | on | 100 us | 2500 / 2500.00 / 2500 | 2500 | ok |
| 100 | on | 40 us | 375 / 998.78 / 1250 | 1000 | ok |
| 100 | on | 20 us | 92 / 624.43 / 1158 | 500 | ok |
| 100 | on | 10 us | 95 / 416.04 / 913 | 250 | ok |
| 100 | off | 40 us | 999 / 1000.00 / 1001 | 1000 | ok |
| 100 | off | 20 us | 499 / 500.00 / 501 | 500 | ok |
| 100 | off | 10 us | 250 / 250.00 / 250 | 250 | ok |
| 100 | off | 5 us | 124 / 125.00 / 126 | 125 | ok |

Findings:

- Fastest period with the nominal spacing, 32 MIPS: **15 us without the load** (10 us already 100 ticks instead of 80);
  **50 us with the 60 % load** (40 us has the right mean but the spacing jumps between 92 and 400 ticks, so the time axis
  of the capture is wrong sample by sample). At 100 MIPS: without the load at least down to 5 us (not tried below);
  with the load 100 us; 40 us already jumps (375 to 1250).
- With the load the mean spacing at 30 us and shorter settles near 250 ticks (about 1000 cycles, one pass of the
  control interrupt at 32 MIPS): the sample interrupt (IPL 2) only runs between control passes, and a timer flag that
  is set several times is served once, so periods are lost, not queued.
- **scope.c neither reports nor refuses a period it cannot hold.** It ACKs the configuration, the capture finishes with
  512 samples, the trigger works, and the data look valid; only the time stamps (here a channel, which a customer
  does not have) show the wrong spacing. At 1 us and 2 us the sampling interrupt leaves no time for the main loop and
  no capture arrived in 300 s (the 32 MIPS runs needed a device reset through MDB; TROUBLESHOOTING.md 7 already names
  the watchdog for very short periods).
- For the docs (not changed here, outside this card): the capture limit depends on FP and on the load of higher
  interrupts; TROUBLESHOOTING.md 7 says "20 us or more", which is right for FP 32 MHz without the load only
  (limit 15 us) and wrong under a 60 % load at the same FP (limit 50 us). A code change that sets a flag when a sample
  period was missed (timer flag already set at entry) would let the GUI warn; this is a proposal for the supervisor,
  not done.

## Not proven

- Timing on a real chip: UART line, interrupt entry cycles (the simulator counts instructions; entry latency and flash
  wait states at 100 MIPS are not modelled), real FOSC 200 MHz behaviour of the PLL.
- The TX interrupt: the simulator does not raise it again; the stream runs here only with the `U1TXIF` kick of the
  control interrupt. With the load the kick is every 50 us (20 kHz), which is better than a real device needs;
  the rate at which a real TX interrupt competes with a 60 % load is not tested.
- Captures below 5 us at 100 MIPS and 1 us anywhere without a hang.
- Whether the 4 frame difference at 100 MIPS (above) is a script or a target effect.

## Deviations

- The kick mode (phase D, a) was added after the first run; phase D of `run32.txt` (354 lost) is kept there as found,
  its share figure (3.23 %) is not used. The rerun is in `run32b.txt`. Phase A and B at 32 MIPS are from `run32.txt`.
- 100 MIPS phase E (stream 500 us) and the 32 MIPS A, B, C figures were run once each. Phase F at 32 MIPS was run in two
  sessions with different period lists, so the table joins both (20 us is in both: 273.23 and 269.93).
- Capture wait: 300 s wall clock in `run32.txt`, 100 s in the later runs.

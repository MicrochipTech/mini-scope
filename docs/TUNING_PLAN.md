# Tuning scripts: record of what was built

Status: built and tested in simulation only, never run on hardware (2026-11-03).
This was the implementation plan; it is now a short record. The fixed decisions,
the architecture and the safety rules stay as they were agreed.

Python scripts that are loaded by the mini-scope GUI, set the firmware's
parameters, measure and compare the results. Users are BLDC motor control
developers looking for the parameters of their control loops. Scripts must run
unchanged on all three ports: `SIM` (PC model), `MDB` (MPLAB X simulator) and a
real COM port.

## Fixed decisions

- A script is a plain `.py` file with `main()`. The GUI loads it; it also runs
  without the GUI.
- The script sets channels, period and parameters itself. Port, baud rate and
  ELF come from the connection, so the same script works on all three ports.
  The GUI is locked during a run and only displays.
- 115200 baud. Two example scripts show the two modes: `tune_startup.py` uses
  the stream, `tune_current.py` uses capture.
- Stream: 1 ms, up to 4 16-bit channels (10-byte frame, 87 % of the UART). Any
  setup above 90 % of the UART refuses to start.
- Capture: for what is too fast for the stream, default 100 µs, 2 16-bit
  channels (1024 samples, 102 ms). `read()` does not work in capture mode, and
  `limit()` is only checked after the transfer of the buffer.
- No change to `scope.c` or the protocol.
- During a stream, script timing counts received samples, never the wall
  clock. Without a stream no samples arrive, so `wait_ms()` and all timeouts use
  the wall clock, scaled by the port.
- Target is BLDC control in general, FOC as well as block commutation, with
  Hall sensors or sensorless. The scripts do not assume a method.
- The reader of a script is a C programming electrical engineer without Python
  experience, who has to understand and adapt it. Scripts therefore use only
  what reads like C: constants at the top, functions, `if`, `while`, `for`, plain
  calls with one value each. No `lambda`, comprehensions, classes, tuple
  unpacking, `**` arguments or decorators. All convenience that needs those
  goes into the API or is left out.
- The language stays Python, but a firmware programmer should feel at home as
  far as possible: plain functions without an object prefix, `main()`, double
  quotes, `printf()` with C format strings, upper case constants, `Q15()` for
  fixed point, values as the raw integers the C variable holds, and errors in
  the form `file:line: error: text`.
- The example is `scripts/tune_startup.py`: a startup parameter sweep against a
  generic virtual motor. It is the same file for the simulator and for a real
  drive; only the sections for the customer change.
- A customer's firmware has entirely different names and structure, and adapting
  a script must be as easy as possible. Every script has the same four sections:
  1. the customer's variables as constants, 2. short functions that say how the
  firmware works, like a hardware abstraction layer, 3. test settings, 4. the
  test, normally unchanged. Variable names are written as in C, `motor.run` or
  `phase[1].current`, resolved through the debug information in the ELF. The
  example firmware is the worked example of this: it uses structs on purpose, and
  each variable says which role it has in the scripts.
- Everything in this repository stays generic: no customer, product or
  application reference in code, names, comments, numbers or example data.
  Identifiers are plain English motor control terms.
- Customer data stays at the customer. Variable names, ELF data, scripts, traces,
  tables, tuning values and file paths never reach this repository, a report or
  an AI session of ours. mini-scope sends nothing by itself, and `runs/` is
  ignored by git. Problems are reported by rule codes and check results only.

## Architecture

```text
script.py  main()
    |
gui/tune_api.py       functions for the script, compile step, limits, safety, run folder
    |              \
Scope (scope_gui.py)   Script tab (gui/script_tab.py), or tools/run_script.py
    |
SimSerial | MdbSerial | pyserial
```

The block diagram of what was built is [tooling_diagram.png](tooling_diagram.png).

`tune_api.py` does not import `scope_gui`: the GUI runs as `__main__`, a
second import would load the page module twice. The `Scope` object, the symbol
table and `render_png` are handed in.

Script API:

| Call | Meaning |
|---|---|
| `channels("speed", "iq", ...)` | variables to stream, at most 4, one sample per ms |
| `allow(name, min, max)` | a variable may be written, with hard limits |
| `safe_value(name, value)` | written at the end, on abort and on error |
| `limit(name, max)` | above this value the whole run is aborted |
| `stream_start()` | runs all checks, starts the stream |
| `write(name, value)` | writes a variable; refused unless allowed |
| `read(name)` | value at the script's own position in the stream, raw integer (changed in 3.4 from the latest value) |
| `wait_ms(ms)` | waits for ms of target time: received samples during a stream, else scaled wall clock |
| `trace_begin()`, `trace_end(format, ...)` | records and shows one curve |
| `table(name, ...)`, `row(value, ...)` | comparison table and CSV |
| `capture_channels("current_ref", "current")` | variables of a capture |
| `capture_period_us(us)` | sample period of a capture |
| `capture_trigger(name, RISING, level, pre_percent)` | trigger channel, edge, raw level, pretrigger |
| `capture_start()` | runs all checks for capture mode |
| `capture_arm()` | one capture; returns when the pretrigger buffer is full |
| `capture_wait(timeout_ms)` | waits for the buffer; 0 or -1 on a timeout |
| `capture_max(name)`, `capture_final(name)` | largest value, mean of the last 10 % |
| `capture_rise_us(name)` | rise time from 10 to 90 %, -1 if not found |
| `capture_length()`, `capture_value(name, i)` | access to single samples, for own loops |
| `capture_save(format, ...)` | saves the capture as a trace |
| `trace_failed()` | marks the current trace as failed, it is drawn dashed |
| `summary_channel(name)` | the channel shown in the summary diagram |
| `summary_text(format, ...)` | the result line of the summary diagram |
| `summary_mark(value)` | highlights the table row and trace of this value |
| `unit(name, factor, text)` | physical value = raw value x factor, for display only |
| `define(NAME, value)` | one line of the exported `tuning.h` |
| `printf(format, ...)` | C format string, into the progress log |
| `Q15(x)` | float to Q15 integer, for example `Q15(0.25)` = 8192 |
| `expect(condition, text)` | in `check_idle()`: stops the run with this text if false |

The script starts with `from tune_api import *`, which is its `#include`, and
polls in a `while` loop, as firmware would:

```python
def start_motor():
    motor_on()
    time_ms = 0
    while time_ms < START_TIMEOUT_MS:
        if has_fault():
            return -1
        if in_closed_loop():
            return time_ms
        wait_ms(POLL_MS)
        time_ms += POLL_MS
    return -1
```

Before a run, the script gets a compile step, as a firmware programmer expects:
syntax errors and undefined names (`pyflakes`) stop it before anything is
written. Python would otherwise report a mistyped name only when that line is
reached, in the middle of a sweep. All variable names are checked against the
ELF in `channels()` and `allow()`, also before anything is written. For a name
that does not exist, the error suggests the closest ones (`difflib`), as a
compiler does: `tune_startup.py:19: error: 'motor.stat' not in ELF, did you mean
'motor.state'?`

Times and values are examples of the model, not of any real drive.

- `write()` returns only after samples sent after the write can have
  arrived (port attribute for the output lag, 0 on a COM port). Otherwise the
  polling loop would count old samples in `MDB`, where the output lags by about
  200 ms of target time, and run into its timeout. M0 measures that lag.
- `unit()` only changes what is shown: the live chart, the axis of `summary.png`
  and an extra column in the trace CSV next to the raw value. `read()`, `write()`,
  `allow()`, `limit()`, trigger levels and `define()` stay raw integers, as in C,
  so a script never mixes the two.
- `stream_start()` refuses a script without `allow()` and `safe_value()`.
- The API functions are module level in `gui/tune_api.py` and work on the one
  running session; only one script runs at a time.
- Sweeps run from gentle to aggressive; the example shows this as a sorted
  array, there is no sweep helper.
- A trace or table row is marked invalid if `lost` or `resyncs` rose meanwhile.
- Abort (button, `limit()`, exception) always ends in: stop the sweep, write
  `SAFE`, stop the stream, keep what was measured.
- A run folder `runs/<script>_<time>/` holds `results.csv`, `trace_NNN.csv`,
  `run.txt` (port, ELF, parameters, result, and every `write()` with time, variable
  and value), a copy of the script, `tuning.h` if there is a confirmed result,
  and `summary.png`.
- Scripts confirm their result: the suggested value runs several times more
  before `tuning.h` is written. If that fails, there is no `tuning.h`.
- Every run ends with `summary.png`, also after an abort, then with the reason
  on top. It is made by `render_png` (1920 x 1080, a slide) and shows the
  result line, port and ELF, all traces of the summary channel overlaid from
  trace start, failed and invalid ones dashed, and the table from `row()`.
  Above 12 traces or rows it shows the first 12 and a note that the rest is in
  the CSV files. The GUI shows the picture when the run ends.
- A trace longer than `KEEP` samples (200 s at 1 ms) is an error at `trace_end()`.

## Script errors

A customer edits the scripts, so they will contain mistakes. All of them appear
in the **Script** tab of the GUI, list **Problems**, in the form
`tune_startup.py:19: error: text`. A click shows the script line. Errors block
Run, warnings do not. Every rule has a code such as `E203`, which is the only
thing a report contains. Line numbers always point into the script, never into
`tune_api.py`.

| Stage | When | Needs | Finds |
|---|---|---|---|
| 1 Compile | on selecting or saving the file | nothing | syntax errors, undefined names |
| 2 ELF check | Check button, before every run | ELF | see below |
| 3 Dry run | Check button, before every run | nothing | runs `main()` against a fake target, so wrong calls and values show up without a board |
| 4 Target probe | Probe button, before every run | connected target, power stage off | see below |

Stage 2 checks every variable:

- errors: name not found (with suggestions), struct or array without a member or
  index, pointer, bit field, width other than 1, 2 or 4 bytes, address not in
  RAM (`const` data in flash), more channels than fit, an f32 trigger,
- errors in the declarations: `allow()` range outside the C type (`70000` on an
  `int16_t`), min above max, `safe_value()` outside the limits or on a variable
  not allowed, `limit()` on a variable that is not measured,
- warnings: a written variable that is not `volatile`, a 32-bit variable (can
  tear), `limit()` in capture mode (only checked after the transfer), a written
  variable without `safe_value()`, a measured variable without `unit()`, shown
  as `raw` then.

Stage 3 replaces the API by a recorder and calls `main()`: `read()` returns 0,
`wait_ms()` returns at once. It catches values outside `allow()`, `read()` of a
variable that is not measured, `write()` of one that is not allowed, a wrong
number of `row()` values, and runtime errors such as a missing argument. It
cannot follow every branch, because the fake target does not behave like a motor.

Stage 4 is about variables that exist but are not what the script assumes:

- **Live values.** List **Variables** shows every variable with its role in the
  script, address, type and current value. The customer sees that `motor.speed`
  is the speed, and reads the physical meaning, which no tool can check.
- **`check_idle()`.** A required function in section 2 states what must be true at
  standstill, with `expect(condition, text)`. It runs before every run. An
  inverted fault flag or a wrong state value fails here, before the motor starts.
- **Write test.** Each allowed variable without `safe_value()` gets its value
  changed by 1 and restored; if it does not hold for 100 ms, the firmware
  overwrites it. That is the most likely real mistake. Variables with
  `safe_value()` are left out, because writing them could start the motor.

What stays open: a variable with the right type, plausible values and the wrong
meaning; a wrong direction in `motor_on()`; a wrong `safe_value()`. The last one
is the dangerous one, an abort would not stop the motor. M8 covers it by a
required test with the power stage off: press Run, press Abort, watch the state
variable return to stop.

## Safety rules

These rules hold for every run, on every port. SCRIPTING.md sections 5 and 6 say the
same for the user.

- mini-scope is not a safety device. Hardware overcurrent protection, firmware limits
  and a stop path that needs no PC must exist in the drive, independent of mini-scope.
- A script may write only variables it declared with `allow()`, and only values inside
  that range. `stream_start()` and `capture_start()` refuse a script without `allow()`
  and `safe_value()`.
- Nothing is written before `tune_build_id` on the target matches the text
  `tune_build` in the ELF. Before that only the ping and the read of the build id reach
  the target.
- Every end of a verified run goes through the safe end: script returned, Abort,
  script error, `limit()`, port error, no sample for 500 ms, no browser page for 5 s.
  The safe values are written twice and read back, the stream is kept 200 ms, then
  stopped, and the open trace is saved. The safe end runs on its own thread, so a
  script stuck in a loop still reaches it.
- The heartbeat `tune_alive` changes every 200 ms of target time while a run lasts.
  `tune_block_ms()` in the firmware returns 1 after 1000 ms without a change, and the
  firmware stops the drive. The PC stops the heartbeat at the end of every run.
- `limit()` is a PC-side stop: in stream mode it sees a sample after 1 ms at the
  earliest, in capture mode only after the transfer. During the probe it covers only
  what the short streams carry, so the probe runs with the power stage off.
- Capture periods below 10 us are refused (`E229`), because at 1 to 2 us the target
  hangs. Below 50 us there is a warning (`W206`).
- `tuning.h` is written only when the run ended normally and the result was confirmed
  by repeated starts or steps. A person decides whether to use it.
- The first run with the user's own firmware is Check, Probe, then Run and Abort with
  the power stage off: the only test of a wrong `safe_value()`.

## What was built

All of it runs on the PC model (`SIM`) and in the MPLAB X simulator (`MDB`). None of
it has run on hardware. Every card has a record in `docs/evidence/` with the commands,
the raw numbers and what was not tested. The decisions taken on the way are in
`docs/evidence/DECISIONS.md`, the points for the user in `docs/evidence/OPEN_FOR_USER.md`.

| Step | What was built | Evidence |
|---|---|---|
| M0 | risk check in the simulator: second timer, slowdown, output lag | [M0](evidence/M0.md) |
| M1 | virtual motor `firmware/tune_model.h`, example firmware with structs, time base on `SIM` | [1.1](evidence/1.1.md), [1.2](evidence/1.2.md) |
| M2 | `Scope` and port changes | [2.1](evidence/2.1.md) |
| M3 | `gui/elf_vars.py`, `gui/tune_api.py` with limits, heartbeat and safe end, `gui/tune_check.py` (stages 1 to 3), the probe (stage 4), `tools/run_script.py`, `tune_startup.py`, `summary.png` | [3.1](evidence/3.1.md), [3.2](evidence/3.2.md), [3.3](evidence/3.3.md), [3.3b](evidence/3.3b.md), [3.4](evidence/3.4.md), [3.5](evidence/3.5.md) |
| M4 | Script tab: selection, Check, Problems, Run, Abort, Probe, Variables, Log, comparison table, overlay, Export `tuning.h`, Report; the GUI on `MDB`; power throttling of the simulator switched off (`full_speed()`) | [4.1](evidence/4.1.md), [4.2](evidence/4.2.md), [4.3](evidence/4.3.md), [4.4](evidence/4.4.md), [4.4b](evidence/4.4b.md) |
| M5 | `firmware/tune_block.h` with build id and heartbeat, `tune_speed.py` | [5.1](evidence/5.1.md), [5.2](evidence/5.2.md) |
| M6 | capture calls and `tune_current.py`, `tools/report.py` from an allow list, `SCRIPTING.md` | [6.1](evidence/6.1.md), [6.2](evidence/6.2.md), [6.3](evidence/6.3.md) |
| M7 | gaps of the simulator build (PLL fix `POST2DIV = 1`, receive path), load test, capture period floor, fault tests, adaptation test with one case per rule, broken firmware variants, `SIM` against `MDB`, a message when the simulator is silent | [M7a](evidence/M7a.md), [M7b](evidence/M7b.md), [M7b-2](evidence/M7b-2.md), [M7c](evidence/M7c.md), [M7d](evidence/M7d.md), [M7e](evidence/M7e.md), [M7e-2](evidence/M7e-2.md), [M7e-3](evidence/M7e-3.md) |
| M7.1 | documentation checked against the code, every procedure run once | [M7.1](evidence/M7.1.md) |

Numbers that matter for the user, all measured in the simulator:

- Under a 60 % control interrupt above mini-scope, a 1 ms stream of 4 channels lost no
  frame; mini-scope used 4.0 % of the cycles at 32 MIPS and 1.3 % at 100 MIPS.
- A capture holds its period down to 15 us (32 MIPS) and 5 us (100 MIPS) without
  load, and down to 50 us and 100 us under that load.
- The three scripts give the same table on `SIM` and `MDB`: status and counts equal,
  times within one poll step.

Not built from the first plan: the target check before Run (answer, clock, data rate
as in `hw_check.py`). It moves to M8, because only a real chip can show a wrong clock.

Three rare events on `MDB` have no known cause: a stream stop in the probe, `E401` on
the first ping, and a page that lost its connection during a run. Each ended in the
safe end with the safe values read back, none came back in later runs.

## Next steps

### M8: first customer board, no motor current

Customer rule of this repository applies: build `default` in the MPLAB X IDE,
program, disconnect the PICkit.

- **Run** first checks the target like `hw_check.py`: answer, clock, data rate.
  On a failure the script does not start, and the message names the section of
  `TROUBLESHOOTING.md`. Not built yet.
- `tools/report.py` and the Report button exist (6.2). The customer reads the file and
  decides whether to send it.
- Power stage disabled or disconnected. `test.X` on the customer's board,
  `hw_check.py`, then the scripts against the virtual motor in the chip.
- With the customer's own firmware and adapted scripts, still with the power
  stage off: Check, Probe, then the required test of Run followed by Abort, which
  must bring the state variable back to stop.

Done when `hw_check.py` shows no FAIL there, the tables match `SIM`, and pulling
the USB cable stops the virtual motor through the heartbeat. Faults found here
go back into the tests, before the next step.

### M9: real motor at the customer

- Real tuning block in the customer's firmware, low voltage, current limited
  supply, independent overcurrent protection confirmed first.
- `tune_current.py` first, because the other loops build on the current loop,
  then `tune_startup.py`, then `tune_speed.py`.

Done when one complete sweep ran and the exported `tuning.h` works after a
rebuild. Only then is the tool described as tested on hardware. Results of M8 and M9
are added here when they exist.

## What simulation cannot prove

These stay open until a real board has run:

- whether the real clock and PLL start, and the real baud rate,
- pins, PPS on the real device, wiring, noise on the UART,
- the real UART receive and TX interrupt behaviour,
- the real reaction time of an abort, and the heartbeat stop on a real chip,
- the watchdog with `FWDTEN = ON`, timing under real interrupt load, the capture period limits,
- how a real motor reacts; the virtual motor is an assumption, not a measurement.

The first customer is therefore also the first hardware test. M8 is designed so
that this happens without motor current.

## Open

- A customer for M8 and M9.

## Out of scope

Higher baud rates, automatic optimisers, external instruments, measurements
over hours.

## The example scripts

The appendix of the plan held the agreed text of `tune_startup.py` and
`tune_current.py`. They are now in `scripts/`, together with `tune_speed.py`, and
SCRIPTING.md explains them. The values belong to the virtual motor.

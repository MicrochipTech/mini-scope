# Writing and adapting tuning scripts

A tuning script is a short Python file. mini-scope loads it, sets parameters of your
firmware, measures what the drive does and compares the results. The scripts in
`scripts/` are written to read like C, so that a firmware engineer can change them.

**Status.** mini-scope has run in the PC model and in the simulator only. It has
**never run on real hardware.** Everything in this file was checked against the code and
on the PC model and the simulator, and every figure in it was measured there. What needs
a real board is marked *not tried*.

| Script | Mode | What it finds |
|---|---|---|
| `tune_startup.py` | stream | ramp rate of the startup that always reaches closed loop |
| `tune_speed.py` | stream | gains of the speed loop (overshoot and settling time) |
| `tune_current.py` | capture | gain of the current loop (rise time and overshoot of a step) |

Contents: 1 Python for C programmers, 2 Adapting a script, 3 The three scripts,
4 Function reference, 5 Preconditions, 6 The tuning block, 7 Safe channels,
8 Limits of the method, 9 Error codes.

## 1. Python for C programmers

| In C | In a script | Note |
|---|---|---|
| `{ }` around a block | indentation, 4 spaces, and a `:` after the head line | `if x:`, `while x:`, `for n in range(3):`, `def f():`. No `;`. Do not mix tabs and spaces. |
| `a / b` on integers | `a // b` | `/` is always a float division: `7 / 2` is `3.5`, `7 // 2` is `3`. `write()` to an integer variable wants a whole number (`3.0` passes, `3.5` is refused), so use `//`. |
| `-7 / 2` is `-3` (rounds to 0) | `-7 // 2` is `-4` (rounds down) | Differs from C for negative values. `int(-3.5)` is `-3` (truncates like C). |
| `a % b` | `a % b` | The sign of the result follows `b`, not `a`. |
| `&&`  `\|\|`  `!` | `and`  `or`  `not` | `not has_fault()`. Bitwise `& \| ^ ~ << >>` are as in C. |
| `else if` | `elif` | `else:` as in C. There is no `switch`: use `if` / `elif`. |
| `i++`, `i--` | `i += 1`, `i -= 1` | Also `*=`, `//=`. No `++`, no `--`. |
| `for (i = 0; i < n; i++)` | `for i in range(n):` | `range(5)` gives 0, 1, 2, 3, 4. `range(2, 5)` gives 2, 3, 4. |
| `for` over an array | `for kp in GAINS_KP:` | Takes the elements one by one. |
| `int a[3] = {1, 2, 3};` | `A = [1, 2, 3]` | Index from 0, `A[0]`. `len(A)` is the count. |
| `#define NAME 5` | `NAME = 5` | Upper case by habit. No type, no `;`. |
| `int x = 0;` | `x = 0` | No type, no declaration. A name exists from its first assignment. |
| `0x1F`, `42` | `0x1F`, `42` | Integers never overflow and never wrap at 16 bits. `write()` refuses a value outside `allow()`. |
| `true`, `false`, `NULL` | `True`, `False`, `None` | Avoid them: write `1` and `0` as in C. A `row()` value must be a number. |
| `// comment` | `# comment` | |
| `void f(int a)` | `def f(a):` | Arguments have no types. `return x` as in C, a bare `return` ends `main()`. |
| global variable | assign it with `global name` inside the function | Without the line, the function creates a local variable of the same name. |
| `break`, `continue`, `while`, `if` | the same | `while time_ms < 400:` |
| `printf("%d\n", n)` | `printf("%d\n", n)` | The same format codes. Use `%%` for a percent sign. Too few or too many arguments is an error. |
| `"text"` | `"text"` | Use double quotes, as in C. |
| `abs()`, `min()`, `max()` | `abs()`, `min()`, `max()` | Available, the delivered scripts do not need them. |
| `(Q15)0.25` | `Q15(0.25)` | Gives 8192. `Q15(1.0)` gives 32768, which does not fit an `int16_t`. |

What the scripts do **not** use, and what you should not add: `lambda`, list
comprehensions, classes, tuple unpacking (`a, b = f()`), `**` arguments, decorators.
Nothing stops you, but then the script no longer reads like C. A function that
needs to give back two numbers sets two global variables instead, as `tune_speed.py` does.

`from tune_api import *` at the top of every script brings in all functions of
section 4. `main()` is called by mini-scope. You never call it.

The body of the file (everything outside the functions) runs more than once per run:
in the dry run, when the declarations are collected, and in the real run. Keep it to
constants and function definitions. A `printf()` there would print several times.

## 2. Adapting a script

Every script has the same four sections:

1. **Your variables**, as constants at the top, written as in your C code:
   `"motor.run"`, `"phase[1].current"`. mini-scope resolves them through the debug
   information in your ELF file.
2. **How your firmware works**: short functions such as `motor_on()`, `in_closed_loop()`,
   `declare_limits()`, `declare_units()` and `check_idle()`.
3. **Test settings**: the values to try and the time-outs.
4. **The test.** Normally unchanged.

Five steps:

1. **Add mini-scope and `tune_block.h` to your firmware.** Add `scope.c` and `scope.h` to
   the project and set pins, baud rate (115200) and clock in `scope.h`, see `README.md`.
   Include `tune_block.h` once, in the file with your 1 ms task, and add the two calls
   of section 6. Build, program, and compile with debug information: the ELF is the
   source of all names.
2. **Copy a script in `scripts/`** and give it your own name, for example
   `tune_startup_mydrive.py`. Keep the delivered file as the reference.
3. **Change sections 1 to 3.**
   - Section 1: the name of each variable in your firmware. Section 3 of this file
     tells for each variable what role it has.
   - Section 2: make each small function true for your firmware. `motor_on()` writes
     your start command. `in_closed_loop()` compares your state variable with the
     value for closed loop. `declare_limits()` gets your real limits: `allow()` the
     range a variable may take, `safe_value()` the value that stops the drive,
     `limit()` the magnitude at which the run is aborted. `check_idle()` states what
     must be true at standstill.
   - Section 3: start values and time-outs that suit your drive. Start cautiously:
     low gains, short steps, small current.
4. **Press Check** in the Script tab (or run `tools/run_script.py --check <script>`).
   It compiles the script, tests every name against your ELF and runs `main()`
   against a fake target. Nothing is written to the drive. Work through the list
   of Problems from the top; the code in brackets is explained in section 9.
5. **Run it with the power stage off.** Press Probe first: it reads every variable of
   the script, runs `check_idle()` and tests that each written variable keeps its
   value. Look at the table of live values: is `motor.speed` really the speed? Then
   press Run and press Abort once while it runs. Check that the state variable
   returns to stop. This is the only test that shows a wrong `safe_value()`, and a
   wrong `safe_value()` is the dangerous mistake: an abort would not stop the drive.
   Only after that, test with the power stage on, under the conditions of section 5.
   The run with the power stage on is *not tried* on any real drive.

Changing section 1 looks like this:

```text
VAR_RUN   = "motor.run"          # the example firmware
VAR_RUN   = "drive.cmd.start"    # your firmware, a struct member
VAR_SPEED = "speed_meas"         # your firmware, a plain global
```

## 3. The three scripts

"Example firmware" means `firmware/example_main.c` with `tune_model.h`: a virtual motor
that needs no hardware. Raw values are the integers the C variable holds.

### tune_startup.py

| Variable | Role | In the example firmware | Look for in your firmware |
|---|---|---|---|
| `VAR_RUN` | start command, written | `motor.run`: 1 starts, 0 stops | A request the firmware handles, not the output stage itself. 0 must mean stop. |
| `VAR_STATE` | state, measured | `motor.state`: 0 stopped, 1 align, 2 open-loop ramp, 3 closed loop | The state machine. Change `in_closed_loop()` to the value that means "running on the real feedback". |
| `VAR_SPEED` | speed, measured, `limit()` in `tune_speed.py` | `motor.speed`: 0.5 rpm per count | A speed that reads 0 at standstill. Check the unit and set `unit()`. |
| `VAR_CURRENT` | current, measured, `limit()` in both stream scripts | `motor.iq`: Q15, 32768 is 20 A | A slowly changing current (torque current or a filtered value), see section 7. |
| `VAR_FAULT` | fault, measured | `motor.fault`: 0 none, else the model stopped | A flag or code that is 0 when all is well. Check the polarity: `check_idle()` fails if it is inverted. |
| `VAR_RAMP_RATE` | parameter under test, written | `startup.ramp_rate`: counts per 10 ms | The open-loop acceleration. Set `allow()` to a range your drive can survive. |
| `VAR_ALIGN_CURRENT` | parameter, written | `startup.align_current`: Q15 | The current while aligning and ramping. |
| `VAR_HANDOVER_SPEED` | parameter, written | `startup.handover_speed`: counts | The speed where the speed loop takes over. |

Settings you will most likely change: `RAMP_RATES` (sorted from slow to fast, in the
unit of your ramp variable), `ALIGN_CURRENT`, `HANDOVER_SPEED`, `STARTS_PER_SETTING`
(1 on the virtual motor, 5 or more on a real drive), `COAST_MS` (sensorless drives
read speed 0 while the rotor still turns).

### tune_speed.py

It uses the five variables of `tune_startup.py` (run, state, speed, current, fault)
and the three startup parameters, with the same roles. In addition:

| Variable | Role | In the example firmware | Look for in your firmware |
|---|---|---|---|
| `VAR_KP` | speed loop proportional gain, written | `motor.kp` | The gain, in the integer format of your C variable. `allow()` the range you accept. |
| `VAR_KI` | speed loop integral gain, written | `motor.ki` | Same for the integral gain. |
| `VAR_REF_OFFSET` | step of the speed reference, written | `motor.ref_offset`: added to the speed reference of 1500 | A variable that the firmware **adds** to its own reference. If you write the reference itself, the firmware may overwrite it. The write test (E420) finds that. |

Settings you will most likely change: `BASE_SPEED` (the speed the firmware runs at
with offset 0), `OFFSETS`, `KP_LIST`, `KI_LIST`, `SOFT_OVERSHOOT`, `HOLD_MS`. The
startup values in the script must be ones that start your drive (use the result of
`tune_startup.py`). Tune the current loop first (`tune_current.py`).

### tune_current.py

| Variable | Role | In the example firmware | Look for in your firmware |
|---|---|---|---|
| `VAR_CURRENT_REF` | current reference that is stepped, written, trigger | `motor.current_ref`: works at standstill | With FOC the d axis reference, so that the rotor stays put. With block commutation the one current reference. The step must not make the rotor move. |
| `VAR_CURRENT` | current actual value, measured, `limit()` | `motor.current`: Q15, 32768 is 20 A | The measured current of the same axis, in raw units. |
| `VAR_KP` | proportional gain of the current loop, written | `motor.current_kp` | The gain. |
| `VAR_KI` | integral gain of the current loop, written | `motor.current_ki` | The gain. |

Settings you will most likely change: `GAINS_KP`, `STEP_CURRENT` (small, in raw
units), `STEP_MS` (longer than the capture window), `SAMPLE_PERIOD_US`,
`MAX_OVERSHOOT_PERCENT`. Look at the unit of `STEP_CURRENT` first: the step must be
well below your current limit.

## 4. Function reference

All values are raw integers, as the C variable holds them. `name` is a string such as
`"motor.run"`. Modes: **S** = stream, **C** = capture, **both** = either, but one script
uses one mode only (mixing is E315). A script is in capture mode as soon as it calls
a `capture_*` function.

**What happens on an error.** The error text appears as `file:line: error: text`, with
the line of your script. In Check (dry run) it is a problem with a code, section 9. In a
real run the run ends, the safe end runs (section 5) and the result says `error`. A
function that is called outside a running script also raises an error.

### Declarations

Declarations come first in `main()`. After `stream_start()` or `capture_start()`
they are an error (E307).

| Function | What it does | Modes | On an error |
|---|---|---|---|
| `channels(name, ...)` | Variables to stream, at most 4, one sample per ms. Width 1, 2 or 4 bytes. | S | More than 4: E212. Name problems: E201 to E211. Stream above 90 % of the UART: E213. |
| `allow(name, low, high)` | The variable may be written, with values from `low` to `high`. | both | Range outside the C type: E220. `low` above `high`: E221. |
| `safe_value(name, value)` | Written at the end of the run, on abort and on error. Must be whole and inside `allow()`. | both | E222, E223, E225. A written variable without it: warning W204. |
| `limit(name, most)` | The run is aborted when the **magnitude** of the variable exceeds `most`. | both | Variable not in `channels()` (S) or `capture_channels()` (C): E224. Negative: E226. In C see section 8. |
| `unit(name, factor, text)` | Physical value is raw times `factor`. For the display only: `read()`, `write()`, `allow()`, `limit()` stay raw. | both | Missing: warning W205, shown as `raw`. |
| `capture_channels(name, ...)` | Variables of the capture, at most 4. The buffer is `SCOPE_CAPBUF` bytes (4096 in the delivered `scope.h`), so two 16-bit variables give 1024 samples. | C | E212 |
| `capture_period_us(us)` | Time between two samples, whole microseconds, 10 to 65535. Default 100. | C | Not whole or outside 1 to 65535: E227. Below 10: E229. Below 50: warning W206. Whether your firmware can sample that fast is not checked, see section 8. |
| `capture_trigger(name, edge, level, pre_percent)` | Trigger on a capture channel. `edge` is `RISING` or `FALLING`. `level` is a raw whole number. `pre_percent` (0 to 100) is the part of the buffer before the trigger. | C | E218 (float), E228. |

### Start

| Function | What it does | Modes | On an error |
|---|---|---|---|
| `stream_start()` | Checks everything, starts the stream and the heartbeat, runs a trial stream of 2000 samples with no write but the heartbeat. Refuses if the trial lost samples. From here on the script may write. | S | Refuses without `allow()` (E214), `safe_value()` (E215), `channels()` (E216), without `tune_alive` in the ELF (E217), or when the build is not the ELF (E402). Twice: E309. |
| `capture_start()` | Same for capture: checks, heartbeat, one complete trial capture without a trigger. Checks that the transfer takes at most 1000 ms at this baud rate. | C | E216, E219, E402. Without `capture_trigger()`: E228. |

### Stream: write, read, wait

| Function | What it does | Modes | On an error |
|---|---|---|---|
| `write(name, value)` | Writes a variable. Returns when samples taken after the write can have arrived, so a following `read()` sees its effect. | both | Not allowed: E303. Outside `allow()`: E301. Before start: E306. In `check_idle()`: E412. |
| `read(name)` | The value of a streamed variable at the script's own position in time, a raw integer (a float for `f32`). In `check_idle()` it gives the value read by the probe. | S, `check_idle()` | Not in `channels()`: E302. In capture mode: E314. Before start: E306. |
| `wait_ms(ms)` | Waits `ms` of target time: counted in received samples during a stream. Without a stream (capture, before start) it is wall clock time, scaled by the port. In `check_idle()` it returns at once. | both | |

The script position moves by exactly `n` with `wait_ms(n)`. A time is therefore right
to about one poll step (10 ms) or one chunk of the port. Do not use `wait_ms()` for
tiny delays and compare times only with margins.

### Stream: traces

| Function | What it does | Modes | On an error |
|---|---|---|---|
| `trace_begin()` | Starts recording one curve of all channels. | S | Without `trace_end()` of the last one, or before start: E308, E306. |
| `trace_end(fmt, ...)` | Ends the curve. The title is a C format string. A trace longer than 200000 samples (200 s) is an error. | S | Without `trace_begin()`: E308. |
| `trace_failed()` | Marks the open trace as failed. A failed trace after the last `row()` stops `tuning.h` from being written. In capture mode it marks the capture just received, so call it between `capture_wait()` and `capture_save()`. | both | Without a trace or capture: E308. |

### Capture: one capture after the other

The order is fixed: `capture_arm()`, `capture_wait()`, then the evaluation calls, then
`capture_save()`. Another order is E316.

| Function | What it does | Modes | On an error |
|---|---|---|---|
| `capture_arm()` | Starts one capture. Returns when the pretrigger part of the buffer is full, so the trigger can fire from now on. The time is computed from the sample period, the target does not report it. | C | Before `capture_start()`: E306. Twice without `capture_wait()`: E316. |
| `capture_wait(timeout_ms)` | Returns 0 when the buffer has arrived, -1 when no capture arrived within `timeout_ms` (wall clock, scaled by the port). Once the transfer has begun it is not timed out while bytes keep coming. After 0 the `limit()` values are checked on the whole buffer. A buffer with a wrong sample count is logged and marked invalid. | C | Without `capture_arm()`: E316. -1 is no error: the script decides. |
| `capture_max(name)` | Largest value of the channel (not the magnitude). | C | No capture yet: E316. |
| `capture_final(name)` | Mean of the last 10 % of the buffer, truncated towards 0 like C. | C | E316 |
| `capture_rise_us(name)` | Rise time from 10 % to 90 % of the step, in microseconds, to the sample. The step goes from the mean before the trigger (the first sample if the pretrigger is 0) to `capture_final()`. Returns -1 if there is no step or a level is not reached. | C | E316 |
| `capture_length()` | Number of samples. | C | E316 |
| `capture_value(name, i)` | Raw value of sample `i`. Sample 0 is the first of the buffer, which lies `pre` samples before the trigger. The trigger sample is number `pre`, where `pre` is the number of samples in `pre_percent` of the buffer (at most the length minus 1). | C | `i` not a whole number from 0 to length - 1: an error in the run (the dry run may not see it). |
| `capture_save(fmt, ...)` | Saves the capture as a trace. Time 0 is the trigger. The title is a C format string. | C | E316 |

### Results and output

| Function | What it does | Modes | On an error |
|---|---|---|---|
| `table(name, ...)` | Column names of the comparison table. One table per run. | both | Twice or no names: E305. |
| `row(value, ...)` | One row. The count must equal the columns. Values must be numbers. | both | E304, E305, E312. |
| `summary_channel(name)` | The channel shown in the summary picture. | both | |
| `summary_text(fmt, ...)` | The result line of the summary picture. | both | E311 |
| `summary_mark(value)` | Highlights the table row and trace of this value. | both | |
| `define(name, value)` | One `#define` line of `tuning.h`. The file is written only when the run ended normally, `define()` was called, and no trace after the last `row()` is failed, invalid or unfinished. | both | Not a C name: E310. |
| `printf(fmt, ...)` | Writes a line into the progress log. A C format string. | both | E311 |
| `Q15(x)` | Float to Q15: `Q15(0.25)` is 8192. Not clamped. | both | |
| `expect(condition, text)` | Meant for `check_idle()`: stops the run with `text` when the condition is false. | `check_idle()` | E410 |

### `check_idle()` and `main()`

Both are required (E104, E105). `check_idle()` runs before every run, from the probe.
It may only look: `read()` gives the values the probe read, `expect()` states what must
be true. `write()`, `stream_start()` and `capture_start()` there are errors (E412).
`wait_ms()` returns at once. The probe learns which variables `check_idle()` reads by running it once on the fake
target; a `read()` of any other variable there is an error. The values do not change,
so a loop that waits there for a value never ends and is stopped after 10000 `read()` calls.

`main()` ends the run when it returns. A bare `return` is the normal way to give up
after a failed step; the safe values are written after `main()` in any case.

Constants: `RISING`, `FALLING`.

### A complete stream script

```python
from tune_api import *

VAR_RUN     = "motor.run"
VAR_STATE   = "motor.state"
VAR_SPEED   = "motor.speed"
VAR_CURRENT = "motor.iq"
VAR_FAULT   = "motor.fault"
VAR_RAMP    = "startup.ramp_rate"

START_MS    = 400
POLL_MS     = 10


def check_idle():
    expect(read(VAR_SPEED) == 0, "motor must stand still: check VAR_SPEED")
    expect(read(VAR_FAULT) == 0, "no fault expected: check VAR_FAULT")


def main():
    channels(VAR_SPEED, VAR_CURRENT, VAR_STATE, VAR_FAULT)
    allow(VAR_RUN, 0, 1)
    allow(VAR_RAMP, 1, 500)
    safe_value(VAR_RUN, 0)
    safe_value(VAR_RAMP, 100)
    limit(VAR_CURRENT, 1500)
    unit(VAR_SPEED, 0.5, "rpm")
    unit(VAR_CURRENT, 20.0 / 32768, "A")

    table("ramp_rate", "started")
    stream_start()

    for ramp in [50, 100, 200]:
        write(VAR_RAMP, ramp)
        trace_begin()
        write(VAR_RUN, 1)

        started = 0
        time_ms = 0
        while time_ms < START_MS and started == 0:
            if read(VAR_STATE) == 3:
                started = 1
            wait_ms(POLL_MS)
            time_ms += POLL_MS

        if started == 0:
            trace_failed()
        write(VAR_RUN, 0)
        wait_ms(300)
        trace_end("ramp %d", ramp)
        row(ramp, started)
        printf("ramp %d: started %d\n", ramp, started)
```

### A complete capture script

```python
from tune_api import *

VAR_REF     = "motor.current_ref"
VAR_CURRENT = "motor.current"

STEP        = 500
OVERSHOOT_MAX = 10                  # percent


def check_idle():
    expect(read(VAR_REF) == 0, "current reference must be 0")


def main():
    capture_channels(VAR_REF, VAR_CURRENT)
    capture_period_us(100)
    capture_trigger(VAR_REF, RISING, STEP // 2, 10)
    allow(VAR_REF, 0, 1000)
    safe_value(VAR_REF, 0)
    limit(VAR_CURRENT, 1500)
    unit(VAR_CURRENT, 20.0 / 32768, "A")

    table("rise_us", "overshoot_percent")
    capture_start()

    capture_arm()
    write(VAR_REF, STEP)                # this step fires the trigger
    wait_ms(100)
    write(VAR_REF, 0)                   # back to 0 before the transfer
    if capture_wait(500) != 0:
        printf("no trigger\n")
        return

    final = capture_final(VAR_CURRENT)
    overshoot = (capture_max(VAR_CURRENT) - final) * 100 // STEP
    rise_us = capture_rise_us(VAR_CURRENT)
    if overshoot > OVERSHOOT_MAX or rise_us < 0:
        trace_failed()
    capture_save("step %d", STEP)
    row(rise_us, overshoot)
    printf("rise time %d us, overshoot %d %%\n", rise_us, overshoot)
```

Both examples pass Check. The stream script gives two warnings (W205, no `unit()` for
`motor.state` and `motor.fault`, which is fine for a state and a flag). The capture script
gives W205 for the reference and W203, the known limit of section 8.

## 5. Preconditions

**mini-scope is not a safety device.** It is a PC program talking to your drive over a
UART. It can be slow, it can crash, the cable can fall out. Every protection below must
exist **in the firmware and the hardware, independent of mini-scope.**

- **Hardware overcurrent protection** (comparator, desaturation, fuse or current-limited
  supply) that works without any firmware.
- **Firmware limits** on current, voltage, speed and temperature that clamp whatever a
  script writes. The `allow()` ranges are a second fence, not the first one.
- **A stop path that does not need the PC.** The heartbeat of section 6 is that path for a
  lost PC. Test it.
- **A safe first run:** power stage off, then a current-limited supply, the motor
  fixed and unloaded, an emergency stop within reach.
- **A correct `safe_value()`.** It is written twice when a run ends, aborts or fails,
  and read back. It only helps if it really stops your drive. The Run and Abort
  test of step 5 in section 2 is the check.
- **`limit()` is a PC-side stop.** The PC sees a sample at the earliest after 1 ms, then
  needs one UART transfer to write the safe values. It will not catch a fast fault.

**How a run starts.** The probe runs first (Run includes it): ping, then the build id
check, then the heartbeat starts and runs on without a gap into the stream of the run.
From then on every end of the run writes the safe values. Before the build id has
passed, only the ping and the read of the build id reach the target, and no safe value
is written either, because the addresses are not proven yet.

How a run ends, whatever the cause (script returned, abort, error, limit, port error,
no sample for 500 ms): the safe values are written twice and read back, the stream is
kept for 200 ms so that the stop is recorded, the stream is stopped, the open trace is saved.
This runs on a thread of its own, so a script stuck in a loop still reaches it.
Nothing is written to the target before the ELF has been proved to be the one
programmed (section 6). Closing the browser aborts a run after 5 s; a reload within that time keeps it.
If the PC goes to standby during a run, the run stops; the safe end runs after wake-up.
In the simulator (MDB) a closed GUI writes the safe value at once, but `run.txt` has no
read-back, because the read-back takes about 30 s there.
All of this was tested on the PC model and the simulator. On a real drive it is *not tried*.

## 6. The tuning block

`firmware/tune_block.h` gives the scripts what they need from your firmware:

| Name | What it is |
|---|---|
| `tune_alive` | `uint16_t` heartbeat. The PC changes it about every 200 ms of target time while a script runs. |
| `tune_build_id` | `uint16_t`, the CRC-16 of the text `tune_build`, computed at start. |
| `tune_build` | `const char[]`, `__DATE__ " " __TIME__` in flash. |
| `tune_block_ms()` | Call it every 1 ms. Returns 1 when the heartbeat is lost. |

Where to put it: include the header **once**, in the file with your 1 ms task, and
add two calls.

```c
#include "tune_block.h"

void firmware_init(void)
{
    tune_block_init();              /* once at start, before any interrupt is enabled */
}

void task_1ms(void)
{
    if (tune_block_ms()) {
        motor_stop();               /* your code: what "stop" means */
    }
}
```

What it does. The first change of `tune_alive` arms it. After that, `tune_block_ms()`
returns 1 as soon as `tune_alive` stayed unchanged for `TUNE_HEARTBEAT_MS` (1000 ms),
and keeps returning 1 until `tune_alive` changes again. Other writes do not arm it.
At the end of every run the PC stops the heartbeat, so about 1 s later your stop code
runs, and it keeps running until the next run or a reset. A lost cable or a crashed PC
has the same effect.

**What "stop" means is your code.** The block does not touch your variables. Put there
what stops your drive for good: run request 0, torque or current reference 0, outputs
off. It must not depend on the variables a script writes, and it must work when those
variables hold bad values.

**The build id.** The PC reads `tune_build` from the ELF and `tune_build_id` from the
target, and writes nothing if they differ. This proves that the ELF it takes the
addresses from is the program on the target. Two points:

- `tune_build` changes **only when the file that includes `tune_block.h` is compiled
  again.** A change in another file does not move it, and then an old ELF with the same
  text is not noticed. Put the block in a file that changes, or build clean (rebuild all).
- The id proves the build, not every address in it. The other variables are resolved from
  the same ELF, which is the one that was proved.

On a real device the string is read from flash; this was tested in the simulator only.
Compiler layouts that put the text in RAM (`-mconst-in-data`) give the error E402 and
refuse to run. *Not tried:* other layouts, other compiler versions, optimisation levels.

## 7. Safe channels

- **The stream is 1 ms, no faster.** One sample per variable per millisecond, 115200
  baud, at most 4 channels of 16 bits (10-byte frame, 87 % of the UART). A setup above
  90 % of the UART is refused.
- **Phase currents only alias.** A phase current of a running motor, with its PWM ripple
  and an electrical frequency of tens to hundreds of Hz, is sampled at 1 kHz with no
  filter. Everything above 500 Hz folds back into the band, and the curve looks like
  something else. Stream slow values: speed, state, fault, references, gains, and
  currents that change slowly, such as a torque current in the rotating frame or a
  filtered value.
- **Fast signals go to capture.** At 100 us per sample, a signal with content above
  5 kHz still aliases. Capture a current of the control loop (after the transformation
  or filtered), not the raw phase current.
- **The samples are not synchronous to your control loop.** Two channels can come from two
  different control cycles. *Not measured.*
- **32-bit and `float` variables** may be read between the two halves of an update
  (warning W202). Prefer 16-bit variables, or update the value atomically.
- **Samples are taken in an interrupt of mini-scope.** Other interrupts of higher priority
  that run long delay it and cost samples or the UART rate. `hw_check.py` (stream test)
  shows this, see `TROUBLESHOOTING.md`.

## 8. Limits of the method

- **mini-scope has never run on real hardware.** Everything here was tested on the PC
  model and the simulator. Timing, pins, clock, the heartbeat stop, the safe end and every
  script on a real drive are *not tried*.
- **Rare events in the simulator.** In long test series on MDB three events were seen
  once each and not again: a stream stop in the probe, E401 on the first ping, and a GUI
  page that lost its connection during a run. Each ended in the safe end with the safe
  values read back. Their cause is not known. Start the run again.
- **`limit()` is only partly supervised during the probe.** The probe reads and writes
  through short streams of up to 4 variables. A `limit()` is watched only when its variable
  is in the short stream that is running: the live values step reads the variables
  in groups of 4, and the write test streams the written variable and up to 3 limited
  ones. The motor stands during the probe and the write test follows `check_idle()`, so
  the risk is accepted, but a `limit()` is not a protection during the probe. This is
  why step 5 says: power stage off.
- **`limit()` in capture mode is only checked after the transfer.** During the capture
  and the transfer (about 0.36 s for 4096 bytes at 115200 baud) nothing is
  supervised. The run is aborted afterwards, but the damage of a bad gain is done. The
  script steps the reference back right after the window for this reason.
- **Capture periods have a floor, and a short period gives wrong spacing without any error.**
  `capture_period_us()` below 10 us is refused (E229, and at run time before anything is sent).
  Measured in the simulator (MDB), spacing of the samples against the period, with and without a
  60 % control interrupt above mini-scope:

  | MIPS | Load | Period held down to | Below that |
  |---|---|---|---|
  | 32 | none | 15 us | 10 us: 100 ticks instead of 80 (mean) |
  | 32 | 60 % | 50 us | 40 us: mean 320 of 320, but single spacings from 92 to 400 ticks |
  | 100 | none | 5 us | not tried below 5 us |
  | 100 | 60 % | 100 us | 40 us: mean 999 of 1000, single spacings from 375 to 1250 ticks |

  (Ticks are of FP/4.) Below the limit the capture still returns a full buffer that looks valid,
  but the samples are not spaced by the period, so rise times and frequencies from it are wrong.
  The PC cannot see this, a capture carries no time stamps. At 1 to 2 us the capture never ends and
  the target needs a reset. W206 warns below 50 us. These are simulator figures, not proven on a chip.
  To see the real spacing, add a free running timer variable of your own firmware as an extra
  capture channel (or stream channel), for example `capture_channels("motor.current", "app_ticks")`,
  and look at the difference of two neighbouring values with `capture_value()`. It must equal
  the period times the timer rate. Use a 16-bit timer or a counter that does not wrap in the buffer.
- **`read()` does not work in capture mode.** The target sends the buffer after the
  capture. Use `capture_value()`, `capture_final()` and the other evaluation calls.
  `wait_ms()` in capture mode is wall clock time.
- **The write test changes every written variable that has no `safe_value()` by 1** and
  restores it. A variable you allow without a `safe_value()` must be harmless at
  `value + 1` (or `value - 1`) with the power stage off. Variables with a `safe_value()`
  are not written by the test, because it could start the motor.
- **What the checks cannot find:** a variable with the right type and plausible values
  but the wrong meaning, a wrong direction in `motor_on()`, a wrong `safe_value()`. The
  live values table and the Run and Abort test are for these.
- **The dry run does not follow every branch.** The fake target does not behave like a
  motor: `read()` gives 0, `wait_ms()` returns at once. A loop that waits for a value
  stops after a budget with the warning W301.
- **Times are right to one poll step (10 ms) or one chunk of the port.** Do not read
  more precision into a start time than that.
- **Sensorless and Hall drives.** The startup sweep of `tune_startup.py` is made for a
  sensorless drive with an open-loop ramp and a handover. With Hall sensors there is no
  ramp to tune, and the sweep only covers the start current. Remove the ramp and the
  handover from the script, or use another script. *Not tried.*
- **Variables must lie below address 0x8000** (and at an even address if 16 or 32 bits wide):
  the write command of the protocol cannot reach more. The firmware streams at most
  `SCOPE_MAX_CH` channels (16 in the delivered `scope.h`).
- **The method does not replace judgement.** The scripts suggest a value and write it to
  `tuning.h` after confirming starts or steps. A person decides whether to use it.

## 9. Error codes

Every rule of the checker has a code. The Problems list shows `file:line: severity: text [code]`.
E is an error and blocks Run, W is a warning and does not. Stage 1 is the compile,
stage 2 the ELF check, stage 3 the dry run, stage 4 the probe on the target.

This table lists every code of `RULES` in `gui/tune_check.py` (70 codes); `tests/test_adapt.py` checks that.

| Code | Stage | Meaning | What to do |
|---|---|---|---|
| E101 | 1 Compile | Syntax error | Read the line and the text; usually a missing `:`, a bracket or a quote. Indentation must be consistent. |
| E102 | 1 Compile | Undefined name (also a mistyped API function or constant) | A name is not defined. Check the spelling against the suggestion and against section 4. |
| E103 | 1 Compile | Local variable used before it is assigned | A name is assigned later in the function but used before. Assign it first, or add `global name`. |
| E104 | 1 Compile | Main() is missing | Add `def main():`. |
| E105 | 1 Compile | Check_idle() is missing | Add `def check_idle():` with `expect()` calls for what is true at standstill. |
| E201 | 2 ELF check | Variable not found in the ELF (suggestions in the text) | The name is not in the ELF. Use the suggestions. Is the variable optimised away, `static` in a file not built, or is the ELF old? |
| E202 | 2 ELF check | Struct without a member name | Name a member: `motor.run`, not `motor`. |
| E203 | 2 ELF check | Array without an index | Give an index: `phase[1].current`. |
| E204 | 2 ELF check | Pointer | A pointer cannot be used. Name the variable it points to, if it is a global. |
| E205 | 2 ELF check | Bit field | Make the flag a whole `uint8_t` or `uint16_t` variable in the firmware. |
| E206 | 2 ELF check | Width other than 1, 2 or 4 bytes | Only 1, 2 or 4 byte variables. Use a different variable. |
| E207 | 2 ELF check | Address not in RAM (const data in flash) | The variable is in flash (`const`). Use a variable in RAM. |
| E208 | 2 ELF check | Name cannot be resolved (syntax, no such member or index, bad address) | Check the name: member, index and syntax. Indices must be inside the array. |
| E209 | 2 ELF check | Name exists several times (static in several files) | The same name exists in several files. Make one of them unique or use a different variable. |
| E210 | 2 ELF check | The ELF has no debug information | Build with debug information (`-g`) and use that ELF. |
| E211 | 2 ELF check | Type cannot be read | The type is not a plain integer or float. Use a variable with a plain type. |
| E212 | 2 ELF check | More channels than fit | Use fewer channels: 4 at most (stream or capture). |
| E213 | 2 ELF check | The stream needs more of the UART than allowed | Use fewer or narrower channels. Above 90 % of the UART at 115200 baud the stream is refused. |
| E214 | 2 ELF check | No allow() before stream_start() | Add `allow()` for every variable the script writes, before the start. |
| E215 | 2 ELF check | No safe_value() before stream_start() | Add `safe_value()` for what stops the drive, before the start. |
| E216 | 2 ELF check | No channels() before stream_start(), no capture_channels() before capture_start() | Add `channels()` (stream) or `capture_channels()` (capture) before the start. |
| E217 | 2 ELF check | The heartbeat variable tune_alive is not in the ELF | Add `tune_block.h` to the firmware and rebuild, section 6. |
| E218 | 2 ELF check | F32 variable as capture trigger | Trigger on an integer variable. |
| E219 | 2 ELF check | The transfer of a capture takes longer than allowed at this baud rate | Fewer or narrower capture channels, or a smaller `SCOPE_CAPBUF`: the transfer must take at most 1000 ms. |
| E220 | 2 ELF check | Allow() range outside the C type | Make the range fit the C type of the variable (an `int16_t` is -32768 to 32767). |
| E221 | 2 ELF check | Allow() with min above max | Swap the two numbers: low first. |
| E222 | 2 ELF check | Safe_value() outside the allow() range | Make the safe value lie inside the `allow()` range. |
| E223 | 2 ELF check | Safe_value() on a variable without allow() | Add `allow()` for that variable. |
| E224 | 2 ELF check | Limit() on a variable that is not in channels() | Add the variable to `channels()` (or `capture_channels()`), or remove the `limit()`. |
| E225 | 2 ELF check | Safe_value() is not a whole number | Use a whole number. |
| E226 | 2 ELF check | Limit() with a negative value | The limit compares the magnitude. Give a positive value. |
| E227 | 2 ELF check | Capture_period_us() is not a whole number from 1 to 65535 | Use a whole number of microseconds from 1 to 65535. |
| E228 | 2 ELF check | Capture_trigger() missing, or edge, level, pretrigger or channel wrong | Call `capture_trigger(name, RISING or FALLING, level, 0 to 100)` with a variable from `capture_channels()` and a level inside its type. |
| E229 | 2 ELF check | Capture_period_us() below the floor of 10 us, mini-scope does not hold it (1 to 2 us hangs the target) | mini-scope does not hold a period below 10 us, and at 1 to 2 us the target hangs. Use 10 us or more, and 50 us or more under interrupt load (section 8). |
| W201 | 2 ELF check | A written variable is not volatile | Declare the variable `volatile` in C, or the compiler may keep it in a register. |
| W202 | 2 ELF check | A 32-bit variable can tear | Use a 16-bit variable, or accept that a read can be torn. |
| W203 | 2 ELF check | Limit() in capture mode is only checked after the transfer | Known: nothing is supervised during a capture, section 8. Step the reference back right after the window. |
| W204 | 2 ELF check | A written variable has no safe_value() | Add `safe_value()` if an abort should reset the variable. The write test changes a variable without it by 1 during the probe. |
| W205 | 2 ELF check | A measured variable has no unit(), shown as raw | Add `unit()`, or ignore it: the display shows raw values. |
| W206 | 2 ELF check | Capture_period_us() below 50 us may not hold under interrupt load | Under a 60 % control interrupt above mini-scope the period held down to 50 us at 32 MIPS and 100 us at 100 MIPS. Use a longer period, or check the spacing with a time base channel (section 8). |
| E301 | 3 Dry run | Write() value outside the allow() range | Write a value inside the `allow()` range, or widen the range. |
| E302 | 3 Dry run | Read() of a variable that is not in channels() | Add the variable to `channels()`. `read()` needs a streamed variable. |
| E303 | 3 Dry run | Write() of a variable without allow() | Add `allow()` for the variable. |
| E304 | 3 Dry run | Row() has another number of values than table() has columns | Give `row()` as many values as `table()` has columns. |
| E305 | 3 Dry run | Table() or row() used wrongly | Call `table()` once, with names, before `row()`. |
| E306 | 3 Dry run | Call before stream_start() | Call `stream_start()` or `capture_start()` first. |
| E307 | 3 Dry run | Declaration after stream_start() | Move `channels()`, `allow()`, `safe_value()`, `limit()`, `unit()` and the capture settings before the start. |
| E308 | 3 Dry run | Trace_begin(), trace_end() and trace_failed() out of order | Pair every `trace_begin()` with one `trace_end()`; `trace_failed()` only between them or after `capture_wait()`. |
| E309 | 3 Dry run | Stream_start() called twice | Call the start function once. |
| E310 | 3 Dry run | Define() name is not a C name | Use letters, digits and `_`, not starting with a digit. |
| E311 | 3 Dry run | Format string does not match its arguments | The number of `%` codes and arguments must match; use `%%` for a percent sign. |
| E312 | 3 Dry run | A value is not a number | Give a number. A value of `True`, a string or `None` is not one. |
| E313 | 3 Dry run | A variable name is not a string | Give the variable name as a string in double quotes. |
| E314 | 3 Dry run | Read() in capture mode (outside check_idle()) | Use `capture_value()`, `capture_final()` and the other capture calls. |
| E315 | 3 Dry run | Stream calls and capture calls in one script | One script uses stream or capture, not both. |
| E316 | 3 Dry run | Capture calls out of order (arm, wait, then max, final, rise, length, value, save) | Order: `capture_arm()`, `capture_wait()`, evaluation, `capture_save()`. Never two arms in a row. |
| E390 | 3 Dry run | Runtime error in the script (missing argument, wrong type, ...) | Read the text: a missing argument or a wrong type. The line is in your script. |
| W301 | 3 Dry run | Dry run stopped by its budget, the rest was not checked | The dry run cannot follow a loop that waits for a value the fake target never gives. Check that the loop has a time-out. |
| E401 | 4 Probe | The target does not answer the ping | Check cable, port, baud rate and that the firmware runs, see `TROUBLESHOOTING.md`. |
| E402 | 4 Probe | The firmware of the target does not match the ELF, or the ELF cannot prove its build (tune_build, tune_build_id) | The ELF is not the program on the target, or it cannot prove its build. Build clean, program, use that ELF. See section 6. |
| E403 | 4 Probe | Variables cannot be read from the target | A variable could not be read. Check the address and that the firmware runs. |
| E404 | 4 Probe | The heartbeat variable cannot be written | The heartbeat could not be written. Check `tune_alive` and the connection. |
| E410 | 4 Probe | Expect() in check_idle() is false: the target is not what the script assumes | The text says what is wrong. The drive is not at standstill, or a variable does not mean what the script assumes. Check the live values table. |
| E411 | 4 Probe | Runtime error in check_idle() | An error in `check_idle()`. Read the text; it may only call `read()` and `expect()`. |
| E412 | 4 Probe | Check_idle() touches the target (write, stream_start) | Remove the `write()` or start call from `check_idle()`. |
| E420 | 4 Probe | A written variable does not hold for 100 ms: the firmware overwrites it | The firmware overwrites this variable. Write a variable that the firmware adds behind its own logic (an offset) instead. |
| E421 | 4 Probe | A variable changed by the write test could not be restored | Reset the drive: the probe left the value changed. Then check the variable. |
| E422 | 4 Probe | The write test could not be done (stream or port) | Repeat the probe. If it stays, check the stream with `hw_check.py`. |
| W401 | 4 Probe | Write test skipped: allow() has a single value | Nothing to do. Widen `allow()` if you want the write test. |
| W402 | 4 Probe | Write test skipped: the current value is outside allow() and no neighbour is inside | Nothing to do. The current value lies outside `allow()`, so the test is skipped. |

To report a problem without sending anything of yours, give the codes only:
`tools/report.py` writes a file with rule codes and counts.

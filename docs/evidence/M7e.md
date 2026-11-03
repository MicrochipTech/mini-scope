# M7e: test the tests and the long run

Base 9c31d5a. Tester role: only tests, scratch scripts and evidence were written; `firmware/`, `gui/` and `tools/`
are unchanged. Everything ran on the simulator or the PC model. Nothing here says anything about hardware.

Scripts (all in `docs/evidence/m7e/`): `make_variants.py` (broken PC-model variants in scratch copies),
`build_nokick.py` (MDB variant without `tx_kick()`, uses `build()` of `docs/evidence/m7a/m7a_sim.py`),
`hw_check_mdb.py` (runs `tools/hw_check.py` against an MDB session with a given ELF), `longrun.py`.

## 1. Broken variants

Variants (one change each, in a scratch copy, never in the repository):

- wr16: command `W` writes a 4 byte value with 16 bits (`*(volatile uint16_t *)p = (uint16_t)v`).
- wr32: `W` writes a 2 byte value with 32 bits.
- edge: rising and falling trigger condition exchanged in `scope_sample()`.
- nokick (MDB): `tx_kick()` sets only `UxTXIE`, the 10 kHz model ISR no longer sets `U1TXIF` (M7a, `tx_nokick`).

Commands per PC-model variant, in its copy: `tests\test_protocol.py`, `tests\test_script.py`, `tests\test_faults.py`,
`tests\test_adapt.py`, `tools\hw_check.py --port SIM --secs 5`. Number of `FAIL` lines (exit code):

| Test | base | wr16 | wr32 | edge |
|---|---|---|---|---|
| test_protocol | 0 (0) | 0 (0) | 1 (1) | 2 (1) |
| test_script | 0 (0) | 4 (1) | 13 (1) | 5 (1) |
| test_faults | 0 (0) | 0 (0) | 10 (1) | 0 (0) |
| test_adapt | 0 (0) | 0 (0) | 18 (1) | 2 (1) |
| hw_check SIM | 11 OK (0) | 11 OK (0) | 11 OK (0) | 1 FAIL (1) |

The first `test_adapt` run of base failed with a missing `test.X` folder in my scratch copy (my copy lacked it, not a
product fault). `test.X` was copied in and the base run repeated once: exit 0. The other variants ran with it.

What failed, with the first messages:

- wr16: only `test_script`, 4 lines: `read() of probe32 gives -2147483648: script ended`, `limit(probe32, 2147483647) on
  -2147483648: script ended`, `probe32: the script was stopped`, `NaN trips the limit: script ended`. No other test
  writes a 32 bit value over the protocol. Caught by one test only.
- wr32: `test_protocol` `FAIL observed values only rise: every write takes effect in order`; `test_script` 13 lines
  (`limit: script ended`, `the open trace was kept`, ...); `test_faults` 10 lines (`overwritten variable: E420 ...`,
  `motor does not stop: no row was written`, `firmware ignores the stop: the script notices`, heartbeat, script_error);
  `test_adapt` 18 lines (`tune_startup.py (original) on SIM: exit 0, {}`, `every code of RULES has a case: missing ['E420', 'W402']`).
  `hw_check` does not see it: its write check uses the 16 bit `ramp_step`, and the neighbour that gets overwritten is not read.
- edge: `test_protocol` `FAIL rising edge at trigger: 128 -> 0` and `FAIL falling edge through 5000: 4921 -> 5024`;
  `test_script` 5 lines (`kp 400 against the model ...`, `time 0 is the trigger sample, 102 samples (10 %) before it`,
  `rise time and overshoot against model_check.txt ...`, `capture_value(): first sample 0, sample 150 is after the step`,
  `4 traces of the gains and 3 confirming traces: 4`); `test_adapt` 2 lines (`tune_current.py (original) on SIM: exit 0, {}`);
  `hw_check` `FAIL  capture trigger: ramp rising through 30000: 65531 -> 2, pretrigger 136 samples` with the hint
  `TROUBLESHOOTING.md 6`. `test_faults` stays green: no case there depends on the trigger edge.
- nokick (MDB): not one byte leaves the target, so the start-up of `MdbSerial` times out. Built with
  `python docs/evidence/m7e/build_nokick.py`, run in two tools:
  `hw_check_mdb.py <elf> --secs 5` and `tools/run_script.py --port MDB --elf <elf> --probe scripts/tune_startup.py`.
  Both end after about 108 s with `TimeoutError: MDB: firmware does not respond` (exit 1). No check of `hw_check` or the
  probe was reached: the failure is in the connect step. The stalled stream itself (ring full, `head` 511, `tail` 0, 0 bytes)
  is the M7a measurement and was not repeated. `test_gui_mdb.py` was not run on it (it starts the GUI with the fixed ELF).

Every variant is caught by at least one test: wr16 by `test_script` alone, wr32 by all four, edge by four of five,
nokick by the connect step.

## 2. SIM against MDB, same script

`tools/run_script.py --port SIM|MDB`, each once, MDB with `test.X/dist/sim/production/test.X.production.elf`.
Status `ok` in all six. Wall clock: SIM 7.3 s, 10.5 s, 8.7 s; MDB 224 s, 353 s, 280 s (startup, speed, current).
No E401, no stream stop, no retry.

| Script | rows | good_starts and status | time columns |
|---|---|---|---|
| tune_startup | 4 | equal | time_ms 230/170/140/-1 on SIM, 220/170/140/-1 on MDB: one value differs by 10 ms (one poll step) |
| tune_current | 4 | status equal, no `good_starts` column | identical (rise_us 2500/1900/500/200, overshoot 3/0/0/47) |
| tune_speed | 20 | status equal, no `good_starts` column, no invalid row on either side | 9 of 20 rows identical; settle_ms differs by 2 to 6 ms in 6 rows, by 16 in one (1000/150/-300) and by 42 and 48 in two (200/60/+300 and -300: SIM 100, MDB 58 and 52); overshoot differs by 1 in 6 rows and by 2, 5 and 6 in three |

Verdict: tune_startup and tune_current agree within the rule. **tune_speed does not**: the rule allows one poll step
(10 ms) or one chunk, and three rows are beyond it (42, 48, 16 ms), and overshoot differs by up to 6 points
(1500/60/-300: 32 against 38). Per the rule this is a finding, not retried. The status is the same; whether the pair
the script picks is the same was not checked in `tuning.h`. Cause not examined. The settle values on SIM at 100 sit
at the end of the measuring window. The comparison in `test_gui_mdb.py` covers tune_startup only.

## 3. 100 runs in a row on SIM

`python docs/evidence/m7e/longrun.py 100`: tune_startup.py through `tune_api.run()` 100 times in one process with one
`Scope` (SIM). Working set from `GetProcessMemoryInfo`, handles from `GetProcessHandleCount`, threads from the
Toolhelp snapshot of the process.

| point | RSS MB | handles | OS threads | Python threads | status |
|---|---|---|---|---|---|
| start | 98.8 | 278 | 21 | 1 | |
| after 1 | 102.3 | 278 | 21 | 1 | ok |
| after 10 | 103.4 | 278 | 18 | 1 | ok |
| after 50 | 105.1 | 269 | 17 | 1 | ok |
| after 100 | 106.2 | 267 | 15 | 1 | ok |
| ports closed | 106.2 | 267 | 15 | 1 | |

100 of 100 runs `ok`, 672 s. RSS grows 3.5 MB with the first run (imports, caches), then 3.9 MB over the next 99 runs,
about 0.04 MB per run; handles and threads fall. Stable. After the last run only `MainThread` is alive. Open files:
the 100 run folders in the scratch directory were deleted in one go afterwards, which Windows refuses when a file in
them is still open; it worked. Not measured: GUI and MDB over 100 runs (one MDB run is 4 to 6 minutes).

## 4. test_gui_mdb.py, whole

`python tests/test_gui_mdb.py`, once: `all passed`, exit 0, 45 OK, 0 FAIL, no retry line in the output. About 20 minutes.
The three-way comparison: same ramp rates, good_starts and status; time_ms differs by [0, 0, 0, 0] ms (limit 61) between
GUI on MDB, run_script on MDB and run_script on SIM. Slowest answer of the GUI to a page request 0.68 s. No java, mdb or
GUI process left. The page prints console errors (`Could not register theme`, `Anchor: target ... not found`); the test
does not treat them as failures. Not known whether they were there before M7e.

## Findings

- F1: tune_speed differs between SIM and MDB beyond the 10 ms rule (section 2). Supervisor decides.
- F2: only `test_script` (probe32) writes a 32 bit value over the protocol. `test_protocol` and `hw_check` do not,
  so a wrong 4 byte write is caught by 1 of the 5 tests (wr16). A 4 byte write in `test_protocol` and `hw_check`
  would close it.
- F3: `hw_check` has no check that sees a neighbour overwritten by a wrong write width (wr32), and `test_faults` has no
  case that depends on the trigger edge. Both are covered by other tests.
- F4: with a dead TX path `tools/run_script.py --port MDB` and `hw_check` (via `hw_check_mdb.py`) end with a Python
  traceback `TimeoutError: MDB: firmware does not respond` after about 108 s, not with a message that names the cause
  (see TROUBLESHOOTING.md 4.2). `hw_check.connect()` catches only `serial.SerialException`.

## Not tested

Hardware. A stalled stream after the connect (M7a only). `test_gui_mdb.py` and `test_faults` on nokick. 100 sweeps on MDB.
Variants for the 8 bit write width and for the trigger level.

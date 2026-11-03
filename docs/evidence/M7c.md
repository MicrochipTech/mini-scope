# M7c: fault tests in SIM

Base 31d0321. Tester card: only `tests/fault_serial.py`, `tests/test_faults.py` and this file changed.
Simulation only (PC model `sim.dll`); nothing here proves anything on hardware. No MDB was run.

## Commands

```
.venv\Scripts\python tests\test_faults.py            all 29 cases, one pass
.venv\Scripts\python tests\test_faults.py <case>     one case
.venv\Scripts\python tests\test_faults.py --break    the fault of each case switched off, each case must FAIL
.venv\Scripts\python tests\test_protocol.py
.venv\Scripts\python tests\test_script.py
```

## Tools

`FaultSerial` (tests/fault_serial.py) is a `SimSerial` with injectors: corrupt bytes, lost bytes, lost frames,
delay, silent target, port gone (also inside a write and at the first safe write), hanging write, dropped safe
writes, target reset (`sim_init` plus command S), and `stick(addr, value)`, which writes a 16 bit variable before
every 100 us model step (stuck fault, overwritten variable, motor that does not stop, reference that never
changes). Stream damage starts after 300 stream bytes so the start of a stream is not hit.
`MS_FAULT_OFF=<case>` turns the injectors and script mutations of a case into no-ops.
Hooks of `tune_api.run` used: `hold_heartbeat` (heartbeat case), `before_send` (port gone inside a write),
`skip_checks`/`skip_probe`/`skip_build_id` only for the small inline scripts (as test_script.py does); the cases
on `scripts/tune_startup.py` and `tune_current.py` run with the full checks and the probe.

## Bounds (from the documents)

SCRIPTING.md section 5: no sample for 500 ms ends a run; "a run is aborted after 5 s" at the latest. Stall cases:
500 ms + 5 s + 1 s slack. Hung port: rest of script + 3 port calls of 5 s (PORT_TIMEOUT_S) + 1 s.
Heartbeat: SCRIPTING.md section 6, 1000 ms.

## Result of the single pass

29 cases, 354 OK lines, 0 FAIL, 0 known findings, 143 s of it the script_error case. 
test_protocol: exit 0, 0 FAIL. test_script: exit 0, 0 FAIL.

## Cases

| Case | Expected (document) | Observed | Result |
|---|---|---|---|
| corrupt, lost_bytes, lost_frames | run goes on, trace and row invalid, no tuning.h (SCRIPTING define(); plan "invalid") | as expected, stream recovers, safe values read back | pass |
| row_invalid | row invalid in table and results.csv | as expected | pass |
| trial_damaged | trial with losses refuses, only heartbeat and safe values written (stream_start) | as expected | pass |
| delay 0.3 s / 1.2 s | below 500 ms no fault; above: stall abort (section 5) | as expected; the read-back during the stall is reported as error | pass |
| silent | stall abort within bound, safe writes sent, read-back failure reported | as expected | pass |
| silent_start | E401 names TROUBLESHOOTING.md, nothing written | as expected | pass |
| port_gone, in a write, at safe write, before probe | status aborted/error, "port error", read-back skipped, open trace kept, no thread left | as expected | pass |
| port_hang | ends, error says port does not return, within bound | 7.5 s after hang, bound 17.5 s | pass |
| reset_trial, reset_sweep | stall abort, no tuning.h, model idle | as expected | pass |
| build_id (target, ELF text, stream_start) | E402, nothing written (section 6, E402) | as expected | pass |
| overwrite | E420 at the allow() line, text names cause and cure | as expected | pass |
| fault_idle | E410 at the expect() line, motor not started | as expected | pass |
| fault_mid | no good start, rows 0, traces failed, no tuning.h | as expected | pass |
| no_stop | script says so, returns, no tuning.h | as expected | pass |
| ignores_stop | "safe value not reached" is an error | as expected | pass |
| trigger_never | script says what to check, no row, no tuning.h | as expected | pass |
| capture_lost | short dump: no trigger result or capture_start refuses | as expected | pass |
| heartbeat | fault 5 1000 ms after last change, motor stopped by target | 1001 ms | pass |
| script_error | error at any line of both example scripts: line named, safe values | 98 lines tried in the two scripts (56 + 42): 58 found by stage 3 as E390 at the line; 40 not found by the dry run, run for real: 25 reported at their line (ZeroDivisionError), 15 never executed (branches not taken) | pass |
| script_types | E101/E102 refused with only ping; KeyError, SystemExit, ValueError at their line; endless loop stopped by abort | as expected | pass |
| script_module | module level error refused at its line | as expected | pass |
| tool_port_lost | run_script.py exit 1, "Status aborted: port error", report | 9 s | pass |
| tool_script_error | exit 1, file:line message | as expected | pass |

## Can each case fail

`test_faults.py --break` (full output kept out of the repo): all 29 cases printed
"FAILS as it must"; "cases that did not fail with their fault off: []". The FAIL lines of several cases are
what the check prints, for example `FAIL heartbeat lost: fault 5 None ms after the last change`.

## Not tested

GUI path, MDB, real timing, real UART errors (framing, overrun), a hung read (only a hung write), reset of
the static state of `scope.c` (the DLL has no real reset; `sim_init` plus S is the nearest model).

# M7d: test of the adaptation (tests/test_adapt.py)

Tester card: only new files, no existing file changed. Python is the repo `.venv`, gcc as in CLAUDE.md. SIM only, no MDB.

New files

- `tests/test_adapt.py`: the test.
- `tests/model2_serial.py`: Python side of the second model (symbol table, port class, gcc call).
- `tests/mock/sim2.c`, `tests/mock/model2.h`: the second model. `sim2.c` is `sim.c` with `m2_step()` in place of
  `tune_model_step()`; the dynamics of `tune_model.h` run hidden behind the new interface.
- `tests/fixtures/adapt/tune_{startup,speed,current}_m2.py`: copies of the three scripts, adapted.

## The second model

Struct `axis[0]` at 0x1400, heartbeat at 0x1480, build id at 0x1482. Names: `ctl.disable`, `st.mode`, `st.rpm`, `st.rpm_cmd`,
`st.torque`, `st.flags`, `sl.kp/ki/trim`, `cl.ref/act/kp/ki`, `su.acc/lock_amp/switch_rpm`. Inverted logic: `disable = 1` stops
(`safe_value` is 1, start is 0), states 7/8/9/12 instead of 0/1/2/3, the fault is a bit mask (bit 0 = power ok, always set; bits 4
to 9 are faults). Build variants by `-D`: `M2_OVERWRITE` (firmware writes `su.acc` back every pass), `M2_REVERT` (writes it back
after 30 ms), `M2_LATCH` (`su.lock_amp` can only grow, so the restore fails), `M2_STUCK_FAULT` (a fault bit that never clears).
`sim_axis_layout()` exports the offsets gcc computed; the test compares them with its symbol table.

## 1. Adaptation, per script

Command: `.venv\Scripts\python tests\test_adapt.py adapt` (also in the full run).

| Script | Lines changed vs scripts/ | Outside sections 1 to 3 | Run on the second model | tuning.h |
|---|---|---|---|---|
| tune_startup | 13 | 0 | status ok, exit 0, 13 s | RAMP_RATE 100, ALIGN_CURRENT 400, HANDOVER_SPEED 600 |
| tune_speed | 16 | 0 | status ok, exit 0, 15 s | SPEED_KP 500, SPEED_KI 150 |
| tune_current | 4 | 0 | status ok, exit 0, 12 s | CURRENT_KP 400, CURRENT_KI 20 |

The tuning.h of each run equals the one of the original script on the example model. `tune_startup_m2` gives the same table
(50: 1 good, 100: 1, 200: 1, 400: 0 and failed). The diff check is `difflib` on lines against the original, with the heading lines
`# ---------- 1.` and `# ---------- 4.` as borders: a change, delete or insert outside the borders is reported. It was shown to
fail for a changed line in section 4, a changed heading, a changed header comment and an inserted line in section 4, and to pass
for a changed line in section 1 and an insert at the end of section 3. The unchanged `tune_startup.py` stops on the second model
with E201. The safe value of the second model (disable = 1, `cl.ref` = 0) is written at the end of each run (found in run.txt).

## 2. Inverted logic and a variable the firmware overwrites

- Names of the second model with sections 2 to 4 of the example: `check_idle()` stops at `expect(not has_fault()...)` with E410 at
  its line (the bit "power ok" reads as a fault). The motor never starts.
- Adapted copy on `M2_STUCK_FAULT`: E410 at the same line. On the healthy model: status ok.
- `M2_OVERWRITE` and `M2_REVERT`: E420 at the `allow()` line of `su.acc`. `M2_LATCH`: E421 for `su.lock_amp`.
- Not caught by design (plan, "What stays open"): a wrong closed-loop value in `in_closed_loop()` is no fact of standstill, so
  `check_idle()` passes. The run then finds it (the start never reaches closed loop).

## 3. Rules

`RULES` has 68 codes. The test reads the list from `RULES` and fails for a code without a case. It also compares the list with the
table of SCRIPTING.md section 9. Result: 68 of 68 have a case, 0 gaps. Each case runs twice: with the mistake it must give
exactly the expected codes, lines (found in the changed script text) and a part of the message; the same expectation must fail
on the script without the mistake (otherwise the case could not fail, and it is not counted).

How each is provoked (all on SIM; where it is not a plain script on the model, the means is named, nothing is skipped silently)

| Codes | Means |
|---|---|
| E101 to E105, E212 to E216, E218 to E228, W203 to W205, E301 to E316, E390, W301 | script text with exactly this mistake, names of the second model, dry run |
| E201 | misspelled name on the second model |
| E202 to E209, W201, W202 | names in the DWARF fixture `tests/fixtures/vars.elf` (a real ELF, not SIM) |
| E210 | fixture ELF without debug information |
| E211 | substitute: a table that raises `VarError('unsupported')` for one name. No fixture variable has that type, so the mapping is tested, not a real ELF type |
| E217 | table without `tune_alive` |
| E401 | substitute: a port class that never answers. A real COM port is not used |
| E402 | target build id differs; and an ELF table without the text `tune_build` |
| E403 | the name `odd` (odd address): the target refuses the stream configuration |
| E404 | test hook `hold_heartbeat` keeps tune_alive unwritten |
| E410, E411, E412 | `check_idle()` with a false expect (three ways: inverted fault, wrong state value, stuck fault bit), a division by zero, a `write()` |
| E420, E421 | firmware variants `M2_OVERWRITE`/`M2_REVERT` and `M2_LATCH` |
| E422 | substitute: `Scope.write` is patched to refuse one address |
| W401, W402 | `allow()` with one value; `allow()` that does not contain the current value |

Not covered by this test: a real COM port, a real missing port at E401, the real timing of E219 (the check is by baud).

Proof that the cases can fail:
`ADAPT_REMOVE_MISTAKE=E220,E301,E102,E214,E410,E420,inverted .venv\Scripts\python tests\test_adapt.py rules inverted`
runs the named cases without the mistake. Result: 11 FAIL lines. E102, E214, E220 and E301 each fail ("got [], wanted ...");
E410 fails three times (inverted fault, wrong state value, stuck fault bit); E420 fails twice (`M2_OVERWRITE`, `M2_REVERT`);
the inverted-logic case fails ("E410 ... []"); and "every code of RULES has a case" fails with the six missing codes. That is
six rules (E102, E214, E220, E301, E410, E420) plus the inverted case.

## 4. Markers

Setting: a copy of tools, gui, firmware, tests and test.X in a folder named `MKRDIR_q7/MKRUSER_w3`, `USERNAME`/`USER`/`HOME`/
`USERPROFILE` set to a marker, an ELF path with a marker folder, script file name and variable names with markers
(`MKRVAR_axis[0].MKRVAR_ctl...`), marker texts in `int()`, `unit()`, `define()`, `printf()`, `expect()` and the abort reason.
`doctor.py` and `hw_check.py --port SIM` ran from that tree. Seven runs on the second model: ok, value error at run time,
script error at run time (write outside `allow()`), hard `limit()` stop, abort by `tune_api.abort(<marker>)`, `check_idle()`
failure (probe), ELF name error. For each: the status is checked, the console must show a marker (so that the scan has something
to find), then the scan of `run_report.txt`, the console of `report.py` and `mini-scope_report.txt`.

Result: no marker in `doctor_report.txt`, `hw_report.txt`, `hw_report.json` and `mini-scope_report.txt` (7 runs). Two exceptions,
see findings F1 and F2.

Proof that the marker test can fail (a leak planted in the COPY of a tool; the repository files are unchanged):

| `ADAPT_LEAK` | Result |
|---|---|
| `report` (path appended to the report text) | FAIL: console and `mini-scope_report.txt` hold `MKRDIR_q7` and `MKRUSER_w3`, in 4 or more runs |
| `doctor` (ROOT in the report header) | FAIL: `doctor_report.txt` holds both |
| `hw` (user name in the header) | FAIL: `hw_report.txt` holds `MKRUSER_w3` |

## Findings

- F1 (low): `tools/report.py` ends with `Written to <absolute path of the output>` on the console. In the marker setting the console
  output holds the folder and user name. `mini-scope_report.txt` itself is clean. The text says "built from an allow list and holds
  no path"; that is true for the file, not for the console. The test marks it `XFAIL` and turns into a FAIL when fixed (then the
  exception in the test is removed). Reproduce: run `tools\report.py` from a folder with a marker in its path, read the last lines.
- F2 (note): `run_report.txt` (the input of `report.py`) holds the script base name (`script <name>.py`), as the `run_script.py`
  docstring documents. `report.py` drops it, so the report is clean. The test checks both.
- No other deviation from the documents found. E211 and the substitutes above are the weakest part: they test the mapping, not a
  real case.

## Whole run

`.venv\Scripts\python tests\test_adapt.py`: 248 OK, 0 FAIL, 1 XFAIL (F1), 180 s, exit 0.
`tests\test_protocol.py` exit 0, `tests\test_check.py` exit 0 (237 OK lines for both together). Each run once.

## Not tested

MDB, a real COM port, real hardware, timing on a real target. The second model reuses the dynamics of the first, so it shows that
the scripts adapt, not that they work on another drive. The markers cover the files named in the card, not the run folder or
`summary.png`, which stay with the user by design.

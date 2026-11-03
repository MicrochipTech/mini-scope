# Decisions

Decisions taken during the supervisor run, with their reason. The supervisor fills this list.


- M0, working copy: sub-agents work in a worktree on a local disk on branch `tuning-work`, because
  the main checkout lies in a synced folder and holds `tuning-scripts`. Each accepted card
  fast-forwards `tuning-scripts`, so only accepted work reaches it.
- M0, `time_scale`: stays 25 for `MdbSerial`. Measured 14.5 and 18.3 under different PC load, never
  without a stream. It only scales wall clock waits and timeouts, so too high costs little and too
  low gives false timeouts.
- M0, TX kick: the simulator build of the example firmware sets `_U1TXIF` in the 10 kHz interrupt,
  which removes the 10 % loss in `MDB`. `scope.c` stays unchanged. The lag attribute of `MdbSerial`
  uses the value with the kick, 3 ms target time. Variants that test a missing `tx_kick()` (M7e)
  must switch this off, or they cannot fail.
- 1.1, GUI check of M1: `motor.run` by name in the GUI on `MDB` needs `gui/elf_vars.py` (3.1), so that
  part of the M1 done criterion moves to the acceptance of M4. On `MDB` the start was shown through
  the `Scope` protocol in 1.1.
- 3.1, `*.elf binary` in `.gitattributes`, because the fixtures are ELF files and `* text=auto` only
  gets that right by detection.
- 3.2, safety design, goes further than the plan's words, none of it weakens them: limits and the
  safe end run on their own monitor thread, not in the script thread, so a script stuck in a loop
  still reaches the safe end. `limit()` compares the magnitude. A stream that stops counts as an
  abort. `W` has no reply, so safe values go out twice and are read back. `tune_alive` missing from
  the ELF refuses the run.
- 3.2, code review of the supervisor sent the card back once: safe values from the moment the checks
  of `stream_start()` passed, not only after the trial stream; before that nothing may be written,
  because the ELF is not verified. The end of a run is guarded against exceptions.
- 3.2, a magnitude check on integer columns would miss the most negative value. `Scope` columns are
  float64, so this cannot occur today; the test stays, shown failing with integer columns injected.
- 3.3, new card 3.3b: stage 4 of "Script errors" (`check_idle()`, target probe) is logic in
  `tune_api.py`, because `run_script.py` needs it as well; 4.2 only adds the button. Stage 4 runs with
  the heartbeat alive, because a fault from the end of the last run stays latched until it changes.
- 3.3b, during the probe `limit()` only watches what the short stream carries. Accepted: the motor
  stands, and the write test follows `check_idle()`. `SCRIPTING.md` has to say so.
- 3.3b, the script body runs three times per run (dry run, declarations, real). Accepted, it holds
  only constants and function definitions.
- 3.4, the overshoot and rise time tests of M3 need capture, so they move to card 6.1.
- 3.4, script cursor: `read()` gives the value at the script's own position in the stream and `wait_ms(n)`
  moves it by exactly n, because samples arrive in chunks and the script would lose time otherwise.
  Limits, heartbeat and stall detection stay on the newest sample. Times are right to one poll step
  or one chunk of the port. The same script gives the same `good_starts` everywhere, so the M7e
  comparison of the table allows that in the time column only.
- 3.4, `MdbSerial.close()` ended only `cmd`, so simulators ran on without a parent. It now ends the
  process tree, with a job object against a killed caller.
- 3.4, the PC went to standby during the run, which stops agents; a keep-awake script runs now.
- 6.2, `tools/report.py` moves up from M8, because M7d tests it and section 9 points to it. The
  button follows in 4.3.
- 4.1, the port row and ELF row stay above the tabs, because both tabs use them.
- 4.2, closing the browser during a run: a run nobody can abort must not go on, so after 5 s without
  a connected page the run is aborted and ends in the safe end. A reload within that time keeps it.
- 4.2, the GUI check on `MDB` of the M4 done criterion follows in 4.3, after 5.1 has released `MDB`.
- 4.2, after a run the chart of the Scope tab is emptied, because the read-back of the safe end starts
  short streams. The curves of the run stay in the comparison table of 4.3 and in the run folder.
- 4.3, new card 4.4: the GUI check on `MDB` of the M4 done criterion, after 5.1 and 6.1 are merged,
  because it needs the firmware of 5.1 and `MDB` is in use by 6.1.
- `docs/tooling_diagram.png`, requested by the user: block diagram of the finished tooling, planned
  parts dashed. M7.1 redraws it, so that nothing stays marked as planned that exists by then.
- 6.1, `capture_time_scale` 100 on `MdbSerial`, for `wait_ms()` in capture mode only. Reason: three
  merge runs had the step at 32 to 53 ms instead of 100 ms, a wrong table with status ok. Not proven:
  the slow speed never came back in 4 later `MDB` runs (14.5 to 16.3 wall seconds per target second),
  its cause is unknown, so the factor was not seen doing its job. Kept, because it costs about 190 s
  per run and a wrong table costs more. `time_scale` stays 25, a heartbeat at 100 would come late.
- 6.1, one rule for the row status of `results.csv`, the summary and the GUI table (`row_status`).
- `test_script.py`, the flaky checks came from `SimSerial` (`cbuf.raw` starved all other threads), not
  from the product. Open for M7d: no test reliably guards this fix, nor `_cap_wait` without its abort
  flag, nor the safe end without `_stop_stream`; the last two are covered by other paths of the safe end.
- Rule of the user, test passes: simulator passes are expensive, one green pass accepts a card. Repeats
  of the full `test_gui_mdb.py` pass happen at fixed points only: after a change to abort, heartbeat or
  safe end; at the end of M7e, before M7.1; once before the final report. A failure is diagnosed, not rerun.
- 4.4b, the cause of the slow `MDB` runs was Windows power throttling (EcoQoS), proven and switched off
  in the port (`full_speed()`), so `capture_time_scale` is removed again. The rare events A (stream stop in
  the probe), C (`E401`) and D (page gone during a run) did not reproduce, their causes stay unknown. Their
  end is safe: the safe values were read back. `STALL_MS` and the 5 s rule stay. Retry rule: one retry in
  `test_gui_mdb.py`, only for `E401` or a stream stop in the probe, counted and printed. A difference in a
  result is never retried, so a mismatch of `SIM` and `MDB` in M7e stays a finding.
- 4.4, a run closed by the GUI on `MDB`: the safe value is written at once, the read-back takes about 30 s
  and the GUI waits 8 s, so `run.txt` has no read-back there. On `SIM` the read-back is there (`test_gui.py`).
- M7a, the PLL fix fb0d836 (`_POST2DIV = 1`, FOSC = FPLLO / 2) is confirmed twice: by the clock
  diagram and Table 31-5 of the data sheet, and by a field check of a dsPIC33CK firmware that made the
  same mistake and so ran at FP 16 MHz while its PWM, clocked from FPLLO, still looked right. That
  firmware is not named here.
- M7a, the receive path in the simulator gives the same answers through the real RX interrupt as through
  the mailbox. One RX interrupt per two bytes is simulator behaviour (data sheet page 278). Still not
  proven: anything that needs the chip, listed in `docs/evidence/M7a.md`.
- M7d finding F1, fixed by the supervisor in one line: `tools/report.py` printed the absolute path of its
  output file, so the user's folder and name were on the console. The marker test now scans the whole
  console output; with the old line it fails 7 times. The report file was clean before and after.
- M7b, a capture that cannot hold its sample period returned full, valid looking buffers with wrong
  spacing (32 MIPS: holds to 15 us without load, about 40 us with 60 % load, mean only). Corrected
  decision: a capture carries no time stamps (the load test sent its own time base channel), so the PC
  cannot check the spacing without a protocol change, which the fixed decisions rule out. Instead the PC
  refuses capture periods below a floor (checker, `tune_api`, Scope tab), so the hang at 1 to 2 us cannot
  happen from mini-scope's own tools, and SCRIPTING.md, USER_GUIDE.md and TROUBLESHOOTING.md state the
  measured limits and that a too short period gives wrong spacing without any error. A script can add a
  time base variable as a channel to see the real spacing; SCRIPTING.md shows how. Card M7b-2.
- M7b, under a 60 % control interrupt above mini-scope a 1 ms stream of 4 channels lost 0 frames at 32
  and 100 MIPS; mini-scope used 4.0 % and 1.3 % of the cycles. Captures hold their period down to 50 us
  (32 MIPS) and 100 us (100 MIPS) under that load, 15 us and 5 us without it. Below that a capture looks
  valid with wrong spacing, at 1 to 2 us it never ends and needs a reset: the follow-up card M7b-2 prevents the hang and documents the limits.
  Timing on a real chip is not proven.
- M7e finding F1, cause found (M7e-2): `tune_speed.py` stepped back for only 50 ms and polled every 2 ms, so a
  step started from a speed still moving, and the result depended on how long a write takes (SIM about 0 to
  6 ms, MDB 50 to 150 ms). Fixed in the script (`BACK_MS` 200, `POLL_MS` 1). The model computes the same on both
  ports (replayed in gcc to 1 to 3 counts). After the fix all 20 rows agree within the rule, and `tuning.h` is
  kp 500, ki 150 on both, as before. Decision on the remaining overshoot difference of 1 point in 2 rows: within
  the rule, because overshoot is computed from a sampled peak, so one sample of shift is the same tolerance as one
  poll step of time. The rule now reads: time columns within one poll step or one chunk, values computed from
  sampled peaks within one count step of that sample shift; status and counts exactly.
- M7e findings F2 and F3 (wrong write width caught by one test only, `hw_check` blind to a 32 bit write width):
  gaps, not faults; other tests catch both. Recorded for the user, not fixed.
- M7e finding F4: a dead TX path ends the tools after 108 s in a bare traceback. Fixed in card M7e-3.
- Final planned repeat of `test_gui_mdb.py` before the report (2026-11-03): 45 OK, 0 FAIL, retries 1. The retry
  was event C of 4.4b: `E401`, no ping answer on the first probe, handled by the counted retry rule. It is the
  second time in about 12 GUI sessions on `MDB`; its cause stays unknown, the run ended safely.

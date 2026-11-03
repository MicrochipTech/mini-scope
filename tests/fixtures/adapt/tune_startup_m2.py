# tune_startup.py - finds the startup parameters of a sensorless BLDC drive
#
# Starts the motor several times per ramp rate, from slow to fast, and shows
# which ramp rates reach closed loop. The result is written to tuning.h.
#
# Delivered for the example firmware. To use it with your own firmware, copy
# this file and change sections 1 to 3, see "Adapting a script" in SCRIPTING.md.
# Then press Check in the GUI: it tests every name against your ELF file
# without touching the target.
#
# Python differs from C in a few points, see the first page of SCRIPTING.md:
# blocks are marked by indentation, there are no { } and no semicolons.

from tune_api import *

# ---------- 1. your variables, written as in your C code ----------

VAR_RUN            = "axis[0].ctl.disable"
VAR_STATE          = "axis[0].st.mode"
VAR_SPEED          = "axis[0].st.rpm"
VAR_CURRENT        = "axis[0].st.torque"
VAR_FAULT          = "axis[0].st.flags"
VAR_RAMP_RATE      = "axis[0].su.acc"
VAR_ALIGN_CURRENT  = "axis[0].su.lock_amp"
VAR_HANDOVER_SPEED = "axis[0].su.switch_rpm"


# ---------- 2. how your firmware works ----------

def motor_on():
    write(VAR_RUN, 0)                       # disable = 0 starts


def motor_off():
    write(VAR_RUN, 1)                       # disable = 1 stops


def in_closed_loop():
    return read(VAR_STATE) == 12            # state 12 is closed loop


def has_fault():
    return (read(VAR_FAULT) & 0x03F0) != 0  # bits 4 to 9 are faults, bit 0 is power ok


def stands_still():
    return read(VAR_SPEED) == 0


def declare_limits():
    allow(VAR_RUN, 0, 1)                    # may be written, with hard limits
    allow(VAR_RAMP_RATE, 1, 500)
    allow(VAR_ALIGN_CURRENT, 0, 1000)
    allow(VAR_HANDOVER_SPEED, 100, 2000)
    safe_value(VAR_RUN, 1)                  # written at the end, on abort and on any error
    limit(VAR_CURRENT, 1500)                # above this the whole run is aborted


# Physical value = raw value x factor. Only for the display: read(), write(),
# allow() and limit() keep working with the raw integers of your C variables.
def declare_units():
    unit(VAR_SPEED, 0.5, "rpm")
    unit(VAR_CURRENT, 20.0 / 32768, "A")    # Q15: 32768 = 20 A


# Called by mini-scope before every run: what must be true at standstill.
def check_idle():
    expect(stands_still(), "motor must stand still: check VAR_SPEED")
    expect(not has_fault(), "no fault expected: check has_fault()")
    expect(not in_closed_loop(), "must not be in closed loop: check in_closed_loop()")


# ---------- 3. test settings ----------

RAMP_RATES         = [50, 100, 200, 400]   # array, sorted from slow to fast
ALIGN_CURRENT      = 400
HANDOVER_SPEED     = 600
STARTS_PER_SETTING = 1                     # virtual motor: 1, real drive: 5 or more
CONFIRM_STARTS     = 5                     # the result must start this many times in a row

START_TIMEOUT_MS   = 400
STOP_TIMEOUT_MS    = 300
POLL_MS            = 10
COAST_MS           = 100                   # sensorless: the speed estimate reads 0 while
                                           # the rotor still turns, so allow for run-down


# ---------- 4. the test ----------

# Returns the start time in ms, or -1 if the start failed.
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


# Returns 0 when the motor stands still, -1 on a timeout.
def stop_motor():
    motor_off()

    time_ms = 0
    while time_ms < STOP_TIMEOUT_MS:
        if stands_still():
            wait_ms(COAST_MS)
            return 0
        wait_ms(POLL_MS)
        time_ms += POLL_MS

    return -1


def main():
    channels(VAR_SPEED, VAR_CURRENT, VAR_STATE, VAR_FAULT)      # at most 4, one sample per ms
    declare_limits()
    declare_units()

    summary_channel(VAR_SPEED)
    table("ramp_rate", "good_starts", "time_ms")
    stream_start()

    write(VAR_ALIGN_CURRENT, ALIGN_CURRENT)
    write(VAR_HANDOVER_SPEED, HANDOVER_SPEED)

    fastest = 0                             # fastest ramp that always started
    previous = 0                            # the ramp before it

    for ramp in RAMP_RATES:
        write(VAR_RAMP_RATE, ramp)
        good_starts = 0
        time_ms = -1

        for n in range(STARTS_PER_SETTING):
            trace_begin()
            time_ms = start_motor()
            stopped = stop_motor()
            if time_ms < 0:
                trace_failed()
            trace_end("ramp %d, start %d", ramp, n + 1)

            if stopped != 0:
                printf("Motor does not stop, aborting\n")
                return                      # safe_value() is written after main()

            if time_ms >= 0:
                good_starts += 1

        row(ramp, good_starts, time_ms)
        printf("ramp %d: %d of %d starts good\n", ramp, good_starts, STARTS_PER_SETTING)

        if good_starts == STARTS_PER_SETTING:
            previous = fastest
            fastest = ramp

    if fastest == 0:
        printf("No ramp rate started reliably: more align current or a slower ramp\n")
        summary_text("No ramp rate started reliably")
        return

    # the fastest ramp sits right at the limit, so take one step below it
    result = fastest
    if previous != 0:
        result = previous

    # confirm the result before it is written to tuning.h
    write(VAR_RAMP_RATE, result)
    for n in range(CONFIRM_STARTS):
        trace_begin()
        time_ms = start_motor()
        stopped = stop_motor()
        if time_ms < 0:
            trace_failed()
        trace_end("confirm %d", n + 1)

        if time_ms < 0 or stopped != 0:
            printf("Ramp rate %d failed on confirming start %d\n", result, n + 1)
            summary_text("Ramp rate %d not confirmed", result)
            return

    define("RAMP_RATE", result)
    define("ALIGN_CURRENT", ALIGN_CURRENT)
    define("HANDOVER_SPEED", HANDOVER_SPEED)
    summary_mark(result)
    summary_text("Suggested ramp rate: %d, %d of %d starts confirmed", result,
                 CONFIRM_STARTS, CONFIRM_STARTS)
    printf("Suggested ramp rate: %d, written to tuning.h\n", result)

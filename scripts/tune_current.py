# tune_current.py - measures the step response of the current loop
#
# Runs a current step at standstill for each gain, from low to high, and
# records it in capture mode.
#
# Delivered for the example firmware. To use it with your own firmware, copy
# this file and change sections 1 to 3, see "Adapting a script" in SCRIPTING.md.
# Then press Check in the GUI: it tests every name against your ELF file
# without touching the target.
#
# Why capture: the current loop is too fast for the stream at 115200 baud. A
# capture samples into a RAM buffer on the target and sends it afterwards.
# During a capture, read() does not work, and limit() is only checked after
# the buffer has arrived.
#
# Python differs from C in a few points, see the first page of SCRIPTING.md.
# Note that / is a float division, // is the integer division.

from tune_api import *

# ---------- 1. your variables, written as in your C code ----------

VAR_CURRENT_REF = "motor.current_ref"       # with FOC: the d axis, so the rotor stays put
VAR_CURRENT     = "motor.current"
VAR_KP          = "motor.current_kp"
VAR_KI          = "motor.current_ki"


# ---------- 2. how your firmware works ----------

def set_current(value):
    write(VAR_CURRENT_REF, value)


def declare_limits():
    allow(VAR_CURRENT_REF, 0, 1000)         # may be written, with hard limits
    allow(VAR_KP, 0, 2000)
    allow(VAR_KI, 0, 500)
    safe_value(VAR_CURRENT_REF, 0)          # written at the end, on abort and on any error
    limit(VAR_CURRENT, 1500)                # above this the whole run is aborted


# Physical value = raw value x factor. Only for the display: read(), write(),
# allow() and limit() keep working with the raw integers of your C variables.
def declare_units():
    unit(VAR_CURRENT_REF, 20.0 / 32768, "A")    # Q15: 32768 = 20 A
    unit(VAR_CURRENT, 20.0 / 32768, "A")


# Called by mini-scope before every run: what must be true at standstill.
def check_idle():
    expect(read(VAR_CURRENT_REF) == 0, "current reference must be 0: check VAR_CURRENT_REF")


# ---------- 3. test settings ----------

GAINS_KP           = [100, 200, 400, 800]   # array, sorted from low to high
GAIN_KI            = 20

STEP_CURRENT       = 500                    # raw value of the current reference
STEP_MS            = 100                    # longer than the capture window
SETTLE_MS          = 200                    # pause between two steps

SAMPLE_PERIOD_US   = 100                    # 2 channels: 1024 samples = 102 ms
PRETRIGGER_PERCENT = 10
CAPTURE_TIMEOUT_MS = 500

MAX_OVERSHOOT_PERCENT = 10                  # accepted overshoot
CONFIRM_STEPS      = 3                      # the result must pass this many times in a row


# ---------- 4. the test ----------

# Returns 0 if the capture arrived, -1 on a timeout.
def step_response():
    capture_arm()                           # returns when the pretrigger is full

    set_current(STEP_CURRENT)               # this step fires the trigger
    wait_ms(STEP_MS)
    set_current(0)                          # back to 0 before the transfer

    return capture_wait(CAPTURE_TIMEOUT_MS)


# Returns the overshoot of the last capture in percent of the step.
def overshoot_percent():
    final = capture_final(VAR_CURRENT)
    return (capture_max(VAR_CURRENT) - final) * 100 // STEP_CURRENT


def main():
    capture_channels(VAR_CURRENT_REF, VAR_CURRENT)
    capture_period_us(SAMPLE_PERIOD_US)
    capture_trigger(VAR_CURRENT_REF, RISING, STEP_CURRENT // 2, PRETRIGGER_PERCENT)
    declare_limits()
    declare_units()

    summary_channel(VAR_CURRENT)
    table("kp", "rise_us", "overshoot_percent")
    capture_start()

    write(VAR_KI, GAIN_KI)

    result = 0                              # highest gain that was good enough

    for kp in GAINS_KP:
        write(VAR_KP, kp)
        wait_ms(SETTLE_MS)

        if step_response() != 0:
            printf("kp %d: no trigger, does the firmware use %s?\n", kp, VAR_CURRENT_REF)
            return                          # safe_value() is written after main()

        rise_us = capture_rise_us(VAR_CURRENT)
        overshoot = overshoot_percent()

        if overshoot > MAX_OVERSHOOT_PERCENT or rise_us < 0:
            trace_failed()
        else:
            result = kp

        capture_save("kp %d", kp)
        row(kp, rise_us, overshoot)
        printf("kp %d: rise time %d us, overshoot %d %%\n", kp, rise_us, overshoot)

        wait_ms(SETTLE_MS)

    if result == 0:
        printf("No gain was good enough: lower the start values\n")
        summary_text("No suitable gain found")
        return

    # confirm the result before it is written to tuning.h
    write(VAR_KP, result)
    for n in range(CONFIRM_STEPS):
        wait_ms(SETTLE_MS)

        if step_response() != 0 or overshoot_percent() > MAX_OVERSHOOT_PERCENT:
            printf("kp %d failed on confirming step %d\n", result, n + 1)
            summary_text("kp %d not confirmed", result)
            return

        capture_save("confirm %d", n + 1)

    define("CURRENT_KP", result)
    define("CURRENT_KI", GAIN_KI)
    summary_mark(result)
    summary_text("Suggested kp: %d, %d of %d steps confirmed", result,
                 CONFIRM_STEPS, CONFIRM_STEPS)
    printf("Suggested kp: %d, written to tuning.h\n", result)

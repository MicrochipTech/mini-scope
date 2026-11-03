# tune_speed.py - finds the gains of the speed loop of a BLDC drive
#
# Starts the motor once, then steps the speed reference up and down around the
# base speed for each pair of speed gains (kp, ki), from gentle to aggressive.
# It measures overshoot and settling time of every step and picks the pair that
# overshoots little and settles fastest. The result is written to tuning.h.
#
# The gains belong to the speed loop. On a real drive tune the current loop
# first (tune_current.py): a speed loop on top of a badly tuned current loop
# gives gains that are only right for that one current loop.
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
VAR_KP             = "axis[0].sl.kp"
VAR_KI             = "axis[0].sl.ki"
VAR_REF_OFFSET     = "axis[0].sl.trim"
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


# Speed reference = base speed + this offset, in raw speed units.
def set_speed_offset(offset):
    write(VAR_REF_OFFSET, offset)


def set_gains(kp, ki):
    write(VAR_KP, kp)
    write(VAR_KI, ki)


def declare_limits():
    allow(VAR_RUN, 0, 1)                    # may be written, with hard limits
    allow(VAR_KP, 1, 4000)
    allow(VAR_KI, 0, 1500)
    allow(VAR_REF_OFFSET, -600, 600)
    allow(VAR_RAMP_RATE, 1, 500)
    allow(VAR_ALIGN_CURRENT, 0, 1000)
    allow(VAR_HANDOVER_SPEED, 100, 2000)
    safe_value(VAR_RUN, 1)                  # written at the end, on abort and on any error
    safe_value(VAR_REF_OFFSET, 0)
    limit(VAR_SPEED, 2200)                  # above this the whole run is aborted
    limit(VAR_CURRENT, 1300)


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

ALIGN_CURRENT      = 400                   # startup values that work, see tune_startup.py
HANDOVER_SPEED     = 600
RAMP_RATE          = 100
BASE_SPEED         = 1500                  # the speed the firmware runs at with offset 0
START_KP           = 500                   # gains for the start and the first settling
START_KI           = 30

KP_LIST            = [200, 500, 1000, 1500] # sorted from gentle to aggressive
KI_LIST            = [60, 150, 300]
OFFSETS            = [300, -300]           # operating points: step up, step down
SOFT_OVERSHOOT     = 20                    # percent of the step: ends the ascent, well before limit()
GOOD_OVERSHOOT     = 5                     # percent of the step: a pair up to this counts as equal
SETTLE_BAND        = 2                     # percent of the step
CONFIRM_STEPS      = 3                     # the result must pass this many times in a row

START_TIMEOUT_MS   = 400
STOP_TIMEOUT_MS    = 300
POLL_MS            = 1                     # every sample: a peak between two polls would be missed
HOLD_MS            = 100                   # time allowed for one step to settle
BACK_MS            = 200                   # time at the base speed between two steps: the speed must be back
                                           # before the next step, or the result depends on how long a write takes
START_SETTLE_MS    = 200

# results of the last step and of the last pair, set by measure_step() and run_points()
overshoot = 0                              # percent of the step, 0 if it did not overshoot
settle_ms = 0                              # HOLD_MS if it did not settle
worst_settle_ms = 0


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
            return 0
        wait_ms(POLL_MS)
        time_ms += POLL_MS

    return -1


# Steps the reference to the offset, watches the speed for HOLD_MS, steps back
# and waits BACK_MS. Sets overshoot and settle_ms.
def measure_step(offset):
    global overshoot
    global settle_ms

    target = BASE_SPEED + offset
    size = offset
    direction = 1
    if offset < 0:
        size = -offset
        direction = -1
    band = size * SETTLE_BAND // 100
    peak = 0
    last_outside = 0

    set_speed_offset(offset)
    time_ms = 0
    while time_ms < HOLD_MS:
        beyond = (read(VAR_SPEED) - target) * direction     # positive: past the target
        if beyond > peak:
            peak = beyond
        if beyond > band or beyond < -band:
            last_outside = time_ms
        wait_ms(POLL_MS)
        time_ms += POLL_MS

    overshoot = peak * 100 // size
    settle_ms = last_outside + POLL_MS
    if settle_ms > HOLD_MS:
        settle_ms = HOLD_MS

    set_speed_offset(0)
    wait_ms(BACK_MS)


# A pair that overshoots up to GOOD_OVERSHOOT is good, and among good pairs the
# fastest settling wins. If no pair is good yet, the smaller overshoot wins.
def is_better(overshoot_a, settle_a, overshoot_b, settle_b):
    if settle_b < 0:
        return True                             # nothing to compare with yet
    a_good = overshoot_a <= GOOD_OVERSHOOT
    b_good = overshoot_b <= GOOD_OVERSHOOT
    if a_good and b_good:
        return settle_a < settle_b
    if a_good:
        return True
    if b_good:
        return False
    return overshoot_a < overshoot_b


# Runs every operating point with the gains that are set. Returns the worst
# overshoot, the worst settling time is left in worst_settle_ms.
def run_points(kp, ki, with_rows):
    global worst_settle_ms

    worst_overshoot = 0
    worst_settle_ms = 0
    for offset in OFFSETS:
        trace_begin()
        measure_step(offset)
        trace_end("kp %d, ki %d, offset %d", kp, ki, offset)
        if with_rows:
            row(kp, ki, offset, overshoot, settle_ms)
        if overshoot > worst_overshoot:
            worst_overshoot = overshoot
        if settle_ms > worst_settle_ms:
            worst_settle_ms = settle_ms
    return worst_overshoot


def main():
    channels(VAR_SPEED, VAR_CURRENT, VAR_STATE, VAR_FAULT)      # at most 4, one sample per ms
    declare_limits()
    declare_units()

    summary_channel(VAR_SPEED)
    table("kp", "ki", "offset", "overshoot_pct", "settle_ms")
    stream_start()

    write(VAR_ALIGN_CURRENT, ALIGN_CURRENT)
    write(VAR_HANDOVER_SPEED, HANDOVER_SPEED)
    write(VAR_RAMP_RATE, RAMP_RATE)
    set_gains(START_KP, START_KI)
    set_speed_offset(0)

    if start_motor() < 0:
        printf("Motor does not start: run tune_startup.py first\n")
        summary_text("Motor did not start")
        return
    wait_ms(START_SETTLE_MS)

    best_kp = 0
    best_ki = 0
    best_overshoot = 0
    best_settle = -1                        # -1: no pair measured yet
    done = 0

    for kp in KP_LIST:
        if done:
            break
        for ki in KI_LIST:
            set_gains(kp, ki)
            wait_ms(BACK_MS)
            worst_overshoot = run_points(kp, ki, 1)
            printf("kp %d, ki %d: worst overshoot %d %%, worst settling %d ms\n",
                   kp, ki, worst_overshoot, worst_settle_ms)

            if is_better(worst_overshoot, worst_settle_ms, best_overshoot, best_settle):
                best_kp = kp
                best_ki = ki
                best_overshoot = worst_overshoot
                best_settle = worst_settle_ms

            if worst_overshoot > SOFT_OVERSHOOT:
                if ki == KI_LIST[0]:
                    printf("kp %d overshoots %d %% already with the smallest ki: ending the grid\n",
                           kp, worst_overshoot)
                    done = 1
                else:
                    printf("kp %d: overshoot %d %% above %d %% at ki %d, no larger ki\n",
                           kp, worst_overshoot, SOFT_OVERSHOOT, ki)
                break

    if best_overshoot > GOOD_OVERSHOOT:
        printf("No pair overshoots less than %d %%: smaller gains or a better current loop\n",
               GOOD_OVERSHOOT)
        summary_text("No pair within %d %% overshoot", GOOD_OVERSHOOT)
        return

    # confirm the result before it is written to tuning.h
    set_gains(best_kp, best_ki)
    wait_ms(BACK_MS)
    for n in range(CONFIRM_STEPS):
        worst_overshoot = run_points(best_kp, best_ki, 0)
        if worst_overshoot > GOOD_OVERSHOOT or worst_settle_ms >= HOLD_MS:
            printf("kp %d, ki %d failed on confirming step %d: overshoot %d %%, settling %d ms\n",
                   best_kp, best_ki, n + 1, worst_overshoot, worst_settle_ms)
            summary_text("kp %d, ki %d not confirmed", best_kp, best_ki)
            return

    if stop_motor() != 0:
        printf("Motor does not stop, aborting\n")
        return                              # safe_value() is written after main()

    define("SPEED_KP", best_kp)
    define("SPEED_KI", best_ki)
    summary_mark(best_kp)
    summary_text("Suggested gains: kp %d, ki %d, %d of %d steps confirmed", best_kp, best_ki,
                 CONFIRM_STEPS, CONFIRM_STEPS)
    printf("Suggested gains: kp %d, ki %d, written to tuning.h\n", best_kp, best_ki)

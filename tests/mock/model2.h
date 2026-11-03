/*
 * model2.h - a second virtual drive for tests/test_adapt.py: other names, other structs, inverted logic
 *
 * The control dynamics are those of tune_model.h, which runs hidden behind this interface at 0x1100
 * (struct motor and startup of sim_b.c, not visible to a script). What a script sees is this struct:
 *
 *   axis[0].ctl.disable    1 stops, 0 runs (the example firmware: run = 1 starts)
 *   axis[0].st.mode        7 idle, 8 locate, 9 spin-up, 12 run (example: 0, 1, 2, 3)
 *   axis[0].st.rpm         speed, axis[0].st.rpm_cmd the reference in use, axis[0].st.torque the current
 *   axis[0].st.flags       bit 0 is always set (power ok), bits 4 to 9 are faults (example: fault code)
 *   axis[0].sl             speed loop: kp, ki, trim (example: motor.kp, ki, ref_offset)
 *   axis[0].cl             current loop: ref, act, kp, ki
 *   axis[0].su             start-up: acc, lock_amp, switch_rpm
 *
 * Fault variants for the tests, set with -D at the gcc call:
 *   M2_OVERWRITE   the firmware writes su.acc back to 100 every pass
 *   M2_REVERT      the firmware writes su.acc back to 100 when it differed for 30 ms
 *   M2_LATCH       su.lock_amp can only grow
 *   M2_STUCK_FAULT a fault bit in st.flags that never clears
 */
#ifndef MODEL2_H
#define MODEL2_H

struct m2_axis {
    struct { uint16_t disable; } ctl;
    struct { uint16_t mode; int16_t rpm, rpm_cmd, torque; uint16_t flags; } st;
    struct { int16_t kp, ki, trim; } sl;
    struct { int16_t ref, act, kp, ki; } cl;
    struct { uint16_t acc, lock_amp, switch_rpm; } su;
};

#define axis    ((volatile struct m2_axis *)&mock_ram[0x1400])

#define M2_IDLE     7
#define M2_LOCATE   8
#define M2_SPINUP   9
#define M2_RUN      12
#define M2_OK       0x0001u

#ifdef M2_LATCH
static uint16_t m2_latch;
#endif
#ifdef M2_REVERT
static uint16_t m2_diff;
#endif

static void m2_init(void)
{
    axis[0].ctl.disable = 1;
    axis[0].st.mode = M2_IDLE;
    axis[0].st.flags = M2_OK;
    axis[0].sl.kp = 300;
    axis[0].sl.ki = 60;
    axis[0].cl.kp = 200;
    axis[0].cl.ki = 20;
    axis[0].su.acc = 100;
    axis[0].su.lock_amp = 400;
    axis[0].su.switch_rpm = 600;
#ifdef M2_LATCH
    m2_latch = 0;
#endif
}

/* one 100 us pass: interface in, the model, interface out */
static void m2_step(void)
{
    static const uint16_t mode[4] = { M2_IDLE, M2_LOCATE, M2_SPINUP, M2_RUN };

    motor.run = !axis[0].ctl.disable;
    motor.kp = axis[0].sl.kp;
    motor.ki = axis[0].sl.ki;
    motor.ref_offset = axis[0].sl.trim;
    motor.current_ref = axis[0].cl.ref;
    motor.current_kp = axis[0].cl.kp;
    motor.current_ki = axis[0].cl.ki;
    startup.ramp_rate = axis[0].su.acc;
    startup.align_current = axis[0].su.lock_amp;
    startup.handover_speed = axis[0].su.switch_rpm;

    tune_model_step();

    axis[0].st.mode = mode[motor.state & 3];
    axis[0].st.rpm = motor.speed;
    axis[0].st.rpm_cmd = motor.speed_ref;
    axis[0].st.torque = motor.iq;
    axis[0].cl.act = motor.current;
    axis[0].st.flags = (uint16_t)(M2_OK | (motor.fault ? 1u << (3 + motor.fault) : 0u));
    if (tune_lost) {                    /* the model stopped on a lost heartbeat */
        axis[0].ctl.disable = 1;
        axis[0].cl.ref = 0;
    }
#ifdef M2_OVERWRITE
    axis[0].su.acc = 100;
#endif
#ifdef M2_REVERT
    m2_diff = axis[0].su.acc != 100 ? m2_diff + 1 : 0;
    if (m2_diff >= 300) {
        axis[0].su.acc = 100;
        m2_diff = 0;
    }
#endif
#ifdef M2_LATCH
    if (axis[0].su.lock_amp < m2_latch)
        axis[0].su.lock_amp = m2_latch;
    m2_latch = axis[0].su.lock_amp;
#endif
#ifdef M2_STUCK_FAULT
    axis[0].st.flags |= 0x0200u;
#endif
}

#endif

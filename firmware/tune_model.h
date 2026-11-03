/*
 * tune_model.h - virtual motor for the tuning scripts, fixed point, no hardware
 *
 * Call tune_model_step() every 100 us (10 kHz). It runs the current loop on
 * every call and the rest (start sequence, speed loop, heartbeat) on every
 * tenth call, so 1 ms.
 *
 * The header only uses names. The file that includes it defines, before the
 * include:
 *
 *   motor        struct: run, state, speed, speed_ref, iq, fault, kp, ki,
 *                ref_offset, current_ref, current, current_kp, current_ki
 *   startup      struct: ramp_rate, align_current, handover_speed
 *
 * and tune_block.h (included here) provides the heartbeat tune_alive and
 * tune_block_ms(). example_main.c defines the structs as volatile variables,
 * tests/mock/sim.c maps them to mock_ram. Units: speed 0.5 rpm per count,
 * current Q15 with 32768 = 20 A. Integer maths only with explicit widths: int is 16 bit with
 * XC-DSC and 32 bit with gcc, so every product is cast to int32_t first and
 * every narrowing cast follows a clamp. >> on a negative value is an
 * arithmetic shift with both compilers.
 *
 * What the model does:
 *  - The current loop (PI, two samples dead time, first-order plant) follows
 *    motor.current_ref at any time, also at standstill. A high current_kp
 *    overshoots, a very high one oscillates and clips.
 *  - run = 1 at standstill starts: align (100 ms, needs align_current of at
 *    least 300), open-loop ramp with ramp_rate (counts per 10 ms), handover
 *    to the speed loop at handover_speed. The ramp needs torque: a ramp too
 *    fast for align_current loses synchronism. A handover below 300 fails,
 *    the position estimate does not work there.
 *  - Closed loop: PI speed controller (kp, ki) on 1500 + ref_offset, the iq
 *    output is limited, the plant is a first-order mechanical model with dead
 *    time. High gains oscillate, above 2500 counts the model sets fault.
 *  - fault is latched and cleared when run goes to 0.
 *  - Heartbeat: armed when tune_alive first changes. If it stays unchanged
 *    for 1 s the model stops, writes run = 0 and current_ref = 0 and sets
 *    fault until tune_alive changes again. Other writes do not arm it.
 */
#ifndef TUNE_MODEL_H
#define TUNE_MODEL_H

#include <stdint.h>
#include "tune_block.h"

/* motor.state */
#define TUNE_STOPPED        0
#define TUNE_ALIGN          1
#define TUNE_RAMP           2
#define TUNE_CLOSED         3

/* motor.fault */
#define TUNE_FAULT_OVERSPEED    1
#define TUNE_FAULT_SYNC         2       /* ramp too fast for the torque */
#define TUNE_FAULT_ALIGN        3       /* align current too low */
#define TUNE_FAULT_HANDOVER     4       /* handover speed too low */
#define TUNE_FAULT_HEARTBEAT    5

/* model constants */
#define TUNE_ALIGN_MS           100
#define TUNE_ALIGN_MIN          300     /* least align_current that turns the rotor */
#define TUNE_HANDOVER_MIN       300     /* lowest speed the position estimate works at */
#define TUNE_SPEED_TARGET       1500    /* closed loop reference before ref_offset */
#define TUNE_OVERSPEED          2500
#define TUNE_IQ_LIMIT           1400
#define TUNE_VMAX               8000    /* voltage limit of the current loop */

static int32_t  tune_ci;                /* current loop integrator */
static int16_t  tune_cu1, tune_cu2;     /* current loop output, delayed by one and two samples */
static uint8_t  tune_div;               /* 10 kHz passes since the last 1 ms pass */
static uint16_t tune_t;                 /* ms in the current state */
static int32_t  tune_ramp;              /* ramp reference, 1/32768 counts */
static int32_t  tune_spd;               /* speed, 1/16 counts */
static int32_t  tune_si;                /* speed loop integrator */
static int16_t  tune_iq1, tune_iq2;     /* iq delayed by one and two ms */
static uint8_t  tune_lost;              /* heartbeat lost, from tune_block_ms() */

static int32_t tune_clamp(int32_t v, int32_t lim)
{
    if (v > lim)
        return lim;
    if (v < -lim)
        return -lim;
    return v;
}

/* current loop, 100 us */
static void tune_fast(void)
{
    int32_t ref = tune_lost ? 0 : motor.current_ref;
    int32_t cur = motor.current;
    int32_t e = ref - cur;
    int32_t u;

    tune_ci = tune_clamp(tune_ci + (int32_t)motor.current_ki * e, (int32_t)TUNE_VMAX << 10);
    u = tune_clamp((((int32_t)motor.current_kp * e) >> 8) + (tune_ci >> 10), TUNE_VMAX);
    cur += (((int32_t)tune_cu2 * 4 - cur) * 1100) >> 15;
    motor.current = (int16_t)tune_clamp(cur, 30000);
    tune_cu2 = tune_cu1;
    tune_cu1 = (int16_t)u;
}

/* run-down: the speed falls without drive, 8 counts per ms plus 1/16 of it */
static void tune_coast(void)
{
    tune_spd -= (tune_spd >> 4) + 128;
    if (tune_spd < 0)
        tune_spd = 0;
    motor.speed = (int16_t)(tune_spd >> 4);
}

static void tune_to_stopped(uint16_t fault)
{
    motor.state = TUNE_STOPPED;
    motor.iq = 0;
    motor.speed_ref = 0;
    tune_iq1 = tune_iq2 = 0;
    tune_spd = (int32_t)motor.speed << 4;
    if (fault)
        motor.fault = fault;
}

/* start sequence, speed loop and heartbeat, 1 ms */
static void tune_slow(void)
{
    int32_t err, iq, ref;
    int32_t ac = tune_clamp(startup.align_current, TUNE_IQ_LIMIT);
    uint8_t lost = tune_block_ms();

    if (lost && !tune_lost) {           /* the heartbeat ended just now: stop once */
        motor.run = 0;
        motor.current_ref = 0;
        tune_to_stopped(TUNE_FAULT_HEARTBEAT);
    }
    tune_lost = lost;

    if (!motor.run && !tune_lost)
        motor.fault = 0;

    switch (motor.state) {
    case TUNE_STOPPED:
        tune_coast();
        if (motor.run && !motor.fault && motor.speed == 0) {
            motor.state = TUNE_ALIGN;
            tune_t = 0;
        }
        break;

    case TUNE_ALIGN:
        if (!motor.run) {
            tune_to_stopped(0);
            break;
        }
        motor.iq = (int16_t)ac;
        if (++tune_t >= TUNE_ALIGN_MS) {
            if (ac < TUNE_ALIGN_MIN) {
                tune_to_stopped(TUNE_FAULT_ALIGN);
            } else {
                motor.state = TUNE_RAMP;
                tune_ramp = 0;
            }
        }
        break;

    case TUNE_RAMP: {
        /* torque left for the ramp: 0.8 counts/ms per count of align current,
         * needed: the ramp itself plus friction of 1/64 of the speed */
        int32_t need, have;

        if (!motor.run) {
            tune_to_stopped(0);
            break;
        }
        tune_ramp += (int32_t)startup.ramp_rate * 3277;     /* ramp_rate / 10 per ms, in 1/32768 */
        motor.speed = (int16_t)(tune_ramp >> 15);
        motor.speed_ref = motor.speed;
        need = (int32_t)startup.ramp_rate + (((int32_t)motor.speed * 10) >> 6);
        have = (ac * 819) >> 10;                        /* 0.8 * ac */
        if (need > have) {
            tune_to_stopped(TUNE_FAULT_SYNC);
            break;
        }
        if (motor.speed >= (int16_t)startup.handover_speed) {
            if (motor.speed < TUNE_HANDOVER_MIN) {
                tune_to_stopped(TUNE_FAULT_HANDOVER);
                break;
            }
            /* the speed loop starts from zero torque, the ramp current is not carried over */
            tune_si = 0;
            tune_iq1 = tune_iq2 = 0;
            tune_spd = (int32_t)motor.speed << 4;
            motor.state = TUNE_CLOSED;
        }
        break;
    }

    case TUNE_CLOSED:
        if (!motor.run) {
            tune_to_stopped(0);
            break;
        }
        ref = (int32_t)TUNE_SPEED_TARGET + motor.ref_offset;
        if (ref < 0)
            ref = 0;
        motor.speed_ref = (int16_t)tune_clamp(ref, 30000);
        err = ref - motor.speed;
        tune_si = tune_clamp(tune_si + (int32_t)motor.ki * err, (int32_t)TUNE_IQ_LIMIT * 16384);
        iq = tune_clamp((((int32_t)motor.kp * err) >> 10) + (tune_si >> 14), TUNE_IQ_LIMIT);
        motor.iq = (int16_t)iq;
        /* plant: acceleration = iq / 4 counts per ms (delayed by 2 ms), friction 1/64 of the speed */
        tune_spd += (int32_t)tune_iq2 * 4 - (tune_spd >> 6);
        tune_iq2 = tune_iq1;
        tune_iq1 = (int16_t)iq;
        tune_spd = tune_clamp(tune_spd, 64000L);          /* 4000 counts, keeps motor.speed in int16_t */
        if (tune_spd < 0)
            tune_spd = 0;
        motor.speed = (int16_t)(tune_spd >> 4);
        if (motor.speed > TUNE_OVERSPEED)
            tune_to_stopped(TUNE_FAULT_OVERSPEED);
        break;
    }
}

/* one 100 us step */
static void tune_model_step(void)
{
    tune_fast();
    if (++tune_div >= 10) {
        tune_div = 0;
        tune_slow();
    }
}

#endif

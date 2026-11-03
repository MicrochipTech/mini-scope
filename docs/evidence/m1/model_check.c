/*
 * model_check.c - host check of firmware/tune_model.h
 *
 *   gcc -O2 -Wall -Ifirmware -o model_check docs/evidence/m1/model_check.c && ./model_check
 *
 * The structs repeat the ones in firmware/example_main.c. Prints the numbers the
 * delivered example scripts rely on, from simulated runs, 100 us per step.
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>

struct tune_motor {
    uint16_t run;
    uint16_t state;
    int16_t  speed;
    int16_t  speed_ref;
    int16_t  iq;
    uint16_t fault;
    int16_t  kp;
    int16_t  ki;
    int16_t  ref_offset;
    int16_t  current_ref;
    int16_t  current;
    int16_t  current_kp;
    int16_t  current_ki;
};
struct tune_startup {
    uint16_t ramp_rate;
    uint16_t align_current;
    uint16_t handover_speed;
};
static struct tune_motor motor;
static struct tune_startup startup;

#include "tune_model.h"

#define DEF_KP  300
#define DEF_KI  60

static void reset_model(void)
{
    memset(&motor, 0, sizeof motor);
    memset(&startup, 0, sizeof startup);
    tune_alive = 0;
    tune_ci = 0; tune_cu1 = tune_cu2 = 0; tune_div = 0; tune_t = 0; tune_ramp = 0;
    tune_spd = 0; tune_si = 0; tune_iq1 = tune_iq2 = 0; tune_lost = 0;
    tune_block_init();
    motor.kp = DEF_KP; motor.ki = DEF_KI;
    motor.current_kp = 200; motor.current_ki = 20;
    startup.ramp_rate = 100; startup.align_current = 400; startup.handover_speed = 600;
}

static void run_ms(int ms)
{
    for (int i = 0; i < ms * 10; i++)
        tune_model_step();
}

struct start_result {
    int t_align, t_ramp, t_closed;      /* ms from run = 1, -1 if never */
    int fault, fault_ms;
    int max_iq, t_stop;
    int final_state;
};

/* run = 1, wait up to 400 ms like START_TIMEOUT_MS, then run = 0, wait for speed 0 */
static struct start_result do_start(int trace)
{
    struct start_result r = {-1, -1, -1, 0, -1, 0, -1, 0};
    int prev = 0;

    motor.run = 1;
    for (int ms = 0; ms < 400; ms++) {
        run_ms(1);
        if (motor.state != prev) {
            if (trace)
                printf("  t=%3d ms state %d -> %d speed %d iq %d fault %d\n", ms + 1, prev, motor.state,
                       motor.speed, motor.iq, motor.fault);
            if (motor.state == TUNE_ALIGN && r.t_align < 0) r.t_align = ms + 1;
            if (motor.state == TUNE_RAMP && r.t_ramp < 0) r.t_ramp = ms + 1;
            if (motor.state == TUNE_CLOSED && r.t_closed < 0) r.t_closed = ms + 1;
            prev = motor.state;
        }
        if (motor.iq > r.max_iq) r.max_iq = motor.iq;
        if (motor.fault && r.fault_ms < 0) { r.fault = motor.fault; r.fault_ms = ms + 1; }
        if (motor.fault || motor.state == TUNE_CLOSED)
            break;
    }
    r.final_state = motor.state;
    motor.run = 0;
    for (int ms = 0; ms < 1000; ms++) {
        run_ms(1);
        if (motor.speed == 0 && motor.fault == 0) { r.t_stop = ms + 1; break; }
    }
    return r;
}

static void startup_table(void)
{
    int rates[] = {50, 100, 200, 400};
    int hv[] = {150, 250, 299, 300, 600, 2000};
    int al[] = {100, 250, 299, 300, 350, 400, 1000};
    int i, rate;

    printf("== tune_startup.py: align 400, handover 600\n");
    printf("rate  t_align t_ramp t_closed fault@ms  max_iq stop_ms\n");
    for (i = 0; i < 4; i++) {
        struct start_result r;
        reset_model();
        startup.ramp_rate = rates[i];
        r = do_start(0);
        printf("%4d  %5d %6d %7d   %d@%d   %6d %6d\n", rates[i], r.t_align, r.t_ramp, r.t_closed,
               r.fault, r.fault_ms, r.max_iq, r.t_stop);
    }
    printf("== other handover speeds (rate 100)\n");
    for (i = 0; i < 6; i++) {
        struct start_result r;
        reset_model();
        startup.handover_speed = hv[i];
        r = do_start(0);
        printf("handover %4d: closed %4d ms fault %d@%d\n", hv[i], r.t_closed, r.fault, r.fault_ms);
    }
    printf("== other align currents (rate 100)\n");
    for (i = 0; i < 7; i++) {
        struct start_result r;
        reset_model();
        startup.align_current = al[i];
        r = do_start(0);
        printf("align %4d: closed %4d ms fault %d@%d max_iq %d\n", al[i], r.t_closed, r.fault, r.fault_ms, r.max_iq);
    }
    printf("== run-down from closed loop at cruise speed\n");
    {
        int ms, mxs, stop = -1;
        reset_model();
        motor.run = 1;
        run_ms(700);
        mxs = motor.speed;
        motor.run = 0;
        for (ms = 1; ms <= 1000 && stop < 0; ms++) {
            run_ms(1);
            if (motor.speed == 0) stop = ms;
        }
        printf("speed %d, run = 0: standstill after %d ms, fault %d\n", mxs, stop, motor.fault);
    }
    printf("== rate sweep with align 400 (sync limit)\n");
    for (rate = 150; rate <= 400; rate += 50) {
        struct start_result r;
        reset_model();
        startup.ramp_rate = rate;
        r = do_start(0);
        printf("rate %3d: closed %4d ms fault %d\n", rate, r.t_closed, r.fault);
    }
}

static void determinism(void)
{
    int rates[] = {100, 400, 100, 400, 100, 100, 100};
    struct start_result first;
    int ok = 1, i;

    memset(&first, 0, sizeof first);
    printf("== determinism: starts 100, 400, 100, 400, 100, 100, 100 in one model run\n");
    reset_model();
    for (i = 0; i < 7; i++) {
        struct start_result r;
        startup.ramp_rate = rates[i];
        r = do_start(0);
        printf("start %d rate %3d: closed %d ms fault %d max_iq %d stop %d\n", i, rates[i], r.t_closed,
               r.fault, r.max_iq, r.t_stop);
        if (i == 0) first = r;
        if (rates[i] == 100 && memcmp(&r, &first, sizeof r)) ok = 0;
    }
    printf("good starts identical: %s\n", ok ? "yes" : "NO");
}

/* emulates the capture of tune_current.py: 1024 samples at 100 us, 10 % pretrigger */
static void current_table(void)
{
    int kps[] = {100, 200, 400, 800};
    int i, n;

    printf("== tune_current.py: ki 20, step 500, 1024 samples at 100 us\n");
    printf("kp    rise_us overshoot%% max   final  settle_us(2%%)\n");
    for (i = 0; i < 4; i++) {
        static int16_t buf[1024];
        int fin, mx = -32768, t10 = -1, t90 = -1, settle = 0;

        reset_model();
        motor.current_kp = kps[i];
        motor.current_ki = 20;
        run_ms(200);                                     /* SETTLE_MS */
        for (n = 0; n < 102; n++) {
            buf[n] = motor.current;
            tune_model_step();
        }
        motor.current_ref = 500;
        for (n = 102; n < 1024; n++) {
            buf[n] = motor.current;
            tune_model_step();
        }
        fin = buf[1023];
        for (n = 102; n < 1024; n++) {
            int d = buf[n] - fin;
            if (buf[n] > mx) mx = buf[n];
            if (t10 < 0 && buf[n] >= fin / 10) t10 = n;
            if (t90 < 0 && buf[n] >= fin * 9 / 10) t90 = n;
            if (d < 0) d = -d;
            if (d * 50 > fin) settle = n - 102;
        }
        printf("%4d  %6d %9d %6d %5d %6d\n", kps[i], (t90 - t10) * 100, (mx - fin) * 100 / 500, mx, fin,
               (settle + 1) * 100);
    }
}

/* speed loop: step of ref_offset from 0 to 500 in closed loop at the target speed */
static void speed_table(void)
{
    int kps[] = {50, 100, 200, 300, 500, 1000, 1500, 2000, 4000};
    int kis[] = {10, 30, 60, 120, 300, 1000};
    int i, j;

    printf("== speed loop: ref_offset 0 -> 500 at 1500 counts (0.5 rpm), 600 ms\n");
    printf("overshoot%% of the step, sign changes of (speed-2000) after 300 ms, peak iq\n");
    printf("kp\\ki ");
    for (j = 0; j < 6; j++)
        printf("%18d", kis[j]);
    printf("\n");
    for (i = 0; i < 9; i++) {
        printf("%5d ", kps[i]);
        for (j = 0; j < 6; j++) {
            int t = 0, ms, mx = 0, last = 0, ncross = 0, maxiq = 0;

            reset_model();
            motor.kp = kps[i];
            motor.ki = kis[j];
            tune_alive = 1;
            tune_model_step();                          /* arms the heartbeat */
            motor.run = 1;
            while (t < 600 && motor.state != TUNE_CLOSED && !motor.fault) {
                run_ms(1);
                t++;
                tune_alive++;
            }
            if (motor.fault) {
                printf("  start fault %-6d", motor.fault);
                continue;
            }
            for (ms = 0; ms < 300 && !motor.fault; ms++) {
                run_ms(1);
                tune_alive++;
            }
            motor.ref_offset = 500;
            for (ms = 0; ms < 600 && !motor.fault; ms++) {
                int s;
                run_ms(1);
                tune_alive++;
                if (motor.speed > mx) mx = motor.speed;
                if (motor.iq > maxiq) maxiq = motor.iq;
                s = motor.speed > 2000 ? 1 : -1;
                if (ms > 300 && s != last) ncross++;
                last = s;
            }
            if (motor.fault)
                printf("  fault %d at %-6d  ", motor.fault, mx);
            else
                printf("  os%4d x%-2d iq%-5d ", (mx - 2000) * 100 / 500, ncross, maxiq);
        }
        printf("\n");
    }
}

static void heartbeat(void)
{
    printf("== heartbeat\n");
    reset_model();
    motor.run = 1;
    run_ms(3000);
    printf("never armed, 3 s: state %d fault %d\n", motor.state, motor.fault);
    reset_model();
    motor.run = 1;
    tune_alive = 5;
    run_ms(999);
    printf("armed, 999 ms: fault %d run %d\n", motor.fault, motor.run);
    run_ms(2);
    printf("armed, 1001 ms: fault %d run %d state %d\n", motor.fault, motor.run, motor.state);
    tune_alive = 6;
    run_ms(5);
    printf("changed again: fault %d run %d\n", motor.fault, motor.run);
    reset_model();
    tune_alive = 1;
    run_ms(10);
    motor.current_ref = 500;
    run_ms(1100);
    printf("standstill step, heartbeat lost: current_ref %d current %d fault %d\n", motor.current_ref,
           motor.current, motor.fault);
}

int main(void)
{
    reset_model();
    printf("== one start in detail (rate 100)\n");
    do_start(1);
    startup_table();
    determinism();
    current_table();
    speed_table();
    heartbeat();
    return 0;
}

/*
 * model_dll.c - firmware/tune_model.h alone, stepped in whole ms, for replay.py
 * Build: gcc -O2 -shared -Ifirmware -o docs/evidence/m7e2/model.dll docs/evidence/m7e2/model_dll.c
 */
#include <stdint.h>

#define EXPORT __declspec(dllexport)

struct tune_motor {
    uint16_t run, state;
    int16_t  speed, speed_ref, iq;
    uint16_t fault;
    int16_t  kp, ki, ref_offset, current_ref, current, current_kp, current_ki;
};

struct tune_startup {
    uint16_t ramp_rate, align_current, handover_speed;
};

volatile struct tune_motor motor;
volatile struct tune_startup startup;

#include "tune_model.h"

EXPORT void m_init(void)
{
    struct tune_motor z = { 0 };
    motor = z;
    motor.kp = 300;
    motor.ki = 60;
    motor.current_kp = 200;
    motor.current_ki = 20;
    startup.ramp_rate = 100;
    startup.align_current = 400;
    startup.handover_speed = 600;
    tune_ci = tune_cu1 = tune_cu2 = tune_div = tune_t = tune_ramp = tune_spd = tune_si = 0;
    tune_iq1 = tune_iq2 = tune_lost = 0;
    tune_block_init();
}

/* one ms: ten model steps, the heartbeat kept alive */
EXPORT void m_ms(void)
{
    int k;
    tune_alive++;
    for (k = 0; k < 10; k++)
        tune_model_step();
}

EXPORT volatile int16_t *m_motor(void)
{
    return (volatile int16_t *)&motor;
}

EXPORT volatile uint16_t *m_startup(void)
{
    return (volatile uint16_t *)&startup;
}

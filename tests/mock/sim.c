/*
 * sim.c - runs the real scope.c on the PC (DLL for sim_serial.py)
 * Build: gcc -O2 -shared -Itests/mock -Ifirmware -o sim.dll tests/mock/sim.c firmware/scope.c
 *
 * Demo variables in mock RAM (same names as example_main.c):
 *   0x1000 sine (int16)  0x1002 ramp (uint16)  0x1004 counter (int32)  0x1008 toggle (uint8)
 *   0x100A sine_gain (int16, Q15)  0x100C ramp_step (uint16)   written from the GUI
 * Virtual motor (tune_model.h), same members and order as example_main.c:
 *   0x1100 motor (13 x 16 bit)  0x1120 startup (3 x 16 bit)  0x1128 tune_alive (uint16)
 *   0x112A tune_build_id (uint16)   the text tune_build is the const string of tune_block.h,
 *   sim_build() gives it to sim_serial.py
 * The model steps on a fixed 100 us tick of the virtual time, Timer1 events fall in between.
 */
#include <math.h>
#include <string.h>
#include "xc.h"
#include "scope.h"

#define EXPORT __declspec(dllexport)

#define TUNE_MODEL_US   100.0           /* one tune_model_step() */

struct tune_motor {
    uint16_t run, state;
    int16_t  speed, speed_ref, iq;
    uint16_t fault;
    int16_t  kp, ki, ref_offset, current_ref, current, current_kp, current_ki;
};

struct tune_startup {
    uint16_t ramp_rate, align_current, handover_speed;
};

#define motor       (*(volatile struct tune_motor *)&mock_ram[0x1100])
#define startup     (*(volatile struct tune_startup *)&mock_ram[0x1120])
#define tune_alive  (*(volatile uint16_t *)&mock_ram[0x1128])
#define tune_build_id   (*(volatile uint16_t *)&mock_ram[0x112A])

#include "tune_model.h"

void _U1TXInterrupt(void);
void _U1RXInterrupt(void);
void _T1Interrupt(void);

sta_t   U1STAbits;
mode_t_ U1MODEbits;
t1con_t T1CONbits;
uint16_t U1MODE, U1MODEH, U1STA, U1STAH, U1BRG, U1BRGH, T1CON, TMR1, PR1;
volatile uint16_t TRISB, TRISC, ANSELB, ANSELC;
int _U1TXIF, _U1TXIE, _U1RXIF, _U1RXIE, _U1TXIP, _U1RXIP, _T1IF, _T1IE, _T1IP, _GIE;
uint8_t mock_ram[0x10000];

static stah_t stah;
static const uint8_t *rx_data;
static int rx_len, rx_pos;
static uint16_t out[1 << 20];           /* every U1TXREG write lands here */
static int out_len, out_max, tx_budget;
static double phase;
static double vt_us, model_next_us, timer_next_us;   /* virtual time and the next events */
static int timer_armed;

stah_t *mock_stah(void)
{
    stah.URXBE = rx_pos >= rx_len;
    stah.UTXBF = tx_budget <= 0 || out_len >= out_max;
    return &stah;
}

uint16_t *mock_txreg(void)
{
    tx_budget--;
    return &out[out_len++];
}

uint16_t mock_rxreg(void)
{
    return rx_data[rx_pos++];
}

static int resets;

void mock_reset(void)
{
    resets++;                           /* the real target restarts here */
}

EXPORT const char *sim_build(void)
{
    return tune_build;
}

EXPORT uint16_t sim_crc16(const char *s)
{
    return tune_crc16(s);
}

EXPORT int sim_resets(void)
{
    return resets;
}

static void tx_drain(int budget)
{
    tx_budget = budget;
    while (_U1TXIE && tx_budget > 0 && out_len < out_max) {
        int before = out_len;
        _U1TXInterrupt();
        if (out_len == before)
            break;
    }
}

EXPORT void sim_init(void)
{
    memset(mock_ram, 0, sizeof mock_ram);
    *(int16_t *)&mock_ram[0x100A] = 32767;
    *(uint16_t *)&mock_ram[0x100C] = 7;
    motor.kp = 300;                     /* defaults of example_main.c */
    motor.ki = 60;
    motor.current_kp = 200;
    motor.current_ki = 20;
    startup.ramp_rate = 100;
    startup.align_current = 400;
    startup.handover_speed = 600;
    tune_ci = tune_cu1 = tune_cu2 = tune_div = tune_t = tune_ramp = tune_spd = tune_si = 0;
    tune_iq1 = tune_iq2 = tune_lost = 0;
    tune_block_init();
    vt_us = 0;
    model_next_us = TUNE_MODEL_US;
    timer_armed = 0;
    scope_init();
}

EXPORT void sim_rx(const uint8_t *data, int len)
{
    rx_data = data;
    rx_len = len;
    rx_pos = 0;
    _U1RXInterrupt();
}

/* period of Timer1 in microseconds (0 when stopped) */
EXPORT double sim_period_us(void)
{
    static const int shift[4] = { 0, 3, 6, 8 };
    if (!T1CONbits.TON)
        return 0.0;
    return (double)(((uint32_t)PR1 + 1u) << shift[T1CONbits.TCKPS]) / (SCOPE_FP_HZ / 1e6);
}

/* One Timer1 period of demo signals, the sample interrupt and the UART */
static void timer_tick(double per, double *credit)
{
    double gain = *(int16_t *)&mock_ram[0x100A] / 32768.0;
    int16_t s = (int16_t)(gain * (20000 * sin(phase) + 3000 * sin(7 * phase)));
    phase += 2 * 3.14159265358979 * 50.0 * per * 1e-6;     /* 50 Hz */
    memcpy(&mock_ram[0x1000], &s, 2);
    (*(uint16_t *)&mock_ram[0x1002]) += *(uint16_t *)&mock_ram[0x100C];
    (*(int32_t *)&mock_ram[0x1004])++;
    mock_ram[0x1008] = s > 0;
    _T1Interrupt();
    *credit += SCOPE_BAUD / 10.0 * per * 1e-6;          /* the UART may send baud/10 bytes per second */
    if (*credit >= 1) {
        tx_drain((int)*credit);
        *credit -= (int)*credit;
    }
}

/* advance the virtual time by us microseconds: the model steps every 100 us, Timer1 at its own
 * period (events in time order), the model also runs while Timer1 is stopped */
EXPORT int sim_run_us(double us, uint8_t *dst, int max)
{
    static double credit;
    double end = vt_us + us;
    int i;

    out_len = 0;
    out_max = max < (int)(sizeof out / sizeof out[0]) ? max : (int)(sizeof out / sizeof out[0]);
    for (;;) {
        double per = sim_period_us();
        if (per == 0) {
            timer_armed = 0;
        } else if (!timer_armed) {
            timer_armed = 1;
            timer_next_us = vt_us + per;
            credit = 0;
        }
        if (timer_armed && timer_next_us <= model_next_us) {
            if (timer_next_us > end)
                break;
            vt_us = timer_next_us;
            if (out_len < out_max)                  /* output full: drop the tick like a lost one */
                timer_tick(per, &credit);
            timer_next_us += per;
        } else {
            if (model_next_us > end)
                break;
            vt_us = model_next_us;
            tune_model_step();
            model_next_us += TUNE_MODEL_US;
        }
    }
    vt_us = end;
    if (!T1CONbits.TON)
        tx_drain(out_max);
    for (i = 0; i < out_len; i++)
        dst[i] = (uint8_t)out[i];
    return out_len;
}

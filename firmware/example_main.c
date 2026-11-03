/*
 * example_main.c - minimal usage of scope.c on dsPIC33CK64MC103
 * FRC 8 MHz -> PLL -> FOSC 64 MHz, FP = FCY = 32 MHz (matches SCOPE_FP_HZ)
 */
#include <xc.h>
#include "scope.h"

#pragma config FNOSC = FRC          /* start on FRC, switch to PLL below */
#pragma config FCKSM = CSECMD       /* clock switching enabled */
#pragma config FWDTEN = ON_SW
#pragma config ICS = PGD3
#pragma config JTAGEN = OFF

volatile int16_t  sine;             /* demo signals, pick them in the GUI */
volatile uint16_t ramp;
volatile int32_t  counter;
volatile uint8_t  toggle;
volatile int16_t  sine_gain = 32767;   /* written from the GUI: sine amplitude, Q15 (32767 = 1.0) */
volatile uint16_t ramp_step = 1;       /* written from the GUI: ramp increment per main loop pass */

/* ---------- virtual motor for the tuning scripts, see tune_model.h ---------- */

#define TUNE_MODEL_HZ   10000UL     /* current loop; the rest runs on every tenth pass */
#define TUNE_MODEL_IPL  3           /* above mini-scope (SCOPE_TIMER_IPL 2), like a control loop */

/* The model needs one SCCP of its own: SCCP1, or SCCP2 if the sample timer is SCCP1. */
#if SCOPE_TIMER == SCOPE_CCP1
#define TUNE_CCPN       2
#else
#define TUNE_CCPN       1
#endif
#define TUNE_CAT_(a, b, c)  a##b##c
#define TUNE_CAT(a, b, c)   TUNE_CAT_(a, b, c)
#define TUNE_CON1L      TUNE_CAT(CCP, TUNE_CCPN, CON1L)
#define TUNE_CON1Lbits  TUNE_CAT(CCP, TUNE_CCPN, CON1Lbits)
#define TUNE_CON1H      TUNE_CAT(CCP, TUNE_CCPN, CON1H)
#define TUNE_CON2L      TUNE_CAT(CCP, TUNE_CCPN, CON2L)
#define TUNE_CON2H      TUNE_CAT(CCP, TUNE_CCPN, CON2H)
#define TUNE_PRL        TUNE_CAT(CCP, TUNE_CCPN, PRL)
#define TUNE_IF         TUNE_CAT(_CCT, TUNE_CCPN, IF)
#define TUNE_IE         TUNE_CAT(_CCT, TUNE_CCPN, IE)
#define TUNE_IP         TUNE_CAT(_CCT, TUNE_CCPN, IP)
#define TUNE_ISR        TUNE_CAT(_CCT, TUNE_CCPN, Interrupt)
#define TUNE_TXIF       TUNE_CAT(_U, SCOPE_UART, TXIF)

struct tune_motor {
    uint16_t run;           /* 1 = start and run, 0 = stop; tune_startup.py */
    uint16_t state;         /* 0 stopped, 1 align, 2 open-loop ramp, 3 closed loop; tune_startup.py */
    int16_t  speed;         /* measured speed, 0.5 rpm per count; tune_startup.py, tune_speed.py */
    int16_t  speed_ref;     /* speed reference actually used by the loop; tune_speed.py */
    int16_t  iq;            /* torque current, Q15 with 32768 = 20 A; tune_startup.py, tune_speed.py */
    uint16_t fault;         /* 0 = none, else the model stopped; cleared by run = 0; all scripts */
    int16_t  kp;            /* speed loop proportional gain; tune_speed.py */
    int16_t  ki;            /* speed loop integral gain; tune_speed.py */
    int16_t  ref_offset;    /* added to the speed reference, for a speed step; tune_speed.py */
    int16_t  current_ref;   /* current loop reference, works at standstill; tune_current.py */
    int16_t  current;       /* current loop actual value, Q15 with 32768 = 20 A; tune_current.py */
    int16_t  current_kp;    /* current loop proportional gain; tune_current.py */
    int16_t  current_ki;    /* current loop integral gain; tune_current.py */
};

struct tune_startup {
    uint16_t ramp_rate;         /* open-loop acceleration, counts per 10 ms; tune_startup.py */
    uint16_t align_current;     /* current while aligning and ramping, Q15; tune_startup.py */
    uint16_t handover_speed;    /* speed where the speed loop takes over, counts; tune_startup.py */
};

volatile struct tune_motor motor = {
    .kp = 300, .ki = 60,
    .current_kp = 200, .current_ki = 20
};
volatile struct tune_startup startup = {
    .ramp_rate = 100, .align_current = 400, .handover_speed = 600
};

/* The tuning block: tune_alive (heartbeat), tune_build_id and tune_block_ms(), see tune_block.h.
 * This file is compiled again at every change, so tune_build changes with it. */
#include "tune_block.h"
#include "tune_model.h"

void __attribute__((interrupt, no_auto_psv)) TUNE_ISR(void)
{
    TUNE_IF = 0;
#ifdef __MPLAB_DEBUGGER_SIMULATOR
    /* The simulator does not raise the UART TX interrupt again once the FIFO was
     * filled, which loses stream samples. This interrupt kicks it every 100 us
     * (M0, DECISIONS.md). Not needed, and not done, on a real device. */
    TUNE_TXIF = 1;
#endif
    tune_model_step();
}

static void tune_model_init(void)
{
    TUNE_CON1L = 0;                 /* 16 bit timer on FP, interrupt on period match */
    TUNE_CON1H = 0;
    TUNE_CON2L = 0;
    TUNE_CON2H = 0;
    TUNE_PRL = (uint16_t)(SCOPE_FP_HZ / TUNE_MODEL_HZ - 1u);
    TUNE_IP = TUNE_MODEL_IPL;
    TUNE_IF = 0;
    TUNE_IE = 1;
    TUNE_CON1Lbits.CCPON = 1;
}

#ifdef __MPLAB_DEBUGGER_SIMULATOR
/* The simulator's UART cannot receive: tools/mdb_serial.py writes the command
 * bytes into sim_rx via MDB and then the count into sim_rx_len. */
volatile uint16_t sim_rx[64];
volatile uint16_t sim_rx_len;
#endif

static const int16_t table[32] = {
    0, 3212, 6393, 9512, 12539, 15446, 18204, 20787, 23170, 25329, 27245,
    28898, 30273, 31356, 32137, 32609, 32767, 32609, 32137, 31356, 30273,
    28898, 27245, 25329, 23170, 20787, 18204, 15446, 12539, 9512, 6393, 3212
};

static void clock_init(void)
{
    _PLLPRE = 1;
    _PLLFBDIV = 80;                 /* 8 MHz * 80 = 640 MHz VCO */
    _POST1DIV = 5;
    _POST2DIV = 1;                  /* FPLLO = 640 / 5 / 1 = 128 MHz, FOSC = FPLLO / 2 = 64 MHz */
    __builtin_write_OSCCONH(0x01);  /* NOSC = FRCPLL */
    __builtin_write_OSCCONL(OSCCON | 0x01);
#ifndef __MPLAB_DEBUGGER_SIMULATOR          /* the simulator never reports the switch / lock */
    while (OSCCONbits.OSWEN)
        ;
    while (!OSCCONbits.LOCK)
        ;
#endif
}

int main(void)
{
    uint16_t i = 0;

    tune_block_init();              /* before any interrupt is enabled */
    clock_init();
    scope_init();
    tune_model_init();

    while (1) {
        uint16_t k = i >> 8;            /* slow the demo signals down */
        int16_t v = table[k & 31];

        v = (int16_t)(((int32_t)v * sine_gain) >> 15);
        sine = (k & 32) ? -v : v;       /* one store: the sampling ISR never sees a half update */
        ramp += ramp_step;
        counter++;
        toggle = k & 1;
        i++;
#ifdef __MPLAB_DEBUGGER_SIMULATOR
        if (sim_rx_len) {
            uint16_t k, n = sim_rx_len;
            for (k = 0; k < n && k < 64; k++)
                scope_rx_byte((uint8_t)sim_rx[k]);
            sim_rx_len = 0;
        }
#endif
    }
}

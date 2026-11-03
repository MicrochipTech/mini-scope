/*
 * load_main.c - load test of card M7b, configuration "load" of test.X (simulator only)
 *
 * Replaces example_main.c in that configuration. Everything is compiled out in the other
 * configurations (LOAD_TEST is set in the "load" configuration only).
 *
 *   control ISR  SCCP1 at 20 kHz, IPL 3 (above mini-scope: sample timer 2, UART 1). Burns a
 *                fixed number of cycles with REPEAT, LOAD_SHARE_PCT of the period.
 *   idle loop    main() counts passes in `idle`. Passes per cycle fall when anything else runs,
 *                so the loss against a run without load and without mini-scope is the share
 *                taken by the interrupts.
 *   time base    SCCP2 free running 32 bit at FP: length of the control ISR, and the time of a snapshot.
 *                SCCP3 free running 16 bit at FP/4: a channel of the stream and the capture (time stamp)
 *   snapshot     the PC writes snap_req = 1, the main loop copies idle and the time base into snap_idle and
 *                snap_time (one pass of the loop apart at most) and clears snap_req
 *
 * LOAD_MIPS (32 or 100) sets the PLL; SCOPE_FP_HZ must be given with it on the command line:
 *   make -f Makefile CONF=load build MP_EXTRA_CC_PRE="-DLOAD_MIPS=100 -DSCOPE_FP_HZ=100000000UL"
 * Variables that the PC writes with the MDB command `write` are 32 bit.
 */
#ifdef LOAD_TEST
#include <xc.h>
#include "scope.h"

#pragma config FNOSC = FRC          /* start on FRC, switch to PLL below */
#pragma config FCKSM = CSECMD       /* clock switching enabled */
#pragma config FWDTEN = ON_SW
#pragma config ICS = PGD3
#pragma config JTAGEN = OFF

#ifndef LOAD_MIPS
#define LOAD_MIPS       32
#endif
#if SCOPE_FP_HZ != LOAD_MIPS * 1000000UL
#error "SCOPE_FP_HZ must be LOAD_MIPS * 1000000"
#endif
#if SCOPE_TIMER != SCOPE_T1
#error "the load test uses SCCP1 and SCCP2, mini-scope must run on Timer1"
#endif

#define LOAD_HZ         20000UL
#define LOAD_IPL        3
#ifndef LOAD_SHARE_PCT
#define LOAD_SHARE_PCT  60
#endif
#ifndef LOAD_OVERHEAD
#define LOAD_OVERHEAD   0           /* cycles of the ISR besides the REPEAT loop, see docs/evidence/M7b.md */
#endif
#ifndef LOAD_BURN
#define LOAD_BURN       (SCOPE_FP_HZ / LOAD_HZ * LOAD_SHARE_PCT / 100UL - LOAD_OVERHEAD)
#endif

#define LOAD_CAT_(a, b, c)  a##b##c
#define LOAD_CAT(a, b, c)   LOAD_CAT_(a, b, c)
#define LOAD_TXIF           LOAD_CAT(_U, SCOPE_UART, TXIF)

volatile uint32_t idle;             /* passes of the main loop */
volatile uint16_t ramp;
volatile uint16_t ramp_step = 1;
volatile uint16_t isr_ticks;        /* passes of the control ISR, 16 bit for the stream */
volatile uint32_t isr_total;
volatile uint16_t isr_len;          /* last length in FP ticks: from the first read of the time base to the last */
volatile uint32_t snap_req;         /* written by the PC */
volatile uint32_t snap_idle;
volatile uint32_t snap_time;        /* SCCP2 ticks = FP cycles */
volatile uint32_t load_on;          /* 1 = control ISR runs; written by the PC */
volatile uint32_t load_burn = LOAD_BURN;    /* REPEAT count of the ISR (cycles); written by the PC */

void __attribute__((interrupt, no_auto_psv)) _CCT1Interrupt(void)
{
    uint16_t t0 = CCP2TMRL;
    uint16_t n = (uint16_t)load_burn;

    _CCT1IF = 0;
#ifdef __MPLAB_DEBUGGER_SIMULATOR
    /* same workaround as the model in example_main.c: the simulator does not raise the UART TX
     * interrupt again once the FIFO was filled. Not needed, and not done, on a real device. */
    LOAD_TXIF = 1;
#endif
    isr_ticks++;
    isr_total++;
    __asm__ volatile ("repeat %0\n\tnop" : : "r" (n - 1u));
    isr_len = CCP2TMRL - t0;
}

static void load_init(void)
{
    CCP2CON1L = 0;                  /* free running 32 bit timer on FP */
    CCP2CON1H = 0;
    CCP2CON2L = 0;
    CCP2CON2H = 0;
    CCP2CON1Lbits.T32 = 1;
    CCP2PRL = 0xFFFF;
    CCP2PRH = 0xFFFF;
    CCP2CON1Lbits.CCPON = 1;

    CCP3CON1L = 0;                  /* free running 16 bit timer on FP/4 */
    CCP3CON1H = 0;
    CCP3CON2L = 0;
    CCP3CON2H = 0;
    CCP3CON1Lbits.TMRPS = 1;
    CCP3PRL = 0xFFFF;
    CCP3CON1Lbits.CCPON = 1;

    CCP1CON1L = 0;                  /* 16 bit timer on FP, interrupt on period match; switched on by load_on */
    CCP1CON1H = 0;
    CCP1CON2L = 0;
    CCP1CON2H = 0;
    CCP1PRL = (uint16_t)(SCOPE_FP_HZ / LOAD_HZ - 1u);
    _CCT1IP = LOAD_IPL;
    _CCT1IF = 0;
    _CCT1IE = 1;
}

#ifdef __MPLAB_DEBUGGER_SIMULATOR
/* The simulator's UART cannot receive: tools/mdb_serial.py writes the command
 * bytes into sim_rx via MDB and then the count into sim_rx_len. */
volatile uint16_t sim_rx[64];
volatile uint16_t sim_rx_len;
#endif

static void clock_init(void)
{
    /* DS70005399D Table 31-5: 100 MIPS = N 1, N2 1, N3 1, M 50, FVCO 400 MHz, FPLLO 400 MHz; FOSC = FPLLO / 2.
     * 32 MIPS is the setting of example_main.c. */
    _PLLPRE = 1;
#if LOAD_MIPS == 100
    _PLLFBDIV = 50;                 /* 8 MHz * 50 = 400 MHz VCO */
    _POST1DIV = 1;
    _POST2DIV = 1;                  /* FPLLO = 400 MHz, FOSC = 200 MHz, FP = 100 MHz */
#elif LOAD_MIPS == 32
    _PLLFBDIV = 80;                 /* 8 MHz * 80 = 640 MHz VCO */
    _POST1DIV = 5;
    _POST2DIV = 1;                  /* FPLLO = 640 / 5 / 1 = 128 MHz, FOSC = 64 MHz, FP = 32 MHz */
#else
#error "LOAD_MIPS must be 32 or 100"
#endif
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
    uint32_t state = 0;

    clock_init();
    scope_init();
    load_init();

    while (1) {
        idle++;
        ramp += ramp_step;
        if (snap_req) {
            uint32_t a = idle;
            uint16_t lo = CCP2TMRL;     /* reading the low word latches the high word */
            uint16_t hi = CCP2TMRH;

            snap_idle = a;
            snap_time = ((uint32_t)hi << 16) | lo;
            snap_req = 0;
        }
        if (load_on != state) {
            state = load_on;
            CCP1CON1Lbits.CCPON = state ? 1 : 0;
            _CCT1IF = 0;
        }
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
#endif

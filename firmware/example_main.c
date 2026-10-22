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
    _POST2DIV = 2;                  /* 640 / 5 / 2 = 64 MHz FOSC */
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

    clock_init();
    scope_init();

    while (1) {
        uint16_t k = i >> 8;            /* slow the demo signals down */
        int16_t v = table[k & 31];

        sine = (k & 32) ? -v : v;       /* one store: the sampling ISR never sees a half update */
        ramp = i;
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

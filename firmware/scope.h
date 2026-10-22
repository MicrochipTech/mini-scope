/*
 * scope.h - minimal variable scope for dsPIC33CK (register level, no MCC)
 *
 * Samples any number of RAM addresses (8/16/32 bit) and sends them over
 * a UART to the PC GUI (scope_gui.py).
 *
 *   stream  : every sample is sent immediately (rate limited by the baud rate)
 *   capture : samples go into a RAM buffer, start on a trigger (with
 *             pretrigger), the full buffer is sent afterwards - any rate
 *
 * Usage:  #include "scope.h"   scope_init();   (after your own clock and PPS setup)
 * scope_init() sets up PPS, UART, timer and interrupts - nothing else to do.
 * All settings below can be overridden with -D in the project properties.
 */
#ifndef SCOPE_H
#define SCOPE_H

#include <stdint.h>

/* ================= selection: UART and sample clock ================= */

#define SCOPE_NONE   0      /* no timer: call scope_sample() from your own ISR */
#define SCOPE_T1     1      /* Timer1 */
#define SCOPE_CCP1  11      /* SCCP1..SCCP4 in 16-bit timer mode */
#define SCOPE_CCP2  12
#define SCOPE_CCP3  13
#define SCOPE_CCP4  14

#ifndef SCOPE_UART
#define SCOPE_UART       1            /* 1, 2 or 3 */
#endif
#ifndef SCOPE_TIMER
#define SCOPE_TIMER      SCOPE_T1     /* SCOPE_T1, SCOPE_CCP1..SCOPE_CCP4 or SCOPE_NONE */
#endif

/* ================= clock, baud rate, sizes ================= */

#ifndef SCOPE_FP_HZ
#define SCOPE_FP_HZ      32000000UL   /* peripheral clock FP = FOSC/2 */
#endif
#ifndef SCOPE_BAUD
#define SCOPE_BAUD       115200UL     /* BRG = FP/(4*baud)-1 = 68 at 32 MHz, +0.6 % */
#endif
#ifndef SCOPE_TIMER_IPL
#define SCOPE_TIMER_IPL  2            /* must be higher than SCOPE_UART_IPL */
#endif
#ifndef SCOPE_UART_IPL
#define SCOPE_UART_IPL   1
#endif
#ifndef SCOPE_MAX_CH
#define SCOPE_MAX_CH     16           /* max. channels (RAM: 4 bytes each) */
#endif
#ifndef SCOPE_CAPBUF
#define SCOPE_CAPBUF     4096u        /* capture buffer in bytes */
#endif
#ifndef SCOPE_TXBUF
#define SCOPE_TXBUF      512u         /* stream TX ring, power of two */
#endif

#if SCOPE_CAPBUF > 65535u || SCOPE_CAPBUF < 64u
#error "SCOPE_CAPBUF must be 64..65535 bytes (and fit into RAM)"
#endif
#if SCOPE_TXBUF < 64u || (SCOPE_TXBUF & (SCOPE_TXBUF - 1u))
#error "SCOPE_TXBUF must be a power of two, at least 64"
#endif
#if SCOPE_MAX_CH < 1 || SCOPE_MAX_CH > 255
#error "SCOPE_MAX_CH must be 1..255"
#endif
#if SCOPE_TXBUF - 1u < 2u + 4u * SCOPE_MAX_CH
#error "SCOPE_TXBUF too small for one stream frame of SCOPE_MAX_CH 32-bit channels"
#endif
/* RAM used by scope.c: SCOPE_CAPBUF + SCOPE_TXBUF + 7 * SCOPE_MAX_CH + 56 bytes,
 * plus 4 * SCOPE_MAX_CH bytes of stack while sampling in stream mode */

/* ================= pins ================= */

/* PPS codes of the selected UART (DS70005399D page 117 outputs, page 113 inputs) */
#if SCOPE_UART == 1
#define SCOPE_UTX_CODE   1            /* U1TX */
#define SCOPE_URXR       _U1RXR       /* RPINR18 */
#elif SCOPE_UART == 2
#define SCOPE_UTX_CODE   3            /* U2TX */
#define SCOPE_URXR       _U2RXR       /* RPINR19 */
#elif SCOPE_UART == 3
#define SCOPE_UTX_CODE   27           /* U3TX */
#define SCOPE_URXR       _U3RXR       /* RPINR27 */
#else
#error "SCOPE_UART must be 1, 2 or 3"
#endif

/* UART pins as RP numbers: RPn = RB(n-32) for 32..47, RC(n-48) for 48..53.
 * Default: TX on RP37/RB5 (PGD3), RX on RP38/RB6 (PGC3) = ICSP header.
 * Direction (TRIS) and digital mode (ANSEL) are set automatically. */
#ifndef SCOPE_TX_RP
#define SCOPE_TX_RP      37           /* RB5 */
#endif
#ifndef SCOPE_RX_RP
#define SCOPE_RX_RP      38           /* RB6 */
#endif
#if SCOPE_TX_RP < 32 || SCOPE_TX_RP > 53 || SCOPE_RX_RP < 32 || SCOPE_RX_RP > 53
#error "SCOPE_TX_RP / SCOPE_RX_RP must be 32..53 (RB0..RB15 = RP32..RP47, RC0..RC5 = RP48..RP53)"
#endif
#if SCOPE_TX_RP == SCOPE_RX_RP
#error "SCOPE_TX_RP and SCOPE_RX_RP must be different pins"
#endif

#define SCOPE_CAT_(a, b, c) a##b##c
#define SCOPE_CAT(a, b, c)  SCOPE_CAT_(a, b, c)

/* own pin setup: define SCOPE_PINS_INIT() before including scope.h */
#ifndef SCOPE_PINS_INIT
#define SCOPE_PINS_INIT() do {                                          \
        __builtin_write_RPCON(0x0000);              /* unlock PPS */     \
        SCOPE_CAT(_RP, SCOPE_TX_RP, R) = SCOPE_UTX_CODE;  /* TX pin */   \
        SCOPE_URXR = SCOPE_RX_RP;                   /* RX pin */         \
        __builtin_write_RPCON(0x0800);              /* lock PPS */       \
        scope_pin_dir(SCOPE_TX_RP, 0);                                  \
        scope_pin_dir(SCOPE_RX_RP, 1);                                  \
    } while (0)
#endif

/* ================= API ================= */

void scope_init(void);
void scope_sample(void);              /* only needed with SCOPE_TIMER = SCOPE_NONE */
void scope_rx_byte(uint8_t b);        /* command byte from elsewhere, e.g. the simulator */
void scope_pin_dir(uint8_t rp, uint8_t input);  /* digital in/out for pin RPn, used by SCOPE_PINS_INIT */

extern volatile uint16_t scope_overflows;   /* stream frames dropped (UART too slow) */

#endif

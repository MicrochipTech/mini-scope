/*
 * sim.c - runs the real scope.c on the PC (DLL for sim_serial.py)
 * Build: gcc -O2 -shared -Itests/mock -Ifirmware -o sim.dll tests/mock/sim.c firmware/scope.c
 *
 * Demo variables in mock RAM (same names as example_main.c):
 *   0x1000 sine (int16)  0x1002 ramp (uint16)  0x1004 counter (int32)  0x1008 toggle (uint8)
 */
#include <math.h>
#include <string.h>
#include "xc.h"
#include "scope.h"

#define EXPORT __declspec(dllexport)

void _U1TXInterrupt(void);
void _U1RXInterrupt(void);
void _T1Interrupt(void);

sta_t   U1STAbits;
mode_t_ U1MODEbits;
t1con_t T1CONbits;
uint16_t U1MODE, U1MODEH, U1STA, U1STAH, U1BRG, U1BRGH, T1CON, TMR1, PR1;
int _U1TXIF, _U1TXIE, _U1RXIF, _U1RXIE, _U1TXIP, _U1RXIP, _T1IF, _T1IE, _T1IP, _GIE;
uint8_t mock_ram[0x10000];

static stah_t stah;
static const uint8_t *rx_data;
static int rx_len, rx_pos;
static uint16_t out[1 << 20];           /* every U1TXREG write lands here */
static int out_len, out_max, tx_budget;
static double phase;

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

/* run n timer ticks, the UART may send baud/10 bytes per second */
EXPORT int sim_run(int ticks, uint8_t *dst, int max)
{
    double per = sim_period_us();
    double credit = 0;
    int i;

    out_len = 0;
    out_max = max < (int)(sizeof out / sizeof out[0]) ? max : (int)(sizeof out / sizeof out[0]);
    if (per == 0) {
        tx_drain(out_max);
    } else {
        double bytes_per_tick = SCOPE_BAUD / 10.0 * per * 1e-6;
        for (i = 0; i < ticks && out_len < out_max; i++) {
            int16_t s = (int16_t)(20000 * sin(phase) + 3000 * sin(7 * phase));
            phase += 2 * 3.14159265358979 * 50.0 * per * 1e-6;     /* 50 Hz */
            memcpy(&mock_ram[0x1000], &s, 2);
            (*(uint16_t *)&mock_ram[0x1002]) += 7;
            (*(int32_t *)&mock_ram[0x1004])++;
            mock_ram[0x1008] = s > 0;
            _T1Interrupt();
            credit += bytes_per_tick;
            if (credit >= 1) {
                tx_drain((int)credit);
                credit -= (int)credit;
            }
        }
    }
    for (i = 0; i < out_len; i++)
        dst[i] = (uint8_t)out[i];
    return out_len;
}

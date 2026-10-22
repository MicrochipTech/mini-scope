/*
 * scope.c - minimal variable scope for dsPIC33CK, see scope.h
 *
 * Protocol (PC -> target), all values little endian:
 *   'P'                     ping            -> "SCP1" capbuf16 maxch8
 *   'C' n mode per16 tch ttype lvl32 pre16 n*(addr16 w)
 *                           configure       -> ACK / NAK
 *       mode  0 = stream, 1 = capture
 *       ttype 0 = none, 1 = rising, 2 = falling   (tch = trigger channel)
 *       w     bits 0..2 = width in bytes (1,2,4), bit 7 = signed
 *   'R'                     run / arm       -> ACK, then data
 *   'S'                     stop            -> ACK
 *
 * Target -> PC:
 *   stream : 0xA5 seq payload            (payload = all channels, rec bytes)
 *   capture: 0xC5 depth16 pre16 depth*payload   (oldest sample first,
 *            trigger sample at index pre)
 */
#include <xc.h>
#include "scope.h"

#ifndef SCOPE_ISR
#define SCOPE_ISR __attribute__((interrupt, no_auto_psv))
#endif
#ifndef SCOPE_ADDR
#define SCOPE_ADDR(a) ((volatile void *)(a))
#endif

/* ---------- register names of the UART and timer selected in scope.h ---------- */

#define CAT_(a, b, c) a##b##c
#define CAT(a, b, c)  CAT_(a, b, c)

#define UxMODE        CAT(U, SCOPE_UART, MODE)
#define UxMODEH       CAT(U, SCOPE_UART, MODEH)
#define UxMODEbits    CAT(U, SCOPE_UART, MODEbits)
#define UxSTA         CAT(U, SCOPE_UART, STA)
#define UxSTAH        CAT(U, SCOPE_UART, STAH)
#define UxSTAbits     CAT(U, SCOPE_UART, STAbits)
#define UxSTAHbits    CAT(U, SCOPE_UART, STAHbits)
#define UxBRG         CAT(U, SCOPE_UART, BRG)
#define UxBRGH        CAT(U, SCOPE_UART, BRGH)
#define UxTXREG       CAT(U, SCOPE_UART, TXREG)
#define UxRXREG       CAT(U, SCOPE_UART, RXREG)
#define UxTXIF        CAT(_U, SCOPE_UART, TXIF)
#define UxTXIE        CAT(_U, SCOPE_UART, TXIE)
#define UxTXIP        CAT(_U, SCOPE_UART, TXIP)
#define UxRXIF        CAT(_U, SCOPE_UART, RXIF)
#define UxRXIE        CAT(_U, SCOPE_UART, RXIE)
#define UxRXIP        CAT(_U, SCOPE_UART, RXIP)
#define UxTX_ISR      CAT(_U, SCOPE_UART, TXInterrupt)
#define UxRX_ISR      CAT(_U, SCOPE_UART, RXInterrupt)

#if SCOPE_TIMER == SCOPE_CCP1
#define CCPN 1
#elif SCOPE_TIMER == SCOPE_CCP2
#define CCPN 2
#elif SCOPE_TIMER == SCOPE_CCP3
#define CCPN 3
#elif SCOPE_TIMER == SCOPE_CCP4
#define CCPN 4
#elif SCOPE_TIMER != SCOPE_T1 && SCOPE_TIMER != SCOPE_NONE
#error "SCOPE_TIMER must be SCOPE_T1, SCOPE_CCP1..SCOPE_CCP4 or SCOPE_NONE"
#endif
#ifdef CCPN
#define CCPxCON1L     CAT(CCP, CCPN, CON1L)
#define CCPxCON1Lbits CAT(CCP, CCPN, CON1Lbits)
#define CCPxCON1H     CAT(CCP, CCPN, CON1H)
#define CCPxCON2L     CAT(CCP, CCPN, CON2L)
#define CCPxCON2H     CAT(CCP, CCPN, CON2H)
#define CCPxPRL       CAT(CCP, CCPN, PRL)
#define CCPxTMRL      CAT(CCP, CCPN, TMRL)
#define CCTxIF        CAT(_CCT, CCPN, IF)
#define CCTxIE        CAT(_CCT, CCPN, IE)
#define CCTxIP        CAT(_CCT, CCPN, IP)
#define CCTx_ISR      CAT(_CCT, CCPN, Interrupt)
#endif

#define SYNC_STREAM  0xA5
#define SYNC_CAPTURE 0xC5
#define ACK          0x06
#define NAK          0x15
#define CFG_HDR      13

enum { IDLE, STREAM, PRE, ARMED, POST };
enum { TRIG_NONE, TRIG_RISE, TRIG_FALL };

typedef struct {
    volatile void *addr;
    uint8_t width;
    uint8_t sign;
} ch_t;

static ch_t     ch[SCOPE_MAX_CH];
static uint8_t  n_ch, mode, trig_ch, trig_type;
static uint16_t rec_len, trig_off;
static int32_t  trig_level, trig_prev;
static uint16_t pre, depth, count;
static volatile uint8_t state;
static uint8_t  seq;

static uint8_t  cap[SCOPE_CAPBUF];
static uint16_t wpos, cap_end;
static volatile uint16_t dump_pos, dump_left;

static uint8_t  txbuf[SCOPE_TXBUF];
static volatile uint16_t head, tail;
volatile uint16_t scope_overflows;

static uint8_t  rx[CFG_HDR + 3 * SCOPE_MAX_CH];
static uint16_t rx_len, rx_need;

/* ---------- TX ring (producer: sample/RX ISR, consumer: TX ISR) ---------- */

static uint16_t tx_free(void)
{
    return (uint16_t)(tail - head - 1u) & (SCOPE_TXBUF - 1u);
}

static void tx_put(uint8_t b)
{
    txbuf[head] = b;
    head = (head + 1u) & (SCOPE_TXBUF - 1u);
}

static void tx_kick(void)
{
    UxTXIF = 1;
    UxTXIE = 1;
}

static void reply(uint8_t b)
{
    if (tx_free())
        tx_put(b);
    tx_kick();
}

void SCOPE_ISR UxTX_ISR(void)
{
    UxTXIF = 0;
    while (!UxSTAHbits.UTXBF) {
        if (tail != head) {
            UxTXREG = txbuf[tail];
            tail = (tail + 1u) & (SCOPE_TXBUF - 1u);
        } else if (dump_left) {
            UxTXREG = cap[dump_pos];
            if (++dump_pos >= cap_end)
                dump_pos = 0;
            dump_left--;
        } else {
            UxTXIE = 0;
            if (tail != head || dump_left)   /* refilled meanwhile */
                UxTXIE = 1;
            break;
        }
    }
}

/* ---------- sample clock: Timer1 or SCCPx (see SCOPE_TIMER) ---------- */

static void timer_run(uint8_t on)
{
#if SCOPE_TIMER == SCOPE_T1
    TMR1 = 0;
    _T1IF = 0;
    T1CONbits.TON = on;
#elif defined(CCPN)
    CCPxTMRL = 0;
    CCTxIF = 0;
    CCPxCON1Lbits.CCPON = on;
#else
    (void)on;
#endif
}

static void timer_set(uint16_t period_us)
{
#if SCOPE_TIMER == SCOPE_T1
    static const uint8_t shift[4] = { 0, 3, 6, 8 };   /* 1:1, 1:8, 1:64, 1:256 */
#elif defined(CCPN)
    static const uint8_t shift[4] = { 0, 2, 4, 6 };   /* 1:1, 1:4, 1:16, 1:64 */
#endif
#if SCOPE_TIMER != SCOPE_NONE
    uint32_t ticks = (SCOPE_FP_HZ / 1000000UL) * period_us;
    uint8_t ps = 0;

    while (ps < 3 && (ticks >> shift[ps]) > 65536UL)
        ps++;
#endif
#if SCOPE_TIMER == SCOPE_T1
    T1CON = 0;                          /* TCS = 0: clock FP */
    T1CONbits.TCKPS = ps;
    PR1 = (uint16_t)((ticks >> shift[ps]) - 1u);
    _T1IP = SCOPE_TIMER_IPL;
    _T1IE = 1;
#elif defined(CCPN)
    CCPxCON1L = 0;                      /* off, MOD = 0: 16-bit timer, CLKSEL = 0: FP */
    CCPxCON1H = 0;                      /* interrupt on every period match */
    CCPxCON2L = 0;
    CCPxCON2H = 0;                      /* no outputs */
    CCPxCON1Lbits.TMRPS = ps;
    CCPxPRL = (uint16_t)((ticks >> shift[ps]) - 1u);
    CCTxIP = SCOPE_TIMER_IPL;
    CCTxIE = 1;
#else
    (void)period_us;
#endif
}

#if SCOPE_TIMER == SCOPE_T1
void SCOPE_ISR _T1Interrupt(void)
{
    _T1IF = 0;
    scope_sample();
}
#elif defined(CCPN)
void SCOPE_ISR CCTx_ISR(void)
{
    CCTxIF = 0;
    scope_sample();
}
#endif

/* ---------- sampling ---------- */

static void read_ch(uint8_t *d)
{
    uint8_t i;

    for (i = 0; i < n_ch; i++) {
        if (ch[i].width == 1) {
            *d++ = *(volatile uint8_t *)ch[i].addr;
        } else if (ch[i].width == 2) {
            uint16_t v = *(volatile uint16_t *)ch[i].addr;
            *d++ = (uint8_t)v;
            *d++ = (uint8_t)(v >> 8);
        } else {
            uint32_t v = *(volatile uint32_t *)ch[i].addr;
            *d++ = (uint8_t)v;
            *d++ = (uint8_t)(v >> 8);
            *d++ = (uint8_t)(v >> 16);
            *d++ = (uint8_t)(v >> 24);
        }
    }
}

static int32_t trig_value(const uint8_t *r)
{
    const ch_t *c = &ch[trig_ch];

    r += trig_off;
    if (c->width == 1)
        return c->sign ? (int32_t)(int8_t)r[0] : (int32_t)r[0];
    if (c->width == 2) {
        uint16_t v = (uint16_t)(r[0] | ((uint16_t)r[1] << 8));
        return c->sign ? (int32_t)(int16_t)v : (int32_t)v;
    }
    return (int32_t)((uint32_t)r[0] | ((uint32_t)r[1] << 8) |
                     ((uint32_t)r[2] << 16) | ((uint32_t)r[3] << 24));
}

static void capture_done(void)
{
    state = IDLE;                       /* timer keeps running and pumps the dump */
    tx_put(SYNC_CAPTURE);
    tx_put((uint8_t)depth);
    tx_put((uint8_t)(depth >> 8));
    tx_put((uint8_t)pre);
    tx_put((uint8_t)(pre >> 8));
    dump_pos = wpos;                    /* oldest sample */
    dump_left = cap_end;
    tx_kick();
}

void scope_sample(void)
{
    uint8_t s = state;
    uint8_t *r;
    int32_t v;

    if (s == IDLE) {
        if (dump_left)
            tx_kick();                  /* in case the TX IRQ does not re-trigger (simulator) */
        else
            timer_run(0);
        return;
    }

    if (s == STREAM) {
        uint8_t rec[4 * SCOPE_MAX_CH];
        uint16_t i;

        if (tx_free() < rec_len + 2u) {
            scope_overflows++;
            seq++;                      /* PC sees the gap */
            return;
        }
        read_ch(rec);
        tx_put(SYNC_STREAM);
        tx_put(seq++);
        for (i = 0; i < rec_len; i++)
            tx_put(rec[i]);
        tx_kick();
        return;
    }

    r = &cap[wpos];
    read_ch(r);
    wpos += rec_len;
    if (wpos >= cap_end)
        wpos = 0;

    if (s == PRE) {
        if (++count >= pre) {
            state = ARMED;
            trig_prev = trig_value(r);
        }
        return;
    }
    if (s == ARMED) {
        v = trig_value(r);
        if (trig_type == TRIG_NONE ||
            (trig_type == TRIG_RISE && trig_prev < trig_level && v >= trig_level) ||
            (trig_type == TRIG_FALL && trig_prev > trig_level && v <= trig_level)) {
            state = POST;
            count = 0;
            s = POST;
        }
        trig_prev = v;
    }
    if (s == POST) {
        if (++count >= depth - pre)     /* trigger sample counts as first */
            capture_done();
    }
}

/* ---------- commands ---------- */

static void configure(void)
{
    uint8_t i, n = rx[1];
    uint16_t len = 0, per;

    for (i = 0; i < n; i++) {
        const uint8_t *p = &rx[CFG_HDR + 3 * i];
        uint16_t a = (uint16_t)(p[0] | ((uint16_t)p[1] << 8));
        uint8_t w = p[2] & 0x07;

        if ((w != 1 && w != 2 && w != 4) || (w > 1 && (a & 1))) {
            n_ch = 0;                   /* odd address -> address error trap */
            reply(NAK);
            return;
        }
        ch[i].addr = SCOPE_ADDR(a);
        ch[i].width = w;
        ch[i].sign = p[2] >> 7;
        if (i == rx[5])
            trig_off = len;
        len += w;
    }
    per = (uint16_t)(rx[3] | ((uint16_t)rx[4] << 8));
    if (per == 0 || rx[5] >= n || len > SCOPE_CAPBUF / 2) {
        n_ch = 0;
        reply(NAK);
        return;
    }
    n_ch = n;
    rec_len = len;
    mode = rx[2];
    trig_ch = rx[5];
    trig_type = rx[6];
    trig_level = (int32_t)((uint32_t)rx[7] | ((uint32_t)rx[8] << 8) |
                           ((uint32_t)rx[9] << 16) | ((uint32_t)rx[10] << 24));
    pre = (uint16_t)(rx[11] | ((uint16_t)rx[12] << 8));
    depth = SCOPE_CAPBUF / len;
    cap_end = depth * len;
    if (trig_type == TRIG_NONE)
        pre = 0;
    if (pre >= depth)
        pre = depth - 1;
    timer_set(per);
    reply(ACK);
}

static void command(void)
{
    if (rx[0] == 'S') {
        state = IDLE;
        timer_run(0);
        dump_left = 0;
        reply(ACK);
        return;
    }
    if (state != IDLE || dump_left)     /* busy: only 'S' is accepted */
        return;

    switch (rx[0]) {
    case 'P':                           /* "SCP1", capture buffer size, max. channels */
        reply('S'); reply('C'); reply('P'); reply('1');
        reply((uint8_t)SCOPE_CAPBUF);
        reply((uint8_t)(SCOPE_CAPBUF >> 8));
        reply(SCOPE_MAX_CH);
        break;
    case 'C':
        configure();
        break;
    case 'R':
        if (!n_ch) {
            reply(NAK);
            break;
        }
        reply(ACK);                     /* ACK goes out before the data */
        seq = 0;
        wpos = 0;
        count = 0;
        scope_overflows = 0;
        state = mode ? (pre ? PRE : ARMED) : STREAM;
        timer_run(1);
        break;
    default:
        reply(NAK);
        break;
    }
}

void scope_rx_byte(uint8_t b)
{
    if (rx_len == 0)
        rx_need = (b == 'C') ? 2 : 1;
    rx[rx_len++] = b;
    if (rx_len == 2 && rx[0] == 'C') {
        if (b == 0 || b > SCOPE_MAX_CH) {
            rx_len = 0;
            if (state == IDLE)
                reply(NAK);
            return;
        }
        rx_need = CFG_HDR + 3u * b;
    }
    if (rx_len >= rx_need) {
        command();
        rx_len = 0;
    }
}

void SCOPE_ISR UxRX_ISR(void)
{
    UxRXIF = 0;
    while (!UxSTAHbits.URXBE)
        scope_rx_byte((uint8_t)UxRXREG);
    if (UxSTAbits.OERR)
        UxSTAbits.OERR = 0;
}

/* ---------- init ---------- */

void scope_init(void)
{
    SCOPE_PINS_INIT();

    UxMODE = 0;
    UxMODEH = 0;                        /* BCLKSEL = FOSC/2 = FP */
    UxSTA = 0;
    UxSTAH = 0;
    UxSTAHbits.UTXISEL = 7;             /* TX IRQ as soon as one FIFO slot is free */
    UxMODEbits.BRGH = 1;                /* baud = FP / (4 * (BRG + 1)), MOD = 0: async 8N1 */
    UxBRG = (uint16_t)((SCOPE_FP_HZ + 2UL * SCOPE_BAUD) / (4UL * SCOPE_BAUD) - 1UL);
    UxBRGH = 0;                         /* upper bits of the 20 bit divider */
    UxMODEbits.UARTEN = 1;              /* same order as MCC: module on, then TX/RX */
    UxMODEbits.UTXEN = 1;
    UxMODEbits.URXEN = 1;

    UxTXIP = SCOPE_UART_IPL;
    UxRXIP = SCOPE_UART_IPL;
    UxRXIF = 0;
    UxRXIE = 1;
    _GIE = 1;                           /* global interrupt enable (0 in the simulator) */
}

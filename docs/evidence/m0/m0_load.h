/*
 * m0_load.h - throwaway load for the risk check M0, never part of the firmware.
 * m0_common.py copies it next to example_main.c in a scratch copy, puts the
 * M0_FAST value in front of it and includes it in example_main.c.
 *
 * M0_FAST 0: nothing added (baseline)
 *         1: SCCP1 interrupt at 10 kHz that only counts
 *         2: the same with a body of 25 fixed point multiplications
 *
 * SCCP1 is the candidate because mini-scope uses Timer1 in configuration sim.
 * SCCP2 runs free at FP/4 as an independent time base: its timer register can
 * be streamed as a channel, and it times the interrupt body.
 */
#define M0_FAST_HZ   10000UL
#define M0_FAST_IPL  3                  /* above mini-scope, like a control loop */

volatile uint16_t probe;                /* written with 'W', streamed back */
volatile uint16_t fast_ticks;           /* passes of the 10 kHz interrupt */
volatile uint16_t fast_cycles;          /* length of its last pass in FP/4 ticks */
volatile uint32_t fast_total;

#if M0_FAST
static inline int16_t qmul(int16_t a, int16_t b)
{
    return (int16_t)(((int32_t)a * b) >> 15);
}

static inline int16_t sat(int32_t v)
{
    if (v > 32767)
        return 32767;
    if (v < -32768)
        return -32768;
    return (int16_t)v;
}

#if M0_FAST == 2
static const int16_t m0_sin[32] = {
    0, 6393, 12539, 18204, 23170, 27245, 30273, 32137, 32767, 32137, 30273,
    27245, 23170, 18204, 12539, 6393, 0, -6393, -12539, -18204, -23170, -27245,
    -30273, -32137, -32767, -32137, -30273, -27245, -23170, -18204, -12539, -6393
};
static uint16_t m_angle;
static int16_t  m_ia, m_ib, m_id, m_iq, m_ud, m_uq, m_speed;
static int32_t  m_int_d, m_int_q, m_int_w;

/* current loop of a generic drive: transforms, 3 PI controllers, plant */
static inline void m0_body(void)
{
    int16_t s = m0_sin[(m_angle >> 11) & 31];
    int16_t c = m0_sin[((m_angle >> 11) + 8) & 31];
    int16_t ialpha = m_ia;
    int16_t ibeta = qmul(18918, m_ia) + qmul(18918, m_ib);
    int16_t id = qmul(ialpha, c) + qmul(ibeta, s);
    int16_t iq = qmul(ibeta, c) - qmul(ialpha, s);
    int16_t iq_ref, ed, eq, ew, ualpha, ubeta;

    ew = (int16_t)(8000 - m_speed);
    m_int_w += qmul(ew, 300);
    m_int_w = sat(m_int_w);
    iq_ref = sat((int32_t)qmul(ew, 12000) + m_int_w);
    ed = (int16_t)(-id);
    eq = (int16_t)(iq_ref - iq);
    m_int_d += qmul(ed, 800);
    m_int_q += qmul(eq, 800);
    m_int_d = sat(m_int_d);
    m_int_q = sat(m_int_q);
    m_ud = sat((int32_t)qmul(ed, 9000) + m_int_d);
    m_uq = sat((int32_t)qmul(eq, 9000) + m_int_q);
    ualpha = qmul(m_ud, c) - qmul(m_uq, s);
    ubeta = qmul(m_ud, s) + qmul(m_uq, c);
    m_id += qmul((int16_t)(ualpha - m_id), 2000);
    m_iq += qmul((int16_t)(ubeta - m_iq), 2000);
    m_ia = qmul(m_id, c) - qmul(m_iq, s);
    m_ib = qmul(m_id, s) + qmul(m_iq, c);
    m_speed += qmul((int16_t)(iq - qmul(m_speed, 3000)), 400);
    m_angle += (uint16_t)qmul(m_speed, 3000);
}
#endif

void __attribute__((interrupt, no_auto_psv)) _CCT1Interrupt(void)
{
    uint16_t t0 = CCP2TMRL;

    _CCT1IF = 0;
#if M0_FAST == 2
    m0_body();
#endif
    fast_ticks++;
    fast_total++;
    fast_cycles = CCP2TMRL - t0;
}
#endif

static void m0_init(void)
{
#if M0_FAST
    CCP2CON1L = 0;                      /* free running 16 bit timer */
    CCP2CON1H = 0;
    CCP2CON2L = 0;
    CCP2CON2H = 0;
    CCP2CON1Lbits.TMRPS = 1;            /* 1:4 */
    CCP2PRL = 0xFFFF;
    CCP2CON1Lbits.CCPON = 1;

    CCP1CON1L = 0;                      /* 16 bit timer on FP, IRQ on period match */
    CCP1CON1H = 0;
    CCP1CON2L = 0;
    CCP1CON2H = 0;
    CCP1PRL = (uint16_t)(SCOPE_FP_HZ / M0_FAST_HZ - 1u);
    _CCT1IP = M0_FAST_IPL;
    _CCT1IF = 0;
    _CCT1IE = 1;
    CCP1CON1Lbits.CCPON = 1;
#endif
}

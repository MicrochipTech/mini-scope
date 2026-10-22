/* xc.h mock - lets scope.c run on the PC for tests (see sim.c) */
#ifndef MOCK_XC_H
#define MOCK_XC_H
#include <stdint.h>

typedef struct { unsigned UTXBF, URXBE, UTXISEL; } stah_t;
typedef struct { unsigned OERR; } sta_t;
typedef struct { unsigned BRGH, UTXEN, URXEN, UARTEN; } mode_t_;
typedef struct { unsigned TON, TCKPS; } t1con_t;

stah_t *mock_stah(void);
uint16_t *mock_txreg(void);
uint16_t mock_rxreg(void);
void mock_reset(void);

extern sta_t   U1STAbits;
extern mode_t_ U1MODEbits;
extern t1con_t T1CONbits;
extern uint16_t U1MODE, U1MODEH, U1STA, U1STAH, U1BRG, U1BRGH, T1CON, TMR1, PR1;
extern volatile uint16_t TRISB, TRISC, ANSELB, ANSELC;
extern int _U1TXIF, _U1TXIE, _U1RXIF, _U1RXIE, _U1TXIP, _U1RXIP, _T1IF, _T1IE, _T1IP, _GIE;
extern uint8_t mock_ram[];

#define U1STAHbits (*mock_stah())
#define U1TXREG    (*mock_txreg())
#define U1RXREG    mock_rxreg()

#define SCOPE_ISR
#define SCOPE_ADDR(a)       ((volatile void *)(mock_ram + (a)))
#define SCOPE_PINS_INIT()   do { } while (0)
#define SCOPE_RESET()       mock_reset()

#endif

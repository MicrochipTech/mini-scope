/* vars.c - fixture for test_elf_vars.py: one variable for every case elf_vars.py has to handle.
 * Built with tests/fixtures/build.txt (xc-dsc, -g, no optimisation). Not a runnable program. */
#include <stdint.h>

typedef int16_t q15_t;                              /* typedef to a typedef chain */
typedef uint16_t word_t;
enum mode { MODE_OFF, MODE_ON, MODE_TEST = 7 };
typedef enum mode drive_mode_t;
enum signed_mode { SM_NEG = -1, SM_ZERO, SM_POS };

struct phase { q15_t current; word_t duty; };
struct flags { uint16_t ready : 1; uint16_t fault : 1; uint16_t level : 4; };

struct motor_s {
    word_t run;
    q15_t speed;
    drive_mode_t mode;
    struct phase phase[3];                          /* array of structs */
    struct { uint8_t a; int8_t b; } inner;          /* anonymous struct type, named member */
    union { uint16_t word; int16_t sword; };        /* anonymous union member */
    struct { uint8_t lo; uint8_t hi; };             /* anonymous struct member */
    struct flags flags;                             /* bit fields */
    float gain;
    int32_t pos;
    uint32_t ticks;
    long long wide;                                 /* 64 bit, refused */
    int *ptr;                                       /* pointer */
};

volatile struct motor_s motor;                      /* the volatile belongs to the whole struct */
struct motor_s plain;                               /* not volatile */
volatile struct motor_s *motor_p = &motor;          /* pointer to struct */
volatile int8_t trim;
volatile uint8_t bytes[4];
volatile int16_t grid[2][3];                        /* two dimensions */
const volatile uint16_t cv_ram = 3;                 /* const volatile chain */
volatile const q15_t cv_q15 = -5;
const uint16_t table_c[4] = { 1, 2, 3, 4 };         /* const data: flash */
const uint16_t limit_c = 9;
volatile enum signed_mode smode;
volatile drive_mode_t dmode;
volatile union { uint32_t l; uint16_t w[2]; } uv;
volatile long long big;                             /* 64 bit, refused */
volatile double dbl;
volatile float fl;
volatile _Bool flag;
volatile char ch;
volatile unsigned char uch;
volatile int8_t *bytes_p;
static volatile uint16_t counter;                   /* a second one is in vars2.c */
static volatile uint16_t only_here;                 /* static, unique */

extern uint16_t other_counter(void);

int main(void)
{
    counter = 1;
    only_here = 2;
    plain.run = other_counter();
    return motor_p->run + (int)(bytes_p != 0) + table_c[1] + limit_c + cv_ram + cv_q15;
}

/* vars2.c - second file of the fixture: a static with the same name as in vars.c. */
#include <stdint.h>

static volatile uint16_t counter;

uint16_t other_counter(void)
{
    return ++counter;
}

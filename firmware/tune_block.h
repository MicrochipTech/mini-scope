/*
 * tune_block.h - the few lines a firmware needs for the tuning scripts
 *
 * Include it once, in the file that holds your 1 ms task, and add two calls:
 *
 *   tune_block_init();                  once at start, before any interrupt is enabled
 *   if (tune_block_ms())                every 1 ms
 *       ...stop the drive...            the PC stopped sending the heartbeat
 *
 * What the scripts need from it, under exactly these names:
 *
 *   tune_alive     uint16_t. The PC changes it about every 200 samples (200 ms of target
 *                  time) while a script runs.
 *   tune_build_id  uint16_t. CRC-16 of the text tune_build, computed at start. The PC
 *                  computes the same CRC from the text in the ELF file and refuses to
 *                  write anything when the two differ: this is how it knows that the
 *                  ELF it resolves variable names from is the one programmed.
 *   tune_build     const text __DATE__ " " __TIME__ in flash, the time this file was
 *                  last compiled. It changes only when the file that includes this
 *                  header is compiled again: put it in a file that changes, or build clean.
 *
 * tune_block_ms() is armed by the first change of tune_alive. It returns 1 as soon as
 * tune_alive stayed unchanged for TUNE_HEARTBEAT_MS, and keeps returning 1 until tune_alive
 * changes again. Other writes do not arm it. It does not touch your variables: what "stop"
 * means (run = 0, torque reference 0, switch off the outputs) is your code.
 *
 * Test builds that keep these variables elsewhere define the macros tune_alive and
 * tune_build_id before the include, then no variable of that name is defined here.
 */
#ifndef TUNE_BLOCK_H
#define TUNE_BLOCK_H

#include <stdint.h>

#define TUNE_HEARTBEAT_MS   1000

#ifndef tune_alive
volatile uint16_t tune_alive;
#endif
#ifndef tune_build_id
volatile uint16_t tune_build_id;
#endif
const char tune_build[] = __DATE__ " " __TIME__;     /* "Nov  1 2026 12:34:56" */

static uint16_t tune_hb_last;
static uint8_t  tune_hb_armed;
static uint8_t  tune_hb_lost;
static uint16_t tune_hb_ms;

/* CRC-16/CCITT-FALSE: start value 0xFFFF, polynomial 0x1021, no reflection. "123456789" gives 0x29B1. */
static uint16_t tune_crc16(const char *s)
{
    uint16_t crc = 0xFFFF;
    uint8_t bit;

    while (*s) {
        crc ^= (uint16_t)((uint16_t)(uint8_t)*s++ << 8);
        for (bit = 0; bit < 8; bit++)
            crc = (uint16_t)((crc & 0x8000) ? (crc << 1) ^ 0x1021 : (crc << 1));
    }
    return crc;
}

static void tune_block_init(void)
{
    tune_build_id = tune_crc16(tune_build);
    tune_hb_last = tune_alive;
    tune_hb_armed = 0;
    tune_hb_lost = 0;
    tune_hb_ms = 0;
}

/* every 1 ms: 1 while the heartbeat is lost, else 0 */
static uint8_t tune_block_ms(void)
{
    uint16_t hb = tune_alive;

    if (hb != tune_hb_last) {
        tune_hb_last = hb;
        tune_hb_armed = 1;
        tune_hb_lost = 0;
        tune_hb_ms = 0;
    } else if (tune_hb_armed && !tune_hb_lost && ++tune_hb_ms >= TUNE_HEARTBEAT_MS) {
        tune_hb_lost = 1;
    }
    return tune_hb_lost;
}

#endif

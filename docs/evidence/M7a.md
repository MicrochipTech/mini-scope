# M7a: gaps of the simulator build

Base fb0d836 (PLL fix, `_POST2DIV = 1`). MPLAB X v6.35, XC-DSC, DFP dsPIC33CK-MC 1.10.386,
device dsPIC33CK64MC103. Simulation and reading only: nothing here proves anything on hardware.

Sources: data sheet DS70005399D ("DS"), FRM Oscillator Module with High-Speed PLL DS70005255B
("FRM osc"), FRM Multiprotocol UART DS70005288C ("FRM uart"), errata DS80000910A, device file
`edc/DSPIC33CK64MC103.PIC` of the DFP.

Commands, from the repository root:

```
python docs/evidence/m7a/m7a_sim.py rx          # receive path, SCL against mailbox
python docs/evidence/m7a/m7a_sim.py overrun     # RX FIFO overrun and recovery
python docs/evidence/m7a/m7a_sim.py tx          # TX path with the kicks taken out
python docs/evidence/m7a/m7a_compile.py <throwaway worktree>
```

`m7a_sim.py` builds configuration `sim` from a scratch copy in the temp folder, with the counters
`rx_hits` and `rx_bytes` of M0 added to `UxRX_ISR`. The worktree firmware is not changed.

## 1. Clock

`clock_init()` in `firmware/example_main.c` compared with DS section 9 and FRM osc section 13:

| Point | example_main.c | Source | Result |
|---|---|---|---|
| Start clock | `FNOSC = FRC` | DS p. 77: on every reset FNOSC is loaded into NOSC; Table 9-1 p. 157 | starts on FRC after POR and after the `reset` of command `X` |
| Clock switching | `FCKSM = CSECMD` | DS FOSC register p. 416, device file: switching on, FSCM off | fits |
| IESO, PLLKEN | not set, erased value 1 | device file: IESO = ON (two speed start, no effect with FNOSC = FRC); PLLKEN = ON, FRM osc p. 65 note 5: the switch waits for lock | fits |
| Dividers changed on the PLL? | set while COSC = FRC, before the switch | DS p. 152 note, Register 9-5 note 1 p. 164: not while the PLL runs | not violated, also not after `X` (reset restarts on FRC) |
| Limits | FPLLI 8 MHz, FPFD 8 MHz, FVCO 640 MHz, M = 80, N2 = 5 >= N3 = 1 | DS 9.1 p. 151: FPLLI 8..64, FPFD 8..FVCO/16, FVCO 400..1600; Register 9-3 p. 162: M 16..200 | inside |
| Frequency | FPLLO 128, FOSC 64, FCY = FP 32 MHz | DS Figure 9-2 p. 150, Equation 9-2 p. 152 | = `SCOPE_FP_HZ` |
| Unlock | `__builtin_write_OSCCONH(0x01)`, `__builtin_write_OSCCONL(OSCCON \| 0x01)` | DS 9.9 p. 157, FRM osc 13.3 p. 66 | same as DS Example 9-2 p. 153 |
| Interrupts during the unlock | none enabled yet (`scope_init()` comes after) | FRM osc p. 66 step 1 | fits |
| Wait loops | `while (OSCCONbits.OSWEN);` then `while (!OSCCONbits.LOCK);` | DS Register 9-1 p. 158/159: OSWEN 0 = switch complete, LOCK 1 = locked; FRM osc p. 65 | polarity and order as DS Example 9-2 |

Register values read in MDB after `clock_init()` (rx run): `CLKDIV=12289` (0x3001, PLLPRE 1),
`PLLFBD=80`, `PLLDIV=81` (0x51, POST1DIV 5, POST2DIV 1). The wait loops are compiled out in `sim`, so
the lock and switch bits are not tested. Errata DS80000910A item 4 concerns the VCO dividers
(FVCO/2, /3, /4, FVCODIV); the example uses FOSC and FP only, the UART uses FP (`BCLKSEL = 0`).

No mismatch. Changes:

- `TROUBLESHOOTING.md` 4.2: reset loop from a watchdog with `FWDTEN = ON` and from a missing trap
  handler. 4.3: the `hw_check.py` factor (`fp_factor`), 0.5 = FOSC half the assumed value, most
  likely `POST2DIV` set for FOSC = FPLLO; why such firmware can look fine (PWM on FPLLO); 0.125 = no
  PLL. PLL note: `PLLPRE` added, page cited, `FNOSC = FRCPLL` start needs a switch to FRC first.
- `tools/doctor.py` `check_settings()`: doctor already reads the dividers in `example_main.c` and
  fails on a mismatch. When the computed FP is exactly half of `SCOPE_FP_HZ`, the hint now names
  POST2DIV and section 4.3. It also accepts the spelling `PLLDIVbits.POST2DIV = n`. Checked by
  calling `check_settings()` with `_POST2DIV = 2` substituted in memory:
  `FAIL clock: ... FOSC 32 MHz, FP 16 MHz, SCOPE_FP_HZ 32 MHz -> FP is half of SCOPE_FP_HZ: FOSC is
  FPLLO / 2, so POST2DIV = 2 is probably set as if FOSC were FPLLO`. With the file as it is: `OK`.

## 2. Receive path

### Simulator, SCL `packetin` against the mailbox

Fresh MDB session per case. A single command byte is followed by a 0 byte so that the count is
even (see below); the firmware answers it with NAK (0x15).

```
--- UxRX_ISR          stimulus: P 0, W(ramp_step = 7), P 0
rx_hits= 6  rx_bytes= 12  ramp_step= 7  U1STA=128  U1STAH=28718  U1BRG=68
UART 16 bytes: 53 43 50 31 00 10 10 15 53 43 50 31 00 10 10 15
--- mailbox           the same bytes through sim_rx
rx_hits= 0  rx_bytes= 0  ramp_step= 7
UART 16 bytes after 2 from S: 53 43 50 31 00 10 10 15 53 43 50 31 00 10 10 15
identical: True
--- UxRX_ISR stream   C(ramp_step, 1000 us) R 0, 100 ms later W(ramp_step = 5), 100 ms later S 0
rx_hits= 14  rx_bytes= 28  ramp_step= 5  scope_overflows= 0
UART 1588 bytes, first 06 06, last 06 15
frames, lost, junk: (396, 0, 0)
ramp_step in the frames: [1, 5], first change at sample 197
```

Ping and W give byte for byte the same answers through `UxRX_ISR` as through the mailbox. A W during
a stream through the real receive path takes effect in the stream, with no lost frame.

Overrun: RX interrupt off, 12 bytes in (6 x `P 0`), interrupt on after 1.5 s, then 2 more bytes:

```
12 bytes in, RX interrupt off: rx_hits= 0 rx_bytes= 0 U1STA=128 U1STAH=28717
RX interrupt on:               rx_hits= 2 rx_bytes= 11 U1STA=128 U1STAH=28716
after 2 more bytes:            rx_hits= 3 rx_bytes= 13 U1STA=128
UART 55 bytes: 7 ping answers, 6 NAK
```

`U1STAH=28717` is 0x702D: URXBF = 1, URXBE = 0, FIFO full. 11 of 12 bytes were kept, and `OERR`
(U1STA bit 1) never got set. The firmware recovered and answered every complete command.

### Data sheet against scope.c

| Point | scope.c | Source | Result |
|---|---|---|---|
| RX interrupt level | `URXISEL = 0` (UxSTAH = 0) | DS Register 15-4 p. 278: 000 = one byte or more; FRM uart 11.1 p. 52 | one interrupt per byte on the device. **The interrupt per two bytes is simulator behaviour.** The ISR reads until `URXBE`, so it works with either. |
| FIFO | drained until `URXBE = 1` | DS p. 269: 8 deep; FRM uart 6.2.1 p. 36 | fits |
| Flag clearing | `UxRXIF = 0` first, then drain | FRM uart 11.1: clear the flag in the ISR | a byte that arrives during the drain sets the flag again: no lost wake up |
| OERR | cleared after the drain | DS Register 15-3 p. 277: cleared by software; FRM uart 6.2.2 p. 36: the buffer must be read at least once to clear it; `RUNOVF = 0`: the receiver stops while the buffer is full | the drain reads first, then clears: receiver resumes. Bytes lost in an overrun are lost; the parser resyncs only through NAK, as the protocol is |
| Error interrupts | `UxSTA = 0`: OERIE, FERIE, PERIE = 0, no error ISR | DS Register 15-4 note 1: the RX watermark interrupt is held back only if PERR or FERR is set **and** its IE bit is set | the RX interrupt keeps running on errors |
| Framing / parity | `MOD = 0`, 8N1, FERR not checked | FRM uart 6.2.2: FERR belongs to the top byte | a byte with a framing error (noise, adapter plugged in) reaches the parser as data: NAK or a wrong command, no hang. The tool's MCC driver reference does the same for read (below) |
| Priority | RX and TX at `SCOPE_UART_IPL` 1, sample timer 2 | | the RX ISR only calls `reply()` when the sample ISR does not put bytes: P, C, R and default NAK only in IDLE, S stops the timer before its ACK, W has no reply |
| PPS input | `RPINR18.U1RXR = SCOPE_RX_RP`, TX code 1 | DS p. 113 (inputs), p. 117 (U1TX = 1), RPCON.IOLOCK p. 122 | fits; unlock `RPCON = 0`, lock 0x0800 = IOLOCK bit 11 |
| ANSEL | bit cleared for the RX and TX pin | device file: ANSELB has bits 0..4, 7..9; RB5, RB6 have no analog function | harmless; needed for other pins |
| Enable order | MOD, BRGH, UTXISEL, BRG, then UARTEN, UTXEN, URXEN; IP, RXIF = 0, RXIE = 1, GIE = 1 | FRM uart 6.1.1 p. 34, 6.2.1 p. 36 | fits |
| Baud | BRG = FP / (4 baud) - 1 with BRGH = 1, `BCLKSEL = 0` (FP) | FRM uart Equation 4-2 p. 24, DS p. 274 | fits; BRG 68 seen in MDB |

Note, not a defect: `scope_init()` clears `UxRXIF` after `URXEN = 1`. A byte arriving in these few
cycles would wait in the FIFO until the next byte (if the flag is edge set). The PC sends pings
repeatedly, and the next byte drains both.

MCC generated UART code: none on this PC (searched the work folders, the user's documents, the
MCC content and pack folders for `uart*.c` and for templates with `UTXISEL`; only code for other
device families was found). For comparison one generated driver from a public Microchip example
repository was downloaded (UART1 driver 1.6.1, PLIB 1.4.1, for another dsPIC33CK). It is a polled
driver: init writes `UxMODE`, `UxMODEH`, `UxSTA`, `UxSTAH`, `UxBRG`, `UxBRGH`, then `UARTEN`,
`UTXEN`, `URXEN` (same order as scope.c); read waits for `URXBE = 0`, clears `OERR` if set and reads
`UxRXREG`; framing and parity are only reported. An interrupt driven MCC driver was not available.

## 3. TX interrupt

| Point | scope.c | Source | Result |
|---|---|---|---|
| Level | `UTXISEL = 7` | DS Register 15-4 p. 278 "one empty slot left"; FRM uart 6.1.1 p. 34: watermark from one to eight empty slots | flag while at least one slot is free |
| Flag at start | not relied on, `tx_kick()` sets it | FRM uart 11.1 p. 52: set when the module is first enabled, clear it in the ISR | fits |
| Clearing | `UxTXIF = 0` at entry, then fill until `UTXBF` | | each pass writes a byte or leaves; at most 8 writes; never writes into a full FIFO, so no TXWRE and no lost byte |
| Ring empty | `UxTXIE = 0`, check again, `UxTXIE = 1` if refilled | | a byte put by the sample ISR (IPL 2) between check and disable is caught; `tx_kick()` after every put sets IE again anyway |
| Software kick on a device | `tx_kick()` sets IF and IE | | if the FIFO is full, the extra ISR entry leaves at once with IE on, and the hardware flag brings it back when a slot frees: no hang, no storm |

The handler cannot hang unless `UTXBF` stays 0 with the UART off, which scope.c never does.

### Simulator, kicks taken out (one pass, this card)

Three builds, each in a fresh session: S, ping, a 1 ms stream of `ramp` for 40 s wall, a capture of
4096 bytes.

```
--- tx_base     (as committed: tx_kick sets IF and IE, model ISR sets U1TXIF every 100 us)
answers to S: 2 bytes, ping: 7 bytes 53 43 50 31 00 10 10
stream: 9699 bytes, frames/lost/junk (2424, 0, 0), scope_overflows= 0, head= 492, tail= 492
capture: 4103 bytes, header C5 00 08 00 00, payload 4096 of 4096, dump_left= 0
--- tx_nokick   (tx_kick sets only IE, model ISR kick removed)
answers to S: 0 bytes, ping: 0 bytes
stream: 0 bytes, scope_overflows= 1792, head= 511, tail= 0
capture: 0 bytes, dump_left= 4096
--- tx_level    (tx_kick sets only IE, model ISR sets U1TXIF only while !UTXBF)
answers to S: 2 bytes, ping: 7 bytes 53 43 50 31 00 10 10
stream: 3503 bytes, frames/lost/junk (875, 0, 0), scope_overflows= 0, head= 440, tail= 440
capture: 4103 bytes, header C5 00 08 00 00, payload 4096 of 4096, dump_left= 0
```

The simulator never sets `U1TXIF` by itself, not even when the UART is enabled: without kicks not
one byte leaves, the ring fills (head 511, tail 0) and stays full. `tx_level` imitates what the data
sheet says the device does (flag while a slot is free) and needs no IF write in `tx_kick()`:
everything arrives. The frame count differs because the simulator ran at a different speed in that
session; loss is 0 in both.

Variant for M7e (no `tx_kick`, no 10 kHz kick): the edits `TX_KICK` (scope.c: `tx_kick()` without
`UxTXIF = 1;`) and `NO_10K` (example_main.c: remove `TUNE_TXIF = 1;` from the model ISR) in
`m7a_sim.py`, built with `build('tx_nokick', main_edits=[NO_10K], scope_edits=[TX_KICK])`. The
expected result is no byte at all, as above.

## 4. Watchdog

scope.c has no `CLRWDT` and does not touch `WDTCONL`. `example_main.c` sets `FWDTEN = ON_SW` and
never sets `WDTCONL.ON`, so the watchdog is off. With `FWDTEN = ON` it is on in hardware and software
cannot turn it off (DS FWDT p. 417; device file: "WDT enabled in hardware"). The customer's main
loop then has to clear it.

Nothing in scope.c blocks: the capture dump runs in the TX ISR, at most 8 bytes per entry, and the
main loop runs between entries; W is one store; configure is a loop over at most `SCOPE_MAX_CH`
channels. The only way mini-scope can starve a main loop is a very short sample period, which
`TROUBLESHOOTING.md` section 7 already covers. No defect, no change.

## 5. Compile

Throwaway worktree outside the repository at fb0d836, Makefiles generated there, configuration `default`,
`make clean` and `make build` per row; the combination comes in through `MP_EXTRA_CC_PRE`. Each ELF
was checked for the vectors of its UART and timer. Nothing was programmed. The worktree was removed
and `git worktree prune` run (the known Permission denied on old entries in `.git/worktrees`).

| Combination | Exit | Warnings | Vectors |
|---|---|---|---|
| default (UART1, Timer1), no simulator symbols in the ELF | 0 | 0 | yes |
| UART1 with T1, CCP1, CCP2, CCP3, CCP4, NONE | 0 each | 0 | yes |
| UART2 with T1, CCP1, CCP2, CCP3, CCP4, NONE | 0 each | 0 | yes |
| UART3 with T1, CCP1, CCP2, CCP3, CCP4, NONE | 0 each | 0 | yes |

19 builds, 0 with an error, a warning or a missing vector, 71 s. The build in the MPLAB X IDE by the
user is still open.

## Not proven in simulation

- The clock wait loops (compiled out in `sim`), lock time, the real FRC and PLL frequency.
- One RX interrupt per byte, `OERR` on a real overrun, framing errors, the receive after an overrun.
- The TX interrupt of `UTXISEL = 7` set by the hardware, and that the software kick does no harm.
- Pins, PPS, baud rate on the wire, the watchdog with `FWDTEN = ON`.

## Baseline after the changes

```
gcc -O2 -shared -Itests/mock -Ifirmware -o tests/sim.dll tests/mock/sim.c firmware/scope.c   ok
python tests/test_protocol.py              56 OK, 0 FAIL
python tools/hw_check.py --port SIM --secs 5     Result: 11 OK, 0 WARN, 0 FAIL
python tests/test_script.py                507 OK, 0 FAIL, exit 0, 4 min 19 s
```

The rx and overrun passes ran once, in the first part of this card, on the firmware of fb0d836
(`PLLDIV=81` in the dump shows the fix is in). They were not repeated. The tx pass ran once in the
second part.

# mini-scope – notes for Claude Code

The typical situation: a customer cloned this project, built and programmed
`test.X` in MPLAB X, and the GUI does not work on their board. Your job is to
get mini-scope running on that board.

**Status:** mini-scope has run in the MPLAB X simulator and in a PC model,
**never on real hardware yet**. A fault can therefore be in mini-scope itself.
If the evidence points there, say so plainly and help the customer report it
(TROUBLESHOOTING.md section 9) instead of patching around it silently.

## Ground rules

- **The customer builds and programs in the MPLAB X IDE.** Do not build or
  program from the command line, and do not touch `test.X/nbproject/Makefile-*`
  or other generated files. After a change, tell the customer exactly what to
  do: "Make and Program Device" with configuration `default` (production, not
  Debug), then disconnect the PICkit, then which check to run. For build
  errors, ask for the build output from the MPLAB X Output window.
- **You cannot see the board.** Ask for concrete observations: which pin,
  which instrument, what value you expect. Do not assume wiring or clock.
- **One change at a time**, then check again. Keep track of what was tried.
- **Do not guess register details.** Pin functions, RP numbers and PPS codes
  come from the data sheet or from the device file in the pack
  (`<pack>\edc\<DEVICE>.PIC`, `tools/doctor.py` prints where the pack is).
  The pin table in README.md is for the dsPIC33CK64MC103 only.
- Customer settings belong in `firmware/scope.h` (or `-D` macros in the
  project) and in `firmware/example_main.c`. Changes to the protocol in
  `scope.c` must be mirrored in `gui/scope_gui.py`, `tests/mock/` and
  `tests/test_protocol.py` (see "Changing the code").

## Procedure

1. **PC and project:** `.venv\Scripts\python tools\doctor.py` (any Python
   3.9+ works if `.venv` does not exist yet). It checks Python, MPLAB X,
   XC-DSC, the device pack, the build, the effective `scope.h` settings
   (pins, clock, baud rate) and the COM ports, and writes
   `doctor_report.txt`.
2. **Ask** what doctor cannot know: which board and device, which USB-UART
   adapter (3.3 V?), which pins it is wired to, PICkit disconnected,
   production build programmed, what the GUI status line says.
3. **Target:** close the GUI connection first (the COM port is exclusive),
   then `.venv\Scripts\python tools\hw_check.py --port COMx`. It writes
   `hw_report.txt`. If not a single byte comes back, check the adapter alone
   with `--loopback` (adapter TX and RX bridged, target disconnected).
4. Every FAIL/WARN in the reports names the section of `TROUBLESHOOTING.md`
   to work through. Follow it in order; it is the decision tree.
5. Done when `hw_check.py` shows no FAIL on the customer's board.

## What hw_check proves

| Check | Proves | Typical cause when it fails |
|---|---|---|
| port | the COM port opens | wrong port, GUI or terminal still open |
| ping | firmware runs, UART pins and baud rate fit | wiring, PICkit attached, debug build, PLL wait hangs, pins reconfigured after `scope_init()` |
| ping at another baud | firmware runs, but the clock is wrong | `SCOPE_FP_HZ` ≠ FOSC/2, PLL not running; the factor is printed |
| clock | sample clock against the PC clock, ±0.2 % | FRC tolerance, wrong PLL setup; above 3 % the UART becomes unreliable |
| consistency | the demo `counter` increases steadily | torn 32-bit reads, main loop stalled |
| stream x us | the UART carries the expected rate without losses | TX interrupt, other interrupts above priority 2 blocking mini-scope |
| capture / trigger | buffer, 10 µs sampling, edge trigger with pretrigger | interrupt load, trigger logic |
| reset | command `X` restarts the firmware, also during a stream | firmware without `X`, slow start-up |

The demo variables (`counter`, `ramp`) come from the ELF of `test.X`. With
the customer's own firmware, pass `--elf`; without these variables only the
checks that need no variables run.

## When the customer's board differs from the test board

`test.X` is set up for a **dsPIC33CK64MC103**, FRC + PLL to FOSC 64 MHz
(FP 32 MHz), UART1 on RB5/RB6 (= ICSP channel 3, `ICS = PGD3`), PICkit 5.

- **Other pins:** set `SCOPE_TX_RP` / `SCOPE_RX_RP` (RB*n* = RP(32+*n*),
  RC*n* = RP(48+*n*)). If the pins are not on the ICSP channel, the customer
  can debug and use mini-scope at the same time.
- **Other clock:** `SCOPE_FP_HZ` must equal FOSC/2. doctor only evaluates the
  clock code in `example_main.c`; with the customer's own clock code, work it
  out from their code and confirm it with hw_check (ping at another baud,
  clock check).
- **Other device of the dsPIC33CK64MC10x family** (32/64 KB, 102…106): the
  customer changes the device in the project properties. Check that the
  chosen pins exist in that package.
- **Another dsPIC33CK (for example the MP family):** this is a port, not a
  setting. `scope_pin_dir()` and the range check in `scope.h` assume
  RP32…53 on ports B and C; PPS output codes, input registers and RAM size
  can differ between family members. Take every value from the device's
  `.PIC` file or data sheet, never from this family. Tell the customer that
  this is untested territory.
- **Other programmer:** the customer selects it in the project properties.

## Changing the code

- Layout: `firmware/` (scope.c/h, example_main.c), `gui/scope_gui.py`
  (NiceGUI, the `Scope` class is the protocol client), `tests/` (PC model:
  scope.c with mock registers as `sim.dll`), `tools/` (doctor, hw_check,
  mdb_serial for the simulator), `test.X` (configurations `default` and
  `sim`).
- After every change to `scope.c` or the protocol, run the PC model; it
  needs gcc (MinGW) and takes seconds:

  ```
  gcc -O2 -shared -Itests/mock -Ifirmware -o tests/sim.dll tests/mock/sim.c firmware/scope.c
  .venv\Scripts\python tests\test_protocol.py
  .venv\Scripts\python tools\hw_check.py --port SIM --secs 5
  ```

- The firmware must still build in MPLAB X for both configurations; the
  customer confirms that.

## The simulator is a reference, not proof

`start_sim.bat` runs the real firmware in the MPLAB X simulator (details in
SIMULATOR.md). It shows whether the logic is right, nothing about timing,
pins or the clock. Its peculiarities, so they are not mistaken for bugs:

- The simulated UART cannot receive; commands go through a RAM mailbox
  (`sim_rx` in `example_main.c`), written by `tools/mdb_serial.py`.
- The TX interrupt is not raised again after the FIFO was filled, so every
  path through the sampling interrupt must call `tx_kick()`. A missing kick
  stalls the stream for good once the TX ring is full.
- `GIE` is 0 after reset; the first mailbox write after `Run` gets lost; the
  simulator runs about 25 times slower than real time and its UART output
  file lags by a few seconds.

## Handing over

When it works, or when you are stuck, collect for the customer:
`doctor_report.txt`, `hw_report.txt`, the list of changes made, and the items
of TROUBLESHOOTING.md section 9. That is what is needed to report a problem.

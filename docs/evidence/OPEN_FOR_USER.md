# Open for the user

Questions and points that need the user. The supervisor fills this list.

## Needs you before a customer sees this

1. **Build `default` in the MPLAB X IDE.** M7a compiled `default` and all 18 combinations from the
   command line in a throwaway copy, 0 warnings. The IDE build itself (Make and Program Device,
   configuration `default`, production) has not been done. Please build once and say whether the
   Output window is clean.
2. **Look at the GUI.** The Script tab, the comparison table, the overlay, Export `tuning.h` and
   Report were checked by tests and screenshots only (`docs/evidence/m4/`). Please run
   `start_sim.bat` once and say whether the layout and wording are right for a customer.
3. **German short guide.** It is kept outside this repository. M7.1 updates it; please read it
   before it goes out.
4. **Approve push and merge.** Nothing is pushed or merged. `tuning-scripts` holds only accepted work.

## Facts that change what the customer was told

5. **Clock of the example firmware was wrong (fixed, fb0d836).** `_POST2DIV = 2` gave FOSC 32 MHz,
   not 64 MHz, because FOSC = FPLLO / 2. On a real chip mini-scope would have run at half the baud
   rate. Any copy of the old example firmware, and any document that repeats "FOSC 64 MHz with
   POST2DIV = 2", is wrong. Please check what was already sent out.
6. **Capture periods below 10 µs are now refused (M7b-2).** Below 50 µs a capture may not hold its
   period under interrupt load; it then gives wrong spacing without any error. Measured in the
   simulator only.

## Gaps that stay open (recorded, not fixed)

7. Rare events on `MDB` with unknown cause (4.4b): a stream stop in the probe, `E401` on the first
   ping, a browser page that went away during a run. Each ended safely. `E401` came back once in the
   final repeat of `test_gui_mdb.py` and was handled by the one counted retry; the others did not recur.
8. A wrong write width of 16 bits is caught by one test only (`test_script`), and `hw_check` does
   not see a 32 bit write width (M7e F2, F3). Other tests catch both.
9. On `MDB` a run closed by the GUI writes the safe value at once, but `run.txt` has no read-back,
   because the read-back takes about 30 s there (4.4).

## Needs hardware (M8, M9)

10. mini-scope has never run on a real chip. Still unproven: clock wait loops and the real PLL
    frequency, one RX interrupt per byte, UART error flags, the hardware TX interrupt, pins and baud
    on the wire, the watchdog with `FWDTEN = ON`, timing under real interrupt load, and every script
    on a real drive. M8 (board without motor current) and M9 (motor) are with the customer.

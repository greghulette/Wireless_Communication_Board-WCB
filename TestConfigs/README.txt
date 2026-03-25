WCB TEST CONFIGURATION FILES
=============================

OVERVIEW
--------
Each file contains a pre-built configuration string for a specific board and
test scenario. Paste the PASTE line directly into the board's terminal in the
wizard. Commands are chained with ^ (the default delimiter) and execute in
sequence. The string ends with ?config to print the final state for verification.

These are SETUP SCRIPTS, not ?backup restore strings. They do not contain a
CRC checksum. After loading a config and verifying it looks correct, run
?backup on each board to capture the validated restore string for that state.


FILES
-----
Config 01 — Base Identity + Network
  WCB1_01_base.txt    Phases 2, 3, 3B, 4, 10, 12, 13
  WCB2_01_base.txt    Phases 2, 3, 3B, 4, 10, 12, 13
  WCB3_01_base.txt    Phases 2, 3, 3B, 4, 10, 12, 13

  What it sets up:
    - Hardware version, WCB number, WCB quantity
    - MAC octets, ESP-NOW password
    - ETM ON with standard parameters (HB=10s, MISS=3, TIMEOUT=500ms, CHKSM=ON)
    - Baud rates matched to physically connected hardware
    - Port labels
    - Broadcast reset to all-on defaults
    - All device configs and mappings CLEARED (Kyber, MP3, Maestro, serial, PWM)

  Use for: ETM/broadcast/serial command testing where device configs would
  interfere, and for any test that needs a known clean starting state.


Config 02 — Full Feature Setup
  WCB1_02_full.txt    Phases 5, 6, 7, 8, 11, 14
  WCB2_02_full.txt    Phases 5, 9, 11, 14
  WCB3_02_full.txt    Phases 5, 8, 9, 11, 14

  What it sets up (everything in base PLUS):
    WCB1: Kyber LOCAL (S2+S3), MP3 Trigger (S5), raw serial mapping S5<->W3S2,
          Maestro routing for M1/M2/M3 (all remote — routes to WCB2/WCB3)
    WCB2: Maestro M1 LOCAL (S1), Maestro M2 LOCAL (S2), Maestro M3 REMOTE (W3S1),
          PWM mapping S3->S4 local + W3S5 remote
    WCB3: Maestro M3 LOCAL (S1), Maestro M1/M2 REMOTE (W2S1/W2S2),
          raw serial mapping S2<->W1S5, PWM output S5

  Use for: All device-level testing, backup/restore round-trip, edge cases.


HOW TO LOAD A CONFIG
---------------------
1. In the wizard, open the terminal for the board you want to configure
2. Open the appropriate .txt file in any text editor
3. Find the line that starts with "PASTE:"
4. Copy everything AFTER "PASTE: " (the entire command chain on that line)
5. Paste into the terminal and press Enter
6. Watch each command execute — errors will print inline
7. The final ?config output shows the complete resulting configuration


AFTER LOADING — CAPTURE THE BACKUP STRING
------------------------------------------
Once you have verified the config looks correct with ?config, run ?backup.
Copy the output string (including the ?CHK... checksum at the end) and save it
as a separate file. That ?backup string is the true validated restore string
that will be accepted by the firmware with CRC verification.

Suggested backup filename format:  WCB1_02_full_BACKUP_v6.0.txt


NETWORK SETTINGS
-----------------
The files use these values from the test system:
  MAC group 2  : D3
  MAC group 3  : 3B
  ESP-NOW pass : Jq6SECq8dsNRmq

If your system uses different values, edit the PASTE line in each file before
using it. Change ?MAC,2,D3 / ?MAC,3,3B / ?EPASS,Jq6SECq8dsNRmq accordingly.
All boards in a system must share the same MAC octets and password.


HARDWARE VERSIONS IN THESE FILES
----------------------------------
  WCB1 : v2.1  (HW=21)
  WCB2 : v2.4  (HW=24)
  WCB3 : v2.3  (HW=23)

If you swap hardware, update the ?HW,xx value in the PASTE line.

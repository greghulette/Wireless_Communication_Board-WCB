#ifndef WCB_HELP_H
#define WCB_HELP_H

#include <Arduino.h>

void printCommandHelp(const String &cmd);

#endif


// ============================================================
// WCB COMPLETE COMMAND REFERENCE
// Last Updated: v6.0_251308FEB2026
// ============================================================
//
// LOCAL FUNCTION COMMANDS (prefix: ?)
// ------------------------------------------------------------
// INITIAL SETUP
//   ?HW,x                    Set hardware version
//                              1=v1.0, 21=v2.1, 23=v2.3, 24=v2.4, 31=v3.1, 32=v3.2
//   ?WCB,x                   Set this board's WCB number (1-9)
//   ?WCBQ,x                  Set total number of WCB boards in system (1-9)
//   ?MAC,2,xx                Set 2nd MAC group octet (hex, all boards must match)
//   ?MAC,3,xx                Set 3rd MAC group octet (hex, all boards must match)
//   ?EPASS,password          Set ESP-NOW network password (all boards must match)
//
// SERIAL PORTS
//   ?BAUD,Sx,rate            Set baud rate for serial port x
//                              Valid: 110,300,600,1200,2400,9600,14400,19200,
//                                     38400,57600,115200,128000,256000
//   ?LABEL,Sx,text           Set a human-readable label for serial port x
//   ?LABEL,CLEAR,Sx          Clear label for port x
//   ?LABEL,CLEAR,ALL         Clear all port labels
//   ?BCAST,IN,Sx,ON/OFF      Enable/disable broadcast input from port x
//   ?BCAST,OUT,Sx,ON/OFF     Enable/disable broadcast output to port x
//   ?BCAST,RESET             Reset all broadcast settings to defaults
//
// SERIAL & PWM MAPPING
//   ?MAP,SERIAL,Sx,dest      Map serial port x to destination (string mode)
//   ?MAP,SERIAL,Sx,R,dest    Map serial port x to destination (raw/binary mode)
//   ?MAP,SERIAL,LIST         List all active serial mappings
//   ?MAP,SERIAL,CLEAR,Sx     Remove serial mapping for port x
//   ?MAP,SERIAL,CLEAR,ALL    Remove all serial mappings
//   ?MAP,PWM,Sx,dest         Map PWM input port x to destination
//   ?MAP,PWM,OUT,Sx          Designate port x as PWM output only
//   ?MAP,PWM,LIST            List all active PWM mappings
//   ?MAP,PWM,CLEAR,Sx        Remove PWM mapping for port x
//   ?MAP,PWM,CLEAR,ALL       Remove all PWM mappings
//   ?MAP,CLEAR,ALL           Remove ALL serial and PWM mappings
//                              dest format: Sx=local port, WxSy=remote WCB x port y
//
// DEVICES
//   ?KYBER,LOCAL             Kyber physically connected to this board (S2)
//   ?MAESTRO,LOCAL,Sx,Mx:WxSx:baud   Configure a Maestro controller
//                              Mx=Maestro ID, WxSx=WCB+port, baud=baud rate
//                              Can chain multiple: M1:W1S1:57600,M2:W2S1:57600
//   ?KYBER,REMOTE            Maestro local, Kyber on another board
//   ?KYBER,CLEAR             Disable Kyber integration
//   ?KYBER,LIST              Show current Kyber configuration
//   ?MAESTRO,LIST            List all configured Maestro mappings
//   ?MAESTRO,CLEAR,x         Clear Maestro config by ID number
//   ?MAESTRO,CLEAR,ALL       Clear all Maestro configurations
//   ?MAESTRO,ENABLE          Set S1 baud to 57600 for Maestro use
//   ?MAESTRO,DISABLE         Restore S1 to original baud rate
//
// MP3 TRIGGER
//   Only needed for DIRECT control of the MP3 Trigger by the WCB itself.
//   If another controller (e.g. Marcduino) is sending MP3 commands through
//   the WCB via broadcast or serial mapping, do NOT use these commands --
//   just configure the serial port baud rate normally with ?BAUD.
//
//   ?MP3,Sx:<baud>:V<vol>    Configure SparkFun MP3 Trigger on serial port x
//                              Sx=serial port (S1-S5), baud=baud rate, vol=0-64
//                              0=loudest, 64=inaudible. S3-S5 max 57600 baud
//                              Example: ?MP3,S2:9600:V0
//   ?MP3,ONERR,key           Set stored sequence key to run on MP3 error response
//   ?MP3,CLEAR               Remove MP3 Trigger config and restore port to normal
//
// NETWORK - ETM
//   ?ETM,ON                  Enable Ensured Transmission Mode
//   ?ETM,OFF                 Disable ETM (all boards must match)
//   ?ETM,TIMEOUT,ms          ACK wait time before retry (default: 500ms)
//   ?ETM,HB,sec              Heartbeat interval in seconds (default: 10)
//   ?ETM,MISS,count          Missed heartbeats before board marked offline (default: 3)
//   ?ETM,BOOT,sec            Random boot heartbeat window in seconds (default: 2)
//   ?ETM,COUNT,n             Characterization message count per phase (10-200, default: 20)
//   ?ETM,DELAY,ms            Characterization inter-message delay (default: 100ms)
//   ?ETM,CHAR                Run network characterization test
//   ?ETM,CHKSM,ON            Enable Checksum on commands
//   ?ETM,CHKSM,OFF           Disable Checksum on commands
//
// DEBUG
//   ?DEBUG,ON                Enable main debug output
//   ?DEBUG,OFF               Disable main debug output
//   ?DEBUG,ETM,ON/OFF        Enable/disable ETM packet debug
//   ?DEBUG,PWM,ON/OFF        Enable/disable PWM passthrough debug
//   ?DEBUG,MAESTRO,ON/OFF    Enable/disable Maestro/Pololu serial debug
//
// STATISTICS
//   ?STATS                   Show ESP-NOW transmission statistics
//   ?STATS,RESET             Reset all statistics counters
//
// COMMAND SEQUENCES
//   ?SEQ,SAVE,key,value      Save a named command sequence to NVS
//   ?SEQ,LIST                List all saved sequences and contents
//   ?SEQ,CLEAR,key           Delete a stored sequence by name
//   ?SEQ,CLEAR,ALL           Delete all stored sequences
//
// SYSTEM CHARACTERS
//   ?DELIM,x                 Set command delimiter character (default: ^)
//   ?FUNCCHAR,x              Set local function identifier (default: ?)
//   ?CMDCHAR,x               Set command character prefix (default: ;)
//
// SYSTEM
//   ?config                  Print full configuration
//   ?backup                  Print backup/restore command string
//   ?reboot                  Reboot this board
//   ?ERASE,NVS               Erase ALL NVS settings and reboot (irreversible!)
//   ??                       Show help menu
//   ?COMMAND?                Show detailed help for a specific command
//
// ------------------------------------------------------------
// COMMAND CHARACTER COMMANDS (prefix: ;)
// ------------------------------------------------------------
//   ;Sx,message              Send message to local serial port x (1-5)
//   ;Wx,message              Send message to WCB x via ESP-NOW unicast
//   ;Ckey                    Run stored sequence named key
//   ;SEQkey                    Run stored sequence named key
//   ;Mx,script               Trigger Maestro x script number
//   ;Tms,command             Wait ms milliseconds then execute command
//                              Used standalone or chained in sequences
//                              Example: ;T500,CMD1^;T1000,CMD2
//
// ------------------------------------------------------------
// BROADCAST COMMANDS (no prefix)
// ------------------------------------------------------------
//   message                  Broadcast to all serial ports and ESP-NOW
//                              Respects per-port broadcast enable/disable settings
//                              Kyber and PWM ports are automatically excluded
//
// ------------------------------------------------------------
// LEGACY COMMANDS (still supported, use new format going forward)
// ------------------------------------------------------------
//   ?DON / ?DOFF             Debug on/off
//   ?DKON / ?DKOFF           Kyber debug on/off
//   ?DPWMON / ?DPWMOFF       PWM debug on/off
//   ?DETMON / ?DETMOFF       ETM debug on/off
//   ?HWx                     Set hardware version  (e.g. ?HW31)
//   ?WCBx                    Set WCB number        (e.g. ?WCB2)
//   ?WCBQx                   Set WCB quantity      (e.g. ?WCBQ2)
//   ?M2xx / ?M3xx            Set MAC octets        (e.g. ?M2AB)
//   ?EPASSpassword           Set ESP-NOW password
//   ?Dx                      Set delimiter         (e.g. ?D^)
//   ?LFx                     Set function identifier
//   ?CCx                     Set command character
//   ?BAUDSx,rate             Set baud rate         (e.g. ?BAUDS1,57600)
//   ?SLSx,label              Set serial label      (e.g. ?SLS1,Marcduino)
//   ?SLCx                    Clear serial label
//   ?SBISxON/OFF             Broadcast input       (e.g. ?SBIS3OFF)
//   ?SBOSxON/OFF             Broadcast output      (e.g. ?SBOS2OFF)
//   ?RESET_BROADCAST         Reset broadcast settings
//   ?SMSx,dest               Serial map string mode
//   ?SMSxR,dest              Serial map raw mode
//   ?SMRSx                   Remove serial mapping
//   ?SMLIST                  List serial mappings
//   ?SMCLEAR                 Clear all serial mappings
//   ?PMSSx,dest              PWM map
//   ?POSx                    PWM output port
//   ?PRSx                    Remove PWM mapping
//   ?PLIST                   List PWM mappings
//   ?PCLEAR                  Clear all PWM mappings
//   ?KYBER_LOCAL             Kyber local
//   ?KYBER,LOCAL,Sx,Mx:WxSx:baud,...
//                            Combined: set Kyber local + configure all Maestros
//                              Generates copy-paste setup commands for all other WCBs
//   ?KYBER_REMOTE            Kyber remote
//   ?KYBER_CLEAR             Kyber clear
//   ?KYBER_LIST              Kyber list
//   ?MAESTRO_ENABLE          Enable Maestro on S1
//   ?MAESTRO_DISABLE         Disable Maestro on S1
//   ?MAESTRO_LIST            List Maestro configs
//   ?MAESTRO_CLEAR,x         Clear Maestro by ID
//   ?MAESTRO_DEFAULT         Clear all Maestros
//   ?MAESTROconfig           Configure Maestro (old format)
//   ?ETMON / ?ETMOFF         ETM on/off
//   ?ETMTIMEOUTms            ETM timeout        (e.g. ?ETMTIMEOUT300)
//   ?ETMHBsec                ETM heartbeat      (e.g. ?ETMHB10)
//   ?ETMMISScount            ETM missed count   (e.g. ?ETMMISS3)
//   ?ETMBOOTsec              ETM boot window    (e.g. ?ETMBOOT2)
//   ?ETMCHARCOUNTn           ETM char count     (e.g. ?ETMCHARCOUNT20)
//   ?ETMCHARDELAYms          ETM char delay     (e.g. ?ETMCHARDELAY100)
//   ?ETMCHAR                 Run characterization
//   ?STATSRESET              Reset stats
//   ?CSkey,value             Save sequence      (e.g. ?CSwave,CMD1^CMD2)
//   ?CEkey                   Clear sequence     (e.g. ?CEwave)
//   ?CCLEAR                  Clear all sequences
//   ?WCB_ERASE               Erase NVS flash
// ============================================================

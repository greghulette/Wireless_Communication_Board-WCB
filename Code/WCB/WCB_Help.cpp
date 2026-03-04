#include "WCB_RemoteTerm.h"  // Must be first — redirects Serial → WCBDebugSerial
#include "WCB_Help.h"
#include "WCB_Storage.h"

extern char commandDelimiter;
extern char LocalFunctionIdentifier;
extern char CommandCharacter;


void printCommandHelp(const String &cmd) {
    String c = cmd;
    c.trim();
    c.toUpperCase();

    // ================================================================
    if (c == "DEBUG") {

        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?DEBUG,<command>[,value]"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Controls debug output for various subsystems. Debug output is"));
        Serial.println(F("  printed to the USB serial monitor. Enabling debug on a busy"));
        Serial.println(F("  system can produce a lot of output and may affect performance."));
        Serial.println(F("\nCommands:"));
        Serial.println(F("  ON                Enable main debug output"));
        Serial.println(F("  OFF               Disable main debug output"));
        Serial.println(F("  KYBER,ON          Enable Kyber serial forwarding debug"));
        Serial.println(F("  KYBER,OFF         Disable Kyber serial forwarding debug"));
        Serial.println(F("  PWM,ON            Enable PWM passthrough debug"));
        Serial.println(F("                      Shows pulse widths and output routing"));
        Serial.println(F("  PWM,OFF           Disable PWM passthrough debug"));
        Serial.println(F("  ETM,ON            Enable ETM packet/ACK/retry debug"));
        Serial.println(F("                      Shows sequence numbers, ACKs, retries"));
        Serial.println(F("  ETM,OFF           Disable ETM debug"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?DEBUG,ON"));
        Serial.println(F("  ?DEBUG,ETM,ON"));
        Serial.println(F("  ?DEBUG,PWM,OFF"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Debug settings are NOT saved to NVS and reset on reboot"));
        Serial.println(F("  - ETM debug is very verbose during characterization runs"));
        Serial.println(F("  - PWM debug will print for every pulse received"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?DON / ?DOFF           - Main debug"));
        Serial.println(F("  ?DKON / ?DKOFF         - Kyber debug"));
        Serial.println(F("  ?DPWMON / ?DPWMOFF     - PWM debug"));
        Serial.println(F("  ?DETMON / ?DETMOFF     - ETM debug"));

    // ================================================================
    } else if (c == "MAP") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?MAP,<type>,<options>"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Configures how data is routed between serial ports, both locally"));
        Serial.println(F("  and across WCB boards via ESP-NOW. Two mapping types exist:"));
        Serial.println(F("  SERIAL maps string commands from one port to destinations."));
        Serial.println(F("  PWM maps RC pulse-width signals from an input port to outputs."));
        Serial.println(F("  Mappings are saved to NVS and restored on reboot."));
        Serial.println(F("\nSERIAL Commands:"));
        Serial.println(F("  SERIAL,Sx,dest        Map port x to destination (string/command mode)"));
        Serial.println(F("                          Data is forwarded as text strings"));
        Serial.println(F("  SERIAL,Sx,R,dest      Map port x to destination (raw/binary mode)"));
        Serial.println(F("                          Raw bytes forwarded, no string processing"));
        Serial.println(F("  SERIAL,LIST           List all active serial mappings"));
        Serial.println(F("  SERIAL,CLEAR,Sx       Remove mapping for port x"));
        Serial.println(F("  SERIAL,CLEAR,ALL      Remove all serial mappings"));
        Serial.println(F("\nPWM Commands:"));
        Serial.println(F("  PWM,Sx,dest           Map PWM input port x to destination"));
        Serial.println(F("                          Triggers reboot to apply pin changes"));
        Serial.println(F("  PWM,OUT,Sx            Designate port x as PWM output only"));
        Serial.println(F("                          Used on remote boards receiving PWM"));
        Serial.println(F("  PWM,LIST              List all active PWM mappings"));
        Serial.println(F("  PWM,CLEAR,Sx          Remove PWM mapping for port x"));
        Serial.println(F("  PWM,CLEAR,ALL         Remove all PWM mappings"));
        Serial.println(F("\nCombined:"));
        Serial.println(F("  CLEAR,ALL             Remove ALL serial and PWM mappings"));
        Serial.println(F("\nDestination format:"));
        Serial.println(F("  Sx                    Local serial port x (1-5)"));
        Serial.println(F("  WxSy                  Remote WCB x, serial port y"));
        Serial.println(F("  Multiple destinations can be comma separated"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?MAP,SERIAL,S3,S4              - Forward S3 to local S4"));
        Serial.println(F("  ?MAP,SERIAL,S3,W2S1            - Forward S3 to WCB2 S1"));
        Serial.println(F("  ?MAP,SERIAL,S3,R,S4            - Forward S3 raw to S4"));
        Serial.println(F("  ?MAP,PWM,S3,S4                 - PWM passthrough S3 to S4"));
        Serial.println(F("  ?MAP,PWM,S3,W2S1               - PWM passthrough S3 to WCB2 S1"));
        Serial.println(F("  ?MAP,PWM,OUT,S4                - Set S4 as PWM output"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - PWM input ports cannot be used for normal serial communication"));
        Serial.println(F("  - S1/S2 cannot be used for PWM when Kyber is configured"));
        Serial.println(F("  - Remote PWM destinations auto-configure the remote board"));
        Serial.println(F("  - PWM mappings trigger a reboot to apply hardware changes"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?SMSx,dest    / ?SMSxR,dest    - Serial map string/raw"));
        Serial.println(F("  ?SMRSx        - Remove serial mapping"));
        Serial.println(F("  ?SMLIST       - List serial mappings"));
        Serial.println(F("  ?SMCLEAR      - Clear all serial mappings"));
        Serial.println(F("  ?PMSSx,dest   - PWM map"));
        Serial.println(F("  ?POSx         - PWM output port"));
        Serial.println(F("  ?PRSx         - Remove PWM mapping"));
        Serial.println(F("  ?PLIST        - List PWM mappings"));
        Serial.println(F("  ?PCLEAR       - Clear all PWM mappings"));

    // ================================================================
    } else if (c == "BAUD") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?BAUD,Sx,<rate>"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Sets the baud rate for a serial port and saves it to NVS."));
        Serial.println(F("  Takes effect immediately without reboot."));
        Serial.println(F("  Hardware serial (S1, S2) supports all standard rates."));
        Serial.println(F("  Software serial (S3, S4, S5) works best at 9600-57600."));
        Serial.println(F("\nParameters:"));
        Serial.println(F("  Sx            Serial port number (S1-S5)"));
        Serial.println(F("  rate          Baud rate - valid values:"));
        Serial.println(F("                  110, 300, 600, 1200, 2400, 9600, 14400,"));
        Serial.println(F("                  19200, 38400, 57600, 115200, 128000, 256000"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?BAUD,S1,57600     - Set S1 to 57600 (Maestro default)"));
        Serial.println(F("  ?BAUD,S2,9600      - Set S2 to 9600"));
        Serial.println(F("  ?BAUD,S3,38400     - Set S3 to 38400"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Default baud rate for all ports is 9600"));
        Serial.println(F("  - Maestro servo controllers require 57600"));
        Serial.println(F("  - Use ?MAESTRO,ENABLE as a shortcut for S1 Maestro setup"));
        Serial.println(F("  - Saved to NVS, persists across reboots"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?BAUDSx,rate  (e.g. ?BAUDS1,57600)"));
        Serial.println(F("  ?Sx,rate      (deprecated, will be removed)"));

    // ================================================================
    } else if (c == "LABEL") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?LABEL,Sx,<text>  /  ?LABEL,CLEAR,Sx  /  ?LABEL,CLEAR,ALL"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Assigns a human-readable label to a serial port. Labels appear"));
        Serial.println(F("  in debug output and config listings to make it easier to identify"));
        Serial.println(F("  which device is connected to which port."));
        Serial.println(F("\nCommands:"));
        Serial.println(F("  Sx,text           Set label for port x"));
        Serial.println(F("                      Maximum 30 characters"));
        Serial.println(F("  CLEAR,Sx          Remove label from port x"));
        Serial.println(F("  CLEAR,ALL         Remove all port labels"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?LABEL,S1,Marcduino"));
        Serial.println(F("  ?LABEL,S2,MP3 Trigger"));
        Serial.println(F("  ?LABEL,S3,Periscope"));
        Serial.println(F("  ?LABEL,CLEAR,S1"));
        Serial.println(F("  ?LABEL,CLEAR,ALL"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Labels are saved to NVS and persist across reboots"));
        Serial.println(F("  - Labels appear in ?CONFIG and ?BACKUP output"));
        Serial.println(F("  - Labels appear in debug serial output when enabled"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?SLSx,label   - Set label  (e.g. ?SLS1,Marcduino)"));
        Serial.println(F("  ?SLCSx        - Clear label (e.g. ?SLC1)"));

    // ================================================================
    } else if (c == "BCAST") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?BCAST,<IN/OUT>,Sx,<ON/OFF>  /  ?BCAST,RESET"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Controls broadcast behavior per serial port. Two independent"));
        Serial.println(F("  settings exist per port: INPUT controls whether commands received"));
        Serial.println(F("  on that port get broadcast to other ports and ESP-NOW. OUTPUT"));
        Serial.println(F("  controls whether broadcast commands get sent OUT that port."));
        Serial.println(F("\nCommands:"));
        Serial.println(F("  IN,Sx,ON          Allow broadcasts from port x input"));
        Serial.println(F("                      Commands received on Sx will be broadcast"));
        Serial.println(F("  IN,Sx,OFF         Block broadcasts from port x input"));
        Serial.println(F("                      Commands received on Sx stay local only"));
        Serial.println(F("  OUT,Sx,ON         Include port x in broadcast output"));
        Serial.println(F("                      Broadcast commands will be sent to Sx"));
        Serial.println(F("  OUT,Sx,OFF        Exclude port x from broadcast output"));
        Serial.println(F("                      Broadcast commands will NOT be sent to Sx"));
        Serial.println(F("  RESET             Reset all broadcast settings to defaults"));
        Serial.println(F("                      All ports enabled for input and output"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?BCAST,IN,S3,OFF       - Don't broadcast S3 input (e.g. sensor)"));
        Serial.println(F("  ?BCAST,OUT,S2,OFF      - Don't send broadcasts to S2"));
        Serial.println(F("  ?BCAST,IN,S1,ON        - Re-enable broadcasting from S1"));
        Serial.println(F("  ?BCAST,RESET           - Restore all defaults"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Settings are saved to NVS and persist across reboots"));
        Serial.println(F("  - Serial mappings (?MAP,SERIAL) override broadcast behavior"));
        Serial.println(F("  - Maestro ports are automatically excluded from broadcasts"));
        Serial.println(F("  - PWM output ports are automatically excluded from broadcasts"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?SBISxON / ?SBISxOFF   - Broadcast input  (e.g. ?SBIS3OFF)"));
        Serial.println(F("  ?SBOSxON / ?SBOSxOFF   - Broadcast output (e.g. ?SBOS2OFF)"));
        Serial.println(F("  ?RESET_BROADCAST        - Reset all settings"));

    // ================================================================
    } else if (c == "KYBER") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?KYBER,<command>"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Configures integration with the Kyber RC system for lightsaber"));
        Serial.println(F("  and droid sound/motion control. Kyber communicates via a"));
        Serial.println(F("  proprietary serial protocol that is forwarded across WCB boards."));
        Serial.println(F("  LOCAL means Kyber is physically connected to this board."));
        Serial.println(F("  REMOTE means the Maestro is local but Kyber is on another board."));
        Serial.println(F("\nCommands:"));
        Serial.println(F("  LOCAL             Kyber connected to this board (S2)"));
        Serial.println(F("                      S1=Maestro, S2=Kyber"));
        Serial.println(F("                      Kyber data broadcast to all remote WCBs"));
        Serial.println(F("  LOCAL,Sx,Mx:WxSx:baud,...  Configure Kyber local AND all Maestros"));
        Serial.println(F("                                in a single command"));
        Serial.println(F("                                Sx  = Kyber serial port on this board"));
        Serial.println(F("                                Then list Maestro configs same as ?MAESTRO"));
        Serial.println(F("                                Generates copy-paste setup for all WCBs"));
        Serial.println(F("  REMOTE            Maestro local, Kyber on another board"));
        Serial.println(F("                      S1=Maestro only"));
        Serial.println(F("                      Receives Kyber data via ESP-NOW"));
        Serial.println(F("  CLEAR             Disable Kyber integration entirely"));
        Serial.println(F("  LIST              Show current Kyber configuration and targets"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?KYBER,LOCAL      - This board has Kyber on S2"));
        Serial.println(F("  ?KYBER,LOCAL,S1,M1:W1S2:115200,M2:W2S1:115200,M3:W3S1:57600"));
        Serial.println(F("  ?KYBER,REMOTE     - This board has Maestro, Kyber elsewhere"));
        Serial.println(F("  ?KYBER,CLEAR      - Disable Kyber"));
        Serial.println(F("  ?KYBER,LIST       - Show configuration"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Kyber LOCAL reserves S1 and S2 - cannot be used for other purposes"));
        Serial.println(F("  - Kyber REMOTE reserves S1 only"));
        Serial.println(F("  - PWM cannot be mapped to S1 or S2 when Kyber is configured"));
        Serial.println(F("  - Setting is saved to NVS and restored on reboot"));
        Serial.println(F("  - Reboot required after changing Kyber mode"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?KYBER_LOCAL / ?KYBER_REMOTE / ?KYBER_CLEAR / ?KYBER_LIST"));

    // ================================================================
    } else if (c == "ETM") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?ETM,<command>[,value]"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Ensured Transmission Mode (ETM) guarantees command delivery"));
        Serial.println(F("  between WCB boards using ACK packets, automatic retries, and"));
        Serial.println(F("  heartbeat monitoring. When enabled, every unicast and broadcast"));
        Serial.println(F("  command is acknowledged. Unacknowledged commands are retried up"));
        Serial.println(F("  to 3 times before being marked as failed. Heartbeats track which"));
        Serial.println(F("  boards are online. ALL boards must have ETM set the same way or"));
        Serial.println(F("  messages will be silently ignored."));
        Serial.println(F("\nCommands:"));
        Serial.println(F("  ON                Enable ETM on this board"));
        Serial.println(F("  OFF               Disable ETM on this board"));
        Serial.println(F("  TIMEOUT,ms        Milliseconds to wait for ACK before retry"));
        Serial.println(F("                      Default: 500ms"));
        Serial.println(F("                      Recommended: run ?ETM,CHAR to find best value"));
        Serial.println(F("  HB,sec            Heartbeat broadcast interval in seconds"));
        Serial.println(F("                      Default: 10 sec (+/- 1 sec random)"));
        Serial.println(F("  MISS,count        Missed heartbeats before board marked offline"));
        Serial.println(F("                      Default: 3"));
        Serial.println(F("  BOOT,sec          Random heartbeat window on boot"));
        Serial.println(F("                      Prevents all boards transmitting simultaneously"));
        Serial.println(F("                      Default: 2 sec"));
        Serial.println(F("  COUNT,n           Test messages per board per phase during CHAR"));
        Serial.println(F("                      Range: 10-200, Default: 20"));
        Serial.println(F("  DELAY,ms          Delay between messages during CHAR"));
        Serial.println(F("                      Default: 100ms"));
        Serial.println(F("  CHAR              Run network characterization test"));
        Serial.println(F("                      Tests 3 phases: baseline, broadcast, loaded"));
        Serial.println(F("                      Outputs recommended TIMEOUT value when done"));
        Serial.println(F("                      ETM debug is auto-disabled during test for"));
        Serial.println(F("                      accurate timing and restored when complete"));
        Serial.println(F("  CHKSM,ON/OFF      Enable/disable CRC32 checksum on ETM packets"));
        Serial.println(F("                      Detects corrupted packets before execution"));
        Serial.println(F("                      Default: OFF"));
        Serial.println(F("                      ALL boards must match or packets are rejected"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?ETM,ON"));
        Serial.println(F("  ?ETM,TIMEOUT,300"));
        Serial.println(F("  ?ETM,HB,15"));
        Serial.println(F("  ?ETM,MISS,5"));
        Serial.println(F("  ?ETM,CHAR"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - ETM packet size is 252 bytes vs 249 bytes normal"));
        Serial.println(F("  - PWM passthrough always bypasses ETM for low latency"));
        Serial.println(F("  - Raw Kyber data always bypasses ETM"));
        Serial.println(F("  - Boards not yet seen via heartbeat will still receive commands"));
        Serial.println(F("  - Run ?ETM,CHAR before setting TIMEOUT for best results"));
        Serial.println(F("  - Settings saved to NVS and persist across reboots"));
        Serial.println(F("  - CHKSM adds 12 bytes overhead, reducing max command to 188 chars"));
        Serial.println(F("  - CHKSM must match across all boards or all packets are rejected"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?ETMON / ?ETMOFF"));
        Serial.println(F("  ?ETMTIMEOUTms      (e.g. ?ETMTIMEOUT300)"));
        Serial.println(F("  ?ETMHBsec          (e.g. ?ETMHB10)"));
        Serial.println(F("  ?ETMMISScount      (e.g. ?ETMMISS3)"));
        Serial.println(F("  ?ETMBOOTsec        (e.g. ?ETMBOOT2)"));
        Serial.println(F("  ?ETMCHARCOUNTn     (e.g. ?ETMCHARCOUNT20)"));
        Serial.println(F("  ?ETMCHARDELAYms    (e.g. ?ETMCHARDELAY100)"));
        Serial.println(F("  ?ETMCHAR"));

    // ================================================================
    } else if (c == "MAC") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?MAC,<octet>,<hex_value>"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Sets the 2nd and 3rd octets of the ESP-NOW MAC address used by"));
        Serial.println(F("  all WCB boards. These two octets form a group identifier - only"));
        Serial.println(F("  boards sharing the same octets 2 and 3 will communicate with"));
        Serial.println(F("  each other. This allows multiple independent WCB systems to"));
        Serial.println(F("  operate in the same RF space without interference."));
        Serial.println(F("\nParameters:"));
        Serial.println(F("  octet         Which octet to set: 2 or 3"));
        Serial.println(F("  hex_value     Two hex digits (00-FF)"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?MAC,2,AB          - Set 2nd octet to 0xAB"));
        Serial.println(F("  ?MAC,3,CD          - Set 3rd octet to 0xCD"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - ALL boards in the same system must have identical octets 2 and 3"));
        Serial.println(F("  - Default value is 0x00 for both octets"));
        Serial.println(F("  - Full MAC format: 02:xx:xx:00:00:WCB# (xx = your octets)"));
        Serial.println(F("  - Reboot required after changing MAC octets"));
        Serial.println(F("  - Saved to NVS and persists across reboots"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?M2xx   (e.g. ?M2AB)"));
        Serial.println(F("  ?M3xx   (e.g. ?M3CD)"));

    // ================================================================
    } else if (c == "EPASS") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?EPASS,<password>"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Sets the ESP-NOW network password used to validate incoming"));
        Serial.println(F("  messages. Only messages containing the correct password are"));
        Serial.println(F("  processed. This provides a basic layer of protection against"));
        Serial.println(F("  messages from other ESP-NOW devices in the area."));
        Serial.println(F("\nParameters:"));
        Serial.println(F("  password      1-39 characters, lowercase recommended"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?EPASS,mysecretpassword"));
        Serial.println(F("  ?EPASS,r2d2_droid_network"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - ALL boards in the system must use the same password"));
        Serial.println(F("  - Default: change_me_or_risk_takeover"));
        Serial.println(F("  - Maximum 39 characters"));
        Serial.println(F("  - Saved to NVS and persists across reboots"));
        Serial.println(F("  - Takes effect immediately without reboot"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?EPASSpassword   (e.g. ?EPASSmy_password)"));

    // ================================================================
    } else if (c == "WCB") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?WCB,<number>"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Sets this board's WCB number (1-9). The WCB number determines"));
        Serial.println(F("  the board's MAC address and how it is addressed in unicast"));
        Serial.println(F("  commands. Each board in a system must have a unique number."));
        Serial.println(F("  WCB1 is typically the primary/master board."));
        Serial.println(F("\nParameters:"));
        Serial.println(F("  number        1-9, must be unique in the system"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?WCB,1         - Set this board as WCB1"));
        Serial.println(F("  ?WCB,2         - Set this board as WCB2"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Reboot required after changing WCB number"));
        Serial.println(F("  - MAC address last byte will be set to this number"));
        Serial.println(F("  - Use ?WCBQ to set the total number of boards in the system"));
        Serial.println(F("  - Saved to NVS and persists across reboots"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?WCBx   (e.g. ?WCB2)"));

    // ================================================================
    } else if (c == "WCBQ") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?WCBQ,<quantity>"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Sets the total number of WCB boards in the system. This is used"));
        Serial.println(F("  to determine how many ESP-NOW peers to register, and which boards"));
        Serial.println(F("  ETM should expect heartbeats from. Must be set correctly on all"));
        Serial.println(F("  boards for the system to function properly."));
        Serial.println(F("\nParameters:"));
        Serial.println(F("  quantity      Total number of WCB boards (1-9)"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?WCBQ,1        - Single board system"));
        Serial.println(F("  ?WCBQ,2        - Two board system"));
        Serial.println(F("  ?WCBQ,3        - Three board system"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Reboot required after changing quantity"));
        Serial.println(F("  - ALL boards in the system should have the same WCBQ value"));
        Serial.println(F("  - ETM uses this to know which boards to expect heartbeats from"));
        Serial.println(F("  - Saved to NVS and persists across reboots"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?WCBQx   (e.g. ?WCBQ2)"));

    // ================================================================
    } else if (c == "MAESTRO") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?MAESTRO,<command>[,options]"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Configures Pololu Maestro servo controller integration. Multiple"));
        Serial.println(F("  Maestro controllers can be configured across different WCB boards."));
        Serial.println(F("  Each Maestro is assigned an ID (M1-M8) and mapped to a specific"));
        Serial.println(F("  WCB board and serial port. Commands targeting a Maestro ID are"));
        Serial.println(F("  automatically routed to the correct board and port."));
        Serial.println(F("\nCommands:"));
        Serial.println(F("  Mx:WxSx:baud      Configure a Maestro mapping"));
        Serial.println(F("                      Mx  = Maestro ID (M1-M8)"));
        Serial.println(F("                      WxSx = WCB number and serial port"));
        Serial.println(F("                      baud = serial baud rate (usually 57600)"));
        Serial.println(F("  LIST              List all configured Maestro mappings"));
        Serial.println(F("  CLEAR,x           Clear Maestro config by ID number"));
        Serial.println(F("  CLEAR,ALL         Clear all Maestro configurations"));
        Serial.println(F("  ENABLE            Set S1 baud to 57600 for Maestro use"));
        Serial.println(F("                      Saves original baud rate for restore"));
        Serial.println(F("  DISABLE           Restore S1 to original baud rate"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?MAESTRO,M1:W1S1:57600              - Maestro 1 on WCB1 S1"));
        Serial.println(F("  ?MAESTRO,M1:W1S1:57600,M2:W2S1:57600 - Two Maestros"));
        Serial.println(F("  ?MAESTRO,LIST                       - Show all configurations"));
        Serial.println(F("  ?MAESTRO,CLEAR,ALL                  - Remove all configs"));
        Serial.println(F("  ?MAESTRO,ENABLE                     - Quick S1 setup for Maestro"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Maestro requires 57600 baud on the connected serial port"));
        Serial.println(F("  - Commands are sent as binary Pololu protocol packets"));
        Serial.println(F("  - Use ;Mx,script to trigger a Maestro script (x=ID, script=number)"));
        Serial.println(F("  - Saved to NVS and persists across reboots"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?MAESTRO_ENABLE / ?MAESTRO_DISABLE / ?MAESTRO_LIST"));
        Serial.println(F("  ?MAESTRO_CLEAR,x / ?MAESTRO_DEFAULT"));
        Serial.println(F("  ?MAESTROconfig    (e.g. ?MAESTROM1:W1S1:57600)"));

    // ================================================================
    } else if (c == "SEQ") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?SEQ,<command>[,key[,value]]"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Stores and manages named command sequences in NVS flash memory."));
        Serial.println(F("  A sequence is a string of chained commands saved under a short"));
        Serial.println(F("  key name. Once saved, sequences can be triggered from any serial"));
        Serial.println(F("  port or ESP-NOW using the recall command. Sequences can contain"));
        Serial.println(F("  timer commands for choreographed multi-step animations."));
        Serial.println(F("\nCommands:"));
        Serial.println(F("  SAVE,key,value    Save a command sequence under key name"));
        Serial.println(F("                      key: short name, no spaces"));
        Serial.println(F("                      value: command string (can include delimiters)"));
        Serial.println(F("  CLEAR,key         Delete a stored sequence by key name"));
        Serial.println(F("  CLEAR,ALL         Delete all stored sequences"));
        Serial.println(F("  LIST              List all stored sequences and their contents"));
        Serial.println(F("\nTo run a stored sequence:"));
        Serial.println(F("  ;Ckey             Run sequence named 'key'"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?SEQ,SAVE,wave,CMD1^CMD2^CMD3      - Save sequence 'wave'"));
        Serial.println(F("  ?SEQ,SAVE,spin,;T500,SPIN^;T1000,STOP  - Timed sequence"));
        Serial.println(F("  ;Cwave                             - Run sequence 'wave'"));
        Serial.println(F("  ?SEQ,LIST                          - Show all sequences"));
        Serial.println(F("  ?SEQ,CLEAR,wave                    - Delete 'wave'"));
        Serial.println(F("  ?SEQ,CLEAR,ALL                     - Delete everything"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Key names are case sensitive"));
        Serial.println(F("  - Maximum value length is approximately 1800 characters"));
        Serial.println(F("  - Sequences can call other sequences using ;Ckey"));
        Serial.println(F("  - Timer commands (;T) create non-blocking delays between steps"));
        Serial.println(F("  - Saved to NVS and persists across reboots"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?CSkey,value   - Save sequence  (e.g. ?CSwave,CMD1^CMD2)"));
        Serial.println(F("  ?CEkey         - Clear sequence (e.g. ?CEwave)"));
        Serial.println(F("  ?CCLEAR        - Clear all sequences"));

    // ================================================================
    } else if (c == "STATS") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?STATS  /  ?STATS,RESET"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Displays ESP-NOW transmission statistics since last reboot."));
        Serial.println(F("  Shows counts for command messages, PWM passthrough, and raw"));
        Serial.println(F("  Kyber data separately. When ETM is enabled, also shows per-board"));
        Serial.println(F("  delivery confirmation, retry counts, and failure rates."));
        Serial.println(F("\nCommands:"));
        Serial.println(F("  (none)            Display all current statistics"));
        Serial.println(F("  RESET             Reset all counters to zero"));
        Serial.println(F("\nOutput includes:"));
        Serial.println(F("  - Unicast command attempts, delivered, failed"));
        Serial.println(F("  - Delivery success rate percentage"));
        Serial.println(F("  - PWM passthrough message counts (if used)"));
        Serial.println(F("  - Raw Kyber data counts (if used)"));
        Serial.println(F("  - Per-board ETM stats when ETM is enabled"));
        Serial.println(F("  - Board online/offline status with last seen time"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?STATS             - Show statistics"));
        Serial.println(F("  ?STATS,RESET       - Reset all counters"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Statistics reset automatically on reboot"));
        Serial.println(F("  - Use ?DEBUG,ETM,ON for detailed per-packet logging"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?STATS         - Show stats"));
        Serial.println(F("  ?STATSRESET    - Reset stats"));

    // ================================================================
    } else if (c == "DELIM") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Sets the command delimiter character used to chain multiple"));
        Serial.println(F("  commands in a single transmission. The delimiter separates"));
        Serial.println(F("  individual commands within a chained command string."));
        Serial.println(F("\nParameters:"));
        Serial.println(F("  character     Single character to use as delimiter"));
        Serial.println(F("                  Default: ^"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?DELIM,^       - Set delimiter to ^ (default)"));
        Serial.println(F("  ?DELIM,|       - Set delimiter to |"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - ALL boards in the system should use the same delimiter"));
        Serial.println(F("  - Choose a character not used in your commands"));
        Serial.println(F("  - Saved to NVS and persists across reboots"));
        Serial.println(F("  - Takes effect immediately without reboot"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?Dx   (e.g. ?D^)"));

    // ================================================================
    } else if (c == "FUNCCHAR") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?FUNCCHAR,<character>"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Sets the local function identifier character. This is the"));
        Serial.println(F("  prefix character that tells the WCB a command is addressed"));
        Serial.println(F("  to this board's local functions rather than being broadcast."));
        Serial.println(F("  All WCB configuration commands start with this character."));
        Serial.println(F("\nParameters:"));
        Serial.println(F("  character     Single character, Default: ?"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?FUNCCHAR,?    - Set to ? (default)"));
        Serial.println(F("  ?FUNCCHAR,/    - Set to /"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Must not conflict with CommandCharacter (;)"));
        Serial.println(F("  - Saved to NVS and persists across reboots"));
        Serial.println(F("  - Takes effect immediately"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?LFx   (e.g. ?LF?)"));

    // ================================================================
    } else if (c == "CMDCHAR") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?CMDCHAR,<character>"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Sets the command character prefix. This is the prefix used"));
        Serial.println(F("  for serial routing, WCB targeting, Maestro, stored sequence"));
        Serial.println(F("  recall, and PWM output commands. Commands starting with this"));
        Serial.println(F("  character are processed locally by the command parser."));
        Serial.println(F("\nParameters:"));
        Serial.println(F("  character     Single character, Default: ;"));
        Serial.println(F("\nCommand character prefixes:"));
        Serial.println(F("  ;Sx,msg       Send msg to serial port x"));
        Serial.println(F("  ;Wx,msg       Send msg to WCB x via ESP-NOW"));
        Serial.println(F("  ;Ckey         Run stored sequence 'key'"));
        Serial.println(F("  ;Mx,script    Trigger Maestro x script number"));
        Serial.println(F("  ;Px,width     Output PWM pulse on port x"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?CMDCHAR,;    - Set to ; (default)"));
        Serial.println(F("  ?CMDCHAR,:    - Set to :"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - Must not conflict with LocalFunctionIdentifier (?)"));
        Serial.println(F("  - Saved to NVS and persists across reboots"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?CCx   (e.g. ?CC;)"));

    // ================================================================
    } else if (c == "ERASE") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?ERASE,NVS"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Erases ALL settings stored in NVS flash memory and reboots."));
        Serial.println(F("  This returns the board to factory default settings. Use this"));
        Serial.println(F("  when troubleshooting configuration issues or preparing a board"));
        Serial.println(F("  for a fresh setup. Use ?BACKUP first to save your configuration."));
        Serial.println(F("\nWhat gets erased:"));
        Serial.println(F("  - WCB number and quantity"));
        Serial.println(F("  - MAC octets"));
        Serial.println(F("  - ESP-NOW password"));
        Serial.println(F("  - Baud rates"));
        Serial.println(F("  - Broadcast settings"));
        Serial.println(F("  - Serial/PWM mappings"));
        Serial.println(F("  - Stored sequences"));
        Serial.println(F("  - Kyber settings"));
        Serial.println(F("  - Hardware version"));
        Serial.println(F("  - ETM settings"));
        Serial.println(F("  - Serial labels"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?ERASE,NVS     - Erase everything and reboot"));
        Serial.println(F("\nWARNING: This cannot be undone! Use ?BACKUP first!"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?WCB_ERASE"));

    // ================================================================
    } else if (c == "HW") {
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("---------------------------------------------------"));
        Serial.println(F("\nUsage: ?HW,<version>"));
        Serial.println(F("\nDescription:"));
        Serial.println(F("  Sets the hardware version of this WCB board. This must be set"));
        Serial.println(F("  correctly before any other configuration as it determines the"));
        Serial.println(F("  pin mapping for all serial ports and the status LED. Setting"));
        Serial.println(F("  the wrong version will result in incorrect pin assignments."));
        Serial.println(F("\nVersions:"));
        Serial.println(F("  1             Version 1.0 (LilyGo T7 V1.5 ESP32)"));
        Serial.println(F("  21            Version 2.1 (Custom PCB)"));
        Serial.println(F("  23            Version 2.3 (Custom PCB)"));
        Serial.println(F("  24            Version 2.4 (Custom PCB)"));
        Serial.println(F("  31            Version 3.1 (DIY Version)"));
        Serial.println(F("  32            Version 3.2 (DIY Version)"));
        Serial.println(F("\nExamples:"));
        Serial.println(F("  ?HW,31         - Set hardware version to 3.1"));
        Serial.println(F("  ?HW,24         - Set hardware version to 2.4"));
        Serial.println(F("\nNotes:"));
        Serial.println(F("  - This should be the FIRST command sent to a new board"));
        Serial.println(F("  - Reboot required after changing hardware version"));
        Serial.println(F("  - Saved to NVS and persists across reboots"));
        Serial.println(F("  - Use ?CONFIG to verify current hardware version"));
        Serial.println(F("\nLegacy commands:"));
        Serial.println(F("  ?HWx   (e.g. ?HW31)"));

    // ================================================================
    } else {
        // Top-level help menu
        Serial.println(F("\n============================================================"));
        Serial.println(F("  Wireless Communication Board (WCB) - Command Reference"));
        Serial.println(F("============================================================"));
        Serial.println(F("  Type ??           - Show this help menu"));
        Serial.println(F("  Type ?COMMAND?    - Quick syntax for a specific command"));
        Serial.println(F("                      (e.g. ?ETM? or ?MAP?)"));
        Serial.println(F("------------------------------------------------------------"));
        Serial.println(F("\n  INITIAL SETUP (do this first on a new board):"));
        Serial.println(F("    ?HW,x           Set hardware version (required before anything else)"));
        Serial.println(F("    ?WCB,x          Set this board's number (1-9)"));
        Serial.println(F("    ?WCBQ,x         Set total boards in system"));
        Serial.println(F("    ?MAC,2,xx       Set 2nd MAC group octet (all boards must match)"));
        Serial.println(F("    ?MAC,3,xx       Set 3rd MAC group octet (all boards must match)"));
        Serial.println(F("    ?EPASS,pass     Set ESP-NOW password   (all boards must match)"));
        Serial.println(F("\n  SERIAL PORTS:"));
        Serial.println(F("    ?BAUD,Sx,rate   Set baud rate for serial port x"));
        Serial.println(F("    ?LABEL,Sx,text  Set a label for serial port x"));
        Serial.println(F("    ?BCAST,IN/OUT   Control broadcast input/output per port"));
        Serial.println(F("    ?MAP,SERIAL     Route serial data between ports/boards"));
        Serial.println(F("    ?MAP,PWM        Route RC PWM signals between ports/boards"));
        Serial.println(F("\n  DEVICES:"));
        Serial.println(F("    ?KYBER          Configure Kyber RC integration"));
        Serial.println(F("    ?MAESTRO        Configure Maestro servo controller(s)"));
        Serial.println(F("\n  NETWORK:"));
        Serial.println(F("    ?ETM            Ensured Transmission Mode (ACK/retry/heartbeat)"));
        Serial.println(F("    ?STATS          ESP-NOW transmission statistics"));
        Serial.println(F("\n  COMMAND SEQUENCES:"));
        Serial.println(F("    ?SEQ,SAVE       Save a named command sequence"));
        Serial.println(F("    ?SEQ,LIST       List all saved sequences"));
        Serial.println(F("    ;Ckey           Run a saved sequence"));
        Serial.println(F("\n  ROUTING COMMANDS:"));
        Serial.println(F("    ;Sx,msg         Send msg to serial port x"));
        Serial.println(F("    ;Wx,msg         Send msg to WCB x via ESP-NOW"));
        Serial.println(F("    ;Mx,script      Trigger Maestro x script number"));
        Serial.println(F("    ;Ckey           Run stored sequence named key"));
        Serial.println(F("    msg             Broadcast msg to all ports and ESP-NOW"));
        Serial.println(F("\n  DEBUG:"));
        Serial.println(F("    ?DEBUG,ON/OFF   Enable/disable debug output"));
        Serial.println(F("    ?DEBUG,ETM      ETM packet/ACK/retry debug"));
        Serial.println(F("    ?DEBUG,PWM      PWM passthrough debug"));
        Serial.println(F("    ?DEBUG,KYBER    Kyber serial forwarding debug"));
        Serial.println(F("\n  SYSTEM:"));
        Serial.println(F("    ?config         Print full configuration"));
        Serial.println(F("    ?backup         Print backup/restore commands"));
        Serial.println(F("    ?DELIM,x        Set command delimiter character (default ^)"));
        Serial.println(F("    ?FUNCCHAR,x     Set local function identifier (default ?)"));
        Serial.println(F("    ?CMDCHAR,x      Set command character (default ;)"));
        Serial.println(F("    ?ERASE,NVS      Erase ALL settings - WARNING: irreversible!"));
        Serial.println(F("    ?reboot         Reboot this board"));
        Serial.println(F("============================================================\n"));
    }
}


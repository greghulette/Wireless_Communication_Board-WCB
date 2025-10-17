# PWM Passthrough System - Quick Reference Guide
## Wireless Communication Board (WCB) v5.2

---

## Overview
The PWM system allows you to read PWM signals on one serial port's RX pin and output them on other serial ports' TX pins, either locally or on remote WCB boards via ESP-NOW. This enables distributed PWM signal routing across multiple boards in a mesh network.

---

## Command Summary

### Mapping Commands
| Command | Description | Example |
|---------|-------------|---------|
| `?PMSx,outputs...` | Map PWM input to outputs | `?PMS5,S3,W2S3` |
| `?PMRx` | Remove mapping for input port x | `?PMR5` |
| `?PLIST` | List all PWM mappings | `?PLIST` |
| `?PCLEAR` | Clear all PWM mappings | `?PCLEAR` |

### Manual Port Configuration
| Command | Description | Example |
|---------|-------------|---------|
| `?POx` | Manually set port as PWM output | `?PO3` |
| `?PXx` | Remove port from PWM output | `?PX3` |

---

## Mapping Syntax

### Basic Format
```
?PMSx,output1,output2,...
```
- `x` = Input port number (1-5)
- Outputs can be local (`Sx`) or remote (`WxSy`)

### Output Format
- **Local output:** `Sx` where x is serial port 1-5
- **Remote output:** `WxSy` where x is WCB board number (1-9) and y is serial port (1-5)

### Examples

#### Local Only
```
?PMS5,S3
```
- Read PWM from Serial5 RX pin
- Output to Serial3 TX pin

#### Local + Remote
```
?PMS5,S3,W2S3
```
- Read PWM from Serial5 RX pin
- Output to local Serial3 TX pin
- Output to WCB2's Serial3 TX pin

#### Multiple Outputs
```
?PMS1,S2,S3,W2S4,W3S5
```
- Read from Serial1 RX pin
- Output to local Serial2 and Serial3 TX pins
- Output to WCB2's Serial4 TX pin
- Output to WCB3's Serial5 TX pin

#### Multiple Boards, Same Port
```
?PMS5,W1S3,W2S3,W3S3
```
- Read from Serial5 RX pin
- Output to Serial3 TX pin on WCB1, WCB2, and WCB3

---

## How It Works

### When You Create a Mapping (`?PMSx,outputs...`)
1. Local board configures input port RX pin for interrupt-based PWM reading
2. Local output ports are configured and marked as PWM outputs
3. Remote boards receive `?POx` commands via ESP-NOW to configure their output ports
4. All settings saved to NVS (persistent across reboots)
5. Remote boards receive `?REBOOT` command
6. **All boards auto-reboot** to apply configuration
   - Remote boards: 2 seconds
   - Local board: 3 seconds

### When You Remove a Mapping (`?PMRx`)
1. Local board detaches interrupts from input port
2. Local output ports removed from tracking
3. Remote boards receive `?PXx` commands to clear their output ports
4. Settings removed from NVS
5. Remote boards receive `?REBOOT` command
6. **All boards auto-reboot** to apply changes

### When You Clear All Mappings (`?PCLEAR`)
1. All local mappings cleared and interrupts detached
2. All local output ports removed from tracking
3. Remote boards with outputs receive `?PXx` for each configured port
4. All PWM preferences cleared from NVS
5. Remote boards receive `?REBOOT` command
6. **All boards auto-reboot** to apply changes

### At Boot Time
1. `initPWM()` loads all mappings from NVS
2. Interrupts attached to configured input ports
3. Output ports configured with proper GPIO settings
4. Boot message displays current PWM configuration
5. `processPWMPassthrough()` task begins monitoring for PWM signals

---

## Boot Message Display

Serial ports are displayed based on their configuration priority:

```
-------------------------------------------------------
Serial1: Used by Kyber (Local)                    ← Kyber takes priority
Serial2: Used by Kyber (Local)
Serial3: Configured for PWM Output                ← PWM output port
Serial4 Baud Rate: 9600, Broadcast: Enabled       ← Normal serial operation
Serial5: Configured for PWM Input                 ← PWM input port
-------------------------------------------------------
```

### Priority Order (Highest to Lowest)
1. **Kyber** - Ports 1 & 2 if configured as Kyber Local/Remote
2. **PWM Input** - Configured RX pins reading PWM signals
3. **PWM Output** - Configured TX pins outputting PWM signals
4. **Normal Serial** - Default serial communication with baud rate

---

## Technical Details

### PWM Signal Specifications
- **Valid pulse width:** 500-2500 microseconds
- **Typical RC/servo range:** 1000-2000 microseconds (1.5ms = center)
- **Out-of-range signals:** Automatically ignored/filtered
- **Signal type:** Standard RC PWM (50Hz typical)

### Interrupt-Based Reading
- Uses hardware interrupts on RX pins (`CHANGE` trigger)
- Measures rising edge to falling edge duration
- Non-blocking operation - runs in background
- No impact on other system operations

### Output Methods

#### Local Output
- Direct GPIO bit-banging using `digitalWrite()` + `delayMicroseconds()`
- Immediate output on TX pin
- Microsecond-accurate timing
- TX pin configured as `OUTPUT`, default LOW

#### Remote Output
- ESP-NOW message: `;Px[pulsewidth]` sent to target WCB
- Example: `;P31500` = Serial3, 1500μs pulse
- 50ms delay between messages to prevent flooding
- Automatic retry on ESP-NOW failure

### Non-Volatile Storage (NVS)
Mappings stored in two NVS namespaces:

#### `pwm_mappings`
- Key format: `map0`, `map1`, ... `map9`
- Value format: `inputPort,output1,output2,...`
- Example: `5,S3,W2S3` = Serial5 input, local S3 + remote W2S3

#### `pwm_outputs`
- Key format: `port0`, `port1`, ... `port4`
- Stores manually configured output ports
- Loaded at boot to restore configuration

### Auto-Reboot Behavior
All PWM configuration changes trigger automatic reboots to ensure clean state:

| Action | Local Delay | Remote Delay | Reason |
|--------|-------------|--------------|--------|
| Create mapping | 3 seconds | 2 seconds | Apply GPIO configurations |
| Remove mapping | 3 seconds | 2 seconds | Reset GPIO to serial mode |
| Clear all | 3 seconds | 2 seconds | Full system reset |

**Why reboot?**
- Ensures clean GPIO reconfiguration
- Prevents conflicts between serial and PWM modes
- Guarantees NVS settings are properly applied
- Resets interrupt handlers correctly

### FreeRTOS Task Integration
PWM processing runs in dedicated task that continuously monitors for new PWM data and outputs to configured destinations.

---

## Troubleshooting

### PWM Not Working

**Symptom:** No PWM output detected

**Solutions:**
1. Check boot message shows correct port configuration
2. Verify PWM signal is within 500-2500μs range
3. Enable debug: `?DON` to see PWM values in serial monitor
4. Check physical connections to RX pins
5. Verify input signal is actual PWM, not serial data

### Remote Output Not Working

**Symptom:** Local output works but remote board doesn't respond

**Solutions:**
1. Verify ESP-NOW connection between boards: Send test command `?CONFIG`
2. Check remote board shows port as "Configured for PWM Output" in boot message
3. Ensure both boards have same ESP-NOW password: `?CONFIG` to verify
4. Check WCB numbers are correct in mapping command
5. Verify remote board is within ESP-NOW range (~100m line of sight)
6. Check MAC address configuration matches on both boards

### Ports Not Resetting

**Symptom:** Ports still show as PWM after clearing

**Solutions:**
1. Use `?PCLEAR` to clear all mappings (cleanest method)
2. Verify all boards reboot after clearing (watch serial monitor)
3. Check boot message after reboot - ports should show normal serial
4. If stuck, use `?WCB_ERASE` (factory reset) and reconfigure from scratch
5. Manually remove with `?PMRx` for specific port

### Mapping Lost After Reboot

**Symptom:** Configuration doesn't persist

**Solutions:**
1. Mappings should automatically persist in NVS
2. Check if NVS is corrupted: `?WCB_ERASE` and reconfigure
3. Verify preferences namespaces not full (unlikely with 50 mapping limit)
4. Check for flash wear - ESP32 NVS has ~100,000 write cycles per sector
5. Ensure you're waiting for "Rebooting in 3 seconds" message before power cycling

### Boot Loop After PWM Configuration

**Symptom:** Board continuously reboots

**Solutions:**
1. This was a bug in early versions - ensure you're using the fixed version
2. `loadPWMMappingsFromPreferences()` should call `addPWMMapping()` with `autoReboot=false`
3. Check `WCB_PWM.cpp` line where mappings are loaded from preferences
4. If stuck, connect to serial monitor during boot and quickly send `?PCLEAR` before loop continues

### Conflicting Port Usage

**Symptom:** Kyber or serial communication broken after PWM config

**Solutions:**
1. Check port priority - Kyber always takes precedence over PWM
2. Don't map PWM to Serial1 or Serial2 if Kyber is configured
3. Review boot message to see actual port assignments
4. Use `?CONFIG` to verify Kyber settings
5. Clear PWM mappings that conflict: `?PMRx` or `?PCLEAR`

---

## Usage Examples

### Example 1: Simple Local Passthrough
**Scenario:** Control a servo on Serial3 from PWM signal on Serial5

```
?PMS5,S3
```

**Result:**
- PWM signal connected to Serial5 RX pin is read
- Same PWM signal output on Serial3 TX pin
- Can connect servo directly to Serial3 TX pin
- 3 second reboot to apply

### Example 2: Distribute to Multiple Remote Boards
**Scenario:** One receiver controls servos on 3 different WCB boards

```
?PMS1,W1S3,W2S3,W3S3
```

**Result:**
- PWM receiver connected to WCB4's Serial1 RX pin
- WCB1, WCB2, and WCB3 all output same signal on their Serial3 TX pins
- Synchronized servo control across multiple boards
- All 4 boards reboot (1 local + 3 remote)

### Example 3: Mix Local and Remote
**Scenario:** Control local servo and remote servo from same input

```
?PMS5,S2,W2S4
```

**Result:**
- Input on Serial5 RX
- Output to local Serial2 TX (servo on this board)
- Output to WCB2's Serial4 TX (servo on remote board)
- Both boards reboot

### Example 4: Multiple Inputs, Different Outputs
**Scenario:** Two separate PWM channels for different servos

```
?PMS4,S2,W1S2
?PMS5,S3,W1S3
```

**Result:**
- Channel 1: Serial4 RX → Serial2 TX local + WCB1 Serial2 TX
- Channel 2: Serial5 RX → Serial3 TX local + WCB1 Serial3 TX
- Independent control of two servo channels
- Both boards reboot after each command (6 seconds total)

### Example 5: Clearing and Reconfiguring
**Scenario:** Change PWM configuration completely

```
?PCLEAR
(wait for reboot)
?PMS5,W2S3
```

**Result:**
- First command clears all existing mappings
- All boards reboot clean
- Second command creates new mapping
- Clean reconfiguration without conflicts

---

## Best Practices

### Planning Your Mappings
1. **Document your setup** - Keep notes on which ports are mapped where
2. **Use consistent numbering** - Serial5 for inputs, Serial3/4 for outputs (example)
3. **Avoid Kyber conflicts** - Don't use Serial1/2 if Kyber is configured
4. **Test locally first** - Verify local passthrough before adding remote outputs
5. **One mapping at a time** - Wait for reboots between configuration changes

### Performance Considerations
1. **Minimize remote outputs** - Each remote output adds ESP-NOW latency (~5-10ms)
2. **Local outputs are fastest** - Direct GPIO bit-banging has microsecond timing
3. **Limit total mappings** - Max 10 mappings, but 3-5 is practical for performance
4. **ESP-NOW range** - Keep boards within ~100m for reliable communication
5. **Power supply** - Ensure stable power for ESP-NOW radio operation

### Maintenance
1. **Regular backups** - Document your configuration with `?CONFIG`
2. **Test after changes** - Always verify PWM passthrough after reconfiguration
3. **Monitor boot messages** - Check for correct port assignments
4. **Use debug mode** - Enable `?DON` when troubleshooting
5. **Keep firmware updated** - Bug fixes and improvements in newer versions

### Safety
1. **Disconnect servos before testing** - Prevent unexpected movements
2. **Test PWM ranges** - Verify 1000-2000μs range before connecting hardware
3. **Add failsafes** - Consider timeout behavior if PWM signal is lost
4. **Label connections** - Physical labels on wires prevent mistakes
5. **Emergency stop** - Have `?PCLEAR` ready to disable all PWM quickly

---

## Advanced Topics

### Custom PWM Processing
The `processPWMPassthrough()` function can be modified to add:
- Pulse width scaling/mapping
- Deadband filtering
- Multi-channel mixing
- Failsafe timeout detection
- PWM signal smoothing

### Integration with Other WCB Features
PWM system works alongside:
- **Timer commands** - Schedule PWM configurations: `;T5000,?PMS5,S3`
- **Stored commands** - Save complex mappings: `?CSservo_map,?PMS5,S2,S3`
- **Maestro control** - Combine PWM passthrough with Maestro servo controller
- **Command chaining** - Multiple PWM commands: `?PMS4,S2^?PMS5,S3`

### ESP-NOW Mesh Behavior
- Broadcast mode not used for PWM (targeted unicast only)
- Each remote board gets individual `?POx` configuration
- Reboot commands sent after all configurations complete
- 50ms delay between messages prevents ESP-NOW buffer overflow

---

## Appendix: File Structure

### Core PWM Files
- `WCB_PWM.h` - Function declarations and structure definitions
- `WCB_PWM.cpp` - PWM implementation and logic
- `WCB.ino` - Command processing integration

### Key Functions
```cpp
void initPWM()                                    // Initialize at boot
void addPWMMapping(String, bool)                  // Create new mapping
void removePWMMapping(int)                        // Remove mapping
void clearAllPWMMappings()                        // Clear all
void processPWMPassthrough()                      // Main processing loop
void attachPWMInterrupt(int)                      // Setup interrupt
bool isSerialPortUsedForPWMInput(int)            // Check input status
bool isSerialPortPWMOutput(int)                   // Check output status
void savePWMMappingsToPreferences()               // Save to NVS
void loadPWMMappingsFromPreferences()             // Load from NVS
```

### Data Structures
```cpp
struct PWMMapping {
    int inputPort;           // 1-5: which serial RX to read
    int outputCount;         // How many outputs
    struct {
        int wcbNumber;       // 0=local, 1-9=remote WCB
        int serialPort;      // 1-5: which serial TX to output
    } outputs[5];
    bool active;
};
```

---

## Support and Contributing

For issues, questions, or contributions, please visit the GitHub repository:
[Wireless Communication Board - WCB](https://github.com/greghulette/Wireless_Communication_Board-WCB)

**Version:** 5.2_170941ROCT2025
**Last Updated:** October 2025
**Author:** Greg Hulette
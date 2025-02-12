# Wireless Communication Board (WCB) User Guide

## **1. Introduction**

### **1.1 Overview**

The Wireless Communication Board (WCB) is a versatile communication system designed to facilitate **ESP-NOW** wireless communication between multiple ESP32-based devices. The system also provides **serial bridging**, command processing, and dynamic configuration for a variety of use cases.

### **1.2 Features**

- **ESP-NOW Communication**: Unicast and broadcast messaging across multiple WCB units.
- **Serial Bridging**: Forward data between Serial1 and Serial2 with low latency.
- **Command Processing**: Handle commands locally and over ESP-NOW.
- **FreeRTOS Multi-Tasking**: Optimized for efficient parallel processing.
- **Dynamic Configuration**: Adjust baud rates, stored commands, and WCB settings via commands.
- **Persistent Storage**: Save and retrieve settings using NVS (Non-Volatile Storage).

## **2. Getting Started**

## **2.1 Setting Up the WCB System**

To ensure proper communication between multiple WCB devices, follow these setup steps:

### **Step 1: Set the Hardware Version**

Each WCB must be configured with the correct hardware version before use. Use the following command:

```
?HWx  (Replace x with the hardware version number: `1` for Version 1.0, `21` for Version 2.1, `23` for Version 2.3, `24` for Version 2.4)
```

Example:

```
?HW1  // Set WCB to hardware version 1
```

### **Step 2: Set the WCB Number**

Each WCB must have a unique identifier between 1-9. Use the following command:

```
?WCBx  (Replace x with the WCB number)
```

Example:

```
?WCB2  // Set this WCB as unit 2
```

### **Step 3: Set the WCB Quantity**

The total number of WCBs in the system must be set and must match on all devices.

```
?WCBQx  (Replace x with the total number of WCBs in the system)
```

Example:

```
?WCBQ3  // There are 3 WCBs in the system
```

### **Step 4: Configure the ESP-NOW Password**

All WCBs must use the same ESP-NOW password to communicate.

```
?EPASSyourpassword
```

Example:

```
?EPASSsecure123  // Set the ESP-NOW password to 'secure123'
```

### **Step 5: Set the MAC Addresses**

To prevent interference between different WCB systems, all devices in the same system must have the same MAC octets.

```
?M2xx  // Set the 2nd MAC octet
?M3xx  // Set the 3rd MAC octet
```

Example:

```
?M222  // Set 2nd MAC octet to 0x22
?M333  // Set 3rd MAC octet to 0x33
```

### **Example Setup for a 3-WCB System:**

| **WCB Unit** | **Command**       | **Value**          |
| ------------ | ----------------- | ------------------ |
| WCB1         | `?HW1`            | Version 1.0        |
|              | `?WCB1`           | Unit 1             |
|              | `?WCBQ3`          | 3 WCBs total       |
|              | `?EPASSsecure123` | ESP-NOW password   |
|              | `?M222`           | MAC Octet 2 = 0x22 |
|              | `?M333`           | MAC Octet 3 = 0x33 |
| WCB2         | `?HW1`            | Version 1.0        |
|              | `?WCB2`           | Unit 2             |
|              | `?WCBQ3`          | 3 WCBs total       |
|              | `?EPASSsecure123` | ESP-NOW password   |
|              | `?M222`           | MAC Octet 2 = 0x22 |
|              | `?M333`           | MAC Octet 3 = 0x33 |
| WCB3         | `?HW1`            | Version 1.0        |
|              | `?WCB3`           | Unit 3             |
|              | `?WCBQ3`          | 3 WCBs total       |
|              | `?EPASSsecure123` | ESP-NOW password   |
|              | `?M222`           | MAC Octet 2 = 0x22 |
|              | `?M333`           | MAC Octet 3 = 0x33 |

All WCBs must have **matching** `WCBQ`, `EPASS`, `M2`, and `M3` values to communicate properly.

Once configured, you can chain all these commands using the command delimiter and reboot at the end:

```
?HW1^?WCB1^?WCBQ3^?EPASSsecure123^?M222^?M333^?REBOOT
```

This ensures all settings are applied before the reboot.

Once configured, reboot each WCB with:

```
?REBOOT
```

## **2.2 Hardware Setup**

### **2.1 Hardware Setup**

1. **Connect the ESP32-based WCB to a power source.**
2. **Ensure all devices are running the latest firmware.**
3. **Connect required peripherals** (e.g., Pololu Maestro, Kyber Light).
4. **Configure the correct pin map** using the `HW` command.

### **2.2 Firmware Installation**

1. Install **Arduino IDE** and required ESP32 libraries.
2. Clone the WCB firmware repository.
3. Open the `WCB1_V5_M.ino` file.
4. Compile and upload the firmware to your ESP32 board.
5. Open the serial monitor to verify successful setup.

## **3. Command Reference**

### **3.1 Local Function Commands (********`?COMMANDS`********)**

| **Command**       | **Description**                                                                                                                                                            |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `?CONFIG`         | Display current configuration settings.                                                                                                                                    |
| `?DON`            | Enable debugging mode.                                                                                                                                                     |
| `?DOFF`           | Disable debugging mode.                                                                                                                                                    |
| `?CCLEAR`         | Clear all stored commands.                                                                                                                                                 |
| `?CSkey,value`    | Store a command with a key-val, separate the name and the stored command with a comma.  You have commas in the command and it will not affect the storing of the commands. |
|                   |                                                                                                                                                                            |
| `?WCBx`           | Set the WCB number (1-9).                                                                                                                                                  |
| `?WCBQx`          | Set the total number of WCBs in the system.MUST match on all boards in system                                                                                              |
| `?WCB_ERASE`      | Erase all WCB stored settings.                                                                                                                                             |
| `?M2xx`           | Set the 2nd MAC Octet (hex)  0x00 - 0xFE   \*\*\*MUST match on all boards in system                                                                                        |
| `?M3xx`           | Set the 3rd MAC octet (hex).   0x00 - 0x FE  \*\*\*MUST match on all boards in system                                                                                      |
| `?Sx0/1`          | Enable/Disable broadcast for Serial x. 0 for disabled, 1 for enabled                                                                                                       |
| `?SxBAUDRATE`     | Set baud rate for Serial x.                                                                                                                                                |
| `?EPASSnewpass`   | Update ESP-NOW password.  \*\*\*MUST match on all boards in system                                                                                                         |
| `?REBOOT`         | Reboot the WCB.                                                                                                                                                            |
| `?MAESTRO_ENABLE` | Enable Maestro communication.                                                                                                                                              |
| `?KYBER_LOCAL`    | Enable Kyber communication locally.                                                                                                                                        |
| `?KYBER_REMOTE`   | Enable Kyber communication remotely.                                                                                                                                       |
| `?KYBER_CLEAR`    | Clear Kyber settings.                                                                                                                                                      |
| `?HWx`            | Set the hardware version.   \*\*\*\* MUST set hardware version to use the system.                                                                                          |

### **3.2 Command Character-Based Commands (********`;COMMANDS`********\*\*\*\*)**

| **Command**  | **Description**                                                                                                                                              |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `;Sxmessage` | Send a message to Serial x (1-5).                                                                                                                            |
| `;Wxmessage` | Send a unicast message to WCB x.                                                                                                                             |
| `;Ckey`      | Recall a stored command by key.                                                                                                                              |
| `;Mxyy`      | Send a Maestro command (x = Maestro ID, yy = sequence). 1-8 valid Maestro IDs.  0 = send to all Maestros.  ID's must match WCB number they are plugged into. |

## **4. Configuration Options**

### **4.1 Serial Port Settings**

- Default baud rates: `9600`
- Adjust baud rate: `?SxBAUDRATE`
- Enable/Disable broadcast: `?Sx0/1`

### **4.2 ESP-NOW Settings**

- Update ESP-NOW password: `?EPASSnewpassword`
- Change WCB ID: `?WCBx`

### **4.3 Stored Commands Management**

- Store command: `?CSkey,value`
- Retrieve command: `;Ckey`
- List stored commands: `?CLIST`
- Delete stored command: `?CDELkey`

## **5. Advanced Features**

### **5.1 ESP-NOW Communication**

- Supports **unicast** (WCB-to-WCB) and **broadcast** (WCB group-wide).
- Automatically ignores messages from unrelated groups (different MAC octets and passwords) for security.

### **5.2 Kyber Support**

- Forwarding data **between Serial1 and Serial2** when Kyber is enabled. 
- Supports **Kyber Local and Remote modes**. The local mode is for when the Kyber is plugged directly into the WCB and the remote mode is when only the maestro with the ID of 2 is plugged into the WCB.

### **5.3 FreeRTOS Tasks**

- **`serialCommandTask`**: Handles all incoming serial data.
- **`KyberLocalTask`**: Forwards Maestro data to a locally connected Kyber.
- **`KyberRemoteTask`**: Forwards Maestro data to a remote Kyber via ESP-NOW.

## **6. Troubleshooting & Debugging**

### **Erasing Flash & Resetting WCB to Factory Defaults**

If you need to completely erase the flash memory and reset the WCB to factory defaults, follow these steps using the Arduino IDE:

1. **Open Arduino IDE** and go to **Tools → Board → Select your ESP32 board**.
2. **Set the Flash Erase Mode:**
   - Go to **Tools → Erase All Flash Before Sketch Upload**.
   - Select **"Enabled"** to erase everything before uploading a new sketch.
3. **Upload the WCB firmware** after enabling flash erase.
4. **Reconfigure the WCB** using the setup commands in Section 2.

This will completely wipe the stored settings, including MAC addresses, ESP-NOW passwords, and stored commands, allowing you to start fresh.
If you need to completely erase the flash memory and reset the WCB to factory defaults, follow these steps using the Arduino IDE:

1. **Open Arduino IDE** and go to **Tools → Board → Select your ESP32 board**.
2. **Set the Flash Erase Mode:**
   - Go to **Tools → Erase Flash**.
   - Select **"All Flash Contents"** to erase everything.
3. **Upload a Blank Sketch to Fully Reset the Board:**
   ```cpp
   void setup() {
       Serial.begin(115200);
       Serial.println("Flash Erased. Upload new firmware.");
   }
   void loop() {}
   ```
4. **Re-upload the WCB firmware** after erasing the flash.
5. **Reconfigure the WCB** using the setup commands in Section 2.

This will completely wipe the stored settings, including MAC addresses, ESP-NOW passwords, and stored commands, allowing you to start fresh.



| **Issue**                     | **Possible Solution**                                              |
| ----------------------------- | ------------------------------------------------------------------ |
| No response from WCB          | Verify power and USB connection. Check baud rate settings.         |
| ESP-NOW messages not received | Ensure correct MAC octets and ESP-NOW password.                    |
| Serial bridging not working   | Confirm correct pin configuration and baud rates.                  |
| Stored commands not executing | Use `?CLIST` to check saved commands. Ensure delimiter is correct. |

## **7. Appendices**

### **7.1 Technical Specifications**

- **ESP32-based board compatibility**
- **Uses WiFi in station mode for ESP-NOW**
- **Supports up to 9 WCB devices in a network**

### **7.2 Pin Maps**

Refer to `wcb_pin_map.h` for pin configurations based on hardware version.

### **7.3 Additional Resour**[ces](https://github.com/espressif/arduino-esp32)

- [ESP-NOW Documentation](https://github.com/espressif/arduino-esp32)
- [Arduino ESP32 Core](https://github.com/espressif/arduino-esp32)
- [Pololu Maestro Documentation](https://www.pololu.com/docs/0J40)

---

This guide provides a complete reference for configuring and using the **Wireless Communication Board (WCB) system**. 🚀


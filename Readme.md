<h1 style="display:inline; height: 75px; font-size:51px;"><img src="./Images/r2logo.png" style="height: 75px; display: inline;" align="center">Wireless Communication Board (WCB)</h1>


## **1. Introduction**

### **1.1 Overview**
<br>Background: <br>I was initially having issues getting signals into and out of the dome reliably and wanted a better way to accomplish this.  I started out with I2C like most builders but quickly found out that I2C wasn't meant for longer distances or electrically noisy environments.  I realized that serial communications are better in this type of environment, but wasn't sure how to get serial to the many different devices within my Droid since serial can only be connected to a single device at a time.  The connected device can replicate the signal out another port, but I didn't feel like that was the most efficient means of communication.   

<br>Hence, the reason I made these boards.  I developed these boards to allow the various microcontrollers in R2 to communicate out their serial port directly to another board, no matter where they are in my droid.  I accomplished this by accepting a serial command into the WCB from a microcontroller, evaluating whether it will go out another locally connected serial port, or forward it to another WCB to send it out to their locally connected serial port.  This is essentially how computer networks work as well.  
<br>I decided to go with a wireless technology between the WCBs to alleviate some of the issues with being in an electrically noisy environment as well as remove the need for passing data through the slip ring between the body and dome. The WCB is using ESP-NOW, which is a connectionless wireless communication protocol that allows the quick and low-power control of smart devices without a router.  It uses the same frequencies that WiFi uses, but I have not seen any RF congestion issues that traditional WiFi sees at conventions.  By using this method, all you need in the dome is power and you can control the dome without it being physically connected to the body.  

While these boards don't control any components within R2, it does allow for the efficient communication of all other microcontrollers.  These boards also allow you to have multiple serially connected devices to communicate with each other directly and bi-directionally. The serially connected devices can communicate to other serial devices connected to the same WCB, or devices connected to remote WCBs. This is accomplished by adding up to 6 characters to your string that you send to the remote device.<br><br>

### **1.2 Features**

- **ESP-NOW Communication**: Unicast and broadcast messaging across multiple WCB units.
- **Serial Bridging**: Forward data between Serial1 and Serial2 with low latency to support Kyber/Maestro Communication.
- **Command Processing**: Handle commands locally and over ESP-NOW.
- **FreeRTOS Multi-Tasking**: Optimized for efficient parallel processing.
- **Dynamic Configuration**: Adjust baud rates, stored commands, and WCB settings via commands.
- **Persistent Storage**: Save and retrieve settings using NVS (Non-Volatile Storage). These settings are stored in NVS which persists after reboot and even after reloading of the sketch.  
- **Store Command Sequences in NVS**: Store your command sequences in the WCB instead of your controller.  200 character limit per stored command.


### **1.3 Physical Board Versions**

#### **HW Version 1.0**


CAD Image       |    Actual Image       | Bare PCB
:---------------:|:---------------------:|:-------------:
<img src="./Images/CADImage.png" style="height: 200px;"> |<img src="./Images/LoadedPCB.jpg" style="height: 200px;"> | <img src="./Images/PCBwithConnectors.jpg" style="height: 200px;">

Features of the board:
- LilyGo T7 Mini32 V1.5
- 5V Terminal Block
- 5 Serial ports that can be used to communicate with microcontrollers  
- Up to 8 WCB's can be used at once in a fully meshed network
- Communication can be individual (Unicast), or be broadcasted to all devices at once.
- Can support bi-directional communications<br>

***  PLEASE NOTE THE DIRECTION OF THE ESP32 WHEN PLUGGING IT INTO THE PCB ON VERSION 1.0. ***

*** THE MICRO USB PORT SHOULD BE ON THE SIDE WITH THE LABEL.  YOU MAY DESTROY YOUR ESP32 IF PLUGGED IN THE WRONG DIRECTION ***

<br><br>

#### **HW Version 2.1**

CAD Image       |    Actual Image     
:---------------:|:---------------------:
<img src="./Images/HWV2.1_CAD.png" style="width: 300px;"><br>|<img src="./Images/HWV2.1_Image_Cropped.jpg" style="width: 300px;">
 



 Features of the board:
- Integrated ESP32-PICO-MINI-02
- 5V Terminal Block
- 5 Serial ports that can be used to communicate with microcontrollers  
- Up to 8 WCB's can be used at once in a fully meshed network
- Communication can be individual (Unicast), or be broadcasted to all devices at once.
- Can support bi-directional communications<br><br>


#### **HW Version 2.3/2.4**

CAD Image       |    Actual Image     
:---------------:|:---------------------:
<img src="./Images/HW2.3CAD.png" style="width: 300px;"><br>|<img src="./Images/HWVer2.3Image.jpg" style="width: 300px;">
 



 Features of the board:
- Integrated ESP32-PICO-MINI-02
- 5V Terminal Block
- 5 Serial ports that can be used to communicate with microcontrollers  
- Up to 8 WCB's can be used at once in a fully meshed network
- Communication can be individual (Unicast), or be broadcasted to all devices at once.
- Can support bi-directional communications

HW Versions 1.0 and 2.1/2.3/2.4 are physically different, but have the same capabilities and are operated the exact same way.  They are 100% interoperable with each other and can be mixed in the same network.  

### **1.4 Concept of Operations**
Below, you will see some possible connections that can exist to the WCB's.  In the picture, there are only 4 WCBs, but the system can handle up to 8 of them.  Each one of the microcontrollers, represented by a green box, can communicate directly with any other microcontroller, regardless if they are physically connected to the same WCB.  I can envision most people using 2 or 3 WCBs.  One in the body, one in the dome, and one on the dome-plate.

<br>
<img src="./Images/Network_Model_1.jpg"><br>



Now if we lay the different types of communications over this picture, you can see how the boards can send the various types of messages.  The green microcontrollers are some examples of the boards that can communicate over serial and can be hooked up to this system.
<img src="./Images/WCB_Unicast.gif">

As you can see in the above image, you can send any other board a direct message. This is what I'm calling a Unicast message.  

With this method, your controller would send each command separately and the WCB will process them as they come in.  There is an option to chain the commands together so your controller only has to send one command.  You would separate each of the inidividual commands with the ^ character.  The WCB will separate the commands when it receives it.  Below is an example

<img src ="./Images/Chained_Commands.gif">

You can see the commands in the previous example are chained together and now the controller is sending that long string of commands to the WCB.  
<br>

If you want to or have a limitation on the length of command you can send to the WCB, you are able to store that chained command on the WCB.  Below is an example on how that would look.  

<img src="./Images/Stored_Commands.gif">

<br>
The WCBs can also Broadcast messages.  The thought is that you send a command everywhere, and if the receiving device accepts the command, it acts on it.  Otherwise it would ignore the command.  The GIF below shows the broadcast messages.  
![til](./Images/NetworkBroadcastGIF.gif)<br><br> <br><br> 




<!-- ## **1. Introduction**

### **1.1 Overview** -->

The Wireless Communication Board (WCB) is a versatile communication system designed to facilitate **ESP-NOW** wireless communication between multiple ESP32-based devices. The system also provides **serial bridging**, command processing, and dynamic configuration for a variety of use cases.


## **2. Getting Started**

### **Required Drivers**

To recognize the WCB on your computer, ensure you have the correct driver installed gpt versions 2.x and higher:

- **CP2102N Driver** (Required for many ESP32 boards)

  - Download: [Silicon Labs CP2102N Driver](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers)



### **Required Libraries**

Before compiling the firmware, install the following libraries in the Arduino IDE:


- **EspSoftwareSerial** (For software-based serial communication)

- **Adafruit NeoPixel** (For controlling LED status indicators)

### **2.1 Firmware Installation**

1. Install **Arduino IDE** and required ESP32 libraries.  ESP32 board version 3.1
2. Clone the WCB firmware repository.
3. Open the `WCB1_V5_M.ino` file.
4. Compile and upload the firmware to your ESP32 board.
5. Open the serial monitor to verify successful setup.


### **2.2 Hardware Setup**
1. **Connect the ESP32-based WCB to a power source.**
2. **Ensure all devices are running the latest firmware.**
3. **Do NOT Connect any peripherals util the WCBs are configured** (e.g., Pololu Maestro, Kyber, Stealth, Roam a Dome Home, ...).
4. **Configure the correct pin map** using the `HW` command.

### **2.3 Setting Up the WCB System**

To ensure proper communication between multiple WCB devices, follow these setup steps:

#### **Step 1: Set the Hardware Version**

Each WCB must be configured with the correct hardware version before use. 

NOTHING WILL WORK WITHOUT PERFORMING THIS STEP!!!! 

Use the following command:

```
?HWx  (Replace x with the hardware version number: `1` for Version 1.0, `21` for Version 2.1, `23` for Version 2.3, `24` for Version 2.4)
```

Examples:

```
?HW1  // Set WCB to hardware version 1
or 
?HW24  // Set WCB to hardware version 2.4
```

#### **Step 2: Set the WCB Number**

Each WCB must have a unique identifier between 1-8. Use the following command:

```
?WCBx  (Replace x with the WCB number)
```

Example:

```
?WCB2  // Set this WCB as unit 2
```

#### **Step 3: Set the WCB Quantity**

The total number of WCBs in the system must be set and must match on all devices.

```
?WCBQx  (Replace x with the total number of WCBs in the system)
```

Example:

```
?WCBQ3  // There are 3 WCBs in the system
```

#### **Step 4: Configure the ESP-NOW Password**

All WCBs must use the same ESP-NOW password to communicate.

```
?EPASSxxxxxxx
```

Example:

```
?EPASSsecure123  // Set the ESP-NOW password to 'secure123'
```

#### **Step 5: Set the MAC Addresses**

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

#### **Example Setup for a 3-WCB System:**

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
Once configured, reboot each WCB with:

```
?REBOOT
```
The rebooting is critical since a lot of these settings are setup at boot, and not after.

<br>
You don't have to enter thse commands one at a time if you don't want to.  You can chain all these commands using the command delimiter and reboot at the end:

Example:
```
?HW1^?WCB1^?WCBQ3^?EPASSsecure123^?M222^?M333^?REBOOT
```




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
| `?WCBx`           | Set the WCB number (1-8).                                                                                                                                                  |
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

- Kyber must be plugged into Serial 2.
- Forwarding data **between Serial1 and Serial2** when Kyber is enabled. 
- Supports **Kyber Local and Remote modes**. The local mode is for when the Kyber is plugged directly into the WCB and the remote mode is when only the maestro with the ID of 2 is plugged into the WCB.

Kyber is not supported in the Serial only mode.  You must be using the ESPNOW to enable this functionality.

### **5.3 Maestro Support**
- Maestros must be plugged into Serial 1.
- Can trigger Maestros from based on a command sent to the WCB.  You do not need to utilize the Pololu Maestro Library within your code. 

Maestro comms is not supported in the Serial only mode.  You must be using the ESPNOW to enable this functionality. 

### **5.4 FreeRTOS Tasks**

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

This will completely wipe the stored settings, including MAC addresses, ESP-NOW passwords, and stored commands, allowing you to start fresh.  You should "Disable" the erasing of flash before upload to prevent from having to reconfigure the WCB every time you load the sketch onto it.



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
- **Supports up to 8 WCB devices in a network**

### **7.2 Pin Maps**

Refer to `wcb_pin_map.h` for pin configurations based on hardware version.

### **7.3 Additional Resour**[ces](https://github.com/espressif/arduino-esp32)

- [ESP-NOW Documentation](https://github.com/espressif/arduino-esp32)
- [Arduino ESP32 Core](https://github.com/espressif/arduino-esp32)
- [Pololu Maestro Documentation](https://www.pololu.com/docs/0J40)

---

This guide provides a complete reference for configuring and using the **Wireless Communication Board (WCB) system**. 🚀


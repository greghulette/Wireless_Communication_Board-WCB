<h1 style="display:inline; height: 75px; font-size:51px;"><img src="./Images/r2logo.png" style="height: 75px; display: inline;" align="center">Wireless Communication Board (WCB)</h1>

# Software 5.2 release includes PWM Passthrough and Timer-based Commands. Current Software version is 5.3_122034RNOV2025

## **1. Introduction**

### **1.1 Overview**
<br>Background: <br>I was initially having issues getting signals into and out of the dome reliably and wanted a better way to accomplish this.  I started out with I2C like most builders but quickly found out that I2C wasn't meant for longer distances or electrically noisy environments.  I realized that serial communications are better in this type of environment, but wasn't sure how to get serial to the many different devices within my Droid since serial can only be connected to a single device at a time.  There is a way for connected device to replicate the signal out another port and implement serial IDs, but I didn't feel like that was the most efficient means of communication.   

<br>Hence, the reason I made these boards.  I developed these boards to allow the various microcontrollers in R2 to communicate out of their serial port directly to another board, no matter where they are in a droid.  I accomplished this by accepting a serial command into the WCB from a microcontroller, evaluating whether it will go out another locally connected serial port, or forward it to another WCB to send it out to one of their locally connected serial ports.  This is essentially how computer networks work as well.  
<br>I decided to go with a wireless technology between the WCBs to alleviate some of the issues with being in an electrically noisy environment as well as remove the need for passing data through the slip ring between the body and dome. The WCB's are using ESP-NOW, which is a connectionless wireless communication protocol that allows the quick and low-power control of smart devices without a router.  It uses the same frequencies that WiFi uses, but I have not seen any RF congestion issues that traditional WiFi sees at conventions.  By using this method, all you need in the dome is power and you can control the dome without it being physically connected to the body.  

While these boards don't control any components within R2, it does allow for the efficient communication of all other microcontrollers.  These boards also allow you to have multiple serially connected devices to communicate with each other directly and bi-directionally. The serially connected devices can communicate to other serial devices connected to the same WCB, or devices connected to remote WCBs. This is accomplished by adding up to 6 characters to your string that you send to the remote device.

### **1.2 Features**

- **ESP-NOW Communication**: Unicast and broadcast messaging across multiple WCB units.
- **Serial Communications**: Bi-Directional Unicast and Broadcast messages to its 5 Serial ports
- **PWM Passthrough**: Read PWM signals on RX pins and output on TX pins locally or remotely via ESP-NOW
- **Timer-Based Commands**: Schedule command execution with millisecond precision using non-blocking delays
- **Kyber Support**: Forward data between Serial1 and Serial2 with low latency to support Kyber/Maestro Communication.
- **FreeRTOS Multi-Tasking**: Optimized for efficient parallel processing.
- **Dynamic Configuration**: Adjust all configurable settings via command.  Examples are baud rates, stored commands, WCB Number, WCB Quantity, PWM mappings, timer sequences...
- **Persistent Storage**: Save and retrieve settings using NVS (Non-Volatile Storage). These settings are stored in NVS which persists after reboot and even after reloading of the sketch.  
- **Store Command Sequences in NVS**: Store your command sequences in the WCB instead of your controller.  200 character limit per stored command.
- **Maestro Support**: Control Pololu Maestro servo controllers with or without Kyber

### **1.3 Physical Board Versions**

#### **1.3.1 HW Version 1.0**


CAD Image       |    Actual Image       | Bare PCB |
:---------------:|:---------------------:|:-------------:|
<img src="./Images/CADImage.png" style="height: 200px;"> |<img src="./Images/LoadedPCB.jpg" style="height: 200px;"> | <img src="./Images/PCBwithConnectors.jpg" style="height: 200px;">|

Features of the board:
- LilyGo T7 Mini32 V1.5
- 5V Terminal Block
- 5 Serial ports that can be used to communicate with microcontrollers  
- Up to 8 WCB's can be used at once in a fully meshed network
- Communication can be individual (Unicast), or be broadcasted to all devices at once.
- Can support bi-directional communications
- Supports Maestros (up to 8 with Strings, or 2 with Kyber)<br>

***  PLEASE NOTE THE DIRECTION OF THE ESP32 WHEN PLUGGING IT INTO THE PCB ON VERSION 1.0. ***

*** THE MICRO USB PORT SHOULD BE ON THE SIDE WITH THE LABEL.  YOU MAY DESTROY YOUR ESP32 IF PLUGGED IN THE WRONG DIRECTION ***

#### **1.3.2 HW Version 2.1**

CAD Image       |    Actual Image     |
:---------------:|:---------------------:|
<img src="./Images/HWV2.1_CAD.png" style="width: 300px;"><br>|<img src="./Images/HWV2.1_Image_Cropped.jpg" style="width: 300px;">|
 



 Features of the board:
- Integrated ESP32-PICO-MINI-02
- 5V Terminal Block
- 5 Serial ports that can be used to communicate with microcontrollers  
- Up to 8 WCB's can be used at once in a fully meshed network
- Communication can be individual (Unicast), or be broadcasted to all devices at once.
- Can support bi-directional communications
- Supports Maestros (up to 8 with Strings, or 2 with Kyber)

#### **1.3.3 HW Version 2.3/2.4**

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
- Supports Maestros (up to 8 with Strings, or 2 with Kyber)


#### **1.3.4 HW Version 3.1**

CAD Image       |    Actual Image     |         Bare                |
:---------------:|:---------------------:|:---------------------:
<img src="./Images/CAD3.1.png" style="width: 300px;"><br>|<img src="./Images/Image_of_3.1.jpg" style="width: 300px;">|<img src="./Images/CAD3.1_Bare.png" style="width:300px;">|
 



 Features of the board:
- ESP32S3-DevKit-1C
- 5V Terminal Block
- 5 Serial ports that can be used to communicate with microcontrollers  
- Up to 8 WCB's can be used at once in a fully meshed network
- Communication can be individual (Unicast), or be broadcasted to all devices at once.
- Can support bi-directional communications
- Supports Maestros (up to 8 with Strings, or 2 with Kyber)
##### **Build Instructions** ####
  You can find the build instructions for the version 3.1 boards [here](./version3.1_build.md)

HW Versions 1.0 and 2.1/2.3/2.4 are physically different, but have the same capabilities and are operated the exact same way.  They are 100% interoperable with each other and can be mixed in the same network.  All WCBs must be running the same version of Software though to ensure they are interoperable.

### **1.4 Concept of Operations**
Below, you will see some possible connections that can exist to the WCB's.  In the picture, there are only 4 WCBs, but the system can handle up to 8 of them.  Each one of the microcontrollers, represented by a green box, can communicate directly with any other microcontroller, regardless if they are physically connected to the same WCB.  I can envision most people using 2 or 3 WCBs.  One in the body, one in the dome, and one on the dome-plate.

<br>
<img src="./Images/Network_Model_1.jpg"><br>


<br>

Now if we lay the different types of communications over this picture, you can see how the boards can send the various types of messages.  The animation is showing how different messages are handled by the WCBs.  
<br><br>

<img src="./Images/WCB_Unicast.gif">
<br>

As you can see in the above image, you can send any other board a direct message. This is what I'm calling a Unicast message.  
<br>
With this method, your controller would send each command separately and the WCB will process them as they come in.  There is an option to chain the commands together so your controller only has to send one command.  You would separate each of the individual commands with the ^ character.  The WCB will separate the commands when it receives it.  Below is an example
<br><br>

With this method, your controller would send each command separately and the WCB will process them as they come in.  There is an option to chain the commands together so your controller only has to send one command.  You would separate each of the individual commands with the ^ character.  The WCB will separate the commands when it receives it.  Below is an example

<img src ="./Images/Chained_Commands.gif">
<br>
You can see the commands in the previous example are chained together and now the controller is sending that long string of commands to the WCB.  
<br>

If you want to or have a limitation on the length of command you can send to the WCB, you are able to store that chained command on the WCB.  Below is an example on how that transmission would look.  
<br>

<img src="./Images/Stored_Commands.gif">

<br>
The WCBs can also Broadcast messages.  The thought is that you send a command everywhere, and if the receiving device accepts the command, it acts on it.  Otherwise it would ignore the command.  The GIF below shows the broadcast messages.  
![til](./Images/NetworkBroadcastGIF.gif)<br><br> <br><br> 


The Wireless Communication Board (WCB) is a versatile communication system designed to facilitate **ESP-NOW** wireless communication between multiple ESP32-based devices. The system also provides **serial bridging**, command processing, and dynamic configuration for a variety of use cases.

## **2. Command Reference**

### **2.1 Command Reference Intro**
I have broken the command structure down into 2 categories.  One of them is to control the board itself, and the other is to execute commands that transfer the data.  The local commands start with the "?" and the execution commands start with ";". These can be changed as needed via a command

### **2.2 Local Function Commands (**`?COMMANDS`**)**

| **Command**        | **Description**                      |
| :------------------| --------------------------------   |
| `?CONFIG`          | Display current configuration settings.       |
| `?DON`             | Enable debugging mode                         |
| `?DOFF`            | Disable debugging mode.                    |
| `?CSkey,value`     | Store a command with a key-val, separate the name and the stored command with a comma.  You have commas in the command and it will not affect the storing of the commands.|
| `***`              | Only used in conjuction with the stored command above.  This allows you to write a comment for each stored command inline for the key.  Example given below. |
| `?CCLEAR`          | Clear all stored commands | 
| `?CEkey`           | Clear a specific stored command with the key name given | 
| `?WCBx`            | Set the WCB number (1-8).|
| `?WCBQx`           | Set the total number of WCBs in the system MUST match on all boards in system |
| `?WCB_ERASE`       | Erase all WCB stored settings.  |
| `?M2xx`            | Set the 2nd MAC Octet (hex)  0x00 - 0xFE   \*\*\*MUST match on all boards in system   |
| `?M3xx`            | Set the 3rd MAC octet (hex).   0x00 - 0x FE  \*\*\*MUST match on all boards in system  |
| `?Sx0/1`           | Enable/Disable broadcast for Serial x. 0 for disabled, 1 for enabled|
| `?SxBAUDRATE`      | Set baud rate for Serial x. |
| `?EPASSxxxx`       | Update ESP-NOW password.  \*\*\*MUST match on all boards in system |
| `?REBOOT`          | Reboot the WCB |
| `?STOP`            | Stop currently running timer sequence |
| `?MAESTRO_ENABLE`  | Enable Maestro communication |
| `?KYBER_LOCAL`     | Enable Kyber communication locally. |
| `?KYBER_REMOTE`    | Enable Kyber communication remotely. |
| `?KYBER_CLEAR`     | Clear Kyber settings. |
| `?PMSx,outputs`    | Map PWM input port x to output ports. Format: `?PMSx,S1,W2S3` |
| `?PMRx`            | Remove PWM mapping for input port x |
| `?PLIST`           | List all active PWM mappings |
| `?PCLEAR`          | Clear all PWM mappings |
| `?POx`             | Manually set port x as PWM output |
| `?PXx`             | Remove port x from PWM output |
| `?HWx`             | Set the hardware version.   \*\*\*\* MUST set hardware version to use the system. |

### **2.3 Command Character-Based Commands (**`;COMMANDS`**)**

These will be the majority of what you will use to interact with the WCB on a normal basis.  These are not case sensitive, so feel free to use upper or lower case characters.

| **Command**  | **Description**  |
| ------------ | -------------------------------- |
| `;Sxyyy`     | Send a message to Serial x (1-5) with the message of yyy. |
| `;Wxyyy` | Send a unicast message to WCB x. with the message of yyy      |
| `;Ckey`      | Recall a stored command by key.                           |
| `;Mxyy`      | Send a Maestro command (x = Maestro ID, yy = sequence). 1-8 valid Maestro IDs.  0 = send to all Maestros.  ID's must match WCB number they are plugged into. |
| `;Tdelay,command` | Execute command after delay (milliseconds). Can be chained for sequences. |

### **2.4 Syntax for Commands**

#### **2.4.1 WCB to WCB->Serial Communication**
```
;W(x);S(y)(zzzzz....)

x: 1-8 : Target WCB's number.  If sending to WCB2, enter 2
y: 1-5 : Target's Serial Port, if sending to Serial 1 on target board, enter 1
zzzz.... : String to send to end device
```
Examples
```
;W3;S4:PP100  : This would send the string ":PP100" to WCB3, and out its Serial 4 port
;W2;S2#SD0    : This would send the string "#SD0" to WCB2, and out its Serial 2 port
```

#### **2.4.2 Serial Communications Command Syntax**
```
;S(y)(zzzzz....)

y: 1-5 : Target's Serial Port, if sending to Serial 1 on target board, enter 1
zzzz.... : String to send to end device
```
Examples
```
;S4:PP100  : This would send the string ":PP100" out its local Serial 4 port
;S2#SD0    : This would send the string "#SD0" out its local Serial 2 port
```

#### **2.4.3 Timer Commands Syntax**
```
;T(delay),(command)

delay: Milliseconds to wait before executing the next group (1-1800000, max 30 minutes)
command: Any valid WCB command to execute after the delay
```
Examples
```
;T5000,;S1:PP100  : Wait 5 seconds, then send :PP100 to Serial1
;T2000^;S2:PS1^;T3000^;S2:PS2  : Execute ;S2:PS1 immediately, wait 2 seconds, then execute ;S2:PS2 after 3 more seconds
```

#### **2.4.4 Chaining Commands:**
You can chain commands together and have the WCB process those commands one after another.  You can do this by adding a "^" in between different commands in the string.

Example:<br>

 ```   
;W3;S4:PP100^;W3;S2#SD0^;W3;S1:PDA180
```

The command would take that string and break it into 3 different commands and process them immediately.  There is only a few millisecond delay between each command.
```
1. ;W3;S4:PP100
2. ;W3;S2#SD0
3. ;W3;S1:PDA180
```

The delimiter is ^ by default, but also can be changed.

I have tested the following characters (& * . - / ) but do not see why others wouldn't work as well.



## **3. Loading and Configuring the WCB**
**All WCBs are configured prior to shipping, but if you want to upgrade an existing WCB or setup a new one, follow these steps.**

### **Required Drivers**

To recognize the WCB on your computer, ensure you have the correct driver installed for versions 2.x and higher:

- **CP2102N Driver** (Required for many ESP32 boards)

  - Download: [Silicon Labs CP2102N Driver](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers)



### **Required Libraries**

Before compiling the firmware, install the following libraries in the Arduino IDE:


- **EspSoftwareSerial** (For software-based serial communication) By : Dirk Kaar and Peter Lerup, Version tested 8.1.0: 

- **Adafruit NeoPixel** (For controlling LED status indicators) By: Adafruit, Version tested: 1.12.4

### **3.1 Firmware Installation**

1. Install **Arduino IDE** and required ESP32 libraries.  ESP32 board version 3.1
2. Install libraries mentioned above.
3. Clone the WCB firmware repository.
4. Open the `WCB.ino` file found in the Code folder.
5. Compile and upload the firmware to your ESP32 board, selecting "ESP Dev Module" as your board type for versions 2.1-2.4.  Select "ESP32S3 Dev Module" for version 3.1.
6. Open the serial monitor to verify successful setup. You will have to hit the reset button on the WCB to show that it booted up after a reload.

### **3.2 Setting Up the WCB System**

To ensure proper communication between multiple WCB devices, follow these setup steps:

#### **Step 1: Set the Hardware Version**

Each WCB must be configured with the correct hardware version before use. 

**NOTHING WILL WORK WITHOUT PERFORMING THIS STEP!!!!**

Use the following command:

```
?HWx  (Replace x with the hardware version number: `1` for Version 1.0, `21` for Version 2.1, `23` for Version 2.3, `24` for Version 2.4, '31' for version 3.1)
```

Examples:

```
?HW1  // Set WCB to hardware version 1.0
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
?WCB2  // Set this WCB as WCB 2
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

To prevent interference between different WCB systems, all devices in the same system must have the same MAC octets, and be different than other WCB systems.  This is accomplished by changing the 2nd and 3rd octet of the MAC address.  All WCBs will have the 1st, 4th, 5th, and 6th octets match, but the 2nd and 3rd must be unique.  The MAC Address should be 

```
02:xx:yy:00:00:0w
``` 
where the xx is the second octet that you set, the yy is the 3rd octet that you set, and the w, the WCB number.  The w is automatically set when you identified the WCB number.  You set the 2nd and 3rd octets with the following commands:

```
?M2xx  // Set the 2nd MAC octet
?M3xx  // Set the 3rd MAC octet
```

Example:

```
?M222  // Set 2nd MAC octet to 0x22
?M333  // Set 3rd MAC octet to 0x33
```

The valid characters that can be used are 0-9, and A-F.  Do not use FF though because that would introduce a possible interference with other systems.

#### **3.2.1 Example Setup for a 3-WCB System:**

| **WCB Unit** | **Command**       | **Value**          |
| ------------ | ----------------- | ------------------ |
| WCB1         | `?HW1`            | Version 1.0        |
|              | `?WCB1`           | Unit 1             |
|              | `?WCBQ3`          | 3 WCBs total       |
|              | `?EPASSsecure123` | ESP-NOW password   |
|              | `?M222`           | MAC Octet 2 = 0x22 |
|              | `?M333`           | MAC Octet 3 = 0x33 |
| WCB2         | `?HW21`           | Version 2.1        |
|              | `?WCB2`           | Unit 2             |
|              | `?WCBQ3`          | 3 WCBs total       |
|              | `?EPASSsecure123` | ESP-NOW password   |
|              | `?M222`           | MAC Octet 2 = 0x22 |
|              | `?M333`           | MAC Octet 3 = 0x33 |
| WCB3         | `?HW24`            | Version    2.4     |
|              | `?WCB3`           | Unit 3             |
|              | `?WCBQ3`          | 3 WCBs total       |
|              | `?EPASSsecure123` | ESP-NOW password   |
|              | `?M222`           | MAC Octet 2 = 0x22 |
|              | `?M333`           | MAC Octet 3 = 0x33 |

All WCBs must have **matching** `WCBQ`, `EPASS`, `M2`, and `M3` values to communicate properly.  The WCBs HW versions do not need to match.  They are all completely interoperable with one another.

Once configured, reboot each WCB with:

```
?REBOOT
```
The rebooting is critical since a lot of these settings are set up at boot, and not after.

<br>
You don't have to enter these commands one at a time if you don't want to.  You can chain all these commands using the command delimiter and reboot at the end:

Example:
```
?HW1^?WCB1^?WCBQ3^?EPASSsecure123^?M222^?M333^?REBOOT
```
By doing it this way, it makes it easy to configure all the WCBs at once by copying this string and pasting it into a new WCB, only changing the ?WCBx for the WCB number.

## **4. Features Overview/Details**

### **4.1 Serial Communication**

- Supports **Unicast** (Single Serial Port Communications) and **Broadcast** (All Configured Serial Ports Communication). 
- If you don't specify a Serial Port, your message will automatically be broadcasted.  
- The baud rate can be changed via commands.  All serial ports are set to 9600 by default.
- You can choose to allow each serial port to be part of the broadcasts or not.  This will ensure messages are not getting sent to boards that don't want them.  You will have to send commands to these boards directly via unicast messages.  Broadcasts are enabled by default for all ports.
- If you don't specify a Serial Port, your message will automatically be broadcasted to all ports with broadcast enabled. 

### **4.2 ESP-NOW Communication**

- Supports **Unicast** (WCB-to-WCB) and **Broadcast** (WCB group-wide).
- Automatically ignores messages from unrelated groups (different MAC octets and passwords) for security.
- If you don't specify a WCB#, your message will automatically be broadcasted to all WCBs.  

### **4.3 Kyber Support**

The WCB supports the Kyber with both the serial commands, via its MarcDuino port, and the Maestro control via its Maestro port.  The WCB supports the serial commands by default, but there is specific configuration you need to enable in the WCB to enable Maestro communication between the Kyber and Maestro.  

The WCBs have two modes for Kyber support,  **Kyber Local** and **Kyber Remote**. The local mode is for when the Kyber is plugged directly into the WCB along with a Maestro, and the remote mode is when only the maestro is plugged into the WCB but triggered by the Kyber remotely.  

 Connections: 
 - The Maestro must be plugged into Serial 1 
 - The Kyber's Maestro port must be plugged into Serial 2.  
 - The Kyber's MarcDuino port can be plugged into any of the remaining serial ports.  

To enable Kyber Local Mode, use the command `?kyber_local`.  To enable the Kyber Remote mode, use `?kyber_remote`. You should not run both of these modes on the same WCB.  You will be notified which mode you are running during the boot message


Here is a simple wiring diagram to show how it would be connected.

<img src="./Images/Kyber Wiring.png">


<br>I know it's a little hard to see, so here's a little closer view of the wiring.  


<img src="./Images/Kyber_Close_Up.png">

Please note that the Tx from the Kyber goes to the Rx of the WCB, and the Rx of the Kyber goes to the Tx of the WCB


 **NOTE:** Kyber is not supported in the Serial only mode.  You must be using the ESPNOW to enable this functionality.

### **4.4 Maestro Support without Kyber**
If you don't have Kyber, but still use the Maestro, you can still trigger sequences with the WCB.  You do not have to utilize the Pololu Maestro Library to accomplish this if you are using the WCBs.  
All you have to do is send the command `;Mxyy` to the WCB, where x is the ID of the Maestro, and yy is the sequence that you want to start. 

Connections/Requirements:
- Maestros must be plugged into Serial 1.  
- Only 1 Maestro is supported per WCB.
- Maestro ID must match the WCB number that it is plugged into (i.e. Maestro ID of 2 plugged into WCB2, Maestro ID of 3 plugged into WCB3)
- Can have up to 8 Maestros when controlling them this way.
- You can broadcast to all Maestros by using the Maestro ID of 0.  This will trigger all Maestros with the Sequence ID that is given.  

**Note:** Maestro comms is not supported in the Serial only mode.  You must be using the ESPNOW to enable this functionality. 

**NOTE:** Only the Restart Sequence is supported when utilizing the Maestros in this method.  It does not return status or have the ability to do servo passthrough at this time.

### **4.5 Timer-Based Command Execution**

The WCB includes a powerful timer-based command system that allows you to schedule command execution with millisecond precision. Unlike blocking delays, timer commands are **non-blocking** and run in the background using FreeRTOS, allowing the WCB to continue processing other commands while waiting for timer delays to elapse.

#### **4.5.1 Overview and Capabilities**

The timer system provides:
- **Non-blocking delays** - WCB continues to process other commands while waiting
- **Millisecond precision** - Delays from 1ms to 1,800,000ms (30 minutes)
- **Sequential execution** - Commands execute in order with specified delays between them
- **Command groups** - Multiple commands can execute together, then wait for next group
- **Chainable with other commands** - Combine timer commands with normal commands using `^`
- **Emergency stop** - Use `?STOP` to halt any running timer sequence
- **Store in NVS** - Timer sequences can be saved as stored commands

#### **4.5.2 Basic Syntax**

**Format:**
```
;Tdelay,command
```

**Parameters:**
- `delay`: Time in milliseconds to wait before executing next group (1-1,800,000 = 30 minutes)
- `command`: Any valid WCB command (optional)

**Key behavior:**
- The delay is applied **AFTER** the commands in the current group execute
- Multiple commands before a `;T` execute immediately as a group
- The timer then waits the specified delay before executing the next group

#### **4.5.3 Simple Examples**

**Example 1: Single delayed command**
```
;T5000,;S1:PP100
```
*Result:* Waits 5 seconds, then sends `:PP100` to Serial1

**Example 2: Immediate command, then delayed command**
```
;S2:PS1^;T3000^;S2:PS2
```
*Result:* 
1. Immediately sends `:PS1` to Serial2
2. Waits 3 seconds
3. Sends `:PS2` to Serial2

**Example 3: Multiple commands in first group**
```
;S1:PH^;S2:SE01^;T2000^;S1:PS1
```
*Result:*
1. Immediately sends `:PH` to Serial1 AND `:SE01` to Serial2
2. Waits 2 seconds
3. Sends `:PS1` to Serial1

#### **4.5.4 Complex Sequence Examples**

**Example 4: Multi-stage animation sequence**
```
;S1:PS1^;T1000^;S1:PS2^;T1000^;S1:PS3^;T1000^;S1:PS4
```
*Result:*
1. Send `:PS1` to Serial1 (animation frame 1)
2. Wait 1 second
3. Send `:PS2` to Serial1 (animation frame 2)
4. Wait 1 second
5. Send `:PS3` to Serial1 (animation frame 3)
6. Wait 1 second
7. Send `:PS4` to Serial1 (animation frame 4)

**Example 5: Coordinated multi-board sequence**
```
;W1;S1:PH^;W2;S1:PH^;T2000^;W1;S1:PP100^;T1000^;W2;S1:PP100
```
*Result:*
1. Immediately send `:PH` to WCB1's Serial1 AND WCB2's Serial1
2. Wait 2 seconds
3. Send `:PP100` to WCB1's Serial1
4. Wait 1 second
5. Send `:PP100` to WCB2's Serial1

**Example 6: Light show with timing**
```
;S2:A001^;T500^;S2:A002^;T500^;S2:A003^;T500^;S2:A001^;T500^;S2:A000
```
*Result:* Cycles through 4 light patterns with 500ms between each change

**Example 7: Maestro sequence with delays**
```
;M11^;T3000^;M12^;T5000^;M13
```
*Result:*
1. Start Maestro 1 sequence 1
2. Wait 3 seconds
3. Start Maestro 1 sequence 2
4. Wait 5 seconds
5. Start Maestro 1 sequence 3

#### **4.5.5 Timer with PWM and Other Features**

**Example 8: Timed PWM mapping changes**
```
;T5000,?PMS5,S3^;T10000^?PMR5
```
*Result:*
1. Wait 5 seconds
2. Map PWM from Serial5 to Serial3
3. Wait 10 seconds
4. Remove PWM mapping

**Example 9: Sequence with sound and servo**
```
;S4<SH1,M1>^;T200^;M11^;T3000^;M10
```
*Result:*
1. Play sound on Serial4
2. Wait 200ms
3. Start Maestro sequence 1
4. Wait 3 seconds
5. Start Maestro sequence 0 (return to neutral)

#### **4.5.6 Storing Timer Sequences**

Timer sequences can be stored as commands for easy recall:

```
?CSwave_sequence,;M11^;T2000^;S4<SH2,M1>^;T1000^;M10
```

Then recall with:
```
;Cwave_sequence
```

**Example with comments:**
```
?CSanimation,;S1:PS1^;T1000^***Frame 1^;S1:PS2^;T1000^***Frame 2^;S1:PS3^;T1000^***Frame 3
```

#### **4.5.7 How Timer Commands Work**

**Parsing and grouping:**
1. When a command string contains `;T` or `;t`, it triggers timer mode
2. Commands are split by the delimiter (`^`)
3. Commands are grouped together until a `;T` is encountered
4. Each `;T` starts a new group with its specified delay

**Execution flow:**
1. **Group 1** executes immediately (all commands before first `;T`)
2. System waits for the delay specified in first `;T`
3. **Group 2** executes (commands after first `;T` but before second `;T`)
4. System waits for delay specified in second `;T`
5. Process continues until all groups execute

**Visual example:**
```
Command: ;S1:PA^;S2:PB^;T2000^;S1:PC^;T1000^;S2:PD

Parsing:
  Group 1: [;S1:PA, ;S2:PB] delay=0ms
  Group 2: [;S1:PC] delay=2000ms
  Group 3: [;S2:PD] delay=1000ms

Execution timeline:
  T=0ms:    Execute ;S1:PA and ;S2:PB
  T=2000ms: Execute ;S1:PC
  T=3000ms: Execute ;S2:PD
  T=3000ms: Sequence complete
```

#### **4.5.8 Technical Details**

**Delay specifications:**
- **Minimum delay:** 1 millisecond
- **Maximum delay:** 1,800,000 milliseconds (30 minutes)
- **Precision:** Millisecond-accurate using FreeRTOS `millis()` function
- **Automatic capping:** Delays exceeding 30 minutes are automatically limited to 1,800,000ms

**Non-blocking operation:**
- Timer sequences run in main loop via `processCommandGroups()`
- WCB continues to accept and process other commands during delays
- Multiple timer sequences cannot run simultaneously (new sequence stops current one)
- Timer state maintained across command processing cycles

**Command queue integration:**
- Timer commands bypass immediate queue execution
- Each group's commands are enqueued when their delay expires
- Commands within a group execute sequentially through normal queue
- FreeRTOS queue ensures no commands are lost

#### **4.5.9 Stopping Timer Sequences**

**Manual stop:**
```
?STOP
```
Immediately halts any running timer sequence and clears all pending groups.

**Automatic completion:**
- Timer sequences complete automatically after all groups execute
- Debug mode (`?DON`) shows completion message: `✅ Timer command sequence complete`

**Effects of stop:**
- Clears all pending command groups
- Disables timer mode
- Resets group index to 0
- Already-executed commands are NOT reversed

#### **4.5.10 Best Practices**

**Planning sequences:**
1. **Sketch timeline first** - Draw out when each command should execute
2. **Group simultaneous actions** - Commands that execute together should be in same group
3. **Test incrementally** - Start with short delays, verify timing, then extend
4. **Use comments in stored sequences** - Add `***` comments for documentation
5. **Consider total duration** - Plan for maximum 30-minute sequences

**Performance considerations:**
1. **Avoid very short delays** - Delays under 100ms may not be precise due to system overhead
2. **Limit sequence length** - While no hard limit, keep sequences reasonable (<20 groups)
3. **Monitor during execution** - Enable debug mode (`?DON`) to watch execution in real-time
4. **Account for command duration** - Serial commands take time to transmit
5. **ESP-NOW latency** - Remote commands via ESP-NOW add ~5-10ms latency

**Debugging timer sequences:**
1. **Enable debug mode:** `?DON`
   - Shows `[TimerGroup X] Executing command: ...` for each group
   - Displays warning if delay exceeds limit
   - Confirms sequence completion
2. **Test with LED commands** - Visual feedback helps verify timing
3. **Use progressive delays** - Start small, increase gradually to find right timing
4. **Add status commands** - Send status queries to verify expected state

**Common pitfalls:**
1. **Forgetting group behavior** - Commands before first `;T` execute immediately
2. **Wrong delay units** - Always use milliseconds, not seconds
3. **Overlapping sequences** - New timer sequence stops previous one
4. **No delay on last command** - Last command group executes immediately after its delay
5. **Exceeding 30-minute limit** - Commands automatically capped but may not behave as expected

#### **4.5.11 Advanced Timer Techniques**

**Looping via stored commands:**
```
?CSloop1,;S1:PS1^;T1000^;S1:PS2^;T1000^;Cloop1
```
Creates infinite loop (be careful - use `?STOP` to exit)

**Conditional sequences with Maestro:**
```
;M11^;T500^;S4<SH1,M1>^;T2000^;M10^;T500^;S4<SH2,M1>
```
Maestro animation triggers sounds at specific points

**Synchronized multi-board choreography:**
```
?CSsyncdance,;W1;S1:PS1^;W2;S1:PS2^;W3;S1:PS3^;T2000^;W1;S1:PS4^;W2;S1:PS5^;W3;S1:PS6
```
Three boards execute coordinated animations with 2-second transition

**Building complex animations:**
```
?CScomplex,;S1:PH^;T200^;S2:A001^;T100^;S1:PP50^;T200^;S2:A002^;T100^;S1:PP100^;T200^;S2:A003^;T500^;S1:PS1^;S2:A000
```
Coordinates holos, lights, and servos in precise timing

### **4.6 PWM Passthrough System**

The WCB includes a sophisticated PWM passthrough system that allows you to read PWM signals on one serial port's RX pin and output them on other serial ports' TX pins, either locally or on remote WCB boards via ESP-NOW. This enables distributed PWM signal routing across multiple boards in your mesh network, perfect for RC applications, servo control, and FrSky receiver integration.

#### **4.6.1 Overview and Capabilities**

The PWM system provides:
- **Interrupt-based PWM reading** on any serial port RX pin (500-2500μs range)
- **Local PWM output** via GPIO bit-banging with microsecond accuracy
- **Remote PWM output** via ESP-NOW to other WCB boards
- **Multiple output mapping** - one input can drive multiple local and remote outputs
- **Persistent configuration** - all mappings saved to NVS and restored on boot
- **Automatic reboot management** - remote boards auto-reboot when configuration changes

#### **4.6.2 Quick Start Example**

**Simple local passthrough:**
```
?PMS5,S3
```
This reads PWM from Serial5 RX pin and outputs it on Serial3 TX pin.

**Local + remote distribution:**
```
?PMS5,S3,W2S3
```
This reads from Serial5 RX, outputs locally to Serial3 TX, and sends to WCB2's Serial3 TX.

**Multiple remote outputs:**
```
?PMS1,W1S3,W2S3,W3S3
```
This reads from Serial1 RX and outputs to Serial3 TX on WCB1, WCB2, and WCB3.

#### **4.6.3 Command Reference**

| Command | Description | Example |
|---------|-------------|---------|
| `?PMSx,outputs...` | Map PWM input to outputs | `?PMS5,S3,W2S3` |
| `?PMRx` | Remove mapping for input port x | `?PMR5` |
| `?PLIST` | List all PWM mappings | `?PLIST` |
| `?PCLEAR` | Clear all PWM mappings | `?PCLEAR` |
| `?POx` | Manually set port as PWM output | `?PO3` |
| `?PXx` | Remove port from PWM output | `?PX3` |

#### **4.6.4 Mapping Syntax**

**Basic format:**
```
?PMSx,output1,output2,...
```
- `x` = Input port number (1-5)
- Outputs can be local (`Sx`) or remote (`WxSy`)

**Output format:**
- **Local output:** `Sx` where x is serial port 1-5
- **Remote output:** `WxSy` where x is WCB board number (1-9) and y is serial port (1-5)

**Examples:**

Local only:
```
?PMS5,S3
```

Local + remote:
```
?PMS5,S3,W2S3
```

Multiple outputs:
```
?PMS1,S2,S3,W2S4,W3S5
```

Multiple boards, same port:
```
?PMS5,W1S3,W2S3,W3S3
```

#### **4.6.5 How It Works**

**When you create a mapping (`?PMSx,outputs...`):**
1. Local board configures input port RX pin for interrupt-based PWM reading
2. Local output ports are configured and marked as PWM outputs
3. Remote boards receive `?POx` commands via ESP-NOW to configure their output ports
4. All settings saved to NVS (persistent across reboots)
5. Remote boards receive `?REBOOT` command
6. **All boards auto-reboot** to apply configuration
   - Remote boards: 2 seconds
   - Local board: 3 seconds

**When you remove a mapping (`?PMRx`):**
1. Local board detaches interrupts from input port
2. Local output ports removed from tracking
3. Remote boards receive `?PXx` commands to clear their output ports
4. Settings removed from NVS
5. Remote boards receive `?REBOOT` command
6. **All boards auto-reboot** to apply changes

**At boot time:**
1. `initPWM()` loads all mappings from NVS
2. Interrupts attached to configured input ports
3. Output ports configured with proper GPIO settings
4. Boot message displays current PWM configuration
5. `processPWMPassthrough()` task begins monitoring for PWM signals

#### **4.6.6 Boot Message Display**

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

**Priority order (highest to lowest):**
1. **Kyber** - Ports 1 & 2 if configured as Kyber Local/Remote
2. **PWM Input** - Configured RX pins reading PWM signals
3. **PWM Output** - Configured TX pins outputting PWM signals
4. **Normal Serial** - Default serial communication with baud rate

#### **4.6.7 Technical Details**

**PWM Signal Specifications:**
- **Valid pulse width:** 500-2500 microseconds
- **Typical RC/servo range:** 1000-2000 microseconds (1.5ms = center)
- **Out-of-range signals:** Automatically ignored/filtered
- **Signal type:** Standard RC PWM (50Hz typical)

**Interrupt-based reading:**
- Uses hardware interrupts on RX pins (`CHANGE` trigger)
- Measures rising edge to falling edge duration
- Non-blocking operation - runs in background
- No impact on other system operations

**Output methods:**

*Local output:*
- Direct GPIO bit-banging using `digitalWrite()` + `delayMicroseconds()`
- Immediate output on TX pin
- Microsecond-accurate timing
- TX pin configured as `OUTPUT`, default LOW

*Remote output:*
- ESP-NOW message: `;Px[pulsewidth]` sent to target WCB
- Example: `;P31500` = Serial3, 1500μs pulse
- 50ms delay between messages to prevent flooding
- Automatic retry on ESP-NOW failure

**Non-volatile storage (NVS):**

Mappings stored in two NVS namespaces:

*pwm_mappings:*
- Key format: `map0`, `map1`, ... `map9`
- Value format: `inputPort,output1,output2,...`
- Example: `5,S3,W2S3` = Serial5 input, local S3 + remote W2S3

*pwm_outputs:*
- Key format: `port0`, `port1`, ... `port4`
- Stores manually configured output ports
- Loaded at boot to restore configuration

**Auto-reboot behavior:**

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

#### **4.6.8 Usage Examples**

**Example 1: Simple local passthrough**

*Scenario:* Control a servo on Serial3 from PWM signal on Serial5

```
?PMS5,S3
```

*Result:*
- PWM signal connected to Serial5 RX pin is read
- Same PWM signal output on Serial3 TX pin
- Can connect servo directly to Serial3 TX pin
- 3 second reboot to apply

**Example 2: Distribute to multiple remote boards**

*Scenario:* One receiver controls servos on 3 different WCB boards

```
?PMS1,W1S3,W2S3,W3S3
```

*Result:*
- PWM receiver connected to WCB4's Serial1 RX pin
- WCB1, WCB2, and WCB3 all output same signal on their Serial3 TX pins
- Synchronized servo control across multiple boards
- All 4 boards reboot (1 local + 3 remote)

**Example 3: Mix local and remote**

*Scenario:* Control local servo and remote servo from same input

```
?PMS5,S2,W2S4
```

*Result:*
- Input on Serial5 RX
- Output to local Serial2 TX (servo on this board)
- Output to WCB2's Serial4 TX (servo on remote board)
- Both boards reboot

**Example 4: Multiple inputs, different outputs**

*Scenario:* Two separate PWM channels for different servos

```
?PMS4,S2,W1S2
?PMS5,S3,W1S3
```

*Result:*
- Channel 1: Serial4 RX → Serial2 TX local + WCB1 Serial2 TX
- Channel 2: Serial5 RX → Serial3 TX local + WCB1 Serial3 TX
- Independent control of two servo channels
- Both boards reboot after each command (6 seconds total)

**Example 5: Clearing and reconfiguring**

*Scenario:* Change PWM configuration completely

```
?PCLEAR
(wait for reboot)
?PMS5,W2S3
```

*Result:*
- First command clears all existing mappings
- All boards reboot clean
- Second command creates new mapping
- Clean reconfiguration without conflicts

#### **4.6.9 Troubleshooting**

**PWM not working:**

*Symptom:* No PWM output detected

*Solutions:*
1. Check boot message shows correct port configuration
2. Verify PWM signal is within 500-2500μs range
3. Enable debug: `?DON` to see PWM values in serial monitor
4. Check physical connections to RX pins
5. Verify input signal is actual PWM, not serial data

**Remote output not working:**

*Symptom:* Local output works but remote board doesn't respond

*Solutions:*
1. Verify ESP-NOW connection between boards: Send test command `?CONFIG`
2. Check remote board shows port as "Configured for PWM Output" in boot message
3. Ensure both boards have same ESP-NOW password: `?CONFIG` to verify
4. Check WCB numbers are correct in mapping command
5. Verify remote board is within ESP-NOW range (~100m line of sight)
6. Check MAC address configuration matches on both boards

**Ports not resetting:**

*Symptom:* Ports still show as PWM after clearing

*Solutions:*
1. Use `?PCLEAR` to clear all mappings (cleanest method)
2. Verify all boards reboot after clearing (watch serial monitor)
3. Check boot message after reboot - ports should show normal serial
4. If stuck, use `?WCB_ERASE` (factory reset) and reconfigure from scratch
5. Manually remove with `?PMRx` for specific port

**Mapping lost after reboot:**

*Symptom:* Configuration doesn't persist

*Solutions:*
1. Mappings should automatically persist in NVS
2. Check if NVS is corrupted: `?WCB_ERASE` and reconfigure
3. Verify preferences namespaces not full (unlikely with 50 mapping limit)
4. Check for flash wear - ESP32 NVS has ~100,000 write cycles per sector
5. Ensure you're waiting for "Rebooting in 3 seconds" message before power cycling

**Conflicting port usage:**

*Symptom:* Kyber or serial communication broken after PWM config

*Solutions:*
1. Check port priority - Kyber always takes precedence over PWM
2. Don't map PWM to Serial1 or Serial2 if Kyber is configured
3. Review boot message to see actual port assignments
4. Use `?CONFIG` to verify Kyber settings
5. Clear PWM mappings that conflict: `?PMRx` or `?PCLEAR`

#### **4.6.10 Best Practices**

**Planning your mappings:**
1. **Document your setup** - Keep notes on which ports are mapped where
2. **Use consistent numbering** - Serial5 for inputs, Serial3/4 for outputs (example)
3. **Avoid Kyber conflicts** - Don't use Serial1/2 if Kyber is configured
4. **Test locally first** - Verify local passthrough before adding remote outputs
5. **One mapping at a time** - Wait for reboots between configuration changes

**Performance considerations:**
1. **Minimize remote outputs** - Each remote output adds ESP-NOW latency (~5-10ms)
2. **Local outputs are fastest** - Direct GPIO bit-banging has microsecond timing
3. **Limit total mappings** - Max 10 mappings, but 3-5 is practical for performance
4. **ESP-NOW range** - Keep boards within ~100m for reliable communication
5. **Power supply** - Ensure stable power for ESP-NOW radio operation

**Maintenance:**
1. **Regular backups** - Document your configuration with `?CONFIG`
2. **Test after changes** - Always verify PWM passthrough after reconfiguration
3. **Monitor boot messages** - Check for correct port assignments
4. **Use debug mode** - Enable `?DON` when troubleshooting
5. **Keep firmware updated** - Bug fixes and improvements in newer versions

**Safety:**
1. **Disconnect servos before testing** - Prevent unexpected movements
2. **Test PWM ranges** - Verify 1000-2000μs range before connecting hardware
3. **Add failsafes** - Consider timeout behavior if PWM signal is lost
4. **Label connections** - Physical labels on wires prevent mistakes
5. **Emergency stop** - Have `?PCLEAR` ready to disable all PWM quickly

### **4.7 Stored Commands**

You can store commands in the WCB's memory and recall them later.  This is accomplished by saving the commands in NVS (Non-Volatile Storage).  There are 2 parts of the stored commands.  The <i>**Key**</i>, and the <i>**Command**</i>.  The Key is used to identify the command being stored.  The Key can be anything you want with the characters of A-Z and 0-9.  The command can be any string of characters you would normally send the WCB.  The command can include delimiters, so multiple commands can be issued at once from the WCB. 

**Example**
Now that you understand what the key and command do, lets go through how to use them.  You store commands by using the `?CS` command.  The CS stands for Command Store.  The format to store a key/command is `?CSkey,command`.  You would enter the `?CS`, then the key, followed by a comma, then the command.  This comma is only read once so that if you need to store a comma in your command, it will allow that.  So lets save the command of `;m11` to the WCB.  

`?CSwave,;m11`

This saves the command `;m11` with the key of `wave`

Now that the key/command is saved into the WCB, you would call that with the:

 `;cwave`

 If you wanted to store a more complex command to wave and to play a sound, you may want to do something like this

 `?CSWaveAndSound,;m11^;S5<SH1,M1>`

 Now when you issue the command of `;cWaveAndSound`, you start the sequence with the ID of 1 on Maestro 1, and play a sound on the HCR that's connected to Serial port 5 of WCB1.

 You can also have comments in your chain commands.  If you use the 3 stars *** you can add a command to your chain commands.

 ```
 ?cstest,;W2:S2A006^***All Holos to Rainbow^;m02^***All Maestros Seq 2
 ```
 and if you issue a ```?config``` you will see this
 ```
   Key: 'test' -> Value: ';W2:S2A006^***All Holos to Rainbow^;m02^***All Maestros Seq 2'
```

When you run this command with the `;ctest`, it will ignore the comments and only process the valid commands.

#### **4.7.1 Stored Command Example**

For this example, I'm going to assume that there is a Maestro connected to Serial 1 on WCB1 and that the Stealth is connected to Serial 2.  That Maestro has an ID of 1 and has a script with a sequence ID of 1 that triggers a wave function.  


In WCB1, you would issue the command `?CSwave,;m11`.  This saves the command `;m11` with a key of `wave` into the WCB memory and called when it receives `;cwave` command.  Now in the stealth, in the aux string 16, you would put `a=;cwave`.  This is shown in the above on the last line in the config example.  Now you have a more clear understanding what is being called with that aux string.  

#### **4.7.2 Storing Timer Sequences**

Stored commands work excellently with timer sequences, allowing you to create complex timed animations that can be triggered with a single command:

```
?CSanimation,;S1:PS1^;T1000^;S1:PS2^;T1000^;S1:PS3^;T2000^;S1:PS4
```

Then trigger with:
```
;Canimation
```

You can even store sequences that include comments for documentation:
```
?CSdemo,;S1:PH^;T500^***Open panel^;M11^;T2000^***Wave^;S1:PS1^;T1000^***Close panel
```

### **4.8 Stealth Users**
The Stealth users should note that the Stealth uses the character ":" to break up a string when it's executing a function with a string. Myself and many builders also use the ":" in their command syntax and this can cause a complication.  There is an easy solution that can be implemented on the Stealth to combat this.  All you will need to do is change the delimiter that it uses to break up its string.  Add this line towards the top of your config.txt file to accomplish this.

<br>

```
auxdelim=&
```

You can change the `&` to another character if that interferes with something in your system.  If you are only using the stored commands specified below, you may not need to change this auxdelim.  

Other than that change, you can set up the Stealth's config.txt file to send out strings via the serial command like normal.  

Examples to add to Stealth config.txt: 
```
b=3,5,1
g=454,5,2

a=;W3;S1:PP100
a=;W2;S2:SE00
```
The b=3,5,1 assigns button 3's action to send a string out the serial port, designated by the 5 in the command, and to send the first string, designated by the 1 in the command. The 1st string in this example is `;W3;S1:PP100`

The g=454,5,2 assigns the gesture 454(left twice) to send a string out the serial port, and to send the 2nd string.  The string in this example is ";W2;S2:SE00<br>

This is a more comprehensive list of gestures and buttons as an example:

```
b=1,5,13
b=2,5,14
b=3,5,15
b=4,5,16
b=6,1,12
b=7,1,5
b=8,1,6
b=9,1,2
g=4,5,1
g=6,5,2
g=2,5,3
g=8,5,4
g=454,5,5
g=656,5,6
g=252,5,7
g=858,5,8
g=45454,5,9
g=65656,5,10
g=25252,5,11
a=;W2;S1:PP100
a=;W2;S1:PH
a=;W2;S1:PS1
a=;W2;S1:PS2
a=;W2;S1:PS4
a=;W2;S2:SE00
a=;W2;S1:SE00
a=;W2;S1:SE01
a=;W2;S1:SE02
a=;W2;S1:SE03
a=;W2;S1:PS1
a=;S1:PS2
a=;S1:PS3
a=;S1:PDA0
a=;W3;S3:PDA0
a=;cwave
```

In this example, button 3 would make the Stealth send the string `;W3;S3:PDA0` out it's serial port.  The WCB would accept that command and forward the string ":PDA0" out the WCB3's Serial 3 port. <br>

This would be a perfect opportunity to use the stored commands.  You can specify what you want to run in the WCB, then in the Stealth, you just call that sequence. 

## **5.0 Power**
 The WCB accepts only 5V power input.  The WCB can be powered by 2 different ways.  The screw terminal block, or the 5V pins on the serial port.  I do not recommend powering the board with both at the same time. <br>
 
 Note:  The 5V pins on the serial header are not connected to the USB 5V output on versions 2.x.  The 5V pins are only connected to the 5V terminal block.  What this means for you is that if you power another device with the 5V pin in the serial header, you can not power that external board via the USB power.  You must power the WCB via the 5V terminal block to power an external board.  It is ok to have both the USB and 5V terminal supplying power to the board at the same time.  There is on board protection and the board will prefer the 5V source over the USB power. <br><br>

V1.0      |    V2.x+     
:---------------:|:---------------------:
<img src="./Images/PowerOptions.png" style="width: 400px;"><br>|<img src="./Images/PowerV2.1.png" style="width:400px;">
 


## **6.0 Dimensions**

Here are the dimensions for the boards.  With these dimensions, you can plan your setup or make a custom mount for your boards.

V1.0      |    V2.1+    
:---------------:|:---------------------:
|<img src="./Images/DimensionsV1.0.png" style="width: 400px;"><br>|<img src="./Images/DimensionsV2.1.png" style="width:400px;">|
|<img src="./Images/DimensionsV3.1.png">
 

<br><br>

## **7. Troubleshooting & Debugging**

### **Erasing Flash & Resetting WCB to Factory Defaults**

If you need to completely erase the flash memory and reset the WCB to factory defaults, follow these steps using the Arduino IDE:

1. **Open Arduino IDE** and go to **Tools → Board → Select your ESP32 board**.
2. **Set the Flash Erase Mode:**
   - Go to **Tools → Erase All Flash Before Sketch Upload**.
   - Select **"Enabled"** to erase everything before uploading a new sketch.
3. **Upload the WCB firmware** after enabling flash erase.
4. **Reconfigure the WCB** using the setup commands in Section 3.

This will completely wipe the stored settings, including MAC addresses, ESP-NOW passwords, stored commands, and PWM mappings, allowing you to start fresh.  You should "Disable" the erasing of flash before upload to prevent from having to reconfigure the WCB every time you load the sketch onto it.

### **Common Issues**

| **Issue**                     | **Possible Solution**                                              |
| ----------------------------- | ------------------------------------------------------------------ |
| No response from WCB          | Verify power and USB connection. Check baud rate settings.         |
| ESP-NOW messages not received | Ensure correct MAC octets and ESP-NOW password.                    |
| Serial bridging not working   | Confirm correct pin configuration and baud rates.                  |
| Stored commands not executing | Use `?CONFIG` to check saved commands. Ensure key is correct.     |
| Command not being sent        | Try issuing the command you are sending to the WCB in the IDE's Serial Monitor to see if there is an issue with the communication between the WCB, or the incoming serial port.|
| PWM not working               | Check boot message for port configuration. Verify PWM signal is 500-2500μs. Enable debug with `?DON`.|
| Remote PWM output failed      | Verify ESP-NOW connection. Check WCB numbers in mapping. Ensure password matches.|
| Timer sequence not executing  | Verify syntax with `;T` prefix. Check delays are within 1-1,800,000ms. Enable debug to watch execution.|
| Timer sequence stops early    | Check for `?STOP` command in sequence. Verify all groups have valid commands. Enable debug mode.|


## **8. Appendices**

### **8.1 Technical Specifications**

- **ESP32-based board compatibility**
- **Uses WiFi in station mode for ESP-NOW**
- **Supports up to 8 WCB devices in a network**
- **PWM range: 500-2500 microseconds**
- **Timer delay range: 1-1,800,000 milliseconds (30 minutes max)**
- **Interrupt-based PWM reading with microsecond precision**
- **Non-blocking timer execution using FreeRTOS**
- **FreeRTOS multi-tasking for parallel operations**

### **8.2 Pin Maps**

Refer to `wcb_pin_map.h` for pin configurations based on hardware version.

### **8.3 Additional Resources**

- [ESP-NOW Documentation](https://github.com/espressif/arduino-esp32)
- [Arduino ESP32 Core](https://github.com/espressif/arduino-esp32)
- [Pololu Maestro Documentation](https://www.pololu.com/docs/0J40)
- [GitHub Repository](https://github.com/greghulette/Wireless_Communication_Board-WCB)


## **9.0 Ordering**
If you are an astromech user, head over to this thread to order a set.

[Astromech.net Forum Post](https://astromech.net/forums/showthread.php?44271-Wireless-Communication-Boards-(WCB)-Continuous-23-JAN-2024&p=581076#post581076)


## 
This guide provides a complete reference for configuring and using the **Wireless Communication Board (WCB) system** with PWM passthrough and timer-based command execution capabilities. 🚀

**Version:** 5.2_170941ROCT2025
**Author:** Greg Hulette
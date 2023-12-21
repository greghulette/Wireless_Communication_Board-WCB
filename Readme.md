<h1 style="display:; font-size:50px;">Wireless Communication Boards (WCB)</h1>

<h2> Description/Purpose </h2>
I developed these boards to allow the various microcontrollers in R2 to communicate wirelessly.  I was initially having issues getting signals into and out of the dome reliably and wanted a better way to accomplish this.  These boards also allow you to have multiple serially connected devies to communicate with each other in a bi-directional manner.  The wireless technology is using ESP-NOW for it's trasnmissioni protocol.  ESP-NOW is a connectionless wireless communication protocol that allows the quick and low-power control of smart devices without a router.  It uses the same frequencies that WiFi uses, but I have not seen any RF congestion issues that traditional WiFi sees at conventions.  

<h2>Board Overview</h2>
<img src="./Images/CADImage.png" style="width: 200px;">

<br>

 Features of the board: 
- LilyGo T7 v1.5 ESP32Wrover boards
- 5V Terminal Block
- 5 Serial ports that can be used to commuincate with microcontrollers  
    - Serial 3-5 baud rate should be below 57600, but recommend 9600 for more reliable link.
- Up to 9 WCB's can be used at once
- Communication can be individual (Unicast), or be broadcasted to all devices at once.
- Can support bi-directional communications

<br>
<br>
Below, you will see some possible connections that can exist to the WCB's.  In the picture, there are only 4 WCBs, but the system can handle up to 9 of them.  Each one of the microcontrollers, represented by a green box, can communicate directly with any other microcontroller, irregardless if they are physically connected to the same WCB.

<br>
<img src="./Images/OverviewImage.png">

<h2>Command Syntax</h2>
I have broken the command structure down into 2 catagories.  One of them is to control the board itself, and the other is to execute commands that transfer the data.  The local commands start with the "#" and the execution commands start with ":".

The following lists out possible commands for local use

#L01  -  Displays the local hostname.  Useful to identify which board you are looking at in the serial monitor

#L02  -  Restarts the ESP32

#DESPNOW  - Toggles the ESPNOW debuggin to allow you to debug ESPNOW related functions

#DSERIAL  -  Toggles the serial debugging to allow you to debug serial related functions

#DLOOP   -  Toggles the loop debugging to allow you to debug the main loop

#S(x)(yyyy) - Allows you to change the baud rate of a serial port.  Persists after reboot.
 - x: 1-5 : Serial port 1-5
- yyyy: any valid baud rate that the IDE can be set at.  

The following is the syntax for sending commands

Wireless Communication Command Sytax


:W(x):S(y)(zzzzz....)
<br>    - x: 1-9 : Target WCB's number.  If sending to WCB2, enter 2
<br>    - y: 1-5 : Target's Serial Port, if sending to Serial 1 on target board, enter 1
<br>    - zzzz.... : String to send to end device

    Examples
    - :W3:S4:PP100  : This would send the string ":PP100" to WCB3, and out it's Serial 4 port
    - :W2:S2#SD0    : This would send the string "#SD0" to WCB2, and out it's Serial 2 port



Serial Communications Command Syntax

:S(y)(zzzzz....)
<br>    - y: 1-5 : Target's Serial Port, if sending to Serial 1 on target board, enter 1
<br>    - zzzz.... : String to send to end device

    Examples
    - :S4:PP100  : This would send the string ":PP100" to WCB3, and out it's Serial 4 port
    - :S2#SD0    : This would send the string "#SD0" to WCB2, and out it's Serial 2 port

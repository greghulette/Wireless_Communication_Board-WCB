# WCB Serial Monitoring & Broadcast Blocking Guide

## Overview
These features allow you to monitor serial port traffic and control broadcast behavior for debugging and system management.

---

## Serial Port Monitoring

**Purpose:** Mirror data from a serial port to USB and/or Kyber serial for debugging/monitoring.

### Commands

| Command | Description |
|---------|-------------|
| `?SMONx1` | Enable monitoring on Serial port x (1-5) |
| `?SMONx0` | Disable monitoring on Serial port x |
| `?SMON_USB1` | Enable mirroring to USB Serial |
| `?SMON_USB0` | Disable mirroring to USB Serial |
| `?SMON_KYBER1` | Enable mirroring to Kyber serial |
| `?SMON_KYBER0` | Disable mirroring to Kyber serial |

### Example Use Case: Monitor RC Receiver

You want to see what your RC receiver (on Serial3) is sending:

```
?SMON31          → Enable monitoring on Serial3
?SMON_USB1       → Make sure USB mirroring is on (default)
```

Now everything Serial3 receives will be echoed to your USB serial monitor!

### Turn it off:
```
?SMON30          → Disable monitoring on Serial3
```

---

## Broadcast Blocking

**Purpose:** Prevent commands received on a specific port from being broadcast to other devices.

### Commands

| Command | Description |
|---------|-------------|
| `?SBLKx1` | Block broadcasts from Serial port x (1-5) |
| `?SBLKx0` | Allow broadcasts from Serial port x |

### Example Use Case: Isolate Debug Device

You have a debug device on Serial4 that sends lots of commands, but you don't want those commands broadcast to your other devices:

```
?SBLK41          → Block broadcasts from Serial4
```

Now Serial4 can still receive commands, but anything it sends won't be broadcast to other WCBs or serial ports.

### Turn it off:
```
?SBLK40          → Allow broadcasts from Serial4 again
```

---

## Important Notes

1. **Monitoring overrides blocking** - If you enable monitoring on a port, it can broadcast even if blocking is enabled
2. **Settings persist** - All settings are saved to flash and survive reboots
3. **View settings** - Use `?CONFIG` to see current monitoring/blocking status
4. **Backup includes settings** - `?BACKUP` will include all monitoring/blocking settings in the configuration backup string

---

## Common Scenarios

### Scenario 1: Debug an RC Receiver
Monitor what your RC receiver is sending:
```
?SMON31          # Monitor Serial3 (RC receiver)
?SMON_USB1       # Mirror to USB (watch in serial monitor)
```

### Scenario 2: Isolate a Noisy Device
Prevent a chatty device from flooding your network:
```
?SBLK21          # Block Serial2 from broadcasting
```

### Scenario 3: Monitor Kyber Communication
Watch Maestro commands going to/from Kyber:
```
?SMON11          # Monitor Serial1 (Maestro)
?SMON_KYBER1     # Mirror to Kyber serial port
?SMON_USB1       # Also mirror to USB
```

### Scenario 4: Debug Multiple Ports Simultaneously
Monitor multiple ports at once:
```
?SMON21          # Monitor Serial2
?SMON31          # Monitor Serial3
?SMON41          # Monitor Serial4
?SMON_USB1       # Mirror all to USB
```

---

## Viewing Configuration

To see your current monitoring and blocking settings:
```
?CONFIG
```

This will display:
- Which ports have monitoring enabled
- Mirror destinations (USB/Kyber)
- Which ports have broadcast blocking enabled

---

## Default Settings

| Setting | Default Value |
|---------|---------------|
| Port Monitoring | All ports OFF |
| Mirror to USB | ON |
| Mirror to Kyber | OFF |
| Broadcast Blocking | All ports OFF |

---

## Troubleshooting

**Q: I enabled monitoring but don't see output**  
A: Make sure `?SMON_USB1` is enabled (it's on by default). Check that you're monitoring the correct port number.

**Q: I want to see Kyber traffic but it's not showing**  
A: You need both `?SMON11` (monitor Serial1) AND `?SMON_KYBER1` (mirror to Kyber). Also ensure Kyber mode is configured (`?KYBER_LOCAL` or `?KYBER_REMOTE`).

**Q: Blocking isn't working**  
A: If monitoring is enabled on the same port, it overrides blocking. Disable monitoring if you want pure blocking behavior.

**Q: How do I reset everything to defaults?**  
A: Disable monitoring on all ports with `?SMON10`, `?SMON20`, etc., and disable blocking with `?SBLK10`, `?SBLK20`, etc.

---

## Integration with Other Features

### Works With:
- Serial port labels (`?SLx,label`) - Labeled ports show up in monitoring messages
- PWM passthrough - Monitored ports can still be used for PWM
- Kyber modes - Monitoring works in both LOCAL and REMOTE Kyber configurations
- Backup/Restore (`?BACKUP`) - Settings are included in configuration backups

### Limitations:
- Monitoring adds minimal processing overhead but may impact timing-sensitive applications
- Cannot monitor Serial0 (USB) as it's the output destination
- Kyber mirroring only works when Kyber is configured (LOCAL or REMOTE mode)

---

**Software Version:** 5.3+  
**Last Updated:** January 2026

For more information, visit: https://github.com/greghulette/Wireless_Communication_Board-WCB
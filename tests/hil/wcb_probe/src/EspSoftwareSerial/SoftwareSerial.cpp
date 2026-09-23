/*

SoftwareSerial.cpp - Implementation of the Arduino software serial for ESP8266/ESP32.
Copyright (c) 2015-2016 Peter Lerup. All rights reserved.
Copyright (c) 2018-2019 Dirk O. Kaar. All rights reserved.

This library is free software; you can redistribute it and/or
modify it under the terms of the GNU Lesser General Public
License as published by the Free Software Foundation; either
version 2.1 of the License, or (at your option) any later version.

This library is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public
License along with this library; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

*/

#include "SoftwareSerial.h"
#include <Arduino.h>
#if ESPSWSERIAL_LEVEL_RX
#include "hal/gpio_ll.h"   // WCB patch (#78): gpio_ll_set_intr_type - one register write, inlined into the IRAM ISR
#endif

using namespace EspSoftwareSerial;

#ifndef ESP32
uint32_t UARTBase::m_savedPS = 0;
#else
portMUX_TYPE UARTBase::m_interruptsMux = portMUX_INITIALIZER_UNLOCKED;
#endif

ALWAYS_INLINE_ATTR inline void IRAM_ATTR UARTBase::disableInterrupts()
{
#ifndef ESP32
    m_savedPS = xt_rsil(15);
#else
    taskENTER_CRITICAL(&m_interruptsMux);
#endif
}

ALWAYS_INLINE_ATTR inline void IRAM_ATTR UARTBase::restoreInterrupts()
{
#ifndef ESP32
    xt_wsr_ps(m_savedPS);
#else
    taskEXIT_CRITICAL(&m_interruptsMux);
#endif
}

constexpr uint8_t BYTE_ALL_BITS_SET = ~static_cast<uint8_t>(0);

UARTBase::UARTBase() {
}

UARTBase::UARTBase(int8_t rxPin, int8_t txPin, bool invert)
{
    m_rxPin = rxPin;
    m_txPin = txPin;
    m_invert = invert;
}

UARTBase::~UARTBase() {
    end();
}

void UARTBase::setRxGPIOPinMode() {
    if (m_rxValid) {
        pinMode(m_rxPin, m_rxGPIOHasPullUp && m_rxGPIOPullUpEnabled ? INPUT_PULLUP : INPUT);
    }
}

void UARTBase::setTxGPIOPinMode() {
    if (m_txValid) {
        pinMode(m_txPin, m_txGPIOOpenDrain ? OUTPUT_OPEN_DRAIN : OUTPUT);
    }
}

void UARTBase::begin(uint32_t baud, Config config,
    int8_t rxPin, int8_t txPin,
    bool invert) {
    if (-1 != rxPin) m_rxPin = rxPin;
    if (-1 != txPin) m_txPin = txPin;
    m_oneWire = (m_rxPin == m_txPin);
    m_invert = invert;
    m_dataBits = 5 + (config & 07);
    m_parityMode = static_cast<Parity>(config & 070);
    m_stopBits = 1 + ((config & 0300) ? 1 : 0);
    m_pduBits = m_dataBits + static_cast<bool>(m_parityMode) + m_stopBits;
    m_bitTicks = (microsToTicks(1000000UL) + baud / 2) / baud;
    m_intTxEnabled = true;
}

void UARTBase::beginRx(bool hasPullUp, int bufCapacity, int isrBufCapacity) {
    m_rxGPIOHasPullUp = hasPullUp;
    m_rxReg = portInputRegister(digitalPinToPort(m_rxPin));
    m_rxBitMask = digitalPinToBitMask(m_rxPin);
    m_buffer.reset(new circular_queue<uint8_t>((bufCapacity > 0) ? bufCapacity : 64));
    if (m_parityMode)
    {
        m_parityBuffer.reset(new circular_queue<uint8_t>((m_buffer->capacity() + 7) / 8));
        m_parityInPos = m_parityOutPos = 1;
    }
    m_isrBuffer.reset(new circular_queue<uint32_t, UARTBase*>((isrBufCapacity > 0) ?
        isrBufCapacity : m_buffer->capacity() * (2 + m_dataBits + static_cast<bool>(m_parityMode))));
    if (m_buffer && (!m_parityMode || m_parityBuffer) && m_isrBuffer) {
        m_rxValid = true;
        setRxGPIOPinMode();
    }
}

void UARTBase::beginTx() {
#if !defined(ESP8266)
    m_txReg = portOutputRegister(digitalPinToPort(m_txPin));
#endif
    m_txBitMask = digitalPinToBitMask(m_txPin);
    m_txValid = true;
    if (!m_oneWire) {
        setTxGPIOPinMode();
        digitalWrite(m_txPin, !m_invert);
    }
}

void UARTBase::end()
{
    enableRx(false);
    m_txValid = false;
    if (m_buffer) {
        m_buffer.reset();
    }
    m_parityBuffer.reset();
    if (m_isrBuffer) {
        m_isrBuffer.reset();
    }
}

uint32_t UARTBase::baudRate() {
    return 1000000UL / ticksToMicros(m_bitTicks);
}

void UARTBase::setTransmitEnablePin(int8_t txEnablePin) {
    if (-1 != txEnablePin) {
        m_txEnableValid = true;
        m_txEnablePin = txEnablePin;
        pinMode(m_txEnablePin, OUTPUT);
        digitalWrite(m_txEnablePin, LOW);
    }
    else {
        m_txEnableValid = false;
    }
}

void UARTBase::enableIntTx(bool on) {
    m_intTxEnabled = on;
}

void UARTBase::enableRxGPIOPullUp(bool on) {
    m_rxGPIOPullUpEnabled = on;
    setRxGPIOPinMode();
}

void UARTBase::enableTxGPIOOpenDrain(bool on) {
    m_txGPIOOpenDrain = on;
    setTxGPIOPinMode();
}

void UARTBase::enableTx(bool on) {
    if (m_txValid && m_oneWire) {
        if (on) {
            enableRx(false);
            setTxGPIOPinMode();
            digitalWrite(m_txPin, !m_invert);
        }
        else {
            setRxGPIOPinMode();
            enableRx(true);
        }
    }
}

void UARTBase::enableRx(bool on) {
    if (m_rxValid && on != m_rxEnabled) {
        if (on) {
            m_rxLastBit = m_pduBits - 1;
            // Init to stop bit level and current tick
            m_isrLastTick = (microsToTicks(micros()) | 1) ^ m_invert;
            if (m_bitTicks >= microsToTicks(1000000UL / 74880UL)) {
#if ESPSWSERIAL_LEVEL_RX
                // WCB patch (#78): arm a LEVEL interrupt for the level the line is NOT at; rxBitISR flips it after
                // every edge. ONLOW/ONHIGH reach gpio_set_intr_type unchanged (esp32-hal-gpio.c), and a level type
                // leaves the pin out of the IDF's clear-on-entry mask, so its status is cleared after the handler.
                m_rxIsrLevel = *m_rxReg & m_rxBitMask;
                attachInterruptArg(digitalPinToInterrupt(m_rxPin), reinterpret_cast<void (*)(void*)>(rxBitISR), this,
                                   m_rxIsrLevel ? ONLOW : ONHIGH);
#else
                attachInterruptArg(digitalPinToInterrupt(m_rxPin), reinterpret_cast<void (*)(void*)>(rxBitISR), this, CHANGE);
#endif
            }
            else {
                // Above ~74880 baud (115200 on the WCB) rxBitSyncISR stays on its FALLING edge even with
                // ESPSWSERIAL_LEVEL_RX (WCB, #78): it busy-waits a whole frame inside the ISR, so as a level interrupt
                // a line held low would re-enter it forever. That path stays exposed to GPIO-3.14; ?BAUD already warns
                // that soft input at 115200 is not received reliably (WCB_Storage.cpp updateBaudRate).
                attachInterruptArg(digitalPinToInterrupt(m_rxPin), reinterpret_cast<void (*)(void*)>(rxBitSyncISR), this, m_invert ? RISING : FALLING);
            }
        }
        else {
            detachInterrupt(digitalPinToInterrupt(m_rxPin));
        }
        m_rxEnabled = on;
    }
}

int UARTBase::read() {
    if (!m_rxValid) { return -1; }
    if (!m_buffer->available()) {
        rxBits();
        if (!m_buffer->available()) { return -1; }
    }
    auto val = m_buffer->pop();
    if (m_parityBuffer)
    {
        m_lastReadParity = m_parityBuffer->peek() & m_parityOutPos;
        m_parityOutPos <<= 1;
        if (!m_parityOutPos)
        {
            m_parityOutPos = 1;
            m_parityBuffer->pop();
        }
    }
    return val;
}

int UARTBase::read(uint8_t* buffer, size_t size) {
    if (!m_rxValid) { return 0; }
    int avail;
    if (0 == (avail = m_buffer->pop_n(buffer, size))) {
        rxBits();
        avail = m_buffer->pop_n(buffer, size);
    }
    if (!avail) return 0;
    if (m_parityBuffer) {
        uint32_t parityBits = avail;
        while (m_parityOutPos >>= 1) ++parityBits;
        m_parityOutPos = (1 << (parityBits % 8));
        m_parityBuffer->pop_n(nullptr, parityBits / 8);
    }
    return avail;
}

size_t UARTBase::readBytes(uint8_t* buffer, size_t size) {
    if (!m_rxValid || !size) { return 0; }
    size_t count = 0;
    auto start = millis();
    do {
        auto readCnt = read(&buffer[count], size - count);
        count += readCnt;
        if (count >= size) break;
        if (readCnt) {
            start = millis();
        }
        else {
            optimistic_yield(1000UL);
        }
    } while (millis() - start < _timeout);
    return count;
}

int UARTBase::available() {
    if (!m_rxValid) { return 0; }
    rxBits();
    int avail = m_buffer->available();
    if (!avail) {
        optimistic_yield(10000UL);
    }
    return avail;
}

void UARTBase::lazyDelay() {
    // Reenable interrupts while delaying to avoid other tasks piling up
    if (!m_intTxEnabled) { restoreInterrupts(); }
    const auto expired = microsToTicks(micros()) - m_periodStart;
    const int32_t remaining = m_periodDuration - expired;
    const uint32_t ms = remaining > 0 ? ticksToMicros(remaining) / 1000UL : 0;
    if (ms > 0)
    {
        delay(ms);
    }
    else
    {
        optimistic_yield(10000UL);
    }
    // Assure that below-ms part of delays are not elided
    preciseDelay();
    // Disable interrupts again if applicable
    if (!m_intTxEnabled) { disableInterrupts(); }
}

void IRAM_ATTR UARTBase::preciseDelay() {
    uint32_t ticks;
    do {
        ticks = microsToTicks(micros());
    } while ((ticks - m_periodStart) < m_periodDuration);
    m_periodDuration = 0;
    m_periodStart = ticks;
}

void IRAM_ATTR UARTBase::writePeriod(
    uint32_t dutyCycle, uint32_t offCycle, bool withStopBit) {
    preciseDelay();
    if (dutyCycle)
    {
#if defined(ESP8266)
        if (16 == m_txPin) {
            GP16O = 1;
        }
        else {
            GPOS = m_txBitMask;
        }
#else
        *m_txReg = *m_txReg | m_txBitMask;
#endif
        m_periodDuration += dutyCycle;
        if (offCycle || (withStopBit && !m_invert)) {
            if (!withStopBit || m_invert) {
                preciseDelay();
            }
            else {
                lazyDelay();
            }
        }
    }
    if (offCycle)
    {
#if defined(ESP8266)
        if (16 == m_txPin) {
            GP16O = 0;
        }
        else {
            GPOC = m_txBitMask;
        }
#else
        *m_txReg = *m_txReg & ~m_txBitMask;
#endif
        m_periodDuration += offCycle;
        if (withStopBit && m_invert) lazyDelay();
    }
}

size_t UARTBase::write(uint8_t byte) {
    return write(&byte, 1);
}

size_t UARTBase::write(uint8_t byte, Parity parity) {
    return write(&byte, 1, parity);
}

size_t UARTBase::write(const uint8_t* buffer, size_t size) {
    return write(buffer, size, m_parityMode);
}

size_t IRAM_ATTR UARTBase::write(const uint8_t* buffer, size_t size, Parity parity) {
    if (m_rxValid) { rxBits(); }
    if (!m_txValid) { return -1; }

    if (m_txEnableValid) {
        digitalWrite(m_txEnablePin, HIGH);
    }
    // Stop bit: if inverted, LOW, otherwise HIGH
    bool b = !m_invert;
    uint32_t dutyCycle = 0;
    uint32_t offCycle = 0;
    if (!m_intTxEnabled) {
        // Disable interrupts in order to get a clean transmit timing
        disableInterrupts();
    }
    const uint32_t dataMask = ((1UL << m_dataBits) - 1);
    bool withStopBit = true;
    m_periodDuration = 0;
    m_periodStart = microsToTicks(micros());
    for (size_t cnt = 0; cnt < size; ++cnt) {
        uint8_t byte = pgm_read_byte(buffer + cnt) & dataMask;
        // push LSB start-data-parity-stop bit pattern into uint32_t
        // Stop bits: HIGH
        uint32_t word = ~0UL;
        // inverted parity bit, performance tweak for xor all-bits-set word
        if (parity && m_parityMode)
        {
            uint32_t parityBit;
            switch (parity)
            {
            case PARITY_EVEN:
                // from inverted, so use odd parity
                parityBit = byte;
                parityBit ^= parityBit >> 4;
                parityBit &= 0xf;
                parityBit = (0x9669 >> parityBit) & 1;
                break;
            case PARITY_ODD:
                // from inverted, so use even parity
                parityBit = byte;
                parityBit ^= parityBit >> 4;
                parityBit &= 0xf;
                parityBit = (0x6996 >> parityBit) & 1;
                break;
            case PARITY_MARK:
                parityBit = 0;
                break;
            case PARITY_SPACE:
                // suppresses warning parityBit uninitialized
            default:
                parityBit = 1;
                break;
            }
            word ^= parityBit;
        }
        word <<= m_dataBits;
        word |= byte;
        // Start bit: LOW
        word <<= 1;
        if (m_invert) word = ~word;
        for (int i = 0; i <= m_pduBits; ++i) {
            bool pb = b;
            b = word & (1UL << i);
            if (!pb && b) {
                writePeriod(dutyCycle, offCycle, withStopBit);
                withStopBit = false;
                dutyCycle = offCycle = 0;
            }
            if (b) {
                dutyCycle += m_bitTicks;
            }
            else {
                offCycle += m_bitTicks;
            }
        }
        withStopBit = true;
    }
    writePeriod(dutyCycle, offCycle, true);
    if (!m_intTxEnabled) {
        // restore the interrupt state if applicable
        restoreInterrupts();
    }
    if (m_txEnableValid) {
        digitalWrite(m_txEnablePin, LOW);
    }
    return size;
}

void UARTBase::flush() {
    if (!m_rxValid) { return; }
    m_buffer->flush();
    if (m_parityBuffer)
    {
        m_parityInPos = m_parityOutPos = 1;
        m_parityBuffer->flush();
    }
}

bool UARTBase::overflow() {
    bool res = m_overflow;
    m_overflow = false;
    return res;
}

int UARTBase::peek() {
    if (!m_rxValid) { return -1; }
    if (!m_buffer->available()) {
        rxBits();
        if (!m_buffer->available()) return -1;
    }
    auto val = m_buffer->peek();
    if (m_parityBuffer) m_lastReadParity = m_parityBuffer->peek() & m_parityOutPos;
    return val;
}

void UARTBase::rxBits() {
#ifdef ESP8266
    if (m_isrOverflow.load()) {
        m_overflow = true;
        m_isrOverflow.store(false);
    }
#else
    if (m_isrOverflow.exchange(false)) {
        m_overflow = true;
    }
#endif

    m_isrBuffer->for_each(m_isrBufferForEachDel);

    // A stop bit can go undetected if leading data bits are at same level
    // and there was also no next start bit yet, so one word may be pending.
    // Check that there was no new ISR data received in the meantime, inserting an
    // extraneous stop level bit out of sequence breaks rx.
    if (m_rxLastBit < m_pduBits - 1) {
        const uint32_t detectionTicks = (m_pduBits - 1 - m_rxLastBit) * m_bitTicks;
        if (!m_isrBuffer->available() && microsToTicks(micros()) - m_isrLastTick > detectionTicks) {
            // Produce faux stop bit level, prevents start bit maldetection
            // tick's LSB is repurposed for the level bit
            rxBits(((m_isrLastTick + detectionTicks) | 1) ^ m_invert);
        }
    }
}

void UARTBase::rxBits(const uint32_t isrTick) {
    const bool level = (m_isrLastTick & 1) ^ m_invert;

    // error introduced by edge value in LSB of isrTick is negligible
    uint32_t ticks = isrTick - m_isrLastTick;
    m_isrLastTick = isrTick;

    uint32_t bits = ticks / m_bitTicks;
    if (ticks % m_bitTicks > (m_bitTicks >> 1)) ++bits;
    while (bits > 0) {
        // start bit detection
        if (m_rxLastBit >= (m_pduBits - 1)) {
            // leading edge of start bit?
            if (level) break;
            m_rxLastBit = -1;
            --bits;
            continue;
        }
        // data bits
        if (m_rxLastBit < (m_dataBits - 1)) {
            uint8_t dataBits = min(bits, static_cast<uint32_t>(m_dataBits - 1 - m_rxLastBit));
            m_rxLastBit += dataBits;
            bits -= dataBits;
            m_rxCurByte >>= dataBits;
            if (level) { m_rxCurByte |= (BYTE_ALL_BITS_SET << (8 - dataBits)); }
            continue;
        }
        // parity bit
        if (m_parityMode && m_rxLastBit == (m_dataBits - 1)) {
            ++m_rxLastBit;
            --bits;
            m_rxCurParity = level;
            continue;
        }
        // stop bits
        // Store the received value in the buffer unless we have an overflow
        // if not high stop bit level, discard word
        if (bits >= static_cast<uint32_t>(m_pduBits - 1 - m_rxLastBit) && level) {
            m_rxCurByte >>= (sizeof(uint8_t) * 8 - m_dataBits);
            if (!m_buffer->push(m_rxCurByte)) {
                m_overflow = true;
            }
            else {
                if (m_parityBuffer)
                {
                    if (m_rxCurParity) {
                        m_parityBuffer->pushpeek() |= m_parityInPos;
                    }
                    else {
                        m_parityBuffer->pushpeek() &= ~m_parityInPos;
                    }
                    m_parityInPos <<= 1;
                    if (!m_parityInPos)
                    {
                        m_parityBuffer->push();
                        m_parityInPos = 1;
                    }
                }
            }
        }
        m_rxLastBit = m_pduBits - 1;
        // reset to 0 is important for masked bit logic
        m_rxCurByte = 0;
        m_rxCurParity = false;
        break;
    }
}

void IRAM_ATTR UARTBase::rxBitISR(UARTBase* self) {
#if ESPSWSERIAL_LEVEL_RX
    // WCB patch (tracker #78): level-emulated edges, ESP32 erratum GPIO-3.14 - see SoftwareSerial.h.
    // The pin is armed for the level it is NOT at. Each entry re-arms for the opposite of the level just read, FIRST,
    // then records a transition only if the level differs from the one last recorded: the same level means the
    // erratum's spurious entry (or a status the IDF had not cleared yet) - no edge happened, so nothing is pushed.
    // After arming, the pad is read again: if it moved meanwhile, that transition is recorded too and the pin re-armed.
    // That narrows, but cannot close, the window in which the line reaches the armed level before the IDF clears a
    // level type's status (gpio_isr_loop writes STATUS_W1TC only after the callback returns through __onPinInterrupt).
    // A transition in that tail, and the guard exit (which deliberately leaves the pin armed for the level the line is
    // at), are caught because an ESP32 level status re-asserts while its condition holds (SoftwareSerial.h): the pin
    // re-fires at once and the next entry records the transition. The loop repeats only when the line toggles within a
    // few register cycles (a glitch); the cap keeps a pathological glitch train from holding the ISR.
    // The ticks carry the same meaning as the stock edge ISR: pad level after the change, in the tick's LSB.
    // Called only from core 1's GPIO ISR (via Arduino's __onPinInterrupt) - the int_type write is a read-modify-write
    // of GPIO_PINn_REG, so the pin must never be reconfigured from a core-0 task (CLAUDE.md rule 13).
    bool level = *self->m_rxReg & self->m_rxBitMask;
    uint32_t curTick = microsToTicks(micros());
    const bool empty = !self->m_isrBuffer->available();
    bool pushed = false;
    for (int guard = 0; ; ++guard) {
        gpio_ll_set_intr_type(&GPIO, self->m_rxPin, level ? GPIO_INTR_LOW_LEVEL : GPIO_INTR_HIGH_LEVEL);
        if (level != self->m_rxIsrLevel) {
            self->m_rxIsrLevel = level;
            // Store level and tick in the buffer unless we have an overflow
            // tick's LSB is repurposed for the level bit
            if (!self->m_isrBuffer->push((curTick | 1U) ^ !level)) self->m_isrOverflow.store(true);
            pushed = true;
        }
        const bool now = *self->m_rxReg & self->m_rxBitMask;
        if (now == level || guard >= 3) break;
        level = now;
        curTick = microsToTicks(micros());
    }
    // Trigger rx callback only when receiver is starved
    if (pushed && empty) self->m_rxHandler();
#else
    const bool level = *self->m_rxReg & self->m_rxBitMask;
    const uint32_t curTick = microsToTicks(micros());
    const bool empty = !self->m_isrBuffer->available();

    // Store level and tick in the buffer unless we have an overflow
    // tick's LSB is repurposed for the level bit
    if (!self->m_isrBuffer->push((curTick | 1U) ^ !level)) self->m_isrOverflow.store(true);
    // Trigger rx callback only when receiver is starved
    if (empty) self->m_rxHandler();
#endif // ESPSWSERIAL_LEVEL_RX
}

void IRAM_ATTR UARTBase::rxBitSyncISR(UARTBase* self) {
    bool level = self->m_invert;
    const uint32_t start = microsToTicks(micros());
    uint32_t wait = self->m_bitTicks;
    const bool empty = !self->m_isrBuffer->available();

    // Store level and tick in the buffer unless we have an overflow
    // tick's LSB is repurposed for the level bit
    if (!self->m_isrBuffer->push(((start + wait) | 1U) ^ !level)) self->m_isrOverflow.store(true);

    for (uint32_t i = 0; i < self->m_pduBits; ++i) {
        while (microsToTicks(micros()) - start < wait) {};
        wait += self->m_bitTicks;

        // Store level and tick in the buffer unless we have an overflow
        // tick's LSB is repurposed for the level bit
        if (static_cast<bool>(*self->m_rxReg & self->m_rxBitMask) != level)
        {
            if (!self->m_isrBuffer->push(((start + wait) | 1U) ^ level)) self->m_isrOverflow.store(true);
            level = !level;
        }
    }
    // Trigger rx callback only when receiver is starved
    if (empty) self->m_rxHandler();
}

void UARTBase::onReceive(const Delegate<void(), void*>& handler) {
    disableInterrupts();
    m_rxHandler = handler;
    restoreInterrupts();
}

void UARTBase::onReceive(Delegate<void(), void*>&& handler) {
    disableInterrupts();
    m_rxHandler = std::move(handler);
    restoreInterrupts();
}

#if __GNUC__ < 12
// The template member functions below must be in IRAM, but due to a bug GCC doesn't currently
// honor the attribute. Instead, it is possible to do explicit specialization and adorn
// these with the IRAM attribute:
// Delegate<>::operator (), circular_queue<>::available,
// circular_queue<>::available_for_push, circular_queue<>::push_peek, circular_queue<>::push

template void IRAM_ATTR delegate::detail::DelegateImpl<void*, void>::operator()() const;
template size_t IRAM_ATTR circular_queue<uint32_t, UARTBase*>::available() const;
template bool IRAM_ATTR circular_queue<uint32_t, UARTBase*>::push(uint32_t&&);
template bool IRAM_ATTR circular_queue<uint32_t, UARTBase*>::push(const uint32_t&);
#endif // __GNUC__ < 12


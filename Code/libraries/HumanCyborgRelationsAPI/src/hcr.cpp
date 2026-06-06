#include "hcr.h"
#include <Stream.h>
// #include <Wire.h>

#if defined(ARDUINO) && ARDUINO >=100
#include <Arduino.h>
#else
#include <WProgram.h>
#include <avr/pgmspace.h>
#define F(str) (str)
#endif

#ifdef ARDUINO_ARCH_AVR
#include <SoftwareSerial.h>
#endif

#ifdef ARDUINO_ARCH_PIC32
#include <SoftwareSerial.h>
#endif

/*!
  @brief   Start the HCR API
*/
/* HCRVocalizer::HCRVocalizer(uint16_t n, byte connectiontype, int refreshRate)
: begun(false), connection(0x00), commandCache(""), refreshSpeed(0),
  emote_happy(0), emote_sad(0), emote_mad(0), emote_scared(0),
  state_override(0),state_musing(0),state_files(0),state_duration(0.00),
  state_chv(0),state_cha(0),state_chb(0), deviceID(n)
{
    connection = connectiontype;
    refreshSpeed = refreshRate;
}; */

// HCRVocalizer::HCRVocalizer(const uint8_t addr, TwoWire &i2c)
//     : _i2caddr(addr), _i2c(&i2c), _serialBaud(400000)
// {
//     connectionType=0x03;
// }

// HCRVocalizer::HCRVocalizer(const uint8_t addr, TwoWire &i2c, int baud)
//     : _i2caddr(addr), _i2c(&i2c), _serialBaud(baud)
// {
//     connectionType=0x03;
// }

HCRVocalizer::HCRVocalizer(HardwareSerial *conn,int baud)
    : _serial(conn), _serialBaud(baud)
{
    connectionType=0x01;
}

HCRVocalizer::HCRVocalizer(SoftwareSerial *conn,int baud)
    : _softserial(conn), _serialBaud(baud)
{
    connectionType=0x02;
}

HCRVocalizer::HCRVocalizer(int rx, int tx,int baud)
    : _serialBaud(9600)
{
    #if defined(ARDUINO_ARCH_AVR) || defined(ARDUINO_ARCH_PIC32)
    connectionType=0x02;
    _softserial = new SoftwareSerial(rx,tx);
    #endif
}

/*!
  @brief    Begin the Human-Cyborg Relations Controller API
*/
void HCRVocalizer::begin(void)
{
    begin(125); // Default 125ms Refresh
}

/*!
  @brief    Begin the Human-Cyborg Relations Controller API
*/
void HCRVocalizer::begin(const uint16_t refspeed)
{
    refreshSpeed = refspeed;
    switch (connectionType)
    {
        case 0x01:
#ifdef ARDUINO_ARCH_AVR
            _serial->begin(_serialBaud);
#elif defined ARDUINO_ARCH_SAM
            ((USARTClass *)_serial)->begin(_serialBaud);
#elif defined ARDUINO_ARCH_SAMD  // Arduino Zero, Zero Pro, M0 and M0 Pro
            ((HardwareSerial *)_serial)->begin(_serialBaud);
#elif defined ARDUINO_ARCH_PIC32
            _serial->begin(_serialBaud);
#else
            _serial->begin(_serialBaud);
#endif
            while (!_serial);
            _serial->println("Begin _serial");
            break;
        case 0x02:
#ifdef ARDUINO_ARCH_AVR
            ((SoftwareSerial *)_softserial)->begin(_serialBaud);
#elif defined ARDUINO_ARCH_PIC32
            ((SoftwareSerial *)_softserial)->begin(_serialBaud);
#endif
            while (!_softserial);
            _softserial->println("Begin _softserial");
            break;
        case 0x03:
            // if (_i2caddr>0){
            //     _i2c->begin();
            //     _i2c->setClock(_serialBaud);
            //     _i2c->setWireTimeout(3000 /* us */, true /* reset_on_timeout */);
            // }
            break;
        default:
            Serial.print("HCR: No Connection Setup Specified");
            break;
    }
}

unsigned long HCRVocalizerTimer = millis();
void HCRVocalizer::update(void)
{
    if (millis() > HCRVocalizerTimer && refreshSpeed > 0) {
        HCRVocalizerTimer = millis() + refreshSpeed;
        transmit((String)"<QD>",true);
        // Serial.print("emote_happy: "); Serial.println(emote_happy);
        // Serial.print("emote_sad: "); Serial.println(emote_sad);
        // Serial.print("emote_mad: "); Serial.println(emote_mad);
        // Serial.print("emote_scared: "); Serial.println(emote_scared);
        // Serial.print("state_override: "); Serial.println(state_override);
        // Serial.print("state_musing: "); Serial.println(state_musing);
        // Serial.print("state_files: "); Serial.println(state_files);
        // Serial.print("state_duration: "); Serial.println(state_duration);
        // Serial.print("state_chv: "); Serial.println(state_chv);
        // Serial.print("state_cha: "); Serial.println(state_cha);
        // Serial.print("state_chb: "); Serial.println(state_chb);
    }
    receive();
}

void HCRVocalizer::sendCommand(String command)
{
    command = hcrstartMarker+command+hcrendMarker;
    transmit(command,false);
}

void HCRVocalizer::transmit(String command)
{
    transmit(command,false);
}

void HCRVocalizer::transmit(String command, bool retry)
{ 
    if (command == "")
    {
        return;
    }

    switch (connectionType)
    {
        case 0x01:
            _serial->write((command + "\n").c_str());
            break;
        case 0x02:
            _softserial->write((command + "\n").c_str());
            break;
        case 0x03:
            // int i2cStatus = 0;

            // _i2c->beginTransmission((byte)_i2caddr);
            // _i2c->write((command + "\n").c_str());
            // i2cStatus = _i2c->endTransmission();

            // // Serial.print(command); Serial.print("-"); Serial.print(i2cStatus); Serial.println(";");

            // if (i2cStatus > 2)
            // {
            //     _i2c->endTransmission();
            //     if (!retry)
            //     {
            //         delay(100);
            //         Serial.print("->");
            //         transmit(command,true);
            //     }
            // }
            break;
        default: break;
    }
}

void HCRVocalizer::receive(void)
{
    switch (connectionType)
    {
        case 0x01:
            if (_serial->available())
            {
                char ch = _serial->read();
                receiveData(ch);
            }
            break;
        case 0x02:
            if (_softserial->available())
            {
                char ch = _softserial->read();
                receiveData(ch);
            }
            break;
        case 0x03:
            // int bytes = _i2c->requestFrom((int)_i2caddr,(int)8);
            // Serial.print("i2c {");
            // Serial.print(bytes);
            // if (_i2c->available())
            // {
            //     Serial.print("} receive: ");
            //     while (_i2c->available())
            //     {
            //         byte ch = _i2c->read();
            //         Serial.print((char)ch);
            //         receiveData(ch);
            //     }
            //     Serial.println(";");
            // }
            // else
            // {
            //     Serial.println("} - No Response;");
            // }
            break;
        default: break;
    }
}

void HCRVocalizer::receiveData(char ch)
{
    if (ch == '<')
        return;

    if (ch == '>' || ch == '\r' || ch == '\n' || ch == 0) {
        cmdBuffer[cmdPos] = '\0';
        cmdPos = 0;
        if (*cmdBuffer != '\0') {
            processCommands(cmdBuffer);
        }
    }
    else if (cmdPos < (int)sizeof(cmdBuffer)) {
        cmdBuffer[cmdPos++] = ch;
    }

    if (cmdPos == (int)sizeof(cmdBuffer)) {
        cmdBuffer[cmdPos-1] = '\0';
        cmdPos = 0;
        if (*cmdBuffer != '\0') {
            processCommands(cmdBuffer);
        }
    }
}

void HCRVocalizer::receiveData(String data)
{
    //
}

void HCRVocalizer::processCommands(char* input)
{
    Serial.println(input);
    String response = ToString(input);
    String qC = getValue(response,',',0);

    if (qC.equals((String)"QE"))
    {
        emote_happy = getValue(response,',',1).toInt();
        emote_sad = getValue(response,',',2).toInt();
        emote_mad = getValue(response,',',3).toInt();
        emote_scared = getValue(response,',',4).toInt();
    }
    else if (qC.equals((String)"QEH"))
    {
        Serial.print("QEH::"); Serial.println(getValue(response,',',1).toInt());
        emote_happy = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QES"))
    {
        Serial.print("QES::"); Serial.println(getValue(response,',',1).toInt());
        emote_sad = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QEM"))
    {
        Serial.print("QEM::"); Serial.println(getValue(response,',',1).toInt());
        emote_mad = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QEC"))
    {
        Serial.print("QEC::"); Serial.println(getValue(response,',',1).toInt());
        emote_scared = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QO"))
    {
        Serial.print("QO::"); Serial.println(getValue(response,',',1).toInt());
        state_override = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QM"))
    {
        Serial.print("QM::"); Serial.println(getValue(response,',',1).toInt());
        state_musing = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QF"))
    {
        Serial.print("QF::"); Serial.println(getValue(response,',',1).toInt());
        state_files = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QT"))
    {
        Serial.print("QT::"); Serial.println(getValue(response,',',1).toFloat());
        state_duration = getValue(response,',',1).toFloat();
    }
    else if (qC.equals((String)"QPV"))
    {
        Serial.print("QPV::"); Serial.println(getValue(response,',',1).toInt());
        state_chv = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QPA"))
    {
        Serial.print("QPA::"); Serial.println(getValue(response,',',1).toInt());
        state_cha = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QPB"))
    {
        Serial.print("QPB::"); Serial.println(getValue(response,',',1).toInt());
        state_chb = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QVV"))
    {
        Serial.print("QVV::"); Serial.println(getValue(response,',',1).toInt());
        Volume_V = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QVA"))
    {
        Serial.print("QVA::"); Serial.println(getValue(response,',',1).toInt());
        Volume_A = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"QVB"))
    {
        Serial.print("QVB::"); Serial.println(getValue(response,',',1).toInt());
        Volume_B = getValue(response,',',1).toInt();
    }
    else if (qC.equals((String)"DF"))
    {
        emote_happy = getValue(response,',',1).toInt();
        emote_sad = getValue(response,',',2).toInt();
        emote_mad = getValue(response,',',3).toInt();
        emote_scared = getValue(response,',',4).toInt();
        state_override = getValue(response,',',5).toInt();
        state_musing = getValue(response,',',6).toInt();
        state_files = getValue(response,',',7).toInt();
        state_duration = getValue(response,',',8).toFloat();
        state_chv = getValue(response,',',9).toInt();
        state_cha = getValue(response,',',10).toInt();
        state_chb = getValue(response,',',11).toInt();
    }
}

//----------------------------------------------------
//----------------------------------------------------
//----------------------------------------------------
//----------------------------------------------------
//----------------------------------------------------
//----------------------------------------------------

void HCRVocalizer::Trigger(int e, int v)
{
    Stimulate(e,v);
}

void HCRVocalizer::Stimulate(int e, int v)
{
    if (e < 0 || e > 4) return;
    if (e == 4) {
        Overload();
    } else {
        char emoteprefix[] = "HSMC";
        String msg = "S" + ToString((char) emoteprefix[e]) + ToString(v) + ",QE" + ToString((char) emoteprefix[e]) + ",QT";
        sendCommand(msg);
    }
}

void HCRVocalizer::Overload(void)
{
    String msg = "SE,QT";
    sendCommand(msg);
}

void HCRVocalizer::Muse(void)
{
    String msg = "MM";
    sendCommand(msg);
}

void HCRVocalizer::Muse(int min, int max)
{
    if (min < 0 || min > 99) return;
    if (max < 0 || max > 99) return;
    String msg = "MN" + ToString(min) + ",MX" + ToString(max);
    sendCommand(msg);
}

void HCRVocalizer::Stop(void)
{
    StopEmote();
    StopWAV(0);
    StopWAV(1);
    StopWAV(2);
}

void HCRVocalizer::StopEmote(void) {
    String msg = "PSV,QT";
    sendCommand(msg);
}

void HCRVocalizer::OverrideEmotions(int v) {
    if (v < 0 || v > 1) return;
    String msg = "O" + ToString(v) + ",QO";
    sendCommand(msg);
}

void HCRVocalizer::ResetEmotions(void) {
    String msg = "OR,QE";
    sendCommand(msg);
}

void HCRVocalizer::SetEmotion(int e,int v) {
    if (e < 0 || e > 3) return;
    if (v < 0 || v > 99) return;
    char emoteprefix[] = "HSMC";
    String msg = "O" + ToString((char) emoteprefix[e]) + ToString(v) + ",QE" + ToString((char) emoteprefix[e]);
    sendCommand(msg);
}

void HCRVocalizer::SetMuse(int v) {
    if (v < 0 || v > 1) return;
    String msg = "M" + ToString(v) + ",QM";
    sendCommand(msg);
}

unsigned long lastPlayWAV = millis();
void HCRVocalizer::PlayWAV(int ch,int fileNumber)
{
    if (millis() > lastPlayWAV) {
        String fileName = String(fileNumber, DEC);
        for (int i = fileName.length(); i < 4; i++) {
            fileName = "0" + fileName;
        }
        PlayWAV(ch,fileName);
    }
}

void HCRVocalizer::PlayWAV(int ch,String file) {
    if (millis() > lastPlayWAV) {
        lastPlayWAV = millis() + 5000;
        char channel[] = "VAB";
        String msg = "C" + ToString((char) channel[ch]) + file + ",QP" + ToString((char) channel[ch]);
        sendCommand(msg);
    }
}

void HCRVocalizer::StopWAV(int ch) {
    char channel[] = "VAB";
    String msg = "PS" + ToString((char) channel[ch]) + ",QP" + ToString((char) channel[ch]);
    sendCommand(msg);
}

void HCRVocalizer::SetVolume(int e,int v) {
    if (e < 0 || e > 2) return;
    char channel[] = "VAB";
    String msg = "PV" + ToString((char) channel[e]) + ToString(v);
    sendCommand(msg);
}

void HCRVocalizer::getUpdate(void)
{
    sendCommand((String)"QD");
}

/*!
    Query
*/
int* HCRVocalizer::GetEmotions(void) {
    static int response[4] = {
        (int)emote_happy,
        (int)emote_sad,
        (int)emote_mad,
        (int)emote_scared
    };
    return response;
}

int HCRVocalizer::GetEmotion(int e) {
    if (e < 0 || e > 3) return 0;
    int response[4] = {
        (int)emote_happy,
        (int)emote_sad,
        (int)emote_mad,
        (int)emote_scared
    };
    return response[e];
}

float HCRVocalizer::GetDuration(void) {
    float duration = state_duration;
    return duration;
}

int HCRVocalizer::GetOverride(void) {
    int override = state_override;
    return override;
}

bool HCRVocalizer::IsPlaying(void) {
    return IsPlaying(3);
}

bool HCRVocalizer::IsPlaying(int ch) {
    if (ch == 0) {
        return state_chv > 0;
    } else if (ch == 1) {
        return state_cha >= 0;
    } else if (ch == 2) {
        return state_chb >= 0;
    } else {
        return (state_chv + (state_cha + 1) + (state_chb + 1)) > 0;
    }
}

int HCRVocalizer::GetMuse(void) {
    int musing = state_musing;
    return musing;
}

int HCRVocalizer::GetWAVCount(void) {
    int wavcount = state_files;
    return wavcount;
}

int HCRVocalizer::GetPlayingWAV(int ch) {
    float wavplaying = 0;
    if (ch == 1) {
        wavplaying = state_cha;
    } else if (ch == 2) {
        wavplaying = state_chb;
    } else {
        wavplaying = state_chv;
    };
    return wavplaying;
}

float HCRVocalizer::getVolume(int ch)
{
    if (ch < 0 || ch > 2) return 0;
    char channel[] = "VAB";
    String msg = "QV" + ToString((char) channel[ch]);

    sendCommand(msg);

    float volume = 0;
    if (ch == 1) {
        volume = Volume_A;
    } else if (ch == 2) {
        volume = Volume_B;
    } else {
        volume = Volume_V;
    };
    return volume;
}

String HCRVocalizer::getValue(String data, char separator, int index)
{
  int found = 0;
  int strIndex[] = {0, -1};
  int maxIndex = data.length()-1;

  for(int i=0; i<=maxIndex && found<=index; i++){
    if(data.charAt(i)==separator || i==maxIndex){
        found++;
        strIndex[0] = strIndex[1]+1;
        strIndex[1] = (i == maxIndex) ? i+1 : i;
    }
  }

  return found>index ? data.substring(strIndex[0], strIndex[1]) : "";
}
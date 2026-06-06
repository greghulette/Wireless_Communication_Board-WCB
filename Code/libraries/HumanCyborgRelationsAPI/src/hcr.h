#pragma once

#ifndef HCRAPI_F
#define HCRAPI_F
#define TwoWire_h
#ifdef ARDUINO
#if (ARDUINO >= 100)
#include <Arduino.h>
#else
#include <WProgram.h>
#include <pins_arduino.h>
#endif
#endif


#include <String.h>
#include <SoftwareSerial.h>
#include <Wire.h>

#define HCR_BUFFER_SIZE 32

#ifndef HCR_BAUD_RATE
#define HCR_BAUD_RATE 9600
#endif

#ifndef HCR_I2C_RATE
#define HCR_I2C_RATE 400000
#endif

#define CONN_SERIAL0 0x00 //"Serial"
#define CONN_SERIAL1 0x01 //"Serial1"
#define CONN_SERIAL2 0x02 //"Serial2"
#define CONN_SERIAL3 0x03 //"Serial3"
#define CONN_SERIAL4 0x04 //"Serial4"
#define CONN_I2C     0x05 //"I2C"
#define CONN_I2C1    0x06 //"I2C1"
#define CONN_I2C2    0x07 //"I2C2"

#define HAPPY 0
#define SAD 1
#define MAD 2
#define SCARED 3
#define OVERLOAD 4

#define EMOTE_MODERATE 0
#define EMOTE_STRONG 1
#define FADE 1

#define CH_V 0
#define CH_A 1
#define CH_B 2

/*!
    @brief  Class that stores state and functions for interacting with
            Human Cyborg Relations Vocalizer and compatible devices.
*/
class HCRVocalizer
{
public:
    /**
     * @brief Configure the HCRVocalizer for I2C
     * 
     * @param addr the I2C Address in hexidecimal
     * @param i2c the I2C interface (aka "Wire")
     */
    // if (esp32bool != true){
        // HCRVocalizer(const uint8_t addr, TwoWire &i2c);
    // }    

    /**
     * @brief Configure the HCRVocalizer for I2C with a baud rate
     * 
     * @param addr the I2C Address in hexidecimal
     * @param i2c the I2C interface (aka "Wire")
     * @param baud the baud rate
     */
    // HCRVocalizer(const uint8_t addr, TwoWire &i2c, int baud);
    
    /**
     * @brief Configure the HCRVocalizer for Hardware Serial with a baud rate
     * 
     * @param conn the serial connection
     * @param baud the baud rate
     */
    HCRVocalizer(HardwareSerial *conn,int baud);

    /**
     * @brief Configure the HCRVocalizer for Software Serial with a baud rate
     * 
     * @param conn the serial connection
     * @param baud the baud rate
     */
    HCRVocalizer(SoftwareSerial *conn,int baud);

    /**
     * @brief Configure the HCRVocalizer for Serial with specific rx and tx pins
     * 
     * @param rx the rx pin number
     * @param tx the tx pin number
     * @param baud the baud rate
     */
    HCRVocalizer(int rx, int tx, int baud);

    /**
    * @brief Initializes the vocalizer
    * 
    */
    void begin(void);

    /**
     * @brief Initializes the vocalizer with a refresh speed
     * 
     * @param refspeed
     */
    void begin(const uint16_t refspeed);
    
    /**
     * @brief Forces the vocalizer to update
     * 
     */
    void update(void);

    /**
     * @brief Triggers an audio response of the specific emotion and level
     * 
     * @param e the emotion (HAPPY|SAD|MAD|SCARED)
     * @param v the level (EMOTE_MODERATE|EMOTE_STRONG)
     */
    void Trigger(int e,int v);

    /**
     * @brief Stimulate an audio response of the specific emotion and level
     * 
     * @param e the emotion (HAPPY|SAD|MAD|SCARED)
     * @param v the level (EMOTE_MODERATE|EMOTE_STRONG)
     */
    void Stimulate(int e,int v);

    /**
    * @brief Triggers the Overload response
    * 
    */
    void Overload(void);

    /**
    * @brief Triggers a single muse
    *
    */
    void Muse(void);

    /**
    * @brief Configures muse minimum and maximum duration between
    * emotes
    * 
    * @param min in seconds
    * @param max in seconds
    */
    void Muse(int min, int max);

    /**
    * @brief Stops all wavs playing and any emotes
    * 
    */ 
    void Stop(void);

    /**
    * @brief Stops whatever emote is playing
    * 
    */
    void StopEmote(void);

    /**
     * @brief Overrides normalization of emotional state
     * 1 is enabled, 0 is disabled
     * 
     * @param v int
     */
    void OverrideEmotions(int v);

    /**
     * @brief Resets all emotions back to 0
     * 
     */
    void ResetEmotions(void);

    /**
     * @brief Sets an emotion to a specific value
     * 
     * @param e the emotion type (HAPPY|SAD|MAD|SCARED)
     * @param v the emotion value
     */
    void SetEmotion(int e,int v);

    /**
     * @brief Turn on or off random periodical mumbling audio animation
     * 1 is enabled, 0 is disabled
     * @param v 
     */
    void SetMuse(int v);

    /**
     * @brief Plays a WAV file by file number
     * 
     * @param v the channel (CH_A|CH_B)
     * @param file the file number
     */
    void PlayWAV(int v,int file);

    /**
     * @brief Plays a WAV file by file name
     * 
     * @param v the channel (CH_A|CH_B)
     * @param file the file name prefix (e.g. "0000")
     */
    void PlayWAV(int v,String file);

    /**
     * @brief Stops the WAV playing on the specified channel
     * 
     * @param ch the channel (CH_A|CH_B)
     */
    void StopWAV(int ch);

    /**
     * @brief Stops the WAV playing on the specified channel
     * 
     * @param ch the channel (CH_V|CH_A|CH_B)
     * @param v  the volume level (0-100)
     */
    void SetVolume(int e,int v);

    /**
     * @brief Retrieve the current state of each emotion
     * 
     * @return int* 
     */
    int* GetEmotions(void);

    /**
     * @brief Retrieve the current state of a specified emotion
     * 
     * @param e the emotion (HAPPY|SAD|MAD|SCARED)
     * @return int 
     */
    int GetEmotion(int e);

    /**
     * @brief Gets the length of the current emote duration
     * 
     * @return float 
     */
    float GetDuration(void);

    /**
     * @brief Retrieves the status of whether override is enabled
     * 
     * @return int 
     */
    int GetOverride(void);

    /**
     * @brief Returns value of whether audio is currently playing
     * 
     * @return true 
     * @return false 
     */
    bool IsPlaying(void);

    /**
     * @brief Returns value of whether audio is currently playing on
     * the specified channel
     * 
     * @param ch the audio channel (CH_V|CH_A|CH_B)
     * @return true 
     * @return false 
     */
    bool IsPlaying(int ch);

    /**
     * @brief Retrieves whether muse is currently enabled or disabled
     * 1 is enabled, 0 is disabled
     *
     * @return int 
     */
    int GetMuse(void);

    /**
     * @brief Returns an interger of the number of compatible WAV files
     * detected on the SD Card
     * 
     * @return int 
     */
    int GetWAVCount(void);

    /**
     * @brief Returns the file number of the WAV currently playing on
     * the specified channel or -1 if nothing is playing
     * 
     * @param ch the audio channel (CH_V|CH_A|CH_B)
     * @return int
     */
    int GetPlayingWAV(int ch);

    /**
     * @brief Retrieves the volume level of the specified channel
     * 
     * @param e the audio channel (CH_V|CH_A|CH_B)
     * @return float 
     */
    float getVolume(int e);
    void getUpdate(void);

    void dfPlayer();

private:
    // uint8_t _i2caddr=0;
    // TwoWire *_i2c;
    HardwareSerial *_serial;
    SoftwareSerial *_softserial;
    int _serialBaud;
    int connectionType;

    void transmit(String command);
    void transmit(String command, bool retry);
    void receive(void);
    void sendCommand(String command);
    void receiveData(char ch);
    void receiveData(String data);
    void processCommands(char* input);
    String getValue(String data, char separator, int index);

protected:
    char hcrstartMarker = '<';
    char hcrendMarker = '>';
    char cmdBuffer[HCR_BUFFER_SIZE];
    unsigned cmdPos;
    byte connection;
    int refreshSpeed;
    int emote_happy;
    int emote_sad;
    int emote_mad;
    int emote_scared;
    int state_override;
    int state_musing;
    int state_files;
    float state_duration;
    int state_chv;
    int state_cha;
    int state_chb;
    int Volume_V;
    int Volume_A;
    int Volume_B;
};

using namespace std;
//ToString
inline String ToString (double d)
{
	char buffer[32];
	snprintf(buffer, sizeof(buffer), "%g", d);
	return buffer;
}
inline String ToString (float d)
{
	char buffer[16];
	snprintf(buffer, sizeof(buffer), "%g", d);
	return buffer;
}
inline String ToString (char a)
{
	return String(a).c_str();
}
inline String ToString (const char* a)
{
	return String(a).c_str();
}
inline String ToString (int number)
{
	return String(number).c_str();
}
inline String ToString (int number, String thing)
{
	String Prefix = "";
	if (thing == "00")
	{
		if (number <= 9)
		{
			Prefix = "0";	
		}
	}
	return (Prefix + String(number)).c_str();
}
inline String ToString (bool d)
{
	if (d)
	{
		return "true";	
	}
	return "false";
}

#endif
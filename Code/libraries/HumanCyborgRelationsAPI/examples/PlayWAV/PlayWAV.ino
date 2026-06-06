/*
Parameter Shortcuts

      Emotions      Levels            Audio Channels
      HAPPY         EMOTE_MODERATE    CH_V
      SAD           EMOTE_STRONG      CH_A
      MAD                             CH_B
      SCARED

Examples
  
  // Play WAV File using the filename (only needs first 4 digits)
    HCR.PlayWAV(CH_A,"0000");
  
  // Play WAV File using the file ID
    HCR.PlayWAV(CH_B,2);

  // Stop Wav Channel
    HCR.StopWAV()

  // Get the number of WAV files on the SD Card
    HCR.GetWAVCount()

  // Get the ID of the playing WAV File (-1 if nothing playing)
    HCR.GetPlayingWAV()
*/

// Include the HCR Library
#include <hcr.h>

// Initialise the HCR Vocalizer API
HCRVocalizer HCR(&Serial,9600); // Serial (Stream Port, baud rate)

void setup() {
  // put your setup code here, to run once:
  Serial.begin(9600);

  // Begin HCR Setup
  HCR.begin(500); // Refresh Speed (Default:125 ms)

  // Enable Musing
  HCR.SetMuse(false);

  // Disable Emotion Override
  HCR.OverrideEmotions(false);
}

unsigned long Wait = millis();
int playing = 0;
void loop() {
  // put your main code here, to run repeatedly:

  // If 10 Seconds have passed
  if (millis() > Wait) {
    // Check if not already playing
    if (!HCR.IsPlaying(1))
    {
        // Play WAV File 0000-000X
        HCR.PlayWAV(1,(String)"000" + String(playing));
    };

    // Force Data update for playing state
    HCR.getUpdate();

    // If track is playing, increment number, ready for the next WAV.
    if ((int)HCR.GetPlayingWAV(1) == playing)
    {
      playing++;
      playing = playing % ((int)HCR.GetWAVCount()+1);
    };

    // Reset Timer    
    Wait = millis() + 1000;
  }

  HCR.update();
}
/*
Parameter Shortcuts

      Emotions      Levels            Audio Channels
      HAPPY         EMOTE_MODERATE    CH_V
      SAD           EMOTE_STRONG      CH_A
      MAD                             CH_B
      SCARED

      Triggers
      OVERLOAD

Examples

  // Stimulate Mild Happy Response
    HCR.Stimulate(HAPPY,EMOTE_MODERATE);

  // Stimulate Strong Sad Response
    HCR.Stimulate(SAD,EMOTE_STRONG);

  // Trigger Overload
    HCR.Overload();

  // Stimulate Random Response
    HCR.Stimulate(random(0,4),random(0,2));

  // Stop all sounds
    HCR.Stop();
  
  // Stop Vocaliser
    HCR.StopEmote();

  // Stop Wav Channel
    HCR.StopWAV()
*/

// Include the HCR Library
#include <hcr.h>

// Initialise the HCR Vocalizer API
HCRVocalizer HCR(&Serial,9600); // Serial (Stream Port, baud rate)
//HCRVocalizer HCR(16,17,9600); // Serial (RX Pin, TX Pin, baud rate)

void setup() {
  // put your setup code here, to run once:
  Serial.begin(9600);

  // Begin HCR Setup
  HCR.begin(); // Refresh Speed (Default:125 ms)

  // Disable Musing
  HCR.SetMuse(false);

  // Disable Emotion Override
  HCR.OverrideEmotions(false);
}

unsigned long Wait = millis();
void loop() {
  // put your main code here, to run repeatedly:

  // If 10 Seconds have passed
  if (millis() > Wait) {
    // Stimulate Mild Happy Response
    HCR.Stimulate(HAPPY,EMOTE_MODERATE);

    // Stimulate Strong Sad Response
    // HCR.Stimulate(SAD,EMOTE_STRONG);

    // Trigger Overload
    // HCR.Overload();

    // Stimulate Random Response
    // HCR.Stimulate(random(0,4),random(0,2));

    // Reset Timer    
    Wait = millis() + 10000;
  }

  HCR.update();
}
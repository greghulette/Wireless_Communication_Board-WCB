#include "DebugWCB.h"


debugClass::debugClass(bool displayMsg){

};

//////////////////////////////////////////////////////////////////////
///*****             Debug Functions                          *****///
//////////////////////////////////////////////////////////////////////


void debugClass::DBG(const char *format, ...) {
        if (!debugflag)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}

void debugClass::DBG_1(const char *format, ...) {
        if (!debugflag1)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}
void debugClass::DBG_2(const char *format, ...) {
        if (!debugflag2)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}

void debugClass::ESPNOW(const char *format, ...) {
        if (!debugflag_espnow)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}

void debugClass::SERVO(const char *format, ...) {
        if (!debugflag_servo)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}

void debugClass::SERIAL_EVENT(const char *format, ...) {
        if (!debugflag_serial_event)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}

void debugClass::LOOP(const char *format, ...) {
        if (!debugflag_loop)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}

void debugClass::HTTP(const char *format, ...) {
        if (!debugflag_loop)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}

void debugClass::LORA(const char *format, ...) {
        if (!debugflag_loop)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}

void debugClass::JSON(const char *format, ...) {
        if (!debugflag_json)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}

void debugClass::STATUS(const char *format, ...) {
        if (!debugflag_status)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}
void debugClass::PARAM(const char *format, ...) {
        if (!debugflag_param)
                return;
        va_list ap;
        va_start(ap, format);
        vfprintf(stderr, format, ap);
        va_end(ap);
}

void debugClass::toggle_Debug(){
  debugflag = !debugflag;
  if (debugflag == 1){
    Serial.println("Debugging Enabled");
    }
  else{
    Serial.println("Debugging Disabled");
  }
}

void debugClass::toggle_Debug1(){
  debugflag1 = !debugflag1;
  if (debugflag1 == 1){
    Serial.println("Debugging 1 Enabled");
    }
  else{
    Serial.println("Debugging 1 Disabled");
  }
}
void debugClass::toggle_Debug2(){
  debugflag2 = !debugflag2;
  if (debugflag2 == 1){
    Serial.println("Debugging 2 Enabled");
    }
  else{
    Serial.println("Debugging 2 Disabled");
  }
}

void debugClass::toggle_ESPNOW(){
  debugflag_espnow = !debugflag_espnow;
  if (debugflag_espnow == 1){
    Serial.println("ESP-NOW Debugging Enabled");
    }
  else{
    Serial.println("ESP-NOW Debugging Disabled");
  }
}

void debugClass::toggle_Servo(){
  debugflag_servo = !debugflag_servo;
  if (debugflag_servo == 1){
    Serial.println("Servo Debugging Enabled");
    }
  else{
    Serial.println("Servo Debugging Disabled");
  }
}

void debugClass::toggle_SerialEvent(){
  debugflag_serial_event = !debugflag_serial_event;
  if (debugflag_serial_event == 1){
    Serial.println("Serial Events Debugging Enabled");
    }
  else{
    Serial.println("Serial Events Debugging Disabled");
  }
}

void debugClass::toggle_Loop(){
  debugflag_loop = !debugflag_loop;
  if (debugflag_loop == 1){
    Serial.println("Main Loop Debugging Enabled");
    }
  else{
    Serial.println("Main Loop Debugging Disabled");
  }
}
void debugClass::toggle_HTTP(){
  debugflag_http = !debugflag_http;
  if (debugflag_http == 1){
    Serial.println("HTTP Debugging Enabled");
    }
  else{
    Serial.println("HTTP Debugging Disabled");
  }
}

void debugClass::toggle_LORA(){
  debugflag_lora = !debugflag_lora;
  if (debugflag_lora == 1){
    Serial.println("LoRa Debugging Enabled");
    }
  else{
    Serial.println("LoRa Debugging Disabled");
  }
}

void debugClass::toggle_JSON(){
  debugflag_json = !debugflag_json;
  if (debugflag_json == 1){
    Serial.println("JSON Debugging Enabled");
    }
  else{
    Serial.println("JSON Debugging Disabled");
  }
}

void debugClass::toggle_PARAM(){
  debugflag_param = !debugflag_param;
  if (debugflag_param == 1){
    Serial.println("Parameter Debugging Enabled");
    }
  else{
    Serial.println("Parameter Debugging Disabled");
  }
}

void debugClass::toggle_STATUS(){
  debugflag_status = !debugflag_status;
  if (debugflag_status == 1){
    Serial.println("Status Debugging Enabled");
    }
  else{
    Serial.println("Status Debugging Disabled");
  }
}


void debugClass::toggle(String debugType){
if (debugType == "DBG"){
  toggle_Debug();
} else if (debugType == "DBG1"){
  toggle_Debug1();
} else if (debugType == "DBG2"){
  toggle_Debug2();
} else if (debugType == "ESPNOW"){
  toggle_ESPNOW();
} else if (debugType == "SERVO"){
  toggle_Servo();
} else if (debugType == "SERIAL"){
  toggle_SerialEvent();
} else if (debugType == "LOOP"){
  toggle_Loop();
} else if (debugType == "HTTP"){
  toggle_HTTP();
} else if (debugType == "LORA"){
  toggle_LORA();
} else if (debugType == "JSON"){
  toggle_JSON();
} else if (debugType == "PARAM"){
  toggle_PARAM();
} else if (debugType == "STATUS"){
  toggle_STATUS();
} else if (debugType == "OFF"){
    debugflag = 0;  // Used for optional level of debuging
    debugflag1 = 0;  // Used for optional level of debuging
    debugflag2 = 0;  // Used for optional level of debuging
    debugflag_espnow = 0;
    debugflag_servo = 0;
    debugflag_serial_event = 0;
    debugflag_loop = 0;
    debugflag_http = 0;
    debugflag_lora = 0;
    debugflag_json = 0;
    debugflag_status = 0;
    debugflag_param = 0;
    Serial.println("All Debugging Turned Off");
}else if (debugType == "ON"){
    debugflag = 1;  // Used for optional level of debuging
    debugflag1 = 1;  // Used for optional level of debuging
    debugflag2 = 1;  // Used for optional level of debuging
    debugflag_espnow = 1;
    debugflag_servo = 1;
    debugflag_serial_event = 1;
    debugflag_loop = 1;
    debugflag_http = 1;
    debugflag_lora = 1;
    debugflag_json = 1;
    debugflag_status = 1;
    debugflag_param = 1;
    Serial.println("All Debugging Turned On");
} else {Serial.println("No valid debug given");}

};
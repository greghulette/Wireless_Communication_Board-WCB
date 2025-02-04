#ifndef DebugWCB_h
#define DebugWCB_h


#if (ARDUINO >= 100)
 #include <Arduino.h>
#else
 #include <WProgram.h>
#endif


class debugClass {
  public:
  // Flags to enable/disable debugging in runtime
    bool debugflag = 0;  // Used for optional level of debuging
    bool debugflag1 = 0;  // Used for optional level of debuging
    bool debugflag2 = 0;  // Used for optional level of debuging
    bool debugflag_espnow = 0;
    bool debugflag_servo = 0;
    bool debugflag_serial_event = 0;
    bool debugflag_loop = 0;
    bool debugflag_http = 0;
    bool debugflag_lora = 0;
    bool debugflag_json = 0;
    bool debugflag_status = 0;
    bool debugflag_param = 0;
    bool debugflag_maestro = 0;


  //constructor
    debugClass(bool displayMsg = false);

  //methods
    void DBG(const char *format, ...);
    void DBG_1(const char *format, ...); 
    void DBG_2(const char *format, ...);
    void ESPNOW(const char *format, ...);
    void SERVO(const char *format, ...);
    void SERIAL_EVENT(const char *format, ...);
    void LOOP(const char *format, ...);
    void HTTP(const char *format, ...);
    void LORA(const char *format, ...);
    void JSON(const char *format, ...);
    void STATUS(const char *format, ...);
    void PARAM(const char *format, ...);
    void MAESTRO_DEBUG(const char *format, ...);
    void toggle(String debugType);
    void toggle_Debug();
    void toggle_Debug1();
    void toggle_Debug2();
    void toggle_ESPNOW();
    void toggle_Servo();
    void toggle_SerialEvent();
    void toggle_Loop();
    void toggle_HTTP();
    void toggle_LORA();
    void toggle_JSON();
    void toggle_STATUS();
    void toggle_PARAM();
    void toggle_MAESTRO();




  private:


};

#endif
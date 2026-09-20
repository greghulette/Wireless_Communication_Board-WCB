// WCB bench probe — instrument firmware for a spare WCB V2.4 board, driven by tests/hil.
//
// The implementation and the USB protocol reference live in probe_main.cpp. It is a .cpp, not
// part of this .ino, on purpose: the Arduino preprocessor inserts auto-generated function
// prototypes above the struct definitions in an .ino, so any function taking a struct by
// reference fails to compile ("'Channel' does not name a type").

#ifndef PERSISTENT_STRING_HANDLER_H
#define PERSISTENT_STRING_HANDLER_H

#include <Arduino.h>
#include <Preferences.h>
#include <nvs.h>
#include <nvs_flash.h>

class PersistentStringHandler {
public:
    PersistentStringHandler(const char* namespaceName);
    ~PersistentStringHandler();

    void begin();
    String getString(const char* key);
    void saveString(const char* key, const String& value);
    void listKeys(); // List all keys stored in the namespace
    void clearAll(); // Optional: Clear all keys in the namespace

private:
    Preferences preferences;
    const char* namespaceName;
};

#endif // PERSISTENT_STRING_HANDLER_H

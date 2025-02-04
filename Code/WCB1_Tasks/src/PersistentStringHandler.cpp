#include "PersistentStringHandler.h"

PersistentStringHandler::PersistentStringHandler(const char* namespaceName) 
    : namespaceName(namespaceName) {}

PersistentStringHandler::~PersistentStringHandler() {
    preferences.end(); // Ensure NVS is closed
}

void PersistentStringHandler::begin() {
    preferences.begin(namespaceName, false); // Open in read/write mode
}

String PersistentStringHandler::getString(const char* key) {
    return preferences.getString(key, ""); // Return the stored string or a default value
}

void PersistentStringHandler::saveString(const char* key, const String& value) {
    preferences.putString(key, value); // Save the string in NVS
}

// void PersistentStringHandler::listKeys() {
//     nvs_handle_t handle;
//     esp_err_t err = nvs_open(namespaceName, NVS_READONLY, &handle);

//     if (err == ESP_ERR_NVS_NOT_FOUND) {
//         Serial.println("Namespace not found or no keys exist yet.");
//         return;
//     } else if (err != ESP_OK) {
//         Serial.printf("Failed to open NVS namespace: %s\n", esp_err_to_name(err));
//         return;
//     }

//     Serial.printf("\n\n\nListing all stored commands:\n");
//     nvs_iterator_t it = nvs_entry_find(NVS_DEFAULT_PART_NAME, namespaceName, NVS_TYPE_STR);

//     if (it == NULL) {
//         Serial.println("No commands are stored at the moment.");
//     } else {
//         while (it != NULL) {
//             nvs_entry_info_t info;
//             nvs_entry_info(it, &info);

//             // Retrieve the value associated with the key
//             size_t requiredSize;
//             if (nvs_get_str(handle, info.key, NULL, &requiredSize) == ESP_OK) {
//                 char* value = new char[requiredSize]; // Allocate memory for the value
//                 if (nvs_get_str(handle, info.key, value, &requiredSize) == ESP_OK) {
//                     Serial.printf("Command Number: %s, Command: %s\n", info.key, value);
//                 }
//                 delete[] value; // Free the allocated memory
//             } else {
//                 Serial.printf("Key: %s, Value: [Unable to retrieve]\n", info.key);
//             }

//             it = nvs_entry_next(it); // Move to the next key
//         }
//     }

//     nvs_release_iterator(it);
//     nvs_close(handle);
// }
// #include <nvs_flash.h>

#include <nvs_flash.h>
#include <nvs.h>

void PersistentStringHandler::listKeys() {
    nvs_iterator_t it = nullptr; // Iterator for entries
    esp_err_t err;

    // Initialize the iterator
    err = nvs_entry_find(NVS_DEFAULT_PART_NAME, namespaceName, NVS_TYPE_STR, &it);
    if (err != ESP_OK) {
        Serial.println("Failed to initialize NVS iterator!");
        return;
    }

    // Iterate through all entries
    while (it != nullptr) {
        nvs_entry_info_t info;
        nvs_entry_info(it, &info); // Retrieve entry info

        Serial.print("Found key: ");
        Serial.println(info.key);

        // Move to the next entry
        err = nvs_entry_next(&it);
        if (err != ESP_OK) {
            break;
        }
    }

    // Release the iterator if valid
    if (it != nullptr) {
        nvs_release_iterator(it);
    }
}


void PersistentStringHandler::clearAll() {
    preferences.clear(); // Clear all keys in the current namespace
}

/*
  Arduino Uno sketch for SmartHouse cardboard demo — Rooms 1-4 LEDs
  -------------------------------------------------------------------
  Listens on Serial for lines in the format:
    device_id,action\n
  Examples:
    light_room_1,on\n
    light_room_3,off\n

  Sent by the ESP8266 bridge in smart/smart.ino, which polls the Flask
  backend and forwards state changes for light_room_1..light_room_4 here.
  This sketch just maps those 4 ids to digital output pins and toggles
  them accordingly.

  Wiring notes:
  - This uses the Uno's hardware Serial (pins 0/1) to talk to the ESP8266,
    since that's what smart.ino's SoftwareSerial link expects on the ESP
    side. That means:
      * Disconnect the ESP link before uploading a new sketch to the Uno
        over USB (both use pins 0/1).
      * Any Serial.print() from this sketch also goes out over that same
        link — the ESP will see it, but it only parses "id,action" lines
        and prints/ignores everything else, so it's harmless for debug.
  - Connect ESP D5 -> Uno RX (pin 0), ESP D6 <- Uno TX (pin 1), and a
    voltage divider on the Uno TX -> ESP RX leg (Uno is 5V, ESP RX must
    stay under 3.3V). Common GND between ESP and Uno is required.
  - Use a current-limiting resistor (~220-330 ohm) in series with each LED.
*/

// device -> pin mapping
// LEDs for Room 1 - Room 4, fed by the ESP8266 bridge (smart/smart.ino).
// Use a current-limiting resistor (~220-330 ohm) in series with each LED.
#include <Arduino.h>

struct DevMap { const char* id; uint8_t pin; bool isMotor; };

DevMap mapList[] = {
  {"light_room_1", 2, false},
  {"light_room_2", 3, false},
  {"light_room_3", 4, false},
  {"light_room_4", 5, false},
};
const int MAP_COUNT = sizeof(mapList)/sizeof(mapList[0]);

String buf = "";

void setup() {
  Serial.begin(9600); // must match UNO_BAUD in smart/smart.ino — mismatched
                       // baud rates on this link mean the Uno receives
                       // garbage and never recognizes any device id
  // init pins
  for (int i = 0; i < MAP_COUNT; ++i) {
    pinMode(mapList[i].pin, OUTPUT);
    digitalWrite(mapList[i].pin, LOW);
  }
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') {
      String line = buf;
      buf = "";
      line.trim();
      if (line.length() > 0) handleLine(line);
    } else if (c != '\r') {
      buf += c;
    }
  }
}

void handleLine(String line) {
  // expected: device,action
  int idx = line.indexOf(',');
  if (idx < 0) return;
  String did = line.substring(0, idx);
  String action = line.substring(idx+1);
  did.trim(); action.trim();
  // find mapping
  for (int i = 0; i < MAP_COUNT; ++i) {
    if (did.equalsIgnoreCase(mapList[i].id)) {
      if (action.equalsIgnoreCase("on") || action.equalsIgnoreCase("open")) {
        if (mapList[i].isMotor) {
          // simple motor pulse for demo
          digitalWrite(mapList[i].pin, HIGH);
          delay(200);
          digitalWrite(mapList[i].pin, LOW);
        } else {
          digitalWrite(mapList[i].pin, HIGH);
        }
        Serial.print("OK:" + String(mapList[i].id) + "=ON\n");
      } else {
        digitalWrite(mapList[i].pin, LOW);
        Serial.print("OK:" + String(mapList[i].id) + "=OFF\n");
      }
      return;
    }
  }
  Serial.print("ERR:unknown_device:" + line + "\n");
}

/*
  Arduino Uno sketch for SmartHouse cardboard demo
  -----------------------------------------------
  Listens on Serial for lines in the format:
    device_id,action\n
  Examples:
    light_living_top_left,on\n
  The sketch maps a small set of device IDs to digital output pins
  and toggles them accordingly. For motors, it's expected the hardware
  is driven via a transistor/H-bridge — do NOT power motors directly
  from Arduino pins.

  Wiring notes:
  - Connect ESP TX -> Arduino RX, ESP RX -> Arduino TX (crossed) if you
    want the ESP to forward commands to the Arduino over Serial.
  - Use a common GND between ESP and Arduino.
  - Use transistors or motor drivers for motors; use current-limited
    resistors for LEDs where appropriate.
*/

// device -> pin mapping (example)
// adjust pins according to your cardboard wiring
#include <Arduino.h>

struct DevMap { const char* id; uint8_t pin; bool isMotor; };

DevMap mapList[] = {
  {"light_living_top_left", 2, false},
  {"light_living_top_right",3, false},
  {"light_living_center",4, false},
  {"socket_living_1",5, false},
  {"motor_demo",6, true},
};
const int MAP_COUNT = sizeof(mapList)/sizeof(mapList[0]);

String buf = "";

void setup() {
  Serial.begin(115200);
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

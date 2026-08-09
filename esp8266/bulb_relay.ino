/*
  Smart House - ESP8266 Bulb Relay
  --------------------------------
  Listens for the SAME command schema the simulator uses, over WiFi.
  The ESP is intentionally "dumb" — it does zero parsing of speech or
  intent, it just receives {"device": "...", "action": "on"/"off"}
  and flips a relay. All the thinking happens on the phone.

  Wiring:
    Relay IN  -> D1 (GPIO5)
    Relay VCC -> 3V3 (or 5V depending on your relay module)
    Relay GND -> GND

  Libraries needed (Arduino IDE > Library Manager):
    - ESP8266WiFi (bundled with ESP8266 board package)
    - ESP8266WebServer (bundled)
    - ArduinoJson (by Benoit Blanchon)
*/

#include <ESP8266WiFi.h>
#include <ESP8266WebServer.h>
#include <ArduinoJson.h>

// ---- EDIT THESE ----
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* DEVICE_ID = "bulb_living_room";   // must match the simulator's device id
const char* SIMULATOR_HOST = "192.168.1.50";  // IP of the machine running app.py (optional, for reporting state back)
// ---------------------

const int RELAY_PIN = D1;
ESP8266WebServer server(80);
String currentState = "off";

void handleCommand() {
  if (server.method() != HTTP_POST) {
    server.send(405, "application/json", "{\"ok\":false,\"error\":\"use POST\"}");
    return;
  }

  StaticJsonDocument<200> doc;
  DeserializationError err = deserializeJson(doc, server.arg("plain"));
  if (err) {
    server.send(400, "application/json", "{\"ok\":false,\"error\":\"bad json\"}");
    return;
  }

  const char* device = doc["device"];
  const char* action = doc["action"];

  if (device == nullptr || action == nullptr || String(device) != DEVICE_ID) {
    server.send(404, "application/json", "{\"ok\":false,\"error\":\"unknown device\"}");
    return;
  }

  if (String(action) == "on") {
    digitalWrite(RELAY_PIN, HIGH);
    currentState = "on";
  } else if (String(action) == "off") {
    digitalWrite(RELAY_PIN, LOW);
    currentState = "off";
  } else {
    server.send(400, "application/json", "{\"ok\":false,\"error\":\"invalid action\"}");
    return;
  }

  Serial.printf("Command received: %s -> %s\n", device, action);
  server.send(200, "application/json", "{\"ok\":true}");
}

void handleStatus() {
  String json = "{\"device\":\"" + String(DEVICE_ID) + "\",\"state\":\"" + currentState + "\"}";
  server.send(200, "application/json", json);
}

void setup() {
  Serial.begin(115200);
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, LOW);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("Connected. ESP8266 IP address: ");
  Serial.println(WiFi.localIP());
  Serial.println("Point the app or simulator's device entry at this IP for real hardware control.");

  server.on("/command", handleCommand);
  server.on("/status", HTTP_GET, handleStatus);
  server.begin();
}

void loop() {
  server.handleClient();
}

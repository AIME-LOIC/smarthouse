/*
  FRIDAY Smart Home - ESP8266 -> Arduino Uno Bridge (Rooms 1-4)
  ---------------------------------------------------------------
  The ESP8266 does NOT drive any LEDs itself anymore. It polls the
  Flask backend's /state endpoint for light_room_1 .. light_room_4,
  and whenever one of them changes, forwards a line over serial to
  an Arduino Uno running arduino/arduino_house.ino, which is the one
  actually switching the LEDs:

      light_room_1,on\n
      light_room_3,off\n

  This is the same "device_id,action\n" protocol arduino_house.ino
  already parses, so no changes are needed on the Uno's receive loop
  beyond pointing its pin map at light_room_1..4 (see the accompanying
  arduino/arduino_house.ino diff).

  WIRING (ESP8266 <-> Arduino Uno):
    ESP D5 (GPIO14, SoftwareSerial TX) -> Uno RX (pin 0 / Serial RX)   [ESP is
                                           3.3V logic, Uno RX accepts that fine]
    ESP D6 (GPIO12, SoftwareSerial RX) <- Uno TX (pin 1 / Serial TX)   [put a
                                           voltage divider here: Uno TX is
                                           5V, ESP RX must not exceed 3.3V]
    ESP GND                            -- Uno GND   (common ground, required)

  NOTE: the Uno's hardware Serial (pins 0/1) is used here for the ESP link,
  which means you must disconnect it while uploading sketches to the Uno
  via USB, and Serial.print() debug lines on the Uno will also go out over
  this same link. If you want the Uno's USB serial free for debugging,
  use SoftwareSerial on the Uno side instead and rewire accordingly.
*/

#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>
#include <ArduinoJson.h>
#include <SoftwareSerial.h>

// ---------- CONFIG ----------
const char* WIFI_SSID     = "loicaime";
const char* WIFI_PASSWORD = "aimeloic132";

const char* SERVER_HOST    = "10.79.65.112";   // LAN IP of the machine running app.py
const uint16_t SERVER_PORT = 8000;

const char* AUTH_USERNAME = "aime";            // seeded default user
const char* AUTH_PASSWORD = "aime";            // matches ESP_DEFAULT_PASSWORD unless you changed it

// Devices this node is responsible for — must match keys in DEVICES{} in app.py
const char* ROOM_DEVICE_IDS[] = { "light_room_1", "light_room_2", "light_room_3", "light_room_4" };
const int ROOM_COUNT = 4;

// SoftwareSerial link to the Arduino Uno
const uint8_t UNO_TX_PIN = D5;   // ESP -> Uno RX
const uint8_t UNO_RX_PIN = D6;   // ESP <- Uno TX
const unsigned long UNO_BAUD = 9600;

const unsigned long POLL_INTERVAL_MS = 1000;
// ---------- END CONFIG ----------

SoftwareSerial unoSerial(UNO_RX_PIN, UNO_TX_PIN); // RX, TX
WiFiClient client;
String sessionCookie = "";
bool loggedIn = false;
bool roomState[ROOM_COUNT] = { false, false, false, false };
unsigned long lastPoll = 0;

bool login() {
  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT + "/auth/login";
  http.begin(client, url);
  const char* headerKeys[] = {"Set-Cookie"};
  http.collectHeaders(headerKeys, 1);
  http.addHeader("Content-Type", "application/json");

  StaticJsonDocument<128> doc;
  doc["username"] = AUTH_USERNAME;
  doc["password"] = AUTH_PASSWORD;
  String body;
  serializeJson(doc, body);

  int code = http.POST(body);
  bool ok = false;

  if (code == 200) {
    String setCookie = http.header("Set-Cookie");
    if (setCookie.length() > 0) {
      int sep = setCookie.indexOf(';');
      sessionCookie = sep == -1 ? setCookie : setCookie.substring(0, sep);
      ok = true;
      Serial.println("Login OK, cookie: " + sessionCookie);
    } else {
      Serial.println("Login returned 200 but no Set-Cookie header - check Flask SECRET_KEY / cookie settings");
    }
  } else {
    Serial.printf("Login failed, HTTP %d\n", code);
  }
  http.end();
  return ok;
}

// Fetches /state once and fills outStates[0..ROOM_COUNT-1] with each room's on/off.
// Returns false only on a transport/auth/parse failure (not on a device missing).
bool fetchRoomStates(bool outStates[ROOM_COUNT]) {
  if (sessionCookie.length() == 0) return false;
  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT + "/state";
  http.begin(client, url);
  http.addHeader("Cookie", sessionCookie);

  int code = http.GET();
  if (code != 200) {
    Serial.printf("GET /state failed, HTTP %d\n", code);
    if (code == 401) loggedIn = false;  // session expired server-side, force re-login
    http.end();
    return false;
  }

  String payload = http.getString();
  http.end();

  DynamicJsonDocument doc(8192);  // /state returns the full device list, needs headroom
  DeserializationError err = deserializeJson(doc, payload);
  if (err) {
    Serial.println("JSON parse failed: " + String(err.c_str()));
    return false;
  }

  JsonObject devices = doc["devices"];
  for (int i = 0; i < ROOM_COUNT; ++i) {
    const char* state = devices[ROOM_DEVICE_IDS[i]]["state"];
    if (!state) {
      Serial.println(String("Warning: ") + ROOM_DEVICE_IDS[i] + " not found in /state response");
      continue; // leave outStates[i] as whatever it already was
    }
    outStates[i] = (strcmp(state, "on") == 0);
  }
  return true;
}

// Forwards one "device_id,on\n" / "device_id,off\n" line to the Uno.
void sendToUno(const char* deviceId, bool isOn) {
  unoSerial.print(deviceId);
  unoSerial.print(",");
  unoSerial.println(isOn ? "on" : "off");
  Serial.println(String("-> Uno: ") + deviceId + "," + (isOn ? "on" : "off"));
}

void setup() {
  Serial.begin(115200);
  unoSerial.begin(UNO_BAUD);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected: " + WiFi.localIP().toString());

  loggedIn = login();

  // Push the Uno an initial known-off state for all 4 rooms so it doesn't
  // start out of sync with whatever it happened to power on with.
  for (int i = 0; i < ROOM_COUNT; ++i) {
    sendToUno(ROOM_DEVICE_IDS[i], roomState[i]);
  }
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    delay(1000);
    return;
  }

  if (!loggedIn) {
    loggedIn = login();
    if (!loggedIn) {
      delay(3000);
      return;
    }
  }

  unsigned long now = millis();
  if (now - lastPoll >= POLL_INTERVAL_MS) {
    lastPoll = now;

    bool newStates[ROOM_COUNT];
    memcpy(newStates, roomState, sizeof(roomState));

    if (fetchRoomStates(newStates)) {
      for (int i = 0; i < ROOM_COUNT; ++i) {
        if (newStates[i] != roomState[i]) {
          roomState[i] = newStates[i];
          sendToUno(ROOM_DEVICE_IDS[i], roomState[i]);
        }
      }
    }
  }
}

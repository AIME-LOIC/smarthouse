/*
  FRIDAY Smart Home - Single Bulb Hardware Node (ESP8266 / Wemos D1 mini)
  Polls the existing Flask backend's /state endpoint and drives one
  relay/LED to match the device's reported state. No server changes needed.
*/

#include <ESP8266WiFi.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClient.h>
#include <ArduinoJson.h>

// ---------- CONFIG ----------
const char* WIFI_SSID     =  "Fofo❤️";
const char* WIFI_PASSWORD =  "fofo12345";

const char* SERVER_HOST    = "172.20.10.14";   // LAN IP of the machine running app.py
const uint16_t SERVER_PORT = 8000;

const char* AUTH_USERNAME = "aime";           // seeded default user
const char* AUTH_PASSWORD = "aime";           // matches ESP_DEFAULT_PASSWORD unless you changed it

const char* DEVICE_ID = "light_room_1";       // must match a key in DEVICES{} in app.py

const uint8_t LED_PIN = LED_BUILTIN;                   // GPIO5 - confirm this is really where your relay/LED is wired
const uint8_t LED_ON  = LOW;
const uint8_t LED_OFF = HIGH;
const unsigned long POLL_INTERVAL_MS = 1000;
// ---------- END CONFIG ----------

WiFiClient client;
String sessionCookie = "";
bool loggedIn = false;
bool currentState = false;
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

bool fetchDeviceState(bool &stateOut) {
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

  const char* state = doc["devices"][DEVICE_ID]["state"];
  if (!state) {
    Serial.println("DEVICE_ID not found in response - check spelling against app.py's DEVICES{} keys");
    return false;
  }

  stateOut = (strcmp(state, "on") == 0);
  return true;
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, LOW);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(400);
    Serial.print(".");
  }
  Serial.println("\nWiFi connected: " + WiFi.localIP().toString());

  loggedIn = login();
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
    bool newState;
    if (fetchDeviceState(newState)) {
      if (newState != currentState) {
        currentState = newState;
        digitalWrite(LED_PIN, currentState ? LED_ON : LED_OFF);
        Serial.println(String(DEVICE_ID) + " -> " + (currentState ? "ON" : "OFF"));
      }
    }
  }
}
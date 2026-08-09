# Smart House Simulator

Shared backend + blueprint dashboard for the smart house project. The app
team and hardware team build against the same command schema — swap the
simulator for the real ESP8266 by changing a URL, nothing else.

## Run it

```bash
pip install -r requirements.txt --break-system-packages
export OPENROUTER_API_KEY=sk-or-v1-...   # only needed for /interpret (online mode)
python app.py
```

- `/` — live floor-plan dashboard (read-only, polls every second)
- `/web` — same floor plan, but every light/door/valve is clickable, plus a
  transcript box to test `/interpret` without the Flutter app

## Command schema

```json
POST /command
{ "device": "light_room_1", "action": "on" }
```

`GET /devices` lists all 19 devices and their valid actions.

**Group commands** — two special device keywords trigger multiple lights
at once (they're not in `/devices` since they're not individual devices):
- `"living_room_corners"` → the 4 corner lights
- `"living_room_all_lights"` → all 5 living room lights including center

**Delayed commands** — add `"delay_seconds": N` to any command (single or
group) to schedule it instead of executing immediately:
```json
{ "device": "light_gate", "action": "on", "delay_seconds": 30 }
```

## Online mode (`/interpret`)

Send a raw transcript instead of a structured command:
```json
POST /interpret
{ "transcript": "turn on all living room lights in 2 minutes" }
```
The server (via OpenRouter) can return **multiple** commands from one
transcript, and can infer delays from phrases like "in 2 minutes". Response:
```json
{ "ok": true, "executed_commands": [ { "device": "living_room_all_lights", "action": "on", "scheduled": true, "delay_seconds": 120, "reasoning": "..." } ] }
```

If `OPENROUTER_API_KEY` isn't set, this returns a clean `503` — `/command`
(offline mode) is unaffected either way.

Get a free-tier key at **https://openrouter.ai/keys**. Set it as an
environment variable, never in code or committed to git.

## For the hardware team

`esp8266/bulb_relay.ino` is a starting sketch that listens for the same
JSON schema over WiFi. Currently `gate_main` is flagged `is_hardware: True`
in `app.py` — update that flag (and the ESP sketch's `DEVICE_ID`) to
whichever device is actually wired to your relay.

## Known gaps (scope these next, don't let them surprise your team)

- No auth on `/command` or `/interpret` — fine on closed camp WiFi, not
  fine for anything real.
- `debug=True` enables Flask's auto-reloader; if you edit `app.py` while a
  `delay_seconds` timer is pending, the reload can kill it before it fires.
  Turn `debug=False` for the actual demo run.
- Flutter app itself (voice → STT → intent parsing → `/command` or
  `/interpret`) isn't built yet.

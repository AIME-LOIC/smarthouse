"""
Smart House Simulator - Backend
--------------------------------
Single source of truth for device state. The phone app and the real
ESP8266 both talk to this same API using the same command schema, so
swapping simulator <-> hardware is just changing a URL.

Electricity model:
  - Balance is stored in kWh (matching how real EUCL prepaid meters work —
    they deduct energy directly, not money).
  - Topping up converts RWF -> kWh using Rwanda's real tiered residential
    tariff (effective 1 Oct 2025): 0-20kWh @ 89 RWF/kWh, 20-50kWh @ 310,
    above 50kWh @ 369. Which tier applies depends on how much you've
    already CONSUMED this "month" (monthly_kwh_consumed), not how much
    you've bought — same as a real meter.
  - A background thread drains the balance every second based on live
    device wattage, and auto-cuts-off all electric devices at zero
    balance. All shared state is protected by one RLock.
	
Run:
    pip install -r requirements.txt --break-system-packages
    export OPENROUTER_API_KEY=sk-or-v1-...   # required for /interpret
    python app.py
Then open http://localhost:8000
"""

import os
import re
import json
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from flask import Flask, jsonify, request, render_template, session, Response
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timezone

# load .env if present
_env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "smarthouse_auth.db")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.0-flash-001")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

ESP_DEFAULT_PASSWORD = os.environ.setdefault("ESP_DEFAULT_PASSWORD", "aime")

ELEVENLABS_API_KEY = os.environ.get("11LABS") or os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v1")
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
# Default voice: ElevenLabs' public premade "Rachel" voice. Override with
# ELEVENLABS_VOICE_ID in .env to use a different voice from your account.
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVENLABS_TTS_MODEL = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_turbo_v2_5")

# One lock guards every mutation of DEVICES / ENERGY / DEVICE_WH_CONSUMED.
# RLock so a function that already holds the lock can safely call another
# locking function (e.g. the drain loop calling apply_command).
_LOCK = threading.RLock()

DEVICES = {
    "light_living_top_left": {"label": "Living Room Light (Top Left)", "room": "Living Room", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_living_top_right": {"label": "Living Room Light (Top Right)", "room": "Living Room", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_living_bottom_left": {"label": "Living Room Light (Bottom Left)", "room": "Living Room", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_living_bottom_right": {"label": "Living Room Light (Bottom Right)", "room": "Living Room", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_living_center": {"label": "Living Room Light (Center)", "room": "Living Room", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_room_1": {"label": "Room 1 Light", "room": "Room 1", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_room_2": {"label": "Room 2 Light", "room": "Room 2", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_room_3": {"label": "Room 3 Light", "room": "Room 3", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_room_4": {"label": "Room 4 Light", "room": "Room 4", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_kitchen": {"label": "Kitchen Light", "room": "Kitchen", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_hallway_1": {"label": "Hallway Light 1", "room": "Hallway", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_hallway_2": {"label": "Hallway Light 2", "room": "Hallway", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_hallway_3": {"label": "Hallway Light 3", "room": "Hallway", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "light_gate": {"label": "Gate Light", "room": "Outdoors", "type": "light", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "fridge": {"label": "Fridge", "room": "Kitchen", "type": "appliance", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "tv": {"label": "TV", "room": "Living Room", "type": "appliance", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "microwave": {"label": "Microwave", "room": "Kitchen", "type": "appliance", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "water_heater": {"label": "Water Heater", "room": "Bathroom", "type": "appliance", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "socket_living_1": {"label": "Living Room Socket 1", "room": "Living Room", "type": "socket", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "socket_living_2": {"label": "Living Room Socket 2", "room": "Living Room", "type": "socket", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "socket_living_3": {"label": "Living Room Socket 3", "room": "Living Room", "type": "socket", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "socket_living_4": {"label": "Living Room Socket 4", "room": "Living Room", "type": "socket", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "socket_room_1": {"label": "Room 1 Socket", "room": "Room 1", "type": "socket", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "socket_room_2": {"label": "Room 2 Socket", "room": "Room 2", "type": "socket", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "socket_room_3": {"label": "Room 3 Socket", "room": "Room 3", "type": "socket", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "socket_room_4": {"label": "Room 4 Socket", "room": "Room 4", "type": "socket", "actions": ["on", "off"], "state": "off", "is_hardware": False, "updated_at": None, "source": None},
    "valve_kitchen": {"label": "Kitchen Valve", "room": "Kitchen", "type": "valve", "actions": ["open", "close"], "state": "close", "is_hardware": False, "updated_at": None, "source": None},
    "gate_main": {"label": "Main Entrance Gate", "room": "Outdoors", "type": "gate", "actions": ["open", "close"], "state": "close", "is_hardware": True, "updated_at": None, "source": None},
    "door_room_1": {"label": "Room 1 Door", "room": "Room 1", "type": "door", "actions": ["open", "close"], "state": "close", "is_hardware": False, "updated_at": None, "source": None},
    "door_room_2": {"label": "Room 2 Door", "room": "Room 2", "type": "door", "actions": ["open", "close"], "state": "close", "is_hardware": False, "updated_at": None, "source": None},
    "door_room_3": {"label": "Room 3 Door", "room": "Room 3", "type": "door", "actions": ["open", "close"], "state": "close", "is_hardware": False, "updated_at": None, "source": None},
    "door_room_4": {"label": "Room 4 Door", "room": "Room 4", "type": "door", "actions": ["open", "close"], "state": "close", "is_hardware": False, "updated_at": None, "source": None},
    "door_kitchen": {"label": "Kitchen Door", "room": "Kitchen", "type": "door", "actions": ["open", "close"], "state": "close", "is_hardware": False, "updated_at": None, "source": None},
    "door_living_room": {"label": "Living Room Main Door", "room": "Living Room", "type": "door", "actions": ["open", "close"], "state": "close", "is_hardware": False, "updated_at": None, "source": None},
    "door_living_hallway": {"label": "Living Room ↔ Hallway Door", "room": "Living Room", "type": "door", "actions": ["open", "close"], "state": "close", "is_hardware": False, "updated_at": None, "source": None},
}

# Watt ratings — only devices that actually draw continuous power are listed.
# Doors/gate/valve are mechanical (momentary draw at most), so they're
# intentionally absent here — they cost 0 in the meter.
DEVICE_WATTS = {
    "light_living_top_left": 10, "light_living_top_right": 10,
    "light_living_bottom_left": 10, "light_living_bottom_right": 10,
    "light_living_center": 15, "light_room_1": 10, "light_room_2": 10,
    "light_room_3": 10, "light_room_4": 10, "light_kitchen": 12,
    "light_gate": 20, "light_hallway_1": 10, "light_hallway_2": 10, "light_hallway_3": 10,
    "fridge": 150, "tv": 100, "microwave": 1200,
    "water_heater": 2000,
    "socket_living_1": 150, "socket_living_2": 150, "socket_living_3": 150, "socket_living_4": 150,
    "socket_room_1": 150, "socket_room_2": 150, "socket_room_3": 150, "socket_room_4": 150,
}

# Real Rwanda residential tariff, effective 1 Oct 2025 (RURA).
# (from_kwh, to_kwh, rate_rwf_per_kwh) — bracket determined by cumulative
# kWh consumed THIS MONTH, same logic a real prepaid meter uses.
TARIFF_TIERS = [(0, 20, 89.0), (20, 50, 310.0), (50, float("inf"), 369.0)]

ENERGY = {
    "balance_kwh": 0.0,          # prepaid energy remaining
    "monthly_kwh_consumed": 0.0, # resets via /energy/reset_month, not real calendar time
    "total_topup_rwf": 0.0,
    "total_consumed_kwh": 0.0,   # lifetime, never resets — for the "money wasted" story
}

# Per-device lifetime Wh consumed — this is what actually answers
# "which device is wasting my electricity"
DEVICE_WH_CONSUMED = {did: 0.0 for did in DEVICE_WATTS}

EVENT_LOG = []


def init_auth_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE,
            password_hash TEXT NOT NULL,
            number_of_room INTEGER,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()

    # Seed a default web login user if missing.
    default_user = conn.execute("SELECT 1 FROM users WHERE username = ?", ("aime",)).fetchone()
    if not default_user:
        conn.execute(
            "INSERT INTO users (username, email, password_hash, number_of_room, created_at) VALUES (?, ?, ?, ?, ?)",
            ("aime", "aime@example.com", generate_password_hash("aime"), 1, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    conn.close()


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id, username, email, number_of_room, created_at FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def log_event(device_id, action, source):
    EVENT_LOG.insert(0, {"device": device_id, "action": action, "source": source, "time": datetime.now(timezone.utc).strftime("%H:%M:%S")})
    del EVENT_LOG[25:]


def apply_command(device_id, action, source):
    with _LOCK:
        DEVICES[device_id]["state"] = action
        DEVICES[device_id]["updated_at"] = datetime.now(timezone.utc).isoformat()
        DEVICES[device_id]["source"] = source
        log_event(device_id, action, source)


def apply_group_command(action, group_type="corners", source="app"):
    corner_keys = ["light_living_top_left", "light_living_top_right", "light_living_bottom_left", "light_living_bottom_right"]
    target_keys = list(corner_keys)
    if group_type == "all_living":
        target_keys.append("light_living_center")
    with _LOCK:
        for device_id in target_keys:
            apply_command(device_id, action, source)
    return target_keys


def schedule_command(device_id, action, delay_seconds, source):
    log_event(device_id, f"scheduled_{action}_in_{delay_seconds}s", source)
    timer = threading.Timer(delay_seconds, apply_command, args=[device_id, action, source])
    timer.daemon = True
    timer.start()


def schedule_group_command(action, group_type, delay_seconds, source):
    log_event(f"group_{group_type}", f"scheduled_{action}_in_{delay_seconds}s", source)
    timer = threading.Timer(delay_seconds, apply_group_command, args=[action, group_type, source])
    timer.daemon = True
    timer.start()


def rwf_to_kwh(amount_rwf, start_kwh_used):
    """Converts an RWF top-up into kWh using the real tiered tariff,
    starting from whatever bracket the household is currently in based
    on monthly consumption so far."""
    remaining = amount_rwf
    used = start_kwh_used
    kwh_bought = 0.0
    for lo, hi, rate in TARIFF_TIERS:
        if used >= hi:
            continue
        capacity = hi - max(used, lo)
        if capacity <= 0:
            continue
        affordable = remaining / rate
        take = min(capacity, affordable)
        if take <= 0:
            continue
        kwh_bought += take
        remaining -= take * rate
        used += take
        if remaining <= 1e-9:
            break
    return kwh_bought


def current_watts():
    with _LOCK:
        return sum(w for did, w in DEVICE_WATTS.items() if DEVICES.get(did, {}).get("state") == "on")


def _energy_drain_loop():
    """Background thread: drains the kWh balance every second based on
    live device wattage, attributes consumption per-device, and auto-cuts
    every electric device off the instant balance hits zero."""
    while True:
        time.sleep(1)
        with _LOCK:
            active = {did: w for did, w in DEVICE_WATTS.items() if DEVICES.get(did, {}).get("state") == "on"}
            total_watts = sum(active.values())
            if total_watts == 0:
                continue

            kwh_this_tick = total_watts / 1000.0 / 3600.0  # 1 second of draw
            if ENERGY["balance_kwh"] > 0:
                actual_kwh = min(kwh_this_tick, ENERGY["balance_kwh"])
                ENERGY["balance_kwh"] -= actual_kwh
                ENERGY["monthly_kwh_consumed"] += actual_kwh
                ENERGY["total_consumed_kwh"] += actual_kwh
                # attribute this tick's consumption to each active device,
                # proportional to its own wattage share of the total load
                actual_wh = actual_kwh * 1000.0
                for did, w in active.items():
                    DEVICE_WH_CONSUMED[did] = DEVICE_WH_CONSUMED.get(did, 0.0) + (w / total_watts) * actual_wh

            if ENERGY["balance_kwh"] <= 0:
                ENERGY["balance_kwh"] = 0.0
                for did in active:
                    apply_command(did, "off", "auto-cutoff")
                if active:
                    log_event("electricity", "auto_cutoff_balance_zero", "system")


def _would_draw_power(device_id):
    return DEVICE_WATTS.get(device_id, 0) > 0


def _blocked_by_zero_balance(device_ids, action):
    """True if this action would turn on/open something that draws power
    while the prepaid balance is already at zero."""
    if action not in ("on", "open"):
        return False
    with _LOCK:
        if ENERGY["balance_kwh"] > 0:
            return False
        return any(_would_draw_power(d) for d in device_ids)


@app.before_request
def require_auth_for_sensitive_routes():
    public_paths = {"/", "/dashboard", "/showcase", "/xray", "/web", "/threed", "/auth/register", "/auth/login", "/auth/logout", "/auth/me"}
    if request.path.startswith("/static"):
        return None
    if request.path in public_paths:
        return None
    if request.path.startswith("/command") or request.path.startswith("/interpret") or request.path.startswith("/state") or request.path.startswith("/energy") or request.path.startswith("/devices"):
        if not session.get("user_id"):
            return jsonify({"ok": False, "error": "Authentication required"}), 401
    return None


@app.route("/")
def index():
    if not session.get("user_id"):
        from flask import redirect, url_for
        return redirect(url_for("login_page"))
    return render_template("xray.html")


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route("/showcase")
def showcase():
    return render_template("showcase.html")


@app.route("/xray")
def xray_tour():
    return render_template("xray.html")


@app.route("/state", methods=["GET"])
def get_state():
    with _LOCK:
        return jsonify({"devices": DEVICES, "log": EVENT_LOG})


@app.route("/energy", methods=["GET"])
def get_energy():
    with _LOCK:
        breakdown = sorted(
            ({"device": did, "label": DEVICES[did]["label"], "watts": DEVICE_WATTS[did], "wh_consumed": round(wh, 2)}
             for did, wh in DEVICE_WH_CONSUMED.items()),
            key=lambda x: x["wh_consumed"], reverse=True,
        )
        return jsonify({
            "balance_kwh": round(ENERGY["balance_kwh"], 4),
            "monthly_kwh_consumed": round(ENERGY["monthly_kwh_consumed"], 4),
            "total_consumed_kwh": round(ENERGY["total_consumed_kwh"], 4),
            "total_topup_rwf": round(ENERGY["total_topup_rwf"], 2),
            "current_watts": current_watts(),
            "tariff_tiers": [{"from_kwh": lo, "to_kwh": (None if hi == float("inf") else hi), "rate_rwf_per_kwh": rate} for lo, hi, rate in TARIFF_TIERS],
            "device_breakdown": breakdown,
        })


@app.route("/energy/topup", methods=["POST"])
def topup_energy():
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount_rwf", 0))
    except (ValueError, TypeError):
        return jsonify({"ok": False, "error": "amount_rwf must be a number"}), 400
    if amount <= 0:
        return jsonify({"ok": False, "error": "amount must be positive"}), 400

    with _LOCK:
        kwh_bought = rwf_to_kwh(amount, ENERGY["monthly_kwh_consumed"])
        ENERGY["balance_kwh"] += kwh_bought
        ENERGY["total_topup_rwf"] += amount
        log_event("electricity", f"topup_{int(amount)}rwf_{kwh_bought:.3f}kwh", "app")
        return jsonify({
            "ok": True,
            "kwh_purchased": round(kwh_bought, 4),
            "balance_kwh": round(ENERGY["balance_kwh"], 4),
        })


@app.route("/energy/reset_month", methods=["POST"])
def reset_month():
    """Demo-only helper: resets the tariff bracket without touching the
    balance. Real meters do this automatically on calendar rollover;
    we don't model real time here."""
    with _LOCK:
        ENERGY["monthly_kwh_consumed"] = 0.0
    return jsonify({"ok": True, "monthly_kwh_consumed": 0.0})


@app.route("/command", methods=["POST"])
def receive_command():
    data = request.get_json(silent=True) or {}
    device_id = data.get("device")
    action = data.get("action")
    source = data.get("source", "app")
    delay_seconds = data.get("delay_seconds", 0)

    if not device_id or not action:
        return jsonify({"ok": False, "error": "device and action are required"}), 400

    try:
        delay_seconds = float(delay_seconds)
    except (ValueError, TypeError):
        delay_seconds = 0

    if device_id in ["living_room_corners", "living_room_all_lights"]:
        group_type = "corners" if device_id == "living_room_corners" else "all_living"
        corner_keys = ["light_living_top_left", "light_living_top_right", "light_living_bottom_left", "light_living_bottom_right"]
        target_keys = corner_keys + (["light_living_center"] if group_type == "all_living" else [])

        if _blocked_by_zero_balance(target_keys, action):
            return jsonify({"ok": False, "error": "Insufficient electricity balance. Top up to continue.", "balance_kwh": 0}), 402

        if delay_seconds > 0:
            schedule_group_command(action, group_type, delay_seconds, f"{source}-scheduled")
            return jsonify({"ok": True, "scheduled": True, "group": group_type, "action": action, "delay_seconds": delay_seconds, "message": f"Group action '{action}' scheduled in {delay_seconds} seconds."})
        updated_devices = apply_group_command(action, group_type, source)
        return jsonify({"ok": True, "scheduled": False, "devices_updated": updated_devices, "action": action})

    if device_id not in DEVICES:
        return jsonify({"ok": False, "error": f"unknown device '{device_id}'"}), 404

    if action not in DEVICES[device_id]["actions"]:
        return jsonify({"ok": False, "error": f"invalid action '{action}' for {device_id}", "valid_actions": DEVICES[device_id]["actions"]}), 400

    if _blocked_by_zero_balance([device_id], action):
        return jsonify({"ok": False, "error": "Insufficient electricity balance. Top up to continue.", "balance_kwh": 0}), 402

    if delay_seconds > 0:
        schedule_command(device_id, action, delay_seconds, f"{source}-scheduled")
        return jsonify({"ok": True, "scheduled": True, "device": device_id, "action": action, "delay_seconds": delay_seconds, "message": f"Action '{action}' scheduled in {delay_seconds} seconds."})

    apply_command(device_id, action, source)
    return jsonify({"ok": True, "scheduled": False, "device": device_id, "state": action})


@app.route("/voice/transcribe", methods=["POST"])
def voice_transcribe():
    """Accepts a recorded audio clip (multipart 'audio' field) and returns
    its transcript using ElevenLabs Speech-to-Text (scribe). This replaces
    the browser's native SpeechRecognition, which is Chrome/Chromium-only
    and unreliable on some networks."""
    if not ELEVENLABS_API_KEY:
        return jsonify({"ok": False, "error": "No 11LABS API key set on the server (.env)."}), 503

    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"ok": False, "error": "audio file is required (multipart field 'audio')."}), 400

    audio_bytes = audio_file.read()
    if not audio_bytes:
        return jsonify({"ok": False, "error": "empty audio clip."}), 400

    filename = audio_file.filename or "clip.webm"
    content_type = audio_file.content_type or "audio/webm"

    boundary = "----smarthouse-voice-boundary"
    body = bytearray()

    def _field(name, value):
        body.extend(f'--{boundary}\r\n'.encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.extend(f'{value}\r\n'.encode())

    _field("model_id", ELEVENLABS_STT_MODEL)

    body.extend(f'--{boundary}\r\n'.encode())
    body.extend(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    body.extend(f'Content-Type: {content_type}\r\n\r\n'.encode())
    body.extend(audio_bytes)
    body.extend(b'\r\n')
    body.extend(f'--{boundary}--\r\n'.encode())

    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    req = urllib.request.Request(ELEVENLABS_STT_URL, data=bytes(body), headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
        except Exception:
            err_body = {"error": str(e)}
        return jsonify({"ok": False, "error": "ElevenLabs STT request failed.", "details": err_body}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": f"ElevenLabs STT call failed: {e}"}), 502

    transcript = (result.get("text") or "").strip()
    if not transcript:
        return jsonify({"ok": False, "error": "No speech detected."}), 422

    return jsonify({"ok": True, "transcript": transcript})


@app.route("/voice/speak", methods=["POST"])
def voice_speak():
    """Takes {"text": "..."} and returns an MP3 of it spoken, via ElevenLabs
    text-to-speech. The frontend plays this back after each assistant reply."""
    if not ELEVENLABS_API_KEY:
        return jsonify({"ok": False, "error": "No 11LABS API key set on the server (.env)."}), 503

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "text is required."}), 400

    # ElevenLabs charges per character and has a hard request limit; keep
    # spoken replies reasonably short regardless of what the caller sends.
    if len(text) > 700:
        text = text[:700].rsplit(" ", 1)[0] + "…"

    voice_id = data.get("voice_id") or ELEVENLABS_VOICE_ID
    payload = json.dumps({
        "text": text,
        "model_id": ELEVENLABS_TTS_MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }).encode("utf-8")

    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    url = f"{ELEVENLABS_TTS_URL}/{voice_id}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            audio_bytes = resp.read()
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
        except Exception:
            err_body = {"error": str(e)}
        return jsonify({"ok": False, "error": "ElevenLabs TTS request failed.", "details": err_body}), 502
    except Exception as e:
        return jsonify({"ok": False, "error": f"ElevenLabs TTS call failed: {e}"}), 502

    return Response(audio_bytes, mimetype="audio/mpeg")


class LLMCallError(Exception):
    """Raised by an LLM provider call with a message safe to log (never shown
    to the end user — /interpret always degrades to the offline parser
    instead of surfacing this). `kind` lets the caller give the user a more
    honest reason: 'network' (couldn't reach the provider at all) vs
    'api_error' (reached it, but got a bad/empty/malformed response)."""
    def __init__(self, message, status=502, kind="api_error"):
        super().__init__(message)
        self.status = status
        self.kind = kind


def _strip_code_fence(raw_text):
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()
    return raw_text


def _call_openrouter(prompt):
    payload = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "Smart House Simulator",
    }
    req = urllib.request.Request(OPENROUTER_URL, data=payload, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_body = resp.read()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "replace")[:500]
        raise LLMCallError(f"OpenRouter HTTP {e.code}: {err_body or e.reason}")
    except urllib.error.URLError as e:
        raise LLMCallError(f"Could not reach OpenRouter: {e.reason}", kind="network")

    if not raw_body.strip():
        raise LLMCallError(
            "OpenRouter returned an empty response body (HTTP 200 with no content). "
            "This usually means the routed provider crashed mid-request or rejected the "
            "response_format for this model — try again, or drop OPENROUTER_MODEL to a "
            "model you know supports JSON mode."
        )

    try:
        result = json.loads(raw_body)
    except json.JSONDecodeError as e:
        raise LLMCallError(f"OpenRouter response wasn't JSON: {e}. Raw body (first 300 chars): {raw_body[:300]!r}")

    raw_text = _strip_code_fence((result["choices"][0]["message"].get("content") or "").strip())

    if not raw_text:
        served_model = result.get("model", "unknown")
        finish_reason = result["choices"][0].get("finish_reason", "unknown")
        raise LLMCallError(
            f"OpenRouter model '{served_model}' returned empty content (finish_reason: {finish_reason}). "
            "Try again — the router may have picked a weaker model."
        )

    return raw_text


def _call_gemini_direct(prompt):
    url = GEMINI_URL_TMPL.format(model=GEMINI_MODEL) + f"?key={GEMINI_API_KEY}"
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 800,
            "response_mime_type": "application/json",
        },
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=payload, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_body = resp.read()
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", "replace")[:500]
        raise LLMCallError(f"Gemini HTTP {e.code}: {err_body or e.reason}")
    except urllib.error.URLError as e:
        raise LLMCallError(f"Could not reach Gemini: {e.reason}", kind="network")

    if not raw_body.strip():
        raise LLMCallError("Gemini returned an empty response body (HTTP 200 with no content). Try again.")

    try:
        result = json.loads(raw_body)
    except json.JSONDecodeError as e:
        raise LLMCallError(f"Gemini response wasn't JSON: {e}. Raw body (first 300 chars): {raw_body[:300]!r}")

    candidates = result.get("candidates") or []
    if not candidates:
        block_reason = (result.get("promptFeedback") or {}).get("blockReason", "unknown")
        raise LLMCallError(f"Gemini returned no candidates (blockReason: {block_reason}).")

    parts = (candidates[0].get("content") or {}).get("parts") or []
    raw_text = _strip_code_fence("".join(p.get("text", "") for p in parts).strip())

    if not raw_text:
        finish_reason = candidates[0].get("finishReason", "unknown")
        raise LLMCallError(f"Gemini returned empty content (finishReason: {finish_reason}). Try again.")

    return raw_text


_OFFLINE_GREETING_REPLIES = [
    (("hey", "hi ", " hi", "hello", "yo ", " yo", "sup", "good morning", "good afternoon", "good evening"),
     "Hey! How can I help you today?"),
    (("how are you", "how's it going", "how r u", "how are u"),
     "I'm doing well, thanks for asking! What can I do for you?"),
    (("thanks", "thank you", "thx"),
     "You're welcome! Let me know if you need anything else."),
    (("bye", "goodbye", "see you", "see ya"),
     "Goodbye! Just say the word if you need me again."),
]

_OFFLINE_ACTION_WORDS = ("light", "lights", "door", "doors", "socket", "sockets",
                          "gate", "valve", "energy", "balance", "power", "kwh")


_OFFLINE_REASON_BLURBS = {
    "no_key": "I'm offline right now (no AI key configured)",
    "network": "I'm offline right now (poor network — couldn't reach the AI)",
    "api_error": "I'm offline right now (the AI service is having issues)",
}


def _offline_interpret(transcript, reason="no_key"):
    """Rule-based fallback used when no LLM key is configured, or the live
    LLM call fails for any reason. Handles ordinary greetings with a canned
    reply and does simple keyword matching (room/device + on/off/open/close)
    against the real DEVICES dict, so basic commands like "turn on room 1"
    still work with zero network access. Returns the same
    {"reply", "commands"} shape the LLM path produces, so the rest of
    /interpret doesn't need to care which path produced it.

    `reason` controls only the human-facing wording ('no_key', 'network',
    'api_error') — the actual provider error text/details are never shown
    to the user, only logged server-side."""
    blurb = _OFFLINE_REASON_BLURBS.get(reason, _OFFLINE_REASON_BLURBS["api_error"])
    t = " " + transcript.lower().strip() + " "

    with _LOCK:
        snapshot = {did: dict(d) for did, d in DEVICES.items()}

    device_terms = {did.lower() for did in snapshot} | {d["label"].lower() for d in snapshot.values()}
    room_terms = {d["room"].lower() for d in snapshot.values()}
    mentions_house = any(term in t for term in device_terms | room_terms | set(_OFFLINE_ACTION_WORDS))

    if not mentions_house:
        for keywords, reply in _OFFLINE_GREETING_REPLIES:
            if any(k in t for k in keywords):
                return {"reply": reply, "commands": []}
        return {
            "reply": f"{blurb}, so I can only handle direct commands like 'turn on room 1' or "
                     "'close the gate' — I can't chat freely right now.",
            "commands": [],
        }

    action = None
    if re.search(r"\bopen\b", t):
        action = "open"
    elif re.search(r"\bclos(e|ed|ing)\b", t):
        action = "close"
    elif re.search(r"\b(turn|switch)\s+off\b", t) or re.search(r"\boff\b", t):
        action = "off"
    elif re.search(r"\b(turn|switch)\s+on\b", t) or re.search(r"\bon\b", t):
        action = "on"

    if not action:
        return {
            "reply": f"{blurb} and I couldn't tell what action you want — try something like "
                     "'turn on room 1' or 'open the gate'.",
            "commands": [],
        }

    matched_room = None
    for d in snapshot.values():
        if d["room"].lower() in t:
            if matched_room is None or len(d["room"]) > len(matched_room):
                matched_room = d["room"]

    matched_devices = []
    if matched_room:
        matched_devices = [did for did, d in snapshot.items()
                            if d["room"] == matched_room and action in d["actions"]]
    else:
        matched_devices = [did for did, d in snapshot.items()
                            if (did.lower() in t or d["label"].lower() in t
                                or f' {d["type"].lower()} ' in t or f' {d["type"].lower()}s ' in t)
                            and action in d["actions"]]

    if not matched_devices:
        return {
            "reply": f"{blurb} and I couldn't match that to a known room or device — try naming "
                     "a room like 'Room 1' or 'Kitchen'.",
            "commands": [],
        }

    commands = [{"device": did, "action": action, "delay_seconds": 0, "reasoning": "offline rule-based match"}
                for did in matched_devices]
    verb = {"on": "Turning on", "off": "Turning off", "open": "Opening", "close": "Closing"}[action]
    target_label = matched_room if matched_room else ", ".join(snapshot[d]["label"] for d in matched_devices)
    reply = f"{verb} {target_label} (offline mode)."
    return {"reply": reply, "commands": commands}


@app.route("/interpret", methods=["POST"])
def interpret_command():
    data = request.get_json(silent=True) or {}
    transcript = data.get("transcript", "").strip()
    if not transcript:
        return jsonify({"ok": False, "error": "transcript is required"}), 400

    if not OPENROUTER_API_KEY and not GEMINI_API_KEY:
        parsed = _offline_interpret(transcript, reason="no_key")
        return _finish_interpret(transcript, parsed, offline=True)

    with _LOCK:
        device_list = "\n".join(
            f'- "{did}" ({d["label"]}, room: {d["room"]}, type: {d["type"]}, current state: {d["state"]}) — valid actions: {d["actions"]}'
            for did, d in DEVICES.items()
        )
        energy_summary = (
            f'Balance: {round(ENERGY["balance_kwh"], 2)} kWh remaining, '
            f'currently drawing {current_watts()} W, '
            f'{round(ENERGY["monthly_kwh_consumed"], 2)} kWh consumed this month.'
        )

    prompt = (
        "You are a friendly smart home AI assistant embedded in a chat interface. You handle THREE kinds "
        "of user messages, and you must always figure out which kind this one is:\n"
        "1. DEVICE COMMANDS — explicit, indirect, or observational statements that imply a device action "
        "(e.g. 'no one is in the room', 'it's too bright', 'we're going to sleep' -> infer and act, never "
        "refuse just because it isn't phrased as a direct command).\n"
        "2. HOUSE QUESTIONS / SUGGESTIONS — the user asks about house status, energy usage, or wants "
        "suggestions (e.g. 'how's the house doing', 'what should I turn off to save power', 'any updates'). "
        "Answer using the real state and energy data below. No commands needed for these.\n"
        "3. ORDINARY CHAT — greetings, small talk, thanks, unrelated questions (e.g. 'hey', 'hello', 'thanks'). "
        "Reply naturally and briefly like a helpful assistant. No commands needed for these.\n\n"
        "CRITICAL: A bare greeting or small talk with NO mention of a room, device, or state "
        "(e.g. 'hey', 'hello', 'thanks', 'how are you') must ALWAYS produce an empty commands list. "
        "Never guess a device action just to have something to do. When in doubt, do nothing and just reply.\n\n"
        "CRITICAL: Never use the word 'Done' or 'Okay' alone as a reply. For ordinary chat, write an actual "
        "sentence a person would say (e.g. 'Hey! Everything's running fine.'). For commands, describe what "
        "you did in plain language (e.g. 'Turned on the center living room light.'), not just 'Done'.\n\n"
        f"Known devices (with current state):\n{device_list}\n\n"
        f"Energy status:\n{energy_summary}\n\n"
        "Special Group Keywords for 'device':\n"
        "- Use 'living_room_corners' for corner lights.\n"
        "- Use 'living_room_all_lights' for all living room lights.\n\n"
        "Rules:\n"
        "- ALWAYS include a short, natural-language 'reply' string meant to be shown directly to the user "
        "in a chat bubble — even when there are no commands. Never leave 'reply' empty.\n"
        "- If it's a device command, parse ALL implied or explicit actions from the transcript, and make "
        "'reply' a brief confirmation of what you're doing.\n"
        "- Only target devices relevant to the mentioned room or context. If a specific room is mentioned, "
        "only act on devices in that room.\n"
        "- If the user says a device/room is already in a state (e.g. 'room 2 and 4 are on'), treat that as "
        "context — if combined with 'no one is there', turn those off.\n"
        "- Parse any time duration (e.g. 'in 2 minutes' -> delay_seconds: 120). Default is 0.\n"
        "- If it's a house question, suggestion request, or ordinary chat, set 'commands' to an empty list "
        "and put the actual answer in 'reply'.\n"
        "- Respond with ONLY a JSON object, no markdown, no code fences:\n"
        '{"reply": "<short natural-language message to show the user, always non-empty>", '
        '"commands": [{"device": "<device_id or group_keyword>", "action": "<action>", "delay_seconds": <number>, "reasoning": "<one short sentence>"}]}\n\n'
        f'Transcript: "{transcript}"'
    )

    offline = False
    try:
        if OPENROUTER_API_KEY:
            try:
                raw_text = _call_openrouter(prompt)
            except LLMCallError:
                if GEMINI_API_KEY:
                    raw_text = _call_gemini_direct(prompt)
                else:
                    raise
        else:
            raw_text = _call_gemini_direct(prompt)
        parsed = json.loads(raw_text)
    except LLMCallError as e:
        # Both providers failed (or the only configured one did) — degrade to
        # the offline rule-based parser instead of erroring out, so basic
        # commands still work when the network/API is unavailable. The real
        # error (e) is only ever logged, never sent to the client.
        app.logger.warning("interpret: LLM call failed (%s: %s), falling back to offline parser", e.kind, e)
        parsed = _offline_interpret(transcript, reason=e.kind)
        offline = True
    except Exception as e:
        # Catch-all: malformed JSON, unexpected shape, anything else. Same
        # deal — never leak internals to the user, just degrade gracefully.
        app.logger.warning("interpret: LLM call/parse failed (%s: %s), falling back to offline parser", type(e).__name__, e)
        parsed = _offline_interpret(transcript, reason="api_error")
        offline = True

    return _finish_interpret(transcript, parsed, offline=offline)


def _finish_interpret(transcript, parsed, offline=False):
    app.logger.info("interpret: transcript=%r parsed=%r offline=%s", transcript, parsed, offline)

    reply_text = (parsed.get("reply") or parsed.get("reasoning") or "").strip()
    commands = parsed.get("commands", [])

    # Safety gate: don't let a short, keyword-free transcript execute a device
    # action even if the model returned one. Greetings/small talk have no
    # business touching hardware.
    if commands:
        with _LOCK:
            known_terms = set()
            for did, d in DEVICES.items():
                known_terms.add(did.lower())
                known_terms.add(d["label"].lower())
                known_terms.add(d["room"].lower())
                known_terms.add(d["type"].lower())
        known_terms.update({"living_room_corners", "living_room_all_lights", "corner", "corners", "sleep", "empty", "leaving", "left", "everyone", "no one"})
        transcript_lower = transcript.lower()
        has_grounding = any(term in transcript_lower for term in known_terms)
        if not has_grounding:
            app.logger.warning(
                "interpret: BLOCKED ungrounded commands for transcript=%r commands=%r", transcript, commands
            )
            return jsonify({
                "ok": True,
                "reply": reply_text or "Got it.",
                "commands": [],
                "executed": [],
                "offline": offline,
                "note": "Model proposed device actions with no matching keywords in your message, so nothing was executed.",
            })

    if not commands:
        # No device action needed — this was a greeting, a house-status/suggestion
        # question, or ordinary chat. That's a normal, successful outcome, not an
        # error: return the assistant's reply so the chat UI has something to show.
        return jsonify({
            "ok": True,
            "reply": reply_text or "Okay.",
            "commands": [],
            "executed": [],
            "offline": offline,
        })

    source_tag = "offline-rules" if offline else "online-llm"
    executed_results = []
    for cmd in commands:
        device_id = cmd.get("device")
        action = cmd.get("action")
        reasoning = cmd.get("reasoning", "")
        try:
            delay_seconds = float(cmd.get("delay_seconds", 0))
        except (ValueError, TypeError):
            delay_seconds = 0

        if device_id in ["living_room_corners", "living_room_all_lights"]:
            group_type = "corners" if device_id == "living_room_corners" else "all_living"
            corner_keys = ["light_living_top_left", "light_living_top_right", "light_living_bottom_left", "light_living_bottom_right"]
            target_keys = corner_keys + (["light_living_center"] if group_type == "all_living" else [])

            if _blocked_by_zero_balance(target_keys, action):
                executed_results.append({"device": device_id, "action": action, "blocked": True, "error": "Insufficient electricity balance.", "reasoning": reasoning})
                continue

            if delay_seconds > 0:
                schedule_group_command(action, group_type, delay_seconds, f"{source_tag}-scheduled")
                executed_results.append({"device": device_id, "action": action, "scheduled": True, "delay_seconds": delay_seconds, "reasoning": reasoning})
            else:
                updated = apply_group_command(action, group_type, source_tag)
                executed_results.append({"device": device_id, "action": action, "scheduled": False, "devices_updated": updated, "reasoning": reasoning})

        elif device_id in DEVICES and action in DEVICES[device_id]["actions"]:
            if _blocked_by_zero_balance([device_id], action):
                executed_results.append({"device": device_id, "action": action, "blocked": True, "error": "Insufficient electricity balance.", "reasoning": reasoning})
                continue

            if delay_seconds > 0:
                schedule_command(device_id, action, delay_seconds, f"{source_tag}-scheduled")
                executed_results.append({"device": device_id, "action": action, "scheduled": True, "delay_seconds": delay_seconds, "reasoning": reasoning})
            else:
                apply_command(device_id, action, source_tag)
                executed_results.append({"device": device_id, "action": action, "scheduled": False, "state": action, "reasoning": reasoning})

    if not executed_results:
        return jsonify({"ok": False, "error": "No valid devices or actions could be executed.", "parsed_commands": commands}), 502

    return jsonify({"ok": True, "reply": reply_text or "Done.", "executed_commands": executed_results, "offline": offline})


@app.route("/web")
def command_tester():
    # require a logged-in user to view the web AI page
    if not session.get("user_id"):
        from flask import redirect, url_for
        return redirect(url_for("login_page"))
    return render_template("tester.html")


@app.route("/threed")
def threed_view():
    return render_template("3d.html")


@app.route("/devices", methods=["GET"])
def list_devices():
    with _LOCK:
        return jsonify({
            device_id: {
                "label": d["label"],
                "room": d["room"],
                "type": d["type"],
                "actions": d["actions"],
                "watts": DEVICE_WATTS.get(device_id, 0),
            }
            for device_id, d in DEVICES.items()
        })


@app.route("/auth/register", methods=["POST"])
def register_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    email = (data.get("email") or "").strip().lower() or None
    number_of_room = data.get("number_of_room")

    if not username or not password:
        return jsonify({"ok": False, "error": "username and password are required"}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "password must be at least 6 characters"}), 400

    try:
        if number_of_room is not None:
            number_of_room = int(number_of_room)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "number_of_room must be an integer"}), 400

    password_hash = generate_password_hash(password)
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, email, password_hash, number_of_room, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, email, password_hash, number_of_room, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        user_id = cursor.lastrowid
        session["user_id"] = user_id
        return jsonify({
            "ok": True,
            "user": {
                "id": user_id,
                "username": username,
                "email": email,
                "number_of_room": number_of_room,
            },
        })
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        if "UNIQUE constraint failed: users.username" in str(exc):
            return jsonify({"ok": False, "error": "username already exists"}), 409
        if "UNIQUE constraint failed: users.email" in str(exc):
            return jsonify({"ok": False, "error": "email already exists"}), 409
        return jsonify({"ok": False, "error": "user could not be created"}), 400
    finally:
        conn.close()


@app.route("/auth/login", methods=["POST"])
def login_user():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not username or not password:
        return jsonify({"ok": False, "error": "username and password are required"}), 400

    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id, username, email, password_hash, number_of_room FROM users WHERE username = ?", (username,)).fetchone()
        # allow login either via stored password hash OR using an ESP default password
        esp_default = os.environ.get("ESP_DEFAULT_PASSWORD")
        valid = False
        if row and check_password_hash(row["password_hash"], password):
            valid = True
        elif row and esp_default and password == esp_default:
            # admin-set default (on the ESP) may be used to authenticate devices/users
            valid = True

        if not row or not valid:
            return jsonify({"ok": False, "error": "invalid username or password"}), 401
        session["user_id"] = row["id"]
        return jsonify({
            "ok": True,
            "user": {
                "id": row["id"],
                "username": row["username"],
                "email": row["email"],
                "number_of_room": row["number_of_room"],
            },
        })
    finally:
        conn.close()


@app.route("/login", methods=["GET"])
def login_page():
    # simple login form for web access
    return render_template("login.html")


@app.route("/auth/logout", methods=["POST"])
def logout_user():
    session.pop("user_id", None)
    return jsonify({"ok": True})


@app.route("/auth/me", methods=["GET"])
def auth_me():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "authenticated": False}), 401
    return jsonify({"ok": True, "authenticated": True, "user": user})


init_auth_db()

# The background drain thread must only ever exist once per running
# process. Werkzeug's auto-reloader (the default when debug=True) forks a
# second process on every file save, which would start a second drain
# thread and silently double the drain rate — and it's the same mechanism
# that can kill pending scheduled commands mid-demo. Simplest fix: disable
# the reloader outright (use_reloader=False below) instead of trying to
# detect which process we're in.
_drain_thread = threading.Thread(target=_energy_drain_loop, daemon=True)
_drain_thread.start()


if __name__ == "__main__":
    import sys
    use_https = "--https" in sys.argv
    if use_https:
        import subprocess
        print("Starting HTTPS via gunicorn on https://0.0.0.0:8000")
        subprocess.run([
            "gunicorn", "app:app",
            "--bind", "0.0.0.0:8000",
            "--certfile", "cert.pem",
            "--keyfile", "key.pem",
            "--workers", "1",
            "--threads", "4",
        ])
    else:
        print("Starting HTTP on http://0.0.0.0:8000")
        app.run(host="0.0.0.0", port=8000, debug=True, use_reloader=False)

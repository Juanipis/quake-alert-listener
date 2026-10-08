#!/usr/bin/env python3
"""android_alert_listener.py
Optional companion: forwards Google's Android Earthquake Alerts (AEAS) from a real
Android device to Quake MCS Listener (POST :8990/android).

Google only sends AEAS alerts to devices that report their location, so this watches
one that does: a phone over ADB or a containerized Android (Redroid) with a mock
location. It follows logcat (no polling), confirms the full-screen alert with
`dumpsys activity top`, ignores the settings demo, and posts the alert signed with
HMAC-SHA256. Status: a Redroid container ran it on a Raspberry Pi 4 on 2026-10-07; it only
saw Google's settings demo (correctly ignored), never a real earthquake alert, and was
paused because that image's Play services (22.09) can't be updated.
See docs/REDROID_SENTINEL.md.

Environment:
  QUAKE_ALERT_URL       e.g. http://127.0.0.1:8990/android (required to send)
  QUAKE_ANDROID_SECRET  shared HMAC secret (also read: QUAKE_SECRET, SISMO_ANDROID_SECRETO)
  QUAKE_LAT, QUAKE_LON  base station, injected as the device location (Redroid)

Protocol: POST QUAKE_ALERT_URL with JSON payload
  {"source": "Google Android", "level": "alert"|"notice", "detected": <epoch>, "text": ...,
   "magnitude"?: float, "distance_km"?: number, "lat"?: float, "lon"?: float}
and header X-Quake-Signature = hex HMAC-SHA256 of the exact request body. `detected` is
when the device showed the alert, not the origin time, so the bridge gives no countdown.

Usage:
  android_alert_listener.py                 Stream events in real time
  android_alert_listener.py --once          Inspect current foreground activity and exit
  android_alert_listener.py --include-demo  Do not discard settings demo (for local testing)
  android_alert_listener.py --drill         End-to-end drill test (fires drill webhook)
  DOCKER_CONTAINER=redroid-quake android_alert_listener.py
  ADB_SERIAL=emulator-5554 android_alert_listener.py
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

URL = os.environ.get("QUAKE_ALERT_URL") or os.environ.get("ANDROID_ALERT_URL", "")
SECRET = (os.environ.get("QUAKE_ANDROID_SECRET") or os.environ.get("QUAKE_SECRET")
          or os.environ.get("SISMO_ANDROID_SECRETO", ""))
SERIAL = os.environ.get("ADB_SERIAL", "")
CONTAINER = os.environ.get("DOCKER_CONTAINER", "")
DEDUPE_S = 10 * 60
ADB = shutil.which("adb") or "/opt/homebrew/bin/adb"
GMS = "com.google.android.gms"

# Base coordinates
LAT = float(os.environ.get("QUAKE_LAT") or os.environ.get("SISMO_LAT", "0.0"))
LON = float(os.environ.get("QUAKE_LON") or os.environ.get("SISMO_LON", "0.0"))

# Regular expressions
RE_QUAKE = re.compile(r"earthquake|sismo|terremoto|temblor", re.I)
RE_ALERT = re.compile(r"take action|toma(r)? medidas|protect yourself|prot[eé]gete|drop,? cover", re.I)
RE_MAGNITUDE = re.compile(r"(?:magnitude|magnitud|\bM)\s*([0-9]+(?:[.,][0-9])?)", re.I)
RE_DISTANCE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*(km|kil[oó]metros?|mi(?:les|llas)?)\b", re.I)

# Fullscreen EAlertSafetyInfoActivity argument parsing from dumpsys activity top
RE_EALERT_ARGS = re.compile(r"EAlertUxArgs\s*\{([^}]+)\}", re.I)
RE_ARG_MAG = re.compile(r"magnitude=([0-9]+(?:\.[0-9]+)?)")
RE_ARG_DIST = re.compile(r"distanceToEpicenterKm=([0-9]+(?:\.[0-9]+)?)")
RE_ARG_EPI = re.compile(r"epicenter=\[lat/lng:\s*\((-?[0-9.]+),(-?[0-9.]+)\)")
RE_ARG_TEST = re.compile(r"isTestAlert=(true|false)", re.I)
RE_ARG_REGION = re.compile(r"arwRegionName=([^,}]+)")

INCLUDE_DEMO = "--include-demo" in sys.argv or "--incluir-demo" in sys.argv
LEVELS = {"notice": 1, "aviso": 1, "alert": 2, "alerta": 2}


def log(msg):
    sys.stdout.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    sys.stdout.flush()


def adb(*args, timeout=15):
    if CONTAINER:
        # If running inside a container, execute shell commands directly via docker exec
        cmd_args = list(args)
        if cmd_args and cmd_args[0] == "shell":
            cmd_args = cmd_args[1:]
        cmd = ["docker", "exec", CONTAINER] + cmd_args
    else:
        cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def ensure_connection():
    if CONTAINER:
        try:
            r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER], capture_output=True, text=True, timeout=5)
            if r.stdout.strip() != "true":
                log(f"Container {CONTAINER} is not running; waiting...")
                return False
            return True
        except Exception as e:
            log(f"Error inspecting container {CONTAINER}: {e}")
            return False
    r = adb("get-state", timeout=10)
    if r.stdout.strip() != "device":
        log(f"ADB device not ready ({(r.stderr or r.stdout).strip()}); reconnecting")
        adb("reconnect", timeout=10)
        return False
    return True


def apply_location_beacon():
    """Injects high-precision base coordinates into Android LocationManager & FakeGPS."""
    if LAT == 0.0 and LON == 0.0:
        return
    try:
        if CONTAINER:
            subprocess.run(["docker", "exec", CONTAINER, "cmd", "location", "set-location-enabled", "true"], timeout=5, capture_output=True)
            subprocess.run(["docker", "exec", CONTAINER, "settings", "put", "secure", "location_mode", "3"], timeout=5, capture_output=True)
            subprocess.run(["docker", "exec", CONTAINER, "appops", "set", "com.lexa.fakegps", "android:mock_location", "allow"], timeout=5, capture_output=True)
            subprocess.run(["docker", "exec", CONTAINER, "am", "start-foreground-service", "-a", "com.lexa.fakegps.START", "-e", "lat", str(LAT), "-e", "long", str(LON)], timeout=5, capture_output=True)
        else:
            adb("shell", "cmd", "location", "set-location-enabled", "true", timeout=5)
            adb("emu", "geo", "fix", str(LON), str(LAT), timeout=5)
    except Exception as e:
        log(f"Error setting location beacon: {e}")


def gms_notifications():
    """Returns a list of (key, channel, text) for active Play Services notifications."""
    r = adb("shell", "dumpsys", "notification", "--noredact", timeout=10)
    result = []
    for block in re.split(r"\n\s*NotificationRecord\(", r.stdout):
        if f"pkg={GMS}" not in block:
            continue
        k = re.search(r"key=(\S+)", block)
        ch = re.search(r"(?:channel|mChannel)=.*?(?:mId|id)=([^\s,]+)", block)
        texts = re.findall(r"android\.(?:title|text|bigText|subText)=\S*\s*\(?\s*(.*)", block)
        text = " | ".join(t.strip().rstrip(")") for t in texts if t.strip())
        result.append((k.group(1) if k else "", ch.group(1) if ch else "", text))
    return result


def inspect_ealert_activity(max_retries=2):
    """Inspects the foreground activity via dumpsys activity top.
    Returns earthquake event dict ONLY if EAlertSafetyInfoActivity is active and visible on screen.
    Returns None if screen is not visible or demo without --include-demo, preventing false alarms."""
    for attempt in range(max_retries):
        r = adb("shell", "dumpsys", "activity", "top", timeout=5)
        if "EAlertSafetyInfoActivity" in r.stdout:
            args_match = RE_EALERT_ARGS.search(r.stdout)
            args_str = args_match.group(1) if args_match else r.stdout

            m_test = RE_ARG_TEST.search(args_str)
            is_test = m_test and m_test.group(1).lower() == "true"
            if is_test and not INCLUDE_DEMO:
                log("Demo/test alert detected in settings; ignored (no dispatch)")
                return None

            m_mag = RE_ARG_MAG.search(args_str)
            m_dist = RE_ARG_DIST.search(args_str)
            m_epi = RE_ARG_EPI.search(args_str)
            m_reg = RE_ARG_REGION.search(args_str)

            mag = float(m_mag.group(1)) if m_mag else None
            dist = round(float(m_dist.group(1)), 1) if m_dist else None
            region = m_reg.group(1).strip() if m_reg else "Local Region"

            event = {
                "fuente": "Google Android",
                "source": "Google Android",
                "nivel": "alerta",
                "level": "alert",
                "detectado": time.time(),
                "detected": time.time(),
                "texto": f"Google AEAS Fullscreen Alert: {f'M{mag}' if mag else ''} at {f'{dist} km' if dist else ''} ({region})".strip(),
                "text": f"Google AEAS Fullscreen Alert: {f'M{mag}' if mag else ''} at {f'{dist} km' if dist else ''} ({region})".strip(),
            }
            if mag:
                event["magnitud"] = mag
                event["magnitude"] = mag
            if dist:
                event["distancia_km"] = dist
                event["distance_km"] = dist
            if m_epi:
                event["lat"] = float(m_epi.group(1))
                event["lon"] = float(m_epi.group(2))

            return event
        if attempt < max_retries - 1:
            time.sleep(0.15)

    # If EAlertSafetyInfoActivity is not confirmed in foreground, discard safely to prevent false alarm
    log("EAlertSafetyInfoActivity not confirmed in foreground; discarded safely to prevent false alarm.")
    return None


def build_notification_event(text):
    level = "alert" if RE_ALERT.search(text) else "notice"
    event = {
        "fuente": "Google Android", "source": "Google Android",
        "nivel": "alerta" if level == "alert" else "aviso", "level": level,
        "detectado": time.time(), "detected": time.time(),
        "texto": text[:500], "text": text[:500]
    }
    m = RE_MAGNITUDE.search(text)
    if m:
        val = float(m.group(1).replace(",", "."))
        event["magnitud"] = val
        event["magnitude"] = val
    d = RE_DISTANCE.search(text)
    if d:
        val = float(d.group(1).replace(",", "."))
        is_miles = re.match(r"mi", d.group(2), re.I)
        dist = round(val * 1.609, 1) if is_miles else val
        event["distancia_km"] = dist
        event["distance_km"] = dist
    return event


def dispatch(event):
    body = json.dumps(event, ensure_ascii=False).encode()
    if not URL:
        log(f"[dry-run] Detected alert (not sending, QUAKE_ALERT_URL is not set): {body.decode()}")
        return
    headers = {"Content-Type": "application/json"}
    if SECRET:
        sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Quake-Signature"] = sig
        headers["X-Sismo-Firma"] = sig   # legacy header name
    req = urllib.request.Request(URL, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        log(f"Dispatched alert to gateway ({resp.status}): {body.decode()}")


def maintenance_loop(interval_s=600):
    """Maintains location beacon alive in Google Play Services every 10 minutes."""
    while True:
        time.sleep(interval_s)
        if ensure_connection():
            apply_location_beacon()


def process_candidate(event, last):
    now = time.time()
    lvl = LEVELS[event.get("level", event.get("nivel", "notice"))]
    if now - last["time"] < DEDUPE_S and lvl <= last["level"]:
        return False
    try:
        dispatch(event)
        last.update(time=now, level=lvl)
        return True
    except Exception as e:
        log(f"Error dispatching event: {e}")
        return False


def run_once():
    if not ensure_connection():
        return
    apply_location_beacon()
    last = {"time": 0.0, "level": 0}
    act = inspect_ealert_activity()
    if act:
        process_candidate(act, last)
    for _, _, t in gms_notifications():
        if RE_QUAKE.search(t):
            process_candidate(build_notification_event(t), last)


def start_logcat_stream():
    """Starts a continuous logcat stream, filtered on the device side so idle cost stays low."""
    filter_regex = "EAlert|ealert|sismo|Sismo|earthquake|Earthquake|terremoto|temblor"
    if CONTAINER:
        cmd = [
            "docker", "exec", CONTAINER,
            "logcat", "-v", "time", "-b", "main", "-b", "events", "-b", "system", "-T", "1",
            "-e", filter_regex
        ]
    else:
        cmd = [ADB] + (["-s", SERIAL] if SERIAL else []) + [
            "logcat", "-v", "time", "-b", "main", "-b", "events", "-b", "system", "-T", "1",
            "-e", filter_regex
        ]
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )


def listen_realtime():
    last = {"time": 0.0, "level": 0}
    log("Starting Android AEAS sentinel (filtered logcat stream)")
    if URL:
        log(f"Target gateway: {URL}{'' if SECRET else ' (unsigned: only accepted by a bridge on this machine)'}")
    else:
        log("Running in local test mode (QUAKE_ALERT_URL not set, nothing is sent)")

    ensure_connection()
    apply_location_beacon()

    # Location maintenance background thread
    t_mant = threading.Thread(target=maintenance_loop, daemon=True)
    t_mant.start()

    while True:
        try:
            if not ensure_connection():
                time.sleep(3)
                continue

            log("Connecting Android logcat event stream...")
            proc = start_logcat_stream()

            for line in proc.stdout:
                line_lower = line.lower()

                # Ignore settings / configuration debug activities
                if "ealertsettings" in line_lower or "ealertgooglesettingdebug" in line_lower:
                    continue

                # Ignore activity exit, pause, stop, or teardown events
                if any(k in line_lower for k in ("pause", "destroy", "stop", "finish", "remove", "killing", "died", "transit to stopped")):
                    continue

                # Ignore lines containing explicit test alert demo flag
                if "istestalert=true" in line_lower and not INCLUDE_DEMO:
                    continue

                # 1. Fullscreen Take Action alert (Ultra-high priority)
                if "EAlertSafetyInfoActivity" in line or "EALERT_SAFETY_INFO" in line:
                    log(f"⚡ AEAS SEISMIC TRIGGER DETECTED IN LOGCAT: {line.strip()}")
                    event = inspect_ealert_activity()
                    if event:
                        process_candidate(event, last)

                # 2. Play Services notification / earthquake advisory
                elif RE_QUAKE.search(line) or "EAlert" in line or "ealert" in line:
                    for _, _, t in gms_notifications():
                        if RE_QUAKE.search(t):
                            event = build_notification_event(t)
                            process_candidate(event, last)

            proc.wait()
            log("Logcat stream exited; reconnecting...")
            time.sleep(2)
        except Exception as e:
            log(f"Stream error: {e}")
            time.sleep(3)


def main():
    if "--drill" in sys.argv or "--simulacro" in sys.argv:
        dispatch({"source": "Google Android", "level": "drill", "nivel": "simulacro", "detected": time.time(), "text": "End-to-end drill test"})
    elif "--once" in sys.argv or "--una-vez" in sys.argv:
        run_once()
    else:
        listen_realtime()


if __name__ == "__main__":
    main()

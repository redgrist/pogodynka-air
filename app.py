import os
import time
import threading
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any

from flask import Flask, render_template_string

# ====== KONFIG ======
REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "5"))

# Progi do "czerwonego" kafelka
PM25_LIMIT = float(os.getenv("PM25_LIMIT", "25.0"))
PM10_LIMIT = float(os.getenv("PM10_LIMIT", "50.0"))

# Plantower
PMS_PORT = os.getenv("PMS_PORT", "/dev/serial0")
PMS_BAUD = int(os.getenv("PMS_BAUD", "9600"))

# BMP280
BMP_ENABLED = os.getenv("BMP_ENABLED", "1") == "1"
BMP_I2C_BUS = int(os.getenv("BMP_I2C_BUS", "1"))  # /dev/i2c-1 domyślnie
BMP_ADDRS = [int(x, 16) for x in os.getenv("BMP_ADDRS", "0x76,0x77").split(",")]

# ====== DANE WSPÓLNE (thread-safe) ======
lock = threading.Lock()

def now_ts() -> float:
    return time.time()

@dataclass
class SensorState:
    status: str = "INIT"          # OK / WARN / ERR / INIT
    last_ok_ts: Optional[float] = None
    last_try_ts: Optional[float] = None
    error: str = ""
    data: Dict[str, Any] = None

    def __post_init__(self):
        if self.data is None:
            self.data = {}

env_state = SensorState()
aq_state  = SensorState()

# ====== HTML (bez plików .html) ======
HTML = r"""
<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="{{ refresh }}" />
  <title>Klimat Rumia</title>
  <style>
    body { font-family: system-ui, Arial; margin: 20px; }
    .grid { display: grid; grid-template-columns: 1fr; gap: 14px; max-width: 1100px; }
    .tile { border-radius: 14px; padding: 14px; border: 1px solid #ddd; background: #fafafa; }
    .tile h2 { margin: 0 0 8px 0; font-size: 16px; }
    .row { display:flex; justify-content:space-between; padding: 6px 0; border-bottom: 1px dashed #e5e5e5; }
    .row:last-child { border-bottom: none; }
    .muted { color:#666; font-size: 12px; margin-top: 8px; }
    .ok  { background: #e9ffe9; border-color: #9bff9b; }
    .bad { background: #ffe5e5; border-color: #ff9b9b; }
    .warn { background: #fff6da; border-color: #ffd36a; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; }
  </style>
</head>
<body>
  <div class="grid">

    <div class="tile {{ env_class }}">
      <h2>Atmospheric environment (BMP280)</h2>

      <div class="row"><div>Temperature</div><div><b>{{ env.temp_c }}</b> °C</div></div>
      <div class="row"><div>Pressure</div><div><b>{{ env.press_hpa }}</b> hPa</div></div>

      <div class="muted">
        Status: <b>{{ env_status }}</b>
        • Ostatni odczyt: {{ env_age }} s temu
        {% if env_err %} • Błąd: <span class="mono">{{ env_err }}</span>{% endif %}
      </div>
    </div>

    <div class="tile {{ aq_class }}">
      <h2>Air quality (Plantower PMS)</h2>

      <div class="row"><div>PM1.0</div><div><b>{{ aq.pm1 }}</b> µg/m³</div></div>
      <div class="row"><div>PM2.5</div><div><b>{{ aq.pm25 }}</b> µg/m³</div></div>
      <div class="row"><div>PM10</div><div><b>{{ aq.pm10 }}</b> µg/m³</div></div>

      <div class="muted">
        Czerwony = przekroczony próg (PM2.5 &gt; {{ pm25_limit }} lub PM10 &gt; {{ pm10_limit }}).
        <br/>
        Status: <b>{{ aq_status }}</b>
        • Ostatni odczyt: {{ aq_age }} s temu
        {% if aq_err %} • Błąd: <span class="mono">{{ aq_err }}</span>{% endif %}
      </div>
    </div>

  </div>
</body>
</html>
"""

# ====== PMS (Plantower) - poprawny parser ======
def u16(be_hi: int, be_lo: int) -> int:
    return (be_hi << 8) | be_lo

def parse_plantower_frame(frame: bytes) -> Optional[dict]:
    # Ramka 32 bajty, zaczyna się od 0x42 0x4D
    if len(frame) != 32:
        return None
    if frame[0] != 0x42 or frame[1] != 0x4D:
        return None

    # checksum: suma bajtów 0..29 (16-bit) == u16(30,31)
    cs_calc = sum(frame[0:30]) & 0xFFFF
    cs_frame = u16(frame[30], frame[31])
    if cs_calc != cs_frame:
        return None

    # Layout Plantower:
    # 0-1 header, 2-3 length,
    # 4-5 PM1.0 CF=1
    # 6-7 PM2.5 CF=1
    # 8-9 PM10  CF=1
    # 10-11 PM1.0 ATM
    # 12-13 PM2.5 ATM
    # 14-15 PM10  ATM
    pm1_cf1  = u16(frame[4],  frame[5])
    pm25_cf1 = u16(frame[6],  frame[7])
    pm10_cf1 = u16(frame[8],  frame[9])

    pm1_atm  = u16(frame[10], frame[11])
    pm25_atm = u16(frame[12], frame[13])
    pm10_atm = u16(frame[14], frame[15])

    return {
        "cf1": {"pm1": pm1_cf1, "pm25": pm25_cf1, "pm10": pm10_cf1},
        "atm": {"pm1": pm1_atm, "pm25": pm25_atm, "pm10": pm10_atm},
    }

def pms_worker():
    import serial

    buf = bytearray()

    while True:
        try:
            with serial.Serial(PMS_PORT, PMS_BAUD, timeout=2) as ser:
                while True:
                    with lock:
                        aq_state.last_try_ts = now_ts()

                    chunk = ser.read(128)
                    if chunk:
                        buf += chunk

                    # szukaj ramek
                    while True:
                        i = buf.find(b"\x42\x4D")
                        if i < 0:
                            # zostaw max 1 bajt (żeby nie rosnąć w nieskończoność)
                            if len(buf) > 1:
                                buf = buf[-1:]
                            break

                        if len(buf) < i + 32:
                            # nie ma pełnej ramki
                            buf = buf[i:]
                            break

                        frame = bytes(buf[i:i+32])
                        buf = buf[i+32:]

                        parsed = parse_plantower_frame(frame)
                        if not parsed:
                            continue

                        # Używamy ATM do kafelka (bardziej "środowiskowe")
                        pm = parsed["atm"]

                        # Prosta sanity-check (odfiltruje śmieci po zakłóceniach):
                        if pm["pm25"] > 5000 or pm["pm10"] > 5000 or pm["pm1"] > 5000:
                            continue

                        with lock:
                            aq_state.data = {
                                "pm1": pm["pm1"],
                                "pm25": pm["pm25"],
                                "pm10": pm["pm10"],
                            }
                            aq_state.status = "OK"
                            aq_state.last_ok_ts = now_ts()
                            aq_state.error = ""

                    time.sleep(0.1)

        except Exception as e:
            with lock:
                aq_state.status = "ERR" if aq_state.last_ok_ts is None else "WARN"
                aq_state.error = str(e)
            time.sleep(2)


# ====== BMP280 worker (I2C) ======
def bmp_worker():
    # Uwaga: Blinka/busio w kontenerze bywa kapryśne.
    # Tutaj robimy "best effort": próbuj różne adresy.
    import board
    import busio
    import adafruit_bmp280

    i2c = None
    bmp = None

    while True:
        try:
            with lock:
                env_state.last_try_ts = now_ts()

            if i2c is None:
                # board.SCL/SDA -> na RPi powinno trafić w /dev/i2c-1
                i2c = busio.I2C(board.SCL, board.SDA)

            if bmp is None:
                last_err = None
                for addr in BMP_ADDRS:
                    try:
                        bmp = adafruit_bmp280.Adafruit_BMP280_I2C(i2c, address=addr)
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                if bmp is None and last_err:
                    raise last_err

            # odczyt
            temp_c = float(bmp.temperature)
            press_hpa = float(bmp.pressure)

            with lock:
                env_state.data = {"temp_c": round(temp_c, 2), "press_hpa": round(press_hpa, 2)}
                env_state.status = "OK"
                env_state.last_ok_ts = now_ts()
                env_state.error = ""

            time.sleep(2)

        except Exception as e:
            with lock:
                env_state.status = "ERR" if env_state.last_ok_ts is None else "WARN"
                env_state.error = str(e)
            time.sleep(2)


# ====== Flask ======
app = Flask(__name__)

def age(ts: Optional[float]) -> Optional[int]:
    if ts is None:
        return None
    return int(now_ts() - ts)

@app.route("/")
def home():
    with lock:
        env = env_state.data.copy() if env_state.data else {}
        aq = aq_state.data.copy() if aq_state.data else {}

        env_status = env_state.status
        aq_status = aq_state.status

        env_age = age(env_state.last_ok_ts)
        aq_age = age(aq_state.last_ok_ts)

        env_err = env_state.error
        aq_err = aq_state.error

    # Uzupełnij “—” gdy brak, zamiast “?”
    env_view = {
        "temp_c": env.get("temp_c", "—"),
        "press_hpa": env.get("press_hpa", "—"),
    }
    aq_view = {
        "pm1": aq.get("pm1", "—"),
        "pm25": aq.get("pm25", "—"),
        "pm10": aq.get("pm10", "—"),
    }

    # Klasy kafelków
    env_class = "ok" if env_status == "OK" else ("warn" if env_status == "WARN" else "")
    aq_exceeded = False
    try:
        if aq.get("pm25") is not None and float(aq["pm25"]) > PM25_LIMIT:
            aq_exceeded = True
        if aq.get("pm10") is not None and float(aq["pm10"]) > PM10_LIMIT:
            aq_exceeded = True
    except Exception:
        pass

    aq_class = "bad" if aq_exceeded else ("ok" if aq_status == "OK" else ("warn" if aq_status == "WARN" else ""))

    return render_template_string(
        HTML,
        refresh=REFRESH_SECONDS,
        env=env_view,
        aq=aq_view,
        env_status=env_status,
        aq_status=aq_status,
        env_age=(env_age if env_age is not None else "—"),
        aq_age=(aq_age if aq_age is not None else "—"),
        env_err=env_err,
        aq_err=aq_err,
        env_class=env_class,
        aq_class=aq_class,
        pm25_limit=PM25_LIMIT,
        pm10_limit=PM10_LIMIT,
    )

def start_threads():
    t1 = threading.Thread(target=pms_worker, daemon=True, name="pms_worker")
    t1.start()

    if BMP_ENABLED:
        t2 = threading.Thread(target=bmp_worker, daemon=True, name="bmp_worker")
        t2.start()

start_threads()

if __name__ == "__main__":
    # w dockerze: port 5000
    app.run(host="0.0.0.0", port=5000, threaded=True)


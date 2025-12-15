import os
import time
import struct
import serial

from flask import Flask, render_template_string

# BMP280 (atmospheric environment)
import board
import busio
import adafruit_bmp280

SERIAL_PORT = os.getenv("PMS_PORT", "/dev/serial0")
SERIAL_BAUD = int(os.getenv("PMS_BAUD", "9600"))

# progi alarmowe (możesz zmienić)
PM25_LIMIT = float(os.getenv("PM25_LIMIT", "25"))   # µg/m3
PM10_LIMIT = float(os.getenv("PM10_LIMIT", "50"))   # µg/m3

app = Flask(__name__)

HTML = """
<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="5">
  <title>Pogoda i jakosc powietrza</title>
  <style>
    body { font-family: system-ui, Arial; margin: 20px; }
    .grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
}
    .tile { border-radius: 14px; padding: 14px; border: 1px solid #ddd; background: #fafafa; }
    .tile h2 { margin: 0 0 8px 0; font-size: 16px; }
    .row { display:flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px dashed #e5e5e5; }
    .row:last-child { border-bottom: none; }
    .bad { background: #ffe5e5; border-color: #ff9b9b; }
    .ok  { background: #e9ffe9; border-color: #9bff9b; }
    .muted { color:#666; font-size: 12px; margin-top: 8px; }
  </style>
</head>
<body>
  <div class="grid">
    <div class="tile">
      <h2>Atmospheric environment (BMP280)</h2>
      <div class="row"><div>Temperature</div><div><b>{{ env.temp_c }} °C</b></div></div>
      <div class="row"><div>Pressure</div><div><b>{{ env.press_hpa }} hPa</b></div></div>
      <div class="muted">Odśwież stronę, aby zaktualizować.</div>
    </div>

    <div class="tile {{ aq_class }}">
      <h2>Air quality (Plantower PMS)</h2>
      <div class="row"><div>PM1.0</div><div><b>{{ aq.pm1 }} µg/m³</b></div></div>
      <div class="row"><div>PM2.5</div><div><b>{{ aq.pm25 }} µg/m³</b></div></div>
      <div class="row"><div>PM10</div><div><b>{{ aq.pm10 }} µg/m³</b></div></div>
      <div class="muted">Czerwony = przekroczony próg (PM2.5>{{ pm25_limit }} lub PM10>{{ pm10_limit }}).</div>
    </div>
  </div>
</body>
</html>
"""

def read_bmp280():
    i2c = busio.I2C(board.SCL, board.SDA)
    bmp = adafruit_bmp280.Adafruit_BMP280_I2C(i2c)
    # opcjonalnie: bmp.sea_level_pressure = 1013.25
    temp_c = round(float(bmp.temperature), 2)
    press_hpa = round(float(bmp.pressure), 2)
    return {"temp_c": temp_c, "press_hpa": press_hpa}

def read_pms_once(port=SERIAL_PORT, baud=SERIAL_BAUD, timeout=3):
    """
    Czyta JEDNĄ poprawną ramkę PMS (32 bajty zaczynające się od 0x42 0x4D).
    Zwraca PM1/PM2.5/PM10 w µg/m3 (tzw. CF=1/standard).
    """
    ser = serial.Serial(port, baudrate=baud, timeout=timeout)
    try:
        start = time.time()
        while time.time() - start < timeout:
            b = ser.read(1)
            if b != b"\x42":
                continue
            b2 = ser.read(1)
            if b2 != b"\x4D":
                continue

            rest = ser.read(30)
            if len(rest) != 30:
                continue

            frame = b"\x42\x4D" + rest
            # PM values (standard particles, CF=1): bytes 10..15
            pm1  = frame[10] * 256 + frame[11]
            pm25 = frame[12] * 256 + frame[13]
            pm10 = frame[14] * 256 + frame[15]
            return {"pm1": pm1, "pm25": pm25, "pm10": pm10}

        raise RuntimeError("Nie udało się odczytać poprawnej ramki PMS w czasie timeout.")
    finally:
        ser.close()

@app.get("/")
def home():
    env = read_bmp280()

    try:
        aq = read_pms_once()
    except Exception:
        # jak PMS nie działa chwilowo, nie wywalaj strony
        aq = {"pm1": "?", "pm25": "?", "pm10": "?"}

    # kolor kafelka
    aq_class = "ok"
    if aq["pm25"] != "?" and aq["pm10"] != "?":
        if (aq["pm25"] > PM25_LIMIT) or (aq["pm10"] > PM10_LIMIT):
            aq_class = "bad"

    return render_template_string(
        HTML,
        env=env,
        aq=aq,
        aq_class=aq_class,
        pm25_limit=PM25_LIMIT,
        pm10_limit=PM10_LIMIT
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)


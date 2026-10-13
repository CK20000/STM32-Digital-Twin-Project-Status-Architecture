"""
pyqt6_control.py — control panel for the digital twin.

Reads channels.json, renders one slider per analog channel, sends
INJECT:<symbol>:<raw> messages over ZMQ to renode_reader.py.

Also subscribes to OBS on 5557 to display the live value the firmware
is actually publishing (in case the write didn't land or firmware
modified it).
"""
import json
import sys
import threading

import zmq
from PyQt6.QtCore import Qt, pyqtSignal, QObject
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSlider, QCheckBox, QGroupBox, QScrollArea, QFrame
)

CHANNELS_FILE   = "channels.json"
ZMQ_INJECT_PORT = 5558
ZMQ_OBS_PORT    = 5557


# ---------- channel model ----------

class Channel:
    def __init__(self, cfg, adc):
        self.id     = cfg["id"]
        self.symbol = cfg["symbol"]
        self.obs    = cfg["obs"]
        self.kind   = cfg["kind"]
        self.vmin   = cfg.get("vmin", 0.0)
        self.vmax   = cfg.get("vmax", 10.0)
        self.adc    = adc
        self.last_raw = 0
        self.last_obs_raw = 0

    def volts_to_raw(self, v):
        v = max(self.vmin, min(self.vmax, v))
        ratio = self.adc["divider_ratio"]
        vref  = self.adc["vref"]
        amax  = self.adc["adc_max"]
        return int(round(v * ratio * amax / vref)) & 0xFFFF

    def raw_to_volts(self, raw):
        return raw * self.adc["vref"] / self.adc["adc_max"] / self.adc["divider_ratio"]


# ---------- ZMQ bridge ----------

class ZmqBridge(QObject):
    obs_updated = pyqtSignal(str, int)     # observable name, raw value

    def __init__(self):
        super().__init__()
        self.ctx = zmq.Context()

        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.connect(f"tcp://127.0.0.1:{ZMQ_INJECT_PORT}")

        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.connect(f"tcp://127.0.0.1:{ZMQ_OBS_PORT}")
        self.sub.setsockopt_string(zmq.SUBSCRIBE, "OBS")

        self._stop = False
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def send_inject(self, symbol, raw_value):
        self.pub.send_string(f"INJECT:{symbol}:{int(raw_value) & 0xFFFF}")

    def _listen(self):
        while not self._stop:
            try:
                msg = self.sub.recv_string(zmq.NOBLOCK)
            except zmq.Again:
                import time as _t
                _t.sleep(0.05)
                continue
            try:
                # OBS:<name>:<hex>
                _, name, hex_str = msg.split(":", 2)
                raw = bytes.fromhex(hex_str)
                if len(raw) == 2:
                    val = raw[0] | (raw[1] << 8)   # little-endian uint16
                    self.obs_updated.emit(name, val)
            except Exception:
                pass

    def close(self):
        self._stop = True
        try: self.pub.close()
        except Exception: pass
        try: self.sub.close()
        except Exception: pass
        self.ctx.term()


# ---------- one row per channel ----------

class ChannelRow(QWidget):
    def __init__(self, ch: Channel, bridge: ZmqBridge):
        super().__init__()
        self.ch = ch
        self.bridge = bridge

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)

        self.name_lbl = QLabel(ch.id)
        self.name_lbl.setFixedWidth(70)
        self.name_lbl.setStyleSheet("font-weight: bold;")
        lay.addWidget(self.name_lbl)

        self.value_lbl = QLabel("0.00 V")
        self.value_lbl.setFixedWidth(90)
        self.value_lbl.setStyleSheet("font-family: monospace;")
        lay.addWidget(self.value_lbl)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setValue(0)
        # We do NOT send on valueChanged. We just update the label.
        # A 20 Hz QTimer (installed by the main window) sends the value.
        self.slider.valueChanged.connect(self._on_slider_moved)
        lay.addWidget(self.slider, stretch=1)
        self._pending_volts = 0.0

        self.obs_lbl = QLabel("obs: —")
        self.obs_lbl.setFixedWidth(140)
        self.obs_lbl.setStyleSheet("font-family: monospace; color: #888;")
        lay.addWidget(self.obs_lbl)

        self._send(0)

    def _on_slider_moved(self, pos):
        v = self.ch.vmin + (self.ch.vmax - self.ch.vmin) * (pos / 1000.0)
        self._pending_volts = v
        self.value_lbl.setText(f"{v:5.2f} V")

    def flush_slider(self):
        """Called by the main window's 20 Hz timer. Always sends the
        current value; the reader coalesces duplicates, so this is
        safe and immune to ZMQ slow-join drops."""
        raw = self.ch.volts_to_raw(self._pending_volts)
        self._send(raw)

    def _send(self, raw):
        self.ch.last_raw = raw
        self.bridge.send_inject(self.ch.symbol, raw)

    def on_obs(self, raw):
        self.ch.last_obs_raw = raw
        v = self.ch.raw_to_volts(raw)
        self.obs_lbl.setText(f"obs: {v:5.2f} V")


# ---------- main window ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Digital Twin — Input Control")
        self.resize(720, 520)

        with open(CHANNELS_FILE) as f:
            cfg = json.load(f)
        self.adc = cfg["adc"]

        self.bridge = ZmqBridge()
        self.bridge.obs_updated.connect(self._on_obs)

        self.channels = []
        self.rows = {}

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        # --- Analog group ---
        analog_box = QGroupBox("Analog inputs (0–10 V)")
        analog_layout = QVBoxLayout(analog_box)
        for c in cfg["channels"]:
            if c["kind"] != "analog":
                continue
            ch = Channel(c, self.adc)
            row = ChannelRow(ch, self.bridge)
            self.channels.append(ch)
            self.rows[ch.obs] = row
            analog_layout.addWidget(row)
        outer.addWidget(analog_box)

        # --- Digital group (populates only if channels.json has digital entries) ---
        digital = [c for c in cfg["channels"] if c["kind"] == "digital"]
        if digital:
            dbox = QGroupBox("Digital inputs")
            dlay = QVBoxLayout(dbox)
            for c in digital:
                cb = QCheckBox(c["id"])
                von = c.get("von_mv", 5000)
                cb.stateChanged.connect(
                    lambda state, sym=c["symbol"], von=von:
                    self.bridge.send_inject(sym, von if state else 0)
                )
                dlay.addWidget(cb)
            outer.addWidget(dbox)

        outer.addStretch(1)

        # Status bar hint
        self.statusBar().showMessage(
            f"Publishing on ZMQ {ZMQ_INJECT_PORT} • "
            f"Reading OBS on {ZMQ_OBS_PORT} • "
            f"Flush 20 Hz"
        )

        # 20 Hz flush timer — sends slider values at a bounded rate,
        # no matter how fast the user drags.
        from PyQt6.QtCore import QTimer
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(50)  # 20 Hz
        self._flush_timer.timeout.connect(self._flush_sliders)
        self._flush_timer.start()

    def _flush_sliders(self):
        for row in self.rows.values():
            row.flush_slider()

    def _on_obs(self, name, raw):
        row = self.rows.get(name)
        if row is not None:
            row.on_obs(raw)

    def closeEvent(self, ev):
        self.bridge.close()
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
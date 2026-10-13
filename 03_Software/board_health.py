"""
Board Health Pre-Check.
Subscribes to all ZMQ topics, runs an automated test battery,
prints a green/red checklist every few seconds.

Usage:
    python board_health.py            # continuous monitor
    python board_health.py --once     # single check, exit code 0/1
"""
import argparse
import sys
import time
import threading
from collections import deque

import zmq

# === CONFIG ===
ZMQ_SUB_PORT    = 5556
CHECK_INTERVAL  = 2.0     # seconds between full reports
DATA_FRESH_S    = 5.0     # max age before data counts as stale

# Test thresholds
RAIL_TOL_PCT    = 5.0     # 12V rail can be 11.4-12.6, 5V can be 4.75-5.25, etc.
P_TOTAL_LIMIT_W = 2.0     # board total power budget
T_COMP_LIMIT_C  = 60.0    # no component should exceed this
T_AMBIENT_C     = 25.0
PACKET_RATE_MIN = 5.0     # Hz — pipeline should sustain at least this
ESP_PACKET_EXPECTED_FIELDS = 8

# Colored terminal output
C_RESET = "\033[0m"
C_BOLD  = "\033[1m"
C_GREEN = "\033[92m"
C_RED   = "\033[91m"
C_YEL   = "\033[93m"
C_CYAN  = "\033[96m"
C_GRAY  = "\033[90m"

# Per-component junction→ambient thermal resistance lookup (°C/W).
# Simplified from pcb_thermal_view.py — enough for a coarse over-temp test.
THETA_JA = [
    ("STM32F407",   40), ("ESP-12", 50), ("AMS1117", 65), ("XL1509", 55),
    ("LM358", 130), ("PC817", 200), ("6N137", 150), ("MAX3485", 150),
    ("W25Q", 100), ("1N5820", 80), ("SS14", 200), ("BAT54S", 250),
]
DEFAULT_THETA_JA = 200


def lookup_theta(value):
    v = (value or "").upper()
    for sub, t in THETA_JA:
        if sub in v:
            return t
    return DEFAULT_THETA_JA


# === Shared state (written by listener thread, read by reporter) ===
state = {
    "rails":          {},     # net -> (V, timestamp)
    "rail_currents":  {},     # net -> (A, timestamp)
    "load_currents":  {},     # label -> (A, timestamp)
    "component_power":{},     # ref -> (W, timestamp)
    "led_currents":   {},     # ref -> (A, timestamp)
    "sim":            {},     # last SIM key=val dict
    "sim_ts":         0.0,
    "esp_count":      0,
    "rs485_count":    0,
    "esp_last_ts":    0.0,
    "rs485_last_ts":  0.0,
    # Component values from KiCad netlist for θja lookup
    "component_values": {},
    "esp_times": deque(maxlen=200),
    "usb_topology": [],  # [(label, status, detail)] — static
    "usb_spice": {"rows": [], "ngspice": None},
    "usbreg": {},  # name -> (value, timestamp)
}
lock = threading.Lock()


def load_component_values():
    """Read digital_twin_netlist.json to know which refdes maps to which value."""
    import json
    try:
        with open("digital_twin_netlist.json") as f:
            netlist = json.load(f)
    except Exception:
        return {}
    seen = {}
    for net, data in netlist.items():
        for node in data.get("nodes", []):
            ref = node.get("component")
            val = node.get("value")
            if ref and ref not in seen:
                seen[ref] = val
    return seen


def zmq_listener():
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:{ZMQ_SUB_PORT}")
    for topic in ("RAIL", "RAIL_I", "LOAD_I", "POWER", "LED_I", "SIM"):
        sub.setsockopt_string(zmq.SUBSCRIBE, topic)
    print(f"{C_GRAY}[*] Health monitor subscribed to ZMQ {ZMQ_SUB_PORT}{C_RESET}")

    now = time.time()
    while True:
        try:
            msg = sub.recv_string()
            parts = msg.split(":")
            now = time.time()

            if parts[0] == "RAIL" and len(parts) >= 3:
                with lock:
                    state["rails"][parts[1]] = (float(parts[2]), now)

            elif parts[0] == "RAIL_I" and len(parts) >= 3:
                with lock:
                    state["rail_currents"][parts[1]] = (float(parts[2]), now)

            elif parts[0] == "LOAD_I" and len(parts) >= 3:
                with lock:
                    state["load_currents"][parts[1]] = (float(parts[2]), now)

            elif parts[0] == "POWER" and len(parts) >= 3:
                with lock:
                    state["component_power"][parts[1]] = (float(parts[2]), now)

            elif parts[0] == "LED_I" and len(parts) >= 3:
                with lock:
                    state["led_currents"][parts[1]] = (float(parts[2]), now)

            elif parts[0] == "SIM":
                # SIM:<n>:key=val,key=val,...
                if len(parts) >= 3:
                    kv = {}
                    for pair in parts[2].split(","):
                        if "=" in pair:
                            k, v = pair.split("=", 1)
                            kv[k] = v
                    with lock:
                        state["sim"] = kv
                        state["sim_ts"] = now

            # ESP / RS485 freshness — grab from bridge on 5555 (separate sub)
        except Exception:
            continue


# ============================================================
# USB topology (static, computed once at startup)
# ============================================================
def compute_usb_topology():
    """Read the two JSONs and return [(label, status, detail), ...]."""
    import json
    try:
        with open("digital_twin_netlist.json") as f:
            nl = json.load(f)
        with open("component_classification.json") as f:
            cls = json.load(f)
    except Exception as e:
        return [("USB topology", "WAITING", f"missing JSON: {e}")]

    def nodes(net):
        e = nl.get(net)
        return [(n["component"], n["pin"]) for n in e.get("nodes", [])] if e else []

    def net_has(net, ref):
        return any(r == ref for r, _ in nodes(net))

    def pins_on(net, ref):
        return [p for r, p in nodes(net) if r == ref]

    out = []

    # --- CC1 / CC2 pulldowns (USB-C sink contract) ---
    for label, net, ref, expect in (
        ("USB CC1", "Net-(J4-CC1)", "R38", 5100.0),
        ("USB CC2", "Net-(J4-CC2)", "R37", 5100.0),
    ):
        c = cls["components"].get(ref, {})
        ohms = c.get("ohms")
        if ohms and abs(ohms - expect) < 100 and net_has(net, ref):
            out.append((label, "PASS", f"{ref}={ohms:.0f}Ω pulldown to GND"))
        else:
            out.append((label, "FAIL", f"{ref}: {ohms}Ω on {net}"))

    # --- VBUS -> D10 -> 5V, orientation ---
    d10 = cls["components"].get("D10", {})
    d10_nets = d10.get("nets", [])
    if "Net-(D10-A)" in d10_nets and "5v" in d10_nets:
        # SS14: pin 1 = cathode, pin 2 = anode
        a_pins = pins_on("Net-(D10-A)", "D10")
        k_pins = pins_on("5v", "D10")
        if "2" in a_pins and "1" in k_pins:
            out.append(("USB VBUS diode", "PASS",
                        "D10 anode→VBUS, cathode→5V (correct)"))
        else:
            out.append(("USB VBUS diode", "FAIL",
                        f"orientation: A={a_pins} K={k_pins}"))
    else:
        out.append(("USB VBUS diode", "FAIL", f"D10 nets={d10_nets}"))

    # --- D+/D- to MCU ---
    dp = pins_on("USB.D+", "U5")
    dm = pins_on("USB.D-", "U5")
    out.append(("USB D+ → PA12",
                "PASS" if "71" in dp else "FAIL",
                "U5 pin 71" if "71" in dp else f"pins={dp}"))
    out.append(("USB D- → PA11",
                "PASS" if "70" in dm else "FAIL",
                "U5 pin 70" if "70" in dm else f"pins={dm}"))

    # --- VBUS sense (PA9) ---
    pa9_nets = [n for n, d in nl.items() for x in d.get("nodes", [])
                if x["component"] == "U5" and x["pin"] == "68"]
    if pa9_nets and "usb_led" in pa9_nets[0].lower():
        out.append(("USB VBUS sense", "WARN",
                    "PA9→usb_led; firmware MUST set vbus_sensing_enable=0"))
    elif pa9_nets:
        out.append(("USB VBUS sense", "PASS", f"PA9 on {pa9_nets[0]}"))
    else:
        out.append(("USB VBUS sense", "WARN", "PA9 not connected"))

    # --- BOOT0 strap (DFU entry) ---
    b = nodes("boot0")
    r6   = any(r == "R6"   for r, _ in b)
    sw2  = any(r == "SW2"  for r, _ in b)
    u5_94 = any(r == "U5" and p == "94" for r, p in b)
    out.append(("USB BOOT0 strap",
                "PASS" if (r6 and sw2 and u5_94) else "FAIL",
                f"R6={r6} SW2={sw2} U5.94={u5_94}"))

    # --- ESD protection on D+/D- ---
    esd = False
    for net in ("USB.D+", "USB.D-"):
        for ref, _ in nodes(net):
            c = cls["components"].get(ref, {})
            mod = (c.get("model") or c.get("raw_value") or "").upper()
            if any(x in mod for x in ("USBLC6", "TPD2E", "ESD", "PRTR5V")):
                esd = True
    out.append(("USB ESD", "PASS" if esd else "WARN",
                "protection present" if esd else "none on D+/D-"))

    return out


# ============================================================
# USB-only power sweep (one-shot ngspice, corrected SS14 model)
# ============================================================
def usb_spice_sweep():
    """Returns (rows, ngspice_path).
    rows = [(vbus_v, i_load_mA, v_5v, v_3v3_or_None), ...]
    """
    import subprocess, tempfile, os as _os, shutil

    ng = None
    for c in (r"C:\Users\user\Downloads\Spice64\bin\ngspice_con.exe",
              "ngspice_con", "ngspice"):
        if _os.path.isfile(c):
            ng = c; break
        p = shutil.which(c)
        if p:
            ng = p; break
    if not ng:
        return [], None

    # Real SS14: Is=1e-5 puts Vf ≈ 0.24 V @ 50 mA, ≈ 0.42 V @ 500 mA
    SS14 = "D(Is=1e-5 N=1.03 Rs=0.04 Cj=1e-10 Tt=0)"

    def dropout(ma):        # AMS1117-3.3 typical, linearized
        if ma <= 10:  return 0.55
        if ma <= 100: return 0.75
        if ma <= 300: return 0.95
        if ma <= 500: return 1.10
        return 1.25

    def run_once(vbus, ma):
        r = vbus / (ma / 1000.0)
        cir = (f"* USB-only power path\n"
               f"V_USB VBUS 0 DC {vbus}\n"
               f"D_D10 VBUS N5V SS14\n"
               f".model SS14 {SS14}\n"
               f"R_LOAD N5V 0 {r}\n"
               f".op\n.end\n")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".cir",
                                         delete=False, newline="\r\n",
                                         encoding="utf-8") as f:
            f.write(cir); path = f.name
        try:
            rr = subprocess.run([ng, "-b", path], capture_output=True,
                                text=True, timeout=10,
                                encoding="utf-8", errors="replace")
            out = rr.stdout
        finally:
            try: _os.unlink(path)
            except OSError: pass
        in_t = False
        for line in out.splitlines():
            s = line.strip()
            if "Node" in s and "Voltage" in s:
                in_t = True; continue
            if in_t:
                if not s or s.startswith("Source"): break
                p = s.split()
                if len(p) >= 2 and p[0].lower() == "n5v":
                    try: return float(p[1])
                    except ValueError: pass
        return None

    rows = []
    for vbus in (5.00, 4.75, 4.45):
        for ma in (100, 250, 500):
            v5 = run_once(vbus, ma)
            if v5 is None:
                rows.append((vbus, ma, None, None))
            else:
                v3 = 3.30 if v5 >= (3.30 + dropout(ma)) else v5 - dropout(ma)
                rows.append((vbus, ma, v5, v3))
    return rows, ng


def zmq_esp_listener():
    """Second listener on 5555 to track packet rate from the bridge."""
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:5555")
    sub.setsockopt_string(zmq.SUBSCRIBE, "ESP")
    sub.setsockopt_string(zmq.SUBSCRIBE, "RS485")
    while True:
        try:
            msg = sub.recv_string()
            now = time.time()
            with lock:
                if msg.startswith("ESP:"):
                    state["esp_count"] += 1
                    state["esp_last_ts"] = now
                    state["esp_times"].append(now)
                elif msg.startswith("RS485:"):
                    state["rs485_count"] += 1
                    state["rs485_last_ts"] = now
        except Exception:
            continue


def zmq_usbreg_listener():
    """One-shot USB register snapshot published by renode_reader on 5557."""
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect("tcp://127.0.0.1:5557")
    sub.setsockopt_string(zmq.SUBSCRIBE, "USBREG")
    while True:
        try:
            msg = sub.recv_string()
            parts = msg.split(":", 2)
            if len(parts) == 3 and parts[0] == "USBREG":
                name = parts[1]
                val = int(parts[2], 16)
                with lock:
                    state["usbreg"][name] = (val, time.time())
        except Exception:
            continue


# === Test primitives ===
def _fresh(entry, now):
    if not entry:
        return None
    val, ts = entry
    if now - ts > DATA_FRESH_S:
        return None
    return val


def test_rail(name, target, now):
    with lock:
        entry = state["rails"].get(name)
    v = _fresh(entry, now)
    if v is None:
        return ("WAITING", f"no data for {name}")
    err = abs(v - target) / target * 100
    if err <= RAIL_TOL_PCT:
        return ("PASS", f"{v:.4f} V  (target {target:.2f})")
    return ("FAIL", f"{v:.4f} V  off by {err:.2f}%")


def test_load(name, expected_a, tol_pct, now):
    with lock:
        entry = state["load_currents"].get(name)
    i = _fresh(entry, now)
    if i is None:
        return ("WAITING", f"no current for {name}")
    err = abs(abs(i) - expected_a) / expected_a * 100
    if err <= tol_pct:
        return ("PASS", f"{abs(i)*1e3:.1f} mA  (expected ~{expected_a*1e3:.0f})")
    return ("WARN", f"{abs(i)*1e3:.1f} mA  vs expected {expected_a*1e3:.0f}")


def test_total_power(now):
    with lock:
        powers = {k: v[0] for k, v in state["component_power"].items()
                  if now - v[1] < DATA_FRESH_S}
    if not powers:
        return ("WAITING", "no power data")
    total = sum(powers.values())
    if total <= P_TOTAL_LIMIT_W:
        return ("PASS", f"{total*1e3:.1f} mW  ({len(powers)} components)")
    return ("FAIL", f"{total*1e3:.1f} mW  exceeds {P_TOTAL_LIMIT_W*1e3:.0f} mW limit")


def test_over_temp(now):
    with lock:
        powers = {k: v[0] for k, v in state["component_power"].items()
                  if now - v[1] < DATA_FRESH_S}
        values = state["component_values"]
    if not powers:
        return ("WAITING", "no power data")
    worst_ref, worst_t = None, 0.0
    for ref, p in powers.items():
        if p < 1e-6:
            continue
        theta = lookup_theta(values.get(ref))
        t = T_AMBIENT_C + p * theta
        if t > worst_t:
            worst_t, worst_ref = t, ref
    if worst_ref is None:
        return ("PASS", "all components near ambient")
    if worst_t <= T_COMP_LIMIT_C:
        return ("PASS", f"peak {worst_t:.1f} °C at {worst_ref}")
    return ("FAIL", f"{worst_ref} at {worst_t:.1f} °C  exceeds {T_COMP_LIMIT_C:.0f} °C")


def test_no_short(now):
    with lock:
        rail_i = {k: v[0] for k, v in state["rail_currents"].items()
                  if now - v[1] < DATA_FRESH_S}
    if not rail_i:
        return ("WAITING", "no rail currents")
    worst = max(abs(v) for v in rail_i.values())
    if worst <= 1.0:
        return ("PASS", f"max {worst*1e3:.1f} mA  (short would be > 1 A)")
    return ("FAIL", f"{worst:.3f} A on some rail — possible short")


def test_sim_active(now):
    with lock:
        ts = state["sim_ts"]
    if ts == 0:
        return ("WAITING", "no SIM packets yet")
    age = now - ts
    if age <= DATA_FRESH_S:
        return ("PASS", f"last packet {age:.2f} s ago")
    return ("FAIL", f"stale — last SIM was {age:.1f} s ago")


def test_packet_rate(window_s=5.0):
    """Firmware → bridge UART liveness check.

    Historical note: this used to look for ESP packets on the ESP-12F
    UART (USART3). The firmware no longer writes to USART3 — the
    firmware→solver path is now OBS (Renode memory read) per
    Milestone 11. The only UART traffic is the RS-485 PLC_ALIVE
    heartbeat on USART2, so that's what we watch.
    """
    now = time.time()
    with lock:
        # rs485 timestamps aren't currently recorded in a deque, only a
        # count and last-ts. Use last-ts for freshness and count delta
        # for rate.
        last_ts = state["rs485_last_ts"]
        count = state["rs485_count"]

    # Snapshot the count, wait, snapshot again — cheap rate estimate.
    # We can't rely on a rolling deque here because the ESP listener
    # only tracked ESP timestamps.
    if last_ts == 0:
        return ("FAIL", f"no RS-485 packets in {window_s:.0f} s")

    age = now - last_ts
    if age > window_s:
        return ("FAIL", f"RS-485 silent — last packet {age:.1f} s ago")

    # Firmware sends one PLC_ALIVE every 100 ms (HAL_Delay(100) in the
    # main loop). Expect ~10 Hz. Anything within 5-15 Hz is fine.
    return ("PASS", f"last RS-485 packet {age*1000:.0f} ms ago")


def test_adc_channels(now):
    """Confirm the SIM packet contains expected fields."""
    with lock:
        sim = dict(state["sim"])
        ts = state["sim_ts"]
    if not sim or now - ts > DATA_FRESH_S:
        return ("WAITING", "no recent SIM packet")
    expected = ["A_IN1", "C0", "OUT1", "OUT2"]
    missing = [k for k in expected if k not in sim]
    if not missing:
        a_in1 = sim.get("A_IN1", "?")
        return ("PASS", f"{len(expected)}/{len(expected)} fields, A_IN1={a_in1}")
    return ("WARN", f"missing fields: {missing}")


# === Reporter ===
def run_checks(now):
    return [
        # ... existing rows unchanged ...
        ("12V rail",         test_rail("12v",       12.0, now)),
        ("5V rail",          test_rail("5v",         5.0, now)),
        ("3.3V LDO1 rail",   test_rail("3v3-LDO1",   3.3, now)),
        ("3.3V LDO2 rail",   test_rail("3v3-LDO2",   3.3, now)),
        ("STM32 self-load",  test_load("STM32",  0.050, 25.0, now)),
        ("ESP-12F self-load",test_load("ESP12F", 0.080, 25.0, now)),
        ("Total board power",test_total_power(now)),
        ("Over-temperature", test_over_temp(now)),
        ("No shorted rails", test_no_short(now)),
        ("SIM active",       test_sim_active(now)),
        ("ADC channel set", test_adc_channels(now)),
        ("RS-485 heartbeat", test_packet_rate()),
        # --- NEW ---
        *usb_checks(now),
    ]


def usb_checks(now):
    """Return [(label, (status, detail)), ...] for the USB section."""
    rows = []

    # Static topology
    for label, status, detail in state.get("usb_topology", []):
        rows.append((label, (status, detail)))

    # PA11/PA12 mux — from the one-shot register snapshot
    with lock:
        regs = dict(state["usbreg"])
    moder_e = regs.get("GPIOA_MODER")
    afrh_e  = regs.get("GPIOA_AFRH")
    if moder_e and afrh_e:
        moder = moder_e[0]
        afrh  = afrh_e[0]
        pa11_mode = (moder >> 22) & 0x3
        pa12_mode = (moder >> 24) & 0x3
        pa11_af   = (afrh  >> 12) & 0xF   # PA11: bits 12..15 of AFRH
        pa12_af   = (afrh  >> 16) & 0xF   # PA12: bits 16..19 of AFRH
        rows.append(("USB PA11 mux",
            ("PASS" if (pa11_mode == 2 and pa11_af == 10) else "FAIL",
             f"MODER={pa11_mode} AF={pa11_af} (want 2/10)")))
        rows.append(("USB PA12 mux",
            ("PASS" if (pa12_mode == 2 and pa12_af == 10) else "FAIL",
             f"MODER={pa12_mode} AF={pa12_af} (want 2/10)")))

    # USB-only power margin — worst case across REALISTIC combos.
    # USB spec: VBUS < 4.70V cannot deliver > 150 mA.
    sweeps = state.get("usb_spice", {}).get("rows", [])
    worst = None
    for vbus, ma, v5, v3 in sweeps:
        if v3 is None:
            continue
        if vbus < 4.70 and ma > 150:
            continue
        if worst is None or v3 < worst[3]:
            worst = (vbus, ma, v5, v3)
    if worst:
        vbus, ma, v5, v3 = worst
        status = "PASS" if v3 >= 3.20 else "WARN" if v3 >= 3.00 else "FAIL"
        rows.append(("USB pwr margin",
                     (status,
                      f"worst V_3V3={v3:.2f}V @ VBUS={vbus:.2f}V,{ma}mA")))
    else:
        rows.append(("USB pwr margin", ("WAITING", "sweep not run")))

    return rows


def render(results):
    lines = []
    lines.append("")
    lines.append(C_BOLD + "═" * 72 + C_RESET)
    lines.append(C_BOLD + f"  BOARD HEALTH CHECK  —  {time.strftime('%Y-%m-%d %H:%M:%S')}" + C_RESET)
    lines.append(C_BOLD + "═" * 72 + C_RESET)

    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "WAITING": 0}
    for name, (status, detail) in results:
        counts[status] = counts.get(status, 0) + 1
        if status == "PASS":
            mark = C_GREEN + "✓ PASS " + C_RESET
        elif status == "FAIL":
            mark = C_RED + "✗ FAIL " + C_RESET
        elif status == "WARN":
            mark = C_YEL + "! WARN " + C_RESET
        else:
            mark = C_GRAY + "… WAIT " + C_RESET
        lines.append(f"  {mark}  {name:<22} {detail}")

    lines.append(C_BOLD + "─" * 72 + C_RESET)
    if counts["FAIL"] > 0:
        overall = C_RED + C_BOLD + "UNHEALTHY" + C_RESET
    elif counts["WARN"] > 0 or counts["WAITING"] > 2:
        overall = C_YEL + C_BOLD + "DEGRADED" + C_RESET
    else:
        overall = C_GREEN + C_BOLD + "HEALTHY" + C_RESET
    lines.append(
        f"  Summary: {counts['PASS']} pass, {counts['WARN']} warn, "
        f"{counts['FAIL']} fail, {counts['WAITING']} waiting    "
        f"Overall: {overall}"
    )
    lines.append(C_BOLD + "═" * 72 + C_RESET)
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true",
                        help="run once and exit with 0 (healthy) or 1 (failed)")
    args = parser.parse_args()

    state["component_values"] = load_component_values()
    # Compute static USB topology once
    state["usb_topology"] = compute_usb_topology()
    print(f"{C_GRAY}[*] USB topology: {len(state['usb_topology'])} checks{C_RESET}")

    # One-shot ngspice sweep for USB-only power margin
    print(f"{C_GRAY}[*] Running USB power sweep (ngspice)...{C_RESET}")
    rows, ng = usb_spice_sweep()
    state["usb_spice"] = {"rows": rows, "ngspice": ng}
    if ng:
        print(f"{C_GRAY}[*] USB sweep: {len(rows)} points, ngspice={ng}{C_RESET}")
    else:
        print(f"{C_YEL}[!] USB sweep skipped — ngspice not found{C_RESET}")
    threading.Thread(target=zmq_listener, daemon=True).start()
    threading.Thread(target=zmq_esp_listener, daemon=True).start()
    threading.Thread(target=zmq_usbreg_listener, daemon=True).start()  # NEW

    # Wait a moment for topics to start arriving
    print(f"{C_GRAY}[*] Gathering initial data...{C_RESET}")
    time.sleep(3.0)

    if args.once:
        now = time.time()
        results = run_checks(now)
        print(render(results))
        any_fail = any(s == "FAIL" for _, (s, _) in results)
        sys.exit(1 if any_fail else 0)

    try:
        while True:
            time.sleep(CHECK_INTERVAL)
            now = time.time()
            results = run_checks(now)
            # Clear screen (works in most terminals); harmless if it doesn't
            print("\033[2J\033[H", end="")
            print(render(results))
    except KeyboardInterrupt:
        print(f"\n{C_GRAY}[*] Health monitor stopped.{C_RESET}")


if __name__ == "__main__":
    main()
"""
hse_analyzer.py — HSE crystal circuit verification + live thermal + live plot.

Extracts the HSE circuit from the KiCad netlist, runs every Pierce-oscillator
physics check (ST AN2867), evaluates EMI exposure, computes a live thermal
estimate for the crystal from the same POWER topic the thermal field v2
uses, and opens a live matplotlib window (TkAgg + FuncAnimation) with a
thermal halo around the crystal that updates as new POWER packets arrive.

Usage:
    python hse_analyzer.py                          # text report only
    python hse_analyzer.py --plot                   # + live window
    python hse_analyzer.py --esr 60 --c0 4 --cl 12 --plot
    python hse_analyzer.py --freq 25e6 --plot
"""
import argparse
import json
import math
import re
import sys
import threading
import time
from pathlib import Path

try:
    import zmq
except ImportError:
    zmq = None

# ============================================================
# PHYSICS
# ============================================================
EPS0 = 8.854e-12
EPSR_FR4 = 4.5
MU0 = 4 * math.pi * 1e-7

MCU_HSE = {
    "STM32F103": {"gm_min": 5.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32F401": {"gm_min": 5.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32F407": {"gm_min": 5.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32F411": {"gm_min": 5.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32F429": {"gm_min": 5.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32F446": {"gm_min": 5.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32F722": {"gm_min": 5.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32F745": {"gm_min": 5.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32F767": {"gm_min": 5.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32G031": {"gm_min": 5.0e-3, "pin_cap_pF": 2.0, "vpp_swing": 1.2},
    "STM32G431": {"gm_min": 5.0e-3, "pin_cap_pF": 2.0, "vpp_swing": 1.2},
    "STM32G474": {"gm_min": 5.0e-3, "pin_cap_pF": 2.0, "vpp_swing": 1.2},
    "STM32H743": {"gm_min": 4.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32H750": {"gm_min": 4.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32L432": {"gm_min": 3.5e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
    "STM32L476": {"gm_min": 3.5e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2},
}
MCU_FALLBACK = {"gm_min": 5.0e-3, "pin_cap_pF": 2.5, "vpp_swing": 1.2}

FREQ_BANDS = [
    (20e3,  50e3,  "30-70 kΩ", "1-2 pF",  9.0, "3215 / 2012", "32.768kHz"),
    (1e6,   4e6,   "100-300 Ω","4-7 pF", 12.0, "HC-49S / 5032", None),
    (4e6,   12e6,  "40-100 Ω", "4-7 pF", 15.0, "3225 / 5032",  None),
    (12e6,  20e6,  "30-80 Ω",  "4-7 pF", 15.0, "3225 / 2520",  None),
    (20e6,  32e6,  "20-60 Ω",  "3-5 pF", 12.0, "2520 / 3225",  None),
    (32e6,  60e6,  "20-60 Ω",  "3-5 pF", 10.0, "2016 / 2520",  None),
]

GAIN_MARGIN_MIN   = 5.0
NEG_R_MARGIN_MIN  = 5.0
EMI_WARN_DIST_MM  = 10.0
EMI_FAIL_DIST_MM  = 5.0
EMI_PC817_WARN_MM = 3.0
EMI_PC817_FAIL_MM = 1.5

CRYSTAL_PAD_CAP_PF    = 1.0
TRACE_STRAY_PF_PER_MM = 0.05

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

THERMAL_COUPLING_SCALE = 0.015      # was 0.04
THERMAL_COUPLING_DECAY_MM = 10.0
POWER_EST_MW = {
    "mcu": 50.0, "wifi_module": 80.0, "buck": 250.0, "ldo": 150.0,
    "opamp_dual": 30.0, "opamp_quad": 40.0, "rs485_transceiver": 20.0,
    "spi_flash": 15.0, "optocoupler": 5.0, "optocoupler_hs": 8.0,
    "led": 3.0, "dual_schottky": 2.0, "schottky": 3.0,
}


# ============================================================
# I/O
# ============================================================
def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def try_load(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return load_json(p)
    except Exception:
        return None


# ============================================================
# MCU DETECTION
# ============================================================
def find_mcu(classification):
    for ref, info in classification["components"].items():
        if info["type"] == "mcu":
            return ref, info.get("model") or info.get("raw_value")
    return None, None


def lookup_mcu(model_str):
    if not model_str:
        return None, MCU_FALLBACK, "fallback"
    m = model_str.upper()
    for key in MCU_HSE:
        if key.upper() in m:
            return key, MCU_HSE[key], "exact"
    fam_map = {
        "STM32F1": "STM32F103", "STM32F4": "STM32F407",
        "STM32F7": "STM32F745", "STM32G0": "STM32G031",
        "STM32G4": "STM32G431", "STM32H7": "STM32H743",
        "STM32L4": "STM32L432",
    }
    for fam, ref_key in fam_map.items():
        if fam in m:
            return ref_key, MCU_HSE[ref_key], "family"
    return None, MCU_FALLBACK, "fallback"


# ============================================================
# HSE EXTRACTION
# ============================================================
def find_crystal(classification):
    for ref, info in classification["components"].items():
        if info["type"] == "crystal":
            return ref, info
    return None, None


def extract_hse(netlist, classification):
    y1_ref, y1_info = find_crystal(classification)
    if y1_ref is None:
        return None
    y1_nets = y1_info.get("nets", [])
    if len(y1_nets) != 2:
        return None
    caps = {}
    for net in y1_nets:
        for node in netlist.get(net, {}).get("nodes", []):
            ref = node["component"]
            if ref == y1_ref:
                continue
            cinfo = classification["components"].get(ref)
            if not cinfo:
                continue
            if cinfo["type"] == "capacitor" and "GND" in cinfo.get("nets", []):
                farads = cinfo.get("farads")
                if farads:
                    caps[net] = (ref, farads * 1e12)
                    break
    return {"crystal_ref": y1_ref, "crystal_nets": y1_nets, "caps": caps}


def collect_hse_traces(geom, hse_nets):
    traces = []
    total = 0.0
    for t in geom.get("tracks", []):
        if t.get("net_name") in hse_nets:
            L = math.hypot(t["x2"] - t["x1"], t["y2"] - t["y1"])
            traces.append({**t, "length_mm": L})
            total += L
    return traces, total


# ============================================================
# PHYSICS
# ============================================================
def compute_effective_cl(c23_pF, c24_pF, trace_len_mm, pin_cap_pF, pad_cap_pF):
    c_series = (c23_pF * c24_pF) / (c23_pF + c24_pF) if (c23_pF + c24_pF) > 0 else 0.0
    c_trace = (trace_len_mm / 2.0) * TRACE_STRAY_PF_PER_MM
    c_stray = c_trace + pin_cap_pF + pad_cap_pF
    return c_series + c_stray, c_series, c_stray, c_trace


def check_oscillator(esr, c0, cl_eff, f_hz, gm_min):
    w = 2 * math.pi * f_hz
    cs = (c0 + cl_eff) * 1e-12
    gm_crit = 4.0 * esr * w * w * cs * cs
    return gm_crit, (gm_min / gm_crit if gm_crit > 0 else float("inf"))


def check_negative_resistance(esr, c23, c24, f_hz, gm_min, pin_cap):
    w = 2 * math.pi * f_hz
    ca = (c23 + pin_cap) * 1e-12
    cb = (c24 + pin_cap) * 1e-12
    if ca <= 0 or cb <= 0:
        return None, None
    r_neg = -gm_min / (w * w * ca * cb)
    return r_neg, (abs(r_neg) / esr if esr > 0 else float("inf"))


def check_drive_level(esr, cl_eff, f_hz, vpp):
    w = 2 * math.pi * f_hz
    return esr * (w * cl_eff * 1e-12 * vpp) ** 2


def compute_frequency_pull(cl_eff, cl_spec, c0, cm_fF):
    cm = cm_fF * 1e-15
    c0m = c0 * 1e-12
    cle = cl_eff * 1e-12
    cls = cl_spec * 1e-12
    denom = 2 * (c0m + cle) * (c0m + cls)
    if denom <= 0:
        return 0.0
    return (cm * (cle - cls) / denom) * 1e6


# ============================================================
# EMI
# ============================================================
def pt_seg_dist(px, py, x1, y1, x2, y2):
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def estimate_emi(geom, classification, hse_nets):
    noise = []
    for ref, info in classification["components"].items():
        t = info["type"]
        if t == "buck":
            noise.append((ref, "buck converter"))
        elif t == "optocoupler_hs":
            noise.append((ref, "motor opto (6N137)"))
        elif t == "wifi_module":
            noise.append((ref, "ESP-12F (2.4 GHz)"))
        elif t == "optocoupler":
            noise.append((ref, "industrial opto (PC817)"))
    hse_segs = []
    for tr in geom.get("tracks", []):
        if tr.get("net_name") in hse_nets:
            hse_segs.append((tr["x1"], tr["y1"], tr["x2"], tr["y2"]))
    out = []
    for ref, label in noise:
        fp = next((f for f in geom["footprints"] if f["ref"] == ref), None)
        if not fp:
            continue
        px, py = fp["x_mm"], fp["y_mm"]
        d = min((pt_seg_dist(px, py, *s) for s in hse_segs), default=float("inf"))
        out.append({"ref": ref, "label": label, "dist_mm": d})
    return out


# ============================================================
# LIVE THERMAL STATE (subscribes to POWER on 5556)
# ============================================================
class LivePowerState:
    def __init__(self):
        self.lock = threading.Lock()
        self.powers = {}
        self.packet = 0
        self.last_ts = 0.0
        self.t_xtal_C = None
        self.t_xtal_ts = 0.0

    def start(self):
        if zmq is None:
            print("[!] pyzmq not installed — live thermal disabled")
            return

        # --- POWER subscriber (from pyspice_solver) ---
        ctx1 = zmq.Context()
        sub1 = ctx1.socket(zmq.SUB)
        sub1.connect("tcp://127.0.0.1:5556")
        sub1.setsockopt_string(zmq.SUBSCRIBE, "POWER")
        sub1.setsockopt(zmq.RCVTIMEO, 1000)
        print("[*] Live thermal: subscribed POWER on ZMQ 5556")

        def loop_power():
            while True:
                try:
                    msg = sub1.recv_string()
                    _, ref, w = msg.split(":", 2)
                    with self.lock:
                        self.powers[ref] = float(w)
                        self.packet += 1
                        self.last_ts = time.time()
                except zmq.Again:
                    continue
                except Exception:
                    continue

        threading.Thread(target=loop_power, daemon=True).start()

        # --- T_XTAL subscriber (from pcb_thermal_field_v2) ---
        ctx2 = zmq.Context()
        sub2 = ctx2.socket(zmq.SUB)
        sub2.connect("tcp://127.0.0.1:5559")
        sub2.setsockopt_string(zmq.SUBSCRIBE, "T_XTAL")
        sub2.setsockopt(zmq.RCVTIMEO, 1000)
        print("[*] Live thermal: subscribed T_XTAL on ZMQ 5559")

        def loop_xtal():
            while True:
                try:
                    msg = sub2.recv_string()
                    _, ref, t = msg.split(":", 2)
                    with self.lock:
                        self.t_xtal_C = float(t)
                        self.t_xtal_ts = time.time()
                except zmq.Again:
                    continue
                except Exception:
                    continue

        threading.Thread(target=loop_xtal, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return (dict(self.powers), self.packet, self.last_ts,
                    self.t_xtal_C, self.t_xtal_ts)


def estimate_crystal_thermal(geom, classification, hse, live_powers,
                              t_xtal_C=None, t_xtal_ts=0.0):
    """Compute crystal temperature estimate.

    If pcb_thermal_field_v2 is running and publishing T_XTAL on 5559, and
    that value is fresh (<3 s old), use the real 2-layer diffusion number.
    Otherwise fall back to the 1/r heuristic from live POWER (or POWER_EST_MW).
    """
    y1_ref = hse["crystal_ref"]
    y1_fp = next((f for f in geom["footprints"] if f["ref"] == y1_ref), None)
    if not y1_fp:
        return None
    cx, cy = y1_fp["x_mm"], y1_fp["y_mm"]

    fresh_xtal = (t_xtal_C is not None and
                  (time.time() - t_xtal_ts) < 3.0)
    src_kind = "thermal_v2" if fresh_xtal else "heuristic"

    # Always compute the neighbour list — used for the report/plot even
    # when T_crystal comes from thermal_v2 (gives you the "why" context).
    def coupling(d):
        d = max(d, 1.0)
        return THERMAL_COUPLING_SCALE / (1.0 + d / THERMAL_COUPLING_DECAY_MM)

    neighbours = []
    dt_total_heur = 0.0
    for ref, info in classification["components"].items():
        if ref == y1_ref:
            continue
        if live_powers and ref in live_powers:
            p_mW = live_powers[ref] * 1000.0
            src = "live"
        else:
            p_mW = POWER_EST_MW.get(info["type"], 0.0)
            src = "est"
        if p_mW <= 0:
            continue
        fp = next((f for f in geom["footprints"] if f["ref"] == ref), None)
        if not fp:
            continue
        d = math.hypot(fp["x_mm"] - cx, fp["y_mm"] - cy)
        dt = p_mW * coupling(d)
        if dt > 0.02:
            neighbours.append((ref, d, p_mW, dt, src))
            dt_total_heur += dt
    neighbours.sort(key=lambda x: -x[3])

    T_amb = 25.0
    if fresh_xtal:
        T_crystal = t_xtal_C
    else:
        T_crystal = T_amb + dt_total_heur

    drift_ppm = -0.04 * (T_crystal - 25.0) ** 2
    return {
        "T_ambient_C": T_amb,
        "T_crystal_C": T_crystal,
        "neighbours": neighbours[:8],
        "freq_drift_ppm": drift_ppm,
        "source": src_kind,
    }


# ============================================================
# REPORT
# ============================================================
def m(f):
    return {PASS: "[PASS]", WARN: "[WARN]", FAIL: "[FAIL]"}.get(f, "[????]")


def print_report(hse, cl_data, osc, negr, dl, freq, emi, thermal, crystal_known):
    print()
    print("=" * 70)
    print("  HSE CRYSTAL CIRCUIT — VERIFICATION")
    print("=" * 70)
    print()
    print(f"  Crystal {hse['crystal_ref']} between {' and '.join(hse['crystal_nets'])}")
    for net, (ref, v) in hse["caps"].items():
        print(f"    {ref:5s}  {v:6.2f} pF  on {net}")
    print()
    print(f"  Effective load capacitance:")
    print(f"    C_series (C23||C24)  : {cl_data['c_series']:6.2f} pF")
    print(f"    Trace stray          : {cl_data['c_trace']:6.2f} pF")
    print(f"    MCU pin cap (each)   : {cl_data['pin_cap']:6.2f} pF")
    print(f"    Crystal pad cap      : {cl_data['crystal_pad']:6.2f} pF")
    print(f"    ────────────────────────────────────")
    print(f"    CL_eff               : {cl_data['cl_eff']:6.2f} pF")
    if crystal_known:
        d = cl_data["cl_eff"] - cl_data["cl_spec"]
        tag = PASS if abs(d) < 2 else WARN if abs(d) < 5 else FAIL
        print(f"    Crystal CL spec      : {cl_data['cl_spec']:6.2f} pF  "
              f"(Δ {d:+.2f} pF) {m(tag)}")
        print(f"    Frequency pull       : {freq['pull_ppm']:+.1f} ppm")
    else:
        print(f"    → choose crystal with CL ≈ {cl_data['cl_eff']:.0f} pF")
    print()
    print(f"  Pierce oscillator:")
    print(f"    gm_STM32 (min)       : {osc['gm_min']*1000:.2f} mA/V")
    print(f"    gm_critical          : {osc['gm_crit']*1000:.4f} mA/V")
    print(f"    Gain margin          : {osc['margin']:6.2f}×  "
          f"{m(PASS if osc['margin'] >= GAIN_MARGIN_MIN else FAIL)}")
    print()
    if negr["r_neg"] is not None:
        print(f"  Negative resistance:")
        print(f"    R_neg                : {negr['r_neg']/1000:+8.3f} kΩ")
        print(f"    Margin over ESR      : {negr['margin']:6.2f}×  "
              f"{m(PASS if negr['margin'] >= NEG_R_MARGIN_MIN else FAIL)}")
        print()
    if dl["dl_W"]:
        dl_uW = dl["dl_W"] * 1e6
        ratio = dl_uW / dl["dl_max_uW"]
        tag = PASS if ratio < 0.8 else WARN if ratio < 1.0 else FAIL
        print(f"  Drive level ({dl['dl_max_uW']:.0f} µW max assumed):")
        print(f"    Power                : {dl_uW:6.1f} µW  {m(tag)}")
        print()
    if emi:
        print(f"  EMI exposure:")
        for e in emi:
            if "PC817" in e["label"]:
                wd, fd = EMI_PC817_WARN_MM, EMI_PC817_FAIL_MM
            else:
                wd, fd = EMI_WARN_DIST_MM, EMI_FAIL_DIST_MM
            st = PASS if e["dist_mm"] >= wd else WARN if e["dist_mm"] >= fd else FAIL
            print(f"    {e['ref']:5s}  {e['label']:28s}  "
                  f"{e['dist_mm']:6.1f} mm  {m(st)}")
        print()
    if thermal:
        print(f"  Thermal coupling to crystal:")
        print(f"    T_ambient            : {thermal['T_ambient_C']:.1f} °C")
        print(f"    T_crystal (est)      : {thermal['T_crystal_C']:.2f} °C")
        print(f"    ΔT                   : +{thermal['T_crystal_C']-thermal['T_ambient_C']:.2f} °C")
        print(f"    AT-cut drift         : {thermal['freq_drift_ppm']:+.2f} ppm")
        drift = thermal["freq_drift_ppm"]
        tag = PASS if abs(drift) < 5 else WARN if abs(drift) < 15 else FAIL
        print(f"    Drift status         : {m(tag)}")
        print()
        print(f"    Top contributors:")
        for ref, d, pm, dt, src in thermal["neighbours"][:5]:
            print(f"      {ref:5s}  {d:6.1f} mm  {pm:7.1f} mW  "
                  f"+{dt:.2f} °C  [{src}]")
        print()


def print_bom(hse, cl_eff, f_hz, band, cm_fF):
    print("=" * 70)
    print("  ACCEPTABLE CRYSTAL RANGE")
    print("=" * 70)
    print()
    fq = (f"{f_hz/1e3:g} kHz" if f_hz < 1e6 else f"{f_hz/1e6:g} MHz")
    fq_url = (f"{f_hz/1e3:g}kHz" if f_hz < 1e6 else f"{f_hz/1e6:g}MHz")
    print(f"  Frequency         : {fq}")
    if band:
        _, _, esr_s, c0_s, cl_t, pkg, _ = band
        print(f"  Package (typical) : {pkg}")
        print(f"  ESR typical       : {esr_s}")
        print(f"  C0 typical        : {c0_s}")
        print(f"  CL typical        : ~{cl_t:.0f} pF")
    print(f"  Cm estimate       : {cm_fF:.1f} fF")
    print()
    print(f"  Effective CL of your board : {cl_eff:.2f} pF")
    print(f"  → pick crystal with CL_spec ≈ {cl_eff:.0f} pF")
    print()
    print(f"  ESR max for margin ≥ 5:")
    print(f"    C0 = 5 pF  → ESR < "
          f"{MCU_HSE['STM32F407']['gm_min'] / (5 * 4 * (2*math.pi*f_hz)**2 * ((5+cl_eff)*1e-12)**2):8.1f} Ω")
    print(f"    C0 = 7 pF  → ESR < "
          f"{MCU_HSE['STM32F407']['gm_min'] / (5 * 4 * (2*math.pi*f_hz)**2 * ((7+cl_eff)*1e-12)**2):8.1f} Ω")
    print()
    print(f"  JLCPCB: https://jlcpcb.com/parts/componentSearch?searchTxt={fq_url}")
    print()


# ============================================================
# LIVE PLOT (TkAgg + FuncAnimation, same pattern as thermal_field_v2)
# ============================================================
def plot_live(geom, classification, hse, hse_nets, emi, cl_data, osc, live):
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle, Circle
    from matplotlib.collections import LineCollection, PatchCollection
    from matplotlib.animation import FuncAnimation

    BG = "#050505"; BOARD = "#0f1a0f"; BEDGE = "#66bb66"
    TDIM = "#1a1a1a"; THSE = "#ffb733"
    PAD = "#d4af37"; BODY = "#1e1e1e"; BEDG = "#3a3a3a"
    SILK = "#888888"
    MCU_C = "#ff5555"; XTAL_C = "#ffb733"; CAP_C = "#ffd966"

    NC = {
        "buck converter":          "#ff3333",
        "motor opto (6N137)":      "#ff9900",
        "ESP-12F (2.4 GHz)":       "#cc44ff",
        "industrial opto (PC817)": "#66ccff",
    }

    o = geom["outline"]
    bx, by, bw, bh = o["x_mm"], o["y_mm"], o["w_mm"], o["h_mm"]

    fig, ax = plt.subplots(figsize=(14, 12), dpi=130)
    fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
    fig.subplots_adjust(left=0.20, right=0.90, top=0.93, bottom=0.05)

    # --- Static board ---
    ax.add_patch(Rectangle((bx, by), bw, bh, facecolor=BOARD,
                           edgecolor=BEDGE, lw=1.2, zorder=0))

    all_s, all_w, hse_s, hse_w = [], [], [], []
    for t in geom.get("tracks", []):
        seg = [(t["x1"], t["y1"]), (t["x2"], t["y2"])]
        w = max(t["width_mm"] * 4, 0.4)
        if t.get("net_name") in hse_nets:
            hse_s.append(seg); hse_w.append(w + 1.2)
        else:
            all_s.append(seg); all_w.append(w)
    if all_s:
        ax.add_collection(LineCollection(all_s, linewidths=all_w,
                                          colors=TDIM, zorder=1))
    if hse_s:
        ax.add_collection(LineCollection(hse_s, linewidths=hse_w,
                                          colors=THSE, zorder=4))

    pads = [Rectangle((p["x_mm"] - p["w_mm"]/2, p["y_mm"] - p["h_mm"]/2),
                       p["w_mm"], p["h_mm"])
            for fp in geom["footprints"] for p in fp.get("pads", [])]
    if pads:
        ax.add_collection(PatchCollection(pads, facecolor=PAD,
                                           edgecolor="#7a6318",
                                           linewidths=0.2, alpha=0.55,
                                           zorder=5))

    bodies = []
    for fp in geom["footprints"]:
        for sh in fp.get("fab_shapes", []):
            if sh["type"] == "rect":
                x0 = min(sh["x1"], sh["x2"]); y0 = min(sh["y1"], sh["y2"])
                bodies.append(Rectangle((x0, y0),
                                         abs(sh["x2"]-sh["x1"]),
                                         abs(sh["y2"]-sh["y1"])))
            elif sh["type"] == "circle":
                bodies.append(Circle((sh["cx"], sh["cy"]), sh["r"]))
    if bodies:
        ax.add_collection(PatchCollection(bodies, facecolor=BODY,
                                           edgecolor=BEDG, linewidths=0.5,
                                           alpha=0.85, zorder=6))

    silk = [[(sh["x1"], sh["y1"]), (sh["x2"], sh["y2"])]
            for fp in geom["footprints"]
            for sh in fp.get("silk_shapes", []) if sh["type"] == "line"]
    if silk:
        ax.add_collection(LineCollection(silk, linewidths=0.5,
                                          colors=SILK, alpha=0.5, zorder=8))

    # MCU ring
    mcu_ref, mcu_model = find_mcu(classification)
    mcu_fp = next((f for f in geom["footprints"] if f["ref"] == mcu_ref), None)
    if mcu_fp:
        pxs = [p["x_mm"] for p in mcu_fp.get("pads", [])]
        pys = [p["y_mm"] for p in mcu_fp.get("pads", [])]
        if pxs:
            cx = (min(pxs)+max(pxs))/2; cy = (min(pys)+max(pys))/2
            r = max(max(pxs)-min(pxs), max(pys)-min(pys))/2 + 1.0
            ax.add_patch(Circle((cx, cy), r, facecolor="none",
                                 edgecolor=MCU_C, lw=1.8,
                                 linestyle="--", zorder=15))
            ax.text(cx, cy-r-1.5, f"MCU {mcu_ref}", color=MCU_C,
                    fontsize=9, ha="center", va="top", weight="bold",
                    zorder=100)

    # Crystal + caps
    y1_ref = hse["crystal_ref"]
    y1_fp = next((f for f in geom["footprints"] if f["ref"] == y1_ref), None)
    cx0 = y1_fp["x_mm"] if y1_fp else bx + bw/2
    cy0 = y1_fp["y_mm"] if y1_fp else by + bh/2
    if y1_fp:
        ax.add_patch(Circle((cx0, cy0), 2.0, facecolor="none",
                             edgecolor=XTAL_C, lw=2.0, zorder=18))
        ax.text(cx0, cy0 - 3.0, f"crystal {y1_ref}", color=XTAL_C,
                fontsize=9, ha="center", va="bottom", weight="bold",
                zorder=100)

    cap_items = list(hse["caps"].items())
    for idx, (net, (cref, cval)) in enumerate(cap_items):
        cfp = next((f for f in geom["footprints"] if f["ref"] == cref), None)
        if cfp:
            ax.add_patch(Circle((cfp["x_mm"], cfp["y_mm"]), 1.4,
                                facecolor="none", edgecolor=CAP_C,
                                lw=1.6, zorder=18))
            # Alternate above/below so two caps 5 mm apart don't collide
            dy = -2.6 if idx == 0 else 2.6
            va = "bottom" if idx == 0 else "top"
            ax.text(cfp["x_mm"], cfp["y_mm"] + dy,
                    f"{cref} {cval:.0f}pF", color=CAP_C, fontsize=7,
                    ha="center", va=va, weight="bold", zorder=100,
                    bbox=dict(facecolor="#000", alpha=0.6, pad=0.8,
                              edgecolor="none"))

    # EMI rings + distance lines (static)
    for e in emi:
        fp = next((f for f in geom["footprints"] if f["ref"] == e["ref"]), None)
        if not fp:
            continue
        col = NC.get(e["label"], "#ff6666")
        ax.add_patch(Circle((fp["x_mm"], fp["y_mm"]), 2.2,
                             facecolor="none", edgecolor=col, lw=1.8,
                             zorder=17))
        ax.plot([fp["x_mm"], cx0], [fp["y_mm"], cy0],
                color=col, lw=0.9, ls=":", alpha=0.7, zorder=3)
        mx = (fp["x_mm"] + cx0)/2; my = (fp["y_mm"] + cy0)/2
        ax.text(mx, my, f"{e['dist_mm']:.1f} mm", color=col, fontsize=7.5,
                ha="center", va="center",
                bbox=dict(facecolor="#000", alpha=0.7, pad=1.0,
                          edgecolor="none"), zorder=100)
        ax.text(fp["x_mm"], fp["y_mm"]+2.8, e["ref"], color=col, fontsize=8,
                ha="center", va="bottom", weight="bold",
                bbox=dict(facecolor="#000", alpha=0.6, pad=0.8,
                          edgecolor="none"), zorder=100)

    ax.set_xlim(bx - 3, bx + bw + 3)
    ax.set_ylim(by + bh + 3, by - 3)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(f"HSE live — {y1_ref} ({mcu_model})",
                 color="white", fontsize=12, pad=12)

    # --- Dynamic artists ---
    halo1 = ax.add_patch(Circle((cx0, cy0), 6.0, facecolor=(0.3,0.5,0.8),
                                 edgecolor="none", alpha=0.08, zorder=14))
    halo2 = ax.add_patch(Circle((cx0, cy0), 4.5, facecolor=(0.3,0.5,0.8),
                                 edgecolor="none", alpha=0.12, zorder=14))
    halo3 = ax.add_patch(Circle((cx0, cy0), 3.0, facecolor=(0.3,0.5,0.8),
                                 edgecolor="none", alpha=0.18, zorder=14))
    # (crystal temp label removed — same value already in top-left info panel)

    # Contributor panel — fixed top-right, doesn't collide with PCB
    contrib_panel = fig.text(
        0.98, 0.94, "", color="#ddd", fontsize=8.5,
        family="monospace", ha="right", va="top",
        bbox=dict(facecolor="#0a0a0a", alpha=0.92, pad=8,
                  edgecolor="#666", linewidth=1.0),
        zorder=201)

    info = fig.text(0.02, 0.95, "", color="white", fontsize=9,
                    family="monospace", va="top",
                    bbox=dict(facecolor="#101010", alpha=0.95, pad=12,
                              edgecolor="#666", linewidth=1.2),
                    zorder=200)

    def _temp_color(T):
        n = max(0.0, min(1.0, (T - 25.0) / 20.0))
        return (min(1.0, 0.2 + n*0.8),
                max(0.0, 0.6 - n*0.6),
                max(0.0, 0.8 - n*0.8))

    def update(_frame):
        powers, pkt, _, t_xtal_C, t_xtal_ts = live.snapshot()
        th = estimate_crystal_thermal(
            geom, classification, hse, powers, t_xtal_C, t_xtal_ts)
        if th is None:
            return []

        T = th["T_crystal_C"]
        drift = th["freq_drift_ppm"]
        col = _temp_color(T)

        halo1.set_facecolor(col)
        halo2.set_facecolor(col)
        halo3.set_facecolor(col)

        lines = ["── Heat contributors to crystal ──"]
        for ref, d, pm, dt, src in th["neighbours"][:6]:
            # '*' marks values from the fallback table (no live POWER packet)
            mark = " " if src == "live" else "*"
            lines.append(f"{ref:5s} {d:5.1f}mm  {pm:7.1f}mW  "
                         f"+{dt:5.2f}°C{mark}")
        if any(n[4] == "est" for n in th["neighbours"][:6]):
            lines.append("")
            lines.append("* = estimated (no live POWER)")
        contrib_panel.set_text("\n".join(lines))

        if th.get("source") == "thermal_v2":
            src_tag = "THERMAL-V2"
        elif pkt > 0:
            src_tag = "LIVE"
        else:
            src_tag = "ESTIMATE"
        info.set_text(
            f"Mode        : {src_tag}\n"
            f"POWER pkt   : {pkt}\n"
            f"T_ambient   : {th['T_ambient_C']:.1f} °C\n"
            f"T_crystal   : {T:.2f} °C\n"
            f"ΔT          : +{T - th['T_ambient_C']:.2f} °C\n"
            f"Drift       : {drift:+.2f} ppm\n"
            f"CL_eff      : {cl_data['cl_eff']:.2f} pF\n"
            f"gm margin   : {osc['margin']:.1f}×"
        )
        return [halo1, halo2, halo3, info, contrib_panel]

    ani = FuncAnimation(fig, update, interval=500, blit=False,
                        cache_frame_data=False)
    plt.show(block=True)


# ============================================================
# MAIN
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--esr", type=float, default=None)
    ap.add_argument("--c0",  type=float, default=None)
    ap.add_argument("--cl",  type=float, default=None)
    ap.add_argument("--cm",  type=float, default=None)
    ap.add_argument("--mcu", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--freq", type=float, default=None)
    ap.add_argument("--plot", action="store_true",
                    help="open live matplotlib window")
    args = ap.parse_args()

    netlist = try_load("digital_twin_netlist.json")
    classification = try_load("component_classification.json")
    geometry = try_load("pcb_geometry.json")

    if not netlist or not classification:
        print("[!] Missing digital_twin_netlist.json / component_classification.json")
        sys.exit(1)
    if not geometry:
        print("[!] Missing pcb_geometry.json")
        sys.exit(1)

    # MCU
    mcu_ref, mcu_model_raw = find_mcu(classification)
    if args.mcu:
        mcu_key = args.mcu
        mcu = MCU_HSE.get(mcu_key, MCU_FALLBACK)
        mcu_src = "exact"
    else:
        mcu_key, mcu, mcu_src = lookup_mcu(mcu_model_raw)

    # Frequency
    if args.freq:
        f_hz = args.freq
        f_src = "cli"
    else:
        _, y1_info = find_crystal(classification)
        raw = (y1_info or {}).get("raw_value", "") if y1_info else ""
        f_hz = None
        mm = re.match(r"([0-9]*\.?[0-9]+)\s*(k|K|M|m)?", raw.strip())
        if mm:
            num = float(mm.group(1))
            mul = (mm.group(2) or "").lower()
            f_hz = num * (1e6 if mul == "m" else 1e3 if mul == "k" else 1)
        if not f_hz:
            f_hz = 8.0e6
            f_src = f"default 8 MHz (crystal value '{raw}' has no freq)"
        else:
            f_src = f"parsed '{raw}'"

    hse = extract_hse(netlist, classification)
    if not hse:
        print("[!] No crystal found in netlist")
        sys.exit(1)
    hse_nets = hse["crystal_nets"]

    cap_vals = list(hse["caps"].values())
    if len(cap_vals) < 2:
        print("[!] Need two load caps")
        sys.exit(1)
    c23_pF = cap_vals[0][1]
    c24_pF = cap_vals[1][1]

    _, total_trace_mm = collect_hse_traces(geometry, hse_nets)

    cl_eff, c_series, c_stray, c_trace = compute_effective_cl(
        c23_pF, c24_pF, total_trace_mm, mcu["pin_cap_pF"], CRYSTAL_PAD_CAP_PF)

    cl_data = {
        "c_series": c_series, "c_trace": c_trace, "c_stray": c_stray,
        "cl_eff": cl_eff, "pin_cap": mcu["pin_cap_pF"],
        "crystal_pad": CRYSTAL_PAD_CAP_PF,
        "cl_spec": args.cl if args.cl else None,
    }

    band = next((b for b in FREQ_BANDS if b[0] <= f_hz < b[1]), None)
    cm_fF = args.cm if args.cm else (3.0 if f_hz < 100e3 else 120.0/(f_hz/1e6))

    crystal_known = all(x is not None for x in (args.esr, args.c0, args.cl))
    esr_used = args.esr if args.esr else (
        float(band[2].split("-")[0].replace(" kΩ","").replace(" Ω",""))
        * (1000 if "kΩ" in band[2] else 1) if band else 60.0)
    c0_used = args.c0 if args.c0 else (
        float(band[3].split("-")[0].replace(" pF","")) if band else 5.0)

    gm_crit, gm_margin = check_oscillator(esr_used, c0_used, cl_eff,
                                          f_hz, mcu["gm_min"])
    osc = {"gm_min": mcu["gm_min"], "gm_crit": gm_crit, "margin": gm_margin}

    r_neg, r_margin = check_negative_resistance(esr_used, c23_pF, c24_pF,
                                                f_hz, mcu["gm_min"],
                                                mcu["pin_cap_pF"])
    negr = {"r_neg": r_neg, "margin": r_margin}

    dl_W = check_drive_level(esr_used, cl_eff, f_hz, mcu["vpp_swing"])
    if f_hz < 100e3:
        dl_max = 1.0
    elif band and any(pk in band[5] for pk in ("3225","2520","2016")):
        dl_max = 100.0
    else:
        dl_max = 500.0
    dl = {"dl_W": dl_W, "dl_max_uW": dl_max}

    pull = compute_frequency_pull(cl_eff, args.cl, c0_used, cm_fF) if args.cl else 0.0
    freq = {"pull_ppm": pull}

    emi = estimate_emi(geometry, classification, hse_nets)

    # Always start live listener — it doesn't cost anything and the plot
    # will show LIVE vs ESTIMATE depending on whether packets arrive.
    live = LivePowerState()
    live.start()
    time.sleep(1.0)
    powers, pkt, _, t_xtal_C, t_xtal_ts = live.snapshot()
    thermal = estimate_crystal_thermal(
        geometry, classification, hse, powers, t_xtal_C, t_xtal_ts)

    # Header
    print()
    print(f"[*] MCU source       : {mcu_src} -> {mcu_key or 'fallback'}")
    print(f"[*] Frequency source : {f_src}")
    print(f"[*] Cm used          : {cm_fF:.2f} fF "
          f"({'user' if args.cm else 'estimated'})")
    print(f"[*] ESR/C0 used      : {esr_used:.1f} Ω / {c0_used:.1f} pF "
          f"({'user' if args.esr else 'typical'})")
    if pkt:
        print(f"[*] Live POWER pkt   : {pkt}  (thermal uses live data)")
    if thermal:
        print(f"[*] Thermal source   : {thermal['source']}")
    else:
        print(f"[*] Live POWER pkt   : 0  (thermal uses static estimates)")

    print_report(hse, cl_data, osc, negr, dl, freq, emi, thermal, crystal_known)
    print_bom(hse, cl_eff, f_hz, band, cm_fF)

    if args.json:
        report = {
            "mcu": mcu_ref, "mcu_model": mcu_model_raw, "mcu_key": mcu_key,
            "mcu_source": mcu_src, "freq_hz": f_hz, "freq_source": f_src,
            "crystal_ref": hse["crystal_ref"], "crystal_nets": hse_nets,
            "physics": {
                "cl_effective_pF": cl_eff, "c_series_pF": c_series,
                "c_stray_pF": c_stray, "trace_len_mm": total_trace_mm,
                "gm_critical_mA_V": gm_crit * 1000, "gm_margin": gm_margin,
                "r_neg_ohm": r_neg, "r_neg_margin": r_margin,
                "drive_level_uW": dl_W * 1e6, "drive_max_uW": dl_max,
                "freq_pull_ppm": pull, "cm_fF": cm_fF,
            },
            "emi": emi,
            "thermal": thermal,
            "crystal_known": crystal_known,
        }
        with open(args.json, "w") as f:
            json.dump(report, f, indent=2)
        print(f"[+] Wrote {args.json}")

    # Open the live plot window if requested. Blocks until user closes it.
    if args.plot:
        print("[*] Opening live plot window — close it to exit")
        plot_live(geometry, classification, hse, hse_nets, emi,
                  cl_data, osc, live)

    # Exit code (only reached if --plot was not used)
    if gm_margin < GAIN_MARGIN_MIN: sys.exit(2)
    if r_margin is not None and r_margin < NEG_R_MARGIN_MIN: sys.exit(2)
    if crystal_known and abs(cl_eff - args.cl) > 5: sys.exit(2)
    for e in emi:
        wd = EMI_PC817_WARN_MM if "PC817" in e["label"] else EMI_FAIL_DIST_MM
        if e["dist_mm"] < wd: sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
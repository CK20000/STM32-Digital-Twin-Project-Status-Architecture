"""
Live SPICE solver — automatic version.

Reads:
  auto_generated_with_traces.cir   (static netlist)
  source_metadata.json             (compiler output — maps every source)
  symbols.json                     (reader output — observable list)

Subscribes to:
  OBS:<name>:<hex>                 (from renode_reader.py, port 5557)

Publishes:
  SIM / RAIL / RAIL_I / POWER / CURRENT / LOAD_I  (port 5556)
"""
import zmq
import time
import struct
import subprocess
import tempfile
import re
import os
import json

# === Config ===
ZMQ_SUB_PORT = 5555       # legacy bridge (RS485 + old ESP packet)
ZMQ_PUB_PORT = 5556
READER_PORT  = 5557       # renode_reader.py

NGSPICE_EXE      = r"C:\Users\user\Downloads\Spice64\bin\ngspice_con.exe"
NETLIST_TEMPLATE = "auto_generated_with_traces.cir"
NETLIST_FALLBACK = "auto_generated.cir"
SOURCE_META      = "source_metadata.json"

# === Hardware constants ===
ADC_MAX       = 4095.0
ADC_VREF      = 3.3
DIVIDER_RATIO = 4.7 / (10.0 + 4.7)


def spice_net(net_name):
    """Same sanitization as spice_compiler.py. Used to look up ngspice
    node voltages by their original KiCad net name."""
    if net_name in ("GND", "gnd", "0"):
        return "0"
    s = re.sub(r"[^A-Za-z0-9_]", "_", net_name)
    if s[0].isdigit():
        s = "N" + s
    return s


# Load the full KiCad net list once at startup so we can publish NODE
# voltages for every net that appears in the trace geometry.
try:
    with open("digital_twin_netlist.json", "r", encoding="utf-8") as _f:
        ALL_KICAD_NETS = list(json.load(_f).keys())
except Exception:
    ALL_KICAD_NETS = []

# Transforms applied to firmware values based on SPICE source kind.
# The 'kind' field comes from the compiler via source_metadata.json.
TRANSFORMS = {
    # value is uint16 ADC raw → industrial input voltage
    "analog_input": lambda b: (struct.unpack("<H", b[:2])[0] / ADC_MAX)
                                * ADC_VREF / DIVIDER_RATIO,
    # value is uint16 DAC code → 0..3.3 V
    "dac":          lambda b: (struct.unpack("<H", b[:2])[0] / ADC_MAX) * ADC_VREF,
    # value is uint8 GPIO pin state (0 = LOW, 1 = HIGH) → voltage
    "led_drive":    lambda b: 0.0 if b[0] == 0 else 3.3,
    "digital_output": lambda b: 0.0 if b[0] == 0 else 3.3,
    # value is uint16 millivolts (5000 = 5 V) -> volts
    "digital_input":  lambda b: struct.unpack("<H", b[:2])[0] / 1000.0,
}

# Firmware observable → J3/J2 field-side KiCad net for every opto-isolated
# digital output. The viewer uses this to draw the "armed" state of an
# output even when the field side has no load attached (current = 0).
# Firmware writes 1 = GPIO HIGH = opto LED ON = output ARMED.
#               0 = GPIO LOW  = opto LED off = output IDLE.
DOUT_MAP = {
    "obs_v_e0_drv":  "/f4/IO/OE0",
    "obs_v_e1_drv":  "/f4/IO/OE1",
    "obs_v_e2_drv":  "/f4/IO/OE2",
    "obs_v_e3_drv":  "/f4/IO/OE3",
    "obs_v_e4_drv":  "/f4/IO/OE4",
    "obs_v_e5_drv":  "/f4/IO/OE5",
    "obs_v_e6_drv":  "/f4/IO/OE6",
    "obs_v_e7_drv":  "/f4/IO/OE7",
    "obs_v_d12_drv": "/f4/MOTORS/M1",
    "obs_v_d13_drv": "/f4/MOTORS/M2",
    "obs_v_d14_drv": "/f4/MOTORS/M3",
    "obs_v_d15_drv": "/f4/MOTORS/M4",
}


# ============================================================
# Netlist parsing (unchanged from previous version)
# ============================================================
def run_ngspice(netlist_str):
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".cir", delete=False,
        newline="\r\n", encoding="utf-8"
    ) as f:
        f.write(netlist_str)
        temp_path = f.name
    try:
        r = subprocess.run(
            [NGSPICE_EXE, "-b", temp_path],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace"
        )
        return r.stdout
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def parse_node_voltages(output):
    voltages = {}
    in_table = False
    for line in output.splitlines():
        s = line.strip()
        if "Node" in s and "Voltage" in s:
            in_table = True
            continue
        if in_table:
            if not s or s.startswith("Source"):
                break
            parts = s.split()
            if len(parts) >= 2:
                try:
                    voltages[parts[0]] = float(parts[1])
                except ValueError:
                    pass
    return voltages


def parse_branch_currents(output):
    currents = {}
    in_section = False
    for line in output.splitlines():
        s = line.strip()
        if "Source" in s and "Current" in s:
            in_section = True
            continue
        if in_section:
            if not s:
                if currents:
                    break
                continue
            parts = s.split()
            if len(parts) >= 2 and parts[0].endswith("#branch"):
                try:
                    currents[parts[0][:-7]] = float(parts[1])
                except ValueError:
                    pass
    return currents


def parse_diode_currents(output):
    """Extract per-diode currents from the ngspice diode section.
    Includes opto subckt LEDs (xu10.d_led etc.)."""
    currents = {}
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s == "Diode: Junction Diode model":
            i += 1
            if i >= len(lines) or not lines[i].strip().startswith("device"):
                i += 1
                continue
            devs = lines[i].strip().split()[1:]
            n = len(devs)
            id_vals = None
            while i < len(lines):
                s2 = lines[i].strip()
                if s2.startswith(("Diode:", "Resistor:", "Isource:")):
                    break
                if s2.startswith("id "):
                    toks = s2.split()
                    if len(toks) >= 1 + n:
                        try:
                            id_vals = [float(x) for x in toks[1:1 + n]]
                        except ValueError:
                            pass
                i += 1
            if id_vals:
                for d, idd in zip(devs, id_vals):
                    ref = _normalize_ref(d)
                    if ref:
                        currents[ref] = idd
        i += 1
    return currents


def _normalize_ref(spice_name):
    n = spice_name.lower()
    if any(tok in n for tok in ("_leak_", "_rail_", "_trace_", "_src", "_sense")):
        return None
    # Subckt LED current: xu10.d_led -> U10
    m = re.match(r"^x(u\d+)\.d_led$", n)
    if m:
        return m.group(1).upper()
    if len(n) >= 2 and n[0] in "rcldf":
        return n[1:].upper()
    return spice_name.upper()


def parse_resistor_currents(output):
    """Return {device_name_lower: current_amps} for every resistor.

    ngspice prints a per-block table like:
        Resistor: Simple linear resistor
          device     r1   r2   r3 ...
          i          1e-5 2e-5 3e-5 ...
          p          1e-7 4e-7 9e-7 ...
    We pick up only the 'i' row.
    """
    currents = {}
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() == "Resistor: Simple linear resistor":
            i += 1
            if i >= len(lines) or not lines[i].strip().startswith("device"):
                continue
            devs = lines[i].strip().split()[1:]
            n = len(devs)
            while i < len(lines):
                s2 = lines[i].strip()
                if s2.startswith("i "):
                    toks = s2.split()
                    if len(toks) >= 1 + n:
                        for d, val in zip(devs, toks[1:1 + n]):
                            try:
                                currents[d.lower()] = float(val)
                            except ValueError:
                                pass
                    break
                if s2.startswith(("Diode:", "Isource:", "Capacitor:")):
                    break
                i += 1
        i += 1
    return currents


def parse_component_powers(output):
    powers = {}
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()

        if s == "Resistor: Simple linear resistor":
            i += 1
            if i >= len(lines) or not lines[i].strip().startswith("device"):
                i += 1
                continue
            devs = lines[i].strip().split()[1:]
            n = len(devs)
            while i < len(lines):
                s2 = lines[i].strip()
                if s2 == "Resistor: Simple linear resistor" and i > 0:
                    break
                if s2.startswith("p "):
                    toks = s2.split()
                    if toks[0] == "p" and len(toks) >= 1 + n:
                        for d, p in zip(devs, toks[1:1 + n]):
                            ref = _normalize_ref(d)
                            if ref:
                                powers[ref] = abs(float(p))
                    break
                i += 1

        elif s == "Isource: Independent current source":
            i += 1
            if i >= len(lines) or not lines[i].strip().startswith("device"):
                i += 1
                continue
            devs = lines[i].strip().split()[1:]
            n = len(devs)
            while i < len(lines):
                s2 = lines[i].strip()
                if s2.startswith("p "):
                    toks = s2.split()
                    if toks[0] == "p" and len(toks) >= 1 + n:
                        for d, p in zip(devs, toks[1:1 + n]):
                            powers[d.upper()] = abs(float(p))
                    break
                if any(x in s2 for x in ("Resistor:", "Isource:", "Diode:")):
                    break
                i += 1

        elif s == "Diode: Junction Diode model":
            i += 1
            if i >= len(lines) or not lines[i].strip().startswith("device"):
                i += 1
                continue
            devs = lines[i].strip().split()[1:]
            n = len(devs)
            vd_vals = id_vals = None
            while i < len(lines):
                s2 = lines[i].strip()
                if s2.startswith(("Diode:", "Resistor:", "Isource:")):
                    break
                if s2.startswith("vd "):
                    toks = s2.split()
                    if len(toks) >= 1 + n:
                        try:
                            vd_vals = [float(x) for x in toks[1:1 + n]]
                        except ValueError:
                            pass
                elif s2.startswith("id "):
                    toks = s2.split()
                    if len(toks) >= 1 + n:
                        try:
                            id_vals = [float(x) for x in toks[1:1 + n]]
                        except ValueError:
                            pass
                i += 1
            if vd_vals and id_vals:
                for d, vd, idd in zip(devs, vd_vals, id_vals):
                    ref = _normalize_ref(d)
                    if ref:
                        powers[ref] = abs(vd * idd)

        i += 1
    return powers


# ============================================================
# Substitution — the automation core
# ============================================================
def build_netlist(base, observations, dynamic_sources):
    """
    Apply every dynamic source's substitution.

    observations:    {"obs_v_in1": bytes([...]), "obs_v_d4_drv": bytes([...])}
    dynamic_sources: [{"spice_name": "v_in1", "kind": "analog_input"}, ...]

    Convention: firmware observable = "obs_" + spice_name
    """
    netlist = base

    # Case-insensitive lookup for observations
    obs_lower = {k.lower(): v for k, v in observations.items()}

    for src in dynamic_sources:
        spice_name = src["spice_name"].lower()          # "v_in1"
        kind = src["kind"]
        fw_key = f"obs_{spice_name}"                    # "obs_v_in1"

        raw = obs_lower.get(fw_key)
        if raw is None:
            continue

        transform = TRANSFORMS.get(kind)
        if transform is None:
            continue

        try:
            volts = transform(raw)
        except Exception as e:
            print(f"[!] transform failed for {spice_name}: {e}")
            continue

        netlist = re.sub(
            rf"^({re.escape(spice_name)}\s+\S+\s+0\s+DC\s+)[\d.eE+\-]+",
            rf"\g<1>{volts:.6f}",
            netlist,
            flags=re.MULTILINE | re.IGNORECASE
        )
    return netlist


# ============================================================
# Main
# ============================================================
def main():
    # --- Load netlist ---
    base_netlist = None
    for path in (NETLIST_TEMPLATE, NETLIST_FALLBACK):
        try:
            with open(path, "r", encoding="utf-8") as f:
                base_netlist = f.read()
            print(f"[*] Loaded netlist: {path} ({len(base_netlist)} bytes)")
            break
        except FileNotFoundError:
            continue
    if base_netlist is None:
        print("[!] No netlist found. Run spice_compiler.py first.")
        return

    # --- Load source metadata (all sources + kinds) ---
    try:
        with open(SOURCE_META, "r", encoding="utf-8") as f:
            all_sources = json.load(f).get("sources", [])
    except FileNotFoundError:
        print(f"[!] {SOURCE_META} not found. Run spice_compiler.py first.")
        return

    DYNAMIC_KINDS = {"analog_input", "dac", "led_drive",
                     "digital_output", "digital_input"}
    dynamic_sources = [s for s in all_sources if s["kind"] in DYNAMIC_KINDS]
    print(f"[*] source_metadata: {len(all_sources)} total, "
          f"{len(dynamic_sources)} dynamic")

    # --- Extract MCU / ESP self-load currents ---
    LOAD_SOURCES = {}
    for m in re.finditer(
        r"^I(U\d+)_(?:vdd|vcc)\s+\S+\s+0\s+DC\s+([\d.eE+\-]+)",
        base_netlist, re.MULTILINE):
        ref = m.group(1).upper()
        label = {"U5": "STM32", "U4": "ESP12F"}.get(ref, ref)
        LOAD_SOURCES[label] = float(m.group(2))
    print(f"[*] Load sources: {LOAD_SOURCES}")

    # --- ZMQ ---
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:{READER_PORT}")
    sub.setsockopt_string(zmq.SUBSCRIBE, "OBS")
    print(f"[*] Subscribed to OBS on {READER_PORT}")

    pub = ctx.socket(zmq.PUB)
    pub.bind(f"tcp://*:{ZMQ_PUB_PORT}")
    print(f"[*] Publishing on {ZMQ_PUB_PORT}")
    print(f"[*] ngspice: {NGSPICE_EXE}")
    print("[*] Solver running...")

    observations = {}      # {"obs_v_in1": bytes, ...}
    packets = 0
    last_solve = time.time()

    while True:
        try:
            msg = sub.recv_string()
            # Format: OBS:<name>:<hex>
            if not msg.startswith("OBS:"):
                continue
            parts = msg.split(":", 2)
            if len(parts) != 3:
                continue
            _, name, hex_str = parts
            try:
                observations[name] = bytes.fromhex(hex_str)
            except ValueError:
                continue

            # Rate limit
            now = time.time()
            if now - last_solve < 0.15:
                continue
            last_solve = now

            # --- Substitute and solve ---
            netlist = build_netlist(base_netlist, observations, dynamic_sources)
            output = run_ngspice(netlist)

            nodes = parse_node_voltages(output)
            currents = parse_branch_currents(output)
            powers = parse_component_powers(output)
            resistor_currents = parse_resistor_currents(output)

            if "IU5_VDD" in powers:
                powers["U5"] = powers.pop("IU5_VDD")
            if "IU4_VCC" in powers:
                powers["U4"] = powers.pop("IU4_VCC")

            for reg_ref, in_n, out_n, sense in (
                    ("U1", "n5v", "n3v3_ldo1", "v_sense_n3v3_ldo1"),
                    ("U2", "n5v", "n3v3_ldo2", "v_sense_n3v3_ldo2"),
            ):
                vin = nodes.get(in_n, 0.0)
                vout = nodes.get(out_n, 0.0)
                iout = abs(currents.get(sense, 0.0))
                powers[reg_ref] = max(0.0, (vin - vout) * iout)

            if "n12v" in nodes and "n5v" in nodes:
                vin = nodes["n12v"]; vout = nodes["n5v"]
                iin = abs(currents.get("v_sense_n12v", 0.0))
                iout = abs(currents.get("v_sense_n5v", 0.0))
                powers["U3"] = max(0.0, vin * iin - vout * iout)

            for reg, q in (("U1", "IU1_QUIESCENT"),
                           ("U2", "IU2_QUIESCENT"),
                           ("U3", "IU3_QUIESCENT")):
                if q in powers:
                    powers[reg] = powers.get(reg, 0.0) + powers.pop(q)

            packets += 1
            if not nodes:
                print(f"[!] pkt {packets}: no nodes")
                continue

            # --- Publish ---
            # Summary
            a_in1 = None
            if "obs_v_in1" in observations:
                a_in1 = TRANSFORMS["analog_input"](observations["obs_v_in1"])
            else:
                a_in1 = 0.0
            pub.send_string(
                f"SIM:{packets}:"
                f"A_IN1={a_in1:.2f}V,"
                f"C0={nodes.get('c0', 0.0):.3f}V,"
                f"OUT1={nodes.get('net__j9_pin_1_', 0.0):.3f}V,"
                f"OUT2={nodes.get('net__j9_pin_2_', 0.0):.3f}V"
            )

            # Digital input pin states (SPICE node voltage at MCU pins)
            for i in range(8, 16):
                node = f"e{i}"
                if node in nodes:
                    pub.send_string(f"PIN:{node}:{nodes[node]:.6f}")

            # Digital output drive states (firmware-controlled optos).
            # Published for both industrial (OE0..OE7) and motor (M1..M4)
            # outputs. The current viewer uses this to draw "armed" state
            # on the field-side net when no load is attached (current = 0).
            for obs_name, field_net in DOUT_MAP.items():
                raw = observations.get(obs_name)
                if raw is None or len(raw) == 0:
                    continue
                st = 1 if raw[0] != 0 else 0
                pub.send_string(f"DOUT:{field_net}:{st}")

            # Per-net node voltages for the voltage viewer. Only nets that
            # exist in both the KiCad netlist and the ngspice solution are
            # published, so the viewer can colour every copper trace by the
            # real DC voltage ngspice solved for it.
            #
            # ngspice lowercases node names in its output table, so we build
            # a case-insensitive lookup before matching.
            if ALL_KICAD_NETS:
                nodes_lower = {k.lower(): v for k, v in nodes.items()}
                matched = 0
                for kicad_net in ALL_KICAD_NETS:
                    sn = spice_net(kicad_net)
                    if sn == "0":
                        v = 0.0
                    else:
                        v = nodes.get(sn)
                        if v is None:
                            v = nodes_lower.get(sn.lower())
                        if v is None:
                            continue
                    matched += 1
                    pub.send_string(f"NODE:{kicad_net}:{v:.6f}")

                # Diagnostic — prints once every 50 packets so you can verify
                # the match rate without spamming the console.
                if packets % 50 == 0:
                    total = len(ALL_KICAD_NETS)
                    print(f"[node] published {matched}/{total} net voltages "
                          f"({total - matched} unmatched)")

            # Rails
            for pcb_net, load_side, src_side in (
                ("12v",       "n12v_trace",       "n12v"),
                ("5v",        "n5v_trace",        "n5v"),
                ("3v3-LDO1",  "n3v3_ldo1_trace",  "n3v3_ldo1"),
                ("3v3-LDO2",  "n3v3_ldo2_trace",  "n3v3_ldo2"),
            ):
                v = nodes.get(load_side, nodes.get(src_side))
                if v is not None:
                    pub.send_string(f"RAIL:{pcb_net}:{v:.6f}")

            # Rail currents (delivered = negative of source branch)
            for branch, pcb_net in (
                ("v_rail_n12v",      "12v"),
                ("v_rail_n5v",       "5v"),
                ("v_rail_n3v3_ldo1", "3v3-LDO1"),
                ("v_rail_n3v3_ldo2", "3v3-LDO2"),
            ):
                if branch in currents:
                    pub.send_string(f"RAIL_I:{pcb_net}:{-currents[branch]:.9e}")

            # Component powers
            for ref, p in powers.items():
                pub.send_string(f"POWER:{ref}:{p:.9e}")
                if p > 0.5:
                    print(f"  [BIG] {ref}: {p * 1e3:.1f} mW")

            # LED currents (from diode section — includes opto subckt LEDs)
            led_currents = parse_diode_currents(output)
            for ref, i_amps in led_currents.items():
                pub.send_string(f"LED_I:{ref}:{i_amps:.9e}")

            # Load currents
            for label, amps in LOAD_SOURCES.items():
                pub.send_string(f"LOAD_I:{label}:{amps:.9e}")

            # Per-net currents for the current viewer — from source_metadata.json
            # Any source that appears in ngspice's branch table gets published.
            for src in all_sources:
                spice_name = src["spice_name"]
                kicad_net = src["kicad_net"]
                if spice_name in currents:
                    i = currents[spice_name]
                    pub.send_string(f"CURRENT:{kicad_net}:{-i:.9e}")

            # MCU-side digital input pin currents (E8..E15).
            # The compiler adds R_pd_E8..R_pd_E15 (10k to GND) as a stand-in
            # for the STM32's input pin. Current through these resistors is
            # exactly the current the opto emitter pushes into the pin.
            # ngspice reports it as i(E8->GND), which is our current into the pin.
            for k in range(8, 16):
                dev = f"r_pd_e{k}"
                if dev in resistor_currents:
                    i_amps = resistor_currents[dev]
                    pub.send_string(f"CURRENT:E{k}:{i_amps:.9e}")

            # Console
            total_p = sum(powers.values())
            led_str = "".join(
                "1" if observations.get(f"obs_v_d{r}_drv", b"\x00")[0] == 0 else "0"
                for r in (4, 5, 6, 7, 8, 9)
            )
            print(f"[SIM] pkt={packets:>5} "
                  f"5V={nodes.get('n5v', 0):.4f} "
                  f"3v3L1={nodes.get('n3v3_ldo1', 0):.4f} "
                  f"A_IN1={a_in1:>5.2f} "
                  f"LEDs={led_str} "
                  f"P_tot={total_p*1000:.2f}mW")

        except KeyboardInterrupt:
            print("\n[*] Stopping...")
            break
        except Exception as e:
            print(f"[!] {type(e).__name__}: {e}")
            continue

    pub.close()
    sub.close()
    ctx.term()


if __name__ == "__main__":
    main()
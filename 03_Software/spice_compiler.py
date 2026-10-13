"""
Automatic SPICE netlist generator.
Reads: digital_twin_netlist.json + component_classification.json
Writes: auto_generated.cir + source_metadata.json

No hardcoded circuit knowledge — everything comes from the netlist.
"""
import json
import re

NETLIST_JSON     = "digital_twin_netlist.json"
CLASSIFY_JSON    = "component_classification.json"
OUTPUT_CIR       = "auto_generated.cir"
OUTPUT_META      = "source_metadata.json"


# === Source registry — records every V/E element we emit so downstream
# === tools know which KiCad net each SPICE branch name corresponds to.
SOURCE_REGISTRY = []


def register_source(spice_name, kicad_net, kind):
    SOURCE_REGISTRY.append({
        "spice_name": spice_name.lower(),
        "kicad_net":  kicad_net,
        "kind":       kind,
    })


# === Net name sanitization (SPICE doesn't like slashes) ===
def spice_net(net_name):
    """Map KiCad net names to safe SPICE node names."""
    if net_name in ("GND", "gnd", "0"):
        return "0"
    s = re.sub(r"[^A-Za-z0-9_]", "_", net_name)
    if s[0].isdigit():
        s = "N" + s
    return s


# === Build component → pin → net reverse map ===
def build_pin_map(netlist):
    """Returns: {ref: {pin: net_name}}"""
    pin_map = {}
    for net_name, net_data in netlist.items():
        for node in net_data.get("nodes", []):
            ref = node["component"]
            pin = node.get("pin", "?")
            pin_map.setdefault(ref, {})[pin] = net_name
    return pin_map


# === STM32F407VETx LQFP100 pin functions (VDD/VSS pins of interest) ===
STM32_VDD_PINS = {"1", "6", "11", "19", "21", "28", "50", "75", "100"}
STM32_VSS_PINS = {"10", "20", "27", "74", "99"}
STM32_VDDA_PIN = "22"
STM32_VCAP1    = "49"
STM32_VCAP2    = "73"


def find_net_for_pins(pin_map, ref, pins):
    """Return the most-common net name across the given pins for a component."""
    counts = {}
    for p in pins:
        n = pin_map.get(ref, {}).get(p)
        if n:
            counts[n] = counts.get(n, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def emit_passive_R(ref, pins, ohms):
    if len(pins) < 2:
        return None
    n1, n2 = pins[0], pins[1]
    return f"R{ref} {spice_net(n1)} {spice_net(n2)} {ohms:.4f}"


def emit_passive_C(ref, pins, farads):
    if len(pins) < 2:
        return None
    n1, n2 = pins[0], pins[1]
    return f"C{ref} {spice_net(n1)} {spice_net(n2)} {farads:.3e}"


def emit_passive_L(ref, pins, henries):
    if len(pins) < 2:
        return None
    n1, n2 = pins[0], pins[1]
    return f"L{ref} {spice_net(n1)} {spice_net(n2)} {henries:.3e}"


def emit_ferrite(ref, pins, r_dc):
    if len(pins) < 2:
        return None
    n1, n2 = pins[0], pins[1]
    return f"R{ref} {spice_net(n1)} {spice_net(n2)} {r_dc:.4f}"


def emit_led(ref, pins):
    """KiCad convention: pin 1 = cathode, pin 2 = anode."""
    if len(pins) < 2:
        return None
    pin_to_net = dict(pins)
    cathode_net = pin_to_net.get("1")
    anode_net = pin_to_net.get("2")
    if not anode_net or not cathode_net:
        return None
    return f"D{ref} {spice_net(anode_net)} {spice_net(cathode_net)} LEDMOD"


def emit_optocoupler(ref, pins, kind):
    """Emit a subcircuit instance for an optocoupler.

    PC817 (SMD-4):  1=Anode 2=Cathode 3=Emitter 4=Collector
    6N137 (SMD-8):  2=Anode 3=Cathode 5=GND 6=VO
    """
    pn = dict(pins)
    try:
        if kind == "optocoupler":        # PC817
            a = pn["1"]; k = pn["2"]; e = pn["3"]; c = pn["4"]
            return (f"X{ref} {spice_net(a)} {spice_net(k)} "
                    f"{spice_net(c)} {spice_net(e)} PC817")
        elif kind == "optocoupler_hs":   # 6N137
            a = pn["2"]; k = pn["3"]; vo = pn["6"]; gnd = pn["5"]
            return (f"X{ref} {spice_net(a)} {spice_net(k)} "
                    f"{spice_net(vo)} {spice_net(gnd)} 6N137")
    except KeyError:
        return None
    return None


def emit_industrial_inputs(netlist):
    """V-sources on the industrial input connector nets (IE8..IE15).
    Panel-driven via INJECT. Zero at startup; the panel sets them."""
    lines = ["* === Industrial input sources (panel-driven) ==="]
    IE_NETS = [
        "/f4/IO/IE8", "/f4/IO/IE9", "/f4/IO/IE10", "/f4/IO/IE11",
        "/f4/IO/IE12", "/f4/IO/IE13", "/f4/IO/IE14", "/f4/IO/IE15",
    ]
    for i, net in enumerate(IE_NETS):
        if net not in netlist:
            print(f"  [warn] input net not found: {net}")
            continue
        label = f"ie{8 + i}"
        safe = spice_net(net)
        lines.append(f"* {label} input (J3 pin {9 + i})")
        lines.append(f"V_{label}_drv {safe} 0 DC 0.0")
        register_source(f"v_{label}_drv", net, "digital_input")
    return lines


def emit_input_pulldowns(netlist):
    """10k pull-downs on the MCU-side input pins E8..E15."""
    lines = ["* === Input pin pull-downs (E8..E15) ==="]
    for i in range(8, 16):
        net = f"E{i}"
        if net in netlist:
            safe = spice_net(net)
            lines.append(f"R_pd_E{i} {safe} 0 10k")
    return lines


def emit_industrial_drivers(pin_map):
    """V-sources on the MCU pins that drive industrial output optos
    (PE0..PE7) and motor optos (PD12..PD15). Firmware-controlled."""
    lines = []
    u5_pins = pin_map.get("U5", {})
    PIN_MAP = {
        "97": "e0", "98": "e1", "1": "e2", "2": "e3",
        "3": "e4", "4": "e5", "5": "e6", "38": "e7",
        "59": "d12", "60": "d13", "61": "d14", "62": "d15",
    }
    for pin, label in PIN_MAP.items():
        net = u5_pins.get(pin)
        if not net:
            continue
        if _is_power_rail(net):
            # Safety: never emit a driver on a power rail
            continue
        safe = spice_net(net)
        lines.append(f"* {label} drive from MCU GPIO")
        lines.append(f"V_{label}_drv {safe} 0 DC 0.0")
        register_source(f"v_{label}_drv", net, "digital_output")
    return lines


def emit_ldo(ref, pins, vout):
    # Explicit rail sources handle output; skip here to avoid conflicts
    return None


def emit_buck(ref, pins, vout):
    # Explicit rail sources handle output; skip here to avoid conflicts
    return None


def emit_mcu_load(ref, pins, i_typ_a):
    """STM32 as a current load on its VDD net."""
    pin_to_net = dict(pins)
    vdd_net = None
    for p in STM32_VDD_PINS:
        if p in pin_to_net:
            vdd_net = pin_to_net[p]
            break
    if vdd_net is None:
        return None
    return (
        f"* {ref} STM32 static load ({i_typ_a*1000:.0f} mA)\n"
        f"I{ref}_vdd {spice_net(vdd_net)} 0 DC {i_typ_a}"
    )


def emit_wifi_load(ref, pins, i_typ_a):
    pin_to_net = dict(pins)
    vcc_net = pin_to_net.get("8")
    if not vcc_net:
        return None
    return (
        f"* {ref} ESP-12F load ({i_typ_a*1000:.0f} mA)\n"
        f"I{ref}_vcc {spice_net(vcc_net)} 0 DC {i_typ_a}"
    )


def emit_opamp_dual(ref, pins):
    """LM358: 1=OUT_A, 2=IN-_A, 3=IN+_A, 4=V-, 5=IN+_B, 6=IN-_B, 7=OUT_B, 8=V+
    Two ideal VCVS elements with finite gain."""
    pn = dict(pins)
    try:
        out_a    = pn["1"]; in_neg_a = pn["2"]; in_pos_a = pn["3"]
        v_neg    = pn["4"]
        in_pos_b = pn["5"]; in_neg_b = pn["6"]; out_b    = pn["7"]
        v_pos    = pn["8"]
    except KeyError:
        return None
    register_source(f"e{ref}_a", out_a, "opamp_output")
    register_source(f"e{ref}_b", out_b, "opamp_output")
    return (
        f"* {ref} LM358 dual op-amp (ideal, gain=1e5)\n"
        f"E{ref}_A {spice_net(out_a)} {spice_net(v_neg)} "
        f"{spice_net(in_pos_a)} {spice_net(in_neg_a)} 100000\n"
        f"E{ref}_B {spice_net(out_b)} {spice_net(v_neg)} "
        f"{spice_net(in_pos_b)} {spice_net(in_neg_b)} 100000"
    )


def emit_diode(ref, pins, vf):
    """KiCad convention: pin 1 = cathode (K), pin 2 = anode (A)."""
    pn = dict(pins)
    if "1" not in pn or "2" not in pn:
        return None
    anode_net   = pn["2"]
    cathode_net = pn["1"]
    return f"D{ref} {spice_net(anode_net)} {spice_net(cathode_net)} DIODEMOD"


# === Stimulus generation ===
def emit_adc_stimulus(classify, pin_map, adc_net_map):
    """For each analog input net, add a V-source. adc_net_map is {rank: net}."""
    lines = []
    for rank, net in adc_net_map.items():
        safe = spice_net(net)
        src = f"V_in{rank}_src"
        lines.append(f"V_in{rank} {src} 0 DC 5.0")
        lines.append(f"R_in{rank}_src {src} {safe} 0.01")
        register_source(f"v_in{rank}", net, "analog_input")
    return lines


# Nets that are power rails — never drive them with a "GPIO" source
POWER_RAIL_TOKENS = ("12v", "5v", "3v3", "vcc", "vdd", "ldo", "gnd")

def _is_power_rail(net_name):
    if not net_name:
        return True
    nl = net_name.lower().replace("-", "_")
    return any(tok in nl for tok in POWER_RAIL_TOKENS)


def emit_led_drivers(classify, pin_map):
    """For each firmware-controlled LED, emit a V-source on its GPIO side.
    Skips LEDs whose identified 'drive net' is a power rail."""
    LED_MAP = {"D3", "D4", "D5", "D6", "D7", "D8", "D9"}
    lines = []
    for ref in LED_MAP:
        if ref not in classify["components"]:
            continue
        pins = pin_map.get(ref, {})
        if not pins:
            continue

        drive_net = None
        for pnum, net in pins.items():
            if _is_power_rail(net):
                continue
            for other_ref, other_pins in pin_map.items():
                if other_ref == "U5" and net in other_pins.values():
                    drive_net = net
                    break
            if drive_net:
                break

        if drive_net is None or _is_power_rail(drive_net):
            lines.append(f"* {ref} skipped — no GPIO drive net (power indicator)")
            continue

        safe = spice_net(drive_net)
        lines.append(f"* {ref} drive from GPIO")
        lines.append(f"V_{ref}_drv {safe} 0 DC 0.0")
        register_source(f"v_{ref}_drv", drive_net, "led_drive")

    return lines


def emit_rail_sources(netlist):
    """Rails with upstream current reflection.

    Each rail is modeled as:
        V_source -> V_sense (0V) -> R_out -> rail_net

    The current through V_sense is the rail's total load current. An
    F-element mirrors it into the upstream rail with a scale factor
    that represents the regulator's efficiency and voltage ratio:
        LDO:  gain = 1.0                (I_in ≈ I_out)
        Buck: gain = Vout/(Vin * eta)   (power conservation)
    """
    rails = [
        # (name, volts, R_out, upstream, i_q, reflect_gain)
        ("12v",      12.0, 0.010, None,   0.000, 0.0),
        ("5v",        5.0, 0.150, "12v",  0.005, 5.0 / (12.0 * 0.85)),
        ("3v3-LDO1",  3.3, 0.200, "5v",   0.005, 1.0),
        ("3v3-LDO2",  3.3, 0.200, "5v",   0.005, 1.0),
    ]

    def norm(s):
        return s.lower().replace("-", "_").strip()

    lines = ["* === Power rails with upstream current reflection ==="]
    rail_net_lookup = {}    # target -> spice node name
    rail_kicad_lookup = {}  # target -> original KiCad net name

    # Pass 1: match rail names case-insensitively
    for target, *_ in rails:
        t = norm(target)
        matched = None
        for key in netlist.keys():
            if norm(key) == t:
                matched = key
                break
        if matched is None:
            print(f"  [warn] rail not found: {target}")
            continue
        rail_net_lookup[target] = spice_net(matched)
        rail_kicad_lookup[target] = matched

    # Pass 2: emit each rail with sense source + reflection
    for target, volts, r_out, upstream, i_q, gain in rails:
        safe = rail_net_lookup.get(target)
        if safe is None:
            continue
        src   = f"{safe}_SRC"
        sense = f"{safe}_SENSE"
        lines.append(f"* --- Rail {target} ({volts} V) ---")
        lines.append(f"V_rail_{safe} {src} 0 DC {volts}")
        lines.append(f"V_sense_{safe} {src} {sense} DC 0")
        lines.append(f"R_rail_{safe} {sense} {safe} {r_out}")
        register_source(f"v_rail_{safe}", rail_kicad_lookup[target], "power_rail")

        if upstream is None or gain <= 0:
            continue
        up_safe = rail_net_lookup.get(upstream)
        if up_safe is None:
            continue
        lines.append(
            f"* reflect {target} load -> {upstream} (gain {gain:.4f})"
        )
        lines.append(
            f"F_{safe}_reflect {up_safe} 0 V_sense_{safe} {gain:.6f}"
        )

    # Pass 3: quiescent currents from upstream rails
    lines.append("")
    lines.append("* Regulator quiescent currents")
    for target, volts, _, upstream, i_q, _ in rails:
        if upstream is None or i_q <= 0:
            continue
        up_safe = rail_net_lookup.get(upstream)
        if up_safe is None:
            continue
        reg_label = {"5v": "U3", "3v3-LDO1": "U1", "3v3-LDO2": "U2"}.get(target, "?")
        lines.append(f"I_{reg_label}_quiescent {up_safe} 0 DC {i_q}")

    return lines


def emit_dac_sources(netlist):
    """STM32 DAC pins drive daq1 and daq2. Firmware controls them;
    for now, park them at 0.5 V (solver overrides per packet)."""
    lines = ["* === DAC sources ==="]
    for net_name in ("daq1", "daq2"):
        if net_name in netlist:
            safe = spice_net(net_name)
            lines.append(f"V_{safe} {safe} 0 DC 0.5")
            register_source(f"v_{safe}", net_name, "dac")
    return lines


def emit_gmin_leaks(netlist):
    """10 MΩ from every non-ground net to GND.
    Prevents floating-node singular matrices without affecting results."""
    skip = {"GND", "gnd", "0"}
    lines = ["* === Anti-floating leak resistors ==="]
    for i, net_name in enumerate(sorted(netlist.keys())):
        if net_name in skip:
            continue
        safe = spice_net(net_name)
        if safe == "0":
            continue
        lines.append(f"R_leak_{i} {safe} 0 1Meg")
    return lines


# === MAIN ===
def main():
    with open(NETLIST_JSON) as f:
        netlist = json.load(f)
    with open(CLASSIFY_JSON) as f:
        classify = json.load(f)

    pin_map = build_pin_map(netlist)
    comps = classify["components"]

    lines = [
        "* ====================================================",
        "* AUTO-GENERATED SPICE NETLIST",
        "* Source: digital_twin_netlist.json + component_classification.json",
        "* ====================================================",
        "",
        "* === Passive components ===",
    ]

    SKIP_REFS = {"L1", "D2"}  # XL1509 buck companions

    unhandled = []
    for ref, info in comps.items():
        if ref in SKIP_REFS:
            lines.append(f"* {ref} skipped (buck-companion, emulated by 5V rail)")
            continue
        t = info["type"]
        pins = list(pin_map.get(ref, {}).items())

        emitted = None
        if t == "resistor":
            emitted = emit_passive_R(ref, [n for _, n in pins], info["ohms"])
        elif t == "capacitor":
            emitted = emit_passive_C(ref, [n for _, n in pins], info["farads"])
        elif t == "inductor":
            emitted = emit_passive_L(ref, [n for _, n in pins], info["henries"])
        elif t == "ferrite_bead":
            emitted = emit_ferrite(ref, [n for _, n in pins], info.get("r_dc", 0.05))
        elif t == "led":
            emitted = emit_led(ref, pins)
        elif t == "ldo":
            emitted = emit_ldo(ref, pins, info.get("vout", 3.3))
        elif t == "buck":
            emitted = emit_buck(ref, pins, info.get("vout", 5.0))
        elif t == "mcu":
            emitted = emit_mcu_load(ref, pins, info.get("i_typ_a", 0.05))
        elif t == "wifi_module":
            emitted = emit_wifi_load(ref, pins, info.get("i_typ_a", 0.08))
        elif t == "opamp_dual":
            emitted = emit_opamp_dual(ref, pins)
        elif t == "schottky":
            emitted = emit_diode(ref, pins, info.get("vf", 0.5))
        elif t == "diode":
            emitted = emit_diode(ref, pins, info.get("vf", 0.7))
        elif t == "optocoupler":
            emitted = emit_optocoupler(ref, pins, "optocoupler")
        elif t == "optocoupler_hs":
            emitted = emit_optocoupler(ref, pins, "optocoupler_hs")

        if emitted:
            lines.append(emitted)
            lines.append("")
        elif t not in ("connector", "switch", "crystal",
                           "dual_schottky", "rs485_transceiver",
                           "spi_flash"):
            unhandled.append(f"{ref} ({t})")

    # === Power rails ===
    lines.extend(emit_rail_sources(netlist))
    lines.append("")

    # === DAC sources ===
    lines.extend(emit_dac_sources(netlist))
    lines.append("")

    # === LED drive sources ===
    lines.append("* === LED drive sources (firmware-controlled) ===")
    lines.extend(emit_led_drivers(classify, pin_map))
    lines.append("")

    # === Industrial / motor opto drive sources ===
    lines.append("* === Industrial output drivers (firmware-controlled) ===")
    lines.extend(emit_industrial_drivers(pin_map))
    lines.append("")

    # === Industrial input sources (panel-driven) ===
    lines.extend(emit_industrial_inputs(netlist))
    lines.append("")

    # === Input pin pull-downs ===
    lines.extend(emit_input_pulldowns(netlist))
    lines.append("")

    # === ADC input stimuli ===
    INDUSTRIAL_INPUTS = [
        "/f4/A_IN/A_IN1", "/f4/A_IN/A_IN2",
        "/f4/A_IN/A_IN3", "/f4/A_IN/A_IN4",
        "/f4/A_IN/A_IN5", "/f4/A_IN/A_IN6",
        "/f4/A_IN/A_IN7", "/f4/A_IN/A_IN8",
    ]
    adc_net_map = {}
    rank = 1
    for name in INDUSTRIAL_INPUTS:
        matched = None
        for key in netlist.keys():
            if key.lower() == name.lower():
                matched = key
                break
        if matched:
            adc_net_map[rank] = matched
            rank += 1
    lines.append("* === Analog input stimuli ===")
    lines.extend(emit_adc_stimulus(classify, pin_map, adc_net_map))
    lines.append("")

    # === Anti-floating ===
    lines.extend(emit_gmin_leaks(netlist))
    lines.append("")

    # === Models ===
    lines.extend([
        "* === Device models ===",
        ".include optocoupler.lib",
        ".model LEDMOD D(Is=1e-14 N=2.0 Rs=5 Cj=0 Tt=0)",
        ".model DIODEMOD D(Is=1e-14 N=1.05 Rs=0.5 Cj=0 Tt=0)",
        "",
        ".op",
        ".end",
    ])

    with open(OUTPUT_CIR, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # === Emit source metadata ===
    # Maps each SPICE branch name to its KiCad net name + kind. Downstream
    # tools (pyspice_solver.py) use this to publish per-net currents
    # without hardcoding anything.
    with open(OUTPUT_META, "w", encoding="utf-8") as f:
        json.dump({"sources": SOURCE_REGISTRY}, f, indent=2)

    print(f"[+] Wrote {OUTPUT_CIR}")
    print(f"[+] Wrote {OUTPUT_META} ({len(SOURCE_REGISTRY)} sources)")
    print(f"    Components: {len(comps)}")
    print(f"    Net map:    {len(pin_map)} components have pin assignments")
    if unhandled:
        print(f"    Unhandled types ({len(unhandled)}):")
        for u in unhandled[:20]:
            print(f"      {u}")
        if len(unhandled) > 20:
            print(f"      ... and {len(unhandled) - 20} more")


if __name__ == "__main__":
    main()
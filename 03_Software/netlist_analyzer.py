"""
Analyze digital_twin_netlist.json and classify every component.
Outputs: component_classification.json

This is the input to spice_compiler.py, which will generate a full
SPICE netlist from any KiCad PCB — no hardcoded circuit knowledge.
"""
import json
import re

NETLIST_JSON  = "digital_twin_netlist.json"
OUTPUT_JSON   = "component_classification.json"


# === Value parsers ===
def parse_resistance(v):
    """'10k' -> 10000, '4.7K' -> 4700, '330' -> 330, '10M' -> 10e6"""
    if v is None:
        return None
    s = str(v).strip().replace("R", "").replace("r", "").replace("Ω", "")
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([kKmM]?)$", s)
    if not m:
        return None
    num = float(m.group(1))
    mult = {"": 1, "k": 1e3, "K": 1e3, "m": 1e-3, "M": 1e6}[m.group(2)]
    return num * mult


def parse_capacitance(v):
    """'0.1u' -> 1e-7, '22p' -> 22e-12, '470u' -> 470e-6, '2.2u' -> 2.2e-6"""
    if v is None:
        return None
    s = str(v).strip().replace("F", "").replace("f", "")
    m = re.match(r"^([0-9]*\.?[0-9]+)\s*([pnuµm]?)$", s)
    if not m:
        return None
    num = float(m.group(1))
    mult = {"": 1.0, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3}[m.group(2)]
    return num * mult


def parse_inductance(v):
    """'68u' -> 68e-6"""
    return parse_capacitance(v)   # same suffix scheme


# === Component classifier ===
def classify(ref, value):
    """Return a dict describing this component."""
    ref = ref.upper()
    val = (value or "").upper()

    # Resistors
    if ref.startswith("R"):
        ohms = parse_resistance(value)
        if ohms is not None:
            return {"type": "resistor", "ohms": ohms, "raw_value": value}

    # Capacitors
    if ref.startswith("C"):
        f = parse_capacitance(value)
        if f is not None:
            return {"type": "capacitor", "farads": f, "raw_value": value}

    # Inductors
    if ref.startswith("L"):
        h = parse_inductance(value)
        if h is not None:
            return {"type": "inductor", "henries": h, "raw_value": value}

    # LEDs
    if ref.startswith("D") and "LED" in val:
        return {"type": "led", "vf_nom": 2.0, "raw_value": value,
                "model": "generic_led"}

    # Diodes / Schottky / BAT54S
    if ref.startswith("D"):
        if "BAT54S" in val:
            return {"type": "dual_schottky", "vf": 0.3,
                    "raw_value": value, "model": "BAT54S"}
        if "1N5820" in val:
            return {"type": "schottky", "vf": 0.5,
                    "raw_value": value, "model": "1N5820"}
        if "SS14" in val:
            return {"type": "schottky", "vf": 0.5,
                    "raw_value": value, "model": "SS14"}
        return {"type": "diode", "vf": 0.7, "raw_value": value}

    # Voltage regulators
    if "AMS1117" in val:
        vout = 3.3 if "3.3" in val else 5.0 if "5" in val else None
        return {"type": "ldo", "vout": vout, "raw_value": value,
                "model": "AMS1117"}
    if "XL1509" in val:
        vout = 5.0 if "5" in val else None
        return {"type": "buck", "vout": vout, "raw_value": value,
                "model": "XL1509"}

    # Op-amps
    if "LM358" in val:
        return {"type": "opamp_dual", "raw_value": value, "model": "LM358"}
    if "LM324" in val:
        return {"type": "opamp_quad", "raw_value": value, "model": "LM324"}

    # Comparators
    if "LM393" in val:
        return {"type": "comparator_dual", "raw_value": value, "model": "LM393"}

    # Optocouplers
    if ref.startswith("U") and "PC817" in val:
        return {"type": "optocoupler", "raw_value": value, "model": "PC817"}
    if ref.startswith("U") and "6N137" in val:
        return {"type": "optocoupler_hs", "raw_value": value, "model": "6N137"}

    # Logic ICs
    if "MAX3485" in val:
        return {"type": "rs485_transceiver", "raw_value": value,
                "model": "MAX3485"}

    # Memory
    if "W25Q" in val:
        return {"type": "spi_flash", "raw_value": value, "model": val}

    # Microcontrollers
    if "STM32" in val:
        return {"type": "mcu", "raw_value": value, "model": val,
                "i_typ_a": 0.05}   # typical ~50 mA; refine later

    # Crystal
    if ref.startswith("Y"):
        return {"type": "crystal", "raw_value": value}

    # Wireless modules
    if "ESP-12" in val or "ESP12" in val:
        return {"type": "wifi_module", "raw_value": value,
                "i_typ_a": 0.08,        # 80 mA typical (peaks much higher)
                "model": "ESP-12F"}

    # Switches / buttons
    if ref.startswith("SW"):
        return {"type": "switch", "raw_value": value}

    # Connectors
    if ref.startswith("J"):
        return {"type": "connector", "raw_value": value}

    # Ferrite bead
    if ref.startswith("FB") or "FERRITE" in val:
        return {"type": "ferrite_bead", "raw_value": value,
                "r_dc": 0.05}   # typical 50 mΩ

    # Unknown
    return {"type": "unknown", "raw_value": value}


# === Main analysis ===
def analyze():
    with open(NETLIST_JSON) as f:
        netlist = json.load(f)

    # Build a registry of every unique component (ref -> value)
    components = {}
    for net_name, net_data in netlist.items():
        for node in net_data.get("nodes", []):
            ref = node["component"]
            val = node.get("value")
            if ref not in components:
                components[ref] = {
                    "ref": ref,
                    "value": val,
                    "nets": [],
                }
            if net_name not in components[ref]["nets"]:
                components[ref]["nets"].append(net_name)

    # Classify each
    classified = {}
    for ref, entry in components.items():
        cls = classify(ref, entry["value"])
        cls["nets"] = entry["nets"]
        cls["pin_count"] = len(cls["nets"])   # rough approximation
        classified[ref] = cls

    # Summary stats
    type_counts = {}
    for c in classified.values():
        type_counts[c["type"]] = type_counts.get(c["type"], 0) + 1

    output = {
        "component_count": len(classified),
        "type_summary": type_counts,
        "components": classified,
    }

    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2)

    print(f"[+] Classified {len(classified)} components")
    print(f"    Type summary:")
    for t, n in sorted(type_counts.items(), key=lambda kv: -kv[1]):
        print(f"      {t:24s}  {n:4d}")
    print(f"[+] Wrote {OUTPUT_JSON}")


if __name__ == "__main__":
    analyze()
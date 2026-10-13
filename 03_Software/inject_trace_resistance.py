"""
Inject trace resistance into auto_generated.cir.

For each power rail (12v, 5v, 3v3-LDO1, 3v3-LDO2), split the SPICE node
into a source side (at the regulator pin) and a load side (where all
other components connect), with a resistor between them whose value is
derived from actual KiCad copper geometry.

Input:  auto_generated.cir   (from spice_compiler.py)
        net_resistance.json  (from trace_resistance.py)
Output: auto_generated_with_traces.cir
"""
import json
import re
import sys

INPUT_CIR   = "auto_generated.cir"
OUTPUT_CIR  = "auto_generated_with_traces.cir"
NET_RES     = "net_resistance.json"

# Nets whose trace resistance we inject. GND is deliberately excluded:
# every component references it, and modern boards use a pour, not a tree.
TARGET_NETS = ["12v", "5v", "3v3-LDO1", "3v3-LDO2"]

# The per-net resistance in net_resistance.json is the SUM of every segment
# on the net. The effective source-to-load resistance for a distributed
# trace tree is only a fraction of that. This factor represents
# "typical source-to-load path / total copper."
LUMP_FRACTION = 0.15


def spice_name(net):
    """Same sanitization as spice_compiler.py."""
    if net in ("GND", "gnd", "0"):
        return "0"
    s = re.sub(r"[^A-Za-z0-9_]", "_", net)
    if s[0].isdigit():
        s = "N" + s
    return s


def load_resistances():
    try:
        with open(NET_RES) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[!] {NET_RES} not found — run trace_resistance.py first")
        return {}
    out = {}
    for net_name, entry in data.get("nets", {}).items():
        total_r = entry.get("total_resistance_ohm", 0.0)
        out[net_name] = total_r * LUMP_FRACTION
    return out


def inject(input_path, output_path):
    try:
        with open(input_path) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        print(f"[!] {input_path} not found — run spice_compiler.py first")
        return False

    resistances = load_resistances()
    if not resistances:
        return False

    # Map: kicad_net -> (spice_node_src, spice_node_load, R_lumped)
    targets = {}
    for net in TARGET_NETS:
        if net not in resistances:
            print(f"  [warn] rail not in net_resistance.json: {net}")
            continue
        r = resistances[net]
        if r < 1e-5:   # below 10 µΩ, negligible
            print(f"  [warn] {net} R too small ({r:.2e} Ω), skipping")
            continue
        sn = spice_name(net)
        targets[net] = (sn, f"{sn}_TRACE", r)

    if not targets:
        print("[!] No rails to inject.")
        return False

    print("[*] Injecting trace resistance:")
    for net, (src, load, r) in targets.items():
        print(f"      {net:12s}  R = {r*1e3:8.3f} mΩ  ({src} -> {load})")

    # === Pass 1: rename node references ===
    # Skip the rail source block (V_rail_<n> / V_sense_<n> / R_rail_<n>).
    # Skip comment lines.
    out_lines = []
    for line in lines:
        stripped = line.strip()

        if stripped.startswith("*"):
            out_lines.append(line)
            continue

        skip = False
        for net, (src_node, _, _) in targets.items():
            if (stripped.startswith(f"V_rail_{src_node}") or
                stripped.startswith(f"V_sense_{src_node}") or
                stripped.startswith(f"R_rail_{src_node}")):
                skip = True
                break
        if skip:
            out_lines.append(line)
            continue

        new_line = line
        for net, (src_node, load_node, _) in targets.items():
            # \b ensures we match whole identifiers only (not substrings
            # like "N5v_SENSE" or "V_sense_N5v")
            new_line = re.sub(
                rf"\b{re.escape(src_node)}\b",
                load_node,
                new_line
            )
        out_lines.append(new_line)

    # === Pass 2: insert R_trace elements before .end ===
    final_lines = []
    inserted = False
    for line in out_lines:
        if not inserted and line.strip().startswith(".end"):
            final_lines.append("")
            final_lines.append("* === Trace resistance (from KiCad copper geometry) ===")
            for net, (src_node, load_node, r) in targets.items():
                final_lines.append(
                    f"R_trace_{src_node} {src_node} {load_node} {r:.6f}"
                )
            final_lines.append("")
            inserted = True
        final_lines.append(line)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(final_lines))

    print(f"[+] Wrote {output_path}")
    return True


if __name__ == "__main__":
    sys.exit(0 if inject(INPUT_CIR, OUTPUT_CIR) else 1)
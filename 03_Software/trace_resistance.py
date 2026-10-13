"""
Compute DC resistance of every copper trace in the PCB.
Reads pcb_geometry.json (produced by pcb_extract_geometry.py).
Writes net_resistance.json with per-net resistance topology.

Runs in SYSTEM Python — no pcbnew dependency.
"""
import json
import math

# === CONFIGURABLE PHYSICS CONSTANTS ==================================
# Change these two numbers to match your PCB stack-up.

COPPER_WEIGHT_OZ  = 1        # 1 oz, 2 oz, etc.
LAYER_COUNT       = 2        # 2-layer, 4-layer board

# Copper resistivity at 20°C (ohm·meter). Do NOT change unless doing
# exotic simulations. For temperature effects, we'd add alpha later.
COPPER_RESISTIVITY = 1.72e-8

# =====================================================================

# Derived: 1 oz copper = 35 µm thick
OZ_TO_METERS        = 35e-6
COPPER_THICKNESS_M  = COPPER_WEIGHT_OZ * OZ_TO_METERS

GEOMETRY_JSON = "pcb_geometry.json"
OUTPUT_JSON   = "net_resistance.json"


def trace_resistance(x1, y1, x2, y2, width_mm):
    """DC resistance of a straight copper segment, in ohms."""
    dx = x2 - x1
    dy = y2 - y1
    length_m = math.sqrt(dx * dx + dy * dy) / 1000.0
    width_m  = width_mm / 1000.0
    if width_m <= 0 or length_m <= 0:
        return 0.0
    area_m2 = width_m * COPPER_THICKNESS_M
    return COPPER_RESISTIVITY * length_m / area_m2


def main():
    with open(GEOMETRY_JSON) as f:
        geom = json.load(f)

    tracks    = geom.get("tracks", [])
    vias      = geom.get("vias", [])
    footprints = geom.get("footprints", [])
    board_layers = geom.get("outline", {}).get("layer_count", LAYER_COUNT)

    print(f"[*] Board: {board_layers} layers, {COPPER_WEIGHT_OZ} oz copper")
    print(f"[*] Copper thickness: {COPPER_THICKNESS_M*1e6:.1f} µm")
    print(f"[*] Analyzing {len(tracks)} tracks, {len(vias)} vias")

    # --- 1. Per-trace resistance ---
    per_net = {}
    total_r = 0.0
    total_len_mm = 0.0
    min_r = float("inf")
    max_r = 0.0

    for t in tracks:
        r = trace_resistance(t["x1"], t["y1"], t["x2"], t["y2"], t["width_mm"])
        seg_len = math.hypot(t["x2"] - t["x1"], t["y2"] - t["y1"])
        total_len_mm += seg_len
        total_r += r
        min_r = min(min_r, r)
        max_r = max(max_r, r)

        net = t.get("net_name") or "<unnamed>"
        if net not in per_net:
            per_net[net] = {
                "segments": [],
                "total_resistance_ohm": 0.0,
                "total_length_mm": 0.0,
                "layers_used": set(),
            }
        entry = per_net[net]
        entry["segments"].append({
            "layer": t["layer"],
            "x1": t["x1"], "y1": t["y1"],
            "x2": t["x2"], "y2": t["y2"],
            "width_mm": t["width_mm"],
            "length_mm": seg_len,
            "resistance_ohm": r,
        })
        entry["total_resistance_ohm"] += r
        entry["total_length_mm"] += seg_len
        entry["layers_used"].add(t["layer"])

    # Finalize sets -> lists for JSON
    for net, entry in per_net.items():
        entry["layers_used"] = sorted(entry["layers_used"])
        entry["segment_count"] = len(entry["segments"])

    # --- 2. Per-pad net map ---
    pad_nets = {}
    for fp in footprints:
        ref = fp["ref"]
        for pad in fp.get("pads", []):
            net = pad.get("net_name") or "<unnamed>"
            if net not in pad_nets:
                pad_nets[net] = []
            pad_nets[net].append({
                "ref": ref,
                "pad": pad.get("pad_name", "?"),
                "x_mm": pad["x_mm"],
                "y_mm": pad["y_mm"],
            })

    # --- 3. Vias (each via is ~0.5 mOhm for standard sizes; approximate) ---
    # A via ~ 25 µm plated wall, 0.3 mm drill, 0.6 mm pad.
    # Barrel resistance formula: R = rho * L / (2*pi*r*t)
    # Approx: 0.5 mOhm per via, but compute properly:
    via_resistances = []
    for v in vias:
        drill_m   = v["drill_mm"] / 1000.0
        barrel_len_m = (board_layers - 1) * 1.6e-3   # board thickness / layers
        r_barrel = drill_m / 2.0 - 25e-6              # subtract plating
        circumference = 2 * math.pi * r_barrel
        plating_t = 25e-6
        area_m2 = circumference * plating_t
        r = COPPER_RESISTIVITY * barrel_len_m / area_m2 if area_m2 > 0 else 0.0
        via_resistances.append(r)

    if via_resistances:
        avg_via_r = sum(via_resistances) / len(via_resistances)
    else:
        avg_via_r = 0.0

    # --- 4. Output ---
    output = {
        "config": {
            "copper_weight_oz": COPPER_WEIGHT_OZ,
            "copper_thickness_um": COPPER_THICKNESS_M * 1e6,
            "layer_count": board_layers,
            "copper_resistivity_ohm_m": COPPER_RESISTIVITY,
        },
        "summary": {
            "track_count": len(tracks),
            "via_count": len(vias),
            "net_count": len(per_net),
            "total_trace_length_mm": total_len_mm,
            "total_trace_resistance_ohm": total_r,
            "min_segment_resistance_ohm": min_r if min_r != float("inf") else 0.0,
            "max_segment_resistance_ohm": max_r,
            "avg_via_resistance_ohm": avg_via_r,
        },
        "nets": per_net,
        "pad_nets": pad_nets,
    }

    with open(OUTPUT_JSON, "w") as f:
        json.dump(output, f, indent=2)

    # --- 5. Print top-10 highest-resistance nets ---
    sorted_nets = sorted(
        per_net.items(),
        key=lambda kv: kv[1]["total_resistance_ohm"],
        reverse=True,
    )
    print(f"\n[+] Total trace length: {total_len_mm:.1f} mm")
    print(f"[+] Total trace copper resistance: {total_r:.4f} Ω")
    print(f"[+] Avg via resistance: {avg_via_r*1000:.3f} mΩ")
    print(f"\n[+] Top 10 nets by total resistance:")
    for net, entry in sorted_nets[:10]:
        print(f"    {net:40s}  "
              f"{entry['total_resistance_ohm']*1000:8.3f} mΩ  "
              f"({entry['segment_count']:3d} segs, "
              f"{entry['total_length_mm']:7.1f} mm)")

    print(f"\n[+] Wrote {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
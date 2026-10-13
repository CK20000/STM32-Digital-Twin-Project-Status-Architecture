"""
Live PCB overlay: Vector KiCad render with animated LED states.
Renders directly from pcb_geometry.json (no PNG loading, high FPS).
Subscribes to ZeroMQ for firmware LED states.
"""
import json
import math
import threading
import matplotlib
matplotlib.use("TkAgg")
import zmq
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Polygon
from matplotlib.collections import LineCollection, PatchCollection
from matplotlib.animation import FuncAnimation

GEOMETRY_JSON = "pcb_geometry.json"
ZMQ_SUB_PORT  = 5557

# --- KiCad Aesthetic Palette ---
C_BOARD_BG   = "#1a3a1a"   # KiCad green solder mask
C_BOARD_EDGE = "#66bb66"
C_TRACE_F    = "#c9a44c"   # Front copper (gold/amber)
C_TRACE_B    = "#3a5a8a"   # Back copper (dim blue)
C_PAD        = "#d4af37"   # Gold pads
C_BODY       = "#1e1e1e"   # IC body dark gray
C_BODY_EDGE  = "#3a3a3a"
C_SILK       = "#e8e8e8"   # Silkscreen white
C_REFDES     = "#ffffff"
C_BG         = "#0a0a0a"

LED_MAP = {
    "D3": 0, "D5": 2, "D6": 3,
    "D7": 4, "D8": 5, "D9": 6,
}

state = {"leds": {}, "packet": 0, "adc": [0] * 8}


def chain_lines_into_polygon(line_shapes, tol=0.05):
    if not line_shapes:
        return None
    segs = [(s["x1"], s["y1"], s["x2"], s["y2"]) for s in line_shapes]
    pts = [(segs[0][0], segs[0][1]), (segs[0][2], segs[0][3])]
    used = {0}
    while len(used) < len(segs):
        extended = False
        for i, (x1, y1, x2, y2) in enumerate(segs):
            if i in used:
                continue
            tx, ty = pts[-1]
            if abs(tx - x1) < tol and abs(ty - y1) < tol:
                pts.append((x2, y2)); used.add(i); extended = True; break
            if abs(tx - x2) < tol and abs(ty - y2) < tol:
                pts.append((x1, y1)); used.add(i); extended = True; break
            hx, hy = pts[0]
            if abs(hx - x1) < tol and abs(hy - y1) < tol:
                pts.insert(0, (x2, y2)); used.add(i); extended = True; break
            if abs(hx - x2) < tol and abs(hy - y2) < tol:
                pts.insert(0, (x1, y1)); used.add(i); extended = True; break
        if not extended:
            break
    return pts if len(pts) >= 3 else None


def rotated_rect_corners(cx, cy, w, h, angle_deg):
    rad = math.radians(-angle_deg)
    ca, sa = math.cos(rad), math.sin(rad)
    hw, hh = w / 2, h / 2
    return [(cx + dx * ca - dy * sa, cy + dx * sa + dy * ca)
            for dx, dy in [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]]


def zmq_listener():
    import struct
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:{ZMQ_SUB_PORT}")
    sub.setsockopt_string(zmq.SUBSCRIBE, "OBS")
    print(f"[*] Viewer subscribed to ZMQ port {ZMQ_SUB_PORT} (OBS)")

    while True:
        try:
            msg = sub.recv_string()
            parts = msg.split(":", 2)
            if len(parts) != 3:
                continue
            _, name, hex_str = parts
            raw = bytes.fromhex(hex_str)

            if name.startswith("obs_v_d") and name.endswith("_drv"):
                ref = name[6:-4].upper()
                state["leds"][ref] = raw[0]
                state["packet"] += 1
            elif name.startswith("obs_v_in"):
                idx_str = name[8:]
                if idx_str.isdigit():
                    idx = int(idx_str) - 1
                    if 0 <= idx < 8:
                        state["adc"][idx] = struct.unpack("<H", raw)[0]
        except Exception as e:
            print(f"[!] viewer: {e}")


def main():
    with open(GEOMETRY_JSON) as f:
        geom = json.load(f)

    o = geom["outline"]
    bx, by, bw, bh = o["x_mm"], o["y_mm"], o["w_mm"], o["h_mm"]

    led_positions = {}
    for fp in geom["footprints"]:
        ref = fp["ref"]
        if ref in LED_MAP:
            led_positions[ref] = (fp["x_mm"], fp["y_mm"])

    threading.Thread(target=zmq_listener, daemon=True).start()

    fig, ax = plt.subplots(figsize=(10, 12), dpi=120)
    fig.patch.set_facecolor(C_BG)
    ax.set_facecolor(C_BG)
    fig.subplots_adjust(left=0.22, right=0.90, top=0.93, bottom=0.05)

    # ============================================================
    # VECTOR KICAD BOARD RENDER (Replaces PNG image)
    # ============================================================
    # Layer 0: Green Solder Mask Board Outline
    ax.add_patch(Rectangle((bx, by), bw, bh, facecolor=C_BOARD_BG, edgecolor=C_BOARD_EDGE, lw=1.5, zorder=0))

    # Layer 1: Back Copper Traces
    b_segs, b_widths = [], []
    for t in geom["tracks"]:
        if t["layer"] == "B_Cu":
            b_segs.append([(t["x1"], t["y1"]), (t["x2"], t["y2"])])
            b_widths.append(max(t["width_mm"] * 4, 0.5))
    if b_segs:
        ax.add_collection(LineCollection(b_segs, linewidths=b_widths, colors=C_TRACE_B, alpha=0.6, capstyle="round", zorder=1))

    # Layer 2: Front Copper Traces
    f_segs, f_widths = [], []
    for t in geom["tracks"]:
        if t["layer"] == "F_Cu":
            f_segs.append([(t["x1"], t["y1"]), (t["x2"], t["y2"])])
            f_widths.append(max(t["width_mm"] * 4, 0.5))
    if f_segs:
        ax.add_collection(LineCollection(f_segs, linewidths=f_widths, colors=C_TRACE_F, alpha=0.85, capstyle="round", zorder=2))

    # Layer 3: Vias
    via_patches = [Circle((v["x_mm"], v["y_mm"]), radius=v["diameter_mm"] / 2) for v in geom.get("vias", [])]
    if via_patches:
        ax.add_collection(PatchCollection(via_patches, facecolor="#888888", edgecolor="#444444", linewidths=0.3, zorder=3))

    # Layer 4: Pads
    pad_patches = []
    for fp in geom["footprints"]:
        for p in fp.get("pads", []):
            angle = p.get("angle_deg", 0.0)
            if abs(angle) < 0.01:
                pad_patches.append(Rectangle((p["x_mm"] - p["w_mm"] / 2, p["y_mm"] - p["h_mm"] / 2), p["w_mm"], p["h_mm"]))
            else:
                pad_patches.append(Polygon(rotated_rect_corners(p["x_mm"], p["y_mm"], p["w_mm"], p["h_mm"], angle), closed=True))
    if pad_patches:
        ax.add_collection(PatchCollection(pad_patches, facecolor=C_PAD, edgecolor="#7a6318", linewidths=0.3, zorder=6))

    # Layer 5: Component Bodies
    body_patches = []
    for fp in geom["footprints"]:
        fab = [s for s in fp.get("fab_shapes", []) if s["type"] != "text"]
        lines_ = [s for s in fab if s["type"] == "line"]
        others = [s for s in fab if s["type"] != "line"]
        if lines_:
            pts = chain_lines_into_polygon(lines_)
            if pts:
                body_patches.append(Polygon(pts, closed=True))
        for sh in others:
            if sh["type"] == "rect":
                x0 = min(sh["x1"], sh["x2"]); y0 = min(sh["y1"], sh["y2"])
                body_patches.append(Rectangle((x0, y0), abs(sh["x2"] - sh["x1"]), abs(sh["y2"] - sh["y1"])))
            elif sh["type"] == "circle":
                body_patches.append(Circle((sh["cx"], sh["cy"]), radius=sh["r"]))
            elif sh["type"] == "poly":
                body_patches.append(Polygon(sh["points"], closed=True))
    if body_patches:
        ax.add_collection(PatchCollection(body_patches, facecolor=C_BODY, edgecolor=C_BODY_EDGE, linewidths=0.6, zorder=7))

    # Layer 6: Silkscreen
    silk_segs, silk_patches = [], []
    for fp in geom["footprints"]:
        for sh in fp.get("silk_shapes", []):
            t = sh["type"]
            if t == "line":
                silk_segs.append([(sh["x1"], sh["y1"]), (sh["x2"], sh["y2"])])
            elif t == "rect":
                x0 = min(sh["x1"], sh["x2"]); y0 = min(sh["y1"], sh["y2"])
                silk_patches.append(Rectangle((x0, y0), abs(sh["x2"] - sh["x1"]), abs(sh["y2"] - sh["y1"])))
            elif t == "circle":
                silk_patches.append(Circle((sh["cx"], sh["cy"]), radius=sh["r"]))
            elif t == "poly":
                silk_patches.append(Polygon(sh["points"], closed=True))
    if silk_segs:
        ax.add_collection(LineCollection(silk_segs, linewidths=0.6, colors=C_SILK, zorder=10))
    if silk_patches:
        ax.add_collection(PatchCollection(silk_patches, facecolor="none", edgecolor=C_SILK, linewidths=0.5, zorder=10))

    # Layer 7: Reference Designators
    for fp in geom["footprints"]:
        ref = fp["ref"]
        val = fp.get("value", "")
        is_main = ref.startswith(("U", "J", "SW", "Y")) or (ref.startswith("D") and "LED" in val.upper())
        if is_main:
            ax.text(fp["x_mm"], fp["y_mm"], ref, fontsize=4, ha="center", va="center", color=C_REFDES, weight="bold", bbox=dict(facecolor="#000000", alpha=0.55, edgecolor="none", pad=0.3), zorder=100)
    # ============================================================

    # Live Dynamic LED Overlay
    led_refs = list(led_positions.keys())
    xs = [led_positions[r][0] for r in led_refs]
    ys = [led_positions[r][1] for r in led_refs]

    glow_scatter = ax.scatter(xs, ys, s=300, c="lime", alpha=0.0, edgecolors="none", zorder=150)
    dot_scatter  = ax.scatter(xs, ys, s=25, c="#333333", edgecolors="white", linewidths=1.2, zorder=160)

    margin = 3
    ax.set_xlim(bx - margin, bx + bw + margin)
    ax.set_ylim(by + bh + margin, by - margin)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("STM32 PLC Digital Twin — Live LED View", color="white", fontsize=13, pad=10)

    # Status Panel anchored to Left Figure Margin
    status = fig.text(
        0.02, 0.95, "", color="white", fontsize=10,
        family="monospace", verticalalignment="top",
        bbox=dict(facecolor="#101010", alpha=0.98, pad=14, edgecolor="#666666", linewidth=1.2),
        zorder=200
    )

    def update(_frame):
        glow_colors = []
        dot_colors = []

        for ref in led_refs:
            if ref == "D3":
                on = True
            else:
                on = (state["leds"].get(ref, 0) == 1)

            if on:
                dot_colors.append("#00ff00")
                glow_colors.append((0, 1, 0, 0.35))
            else:
                dot_colors.append("#333333")
                glow_colors.append((0, 1, 0, 0.0))

        dot_scatter.set_facecolors(dot_colors)
        glow_scatter.set_facecolors(glow_colors)

        mask_str = "".join("1" if state["leds"].get(f"D{i}", 1) == 0 else "0" for i in range(9, 3, -1))
        status.set_text(
            f"Packet : {state['packet']:>7}\n"
            f"LEDs   : {mask_str}\n"
            f"ADC[0] : {state['adc'][0]}\n"
            f"ADC[1] : {state['adc'][1]}\n"
            f"ADC[7] : {state['adc'][7]}"
        )
        return [glow_scatter, dot_scatter]

    ani = FuncAnimation(fig, update, interval=200, blit=False, cache_frame_data=False)
    plt.show()


if __name__ == "__main__":
    main()
"""
Live current-flow view (optimized).
  - Traces colored by current magnitude (log scale)
  - Animated dots traveling along each trace, direction & speed ∝ current
  - Hover a trace to see net-level current details
"""
import json
import math
import threading
import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Polygon
from matplotlib.collections import LineCollection, PatchCollection
from matplotlib.colors import LogNorm
from matplotlib.cm import ScalarMappable
from matplotlib.animation import FuncAnimation

import zmq

# === CONFIG ===
ZMQ_SUB_PORT = 5556
GEOMETRY_JSON = "pcb_geometry.json"

C_BODY      = "#1e1e1e"
C_BODY_EDGE = "#3a3a3a"
C_PAD       = "#d4af37"
C_PAD_EDGE  = "#7a6318"
C_SILK      = "#e8e8e8"
C_REFDES    = "#ffffff"
C_BG        = "#050505"
C_TRACE_DIM = "#1a1a1a"

state = {
    "currents": {},
    "dout": {},          # KiCad net -> 0 / 1 (firmware-commanded output state)
    "packet": 0,
    "hovering": False,
}


def trace_resistance(x1, y1, x2, y2, width_mm):
    length_m = math.hypot(x2 - x1, y2 - y1) / 1000.0
    width_m  = width_mm / 1000.0
    if width_m <= 0 or length_m <= 0:
        return 0.0
    return 1.72e-8 * length_m / (width_m * 35e-6)


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
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:{ZMQ_SUB_PORT}")
    sub.setsockopt_string(zmq.SUBSCRIBE, "CURRENT")
    sub.setsockopt_string(zmq.SUBSCRIBE, "DOUT")
    print(f"[*] Current viewer subscribed to ZMQ {ZMQ_SUB_PORT} (CURRENT, DOUT)")
    while True:
        try:
            msg = sub.recv_string()
            parts = msg.split(":", 2)
            if len(parts) != 3:
                continue
            topic, net, val = parts
            if topic == "CURRENT":
                state["currents"][net] = float(val)
                state["packet"] += 1
            elif topic == "DOUT":
                state["dout"][net] = int(val)
        except Exception as e:
            print(f"[!] viewer zmq: {e}")


def main():
    with open(GEOMETRY_JSON) as f:
        geom = json.load(f)

    o = geom["outline"]
    bx, by, bw, bh = o["x_mm"], o["y_mm"], o["w_mm"], o["h_mm"]
    tracks = geom["tracks"]

    for t in tracks:
        t["_r"] = trace_resistance(t["x1"], t["y1"], t["x2"], t["y2"], t["width_mm"])
        t["_len"] = math.hypot(t["x2"] - t["x1"], t["y2"] - t["y1"])
        t["_net"] = t.get("net_name") or "<unnamed>"

    tracks_by_net = {}
    for i, t in enumerate(tracks):
        tracks_by_net.setdefault(t["_net"], []).append(i)

    threading.Thread(target=zmq_listener, daemon=True).start()

    fig, ax = plt.subplots(figsize=(14, 12), dpi=130)
    fig.patch.set_facecolor(C_BG)
    ax.set_facecolor(C_BG)
    fig.subplots_adjust(left=0.22, right=0.90, top=0.93, bottom=0.05)

    # === STATIC: all traces, dim gray, one LineCollection ===
    static_segs = []
    static_widths = []
    for t in tracks:
        static_segs.append([(t["x1"], t["y1"]), (t["x2"], t["y2"])])
        static_widths.append(max(t["width_mm"] * 4, 0.5))
    ax.add_collection(LineCollection(
        static_segs, linewidths=static_widths, colors=C_TRACE_DIM,
        capstyle="projecting", zorder=1))

    # === LIVE: colored traces, one LineCollection, updated per frame ===
    # zorder=180 puts the current-colored traces ABOVE everything else:
    # pads (6), bodies (7), silkscreen (10), and refdes (100).
    live_lc = LineCollection([], linewidths=[],
                             capstyle="projecting", zorder=180)
    ax.add_collection(live_lc)

    # === LIVE: outline colors for digital outputs (industrial + motor) ===
    # AMBER = firmware HIGH (armed, no field load attached yet)
    # SLATE = firmware LOW (idle — net is a digital output, not driving)
    # Sits just below the current-colored traces so real currents always
    # win when a field load is attached.
    ARMED_COLOR = "#ffb733"
    IDLE_COLOR = "#4a5568"
    armed_lc = LineCollection([], linewidths=[],
                              capstyle="round", alpha=0.9, zorder=175)
    ax.add_collection(armed_lc)

    # === STATIC: vias ===
    via_patches = [
        Circle((v["x_mm"], v["y_mm"]), radius=v["diameter_mm"] / 2)
        for v in geom.get("vias", [])
    ]
    if via_patches:
        ax.add_collection(PatchCollection(
            via_patches, facecolor="#666666", edgecolor="#222222",
            linewidths=0.2, zorder=3))

    # === STATIC: pads ===
    pad_patches = []
    for fp in geom["footprints"]:
        for p in fp.get("pads", []):
            angle = p.get("angle_deg", 0.0)
            if abs(angle) < 0.01:
                pad_patches.append(Rectangle(
                    (p["x_mm"] - p["w_mm"] / 2, p["y_mm"] - p["h_mm"] / 2),
                    p["w_mm"], p["h_mm"]))
            else:
                pad_patches.append(Polygon(
                    rotated_rect_corners(
                        p["x_mm"], p["y_mm"], p["w_mm"], p["h_mm"], angle),
                    closed=True))
    if pad_patches:
        ax.add_collection(PatchCollection(
            pad_patches, facecolor=C_PAD, edgecolor=C_PAD_EDGE,
            linewidths=0.3, zorder=6))

    # === STATIC: component bodies ===
    body_patches = []
    for fp in geom["footprints"]:
        fab_geo = [s for s in fp.get("fab_shapes", []) if s["type"] != "text"]
        lines_ = [s for s in fab_geo if s["type"] == "line"]
        others = [s for s in fab_geo if s["type"] != "line"]
        if lines_:
            pts = chain_lines_into_polygon(lines_)
            if pts:
                body_patches.append(Polygon(pts, closed=True))
        for sh in others:
            if sh["type"] == "rect":
                x0 = min(sh["x1"], sh["x2"]); y0 = min(sh["y1"], sh["y2"])
                body_patches.append(Rectangle(
                    (x0, y0), abs(sh["x2"] - sh["x1"]),
                    abs(sh["y2"] - sh["y1"])))
            elif sh["type"] == "circle":
                body_patches.append(Circle((sh["cx"], sh["cy"]),
                                            radius=sh["r"]))
            elif sh["type"] == "poly":
                body_patches.append(Polygon(sh["points"], closed=True))
    if body_patches:
        ax.add_collection(PatchCollection(
            body_patches, facecolor=C_BODY, edgecolor=C_BODY_EDGE,
            linewidths=0.6, zorder=7))

    # === STATIC: silkscreen lines (one LineCollection) ===
    silk_segs = []
    for fp in geom["footprints"]:
        for sh in fp.get("silk_shapes", []):
            if sh["type"] == "line":
                silk_segs.append([(sh["x1"], sh["y1"]), (sh["x2"], sh["y2"])])
    if silk_segs:
        ax.add_collection(LineCollection(
            silk_segs, linewidths=0.6, colors=C_SILK, zorder=10))

    # === STATIC: silkscreen shapes (one PatchCollection) ===
    silk_patches = []
    for fp in geom["footprints"]:
        for sh in fp.get("silk_shapes", []):
            t = sh["type"]
            if t == "rect":
                x0 = min(sh["x1"], sh["x2"]); y0 = min(sh["y1"], sh["y2"])
                silk_patches.append(Rectangle(
                    (x0, y0), abs(sh["x2"] - sh["x1"]),
                    abs(sh["y2"] - sh["y1"])))
            elif t == "circle":
                silk_patches.append(Circle((sh["cx"], sh["cy"]),
                                            radius=sh["r"]))
            elif t == "poly":
                silk_patches.append(Polygon(sh["points"], closed=True))
    if silk_patches:
        ax.add_collection(PatchCollection(
            silk_patches, facecolor="none", edgecolor=C_SILK,
            linewidths=0.5, zorder=10))

    # === STATIC: refdes text ===
    for fp in geom["footprints"]:
        ref = fp["ref"]
        if not (ref.startswith(("U", "J", "SW", "Y")) or ref.startswith("D")):
            continue
        ax.text(fp["x_mm"], fp["y_mm"], ref,
                fontsize=4, ha="center", va="center",
                color=C_REFDES, weight="bold",
                bbox=dict(facecolor="#000000", alpha=0.55,
                          edgecolor="none", pad=0.3),
                zorder=100)

    ax.set_xlim(bx - 3, bx + bw + 3)
    ax.set_ylim(by + bh + 3, by - 3)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("PCB Current Flow View (per-net currents from ngspice)",
                 color="white", fontsize=13, pad=14)

    # === Info panel ===
    info = fig.text(
        0.02, 0.95, "",
        color="white", fontsize=10, family="monospace",
        verticalalignment="top",
        bbox=dict(facecolor="#101010", alpha=0.98, pad=14,
                  edgecolor="#666666", linewidth=1.2),
        zorder=200
    )
    info.set_text(
        "Waiting for currents...\n\n"
        "Topic: CURRENT on ZMQ 5556\n"
        "Restart pyspice_solver.py if empty."
    )

    # === Hover highlight ===
    highlight_lines = []

    def clear_highlight():
        for ln in highlight_lines:
            ln.remove()
        highlight_lines.clear()

    mid_cache = np.array([[(t["x1"] + t["x2"]) / 2, (t["y1"] + t["y2"]) / 2]
                          for t in tracks])
    mid_nets = [t["_net"] for t in tracks]

    # === Per-frame update ===
    # === Per-frame update ===
    def update(_frame):
        currents = state["currents"]

        # Compute log normalization once per frame
        abs_vals = [abs(v) for v in currents.values() if v != 0]
        if abs_vals:
            i_min = max(min(abs_vals) * 0.5, 1e-12)
            i_max = max(abs_vals) * 1.1
            norm = LogNorm(vmin=i_min, vmax=i_max)
        else:
            norm = None

        cmap = plt.cm.plasma

        # Colored traces — one pass, one set_* call per property
        segs, widths, colors = [], [], []
        for net, i_amps in currents.items():
            if i_amps == 0 or net not in tracks_by_net:
                continue
            color = cmap(norm(abs(i_amps))) if norm else (0.5, 0.5, 0.5, 1)
            for idx in tracks_by_net[net]:
                t = tracks[idx]
                segs.append([(t["x1"], t["y1"]), (t["x2"], t["y2"])])
                widths.append(max(t["width_mm"] * 4, 0.6))
                colors.append(color)
        live_lc.set_segments(segs)
        live_lc.set_linewidths(widths)
        live_lc.set_colors(colors)

        # Digital output outlines: drawn for both states so a digital-output
        # trace is always identifiable, whether it is driving or idle.
        # Real current takes priority — a lit trace uses the plasma map.
        armed_segs, armed_widths, armed_colors = [], [], []
        for net, st in state["dout"].items():
            if net not in tracks_by_net:
                continue
            if currents.get(net, 0.0) != 0.0:
                continue
            color = ARMED_COLOR if st == 1 else IDLE_COLOR
            for idx in tracks_by_net[net]:
                t = tracks[idx]
                armed_segs.append([(t["x1"], t["y1"]), (t["x2"], t["y2"])])
                armed_widths.append(max(t["width_mm"] * 4 + 1.0, 1.4))
                armed_colors.append(color)
        armed_lc.set_segments(armed_segs)
        armed_lc.set_linewidths(armed_widths)
        armed_lc.set_colors(armed_colors)

        # Info panel (skip if hovering — on_move owns the text)
        if not state["hovering"]:
            n_armed = sum(1 for st in state["dout"].values() if st == 1)
            if currents or state["dout"]:
                n_nets = len(currents)
                total_abs = sum(abs(v) for v in currents.values())
                info.set_text(
                    f"Packet  : {state['packet']}\n"
                    f"Active  : {n_nets} nets\n"
                    f"Armed   : {n_armed} outputs\n"
                    f"Sum |I| : {total_abs * 1000:.3f} mA\n\n"
                    f"Hover a trace\n"
                    f"for details."
                )
            else:
                info.set_text("Waiting for currents...\n\n"
                              "Topic: CURRENT on ZMQ 5556")

        return [live_lc, armed_lc]

    # === Hover ===
    def on_move(event):
        if event.inaxes != ax or event.xdata is None:
            state["hovering"] = False
            clear_highlight()
            fig.canvas.draw_idle()
            return
        dx = mid_cache[:, 0] - event.xdata
        dy = mid_cache[:, 1] - event.ydata
        d2 = dx * dx + dy * dy
        idx = int(np.argmin(d2))
        if math.sqrt(d2[idx]) > 5.0:
            state["hovering"] = False
            clear_highlight()
            fig.canvas.draw_idle()
            return

        state["hovering"] = True
        net = mid_nets[idx]
        i_amps = state["currents"].get(net, 0.0)

        segs_idx = tracks_by_net.get(net, [])
        total_r = sum(tracks[i]["_r"] for i in segs_idx)
        v_drop = abs(i_amps) * total_r if total_r > 0 else 0.0

        clear_highlight()
        for i in segs_idx:
            t = tracks[i]
            ln, = ax.plot([t["x1"], t["x2"]], [t["y1"], t["y2"]],
                          color="white",
                          linewidth=max(t["width_mm"] * 4 + 1.2, 2.0),
                          alpha=0.95, zorder=160, solid_capstyle="round")
            highlight_lines.append(ln)

        display = net if len(net) <= 38 else net[:35] + "..."

        dout_state = state["dout"].get(net)
        if dout_state is not None:
            state_str = ("ARMED (firmware HIGH)" if dout_state == 1
                         else "IDLE (firmware LOW)")
            if i_amps == 0.0:
                i_line = "0.0000 mA  (no field load)"
            else:
                i_line = f"{i_amps * 1000:+.4f} mA"
            info.set_text(
                f"Net    : {display}\n"
                f"State  : {state_str}\n"
                f"Current: {i_line}\n"
                f"Segs   : {len(segs_idx)}\n"
                f"TotalR : {total_r * 1000:.4f} mΩ\n"
                f"V drop : {v_drop * 1000:.4f} mV"
            )
        else:
            info.set_text(
                f"Net    : {display}\n"
                f"Current: {i_amps * 1000:+.4f} mA\n"
                f"        ({i_amps:+.3e} A)\n"
                f"Segs   : {len(segs_idx)}\n"
                f"TotalR : {total_r * 1000:.4f} mΩ\n"
                f"V drop : {v_drop * 1000:.4f} mV"
            )
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("motion_notify_event", on_move)

    ani = FuncAnimation(fig, update, interval=200, blit=False,
                        cache_frame_data=False)

    plt.show(block=True)


if __name__ == "__main__":
    main()
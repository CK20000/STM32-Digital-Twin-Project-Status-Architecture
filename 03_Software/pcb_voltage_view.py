"""
Live voltage view (mirrors pcb_current_view.py).
Colors every copper trace by its ngspice DC node voltage.
Subscribes to NODE:<kicad_net>:<volts> on ZMQ 5556.
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
from matplotlib.colors import Normalize
from matplotlib.cm import ScalarMappable
from matplotlib.animation import FuncAnimation

import zmq

# === CONFIG ===
ZMQ_SUB_PORT = 5556
GEOMETRY_JSON = "pcb_geometry.json"

# Fixed voltage range — board spans 0 V (GND) to 12 V (wall input).
V_MIN = 0.0
V_MAX = 12.0
VMAP = plt.cm.viridis

C_BODY      = "#1e1e1e"
C_BODY_EDGE = "#3a3a3a"
C_PAD       = "#d4af37"
C_PAD_EDGE  = "#7a6318"
C_SILK      = "#e8e8e8"
C_REFDES    = "#ffffff"
C_BG        = "#050505"
C_TRACE_DIM = "#1a1a1a"

state = {
    "voltages": {},
    "packet": 0,
    "hovering": False,
}


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
    sub.setsockopt_string(zmq.SUBSCRIBE, "NODE")
    print(f"[*] Voltage viewer subscribed to ZMQ {ZMQ_SUB_PORT} (NODE)")
    while True:
        try:
            msg = sub.recv_string()
            parts = msg.split(":", 2)
            if len(parts) != 3:
                continue
            _, net, volts_str = parts
            state["voltages"][net] = float(volts_str)
            state["packet"] += 1
        except Exception as e:
            print(f"[!] voltage viewer zmq: {e}")


def main():
    with open(GEOMETRY_JSON) as f:
        geom = json.load(f)

    o = geom["outline"]
    bx, by, bw, bh = o["x_mm"], o["y_mm"], o["w_mm"], o["h_mm"]
    tracks = geom["tracks"]

    for t in tracks:
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

    # === STATIC: dim all traces ===
    static_segs, static_widths = [], []
    for t in tracks:
        static_segs.append([(t["x1"], t["y1"]), (t["x2"], t["y2"])])
        static_widths.append(max(t["width_mm"] * 4, 0.5))
    ax.add_collection(LineCollection(
        static_segs, linewidths=static_widths, colors=C_TRACE_DIM,
        capstyle="projecting", zorder=1))

    # === LIVE: voltage-colored traces ===
    live_lc = LineCollection([], linewidths=[],
                             capstyle="projecting", zorder=180)
    ax.add_collection(live_lc)

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

    # === STATIC: silkscreen ===
    silk_segs = []
    for fp in geom["footprints"]:
        for sh in fp.get("silk_shapes", []):
            if sh["type"] == "line":
                silk_segs.append([(sh["x1"], sh["y1"]), (sh["x2"], sh["y2"])])
    if silk_segs:
        ax.add_collection(LineCollection(
            silk_segs, linewidths=0.6, colors=C_SILK, zorder=10))

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

    # === STATIC: refdes ===
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
    ax.set_title("PCB Voltage View (per-net DC voltage from ngspice)",
                 color="white", fontsize=13, pad=14)

    # === Colorbar ===
    norm = Normalize(vmin=V_MIN, vmax=V_MAX)
    sm = ScalarMappable(norm=norm, cmap=VMAP)
    sm.set_array([])
    cbar = plt.colorbar(sm, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("Net voltage (V, linear 0-12)", color="white")
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.get_yticklabels(), color="white")

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
        "Waiting for node voltages...\n\n"
        "Topic: NODE on ZMQ 5556\n"
        "Restart pyspice_solver.py if empty."
    )

    # === Hover ===
    mid_cache = np.array([[(t["x1"] + t["x2"]) / 2, (t["y1"] + t["y2"]) / 2]
                          for t in tracks])
    mid_nets = [t["_net"] for t in tracks]
    highlight_lines = []

    def clear_highlight():
        for ln in highlight_lines:
            ln.remove()
        highlight_lines.clear()

    def update(_frame):
        voltages = state["voltages"]

        segs, widths, colors = [], [], []
        for net, v in voltages.items():
            if net not in tracks_by_net:
                continue
            v_clamped = max(V_MIN, min(V_MAX, v))
            color = VMAP(norm(v_clamped))
            for idx in tracks_by_net[net]:
                t = tracks[idx]
                segs.append([(t["x1"], t["y1"]), (t["x2"], t["y2"])])
                widths.append(max(t["width_mm"] * 4, 0.6))
                colors.append(color)
        live_lc.set_segments(segs)
        live_lc.set_linewidths(widths)
        live_lc.set_colors(colors)

        if not state["hovering"]:
            if voltages:
                n_nets = len(voltages)
                vmin = min(voltages.values())
                vmax = max(voltages.values())
                info.set_text(
                    f"Packet  : {state['packet']}\n"
                    f"Active  : {n_nets} nets\n"
                    f"V_min   : {vmin:7.4f} V\n"
                    f"V_max   : {vmax:7.4f} V\n\n"
                    f"Hover a trace\n"
                    f"for details."
                )
            else:
                info.set_text("Waiting for node voltages...\n\n"
                              "Topic: NODE on ZMQ 5556")
        return [live_lc]

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
        v = state["voltages"].get(net, None)

        segs_idx = tracks_by_net.get(net, [])
        total_len = sum(tracks[i]["_len"] for i in segs_idx)

        clear_highlight()
        for i in segs_idx:
            t = tracks[i]
            ln, = ax.plot([t["x1"], t["x2"]], [t["y1"], t["y2"]],
                          color="white",
                          linewidth=max(t["width_mm"] * 4 + 1.2, 2.0),
                          alpha=0.95, zorder=160, solid_capstyle="round")
            highlight_lines.append(ln)

        display = net if len(net) <= 38 else net[:35] + "..."
        if v is None:
            v_line = "no node data"
        else:
            v_line = f"{v:+.6f} V"
        info.set_text(
            f"Net    : {display}\n"
            f"Voltage: {v_line}\n"
            f"Segs   : {len(segs_idx)}\n"
            f"Length : {total_len:.2f} mm"
        )
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("motion_notify_event", on_move)

    ani = FuncAnimation(fig, update, interval=200, blit=False,
                        cache_frame_data=False)

    plt.show(block=True)


if __name__ == "__main__":
    main()
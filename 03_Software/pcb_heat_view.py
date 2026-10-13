"""
PCB Power Dissipation — Thermal View.
Uses PatchCollection + LineCollection for fast redraws.
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
OUTPUT_PNG = "pcb_heat_view.png"

C_BODY_DARK = (0.094, 0.094, 0.094, 1.0)
C_BODY_EDGE = "#3a3a3a"
C_PAD       = "#d4af37"
C_PAD_EDGE  = "#7a6318"
C_SILK      = "#e8e8e8"
C_REFDES    = "#ffffff"
C_BG        = "#050505"
C_TRACE     = "#101010"

P_MIN = 0.0
P_MAX = 0.35

state = {
    "component_power": {},
    "packet_power": 0,
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
    sub.setsockopt_string(zmq.SUBSCRIBE, "POWER")
    print(f"[*] Heat viewer subscribed to ZMQ {ZMQ_SUB_PORT} (POWER)")
    while True:
        try:
            msg = sub.recv_string()
            _, ref, watts = msg.split(":", 2)
            state["component_power"][ref] = float(watts)
            state["packet_power"] += 1
        except Exception as e:
            print(f"[!] heat zmq: {e}")


def main():
    with open(GEOMETRY_JSON) as f:
        geom = json.load(f)

    o = geom["outline"]
    bx, by, bw, bh = o["x_mm"], o["y_mm"], o["w_mm"], o["h_mm"]

    foot_pos = {fp["ref"]: (fp["x_mm"], fp["y_mm"]) for fp in geom["footprints"]}

    threading.Thread(target=zmq_listener, daemon=True).start()

    fig, ax = plt.subplots(figsize=(14, 12), dpi=130)
    fig.patch.set_facecolor(C_BG)
    ax.set_facecolor(C_BG)
    fig.subplots_adjust(left=0.20, right=0.90, top=0.93, bottom=0.05)

    # === TRACES (LineCollection) ===
    segs, widths = [], []
    for t in geom["tracks"]:
        segs.append([(t["x1"], t["y1"]), (t["x2"], t["y2"])])
        widths.append(max(t["width_mm"] * 4, 0.5))
    ax.add_collection(LineCollection(
        segs, linewidths=widths, colors=C_TRACE,
        capstyle="round", zorder=1))

    # === VIAS (PatchCollection) ===
    via_patches = [
        Circle((v["x_mm"], v["y_mm"]), radius=v["diameter_mm"] / 2)
        for v in geom.get("vias", [])
    ]
    if via_patches:
        ax.add_collection(PatchCollection(
            via_patches, facecolor="#555555", edgecolor="#222222",
            linewidths=0.2, zorder=3))

    # === PADS (PatchCollection) ===
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
    ax.add_collection(PatchCollection(
        pad_patches, facecolor=C_PAD, edgecolor=C_PAD_EDGE,
        linewidths=0.3, zorder=5))

    # === BODIES (PatchCollection + parallel ref list) ===
    body_patches, body_refs = [], []
    for fp in geom["footprints"]:
        ref = fp["ref"]
        fab_geo = [s for s in fp.get("fab_shapes", []) if s["type"] != "text"]
        line_shapes = [s for s in fab_geo if s["type"] == "line"]
        other_shapes = [s for s in fab_geo if s["type"] != "line"]

        if line_shapes:
            pts = chain_lines_into_polygon(line_shapes)
            if pts:
                body_patches.append(Polygon(pts, closed=True))
                body_refs.append(ref)

        for sh in other_shapes:
            t = sh["type"]
            if t == "rect":
                x0 = min(sh["x1"], sh["x2"])
                y0 = min(sh["y1"], sh["y2"])
                body_patches.append(Rectangle(
                    (x0, y0), abs(sh["x2"] - sh["x1"]),
                    abs(sh["y2"] - sh["y1"])))
                body_refs.append(ref)
            elif t == "circle":
                body_patches.append(Circle(
                    (sh["cx"], sh["cy"]), radius=sh["r"]))
                body_refs.append(ref)
            elif t == "poly":
                body_patches.append(Polygon(sh["points"], closed=True))
                body_refs.append(ref)

    body_pc = PatchCollection(
        body_patches, facecolor=list(C_BODY_DARK),
        edgecolor=C_BODY_EDGE, linewidths=0.6, zorder=7)
    ax.add_collection(body_pc)

    # Precompute base RGBA for dark bodies
    N_BODIES = len(body_patches)
    colors_array = np.tile(
        np.array(C_BODY_DARK, dtype=np.float32), (N_BODIES, 1))

    # === SILKSCREEN lines (LineCollection) ===
    silk_segs = []
    for fp in geom["footprints"]:
        for sh in fp.get("silk_shapes", []):
            if sh["type"] == "line":
                silk_segs.append([(sh["x1"], sh["y1"]), (sh["x2"], sh["y2"])])
    if silk_segs:
        ax.add_collection(LineCollection(
            silk_segs, linewidths=0.6, colors=C_SILK, zorder=10))

    # === SILKSCREEN other (PatchCollection) ===
    silk_patches = []
    for fp in geom["footprints"]:
        for sh in fp.get("silk_shapes", []):
            t = sh["type"]
            if t == "rect":
                x0 = min(sh["x1"], sh["x2"])
                y0 = min(sh["y1"], sh["y2"])
                silk_patches.append(Rectangle(
                    (x0, y0), abs(sh["x2"] - sh["x1"]),
                    abs(sh["y2"] - sh["y1"])))
            elif t == "circle":
                silk_patches.append(Circle(
                    (sh["cx"], sh["cy"]), radius=sh["r"]))
            elif t == "poly":
                silk_patches.append(Polygon(sh["points"], closed=True))
    if silk_patches:
        ax.add_collection(PatchCollection(
            silk_patches, facecolor="none", edgecolor=C_SILK,
            linewidths=0.4, zorder=10))

    # === REFDES (Text) ===
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
    ax.set_title("PCB Power Dissipation — Thermal View",
                 color="white", fontsize=13, pad=14)

    # === Colorbar ===
    p_norm = Normalize(vmin=P_MIN, vmax=P_MAX)
    p_cmap = plt.cm.jet
    sm = ScalarMappable(norm=p_norm, cmap=p_cmap)
    sm.set_array([])
    cbar = plt.colorbar(sm, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("Power dissipation (W, linear)", color="white")
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.get_yticklabels(), color="white")

    # === Info panel ===
    info = fig.text(0.02, 0.95, "",
                    color="white", fontsize=10, family="monospace",
                    verticalalignment="top",
                    bbox=dict(facecolor="#101010", alpha=0.95, pad=14,
                              edgecolor="#666666", linewidth=1.2),
                    zorder=200)
    info.set_text("Waiting for POWER data...")

    # === Hover ===
    comp_refs = [fp["ref"] for fp in geom["footprints"]]
    comp_xy = np.array([foot_pos[r] for r in comp_refs])

    def on_move(event):
        if event.inaxes != ax or event.xdata is None:
            state["hovering"] = False
            return
        dx = comp_xy[:, 0] - event.xdata
        dy = comp_xy[:, 1] - event.ydata
        d2 = dx * dx + dy * dy
        idx = int(np.argmin(d2))
        if math.sqrt(d2[idx]) > 2.5:
            state["hovering"] = False
            return
        state["hovering"] = True
        ref = comp_refs[idx]
        p = state["component_power"].get(ref, 0.0)
        info.set_text(
            f"Component : {ref}\n"
            f"Power     : {p*1e3:>10.4f} mW\n"
            f"          = {p:.6e} W"
        )
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("motion_notify_event", on_move)

    # === Fast per-frame update: one set_facecolors call ===
    def update(_frame):
        powers = state["component_power"]
        # Build color array in one pass
        for i, ref in enumerate(body_refs):
            p = powers.get(ref, 0.0)
            if p > 1e-6:
                colors_array[i] = p_cmap(p_norm(min(p, P_MAX)))
            else:
                colors_array[i] = C_BODY_DARK
        body_pc.set_facecolors(colors_array)

        if not state["hovering"]:
            n_active = sum(1 for p in powers.values() if p > 1e-6)
            total_p = sum(powers.values())
            info.set_text(
                f"Packet  : {state['packet_power']}\n"
                f"Active  : {n_active} components\n"
                f"Sum P   : {total_p*1e3:.2f} mW\n\n"
                f"Hover a component\n"
                f"for details."
            )
        return []

    ani = FuncAnimation(fig, update, interval=200, blit=False,
                        cache_frame_data=False)

    plt.savefig(OUTPUT_PNG, dpi=130, bbox_inches="tight",
                facecolor=C_BG, pad_inches=0.15)
    print(f"[+] Saved static -> {OUTPUT_PNG}")
    plt.show(block=True)


if __name__ == "__main__":
    main()
"""
Combined resistance view:
  - Traces colored by DC resistance (log scale)
  - Component bodies (F.Fab), pads, silkscreen, refdes on top
  - No green board fill — pure dark background
  - Hover a trace to see its NET-level resistance (end-to-end)
"""
import json
import math
import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Polygon
from matplotlib.colors import LogNorm
from matplotlib.cm import ScalarMappable

# === CONFIGURABLE PHYSICS ===
COPPER_WEIGHT_OZ   = 1
COPPER_RESISTIVITY = 1.72e-8
OZ_TO_METERS       = 35e-6
COPPER_THICKNESS_M = COPPER_WEIGHT_OZ * OZ_TO_METERS

GEOMETRY_JSON = "pcb_geometry.json"
OUTPUT_PNG    = "pcb_resistance_view.png"

C_BODY      = "#1e1e1e"
C_BODY_EDGE = "#3a3a3a"
C_PAD       = "#d4af37"
C_PAD_EDGE  = "#7a6318"
C_SILK      = "#e8e8e8"
C_REFDES    = "#ffffff"
C_BG        = "#0a0a0a"


def trace_resistance(x1, y1, x2, y2, width_mm):
    length_m = math.hypot(x2 - x1, y2 - y1) / 1000.0
    width_m  = width_mm / 1000.0
    if width_m <= 0 or length_m <= 0:
        return 0.0
    return COPPER_RESISTIVITY * length_m / (width_m * COPPER_THICKNESS_M)


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


def main():
    with open(GEOMETRY_JSON) as f:
        geom = json.load(f)

    o = geom["outline"]
    bx, by, bw, bh = o["x_mm"], o["y_mm"], o["w_mm"], o["h_mm"]
    tracks = geom["tracks"]

    # --- Compute per-segment resistance ---
    for t in tracks:
        t["_r"] = trace_resistance(t["x1"], t["y1"], t["x2"], t["y2"], t["width_mm"])

    valid_r = [t["_r"] for t in tracks if t["_r"] > 0]
    r_min, r_max = min(valid_r), max(valid_r)
    norm = LogNorm(vmin=r_min * 0.9, vmax=r_max * 1.1)
    cmap = plt.cm.turbo

    # ============================================================
    # --- AGGREGATE RESISTANCE PER NET (end-to-end) ---
    # ============================================================
    net_stats = {}       # net_name -> { total_r, total_len, segment_count, layers }
    net_segments = {}    # net_name -> list of track indices (for highlight)
    for i, t in enumerate(tracks):
        net = t.get("net_name") or "<unnamed>"
        if net not in net_stats:
            net_stats[net] = {
                "total_r": 0.0,
                "total_len_mm": 0.0,
                "segment_count": 0,
                "layers": set(),
            }
            net_segments[net] = []
        net_stats[net]["total_r"] += t["_r"]
        net_stats[net]["total_len_mm"] += math.hypot(
            t["x2"] - t["x1"], t["y2"] - t["y1"])
        net_stats[net]["segment_count"] += 1
        net_stats[net]["layers"].add(t["layer"])
        net_segments[net].append(i)

    for net in net_stats:
        net_stats[net]["layers"] = sorted(net_stats[net]["layers"])

    # --- Figure ---
    fig, ax = plt.subplots(figsize=(14, 12), dpi=130)
    fig.patch.set_facecolor(C_BG)
    ax.set_facecolor(C_BG)
    # Reserve 22% of figure width on the LEFT for the info panel.
    # right=0.90 leaves room for the colorbar.
    fig.subplots_adjust(left=0.22, right=0.90, top=0.93, bottom=0.05)

    # --- Colored traces ---
    for t in tracks:
        if t["_r"] <= 0:
            continue
        color = cmap(norm(t["_r"]))
        lw = max(t["width_mm"] * 4, 0.5)
        ax.plot(
            [t["x1"], t["x2"]], [t["y1"], t["y2"]],
            color=color, linewidth=lw,
            solid_capstyle="round", alpha=0.95, zorder=2
        )

    # --- Vias ---
    for v in geom.get("vias", []):
        ax.add_patch(Circle(
            (v["x_mm"], v["y_mm"]), radius=v["diameter_mm"] / 2,
            facecolor="#666666", edgecolor="#222222",
            lw=0.2, zorder=3
        ))

    # --- Pads ---
    for fp in geom["footprints"]:
        for p in fp.get("pads", []):
            angle = p.get("angle_deg", 0.0)
            if abs(angle) < 0.01:
                ax.add_patch(Rectangle(
                    (p["x_mm"] - p["w_mm"] / 2, p["y_mm"] - p["h_mm"] / 2),
                    p["w_mm"], p["h_mm"],
                    facecolor=C_PAD, edgecolor=C_PAD_EDGE,
                    lw=0.3, zorder=6
                ))
            else:
                corners = rotated_rect_corners(
                    p["x_mm"], p["y_mm"], p["w_mm"], p["h_mm"], angle
                )
                ax.add_patch(Polygon(
                    corners, closed=True,
                    facecolor=C_PAD, edgecolor=C_PAD_EDGE,
                    lw=0.3, zorder=6
                ))

    # --- Component bodies ---
    for fp in geom["footprints"]:
        fab_geo = [s for s in fp.get("fab_shapes", []) if s["type"] != "text"]
        line_shapes = [s for s in fab_geo if s["type"] == "line"]
        other_shapes = [s for s in fab_geo if s["type"] != "line"]

        filled = False
        if line_shapes:
            pts = chain_lines_into_polygon(line_shapes)
            if pts:
                ax.add_patch(Polygon(
                    pts, closed=True,
                    facecolor=C_BODY, edgecolor=C_BODY_EDGE,
                    lw=0.6, zorder=7
                ))
                filled = True
        if not filled:
            for sh in line_shapes:
                ax.plot(
                    [sh["x1"], sh["x2"]], [sh["y1"], sh["y2"]],
                    color=C_BODY_EDGE, linewidth=0.5, zorder=7
                )
        for sh in other_shapes:
            t = sh["type"]
            if t == "rect":
                x0 = min(sh["x1"], sh["x2"])
                y0 = min(sh["y1"], sh["y2"])
                ax.add_patch(Rectangle(
                    (x0, y0), abs(sh["x2"] - sh["x1"]), abs(sh["y2"] - sh["y1"]),
                    facecolor=C_BODY, edgecolor=C_BODY_EDGE, lw=0.6, zorder=7
                ))
            elif t == "circle":
                ax.add_patch(Circle(
                    (sh["cx"], sh["cy"]), radius=sh["r"],
                    facecolor=C_BODY, edgecolor=C_BODY_EDGE, lw=0.6, zorder=7
                ))
            elif t == "poly":
                ax.add_patch(Polygon(
                    sh["points"], closed=True,
                    facecolor=C_BODY, edgecolor=C_BODY_EDGE, lw=0.6, zorder=7
                ))

    # --- Silkscreen ---
    for fp in geom["footprints"]:
        for sh in fp.get("silk_shapes", []):
            t = sh["type"]
            if t == "line":
                ax.plot(
                    [sh["x1"], sh["x2"]], [sh["y1"], sh["y2"]],
                    color=C_SILK, linewidth=0.7, zorder=10
                )
            elif t == "rect":
                x0 = min(sh["x1"], sh["x2"])
                y0 = min(sh["y1"], sh["y2"])
                ax.add_patch(Rectangle(
                    (x0, y0), abs(sh["x2"] - sh["x1"]), abs(sh["y2"] - sh["y1"]),
                    facecolor="none", edgecolor=C_SILK, lw=0.5, zorder=10
                ))
            elif t == "circle":
                ax.add_patch(Circle(
                    (sh["cx"], sh["cy"]), radius=sh["r"],
                    facecolor="none", edgecolor=C_SILK, lw=0.5, zorder=10
                ))
            elif t == "poly":
                ax.add_patch(Polygon(
                    sh["points"], closed=True,
                    facecolor="none", edgecolor=C_SILK, lw=0.5, zorder=10
                ))

    # --- Refdes ---
    for fp in geom["footprints"]:
        ref = fp["ref"]
        if not (ref.startswith(("U", "J", "SW", "Y")) or ref.startswith("D")):
            continue
        ax.text(
            fp["x_mm"], fp["y_mm"], ref,
            fontsize=4, ha="center", va="center",
            color=C_REFDES, weight="bold",
            bbox=dict(facecolor="#000000", alpha=0.6, edgecolor="none", pad=0.3),
            zorder=100
        )

    # --- Colorbar ---
    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cbar = plt.colorbar(sm, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("Trace DC resistance (Ω per segment)",
                   color="white")
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.get_yticklabels(), color="white")

    # --- Limits + title ---
    ax.set_xlim(bx - 3, bx + bw + 3)
    ax.set_ylim(by + bh + 3, by - 3)
    ax.set_aspect("equal")
    ax.axis("off")

    # FIX: title with proper padding so it doesn't get clipped
    ax.set_title(
        "PCB Trace Resistance View (1 oz copper)",
        color="white", fontsize=13, pad=14
    )

    # ============================================================
    # --- HOVER INTERACTIVITY (NET-LEVEL) ---
    # ============================================================
    mids = np.array([[(t["x1"] + t["x2"]) / 2, (t["y1"] + t["y2"]) / 2]
                     for t in tracks])
    mid_nets = [t.get("net_name", "") or "<unnamed>" for t in tracks]

    # Info panel lives in FIGURE coordinates, in the reserved left strip.
    # (0,0) = bottom-left of figure, (1,1) = top-right.
    info = fig.text(
        0.02, 0.95,
        "Net    : --\n"
        "Layers : --\n"
        "Segs   : --\n"
        "Length : -- mm\n"
        "Total R: -- mΩ",
        color="white", fontsize=10, family="monospace",
        verticalalignment="top",
        bbox=dict(facecolor="#101010", alpha=0.98, pad=14,
                  edgecolor="#666666", linewidth=1.2),
        zorder=200
    )
    info.set_visible(True)

    # Single highlight line — we'll replace data when hovering a new net
    highlight_lines = []

    def clear_highlight():
        for ln in highlight_lines:
            ln.remove()
        highlight_lines.clear()

    def draw_net_highlight(net_name):
        clear_highlight()
        for idx in net_segments.get(net_name, []):
            t = tracks[idx]
            ln, = ax.plot(
                [t["x1"], t["x2"]], [t["y1"], t["y2"]],
                color="white", linewidth=max(t["width_mm"] * 4 + 1.2, 2.0),
                alpha=0.95, zorder=150, solid_capstyle="round"
            )
            highlight_lines.append(ln)

    def on_move(event):
        if event.inaxes != ax or event.xdata is None:
            if info.get_visible():
                info.set_visible(False)
                clear_highlight()
                fig.canvas.draw_idle()
            return

        dx = mids[:, 0] - event.xdata
        dy = mids[:, 1] - event.ydata
        d2 = dx * dx + dy * dy
        idx = int(np.argmin(d2))

        if math.sqrt(d2[idx]) > 5.0:
            if info.get_visible():
                info.set_visible(False)
                clear_highlight()
                fig.canvas.draw_idle()
            return

        net = mid_nets[idx]
        stats = net_stats[net]

        # Format layers string
        layers_str = ", ".join(stats["layers"]) if stats["layers"] else "?"

        # Truncate very long net names for readability
        display_name = net if len(net) <= 42 else net[:39] + "..."

        text = (
            f"Net    : {display_name}\n"
            f"Layers : {layers_str}\n"
            f"Segs   : {stats['segment_count']}\n"
            f"Length : {stats['total_len_mm']:.2f} mm\n"
            f"Total R: {stats['total_r']*1000:.4f} mΩ\n"
            f"        ({stats['total_r']:.6f} Ω)"
        )
        info.set_text(text)
        info.set_visible(True)

        draw_net_highlight(net)
        fig.canvas.draw_idle()

    fig.canvas.mpl_connect("motion_notify_event", on_move)

    # Save static version
    plt.savefig(OUTPUT_PNG, dpi=130, bbox_inches="tight",
                facecolor=C_BG, pad_inches=0.15)
    print(f"[+] Saved static render -> {OUTPUT_PNG}")
    print(f"[*] Segment R range: {r_min*1000:.4f} mΩ .. {r_max*1000:.4f} mΩ")

    # Print top 10 nets by end-to-end resistance
    sorted_nets = sorted(net_stats.items(),
                         key=lambda kv: kv[1]["total_r"], reverse=True)
    print("\n[*] Top 10 nets by total resistance:")
    for net, st in sorted_nets[:10]:
        print(f"    {net:45s}  "
              f"{st['total_r']*1000:9.4f} mΩ  "
              f"({st['segment_count']:3d} segs, "
              f"{st['total_len_mm']:7.1f} mm)")

    fig.canvas.draw_idle()
    plt.show(block=True)


if __name__ == "__main__":
    main()
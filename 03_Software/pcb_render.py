"""
Reads pcb_geometry.json, renders a KiCad-style top view.
No pcbnew dependency — runs in system Python.
"""
import json
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Polygon
import matplotlib.image as _mpimg
import math

GEOMETRY_JSON = "pcb_geometry.json"
OUTPUT_PNG = "pcb_background.png"

# --- Colors (KiCad 3D viewer palette) ---
C_BOARD_BG = "#1a3a1a"
C_BOARD_EDGE = "#66bb66"
C_TRACE_F = "#c9a44c"
C_TRACE_B = "#3a5a8a"
C_PAD = "#d4af37"
C_BODY = "#1e1e1e"        # component body (dark gray)
C_BODY_EDGE = "#3a3a3a"
C_SILK = "#e8e8e8"        # silkscreen white
C_REFDES = "#ffffff"

def chain_lines_into_polygon(line_shapes, tol=0.05):
    """Given a list of line shape dicts, chain endpoints into an ordered polygon."""
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
    """Return 4 corners of a rectangle rotated around its center."""
    # KiCad angles are counterclockwise, matplotlib Y is inverted
    rad = math.radians(-angle_deg)
    ca, sa = math.cos(rad), math.sin(rad)
    hw, hh = w / 2, h / 2
    corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
    return [(cx + dx * ca - dy * sa, cy + dx * sa + dy * ca)
            for dx, dy in corners]

def render():
    with open(GEOMETRY_JSON) as f:
        data = json.load(f)

    o = data["outline"]
    bx, by, bw, bh = o["x_mm"], o["y_mm"], o["w_mm"], o["h_mm"]

    fig, ax = plt.subplots(figsize=(10, 12), dpi=150)
    fig.patch.set_facecolor("#0a0a0a")
    ax.set_facecolor("#0a0a0a")

    # ---------- Layer 0: board ----------
    ax.add_patch(Rectangle(
        (bx, by), bw, bh,
        facecolor=C_BOARD_BG, edgecolor=C_BOARD_EDGE,
        lw=1.5, zorder=0
    ))

    # ---------- Layer 1: back copper (dimmer) ----------
    for t in data["tracks"]:
        if t["layer"] != "B_Cu":
            continue
        ax.plot(
            [t["x1"], t["x2"]], [t["y1"], t["y2"]],
            color=C_TRACE_B, linewidth=max(t["width_mm"] * 4, 0.5),
            solid_capstyle="round", alpha=0.6, zorder=1
        )

    # ---------- Layer 2: front copper ----------
    for t in data["tracks"]:
        if t["layer"] != "F_Cu":
            continue
        ax.plot(
            [t["x1"], t["x2"]], [t["y1"], t["y2"]],
            color=C_TRACE_F, linewidth=max(t["width_mm"] * 4, 0.5),
            solid_capstyle="round", alpha=0.85, zorder=2
        )

    # ---------- Layer 3: vias ----------
    for v in data.get("vias", []):
        ax.add_patch(Circle(
            (v["x_mm"], v["y_mm"]),
            radius=v["diameter_mm"] / 2,
            facecolor="#888888", edgecolor="#444444",
            lw=0.3, zorder=3
        ))

    # ---------- Layer 4: component bodies from F.Fab ----------
    for fp in data["footprints"]:
        fab_geo = [s for s in fp.get("fab_shapes", []) if s["type"] != "text"]

        # Separate lines from other primitives
        line_shapes = [s for s in fab_geo if s["type"] == "line"]
        other_shapes = [s for s in fab_geo if s["type"] != "line"]

        # Strategy: try to chain lines into a closed polygon and fill it
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

        # Fallback: draw the individual lines (thin, for non-closed outlines)
        if not filled:
            for sh in line_shapes:
                ax.plot(
                    [sh["x1"], sh["x2"]], [sh["y1"], sh["y2"]],
                    color=C_BODY_EDGE, linewidth=max(sh.get("width", 0.15) * 3, 0.4),
                    zorder=7
                )

        # Draw any non-line primitives (rects, circles, polys)
        for sh in other_shapes:
            t = sh["type"]
            if t == "rect":
                x0 = min(sh["x1"], sh["x2"])
                y0 = min(sh["y1"], sh["y2"])
                ax.add_patch(Rectangle(
                    (x0, y0), abs(sh["x2"] - sh["x1"]), abs(sh["y2"] - sh["y1"]),
                    facecolor=C_BODY if sh.get("filled") else "none",
                    edgecolor=C_BODY_EDGE, lw=0.6, zorder=7
                ))
            elif t == "circle":
                ax.add_patch(Circle(
                    (sh["cx"], sh["cy"]), radius=sh["r"],
                    facecolor=C_BODY if sh.get("filled") else "none",
                    edgecolor=C_BODY_EDGE, lw=0.6, zorder=7
                ))
            elif t == "poly":
                ax.add_patch(Polygon(
                    sh["points"], closed=True,
                    facecolor=C_BODY, edgecolor=C_BODY_EDGE,
                    lw=0.6, zorder=7
                ))

    # ---------- Layer 5: pads (rotation-aware) ----------
    for fp in data["footprints"]:
        for p in fp["pads"]:
            angle = p.get("angle_deg", 0.0)
            if abs(angle) < 0.01:
                # Fast path: axis-aligned
                ax.add_patch(Rectangle(
                    (p["x_mm"] - p["w_mm"] / 2, p["y_mm"] - p["h_mm"] / 2),
                    p["w_mm"], p["h_mm"],
                    facecolor=C_PAD, edgecolor="#7a6318",
                    lw=0.3, zorder=6
                ))
            else:
                corners = rotated_rect_corners(
                    p["x_mm"], p["y_mm"],
                    p["w_mm"], p["h_mm"], angle
                )
                ax.add_patch(Polygon(
                    corners, closed=True,
                    facecolor=C_PAD, edgecolor="#7a6318",
                    lw=0.3, zorder=6
                ))

    # ---------- Layer 6: silkscreen (lines + shapes) ----------
    for fp in data["footprints"]:
        for sh in fp.get("silk_shapes", []):
            t = sh["type"]
            if t == "line":
                ax.plot(
                    [sh["x1"], sh["x2"]], [sh["y1"], sh["y2"]],
                    color=C_SILK, linewidth=sh.get("width", 0.15) * 3,
                    zorder=10
                )
            elif t == "rect":
                x0 = min(sh["x1"], sh["x2"])
                y0 = min(sh["y1"], sh["y2"])
                w = abs(sh["x2"] - sh["x1"])
                h = abs(sh["y2"] - sh["y1"])
                ax.add_patch(Rectangle(
                    (x0, y0), w, h,
                    facecolor="none", edgecolor=C_SILK,
                    lw=0.5, zorder=10
                ))
            elif t == "circle":
                ax.add_patch(Circle(
                    (sh["cx"], sh["cy"]), radius=sh["r"],
                    facecolor="none", edgecolor=C_SILK,
                    lw=0.5, zorder=10
                ))
            elif t == "poly":
                ax.add_patch(Polygon(
                    sh["points"], closed=True,
                    facecolor="none", edgecolor=C_SILK,
                    lw=0.5, zorder=10
                ))

    # ---------- Layer 7: reference designators (topmost) ----------
    for fp in data["footprints"]:
        ref = fp["ref"]
        val = fp["value"]

        # Two-tier labeling:
        #  - Larger white text on dark bbox for ICs, connectors, LEDs, switches
        #  - Tiny text for passives (R/C/L/FB/Y)
        is_main = (
                ref.startswith(("U", "J", "SW", "Y"))
                or (ref.startswith("D") and "LED" in val.upper())
        )

        if is_main:
            ax.text(
                fp["x_mm"], fp["y_mm"], ref,
                fontsize=5, ha="center", va="center",
                color="#ffffff", weight="bold",
                bbox=dict(facecolor="#000000", alpha=0.7,
                          edgecolor="none", pad=0.35),
                zorder=100
            )
        else:
            # Small passives — label without background, small font
            ax.text(
                fp["x_mm"], fp["y_mm"], ref,
                fontsize=2.5, ha="center", va="center",
                color="#e8e8e8", weight="normal",
                zorder=100
            )

    # ---------- View ----------
    margin = 3
    ax.set_xlim(bx - margin, bx + bw + margin)
    ax.set_ylim(by + bh + margin, by - margin)
    ax.set_aspect("equal")
    ax.axis("off")

    plt.tight_layout(pad=0)
    plt.savefig(OUTPUT_PNG, dpi=150, bbox_inches="tight",
                facecolor="#0a0a0a", pad_inches=0.1)
    plt.close()

    # Save coordinate metadata so the viewer can map mm -> pixel exactly
    _img = _mpimg.imread(OUTPUT_PNG)
    _H, _W = _img.shape[:2]
    meta = {
        "xlim_mm": [bx - margin, bx + bw + margin],
        "ylim_mm_top": by - margin,
        "ylim_mm_bottom": by + bh + margin,
        "png_width_px": int(_W),
        "png_height_px": int(_H),
        "board_bbox_mm": {
            "x": bx, "y": by, "w": bw, "h": bh
        }
    }
    with open("pcb_render_meta.json", "w") as f:
        json.dump(meta, f, indent=2)
    print(f"[+] Rendered {OUTPUT_PNG}")


if __name__ == "__main__":
    render()
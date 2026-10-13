"""
PCB Thermal Field — 2D heat diffusion.
Solves steady-state: ∇·(k_eff·t·∇T) − 2h·(T − T_ambient) + Q = 0
over the board, with power sources from the live POWER topic.
"""
import json
import math
import time
import threading
import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import spsolve

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
OUTPUT_PNG = "pcb_thermal_field.png"

# === Physics constants ===
# Effective in-plane conductivity of the copper-clad board.
# Pure FR4: ~0.3 W/(m·K). Pure copper: ~385 W/(m·K).
# Boards with 30-40% copper coverage → k_eff 50-120 W/(m·K).
K_EFF_W_MK       = 25.0          # conservative estimate

BOARD_THICK_MM   = 1.6
CONV_H_W_M2K     = 10.0          # natural convection, top + bottom same
AMBIENT_C        = 25.0

GRID_DX_MM       = 1.0           # grid resolution (mm per cell)

# === Visual ===
C_BG       = "#050505"
C_PAD      = "#d4af37"
C_PAD_EDGE = "#7a6318"
C_SILK     = "#cccccc"
C_REFDES   = "#ffffff"

# Temperature color range — will be auto-tightened per solve
T_CMAP = plt.cm.jet

state = {
    "component_power": {},
    "packet_power": 0,
    "last_solve": 0.0,
    "T_field": None,       # 2D numpy array (ny, nx)
    "T_min": AMBIENT_C,
    "T_max": AMBIENT_C + 5,
    "solve_count": 0,
}


def zmq_listener():
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:{ZMQ_SUB_PORT}")
    sub.setsockopt_string(zmq.SUBSCRIBE, "POWER")
    print(f"[*] Thermal viewer subscribed to ZMQ {ZMQ_SUB_PORT} (POWER)")
    while True:
        try:
            msg = sub.recv_string()
            _, ref, watts = msg.split(":", 2)
            state["component_power"][ref] = float(watts)
            state["packet_power"] += 1
        except Exception as e:
            print(f"[!] thermal zmq: {e}")


# === Geometry helpers ===
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


def footprint_bbox(fp):
    """Return (x0, y0, x1, y1) bbox of a footprint using its pad extents."""
    xs, ys = [], []
    for p in fp.get("pads", []):
        xs.append(p["x_mm"])
        ys.append(p["y_mm"])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def build_power_grid(geom, powers, grid):
    """Distribute component power to grid cells."""
    nx = grid["nx"]; ny = grid["ny"]
    bx = grid["bx"]; by = grid["by"]
    dx = grid["dx"]

    Q = np.zeros((ny, nx), dtype=np.float64)   # power (W) per cell

    for fp in geom["footprints"]:
        ref = fp["ref"]
        p = powers.get(ref, 0.0)
        if p <= 0:
            continue
        bbox = footprint_bbox(fp)
        if bbox is None:
            continue
        x0, y0, x1, y1 = bbox

        # Pad bbox to a minimum size so small components get at least 1-2 cells
        if x1 - x0 < dx:
            cx = (x0 + x1) / 2
            x0, x1 = cx - dx / 2, cx + dx / 2
        if y1 - y0 < dx:
            cy = (y0 + y1) / 2
            y0, y1 = cy - dx / 2, cy + dx / 2

        j0 = max(0, int((x0 - bx) / dx))
        j1 = min(nx - 1, int((x1 - bx) / dx))
        i0 = max(0, int((y0 - by) / dx))
        i1 = min(ny - 1, int((y1 - by) / dx))

        n_cells = (j1 - j0 + 1) * (i1 - i0 + 1)
        if n_cells <= 0:
            continue
        per_cell = p / n_cells
        Q[i0:i1 + 1, j0:j1 + 1] += per_cell

    return Q


def solve_thermal_field(geom, powers, grid):
    """Build sparse matrix and solve for T field."""
    nx = grid["nx"]; ny = grid["ny"]
    dx_m = grid["dx"] * 1e-3     # cell size in meters
    t_m = BOARD_THICK_MM * 1e-3

    # Conductance and convection per cell
    G = K_EFF_W_MK * t_m              # W/K sheet conductance per cell
    B = 2.0 * CONV_H_W_M2K * dx_m * dx_m  # W/K convective loss per cell

    N = nx * ny
    diag = np.full(N, -(4 * G + B))
    off_x = np.full(N, G)     # x-neighbor
    off_y = np.full(N, G)     # y-neighbor

    # Fix boundary cells (no-flux BC): reduce the neighbor count
    # Top/bottom rows
    for j in range(nx):
        diag[0 * nx + j] += G       # no neighbor above
        diag[(ny - 1) * nx + j] += G  # no neighbor below
    # Left/right cols
    for i in range(ny):
        diag[i * nx + 0] += G
        diag[i * nx + (nx - 1)] += G

    # Build sparse matrix in COO format
    rows = list(range(N))                     # diagonal
    cols = list(range(N))
    vals = list(diag)

    # +x neighbors
    for i in range(ny):
        for j in range(nx - 1):
            rows.append(i * nx + j)
            cols.append(i * nx + j + 1)
            vals.append(G)
            rows.append(i * nx + j + 1)
            cols.append(i * nx + j)
            vals.append(G)

    # +y neighbors
    for i in range(ny - 1):
        for j in range(nx):
            rows.append(i * nx + j)
            cols.append((i + 1) * nx + j)
            vals.append(G)
            rows.append((i + 1) * nx + j)
            cols.append(i * nx + j)
            vals.append(G)

    M = sp.csr_matrix((vals, (rows, cols)), shape=(N, N))

    # RHS
    Q = build_power_grid(geom, powers, grid)
    Q_flat = Q.flatten()
    rhs = -Q_flat - B * AMBIENT_C

    # Solve
    T_flat = spsolve(M, rhs)
    T_field = T_flat.reshape((ny, nx))
    return T_field


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

    dx = GRID_DX_MM
    nx = int(math.ceil(bw / dx))
    ny = int(math.ceil(bh / dx))
    grid = {"nx": nx, "ny": ny, "bx": bx, "by": by, "dx": dx}

    print(f"[*] Grid: {nx} x {ny} cells ({nx*ny} total) at {dx} mm resolution")

    threading.Thread(target=zmq_listener, daemon=True).start()

    # === Figure ===
    fig, ax = plt.subplots(figsize=(14, 12), dpi=130)
    fig.patch.set_facecolor(C_BG)
    ax.set_facecolor(C_BG)
    fig.subplots_adjust(left=0.20, right=0.90, top=0.93, bottom=0.05)

    # Background image (T field), updated per solve
    im = ax.imshow(
        np.full((ny, nx), AMBIENT_C),
        extent=[bx, bx + bw, by + bh, by],
        origin="upper",
        cmap=T_CMAP,
        vmin=AMBIENT_C, vmax=AMBIENT_C + 5,
        interpolation="bilinear",
        zorder=2,
    )

    # === Silkscreen lines ===
    silk_segs = []
    for fp in geom["footprints"]:
        for sh in fp.get("silk_shapes", []):
            if sh["type"] == "line":
                silk_segs.append([(sh["x1"], sh["y1"]), (sh["x2"], sh["y2"])])
    if silk_segs:
        ax.add_collection(LineCollection(
            silk_segs, linewidths=0.5, colors=C_SILK, alpha=0.5, zorder=10))

    # === Pads (light overlay, rotation-aware) ===
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
                        p["x_mm"], p["y_mm"],
                        p["w_mm"], p["h_mm"], angle),
                    closed=True))
    if pad_patches:
        ax.add_collection(PatchCollection(
            pad_patches, facecolor=C_PAD, edgecolor=C_PAD_EDGE,
            linewidths=0.2, alpha=0.75, zorder=5))

    # === Component outlines (no fill) ===
    outline_patches = []
    for fp in geom["footprints"]:
        fab_geo = [s for s in fp.get("fab_shapes", []) if s["type"] != "text"]
        line_shapes = [s for s in fab_geo if s["type"] == "line"]
        other_shapes = [s for s in fab_geo if s["type"] != "line"]
        if line_shapes:
            pts = chain_lines_into_polygon(line_shapes)
            if pts:
                outline_patches.append(Polygon(pts, closed=True))
        for sh in other_shapes:
            if sh["type"] == "rect":
                x0 = min(sh["x1"], sh["x2"])
                y0 = min(sh["y1"], sh["y2"])
                outline_patches.append(Rectangle(
                    (x0, y0), abs(sh["x2"] - sh["x1"]),
                    abs(sh["y2"] - sh["y1"])))
            elif sh["type"] == "circle":
                outline_patches.append(Circle(
                    (sh["cx"], sh["cy"]), radius=sh["r"]))
            elif sh["type"] == "poly":
                outline_patches.append(Polygon(sh["points"], closed=True))
    if outline_patches:
        ax.add_collection(PatchCollection(
            outline_patches, facecolor="none", edgecolor="#000000",
            linewidths=0.8, alpha=0.85, zorder=8))

    # === Refdes ===
    for fp in geom["footprints"]:
        ref = fp["ref"]
        if not (ref.startswith(("U", "J", "SW", "Y")) or ref.startswith("D")):
            continue
        ax.text(fp["x_mm"], fp["y_mm"], ref,
                fontsize=4, ha="center", va="center",
                color=C_REFDES, weight="bold",
                bbox=dict(facecolor="#000000", alpha=0.5,
                          edgecolor="none", pad=0.3),
                zorder=100)

    ax.set_xlim(bx - 3, bx + bw + 3)
    ax.set_ylim(by + bh + 3, by - 3)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(
        f"PCB Thermal Field — 2D diffusion "
        f"(k_eff={K_EFF_W_MK:.0f} W/(m·K), h={CONV_H_W_M2K:.0f} W/(m²·K), "
        f"T_amb={AMBIENT_C:.0f}°C)",
        color="white", fontsize=12, pad=14)

    # === Colorbar ===
    t_norm = Normalize(vmin=AMBIENT_C, vmax=AMBIENT_C + 20)
    sm = ScalarMappable(norm=t_norm, cmap=T_CMAP)
    sm.set_array([])
    cbar = plt.colorbar(sm, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("Board temperature (°C)", color="white")
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.get_yticklabels(), color="white")

    # === Info panel ===
    info = fig.text(0.02, 0.95, "Waiting for POWER data...",
                    color="white", fontsize=10, family="monospace",
                    verticalalignment="top",
                    bbox=dict(facecolor="#101010", alpha=0.95, pad=14,
                              edgecolor="#666666", linewidth=1.2),
                    zorder=200)

    def update(_frame):
        now = time.time()
        # Solve at most every 700 ms
        if (now - state["last_solve"] > 0.7) and state["component_power"]:
            state["last_solve"] = now
            try:
                T = solve_thermal_field(geom, state["component_power"], grid)
                state["T_field"] = T
                state["T_min"] = float(T.min())
                state["T_max"] = float(T.max())
                state["solve_count"] += 1
                im.set_data(T)
                # Auto-tighten color range for visibility
                lo = AMBIENT_C
                hi = max(AMBIENT_C + 3, T.max())
                im.set_clim(lo, hi)
                cbar.mappable.set_clim(lo, hi)
            except Exception as e:
                print(f"[!] thermal solve: {type(e).__name__}: {e}")

        if state["T_field"] is not None:
            powers = state["component_power"]
            hottest = max(powers.items(), key=lambda kv: kv[1]) if powers else None
            info.set_text(
                f"Packet    : {state['packet_power']}\n"
                f"Solves    : {state['solve_count']}\n"
                f"T_min     : {state['T_min']:.2f} °C\n"
                f"T_max     : {state['T_max']:.2f} °C\n"
                f"ΔT_max    : {state['T_max']-AMBIENT_C:.2f} °C\n"
                f"Total P   : {sum(powers.values())*1e3:.1f} mW\n"
            )
        return [im]

    ani = FuncAnimation(fig, update, interval=250, blit=False,
                        cache_frame_data=False)

    plt.savefig(OUTPUT_PNG, dpi=130, bbox_inches="tight",
                facecolor=C_BG, pad_inches=0.15)
    print(f"[+] Saved static -> {OUTPUT_PNG}")
    plt.show(block=True)


if __name__ == "__main__":
    main()
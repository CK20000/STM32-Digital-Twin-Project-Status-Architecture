"""
PCB Thermal Field v2 — 2-layer copper model.

  T_top field  (front copper)  ── via + FR4 ──  T_bot field (back copper)

- Horizontal conduction uses local copper coverage rasterized from
  F.Cu / B.Cu traces and pads (no more uniform k_eff assumption).
- Vertical coupling = FR4 substrate + vias (30x better than FR4).
- Convective losses from both surfaces to ambient.
- Pre-factorized sparse matrix → fast per-packet solves.
"""
import json
import math
import time
import threading
import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Polygon
from matplotlib.collections import LineCollection, PatchCollection
from matplotlib.colors import Normalize
from matplotlib.cm import ScalarMappable
from matplotlib.animation import FuncAnimation

import zmq

# Global PUB socket — set up in main(), used by update() to push T_XTAL
pub_thermal = None

# ===== CONFIG =====
ZMQ_SUB_PORT = 5556
ZMQ_PUB_PORT = 5559              # NEW — publishes T_XTAL for hse_analyzer
GEOMETRY_JSON = "pcb_geometry.json"
OUTPUT_PNG    = "pcb_thermal_field_v2.png"

GRID_DX_MM = 1.0

AMBIENT_C = 25.0
THERMAL_TAU_S = 10.0             # NEW — first-order lag (board thermal mass)

# Layer physics
K_CU       = 385.0     # W/(m·K)
K_FR4      = 0.30      # W/(m·K)
T_CU_MM    = 0.035     # 1 oz copper
T_BOARD_MM = 1.6       # board thickness
T_FR4_MM   = T_BOARD_MM - 2.0 * T_CU_MM

H_CONV     = 10.0      # W/(m²·K) natural convection

# Background copper coverage per layer (pours + routing not captured by traces)
# For 2-layer boards: top is signal + small pours, bottom often near-solid GND.
COV_TOP_BG = 0.35
COV_BOT_BG = 0.70

# Via thermal parameters
VIA_DIA_MM      = 0.30
VIA_PLATING_UM  = 25.0
G_PER_VIA = K_CU * (math.pi * VIA_DIA_MM * 1e-3 * VIA_PLATING_UM * 1e-6) / (T_BOARD_MM * 1e-3)

# Component package thermal resistance (junction -> board), °C/W
THETA_JB = [
    ("STM32F407",   30),
    ("ESP-12",      35),
    ("AMS1117",     40),
    ("XL1509",      35),
    ("LM358",       80),
    ("PC817",      120),
    ("6N137",      100),
    ("MAX3485",    100),
    ("W25Q",        80),
    ("1N5820",      60),
    ("SS14",       150),
    ("BAT54S",     180),
    ("FerriteBead",150),
    ("Crystal",     80),
    ("SW_Push",    200),
]
DEFAULT_THETA_JB = 150

C_BG        = "#050505"
C_PAD       = "#d4af37"
C_PAD_EDGE  = "#7a6318"
C_SILK      = "#cccccc"
C_REFDES    = "#ffffff"
T_CMAP      = plt.cm.jet

state = {
    "component_power": {},
    "packet_power":    0,
    "last_solve":      0.0,
    "T_top":           None,
    "T_bot":           None,
    "T_display": None,
    "T_min":           AMBIENT_C,
    "T_max":           AMBIENT_C + 5,
    "solve_count":     0,
    "last_solve_ms":   0.0,
    "via_cells":       0,
    "hovering":        False,   # set by on_move, respected by update()
    "t_prev_solve":    0.0,        # NEW — dt for smoothing
    "smoothed_once":   False,      # NEW — first solve bypasses filter
}


def theta_jb(ref, value):
    v = (value or "").upper()
    for sub, t in THETA_JB:
        if sub.upper() in v:
            return float(t)
    return float(DEFAULT_THETA_JB)


def rotated_rect_corners(cx, cy, w, h, angle_deg):
    rad = math.radians(-angle_deg)
    ca, sa = math.cos(rad), math.sin(rad)
    hw, hh = w / 2.0, h / 2.0
    return [(cx + dx * ca - dy * sa, cy + dx * sa + dy * ca)
            for dx, dy in [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]]


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


def zmq_listener():
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.connect(f"tcp://127.0.0.1:{ZMQ_SUB_PORT}")
    sub.setsockopt_string(zmq.SUBSCRIBE, "POWER")
    print(f"[*] Thermal v2 subscribed to ZMQ {ZMQ_SUB_PORT} (POWER)")
    while True:
        try:
            msg = sub.recv_string()
            _, ref, watts = msg.split(":", 2)
            state["component_power"][ref] = float(watts)
            state["packet_power"] += 1
        except Exception as e:
            print(f"[!] zmq: {e}")


# ============================================================
# Rasterization
# ============================================================
def rasterize_coverage(geom, layer, grid, background):
    nx, ny = grid["nx"], grid["ny"]
    bx, by, dx = grid["bx"], grid["by"], grid["dx"]
    cov = np.full((ny, nx), background, dtype=np.float64)
    step = dx / 3.0

    # Traces
    for t in geom["tracks"]:
        if t["layer"] != layer:
            continue
        x1, y1, x2, y2 = t["x1"], t["y1"], t["x2"], t["y2"]
        w = t["width_mm"]
        length = math.hypot(x2 - x1, y2 - y1)
        if length < 1e-6:
            continue
        n_steps = max(2, int(length / step))
        ds = length / n_steps
        contrib = w * ds / (dx * dx)
        for k in range(n_steps + 1):
            f = k / n_steps
            px = x1 + (x2 - x1) * f
            py = y1 + (y2 - y1) * f
            j = int((px - bx) / dx)
            i = int((py - by) / dx)
            if 0 <= i < ny and 0 <= j < nx:
                cov[i, j] = min(1.0, cov[i, j] + contrib)

    # Pads (all pads touch both layers via their plating)
    for fp in geom["footprints"]:
        for p in fp.get("pads", []):
            x0 = p["x_mm"] - p["w_mm"] / 2
            x1 = p["x_mm"] + p["w_mm"] / 2
            y0 = p["y_mm"] - p["h_mm"] / 2
            y1 = p["y_mm"] + p["h_mm"] / 2
            j0 = max(0, int((x0 - bx) / dx))
            j1 = min(nx - 1, int((x1 - bx) / dx))
            i0 = max(0, int((y0 - by) / dx))
            i1 = min(ny - 1, int((y1 - by) / dx))
            if j1 < j0 or i1 < i0:
                continue
            pad_area = p["w_mm"] * p["h_mm"]
            n_cells = (j1 - j0 + 1) * (i1 - i0 + 1)
            add = 0.5 * pad_area / (dx * dx * n_cells)
            cov[i0:i1 + 1, j0:j1 + 1] = np.minimum(
                1.0, cov[i0:i1 + 1, j0:j1 + 1] + add)

    return np.clip(cov, 0.0, 1.0)


def rasterize_vias(geom, grid):
    nx, ny = grid["nx"], grid["ny"]
    bx, by, dx = grid["bx"], grid["by"], grid["dx"]
    vc = np.zeros((ny, nx), dtype=np.int32)
    for v in geom.get("vias", []):
        j = int((v["x_mm"] - bx) / dx)
        i = int((v["y_mm"] - by) / dx)
        if 0 <= i < ny and 0 <= j < nx:
            vc[i, j] += 1
    return vc


def build_power_grid(geom, powers, grid):
    nx, ny = grid["nx"], grid["ny"]
    bx, by, dx = grid["bx"], grid["by"], grid["dx"]
    Q = np.zeros((ny, nx), dtype=np.float64)
    for fp in geom["footprints"]:
        ref = fp["ref"]
        p = powers.get(ref, 0.0)
        if p <= 0:
            continue
        xs = [pp["x_mm"] for pp in fp.get("pads", [])]
        ys = [pp["y_mm"] for pp in fp.get("pads", [])]
        if not xs:
            continue
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        if x1 - x0 < dx:
            cx = (x0 + x1) / 2; x0, x1 = cx - dx / 2, cx + dx / 2
        if y1 - y0 < dx:
            cy = (y0 + y1) / 2; y0, y1 = cy - dx / 2, cy + dx / 2
        j0 = max(0, int((x0 - bx) / dx))
        j1 = min(nx - 1, int((x1 - bx) / dx))
        i0 = max(0, int((y0 - by) / dx))
        i1 = min(ny - 1, int((y1 - by) / dx))
        n_cells = (j1 - j0 + 1) * (i1 - i0 + 1)
        if n_cells <= 0:
            continue
        Q[i0:i1 + 1, j0:j1 + 1] += p / n_cells
    return Q


# ============================================================
# Matrix assembly (once)
# ============================================================
def assemble_matrix(geom, grid):
    nx, ny = grid["nx"], grid["ny"]
    dx = grid["dx"] * 1e-3
    t_cu = T_CU_MM * 1e-3
    t_fr4 = T_FR4_MM * 1e-3

    cov_top = rasterize_coverage(geom, "F_Cu", grid, COV_TOP_BG)
    cov_bot = rasterize_coverage(geom, "B_Cu", grid, COV_BOT_BG)
    via_cnt = rasterize_vias(geom, grid)

    # Per-cell sheet conductances (W/K) — horizontal
    G_top = cov_top * K_CU * t_cu
    G_bot = cov_bot * K_CU * t_cu

    # Boundary conductances (min of two sides)
    Gx_top = np.minimum(G_top[:, :-1], G_top[:, 1:])
    Gy_top = np.minimum(G_top[:-1, :], G_top[1:, :])
    Gx_bot = np.minimum(G_bot[:, :-1], G_bot[:, 1:])
    Gy_bot = np.minimum(G_bot[:-1, :], G_bot[1:, :])

    # Vertical coupling per cell (W/K)
    G_fr4 = K_FR4 * (dx * dx) / t_fr4
    G_couple = G_fr4 + via_cnt * G_PER_VIA

    # Convection per cell per surface (W/K)
    G_conv = H_CONV * dx * dx

    N = nx * ny
    M = 2 * N
    A = sp.lil_matrix((M, M))

    def top_idx(i, j): return i * nx + j
    def bot_idx(i, j): return N + i * nx + j

    for i in range(ny):
        for j in range(nx):
            kt = top_idx(i, j)
            kb = bot_idx(i, j)
            # Top row
            diag = -G_conv - G_couple[i, j]
            if j < nx - 1:
                A[kt, top_idx(i, j + 1)] = Gx_top[i, j]; diag -= Gx_top[i, j]
            if j > 0:
                A[kt, top_idx(i, j - 1)] = Gx_top[i, j - 1]; diag -= Gx_top[i, j - 1]
            if i < ny - 1:
                A[kt, top_idx(i + 1, j)] = Gy_top[i, j]; diag -= Gy_top[i, j]
            if i > 0:
                A[kt, top_idx(i - 1, j)] = Gy_top[i - 1, j]; diag -= Gy_top[i - 1, j]
            A[kt, kb] = G_couple[i, j]
            A[kt, kt] = diag
            # Bottom row
            diag = -G_conv - G_couple[i, j]
            if j < nx - 1:
                A[kb, bot_idx(i, j + 1)] = Gx_bot[i, j]; diag -= Gx_bot[i, j]
            if j > 0:
                A[kb, bot_idx(i, j - 1)] = Gx_bot[i, j - 1]; diag -= Gx_bot[i, j - 1]
            if i < ny - 1:
                A[kb, bot_idx(i + 1, j)] = Gy_bot[i, j]; diag -= Gy_bot[i, j]
            if i > 0:
                A[kb, bot_idx(i - 1, j)] = Gy_bot[i - 1, j]; diag -= Gy_bot[i - 1, j]
            A[kb, kt] = G_couple[i, j]
            A[kb, kb] = diag

    return A.tocsc(), G_conv, cov_top, cov_bot, via_cnt, G_couple


def main():
    with open(GEOMETRY_JSON) as f:
        geom = json.load(f)

    o = geom["outline"]
    bx, by, bw, bh = o["x_mm"], o["y_mm"], o["w_mm"], o["h_mm"]
    dx = GRID_DX_MM
    nx = int(math.ceil(bw / dx))
    ny = int(math.ceil(bh / dx))
    grid = {"nx": nx, "ny": ny, "bx": bx, "by": by, "dx": dx}
    N = nx * ny
    M = 2 * N

    print(f"[*] Grid: {nx} x {ny} = {N} cells  ({M} unknowns, 2-layer)")

    A, G_conv, cov_top, cov_bot, via_cnt, G_couple = assemble_matrix(geom, grid)
    print(f"[*] Matrix: {A.nnz} nonzeros  "
          f"(avg top cover {cov_top.mean()*100:.0f}%, "
          f"bot {cov_bot.mean()*100:.0f}%, "
          f"via cells {int((via_cnt>0).sum())})")

    t0 = time.time()
    lu = splu(A)
    print(f"[*] Pre-factorization: {(time.time()-t0)*1000:.0f} ms")

    theta_map = {fp["ref"]: theta_jb(fp["ref"], fp["value"])
                 for fp in geom["footprints"]}
    foot_pos = {fp["ref"]: (fp["x_mm"], fp["y_mm"]) for fp in geom["footprints"]}

    # Per-component bounding box (from pad extents) for accurate hover hit testing
    comp_bboxes = {}
    for fp in geom["footprints"]:
        xs = [p["x_mm"] for p in fp.get("pads", [])]
        ys = [p["y_mm"] for p in fp.get("pads", [])]
        if not xs:
            continue
        comp_bboxes[fp["ref"]] = (min(xs), min(ys), max(xs), max(ys))

    comp_refs = list(foot_pos.keys())
    comp_xy = np.array([foot_pos[r] for r in comp_refs])
    comp_bbox_arr = np.array([comp_bboxes.get(r, (
        foot_pos[r][0], foot_pos[r][1],
        foot_pos[r][0], foot_pos[r][1]
    )) for r in comp_refs])

    threading.Thread(target=zmq_listener, daemon=True).start()

    # NEW — PUB socket so hse_analyzer.py can read the crystal temp
    global pub_thermal
    ctx_pub = zmq.Context()
    pub_thermal = ctx_pub.socket(zmq.PUB)
    pub_thermal.bind(f"tcp://*:{ZMQ_PUB_PORT}")
    print(f"[*] Publishing T_XTAL on ZMQ {ZMQ_PUB_PORT}")

    # NEW — cache Y1 (crystal) grid index for publishing
    y1_fp = next((f for f in geom["footprints"] if f["ref"] == "Y1"), None)
    if y1_fp is not None:
        jj_y1 = int((y1_fp["x_mm"] - bx) / dx)
        ii_y1 = int((y1_fp["y_mm"] - by) / dx)
        if not (0 <= ii_y1 < ny and 0 <= jj_y1 < nx):
            ii_y1 = jj_y1 = -1
    else:
        ii_y1 = jj_y1 = -1

    # ===== Figure =====
    fig, ax = plt.subplots(figsize=(14, 12), dpi=130)
    fig.patch.set_facecolor(C_BG)
    ax.set_facecolor(C_BG)
    fig.subplots_adjust(left=0.20, right=0.90, top=0.93, bottom=0.05)

    # Background = top layer temperature field
    im = ax.imshow(
        np.full((ny, nx), AMBIENT_C),
        extent=[bx, bx + bw, by + bh, by],
        origin="upper", cmap=T_CMAP,
        vmin=AMBIENT_C, vmax=AMBIENT_C + 5,
        interpolation="bilinear", zorder=2)

    # Copper coverage overlay (subtle)
    # Show traces as dim highlights so the "copper highways" are visible
    silk_segs = []
    for fp in geom["footprints"]:
        for sh in fp.get("silk_shapes", []):
            if sh["type"] == "line":
                silk_segs.append([(sh["x1"], sh["y1"]), (sh["x2"], sh["y2"])])
    if silk_segs:
        ax.add_collection(LineCollection(
            silk_segs, linewidths=0.4, colors=C_SILK, alpha=0.35, zorder=10))

    pad_patches = []
    for fp in geom["footprints"]:
        for p in fp.get("pads", []):
            a = p.get("angle_deg", 0.0)
            if abs(a) < 0.01:
                pad_patches.append(Rectangle(
                    (p["x_mm"]-p["w_mm"]/2, p["y_mm"]-p["h_mm"]/2),
                    p["w_mm"], p["h_mm"]))
            else:
                pad_patches.append(Polygon(
                    rotated_rect_corners(p["x_mm"], p["y_mm"],
                                         p["w_mm"], p["h_mm"], a),
                    closed=True))
    if pad_patches:
        ax.add_collection(PatchCollection(
            pad_patches, facecolor=C_PAD, edgecolor=C_PAD_EDGE,
            linewidths=0.2, alpha=0.7, zorder=5))

    outline_patches = []
    for fp in geom["footprints"]:
        fab = [s for s in fp.get("fab_shapes", []) if s["type"] != "text"]
        lines_ = [s for s in fab if s["type"] == "line"]
        others = [s for s in fab if s["type"] != "line"]
        if lines_:
            pts = chain_lines_into_polygon(lines_)
            if pts:
                outline_patches.append(Polygon(pts, closed=True))
        for sh in others:
            if sh["type"] == "rect":
                x0 = min(sh["x1"], sh["x2"]); y0 = min(sh["y1"], sh["y2"])
                outline_patches.append(Rectangle(
                    (x0, y0), abs(sh["x2"]-sh["x1"]), abs(sh["y2"]-sh["y1"])))
            elif sh["type"] == "circle":
                outline_patches.append(Circle((sh["cx"], sh["cy"]),
                                              radius=sh["r"]))
    if outline_patches:
        ax.add_collection(PatchCollection(
            outline_patches, facecolor="none", edgecolor="#000000",
            linewidths=0.7, alpha=0.85, zorder=8))

    for fp in geom["footprints"]:
        ref = fp["ref"]
        if not (ref.startswith(("U", "J", "SW", "Y")) or ref.startswith("D")):
            continue
        ax.text(fp["x_mm"], fp["y_mm"], ref,
                fontsize=4, ha="center", va="center",
                color=C_REFDES, weight="bold",
                bbox=dict(facecolor="#000000", alpha=0.5,
                          edgecolor="none", pad=0.3), zorder=100)

    ax.set_xlim(bx-3, bx+bw+3)
    ax.set_ylim(by+bh+3, by-3)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(
        f"PCB Thermal Field v2 — 2-layer, K_Cu={K_CU:.0f} K_FR4={K_FR4} "
        f"via={G_PER_VIA*1e3:.2f} mW/K",
        color="white", fontsize=11, pad=14)

    t_norm = Normalize(vmin=AMBIENT_C, vmax=AMBIENT_C + 15)
    sm = ScalarMappable(norm=t_norm, cmap=T_CMAP)
    sm.set_array([])
    cbar = plt.colorbar(sm, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("Board temperature (°C)", color="white")
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.get_yticklabels(), color="white")

    info = fig.text(0.02, 0.95, "Waiting for POWER data...",
                    color="white", fontsize=9, family="monospace",
                    verticalalignment="top",
                    bbox=dict(facecolor="#101010", alpha=0.95, pad=14,
                              edgecolor="#666666", linewidth=1.2),
                    zorder=200)

    def on_move(event):
        # Mouse left the axes
        if event.inaxes != ax or event.xdata is None:
            state["hovering"] = False
            return

        x, y = event.xdata, event.ydata
        margin = 1.5  # mm of slack around the bbox

        # Find all components whose bbox contains the mouse (with margin)
        x0 = comp_bbox_arr[:, 0] - margin
        y0 = comp_bbox_arr[:, 1] - margin
        x1 = comp_bbox_arr[:, 2] + margin
        y1 = comp_bbox_arr[:, 3] + margin
        inside = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)

        if not inside.any():
            state["hovering"] = False
            return

        # Among candidates, pick the one whose anchor is nearest the mouse
        # (avoids picking a giant connector when a small chip is also nearby)
        candidate_idx = np.where(inside)[0]
        ddx = comp_xy[candidate_idx, 0] - x
        ddy = comp_xy[candidate_idx, 1] - y
        d2 = ddx * ddx + ddy * ddy
        i = int(candidate_idx[int(np.argmin(d2))])

        state["hovering"] = True
        ref = comp_refs[i]
        p = state["component_power"].get(ref, 0.0)
        theta = theta_map.get(ref, DEFAULT_THETA_JB)
        t_board = None
        if state["T_top"] is not None:
            fx, fy = foot_pos[ref]
            jc = int((fx - bx) / dx)
            ic = int((fy - by) / dx)
            if 0 <= ic < ny and 0 <= jc < nx:
                t_board = max(state["T_top"][ic, jc], state["T_bot"][ic, jc])
        if t_board is not None:
            tj = t_board + p * theta
            info.set_text(
                f"Component : {ref}\n"
                f"Power     : {p * 1e3:>9.3f} mW\n"
                f"θjb       : {theta:>6.0f} °C/W\n"
                f"T_board   : {t_board:>9.2f} °C\n"
                f"T_junction: {tj:>9.2f} °C\n"
                f"ΔT vs amb : {tj - AMBIENT_C:>9.2f} °C"
            )
        fig.canvas.draw_idle()

    # Wire hover events — REQUIRED or on_move never fires
    fig.canvas.mpl_connect("motion_notify_event", on_move)

    def update(_frame):
        now = time.time()
        if (now - state["last_solve"] > 0.6) and state["component_power"]:
            state["last_solve"] = now
            try:
                t_start = time.time()
                Q_top = build_power_grid(geom, state["component_power"], grid)
                rhs = np.zeros(M)
                rhs[:N] = -Q_top.flatten() - G_conv * AMBIENT_C
                rhs[N:] = -G_conv * AMBIENT_C
                T = lu.solve(rhs)
                state["last_solve_ms"] = (time.time() - t_start) * 1000
                T_top = T[:N].reshape((ny, nx))
                T_bot = T[N:].reshape((ny, nx))
                state["T_top"] = T_top
                state["T_bot"] = T_bot
                T_display = np.maximum(T_top, T_bot)
                state["solve_count"] += 1

                # NEW — first-order thermal lag. The solver is steady-state
                # so without this the field jumps instantly. TAU_S gives it
                # realistic board heat-capacity behavior.
                now2 = time.time()
                dt_solve = now2 - state["t_prev_solve"]
                state["t_prev_solve"] = now2
                if not state["smoothed_once"] or dt_solve <= 0:
                    state["T_display"] = T_display.copy()
                    state["smoothed_once"] = True
                else:
                    alpha = 1.0 - math.exp(-dt_solve / THERMAL_TAU_S)
                    state["T_display"] += alpha * (T_display - state["T_display"])

                T_show = state["T_display"]
                state["T_min"] = float(T_show.min())
                state["T_max"] = float(T_show.max())
                im.set_data(T_show)
                lo = AMBIENT_C
                # Fixed 10 °C floor on colorbar span — stops the whole cmap
                # from being squeezed into a 2 °C window when the board is cool
                hi = max(AMBIENT_C + 10.0, T_show.max() * 1.02)
                im.set_clim(lo, hi)
                cbar.mappable.set_clim(lo, hi)

                # NEW — publish T_XTAL for hse_analyzer
                if pub_thermal is not None and ii_y1 >= 0:
                    t_x = float(T_show[ii_y1, jj_y1])
                    pub_thermal.send_string(f"T_XTAL:Y1:{t_x:.3f}")
            except Exception as e:
                print(f"[!] solve: {type(e).__name__}: {e}")

        # Only overwrite the info panel when NOT hovering a component.
        # When hovering, on_move owns the text — leave it alone.
        if state["T_top"] is not None and not state["hovering"]:
            powers = state["component_power"]
            info.set_text(
                f"Packet     : {state['packet_power']}\n"
                f"Solves     : {state['solve_count']}  "
                f"({state['last_solve_ms']:.0f} ms)\n"
                f"T_min      : {state['T_min']:.2f} °C\n"
                f"T_max      : {state['T_max']:.2f} °C\n"
                f"ΔT_max     : {state['T_max'] - AMBIENT_C:.2f} °C\n"
                f"Total P    : {sum(powers.values()) * 1e3:.1f} mW\n"
                f"Layer covs : top {cov_top.mean() * 100:.0f}%, "
                f"bot {cov_bot.mean() * 100:.0f}%"
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
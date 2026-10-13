"""Tile the six Digital Twin viewer windows into a 3x2 grid,
   accounting for taskbar and title bars. Retries each move."""
import sys
import time
import pygetwindow as gw

EXPECTED = 6
TIMEOUT_S = 30
TASKBAR_PX = 48   # typical Windows 10/11 taskbar height

def get_figure_windows():
    return [w for w in gw.getAllWindows()
            if w.title.startswith("Figure")
            and w.visible and w.width > 100 and w.height > 100]

def force_geometry(win, x, y, w, h):
    for _ in range(3):
        try:
            if win.isMaximized:
                win.restore()
            win.moveTo(x, y)
            win.resizeTo(w, h)
            win.moveTo(x, y)   # second move fixes edge cases
            if (win.left == x and win.top == y
                    and win.width == w and win.height == h):
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False

def main():
    print("[*] Waiting for figure windows...")
    deadline = time.time() + TIMEOUT_S
    figs = []
    while time.time() < deadline:
        figs = get_figure_windows()
        if len(figs) >= EXPECTED:
            break
        time.sleep(0.5)

    if not figs:
        print("[!] No figure windows found.")
        sys.exit(1)

    def sort_key(w):
        try:
            return int(w.title.split()[-1])
        except Exception:
            return 999

    figs.sort(key=sort_key)
    figs = figs[:EXPECTED]
    print(f"[*] Found {len(figs)} windows, tiling...")

    try:
        import win32api, win32con
        sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
    except Exception:
        sw, sh = 1920, 1080

    usable_h = sh - TASKBAR_PX
    COLS, ROWS = 3, 2
    W = sw // COLS
    H = usable_h // ROWS

    for i, win in enumerate(figs):
        col = i % COLS
        row = i // COLS
        x, y = col * W, row * H
        ok = force_geometry(win, x, y, W, H)
        tag = "OK " if ok else "ERR"
        print(f"  [{tag}] {win.title}  ->  ({x},{y})  {W}x{H}")

    print("[+] Tiling complete.")

if __name__ == "__main__":
    main()
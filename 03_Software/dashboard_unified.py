"""
dashboard_unified.py — All-in-one Digital Twin UI.

v4:
  - Pipeline tabs tail log files (works on Windows for .NET + Python)
  - Sidebar Plots section only visible on Home tab
  - Sidebar hidden entirely on Trace Resistance and Pipeline tabs
"""
import sys, os, traceback

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "dashboard.log")
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "pipeline_logs")
os.makedirs(_LOG_DIR, exist_ok=True)

_log_fp = open(_LOG_PATH, "w", buffering=1, encoding="utf-8")
if sys.stdout is None:
    sys.stdout = _log_fp
if sys.stderr is None:
    sys.stderr = _log_fp


def _excepthook(t, v, tb):
    traceback.print_exception(t, v, tb, file=_log_fp)
    _log_fp.flush()


sys.excepthook = _excepthook

import threading
import subprocess
os.environ.setdefault("QT_OPENGL", "desktop")

import matplotlib
_orig_use = matplotlib.use


def _safe_use(backend, *a, **kw):
    if backend in ("TkAgg", "Tk"):
        return
    return _orig_use(backend, *a, **kw)


matplotlib.use = _safe_use
matplotlib.use("QtAgg")

import matplotlib.pyplot as plt
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.animation import FuncAnimation

_orig_subplots = plt.subplots
_fig_queue = []
_fig_lock = threading.Lock()


def _patched_subplots(*a, **kw):
    fig, ax = _orig_subplots(*a, **kw)
    with _fig_lock:
        _fig_queue.append(fig)
    return fig, ax


plt.subplots = _patched_subplots
plt.show = lambda *a, **kw: None

_anim_by_fig = {}
_orig_fa_init = FuncAnimation.__init__


def _fa_init(self, fig, *a, **kw):
    _orig_fa_init(self, fig, *a, **kw)
    _anim_by_fig[id(fig)] = self


FuncAnimation.__init__ = _fa_init

import shutil
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QLabel, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QCheckBox,
    QPlainTextEdit, QFrame,
)
from PyQt6.QtCore import Qt, QTimer

VIEWERS = [
    ("LED Overlay",       "pcb_viewer"),
    ("Current Flow",      "pcb_current_view"),
    ("Voltage Map",       "pcb_voltage_view"),
    ("Power Dissipation", "pcb_heat_view"),
    ("Thermal Field v2",  "pcb_thermal_field_v2"),
    ("Trace Resistance",  "pcb_resistance_view"),
]

_py = sys.executable
if _py.lower().endswith("pythonw.exe"):
    _py = _py[:-len("pythonw.exe")] + "python.exe"
PY = _py
RENODE = shutil.which("renode") or "renode"

# Each step: (name, executable, args, delay_ms)
PIPELINE_STEPS = [
    ("Renode",     RENODE, ["--port", "1234", "machine.resc"], 0),
    ("Bridge",     PY,     ["-u", "stm32_bridge.py"],          12000),
    ("Reader",     PY,     ["-u", "renode_reader.py"],         15000),
    ("Solver",     PY,     ["-u", "pyspice_solver.py"],        17000),
    ("Subscriber", PY,     ["-u", "test_sub.py"],              19000),
]

C_BG       = "#050505"
C_PANEL    = "#101010"
C_PANEL_BD = "#3a3a3a"
C_TEXT     = "#e8e8e8"
C_TEXT_DIM = "#888888"
C_ACCENT   = "#ffb733"

STYLESHEET = f"""
QMainWindow, QWidget      {{ background: {C_BG}; color: {C_TEXT}; }}
QTabWidget::pane          {{ border: 0; background: {C_BG}; }}
QTabBar::tab              {{ background: #1a1a1a; color: #bbb;
                             padding: 8px 18px; border: 0; font-size: 10pt; }}
QTabBar::tab:selected     {{ background: #2c2c2c; color: #fff; }}
QTabBar::tab:hover        {{ background: #242424; }}
QCheckBox                 {{ color: {C_TEXT}; padding: 3px 6px;
                             font-size: 10pt; }}
QCheckBox::indicator      {{ width: 14px; height: 14px; }}
QPlainTextEdit            {{ background: #000; color: #c8c8c8;
                             border: 0; font-family: Consolas, monospace;
                             font-size: 9pt; }}
"""


# ============================================================
# PipelineTab — subprocess + log-file tail (reliable on Windows)
# ============================================================

class PipelineTab(QWidget):
    def __init__(self, name, exe, args, delay_ms, cwd):
        super().__init__()
        self.name = name
        self.exe = exe
        self.args = args
        self.cwd = cwd
        self.log_path = os.path.join(_LOG_DIR, f"{name.lower()}.log")
        self.proc = None
        self._pos = 0

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(10000)
        lay.addWidget(self.view)

        self.view.appendPlainText(
            f"[{name}] scheduled in {delay_ms} ms\n"
            f"[{name}] cmd: {exe} {' '.join(args)}\n"
            f"[{name}] log: {self.log_path}\n"
        )

        self._poll = QTimer(self)
        self._poll.setInterval(250)
        self._poll.timeout.connect(self._tail)
        self._poll.start()

        QTimer.singleShot(delay_ms, self._start)

    def _start(self):
        # Truncate old log so we start fresh
        try:
            open(self.log_path, "w").close()
        except Exception:
            pass
        self._pos = 0
        self.view.appendPlainText(f"\n[{self.name}] starting...\n")

        try:
            # Open the log file for append; process owns this handle
            self._logf = open(self.log_path, "a",
                              encoding="utf-8",
                              errors="replace",
                              buffering=1)
            flags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                flags = subprocess.CREATE_NO_WINDOW
            self.proc = subprocess.Popen(
                [self.exe] + self.args,
                cwd=self.cwd,
                stdout=self._logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
        except Exception as e:
            self.view.appendPlainText(
                f"\n[{self.name}] spawn error: {type(e).__name__}: {e}\n")

    def _tail(self):
        try:
            size = os.path.getsize(self.log_path)
        except OSError:
            return
        if size <= self._pos:
            return
        try:
            with open(self.log_path, "r",
                      encoding="utf-8", errors="replace") as f:
                f.seek(self._pos)
                data = f.read()
                self._pos = f.tell()
        except OSError:
            return
        if data:
            self.view.moveCursor(self.view.textCursor().End)
            self.view.insertPlainText(data)

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        try:
            self._logf.close()
        except Exception:
            pass


# ============================================================
# ViewerTab — placeholder that gets replaced by a canvas
# ============================================================

class ViewerTab(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.layout_ = lay
        self.placeholder = QLabel("Switch to Home tab to enable this plot.")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet("color: #666; font-size: 14pt;")
        lay.addWidget(self.placeholder)

    def attach_canvas(self, canvas):
        if self.placeholder is not None:
            self.placeholder.setParent(None)
            self.placeholder.deleteLater()
            self.placeholder = None
        canvas.setParent(self)
        self.layout_.addWidget(canvas)
        canvas.setVisible(True)


class HomeTab(QWidget):
    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        for c in range(3):
            self.grid.setColumnStretch(c, 1)
        for r in range(2):
            self.grid.setRowStretch(r, 1)
        outer.addWidget(self.grid_host)

    def attach_canvas(self, canvas, slot):
        row, col = slot
        canvas.setParent(self.grid_host)
        self.grid.addWidget(canvas, row, col)
        canvas.setVisible(True)

    def detach_all(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)


class Dashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("STM32 Digital Twin — Unified UI")
        self.resize(1800, 1000)
        self.setStyleSheet(STYLESHEET)

        self.cwd = r"C:\Users\user\Desktop\automation\kicad_plugin"

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        # -------- Left: tabs --------
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        self.home_tab = HomeTab()
        self.tabs.addTab(self.home_tab, "Home")

        self.viewer_tabs = {}
        for label, mod in VIEWERS:
            vt = ViewerTab()
            self.tabs.addTab(vt, label)
            self.viewer_tabs[mod] = vt

        self.pipeline_tabs = []
        pipe_host = QWidget()
        pipe_grid = QGridLayout(pipe_host)
        pipe_grid.setContentsMargins(2, 2, 2, 2)
        pipe_grid.setSpacing(4)
        for idx, (name, exe, args, delay) in enumerate(PIPELINE_STEPS):
            tab = PipelineTab(name, exe, args, delay, self.cwd)
            self.pipeline_tabs.append(tab)
            row, col = divmod(idx, 3)
            pipe_grid.addWidget(tab, row, col)
        for c in range(3):
            pipe_grid.setColumnStretch(c, 1)
        for r in range(2):
            pipe_grid.setRowStretch(r, 1)
        self._pipeline_host = pipe_host
        self.tabs.addTab(pipe_host, "Pipeline")

        # -------- Right: sidebar --------
        self.sidebar = QWidget()
        self.sidebar.setFixedWidth(430)
        slay = QVBoxLayout(self.sidebar)
        slay.setContentsMargins(6, 6, 6, 6)
        slay.setSpacing(4)

        # Plots section (hidden on non-home tabs)
        self.plots_section = QWidget()
        pslay = QVBoxLayout(self.plots_section)
        pslay.setContentsMargins(0, 0, 0, 0)
        pslay.setSpacing(2)

        plots_title = QLabel("Plots")
        plots_title.setStyleSheet(
            f"color: {C_ACCENT}; font-weight: bold; padding: 2px;")
        pslay.addWidget(plots_title)

        self.checkboxes = {}
        for label, mod in VIEWERS:
            cb = QCheckBox(f"  {label}")
            cb.setChecked(False)
            cb.stateChanged.connect(self._on_checkbox_changed)
            pslay.addWidget(cb)
            self.checkboxes[mod] = cb

        slay.addWidget(self.plots_section)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C_PANEL_BD}; background: {C_PANEL_BD};")
        sep.setFixedHeight(1)
        slay.addWidget(sep)
        self.sep = sep

        ctrl_title = QLabel("Control Panel")
        ctrl_title.setStyleSheet(
            f"color: {C_ACCENT}; font-weight: bold; padding: 4px 2px;")
        slay.addWidget(ctrl_title)
        self.ctrl_title = ctrl_title

        self.control_host = QWidget()
        QVBoxLayout(self.control_host).setContentsMargins(0, 0, 0, 0)
        slay.addWidget(self.control_host, 1)

        root.addWidget(self.sidebar, 0)

        # Bookkeeping
        self._modules = []
        self._figs = {}
        self._canvases = {}
        self._next_idx = 0
        self._control_win = None

        self.tabs.currentChanged.connect(self._on_tab_changed)
        QTimer.singleShot(1500, self._launch_next)

    # ---------- Viewer loading ----------

    def _launch_next(self):
        if self._next_idx >= len(VIEWERS):
            QTimer.singleShot(300, self._embed_control_panel)
            QTimer.singleShot(600, self._pre_tick_home)
            return

        idx = self._next_idx
        label, mod = VIEWERS[idx]
        print(f"[*] Loading viewer {idx + 1}/{len(VIEWERS)}: {label}")
        try:
            module = __import__(mod)
            self._modules.append(module)
            with _fig_lock:
                before = len(_fig_queue)
            module.main()
            with _fig_lock:
                after = len(_fig_queue)
            if after > before:
                fig = _fig_queue[before]
                canvas = FigureCanvas(fig)
                canvas.setMinimumHeight(400)
                self._figs[mod] = fig
                self._canvases[mod] = canvas
                self.viewer_tabs[mod].attach_canvas(canvas)
                canvas.setVisible(True)
        except Exception as e:
            print(f"[!] {label} failed: {type(e).__name__}: {e}")
            lbl = QLabel(f"{label}\nFAILED\n{e}")
            lbl.setStyleSheet("color: #f88; padding: 20px;")
            self.viewer_tabs[mod].layout_.addWidget(lbl)

        self._next_idx += 1
        QTimer.singleShot(300, self._launch_next)

    def _embed_control_panel(self):
        try:
            import pyqt6_control
            win = pyqt6_control.MainWindow()
            central = win.takeCentralWidget()
            if central is None:
                raise RuntimeError("control panel has no central widget")
            central.setParent(self.control_host)
            lay = self.control_host.layout()
            lay.addWidget(central)
            central.show()
            self._control_win = win
            print("[+] Control panel embedded in global sidebar.")
        except Exception as e:
            print(f"[!] Control panel embed failed: {type(e).__name__}: {e}")
            lbl = QLabel(f"Control panel failed:\n{type(e).__name__}: {e}")
            lbl.setStyleSheet("color: #f88; padding: 20px;")
            self.control_host.layout().addWidget(lbl)

    def _pre_tick_home(self):
        for mod in ("pcb_viewer", "pcb_current_view", "pcb_voltage_view"):
            self.checkboxes[mod].setChecked(True)

    # ---------- Layout ----------

    def _on_checkbox_changed(self, _state):
        if self.tabs.currentWidget() is self.home_tab:
            self._relayout()

    def _on_tab_changed(self, _idx):
        current = self.tabs.currentWidget()

        # ---- Sidebar visibility rules ----
        # Entire sidebar hidden on: Trace Resistance, Pipeline
        # Plots section hidden on: all tabs except Home
        # (Control Panel always visible when sidebar is shown)
        hide_sidebar = (
            current is self.viewer_tabs.get("pcb_resistance_view")
            or current is self._pipeline_host
        )
        self.sidebar.setVisible(not hide_sidebar)

        is_home = (current is self.home_tab)
        self.plots_section.setVisible(is_home)
        self.sep.setVisible(is_home)
        # Control panel title stays visible whenever the sidebar is visible
        # (only when plots section is hidden do we move the title up)
        if not is_home:
            self.ctrl_title.setStyleSheet(
                f"color: {C_ACCENT}; font-weight: bold; padding: 2px;")

        self._relayout()

    def _relayout(self):
        current = self.tabs.currentWidget()
        if current is self.home_tab:
            self._layout_home()
        elif current in self.viewer_tabs.values():
            mod = [m for m, t in self.viewer_tabs.items() if t is current][0]
            self._layout_single(mod)
        else:
            for mod, canvas in self._canvases.items():
                self.viewer_tabs[mod].attach_canvas(canvas)
                canvas.setVisible(True)
        self._apply_animation_policy()

    def _layout_home(self):
        wanted = [m for m, cb in self.checkboxes.items() if cb.isChecked()]
        self.home_tab.detach_all()
        for slot_idx, mod in enumerate(wanted[:6]):
            row, col = divmod(slot_idx, 3)
            canvas = self._canvases[mod]
            self._figs[mod].set_dpi(70)
            canvas.setMinimumSize(300, 220)
            self.home_tab.attach_canvas(canvas, (row, col))
        for mod, canvas in self._canvases.items():
            if mod not in wanted[:6]:
                self._figs[mod].set_dpi(130)
                self.viewer_tabs[mod].attach_canvas(canvas)
                canvas.setVisible(True)

    def _layout_single(self, mod):
        for m, canvas in self._canvases.items():
            self._figs[m].set_dpi(130)
            canvas.setMinimumSize(0, 0)
            self.viewer_tabs[m].attach_canvas(canvas)
            canvas.setVisible(True)

    def _apply_animation_policy(self):
        current = self.tabs.currentWidget()
        running_mods = set()

        if current is self.home_tab:
            running_mods = {m for m, cb in self.checkboxes.items()
                            if cb.isChecked()}
        elif current in self.viewer_tabs.values():
            for m, t in self.viewer_tabs.items():
                if t is current:
                    running_mods.add(m)

        n = len(running_mods)
        on_home = (current is self.home_tab)
        if n == 0:
            interval = None
        elif on_home:
            interval = 400 if n <= 2 else 1000 if n <= 4 else 1500
        else:
            interval = 150

        for mod, fig in self._figs.items():
            anim = _anim_by_fig.get(id(fig))
            if anim is None:
                continue
            try:
                if mod in running_mods:
                    anim.event_source.interval = interval
                    anim.event_source.start()
                else:
                    anim.event_source.stop()
            except Exception:
                pass

    # ---------- Dark OS title bar ----------

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_dark_title_bar()

    def _apply_dark_title_bar(self):
        try:
            import ctypes
            hwnd = int(self.winId())
            for attr in (20, 19):
                v = ctypes.c_int(1)
                res = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))
                if res == 0:
                    break
        except Exception:
            pass

    # ---------- Shutdown ----------

    def closeEvent(self, event):
        print("[*] Shutting down dashboard...")
        for tab in self.pipeline_tabs:
            tab.stop()
        try:
            subprocess.run(["taskkill", "/F", "/IM", "renode.exe"],
                           capture_output=True)
            subprocess.run(["taskkill", "/F", "/IM", "ngspice_con.exe"],
                           capture_output=True)
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    win = Dashboard()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
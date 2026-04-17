import os
import sys
import atexit
import ctypes
import subprocess
import threading
import statistics
import keyboard
import psutil
import win32pdh
import wmi
import tkinter as tk
import webbrowser
import json
import winreg
import shutil
import time
from tkinter import scrolledtext, colorchooser, messagebox, ttk
from PIL import Image, ImageDraw
from collections import deque

# PySide6 for Custom Tray and Menu (Restored for Dark Theme and Click Support)
os.environ['QT_LOGGING_RULES'] = 'qt.qpa.window=false'
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QAction

# ================= BUILD MODE =================
# test=1 -> testing mode (uses island.pyw)
# test=0 -> deployment mode (uses island.exe)
test = 1

# ================= DESKTOP MODE SETTING =================
# Set to 0 for Automatic Battery Detection
# Set to 1 to Force Desktop Mode (Disables battery monitoring, saves CPU/RAM)
FORCE_DESKTOP_MODE = 0

# ================= HIGH DPI FIX (Sharp Text) =================
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

# ================= CONSTANTS & SINGLE INSTANCE CHECK =================
UNIQUE_TITLE = "STABLE_BATTERY_MONITOR_V2.9_PERSISTENT"
ACTIVATION_KEY = "jaks360@"
ACTIVATION_REG_PATH = r"Software\JAKS360\SmartMonitor"
ACTIVATION_REG_VALUE = "IslandKey"


def _app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _resolve_app_icon_path():
    base_dir = _app_base_dir()
    candidates = [
        os.path.join(base_dir, "icon.ico"),
        os.path.join(base_dir, "desktop.ico"),
        os.path.join(os.getcwd(), "icon.ico"),
        os.path.join(os.getcwd(), "desktop.ico"),
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def _apply_tk_icon(win):
    try:
        icon_path = _resolve_app_icon_path()
        if icon_path:
            win.iconbitmap(icon_path)
    except Exception:
        pass


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _activation_store_path():
    app_data = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(app_data, "Smart Monitor", "island_activation.bin")


def _bytes_to_blob(data):
    if not data:
        return _DATA_BLOB(0, None), None
    buf = ctypes.create_string_buffer(data)
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _blob_to_bytes(blob):
    if not blob.cbData or not blob.pbData:
        return b""
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_protect_text(value):
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        in_blob, in_buf = _bytes_to_blob(value.encode("utf-8"))
        out_blob = _DATA_BLOB()
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "SmartMonitor Island Activation",
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob)
        )
        if not ok:
            return None
        try:
            return _blob_to_bytes(out_blob)
        finally:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
    except Exception:
        return None


def _dpapi_unprotect_bytes(data):
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        in_blob, in_buf = _bytes_to_blob(data)
        out_blob = _DATA_BLOB()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(out_blob)
        )
        if not ok:
            return None
        try:
            raw = _blob_to_bytes(out_blob)
        finally:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, ctypes.c_void_p))
        return raw.decode("utf-8", errors="ignore")
    except Exception:
        return None


def _read_secure_activation_key():
    try:
        path = _activation_store_path()
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            payload = f.read()
        if not payload:
            return None
        return _dpapi_unprotect_bytes(payload)
    except Exception:
        return None


def _write_secure_activation_key(value):
    encrypted = _dpapi_protect_text(value)
    if not encrypted:
        return False
    try:
        path = _activation_store_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(encrypted)
        os.replace(tmp, path)
        try:
            ctypes.windll.kernel32.SetFileAttributesW(path, 0x02)  # hidden
        except Exception:
            pass
        return True
    except Exception:
        return False


def _read_legacy_registry_key():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, ACTIVATION_REG_PATH, 0, winreg.KEY_READ)
        value, _ = winreg.QueryValueEx(key, ACTIVATION_REG_VALUE)
        winreg.CloseKey(key)
        return str(value).strip()
    except Exception:
        return None


def _migrate_legacy_registry_key():
    legacy = _read_legacy_registry_key()
    if legacy != ACTIVATION_KEY:
        return False
    if not _write_secure_activation_key(ACTIVATION_KEY):
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, ACTIVATION_REG_PATH, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, ACTIVATION_REG_VALUE)
        winreg.CloseKey(key)
    except Exception:
        pass
    return True

def check_for_running_instance():
    hwnd = ctypes.windll.user32.FindWindowW(None, UNIQUE_TITLE)
    if hwnd:
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
        sys.exit(0)

check_for_running_instance()

# ================= ACTIVATION =================
def check_island_activation():
    stored = _read_secure_activation_key()
    if stored == ACTIVATION_KEY:
        return True
    return _migrate_legacy_registry_key()

def activate_island(key_str):
    if (key_str or "").strip() != ACTIVATION_KEY:
        return False
    return _write_secure_activation_key(ACTIVATION_KEY)

# ================= MAIN MONITOR APPLICATION CLASS =================
class StableStartMonitor:
    def __init__(self):
        # 1. Default State Flags
        self.visible = False
        self.show_network = True
        self.show_stats = True
        self.show_extended_colors = False
        self.show_about = False
        self.show_hardware = False
        self.show_graphs = False    
        self.show_hotkey_editor = False 
        self.show_task_killer = False
        self.show_startup_opt = False
        self.show_boost_notif = False
        self.has_moved = False # Tracks if user manually moved the window

        # Default Colors & Theme
        self.current_theme = "dark"
        self.rgb_enabled = False
        self.rgb_hue = 0
        self.color_cpu = "#00d4ff"
        self.color_gpu = "#50fa7b"
        self.color_ram = "#ff79c6"
        self.color_disk = "#bd93f9"
        self.color_customized = False

        # Island launcher state (for Dynamic Island subprocess)
        self.island_proc = None
        island_file_name = "island.pyw" if test == 1 else "island.exe"
        self.island_path = os.path.join(os.path.dirname(__file__), island_file_name)
        self.island_action = None
        self.island_control_action = None
        self.island_activation_popup = None

        # Ensure island stops on interpreter exit
        atexit.register(self.stop_island)

        # Shortcuts Defaults
        self.shortcuts_mode = 1  
        self.shortcuts_height_val = 70

        # Graph Data (trimmed length to reduce per-frame work on older PCs)
        self.graph_history_cpu = deque([0]*45, maxlen=45)
        self.graph_history_gpu = deque([0]*45, maxlen=45)
        self._last_graph_draw_ts = 0.0

        # GPU fallback cache to avoid GPUtil scan every loop
        self._last_gpu_fallback_ts = 0.0
        self._last_gpu_fallback_val = 0.0

        # Default Hotkeys 
        self.hotkeys = {
            "toggle_visibility": "ctrl+b",
            "toggle_stats": "ctrl+m",
            "toggle_network": "ctrl+i",
            "toggle_hardware": "ctrl+h",
            "toggle_colors": "ctrl+alt+s",
            "toggle_rgb": "ctrl+alt+r",
            "toggle_theme": "ctrl+alt+t",
            "toggle_about": "ctrl+alt+a",
            "exit_app": "ctrl+alt+b",
            "toggle_graphs": "ctrl+g",      
            "edit_hotkeys": "ctrl+k",
            "pc_boost": "ctrl+shift+b",
            "task_killer": "ctrl+shift+t",
            "startup_opt": "ctrl+shift+o",
            "toggle_island": "ctrl+d"
        }

        # Hotkey editing state
        self.active_remap_action = None

        # Layout Defaults
        self.border_thickness = 2
        self.prev_border_thickness = 2  # Store thickness before RGB mode
        self.border_color = "#444444"
        self.stats_left_padding = 15
        self.stats_spacing = 10
        self.stats_right_padding = 0

        # --- LOAD SAVED SETTINGS ---
        self.load_config()
        self._last_island_cfg_mtime = None
        self.sync_island_hotkey(update_about=False, force=True)
        self.is_island_activated = check_island_activation()
        self.island_trial_until = 0.0

        # --- RATING & UNLOCK STATUS ---
        self.is_premium = self.check_if_rated()
        self.trial_ended = False
        self.trial_timer = None

        self.current_launch_count = self.increment_launch_count()

        # ---------- WINDOW SETUP ----------
        self.root = tk.Tk()
        self.root.withdraw()
        _apply_tk_icon(self.root)

        self.root.tk.call('tk', 'scaling', 2.0)
        self.root.title(UNIQUE_TITLE)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg="#000000")
        self._corner_refresh_job = None
        self.root.bind("<Configure>", lambda _event: self.schedule_win11_style_refresh(35))

        self.set_tool_window()

        # Static width setup for inward-growing borders
        self.width = 284 
        self.height_base = 135
        self.current_height = self.height_base

        # Start Trial Timer if not premium
        if not self.is_premium:
            self.run_trial_timer()

        # ---------- MAIN CONTAINER ----------
        self.outer_frame = tk.Frame(self.root, bg="#000000",
                                    highlightthickness=self.border_thickness,
                                    highlightbackground=self.border_color, bd=0)
        self.outer_frame.pack(fill="both", expand=True)

        # ---------- DRAG GRIP ----------
        self.drag_grip = tk.Frame(self.outer_frame, bg="#444444", height=4, width=40)

        # ---------- BOOST NOTIFICATION FRAME ----------
        self.boost_notif_frame = tk.Frame(self.outer_frame, bg="#00ff66")
        self.boost_notif_lbl = tk.Label(self.boost_notif_frame, text="🚀 PC BOOSTED!\n(RAM/Temp Cleared)", 
                                        fg="#000000", bg="#00ff66", font=("Segoe UI", 9, "bold"), justify="center")
        self.boost_notif_lbl.pack(pady=4)
        self.boost_deadline = 0
        self.boost_timer = None

        # ---------- BOTTOM ARROW ----------
        self.arrow_canvas = tk.Canvas(self.root, width=self.width, height=16, bg="#000000",
                                      highlightthickness=0, cursor="hand2")
        self.arrow_canvas.create_line(0, 8, self.width, 8, fill="#555555", width=1, capstyle="butt")
        self.arrow_canvas.create_polygon(self.width // 2 - 4, 4, self.width // 2 + 4, 4, self.width // 2, 12,
                                         fill="#ffffff", outline="#ffffff")
        self.arrow_canvas.bind("<Button-1>", lambda e: self.toggle_about())

        self.arrow_visible = False
        self.arrow_timer = None
        self.arrow_cycle_timer = None

        # ---------- SHORTCUTS / RATING CANVAS ----------
        self.shortcuts_canvas = tk.Canvas(self.root, width=self.width, height=self.shortcuts_height_val, bg="#000000",
                                          highlightthickness=1, highlightbackground=self.border_color,
                                          cursor="hand2")

        self.shortcuts_canvas.create_rectangle(2, 2, self.width-2, self.shortcuts_height_val-2,
                                               outline="#666666", width=1, fill="#111111")

        self.sc_text_id = self.shortcuts_canvas.create_text(self.width // 2, 20, text="SHORTCUTS KEYS",
                                                            fill="#ffffff", font=("Segoe UI", 8, "bold"),
                                                            anchor="center")

        self.sc_line_id = self.shortcuts_canvas.create_line(40, 35, self.width - 40, 35, fill="#444444")

        self.sc_footer_id = self.shortcuts_canvas.create_text(self.width // 2, 50,
                                                              text="Created by JAKS 360 | v1.5",
                                                              fill="#bbbbbb", font=("Segoe UI", 7),
                                                              anchor="center")

        self.shortcuts_canvas.bind("<Button-1>", lambda e: self.toggle_about())

        self.shortcuts_visible = False
        self.shortcuts_timer = None
        self.shortcuts_cycle_timer = None

        def bind_drag(w):
            w.bind("<Button-1>", self.start_move)
            w.bind("<B1-Motion>", self.do_move)
            w.bind("<Button-3>", lambda e: self.hide())
            for child in w.winfo_children():
                if not isinstance(child, (tk.Scrollbar, tk.Text, tk.Button, tk.Scale, tk.Entry, tk.Listbox)):
                    bind_drag(child)

        bind_drag(self.outer_frame)
        bind_drag(self.drag_grip)
        bind_drag(self.boost_notif_frame)
        bind_drag(self.boost_notif_lbl)
        
        # 1. FIXED UI (Battery)
        self.canvas = tk.Canvas(self.outer_frame, width=240, height=34, bg="#000000", highlightthickness=0)
        self.canvas.create_rectangle(15, 6, 215, 26, outline="#888888", width=2)
        self.canvas.create_rectangle(215, 11, 223, 21, fill="#888888", outline="#888888")
        self.battery_fill = self.canvas.create_rectangle(17, 8, 17, 24, fill="#00ff66", outline="")

        self.stat_row = tk.Frame(self.outer_frame, bg="#000000")

        self.label_pct = tk.Label(self.stat_row, text="--.-%", fg="#00ff66",
                                  bg="#000000", font=("Segoe UI Variable", 15, "bold"))
        self.label_pct.pack(side="left", expand=True, anchor="e", padx=(0, 5))

        self.label_watts = tk.Label(self.stat_row, text="0.00 W", fg="#ffffff", bg="#000000", font=("Segoe UI Variable", 12, "bold"))
        self.label_watts.pack(side="left", expand=True, anchor="w", padx=(5, 0))

        self.label_info = tk.Label(self.outer_frame, text="Waiting...", fg="#aaaaaa", bg="#000000", font=("Bahnschrift", 11, "normal"))

        # 1.1 DESKTOP HEADER (For PC Mode)
        self.label_desktop = tk.Label(self.outer_frame, text="DESKTOP MODE", fg="#00d4ff", bg="#000000", font=("Segoe UI Display", 11, "bold"))

        # 2. DYNAMIC FRAMES

        # --- STATS FRAME ---
        self.stats_frame = tk.Frame(self.outer_frame, bg="#000000")

        # Removed fill="x" so Tkinter mathematically centers the row
        self.row1 = tk.Frame(self.stats_frame, bg="#000000")
        self.row1.pack(pady=2) 
        self.lbl_cpu = tk.Label(self.row1, text="CPU: 0%", fg=self.color_cpu,
                                bg="#000000", font=("Segoe UI Variable", 10, "bold"), width=8, anchor="w")
        self.lbl_cpu.pack(side="left", padx=(0, 10)) # 10px padding on the right
        self.lbl_gpu = tk.Label(self.row1, text="GPU: 0%", fg=self.color_gpu, bg="#000000", font=("Segoe UI Variable", 10, "bold"), width=8, anchor="w")
        self.lbl_gpu.pack(side="left", padx=(10, 0)) # 10px padding on the left

        self.row2 = tk.Frame(self.stats_frame, bg="#000000")
        self.row2.pack(pady=2) 
        self.lbl_ram = tk.Label(self.row2, text="RAM: 0%", fg=self.color_ram, bg="#000000", font=("Segoe UI Variable", 10, "bold"), width=8, anchor="w")
        self.lbl_ram.pack(side="left", padx=(0, 10))
        self.lbl_disk = tk.Label(self.row2, text="DSK: 0%", fg=self.color_disk, bg="#000000", font=("Segoe UI Variable", 10, "bold"), width=8, anchor="w")
        self.lbl_disk.pack(side="left", padx=(10, 0))

        # --- GRAPH FRAME (OPTIMIZED) ---
        self.graph_frame = tk.Frame(self.outer_frame, bg="#000000", height=60)
        self.graph_canvas = tk.Canvas(self.graph_frame, width=260, height=55, bg="#000000", highlightthickness=0)
        self.graph_canvas.pack(pady=2, padx=10)
        bind_drag(self.graph_frame)
        bind_drag(self.graph_canvas)
        
        # Initialize graph canvas elements ONCE
        self.graph_canvas.create_line(0, 55/2, 260, 55/2, fill="#333", dash=(2,2))
        self.bg_rect = self.graph_canvas.create_rectangle(0, 0, 260, 15, fill="#000000", outline="")
        self.cpu_line = self.graph_canvas.create_line(0,0,0,0, fill=self.color_cpu, width=2, smooth=True)
        self.gpu_line = self.graph_canvas.create_line(0,0,0,0, fill=self.color_gpu, width=2, smooth=True)
        self.cpu_txt = self.graph_canvas.create_text(10, 7, text="CPU", fill=self.color_cpu, anchor="w", font=("Segoe UI", 8, "bold"))
        self.gpu_txt = self.graph_canvas.create_text(45, 7, text="GPU", fill=self.color_gpu, anchor="w", font=("Segoe UI", 8, "bold"))

        # --- TRIAL LOCK FRAME ---
        self.trial_lock_frame = tk.Frame(self.outer_frame, bg="#000000")

        self.lock_msg = tk.Label(self.trial_lock_frame, text="TRIAL PERIOD ENDED",
                                 fg="#ff4444", bg="#000000", font=("Segoe UI", 10, "bold"))
        self.lock_msg.pack(pady=(15, 5))

        self.lock_desc = tk.Label(self.trial_lock_frame, text="Please rate us to enable\n CPU, GPU, RAM & Disk stats.",
                                  fg="#dddddd", bg="#000000", font=("Segoe UI", 9), justify="center")
        self.lock_desc.pack(pady=2)

        self.btn_unlock = tk.Button(self.trial_lock_frame, text="★★★  ★\nClick here to share",
                                    fg="#ffcc00", bg="#222222", font=("Segoe UI", 10, "bold"),
                                    relief="flat", cursor="hand2", command=self.unlock_premium_features)
        self.btn_unlock.pack(pady=(15, 20), ipadx=10, ipady=5)

        bind_drag(self.trial_lock_frame)
        bind_drag(self.lock_msg)
        bind_drag(self.lock_desc)

        # --- NETWORK FRAME ---
        self.network_frame = tk.Frame(self.outer_frame, bg="#000000")
        self.net_inner = tk.Frame(self.network_frame, bg="#000000")
        self.net_inner.pack(expand=True)
        self.arr_down = tk.Label(self.net_inner, text="↓", fg="white", bg="#000000", font=("Segoe UI Semibold", 11))
        self.arr_down.pack(side="left")
        self.lbl_down = tk.Label(self.net_inner, text="0 Kbps", fg="white", bg="#000000", font=("Segoe UI Semibold", 9))
        self.lbl_down.pack(side="left", padx=(1, 6))
        tk.Label(self.net_inner, text="|", fg="#cccccc", bg="#000000", font=("Segoe UI Semibold", 9)).pack(side="left")
        self.arr_up = tk.Label(self.net_inner, text="↑", fg="white", bg="#000000", font=("Segoe UI Semibold", 11))
        self.arr_up.pack(side="left", padx=(6, 1))
        self.lbl_up = tk.Label(self.net_inner, text="0 Kbps", fg="white", bg="#000000", font=("Segoe UI Semibold", 9))
        self.lbl_up.pack(side="left")

        # --- COLOR SETTINGS FRAME ---
        self.color_frame = tk.Frame(self.outer_frame, bg="#000000")

        def mk_btn(parent, txt, col, cmd, width=3):
            f = tk.Frame(parent, bg="#000000")
            f.pack(fill="x", padx=20, pady=5)
            tk.Label(f, text=txt, fg="white", bg="#000000", justify="center").pack(side="left")
            fg_col = col
            if col in ["#00ff00", "#ffcc00"]:
                fg_col = "black"
            if col == "#ff3333":
                fg_col = "white"
            b = tk.Button(f, text="" if width == 3 else "AUTO", fg=fg_col, bg=col, width=width, height=1,
                          bd=0, highlightthickness=1, highlightbackground="#555", command=cmd)
            b.pack(side="right")
            return b

        self.btn_cpu = mk_btn(self.color_frame, "CPU Color", self.color_cpu, lambda: self.set_col('cpu'))
        self.btn_gpu = mk_btn(self.color_frame, "GPU Color", self.color_gpu, lambda: self.set_col('gpu'))
        self.btn_ram = mk_btn(self.color_frame, "RAM Color", self.color_ram, lambda: self.set_col('ram'))
        self.btn_dsk = mk_btn(self.color_frame, "Disk Color", self.color_disk, lambda: self.set_col('disk'))

        # --- ISLAND ACTIVATION BUTTON ---
        self.island_act_frame = tk.Frame(self.color_frame, bg="#000000")
        self.island_act_frame.pack(fill="x", padx=20, pady=5)

        # --- BORDER THICKNESS SLIDER ---
        self.slider_frame = tk.Frame(self.color_frame, bg="#000000")
        self.slider_frame.pack(fill="x", padx=20, pady=10)
        self.lbl_slider = tk.Label(self.slider_frame, text="Border Thickness", fg="white", bg="#000000")
        self.lbl_slider.pack(side="top", anchor="center") # Centered looks better!
        
        self.thickness_slider = tk.Scale(self.slider_frame, from_=1, to=10, orient="horizontal",
                                         bg="#000000", fg="white", highlightthickness=0, bd=0,
                                         command=self.update_border_thickness)
        safe_thickness = max(1, self.border_thickness)
        self.thickness_slider.set(safe_thickness)
        self.thickness_slider.pack(fill="x")

        # --- GAME BOOST BUTTON ---
        self.btn_game_boost = tk.Button(self.color_frame, text="🚀 ONE-CLICK PC BOOST",
                                        fg="#ffffff", bg="#cc0000", font=("Segoe UI", 9, "bold"),
                                        relief="flat", cursor="hand2", 
                                        activebackground="#ff4c4c", activeforeground="#ffffff",
                                        command=self.execute_pc_boost)
        self.btn_game_boost.pack(fill="x", padx=20, pady=10, ipady=5)

        # --- TOGGLE BUTTON FOR SHORTCUTS WINDOW ---
        self.btn_sc_toggle = mk_btn(self.color_frame, "Shortcuts Menu", "#00ff00", self.toggle_shortcuts_mode, width=6)
        self.btn_sc_toggle.config(text="FIXED") 

        # --- TASK KILLER FRAME ---
        self.task_killer_frame = tk.Frame(self.outer_frame, bg="#000000")
        self.tk_lbl_title = tk.Label(self.task_killer_frame, text="TOP USER RAM EATERS", fg="#ff4c4c", bg="#000000", font=("Segoe UI", 10, "bold"))
        self.tk_lbl_title.pack(pady=(5, 5))
        
        # Scrollable area for RAM hogs
        self.tk_canvas = tk.Canvas(self.task_killer_frame, bg="#111111", highlightthickness=0, height=220)
        self.tk_scrollbar = ttk.Scrollbar(self.task_killer_frame, orient="vertical", command=self.tk_canvas.yview)
        self.tk_scroll_frame = tk.Frame(self.tk_canvas, bg="#111111")
        self.tk_scroll_frame.bind("<Configure>", lambda e: self.tk_canvas.configure(scrollregion=self.tk_canvas.bbox("all")))
        self.tk_window_id = self.tk_canvas.create_window((0, 0), window=self.tk_scroll_frame, anchor="nw")
        
        self.tk_canvas.bind('<Configure>', lambda e: self.tk_canvas.itemconfig(self.tk_window_id, width=e.width))
        self.tk_canvas.configure(yscrollcommand=self.tk_scrollbar.set)
        self.tk_canvas.pack(side="left", fill="both", expand=True, padx=10, pady=5)
        self.tk_scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=5)
        self.btn_refresh_tk = tk.Button(self.task_killer_frame, text="Refresh List", fg="#000000", bg="#ffcc00", font=("Segoe UI", 8, "bold"), relief="flat", activebackground="#ffe066", command=self.refresh_task_killer)
        self.btn_refresh_tk.pack(pady=(0, 5))

        # Mouse-wheel scrolling for RAM hog list (Now handled globally)

        bind_drag(self.task_killer_frame)

        # --- STARTUP OPTIMIZER FRAME ---
        self.startup_frame = tk.Frame(self.outer_frame, bg="#000000")
        self.su_lbl_title = tk.Label(self.startup_frame, text="STARTUP APPS\n(CURRENT USER)", fg="#00d4ff", bg="#000000", font=("Segoe UI", 10, "bold"), justify="center")
        self.su_lbl_title.pack(pady=(5, 2))
        
        self.btn_disable_su = tk.Button(self.startup_frame, text="Disable Startups", fg="#000000", bg="#ffcc00", font=("Segoe UI", 8, "bold"), relief="flat", activebackground="#ffe066", command=self.open_taskmgr_startup)
        self.btn_disable_su.pack(pady=(0, 5))

        # Scrollable area for startup apps
        self.su_canvas = tk.Canvas(self.startup_frame, bg="#111111", highlightthickness=0, height=260)
        self.su_scrollbar = ttk.Scrollbar(self.startup_frame, orient="vertical", command=self.su_canvas.yview)
        self.su_scroll_frame = tk.Frame(self.su_canvas, bg="#111111")
        self.su_scroll_frame.bind("<Configure>", lambda e: self.su_canvas.configure(scrollregion=self.su_canvas.bbox("all")))
        self.su_window_id = self.su_canvas.create_window((0, 0), window=self.su_scroll_frame, anchor="nw")
        
        self.su_canvas.bind('<Configure>', lambda e: self.su_canvas.itemconfig(self.su_window_id, width=e.width))
        self.su_canvas.configure(yscrollcommand=self.su_scrollbar.set)
        self.su_canvas.pack(side="left", fill="both", expand=True, padx=10, pady=5)
        self.su_scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=5)

        # Mouse-wheel scrolling for Startup Apps (Now handled globally)
        
        bind_drag(self.startup_frame)

        # --- HOTKEY EDITOR FRAME ---
        self.hotkey_editor_frame = tk.Frame(self.outer_frame, bg="#000000")
        self.hotkey_canvas = tk.Canvas(self.hotkey_editor_frame, bg="#111", highlightthickness=0)
        self.hotkey_scrollbar = ttk.Scrollbar(self.hotkey_editor_frame, orient="vertical", command=self.hotkey_canvas.yview)
        self.hotkey_scroll_frame = tk.Frame(self.hotkey_canvas, bg="#111")
        
        self.hotkey_scroll_frame.bind(
            "<Configure>",
            lambda e: self.hotkey_canvas.configure(scrollregion=self.hotkey_canvas.bbox("all"))
        )
        
        self.hotkey_window_id = self.hotkey_canvas.create_window((0, 0), window=self.hotkey_scroll_frame, anchor="nw")
        self.hotkey_canvas.bind('<Configure>', lambda e: self.hotkey_canvas.itemconfig(self.hotkey_window_id, width=e.width))
        self.hotkey_canvas.configure(yscrollcommand=self.hotkey_scrollbar.set)
        
        self.hotkey_status = tk.Label(self.hotkey_editor_frame, text="Click a button to edit its key", fg="#888", bg="#000000")
        self.hotkey_status.pack(side="bottom", fill="x", pady=2)

        self.hotkey_scrollbar.pack(side="right", fill="y", padx=(0, 2), pady=5)
        self.hotkey_canvas.pack(side="left", fill="both", expand=True, padx=(5, 0), pady=5)
        
        # --- MASTER SCROLL (GHOST + MENUS) ---
        def handle_scroll(e):
            if e.state & 0x0004:  # Detects if the 'Ctrl' key is being held down
                current_alpha = self.root.attributes("-alpha")
                new_alpha = min(1.0, current_alpha + 0.05) if e.delta > 0 else max(0.1, current_alpha - 0.05)
                self.root.attributes("-alpha", new_alpha)
            else:
                # Automatically scroll whichever menu is currently open
                if self.show_hotkey_editor:
                    self.hotkey_canvas.yview_scroll(int(-1*(e.delta/120)), "units")
                elif self.show_task_killer:
                    self.tk_canvas.yview_scroll(int(-1*(e.delta/120)), "units")
                elif self.show_startup_opt:
                    self.su_canvas.yview_scroll(int(-1*(e.delta/120)), "units")

        self.root.bind_all("<MouseWheel>", handle_scroll)

        # --- FIX: MOUSE-WHEEL SCROLLING FOR HOTKEYS ---
        def _bind_hk_scroll(canvas):
            def _on_mousewheel(event):
                canvas.yview_scroll(int(-1*(event.delta/120)), "units")
            canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_mousewheel))
            canvas.bind("<Leave>", lambda e: (canvas.unbind_all("<MouseWheel>"), self.root.bind_all("<MouseWheel>", handle_scroll)))
        _bind_hk_scroll(self.hotkey_canvas)
        
        bind_drag(self.hotkey_editor_frame)
        bind_drag(self.hotkey_status)
        bind_drag(self.hotkey_scroll_frame)

        self.root.bind_all("<MouseWheel>", handle_scroll)

        # --- ABOUT FRAME ---
        self.about_frame = tk.Frame(self.outer_frame, bg="#000000")

        self.lbl_about_version = tk.Label(
            self.about_frame,
            text="Version v1.5",
            fg="#bbbbbb",
            bg="#000000",
            font=("Segoe UI", 9, "bold")
        )
        self.lbl_about_version.pack(pady=(8, 2))

        # --- BUY ME A COFFEE BUTTON ---
        self.buy_coffee_url = "https://jaks-360-aboutme.netlify.app/"
        self.btn_coffee = tk.Button(
            self.about_frame,
            text="☕ Buy Me a Coffee",
            fg="#000000",
            bg="#ffdd00",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground="#c9a800",
            activebackground="#ffe766",
            activeforeground="#000000",
            cursor="hand2",
            command=self.open_buy_me_a_coffee
        )
        self.btn_coffee.pack(pady=(4, 8), ipadx=18, ipady=4)

        self.txt_about = scrolledtext.ScrolledText(self.about_frame, height=10, width=35, bg="#111", fg="#ccc",
                                          bd=0, highlightthickness=0, font=("Consolas", 9))
        self.txt_about.pack(fill="both", expand=True, padx=5, pady=(4, 5))

        self.update_about_text()

        # --- HARDWARE FRAME ---
        self.hardware_frame = tk.Frame(self.outer_frame, bg="#000000")
        self.txt_hardware = scrolledtext.ScrolledText(
            self.hardware_frame,
            height=16,
            width=36,
            wrap=tk.WORD,
            bg="#111",
            fg="#ccc",
            bd=0,
            highlightthickness=0,
            font=("Consolas", 9),
            padx=6,
            pady=6
        )
        self.txt_hardware.pack(fill="both", expand=True, padx=5, pady=(5, 2))
        self.txt_hardware.config(state="disabled")

        # --- COPY TO CLIPBOARD BUTTON ---
        self.btn_copy_hw = tk.Button(
            self.hardware_frame, text="📋 Copy Hardware Specs", 
            fg="#000000", bg="#00ff66", font=("Segoe UI", 9, "bold"), 
            relief="flat", cursor="hand2",
            command=lambda: (self.root.clipboard_clear(), self.root.clipboard_append(self.txt_hardware.get("1.0", "end-1c")), self.btn_copy_hw.config(text="✔ Copied!"))
        )
        self.btn_copy_hw.pack(pady=(0, 8), ipadx=10, ipady=2)

        bind_drag(self.canvas)
        bind_drag(self.stat_row)
        bind_drag(self.label_info)
        bind_drag(self.label_desktop)
        bind_drag(self.stats_frame)
        bind_drag(self.network_frame)
        
        # --- QUICK COPY IP FEATURE ---
        def copy_local_ip(e):
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                s.close()
                self.root.clipboard_clear()
                self.root.clipboard_append(ip)

                original_text = self.lbl_down.cget("text")
                self.lbl_down.config(text=f"Copied: {ip}", fg="#00ff66")
                self.root.after(3000, lambda: self.lbl_down.config(text=original_text, fg="white" if self.current_theme != "light" else "black"))
            except:
                pass

        self.network_frame.bind("<Double-Button-1>", copy_local_ip)
        self.net_inner.bind("<Double-Button-1>", copy_local_ip)
        for child in self.net_inner.winfo_children():
            child.bind("<Double-Button-1>", copy_local_ip)

        # --- HOTKEYS ---
        self.register_hotkeys()

        # PDH Setup
        self.pq = win32pdh.OpenQuery()
        self.cpu_h = self.safe_add_counter(r"\Processor Information(_Total)\% Processor Utility", r"\Processor(_Total)\% Processor Time")
        self.disk_h = self.safe_add_counter(r"\PhysicalDisk(_Total)\% Disk Time", None)
        self.gpu_h = None
        for p in [r"\GPU Engine(*)\Utilization Percentage", r"\GPU Engine(*)\UtilizationPercentage"]:
            try:
                self.gpu_h = win32pdh.AddCounter(self.pq, p)
                break
            except:
                continue

        # --- BATTERY / DESKTOP CHECK ---
        self.batt_pct = 0.0
        self.batt_watts = 0.0
        self.batt_charging = False
        self.batt_ready = False

        def battery_worker():
            try:
                import pythoncom
                pythoncom.CoInitialize()
                w = wmi.WMI(namespace="root\\wmi")
                while True:
                    try:
                        stat = w.ExecQuery("Select * from BatteryStatus")[0]
                        cap = w.ExecQuery("Select * from BatteryFullChargedCapacity")[0]
                        self.batt_pct = (stat.RemainingCapacity / cap.FullChargedCapacity) * 100
                        self.batt_charging = stat.Charging
                        self.batt_watts = (stat.ChargeRate if stat.Charging else stat.DischargeRate) / 1000.0
                        self.batt_ready = True
                    except Exception:
                        pass
                    time.sleep(10)
            except Exception:
                pass

        def gpu_worker():
            while True:
                try:
                    import GPUtil
                    gpus = GPUtil.getGPUs()
                    if gpus:
                        self._last_gpu_fallback_val = gpus[0].load * 100.0
                except Exception:
                    pass
                time.sleep(8)

        threading.Thread(target=gpu_worker, daemon=True).start()

        self.has_battery = False
        if FORCE_DESKTOP_MODE == 0:
            try:
                import pythoncom
                pythoncom.CoInitialize()
                w_temp = wmi.WMI(namespace="root\\wmi")
                bat_list = w_temp.ExecQuery("Select * from BatteryStatus")
                if bat_list and hasattr(bat_list[0], 'RemainingCapacity'):
                    self.has_battery = True
                    threading.Thread(target=battery_worker, daemon=True).start()
            except:
                pass

        self.history = []
        self.plugged_start_time = 0
        self.was_plugged = False
        self.last_recv = psutil.net_io_counters().bytes_recv
        self.last_sent = psutil.net_io_counters().bytes_sent
        self.last_discharge_watts = None

        self.apply_win11_style()
        self.update_shortcuts_content()
        
        # FIX FOR THEME BUG: Sync UI visuals with saved config immediately on launch
        self.apply_theme(self.current_theme)

        self.root.update_idletasks()
        if "--activate-island" in sys.argv:
            self.show_extended_colors = True
        self.repack_ui()
        self.dock_top_right()
        threading.Thread(target=self.run_tray_thread, daemon=True).start()
        threading.Thread(target=self.rgb_animation_loop, daemon=True).start()

        self.show()
        self.update_loop()
        self.root.mainloop()

    def build_island_activation_ui(self):
        if not hasattr(self, 'island_act_frame'): return
        for widget in self.island_act_frame.winfo_children():
            widget.destroy()
            
        bg_col = "#f5f5f5" if self.current_theme == "light" else ("#000001" if self.current_theme == "transparent" else "#000000")
        fg_col = "black" if self.current_theme == "light" else "white"
        self.island_act_frame.config(bg=bg_col)
        
        if self.is_island_activated:
            tk.Label(self.island_act_frame, text="Dynamic Island:", fg=fg_col, bg=bg_col).pack(side="left")
            tk.Label(self.island_act_frame, text="ACTIVATED", fg="#00ff66", bg=bg_col, font=("Segoe UI", 9, "bold")).pack(side="right")
        else:
            tk.Label(self.island_act_frame, text="Activate Dynamic Island", fg=fg_col, bg=bg_col, font=("Segoe UI", 9, "bold")).pack(anchor="center", pady=(0, 2))
            
            entry_bg = "#ffffff" if self.current_theme == "light" else "#111111"
            self.entry_island_key = tk.Entry(self.island_act_frame, width=25, font=("Segoe UI", 9), justify="center", bg=entry_bg, fg=fg_col, insertbackground=fg_col)
            self.entry_island_key.pack(pady=2)
            self.entry_island_key.insert(0, "Enter Key Here")
            self.entry_island_key.bind("<FocusIn>", lambda e: self.entry_island_key.delete(0, 'end') if self.entry_island_key.get() == "Enter Key Here" else None)
            
            btn_f = tk.Frame(self.island_act_frame, bg=bg_col)
            btn_f.pack(pady=2)
            
            tk.Button(btn_f, text="BUY", bg="#0078d4", fg="white", font=("Segoe UI", 8, "bold"), relief="flat", width=8,
                      command=lambda: webbrowser.open("https://jaks-360-aboutme.netlify.app/")).pack(side="left", padx=5)
            tk.Button(btn_f, text="ACTIVATE", bg="#00ff66", fg="black", font=("Segoe UI", 8, "bold"), relief="flat", width=8,
                      command=self.submit_activation).pack(side="left", padx=5)
            tk.Button(btn_f, text="TRY 2 MIN", bg="#2f6f3e", fg="white", font=("Segoe UI", 8, "bold"), relief="flat", width=10,
                      command=self.start_island_trial).pack(side="left", padx=5)

            tk.Button(
                self.island_act_frame,
                text="Activate using key...",
                bg="#3a3a3a",
                fg="#ffffff",
                font=("Segoe UI", 8, "bold"),
                relief="flat",
                command=self.open_island_activation_dialog
            ).pack(pady=(4, 0))

            rem = max(0, int(self.island_trial_until - time.time()))
            if rem > 0:
                tk.Label(self.island_act_frame, text=f"Trial active: {rem}s left", fg="#88ffb0", bg=bg_col, font=("Segoe UI", 8, "bold")).pack(pady=(2, 0))

    def submit_activation(self):
        val = getattr(self, "entry_island_key", None)
        if not val: return
        key_str = val.get().strip()
        if activate_island(key_str):
            self.is_island_activated = True
            self.island_trial_until = 0.0
            self.build_island_activation_ui()
            self.repack_ui()
            if not self.is_island_running():
                self.start_island()
        else:
            messagebox.showerror("Error", "Invalid Activation Key!")

    def start_island_trial(self):
        self.island_trial_until = time.time() + 120
        self.build_island_activation_ui()
        self.repack_ui()
        self.start_island(force_trial=True)

    def open_island_activation_dialog(self):
        if self.is_island_activated:
            if not self.is_island_running():
                self.start_island()
            return

        if self.island_activation_popup and self.island_activation_popup.winfo_exists():
            self.island_activation_popup.lift()
            self.island_activation_popup.focus_force()
            return

        popup = tk.Toplevel(self.root)
        popup.title("Activate Dynamic Island")
        popup.geometry("370x180")
        popup.configure(bg="#1e1e1e")
        popup.resizable(False, False)
        popup.attributes("-topmost", True)
        _apply_tk_icon(popup)

        self.island_activation_popup = popup

        tk.Label(
            popup,
            text="Dynamic Island requires an activation key.\n\nGet yours today or activate it in\nSmart Monitor Settings.",
            fg="#ffffff",
            bg="#1e1e1e",
            font=("Segoe UI", 11),
            justify="center"
        ).pack(pady=(14, 8))

        btn_frame = tk.Frame(popup, bg="#1e1e1e")
        btn_frame.pack(pady=8)

        tk.Button(
            btn_frame,
            text="BUY",
            bg="#0078d4",
            fg="#ffffff",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            width=10,
            command=lambda: webbrowser.open("https://jaks-360-aboutme.netlify.app/")
        ).pack(side="left", padx=6)

        tk.Button(
            btn_frame,
            text="Activate using key",
            bg="#444444",
            fg="#ffffff",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            width=16,
            command=self.activate_from_popup
        ).pack(side="left", padx=6)

        tk.Button(
            popup,
            text="Try 2 mins",
            bg="#2f6f3e",
            fg="#ffffff",
            font=("Segoe UI", 9, "bold"),
            relief="flat",
            width=14,
            command=lambda: (popup.destroy(), self.start_island_trial())
        ).pack(pady=(2, 0))

        popup.protocol("WM_DELETE_WINDOW", popup.destroy)

    def _prompt_activation_key_dialog(self):
        parent = self.island_activation_popup if (self.island_activation_popup and self.island_activation_popup.winfo_exists()) else self.root
        dlg = tk.Toplevel(parent)
        dlg.title("Activate Island")
        dlg.configure(bg="#1e1e1e")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)
        dlg.transient(parent)
        dlg.grab_set()
        _apply_tk_icon(dlg)

        tk.Label(
            dlg,
            text="Enter activation key:",
            fg="#ffffff",
            bg="#1e1e1e",
            font=("Segoe UI", 11)
        ).pack(padx=18, pady=(14, 8), anchor="w")

        key_var = tk.StringVar()
        entry = tk.Entry(
            dlg,
            textvariable=key_var,
            show="*",
            fg="#ffffff",
            bg="#111111",
            insertbackground="#ffffff",
            relief="solid",
            bd=1,
            font=("Segoe UI", 11),
            width=28
        )
        entry.pack(padx=18, pady=(0, 10))

        result = {"value": None}

        def on_ok(event=None):
            result["value"] = key_var.get().strip()
            dlg.destroy()

        def on_cancel(event=None):
            result["value"] = None
            dlg.destroy()

        btn_row = tk.Frame(dlg, bg="#1e1e1e")
        btn_row.pack(pady=(0, 12))
        tk.Button(
            btn_row,
            text="OK",
            width=10,
            bg="#2f6f3e",
            fg="#ffffff",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            command=on_ok
        ).pack(side="left", padx=(0, 8))
        tk.Button(
            btn_row,
            text="Cancel",
            width=10,
            bg="#444444",
            fg="#ffffff",
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            command=on_cancel
        ).pack(side="left")

        dlg.bind("<Return>", on_ok)
        dlg.bind("<Escape>", on_cancel)
        dlg.protocol("WM_DELETE_WINDOW", on_cancel)

        dlg.update_idletasks()
        w = dlg.winfo_reqwidth()
        h = dlg.winfo_reqheight()
        if parent and parent.winfo_exists():
            x = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        else:
            x = (dlg.winfo_screenwidth() - w) // 2
            y = (dlg.winfo_screenheight() - h) // 2
        dlg.geometry(f"{w}x{h}+{x}+{y}")

        entry.focus_set()
        dlg.wait_window()
        return result["value"]

    def activate_from_popup(self):
        key_str = self._prompt_activation_key_dialog()
        if key_str is None:
            return
        if activate_island(key_str.strip()):
            self.is_island_activated = True
            self.island_trial_until = 0.0
            self.build_island_activation_ui()
            self.repack_ui()
            if self.island_activation_popup and self.island_activation_popup.winfo_exists():
                self.island_activation_popup.destroy()
            self.start_island()
            return
        messagebox.showerror("Error", "Invalid Activation Key!", parent=self.island_activation_popup)

    # --- FEATURE: ONE-CLICK BOOST ---
    def execute_pc_boost(self):
        def boost_worker():
            try:
                # Clear User Temp
                temp_path = os.environ.get('TEMP')
                if temp_path and os.path.exists(temp_path):
                    for filename in os.listdir(temp_path):
                        file_path = os.path.join(temp_path, filename)
                        try:
                            if os.path.isfile(file_path) or os.path.islink(file_path):
                                os.unlink(file_path)
                            elif os.path.isdir(file_path):
                                shutil.rmtree(file_path)
                        except Exception:
                            pass
                
                # Attempt to empty working sets of processes
                try:
                    for proc in psutil.process_iter():
                        try:
                            handle = ctypes.windll.kernel32.OpenProcess(0x1F0FFF, False, proc.pid)
                            if handle:
                                ctypes.windll.psapi.EmptyWorkingSet(handle)
                                ctypes.windll.kernel32.CloseHandle(handle)
                        except Exception:
                            pass
                except Exception:
                    pass

                self.root.after(0, self.flash_boost_msg)
            except Exception:
                pass

        threading.Thread(target=boost_worker, daemon=True).start()

    def flash_boost_msg(self):
        self.show_boost_notif = True
        self.boost_deadline = time.time() + 3
        self.repack_ui()
        if getattr(self, 'boost_timer', None):
            try:
                self.root.after_cancel(self.boost_timer)
            except:
                pass
        self.boost_timer = self.root.after(3000, self.hide_boost_msg)

    def hide_boost_msg(self):
        self.show_boost_notif = False
        self.boost_deadline = 0
        if getattr(self, 'boost_timer', None):
            try:
                self.root.after_cancel(self.boost_timer)
            except:
                pass
            self.boost_timer = None
        self.boost_notif_frame.pack_forget()
        self.repack_ui()
        
    # --- FEATURE: TASK KILLER ---
    def toggle_task_killer(self):
        if self.show_about: self.show_about = False
        if self.show_hardware: self.show_hardware = False
        if self.show_hotkey_editor: self.show_hotkey_editor = False
        if self.show_extended_colors: self.show_extended_colors = False
        if self.show_startup_opt: self.show_startup_opt = False
        self.show_task_killer = not self.show_task_killer
        if self.show_task_killer:
            self.refresh_task_killer()
        self.repack_ui()

    def refresh_task_killer(self):
        for widget in self.tk_scroll_frame.winfo_children():
            widget.destroy()
        
        try:
            current_pid = os.getpid()
            
            try:
                current_user = psutil.Process(current_pid).username()
            except:
                current_user = None

            visible_pids = set()
            try:
                def enum_windows_proc(hwnd, lParam):
                    if ctypes.windll.user32.IsWindowVisible(hwnd):
                        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                        if length > 0:
                            pid_out = ctypes.c_ulong()
                            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_out))
                            visible_pids.add(pid_out.value)
                    return True
                EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
                ctypes.windll.user32.EnumWindows(EnumWindowsProc(enum_windows_proc), 0)
            except Exception:
                pass

            ignore_list = [
                'system idle process', 'system', 'registry', 'smss.exe', 'csrss.exe', 'wininit.exe',
                'services.exe', 'lsass.exe', 'svchost.exe', 'dwm.exe', 'explorer.exe',
                'memcompression', 'msmpeng.exe', 'taskmgr.exe', 'spoolsv.exe', 'winlogon.exe',
                'fontdrvhost.exe', 'wmiprvse.exe', 'searchui.exe', 'ctfmon.exe', 'conhost.exe',
                'dllhost.exe', 'sihost.exe', 'runtimebroker.exe', 'startmenuexperiencehost.exe',
                'searchapp.exe', 'audiodg.exe', 'dashost.exe', 'applicationframehost.exe',
                'securityhealthservice.exe', 'sgrmbroker.exe', 'wudfhost.exe', 'cmd.exe',
                'powershell.exe', 'smartscreen.exe'
            ]

            processes = []
            for proc in psutil.process_iter(['pid', 'name', 'memory_info', 'username']):
                try:
                    if current_user and proc.info.get('username') != current_user:
                        continue

                    name = (proc.info['name'] or '').lower()
                    pid = proc.info['pid']
                    if not name or name in ignore_list or pid == current_pid:
                        continue
                        
                    if pid in visible_pids:
                        continue

                    mem = proc.info['memory_info'].rss / (1024 * 1024)
                    processes.append((pid, proc.info['name'], mem))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass
            
            processes.sort(key=lambda x: x[2], reverse=True)
            top_hogs = processes[:5]

            if not top_hogs:
                tk.Label(self.tk_scroll_frame, text="No heavy user apps found.", fg="#888888", bg="#111111").pack(pady=5)

            for i, (pid, name, mem) in enumerate(top_hogs):
                f = tk.Frame(self.tk_scroll_frame, bg="#111111")
                f.pack(fill="x", pady=2)
                f.grid_columnconfigure(0, weight=1)
                f.grid_columnconfigure(1, weight=0)

                lbl = tk.Label(f, text=f"{name[:22]} ({mem:.1f}MB)", fg="#ffffff", bg="#111111", font=("Segoe UI", 9), anchor="w", justify="left", wraplength=160)
                lbl.grid(row=0, column=0, sticky="w", padx=(5, 2))

                btn = tk.Button(f, text="Kill", fg="#ffffff", bg="#cc0000", font=("Segoe UI", 8, "bold"), width=6, relief="flat", activebackground="#ff4c4c", activeforeground="white", command=lambda p=pid: self.kill_process(p))
                btn.grid(row=0, column=1, sticky="e", padx=(2, 8)) 
                
        except Exception as e:
            lbl = tk.Label(self.tk_scroll_frame, text="Error loading tasks", fg="#ff4c4c", bg="#111111")
            lbl.pack()

    def kill_process(self, pid):
        try:
            psutil.Process(pid).terminate()
            self.root.after(500, self.refresh_task_killer)
        except Exception:
            pass

    # --- FEATURE: STARTUP OPTIMIZER ---
    def open_taskmgr_startup(self):
        try:
            subprocess.Popen("taskmgr /0 /startup", shell=True)
        except Exception:
            pass

    def toggle_startup_opt(self):
        if self.show_about: self.show_about = False
        if self.show_hardware: self.show_hardware = False
        if self.show_hotkey_editor: self.show_hotkey_editor = False
        if self.show_extended_colors: self.show_extended_colors = False
        if self.show_task_killer: self.show_task_killer = False
        self.show_startup_opt = not self.show_startup_opt
        if self.show_startup_opt:
            self.refresh_startup_opt()
        self.repack_ui()

    def _list_registry_values(self, hive, path, access):
        try:
            key = winreg.OpenKey(hive, path, 0, access)
        except:
            return []
        result = []
        try:
            count = winreg.QueryInfoKey(key)[1]
            for i in range(count):
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                    result.append((name, value))
                except Exception:
                    pass
        except Exception:
            pass
        return result

    def refresh_startup_opt(self):
        for widget in self.su_scroll_frame.winfo_children():
            widget.destroy()

        sources = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "(User)"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run", "(All)"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "(All 32-bit)")
        ]

        found_any = False

        def render_entry(name, value, scope):
            f = tk.Frame(self.su_scroll_frame, bg="#222222")
            f.pack(fill="x", pady=2, padx=0)
            
            tk.Label(f, text=f"{name[:30]} {scope}", fg="#ffffff", bg="#222222", anchor="w", justify="left", wraplength=220).pack(side="left", anchor="w", padx=5, pady=2)

        for hive, run_path, scope in sources:
            run_items = self._list_registry_values(hive, run_path, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            if not run_items:
                run_items = self._list_registry_values(hive, run_path, winreg.KEY_READ | winreg.KEY_WOW64_32KEY)

            for name, value in run_items:
                render_entry(name, value, scope)
                found_any = True

        if not found_any:
            tk.Label(self.su_scroll_frame, text="No active startup applications detected.", fg="#888888", bg="#111111").pack(pady=5)

    # --- DYNAMIC ABOUT TEXT UPDATE ---
    def update_about_text(self, skip_sync=False):
        if not skip_sync:
            self.sync_island_hotkey(update_about=False)

        self.txt_about.config(state="normal")
        self.txt_about.delete("1.0", tk.END)
        
        lines = [
            "       About & Help:",
            "-----------------------",
            "    Created by JAKS 360",
            "    Version v1.5",
            "",
            "    ACTIVE HOTKEYS:"
        ]
        
        key_map = {
            "toggle_visibility": "Show/Hide App",
            "toggle_stats": "Toggle Stats",
            "toggle_network": "Toggle Network",
            "toggle_hardware": "Hardware Info",
            "toggle_colors": "Color Settings",
            "toggle_rgb": "RGB Border",
            "toggle_theme": "Switch Theme",
            "toggle_about": "About & Help",
            "exit_app": "Exit App",
            "toggle_graphs": "Toggle Graphs",
            "edit_hotkeys": "Edit Shortcuts",
            "pc_boost": "One-Click PC Boost",
            "task_killer": "Mini Task Killer",
            "startup_opt": "Startup Optimizer",
            "toggle_island": "Island On/Off"
        }

        for action, key in self.hotkeys.items():
            friendly_name = key_map.get(action, action.replace("_", " ").title())
            lines.append(f" • {friendly_name}")
            lines.append(f"   [ {str(key).upper()} ]")
            
        lines.append("")
        lines.append("       You can now  ")
        lines.append("    customize all keys!")
        lines.append("")
        lines.append("")
        lines.append("")
        
        self.txt_about.insert("1.0", "\n".join(lines))
        self.txt_about.config(state="disabled")

    # --- HOTKEY MANAGEMENT ---
    def register_hotkeys(self):
        try:
            keyboard.unhook_all_hotkeys()
        except:
            pass
        
        try:
            def bind(key, func):
                try: keyboard.add_hotkey(key, func)
                except: pass

            bind(self.hotkeys["toggle_visibility"], lambda: self.root.after(0, self.toggle))
            bind(self.hotkeys["toggle_stats"], lambda: self.root.after(0, self.toggle_stats))
            bind(self.hotkeys["toggle_network"], lambda: self.root.after(0, self.toggle_network))
            bind(self.hotkeys["toggle_hardware"], lambda: self.root.after(0, self.toggle_hardware))
            bind(self.hotkeys["toggle_colors"], lambda: self.root.after(0, self.toggle_colors))
            bind(self.hotkeys["toggle_rgb"], lambda: self.root.after(0, self.toggle_rgb))
            bind(self.hotkeys["toggle_theme"], lambda: self.root.after(0, self.toggle_theme))
            bind(self.hotkeys["toggle_about"], lambda: self.root.after(0, self.toggle_about))
            bind(self.hotkeys["exit_app"], lambda: self.root.after(0, self.exit_app))
            bind(self.hotkeys["toggle_graphs"], lambda: self.root.after(0, self.toggle_graphs))
            bind(self.hotkeys["edit_hotkeys"], lambda: self.root.after(0, self.toggle_hotkey_editor))
            bind(self.hotkeys["pc_boost"], lambda: self.root.after(0, self.execute_pc_boost))
            bind(self.hotkeys["task_killer"], lambda: self.root.after(0, self.toggle_task_killer))
            bind(self.hotkeys["startup_opt"], lambda: self.root.after(0, self.toggle_startup_opt))
        except Exception as e:
            print(f"Hotkey Error: {e}")

    def rebuild_hotkey_ui(self):
        for widget in self.hotkey_scroll_frame.winfo_children():
            widget.destroy()
        
        for action, key in self.hotkeys.items():
            if action == "toggle_island":
                continue
            f = tk.Frame(self.hotkey_scroll_frame, bg="#111")
            f.pack(fill="x", padx=10, pady=4, anchor="w")

            clean_name = action.replace("_", " ").title()
            
            lbl = tk.Label(f, text=clean_name, fg="#aaaaaa", bg="#111", anchor="w")
            lbl.pack(anchor="w")
            
            btn = tk.Button(f, text=str(key).upper(), bg="#333", fg="#ffcc00", width=20, relief="flat")
            btn.config(command=lambda a=action, b=btn: self.start_remap_hotkey(a, b))
            btn.pack(anchor="w", pady=(2, 0))
            
        # --- ADD EXTRA SCROLL SPACE FOR SHORTCUTS MENU OVERLAP ---
        tk.Frame(self.hotkey_scroll_frame, bg="#111", height=45).pack(fill="x")
        
        self.hotkey_scroll_frame.update_idletasks()
        self.hotkey_canvas.configure(scrollregion=self.hotkey_canvas.bbox("all"))

    def start_remap_hotkey(self, action, btn_widget):
        if getattr(self, 'active_remap_action', None) == action:
            self.active_remap_action = None
            btn_widget.config(text=str(self.hotkeys[action]).upper(), bg="#333", fg="#ffcc00")
            self.set_hotkey_status("Remap cancelled.", "#ffcc00")
            self.register_hotkeys()
            return
            
        if getattr(self, 'active_remap_action', None) is not None:
            self.rebuild_hotkey_ui()

        self.active_remap_action = action
        btn_widget.config(text="PRESS NEW KEY...", bg="#444", fg="#00ff66")
        self.set_hotkey_status(f"Recording {action}...", "#ffcc00")
        
        try:
            keyboard.unhook_all_hotkeys()
        except:
            pass

        threading.Thread(target=self.wait_for_hotkey_input, args=(action,), daemon=True).start()

    def wait_for_hotkey_input(self, action):
        try:
            event = keyboard.read_hotkey(suppress=False)
            
            if getattr(self, 'active_remap_action', None) != action:
                return

            if event:
                key_str = str(event)
                
                if key_str.lower() == 'esc':
                    self.active_remap_action = None
                    self.root.after(0, lambda: self.finish_remap(action, self.hotkeys[action], cancelled=True))
                    self.root.after(0, self.register_hotkeys)
                    return

                if key_str != self.hotkeys[action] and key_str in self.hotkeys.values():
                    self.active_remap_action = None
                    self.root.after(0, lambda: self.finish_remap(action, self.hotkeys[action], conflict=True))
                    self.root.after(0, self.register_hotkeys)
                    return

                self.hotkeys[action] = key_str
                self.save_config()
                self.active_remap_action = None
                
                self.root.after(0, lambda: self.finish_remap(action, key_str))
                self.root.after(0, self.register_hotkeys)
        except Exception as e:
            print(e)
            self.active_remap_action = None
            self.root.after(0, lambda: self.set_hotkey_status("Error setting key", "red"))
            self.root.after(0, self.rebuild_hotkey_ui)
            self.root.after(0, self.register_hotkeys)

    def finish_remap(self, action, key, cancelled=False, conflict=False):
        if cancelled:
            self.set_hotkey_status("Remap cancelled.", "#ffcc00")
        elif conflict:
            self.set_hotkey_status("Already taken! Reverted.", "#ff4c4c")
        else:
            self.set_hotkey_status(f"{action} to {key}", "#00ff00")
            self.update_about_text()
        self.rebuild_hotkey_ui()

    def set_hotkey_status(self, text, fg_col):
        try:
            self.hotkey_status.config(text=text, fg=fg_col)
            if getattr(self, 'hotkey_status_timer', None):
                 self.root.after_cancel(self.hotkey_status_timer)
            self.hotkey_status_timer = self.root.after(5000, lambda: self.hotkey_status.config(text="Click a button to edit its key", fg="#888"))
        except:
            pass

    def update_border_thickness(self, val):
        self.border_thickness = int(val)
        if self.current_theme != "transparent":
            self.outer_frame.config(highlightthickness=self.border_thickness)
        self.save_config()
        self.repack_ui()

    def format_minutes(self, minutes):
        total_mins = max(0, int(minutes))
        hrs = total_mins // 60
        mins = total_mins % 60
        if hrs > 0:
            return f"{hrs}h {mins} min"
        return f"{mins} min"

    def _restore_topmost(self, bring_to_front=True):
        try:
            self.root.attributes("-topmost", False)
            self.root.update_idletasks()
            self.root.attributes("-topmost", True)

            if bring_to_front:
                if not self.visible:
                    self.show()
                else:
                    self.root.deiconify()
                    self.root.lift()
                    try:
                        self.root.focus_force()
                    except:
                        pass
            self.set_tool_window()
        except:
            pass

    # --- BUY ME A COFFEE ---
    def open_buy_me_a_coffee(self):
        self.root.attributes("-topmost", False)
        def open_url():
            try:
                webbrowser.open(self.buy_coffee_url)
            except:
                pass
            self.root.after(0, lambda: self._restore_topmost(bring_to_front=False))
        threading.Thread(target=open_url, daemon=True).start()

    # --- HARDWARE PROPERTIES ---
    def gather_hardware_info(self):
        lines = []
        lines.append("HARDWARE PROPERTIES")
        lines.append("-----------------------")

        try:
            w = wmi.WMI()

            for os_info in w.Win32_OperatingSystem():
                lines.append(f"\nOS: {os_info.Caption}")
                lines.append(f"Build: {os_info.BuildNumber}")
                lines.append(f"Arch: {os_info.OSArchitecture}")

            for cpu in w.Win32_Processor():
                lines.append(f"\nCPU: {cpu.Name.strip()}")
                lines.append(f"Cores: {cpu.NumberOfCores}")
                lines.append(f"Threads: {cpu.NumberOfLogicalProcessors}")
                lines.append(f"Max Clock: {cpu.MaxClockSpeed} MHz")

            for gpu in w.Win32_VideoController():
                lines.append(f"\nGPU: {gpu.Name}")
                if gpu.AdapterRAM and gpu.AdapterRAM > 0:
                    vram_gb = gpu.AdapterRAM / (1024**3)
                    lines.append(f"VRAM: {vram_gb:.1f} GB")
                lines.append(f"Driver: {gpu.DriverVersion}")

            total_ram = 0
            ram_sticks = []
            for mem in w.Win32_PhysicalMemory():
                cap_gb = int(mem.Capacity) / (1024**3)
                total_ram += cap_gb
                speed = mem.Speed if mem.Speed else "N/A"
                ram_sticks.append(f"{cap_gb:.0f}GB @ {speed}MHz")
        
            lines.append(f"\nRAM: {total_ram:.0f} GB Total")
            for i, stick in enumerate(ram_sticks):
                lines.append(f"Slot {i}: {stick}")

            for disk in w.Win32_DiskDrive():
                size_gb = int(disk.Size) / (1024**3) if disk.Size else 0
                lines.append(f"\nDisk: {disk.Model}")
                lines.append(f"Size: {size_gb:.1f} GB")
                lines.append(f"Interface: {disk.InterfaceType}")

            for board in w.Win32_BaseBoard():
                lines.append(f"\nBoard: {board.Manufacturer}")
                lines.append(f"Product: {board.Product}")

            if self.has_battery:
                try:
                    for batt in w.Win32_Battery():
                        lines.append(f"\nBattery: {batt.Name}")
                        if batt.DesignCapacity:
                            lines.append(f"Design Cap: {batt.DesignCapacity} mWh")
                        lines.append(f"Status: {batt.Status}")
                except:
                    pass

        except Exception as e:
            lines.append(f"\nError reading hardware: {e}")

        lines.append("\n-----------------------")
        lines.append("Created by JAKS 360 | v1.5")
        return "\n".join(lines)

    def toggle_hardware(self):
        if self.show_about: self.show_about = False
        if self.show_extended_colors: self.show_extended_colors = False
        if self.show_hotkey_editor: self.show_hotkey_editor = False
        if self.show_task_killer: self.show_task_killer = False
        if self.show_startup_opt: self.show_startup_opt = False
        self.show_hardware = not self.show_hardware
        if self.show_hardware:
            self.txt_hardware.config(state="normal")
            self.txt_hardware.delete("1.0", tk.END)
            self.txt_hardware.insert("1.0", "  Loading hardware info...")
            self.txt_hardware.config(state="disabled")
            self.repack_ui()
            threading.Thread(target=self._load_hardware_async, daemon=True).start()
        else:
            self.repack_ui()

    def _load_hardware_async(self):
        def _update_ui(text_content):
            self.txt_hardware.config(state="normal")
            self.txt_hardware.delete("1.0", tk.END)
            self.txt_hardware.insert("1.0", text_content)
            self.txt_hardware.config(state="disabled")

        try:
            import pythoncom
            pythoncom.CoInitialize()
            
            info = self.gather_hardware_info()
            self.root.after(0, lambda: _update_ui(info))
            
        except Exception as e:
            err_msg = f"⚠️ THREAD CRASHED:\n{str(e)}\n\nMake sure 'pywin32' is fully installed. contract developer"
            self.root.after(0, lambda: _update_ui(err_msg))
            
        finally:
            try:
                pythoncom.CoUninitialize()
            except:
                pass

    # --- CONFIGURATION (SAVING SETTINGS) ---
    def get_config_path(self):
        return os.path.join(os.getenv('LOCALAPPDATA'), "Smart Monitor", "config.json")

    def get_island_config_path(self):
        app_data = os.getenv('LOCALAPPDATA') or os.path.expanduser("~")
        return os.path.join(app_data, "Smart Monitor", "island_config.json")

    def save_config(self):
        data = {
            "theme": self.current_theme,
            "rgb": self.rgb_enabled,
            "colors": {
                "cpu": self.color_cpu,
                "gpu": self.color_gpu,
                "ram": self.color_ram,
                "disk": self.color_disk
            },
            "manual_colors": self.color_customized,
            "shortcuts_mode": self.shortcuts_mode,
            "show_stats": self.show_stats,
            "show_network": self.show_network,
            "border_thickness": getattr(self, 'border_thickness', 2),
            "hotkeys": self.hotkeys
        }
        try:
            with open(self.get_config_path(), 'w') as f:
                json.dump(data, f)
        except:
            pass

    def load_config(self):
        try:
            path = self.get_config_path()
            if os.path.exists(path):
                with open(path, 'r') as f:
                    data = json.load(f)
                    self.current_theme = data.get("theme", "dark")
                    self.rgb_enabled = data.get("rgb", False)
                    cols = data.get("colors")
                    if cols:
                        self.color_cpu = cols.get("cpu", self.color_cpu)
                        self.color_gpu = cols.get("gpu", self.color_gpu)
                        self.color_ram = cols.get("ram", self.color_ram)
                        self.color_disk = cols.get("disk", self.color_disk)
                        self.color_customized = True
                    else:
                        self.color_customized = data.get("manual_colors", False)

                    loaded_mode = data.get("shortcuts_mode", 1)
                    if loaded_mode == 0: 
                        loaded_mode = 1
                    self.shortcuts_mode = loaded_mode

                    self.show_stats = data.get("show_stats", True)
                    self.show_network = data.get("show_network", True)
                    self.border_thickness = data.get("border_thickness", 2)
                    
                    loaded_keys = data.get("hotkeys", {})
                    if loaded_keys:
                        self.hotkeys.update(loaded_keys)
        except:
            pass

    def sync_island_hotkey(self, update_about=False, force=False):
        """Pull the Island toggle hotkey from island_config.json so About stays in sync."""
        try:
            cfg_path = self.get_island_config_path()
            if not os.path.exists(cfg_path):
                return False

            mtime = os.path.getmtime(cfg_path)
            if not force and getattr(self, '_last_island_cfg_mtime', None) == mtime:
                return False

            with open(cfg_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            island_hotkey = data.get("toggle_hotkey")
            if island_hotkey:
                if self.hotkeys.get("toggle_island") != island_hotkey:
                    self.hotkeys["toggle_island"] = island_hotkey
                    self.save_config()
                    if update_about:
                        self.update_about_text(skip_sync=True)

            self._last_island_cfg_mtime = mtime
            return True
        except Exception:
            return False

    # --- TRIAL MODE LOGIC ---
    def run_trial_timer(self):
        if hasattr(self, 'root') and self.root:
            self.trial_timer = self.root.after(60000, self.enforce_trial_lockdown)

    def enforce_trial_lockdown(self):
        if not self.is_premium:
            self.trial_ended = True
            self.repack_ui()

    def force_repin(self):
        # Force a hide/show cycle to guarantee z-order after rating flow
        try:
            self.hide()
        except:
            pass
        self.root.after(10, self.show)

    def unlock_premium_features(self):
        self.root.attributes("-topmost", False)
        def open_store():
            try:
                os.startfile("ms-windows-store://pdp/?ProductId=9p9j9tfq00ht")
            except:
                pass
        threading.Thread(target=open_store, daemon=True).start()
        self.mark_as_rated()
        self.is_premium = True
        self.trial_ended = False
        self.show_stats = True
        self.save_config()
        self.repack_ui()
        self.root.after(30000, self.force_repin)

    # --- RGB & THEME METHODS ---
    def toggle_rgb(self):
        self.rgb_enabled = not self.rgb_enabled
        
        if self.rgb_enabled:
            # Enabling RGB: save current thickness and set to 7
            self.prev_border_thickness = self.border_thickness
            self.border_thickness = 7
            self.outer_frame.config(highlightthickness=self.border_thickness)
            self.thickness_slider.set(7)
        else:
            # Disabling RGB: restore previous thickness
            self.border_thickness = self.prev_border_thickness
            self.outer_frame.config(highlightthickness=self.border_thickness)
            self.thickness_slider.set(self.border_thickness)
            col = "#444444" if self.current_theme in ("dark", "transparent") else "#cccccc"
            self.outer_frame.config(highlightbackground=col)
            self.shortcuts_canvas.config(highlightbackground=col)
        
        self.save_config()

    def toggle_theme(self):
        if self.current_theme == "dark":
            self.apply_theme("light")
        elif self.current_theme == "light":
            self.apply_theme("transparent")
        else:
            self.apply_theme("dark")
        self.save_config()

    def rgb_animation_loop(self):
        while True:
            if self.rgb_enabled:
                import colorsys
                rgb = colorsys.hsv_to_rgb(self.rgb_hue / 360.0, 1.0, 1.0)
                color = f'#{int(rgb[0]*255):02x}{int(rgb[1]*255):02x}{int(rgb[2]*255):02x}'
                try:
                    def update_borders(c):
                        self.outer_frame.config(highlightbackground=c)
                        self.shortcuts_canvas.config(highlightbackground=c)
                    self.root.after(0, lambda c=color: update_borders(c))
                except:
                    pass
                self.rgb_hue = (self.rgb_hue + 5) % 360
            threading.Event().wait(0.08)

    def apply_theme(self, theme):
        self.current_theme = theme

        try:
            self.root.attributes("-transparentcolor", "")
        except:
            pass

        if theme == "dark":
            bg, fg, border = "#000000", "#ffffff", "#444444"
            canvas_bg = "#000000"
            info_fg = "#aaaaaa"
            net_fg = "white"
            net_sep_fg = "#cccccc"
            about_bg = "#111"
            about_fg = "#ccc"
            self.outer_frame.config(highlightthickness=self.border_thickness)
            self.drag_grip.pack_forget()
            if not self.color_customized:
                self.color_cpu = "#00d4ff"
                self.color_gpu = "#50fa7b"
                self.color_ram = "#ff79c6"
                self.color_disk = "#bd93f9"
            self.label_desktop.config(fg="#00d4ff")

        elif theme == "light":
            bg, fg, border = "#f5f5f5", "#000000", "#cccccc"
            canvas_bg = "#f5f5f5"
            info_fg = "#555555"
            net_fg = "#000000"
            net_sep_fg = "#333333"
            about_bg = "#ffffff"
            about_fg = "#333333"
            self.outer_frame.config(highlightthickness=self.border_thickness)
            self.drag_grip.pack_forget()
            if not self.color_customized:
                self.color_cpu = "#0066cc"
                self.color_gpu = "#009933"
                self.color_ram = "#c71585"
                self.color_disk = "#800080"
            self.label_desktop.config(fg="#0066cc")

        elif theme == "transparent":
            bg = "#000001"
            fg, border = "#ffffff", "#444444"
            canvas_bg = "#000001"
            info_fg = "#ffffff"
            net_fg = "white"
            net_sep_fg = "#cccccc"
            about_bg = "#111"
            about_fg = "#ccc"
            self.outer_frame.config(highlightthickness=0)
            self.drag_grip.pack(side="top", pady=(0, 2))
            if not self.color_customized:
                self.color_cpu = "#00d4ff"
                self.color_gpu = "#50fa7b"
                self.color_ram = "#ff79c6"
                self.color_disk = "#bd93f9"
            self.label_desktop.config(fg="#00d4ff")
            try:
                self.root.attributes("-transparentcolor", bg)
            except:
                pass

        self.root.config(bg=bg)
        self.outer_frame.config(bg=bg)
        if not self.rgb_enabled:
            self.outer_frame.config(highlightbackground=border)
        self.canvas.config(bg=canvas_bg)

        for widget in [self.stat_row, self.label_info, self.stats_frame,
                       self.network_frame, self.color_frame, self.about_frame,
                       self.row1, self.row2, self.net_inner, self.trial_lock_frame,
                       self.lock_msg, self.lock_desc, self.label_desktop,
                       self.hardware_frame, self.graph_frame, self.hotkey_editor_frame, 
                       self.hotkey_status, self.hotkey_canvas, self.hotkey_scroll_frame,
                       self.task_killer_frame, self.tk_lbl_title, self.tk_scroll_frame,
                       self.startup_frame, self.su_lbl_title, self.su_scroll_frame]:
            try:
                widget.config(bg=bg)
            except:
                pass

        self.label_pct.config(bg=bg)
        self.label_watts.config(bg=bg)
        self.label_info.config(bg=bg, fg=info_fg)
        self.graph_canvas.config(bg=bg) 

        self.lbl_cpu.config(bg=bg, fg=self.color_cpu)
        self.lbl_gpu.config(bg=bg, fg=self.color_gpu)
        self.lbl_ram.config(bg=bg, fg=self.color_ram)
        self.lbl_disk.config(bg=bg, fg=self.color_disk)

        if theme == "light":
            self.lock_msg.config(bg=bg, fg="#cc0000")
            self.lock_desc.config(bg=bg, fg="#555555")
            self.btn_unlock.config(bg="#e0e0e0", fg="#cc8800")
        else:
            self.lock_msg.config(bg=bg, fg="#ff4444")
            self.lock_desc.config(bg=bg, fg="#dddddd")
            self.btn_unlock.config(bg="#222222", fg="#ffcc00")

        self.arr_down.config(bg=bg, fg=net_fg)
        self.arr_up.config(bg=bg, fg=net_fg)
        self.lbl_down.config(bg=bg, fg=net_fg)
        self.lbl_up.config(bg=bg, fg=net_fg)

        for child in self.net_inner.winfo_children():
            if isinstance(child, tk.Label) and child.cget("text") == "|":
                child.config(bg=bg, fg=net_sep_fg)

        for child in self.color_frame.winfo_children():
            if isinstance(child, tk.Frame):
                child.config(bg=bg)
                for subchild in child.winfo_children():
                    if isinstance(subchild, tk.Label):
                        subchild.config(bg=bg, fg=fg)
                    elif isinstance(subchild, tk.Scale):
                        subchild.config(bg=bg, fg=fg)

        self.txt_about.config(bg=about_bg, fg=about_fg)
        self.txt_hardware.config(bg=about_bg, fg=about_fg)
        self.lbl_about_version.config(bg=bg, fg=about_fg)
        self.btn_coffee.config(bg="#FFDD00", fg="#000000", activebackground="#FFE94A", activeforeground="#000000")

        if theme == "light":
            self.arrow_canvas.config(bg="#f5f5f5")
            self.arrow_canvas.itemconfig(2, fill="#000000", outline="#000000")
            self.arrow_canvas.itemconfig(1, fill="#cccccc")
        elif theme == "transparent":
            self.arrow_canvas.config(bg=bg)
            self.arrow_canvas.itemconfig(2, fill="#ffffff", outline="#ffffff")
            self.arrow_canvas.itemconfig(1, fill="#555555")
        else:
            self.arrow_canvas.config(bg="#000000")
            self.arrow_canvas.itemconfig(2, fill="#ffffff", outline="#ffffff")
            self.arrow_canvas.itemconfig(1, fill="#555555")

        self.update_shortcuts_content()
        self.save_config()

    def update_shortcuts_content(self):
        bg = "#000000"
        if self.current_theme == "light":
            bg = "#f5f5f5"
        elif self.current_theme == "transparent":
            bg = "#000001"

        border = "#444444"
        if self.current_theme == "light":
            border = "#cccccc"

        text_col = "#ffffff"
        if self.current_theme == "light":
            text_col = "#000000"

        footer_col = "#bbbbbb"
        if self.current_theme == "light":
            footer_col = "#666666"

        line_col = "#444444"
        if self.current_theme == "light":
            line_col = "#dddddd"

        self.shortcuts_canvas.config(bg=bg)
        if not self.rgb_enabled:
            self.shortcuts_canvas.config(highlightbackground=border)

        rect_col = "#666666" if self.current_theme != "light" else "#999999"
        fill_col = "#111111" if self.current_theme != "light" else "#ffffff"
        self.shortcuts_canvas.itemconfig(1, outline=rect_col, fill=fill_col)
        self.shortcuts_canvas.itemconfig(self.sc_line_id, fill=line_col)

        self.shortcuts_canvas.itemconfig(self.sc_text_id, text="SHORTCUTS KEYS", fill=text_col, font=("Segoe UI", 8, "bold"))
        self.shortcuts_canvas.itemconfig(self.sc_footer_id, text="Created by JAKS 360 | v1.5", fill=footer_col)

    def set_tool_window(self):
        """Set window as tool window to remove taskbar entry and ensure proper styling."""
        try:
            hwnd = self._get_window_handle()
            
            # GWL_EXSTYLE = -20
            # WS_EX_TOOLWINDOW = 0x00000080
            style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            style = style | 0x00000080
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)
        except Exception as e:
            # Silently pass - not critical for functionality
            pass

    def _get_window_handle(self):
        hwnd = int(self.root.winfo_id())
        try:
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent:
                return parent
        except Exception:
            pass
        return hwnd

    def _apply_round_region(self, hwnd):
        width = max(1, self.root.winfo_width())
        height = max(1, self.root.winfo_height())
        if width < 10 or height < 10:
            return False

        radius = 18
        rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
        if not rgn:
            return False

        ok = ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
        if ok == 0:
            ctypes.windll.gdi32.DeleteObject(rgn)
            return False
        return True

    def schedule_win11_style_refresh(self, delay_ms=40):
        try:
            if self._corner_refresh_job is not None:
                self.root.after_cancel(self._corner_refresh_job)
        except Exception:
            pass
        self._corner_refresh_job = self.root.after(delay_ms, self.apply_win11_style)

    def apply_win11_style(self, retry_count=0):
        """Apply Win11 rounded corners with DWM and a hard region fallback for frozen EXE builds."""
        self._corner_refresh_job = None
        try:
            hwnd = self._get_window_handle()
            if hwnd <= 0:
                raise RuntimeError("Invalid window handle")

            # Preferred Win11 path: ask DWM for rounded corners.
            dwm_ok = False
            try:
                corner_preference = ctypes.c_int(2)  # DWMWCP_ROUND
                result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    33,  # DWMWA_WINDOW_CORNER_PREFERENCE
                    ctypes.byref(corner_preference),
                    ctypes.sizeof(corner_preference),
                )
                dwm_ok = (result == 0)
            except Exception:
                dwm_ok = False

            # Fallback path: force a rounded region so EXE builds still look rounded.
            region_ok = self._apply_round_region(hwnd)

            if not (dwm_ok or region_ok) and retry_count < 4:
                self.root.after(120, lambda: self.apply_win11_style(retry_count + 1))
        except Exception:
            if retry_count < 4:
                self.root.after(120, lambda: self.apply_win11_style(retry_count + 1))

    def safe_add_counter(self, path1, path2):
        try:
            return win32pdh.AddCounter(self.pq, path1)
        except:
            if path2:
                try:
                    return win32pdh.AddCounter(self.pq, path2)
                except:
                    pass
        return None

    def set_col(self, target):
        self.root.attributes("-topmost", False)
        c = colorchooser.askcolor(title=f"Choose {target.upper()} Color", parent=self.root)[1]
        self.root.attributes("-topmost", True)
        self.root.lift()
        if c:
            self.color_customized = True
            if target == 'cpu':
                self.color_cpu = c
                self.lbl_cpu.config(fg=c)
                self.btn_cpu.config(bg=c, fg=c)
            if target == 'gpu':
                self.color_gpu = c
                self.lbl_gpu.config(fg=c)
                self.btn_gpu.config(bg=c, fg=c)
            if target == 'ram':
                self.color_ram = c
                self.lbl_ram.config(fg=c)
                self.btn_ram.config(bg=c, fg=c)
            if target == 'disk':
                self.color_disk = c
                self.lbl_disk.config(fg=c)
                self.btn_dsk.config(bg=c, fg=c)
            self.save_config()

    def toggle_shortcuts_mode(self):
        if self.shortcuts_mode == 1:
            self.shortcuts_mode = 2
        else:
            self.shortcuts_mode = 1
            
        self.save_config()

        if self.shortcuts_mode == 1: 
            self.btn_sc_toggle.config(text="FIXED", bg="#00ff00", fg="black")
            self.show_shortcuts()
        elif self.shortcuts_mode == 2: 
            self.btn_sc_toggle.config(text="OFF", bg="#ff3333", fg="white")
            self.hide_shortcuts()

    def repack_ui(self):
        self.arrow_canvas.config(width=self.width)
        self.shortcuts_canvas.config(width=self.width)
        self.arrow_canvas.coords(2, self.width // 2 - 4, 4, self.width // 2 + 4, 4, self.width // 2, 12)
        self.shortcuts_canvas.coords(self.sc_text_id, self.width // 2, 20)
        self.shortcuts_canvas.coords(self.sc_footer_id, self.width // 2, 50)
        self.shortcuts_canvas.coords(self.sc_line_id, 40, 35, self.width - 40, 35)
        self.shortcuts_canvas.coords(1, 2, 2, self.width-2, self.shortcuts_height_val-2) 

        desired_widgets = []

        if self.current_theme == "transparent":
            desired_widgets.append((self.drag_grip, {"side": "top", "pady": (0, 2)}))

        h = 0
        if self.current_theme == "transparent": h += 6

        if self.has_battery:
            desired_widgets.append((self.canvas, {"pady": (8, 0)}))
            desired_widgets.append((self.stat_row, {"fill": "x", "padx": 15, "pady": (2, 0)}))
            desired_widgets.append((self.label_info, {"pady": (2, 5)}))
            h += 135
        else:
            desired_widgets.append((self.label_desktop, {"pady": (10, 5)}))
            h += 55

        if self.show_boost_notif:
            desired_widgets.append((self.boost_notif_frame, {"fill": "x", "pady": (0, 5)}))
            h += 45

        if self.trial_ended and not self.is_premium:
            desired_widgets.append((self.trial_lock_frame, {"fill": "x", "pady": (5, 5)}))
            self.trial_lock_frame.update_idletasks()
            h += self.trial_lock_frame.winfo_reqheight() + 10
        else:
            if self.show_stats:
                desired_widgets.append((self.stats_frame, {"fill": "x", "pady": (2, 0)}))
                h += 65 + 2

        if self.show_graphs:
            desired_widgets.append((self.graph_frame, {"fill": "x", "pady": (5, 5)}))
            h += 65

        if self.show_network:
            top_pad = 5 if self.show_stats else 5
            desired_widgets.append((self.network_frame, {"fill": "x", "pady": (top_pad, 15)}))
            h += 55 + top_pad

        if self.show_task_killer:
            desired_widgets.append((self.task_killer_frame, {"fill": "both", "expand": True, "pady": (5, 5)}))
            h += 260

        if self.show_startup_opt:
            desired_widgets.append((self.startup_frame, {"fill": "both", "expand": True, "pady": (5, 5)}))
            h += 280

        if self.show_extended_colors:
            desired_widgets.append((self.color_frame, {"fill": "x", "pady": (10, 10)}))
            h += 430

        if self.show_hotkey_editor:
            desired_widgets.append((self.hotkey_editor_frame, {"fill": "both", "expand": True, "pady": (5, 10)}))
            h += 250
            self.rebuild_hotkey_ui()

        if self.show_about:
            desired_widgets.append((self.about_frame, {"fill": "both", "expand": True, "pady": (5, 10)}))
            h += 300 

        if self.show_hardware:
            desired_widgets.append((self.hardware_frame, {"fill": "both", "expand": True, "pady": (5, 10)}))
            h += 320

        if self.shortcuts_visible:
            h += self.shortcuts_height_val

        # FIXED: Maintain a constant base height addition for the border so height DOES NOT expand.
        h += 4  

        current_slaves = self.outer_frame.pack_slaves()
        desired_objs = [w for w, _ in desired_widgets]

        if current_slaves != desired_objs:
            for w in current_slaves:
                w.pack_forget()
            for w, kwargs in desired_widgets:
                w.pack(**kwargs)
        
        self.root.update_idletasks()
        if h != self.current_height or self.root.winfo_width() != self.width:
            self.current_height = h
            cx = self.root.winfo_x()
            cy = self.root.winfo_y()
            if cx < 0 and not self.visible:
                self.root.geometry(f"{self.width}x{h}")
            else:
                self.root.geometry(f"{self.width}x{h}+{cx}+{cy}")
            
        if self.arrow_visible:
            self.arrow_canvas.place(x=0, y=h-16, width=self.width, height=16)
        else:
            self.arrow_canvas.place_forget()
        
        if self.shortcuts_visible:
            self.shortcuts_canvas.place(x=0, y=h-self.shortcuts_height_val, width=self.width, height=self.shortcuts_height_val)
            self.shortcuts_canvas.lift()
        else:
            self.shortcuts_canvas.place_forget()

    def toggle(self):
        self.hide() if self.visible else self.show()

    def show(self):
        if not getattr(self, 'has_moved', False):
            self.dock_top_right()
        else:
            cx = self.root.winfo_x()
            cy = self.root.winfo_y()
            self.root.geometry(f"{self.width}x{self.current_height}+{cx}+{cy}")
            if self.arrow_visible:
                self.arrow_canvas.place(x=0, y=self.current_height-16, width=self.width, height=16)
            if self.shortcuts_visible:
                self.shortcuts_canvas.place(x=0, y=self.current_height-self.shortcuts_height_val, width=self.width, height=self.shortcuts_height_val)

        self.root.deiconify()
        self.root.attributes("-alpha", 0.92)
        self.bring_to_front()
        self.visible = True
        self.root.after(20, self.set_tool_window)
        self.root.after(25, self.apply_win11_style)  # Ensure rounded corners are applied when window becomes visible
        self.root.after(30, self.root.lift)
        self.root.after(35, lambda: (self.start_arrow_cycle(), self.start_shortcuts_cycle()))

        if self.shortcuts_mode == 1:
            self.root.after(50, self.show_shortcuts)

    def hide(self):
        self.root.attributes("-alpha", 0.0)
        self.root.withdraw()
        self.hide_arrow_button()
        if self.shortcuts_visible:
            self.shortcuts_visible = False
            self.shortcuts_canvas.place_forget()
            self.repack_ui()
        if self.arrow_cycle_timer:
            self.root.after_cancel(self.arrow_cycle_timer)
            self.arrow_cycle_timer = None
        if self.shortcuts_cycle_timer:
            self.root.after_cancel(self.shortcuts_cycle_timer)
            self.shortcuts_cycle_timer = None
        if self.shortcuts_timer:
            self.root.after_cancel(self.shortcuts_timer)
            self.shortcuts_timer = None
        self.visible = False

    def bring_to_front(self):
        try:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.update_idletasks()
            try:
                self.root.focus_force()
            except:
                pass
        except:
            pass

    def toggle_stats(self):
        self.show_stats = not self.show_stats
        self.save_config()
        self.repack_ui()

    def toggle_network(self):
        self.show_network = not self.show_network
        self.save_config()
        self.repack_ui()

    def toggle_colors(self):
        if self.show_about: self.show_about = False
        if self.show_hardware: self.show_hardware = False
        if self.show_hotkey_editor: self.show_hotkey_editor = False
        if self.show_task_killer: self.show_task_killer = False
        if self.show_startup_opt: self.show_startup_opt = False
        self.show_extended_colors = not self.show_extended_colors
        self.repack_ui()

    def toggle_about(self):
        if self.show_extended_colors: self.show_extended_colors = False
        if self.show_hardware: self.show_hardware = False
        if self.show_hotkey_editor: self.show_hotkey_editor = False
        if self.show_task_killer: self.show_task_killer = False
        if self.show_startup_opt: self.show_startup_opt = False
        self.show_about = not self.show_about
        if self.show_about:
            self.update_about_text()
        self.repack_ui()
    
    def toggle_graphs(self):
        self.show_graphs = not self.show_graphs
        self.repack_ui()

    def toggle_hotkey_editor(self):
        if self.show_about: self.show_about = False
        if self.show_hardware: self.show_hardware = False
        if self.show_extended_colors: self.show_extended_colors = False
        if self.show_task_killer: self.show_task_killer = False
        if self.show_startup_opt: self.show_startup_opt = False
        self.show_hotkey_editor = not self.show_hotkey_editor
        self.repack_ui()

    def show_arrow_button(self):
        if not self.arrow_visible and self.visible:
            self.arrow_canvas.place(x=0, y=self.current_height-16, width=self.width, height=16)
            self.arrow_visible = True
            if self.arrow_timer:
                self.root.after_cancel(self.arrow_timer)
            self.arrow_timer = self.root.after(10000, self.hide_arrow_button)

    def hide_arrow_button(self):
        if self.arrow_visible:
            self.arrow_canvas.place_forget()
            self.arrow_visible = False
            self.arrow_timer = None
            if self.arrow_cycle_timer:
                self.root.after_cancel(self.arrow_cycle_timer)
            self.arrow_cycle_timer = self.root.after(120000, self.show_arrow_button)

    def start_arrow_cycle(self):
        if self.arrow_cycle_timer:
            self.root.after_cancel(self.arrow_cycle_timer)

    def get_launch_count(self):
        app_data = os.getenv('LOCALAPPDATA')
        if not app_data:
            app_data = os.path.expanduser("~")
        launch_file = os.path.join(app_data, "Smart Monitor", "launch_count.txt")
        try:
            with open(launch_file, "r") as f:
                return int(f.read().strip())
        except:
            return 0

    def increment_launch_count(self):
        app_data = os.getenv('LOCALAPPDATA')
        if not app_data:
            app_data = os.path.expanduser("~")
        launch_dir = os.path.join(app_data, "Smart Monitor")
        launch_file = os.path.join(launch_dir, "launch_count.txt")
        if not os.path.exists(launch_dir):
            try:
                os.makedirs(launch_dir)
            except:
                pass
        count = self.get_launch_count() + 1
        try:
            with open(launch_file, "w") as f:
                f.write(str(count))
        except:
            pass
        return count

    def check_if_rated(self):
        app_data = os.getenv('LOCALAPPDATA')
        if not app_data:
            app_data = os.path.expanduser("~")
        check_file = os.path.join(app_data, "Smart Monitor", "rated_check.tag")
        return os.path.exists(check_file)

    def mark_as_rated(self):
        app_data = os.getenv('LOCALAPPDATA')
        if not app_data:
            app_data = os.path.expanduser("~")

        save_folder = os.path.join(app_data, "Smart Monitor")
        check_file = os.path.join(save_folder, "rated_check.tag")

        try:
            if not os.path.exists(save_folder):
                os.makedirs(save_folder)
            with open(check_file, "w") as f:
                f.write("rated")
        except Exception as e:
            print(f"ERROR SAVING RATING: {e}")

    def should_show_shortcuts(self, launch_count):
        return True 

    def show_shortcuts(self):
        if self.shortcuts_mode != 1 or not self.visible:
            return

        if not self.shortcuts_visible:
            self.shortcuts_visible = True
            self.repack_ui()

    def hide_shortcuts(self):
        if self.shortcuts_visible:
            self.shortcuts_visible = False
            self.shortcuts_canvas.place_forget()
            self.repack_ui()

        self.shortcuts_timer = None
        self.shortcuts_cycle_timer = None

    def start_shortcuts_cycle(self):
        pass

    def shortcuts_cycle(self):
        pass

    def draw_graphs(self):
        w = 260
        cpu_pts = []
        step = w / (len(self.graph_history_cpu) - 1) if len(self.graph_history_cpu) > 1 else w
        for i, val in enumerate(self.graph_history_cpu):
            x = i * step
            y = 55 - (val / 100.0 * 40)
            cpu_pts.append(x)
            cpu_pts.append(y)
         
        if len(cpu_pts) >= 4:
            self.graph_canvas.coords(self.cpu_line, *cpu_pts)
            self.graph_canvas.itemconfig(self.cpu_line, fill=self.color_cpu)
            self.graph_canvas.itemconfig(self.cpu_txt, fill=self.color_cpu)

        gpu_pts = []
        step = w / (len(self.graph_history_gpu) - 1) if len(self.graph_history_gpu) > 1 else w
        for i, val in enumerate(self.graph_history_gpu):
            x = i * step
            y = 55 - (val / 100.0 * 40)
            gpu_pts.append(x)
            gpu_pts.append(y)
            
        if len(gpu_pts) >= 4:
            self.graph_canvas.coords(self.gpu_line, *gpu_pts)
            self.graph_canvas.itemconfig(self.gpu_line, fill=self.color_gpu)
            self.graph_canvas.itemconfig(self.gpu_txt, fill=self.color_gpu)
            
        self.graph_canvas.itemconfig(self.bg_rect, fill=self.graph_canvas.cget("bg"))

    def update_loop(self):
        if not hasattr(self, 'loop_counter'):
            self.loop_counter = 0
        self.loop_counter += 1

        # Keep Island hotkey in sync so About shows the latest value
        self.sync_island_hotkey(update_about=self.show_about)

        if getattr(self, "show_boost_notif", False) and getattr(self, "boost_deadline", 0):
            if time.time() > self.boost_deadline:
                try:
                    self.hide_boost_msg()
                except Exception:
                    pass

        try:
            if self.has_battery:
                if self.batt_ready:
                    pct = self.batt_pct
                    charging = self.batt_charging
                    watts = self.batt_watts
                else:
                    pct, charging, watts = 0, False, 0.0

                sign = '+' if charging else '-'
                
                watts_color = "#ffffff" if charging else ("#ff4c4c" if watts >= 30 else "#ffffff")
                if self.current_theme == "light":
                    watts_color = "#000000" if charging else ("#cc0000" if watts >= 30 else "#000000")

                self.label_watts.config(text=f"{sign}{watts:.2f} W", fg=watts_color)

                if self.current_theme == "light":
                    if charging:
                        color = "#0066cc"
                    elif pct >= 90.0:
                        color = "#0055aa"
                    elif pct >= 60.0:
                        color = "#009933"
                    elif pct >= 30.0:
                        color = "#cc9900"
                    else:
                        color = "#cc0000"
                else:
                    if charging:
                        color = "#4fc3ff"
                    elif pct >= 90.0:
                        color = "#1b9de3"
                    elif pct > 89.0:
                        ratio = (pct - 89.0)
                        color = f'#{int(0+(27-0)*ratio):02x}{int(255+(157-255)*ratio):02x}{int(102+(227-102)*ratio):02x}'
                    elif pct >= 60.0:
                        color = "#00ff66"
                    elif pct > 59.0:
                        ratio = (pct - 59.0)
                        color = f'#{int(255+(0-255)*ratio):02x}{int(192+(255-192)*ratio):02x}{int(0+(102-0)*ratio):02x}'
                    elif pct >= 30.0:
                        color = "#ffc000"
                    elif pct > 29.0:
                        ratio = (pct - 29.0)
                        color = f'#ff{int(76+(192-76)*ratio):02x}{int(76+(0-76)*ratio):02x}'
                    else:
                        color = "#ff4c4c"

                self.canvas.itemconfig(self.battery_fill, fill=color)
                self.label_pct.config(text=f"{pct:.1f}%", fg=color)
                self.canvas.coords(self.battery_fill, 17, 8, 17 + int((pct/100)*196), 24)

                charging_fg = "#0066cc" if self.current_theme == "light" else "#4fc3ff"

                if charging:
                    if not self.was_plugged:
                        self.plugged_start_time, self.history = 0, []
                        self.was_plugged = True
                    if self.plugged_start_time < 5:
                        self.plugged_start_time += 1
                        self.label_info.config(text="Charger handshake ⚡", fg=charging_fg)
                    elif watts > 0:
                        mins = ((100.0 - pct) / 100.0) * (60.0 / max(0.1, watts)) * 60.0
                        self.history.append(mins)
                        if len(self.history) > 60:
                            self.history.pop(0)
                        avg = statistics.median(self.history[-5:])
                        self.label_info.config(text=f"{self.format_minutes(avg)} to full ⚡", fg=charging_fg)
                else:
                    self.was_plugged = False
                    estimate_watts = watts if watts > 0 else self.last_discharge_watts
                    if watts > 0:
                        self.last_discharge_watts = watts
                    if estimate_watts and estimate_watts > 0:
                        mins = (pct / 100.0) * (60.0 / estimate_watts) * 60.0
                        self.history.append(mins)
                        if len(self.history) > 60:
                            self.history.pop(0)
                        avg = statistics.median(self.history[-5:])
                        info_fg = "#555555" if self.current_theme == "light" else "#dddddd"
                        self.label_info.config(text=f"{self.format_minutes(avg)} remaining", fg=info_fg)
                    else:
                        info_fg = "#999999" if self.current_theme == "light" else "#777777"
                        self.label_info.config(text="     Smart charging ❤️", fg=info_fg)

            win32pdh.CollectQueryData(self.pq)
            c_val = win32pdh.GetFormattedCounterValue(self.cpu_h, win32pdh.PDH_FMT_DOUBLE)[1] if self.cpu_h else 0
            d_val = win32pdh.GetFormattedCounterValue(self.disk_h, win32pdh.PDH_FMT_DOUBLE)[1] if self.disk_h else 0
            g_val = 0
            if self.gpu_h:
                data = win32pdh.GetFormattedCounterArray(self.gpu_h, win32pdh.PDH_FMT_DOUBLE)
                engine_totals = {}
                for key, val in data.items():
                    if 'engtype_' in key:
                        etype = key.split('engtype_')[-1]
                        engine_totals[etype] = engine_totals.get(etype, 0.0) + val
                if engine_totals:
                    g_val = max(engine_totals.values())

            if g_val == 0:
                g_val = self._last_gpu_fallback_val

            g_val = min(g_val, 100.0)
            c_val = min(c_val, 100.0)
            r_val = psutil.virtual_memory().percent

            self.graph_history_cpu.append(c_val)
            self.graph_history_gpu.append(g_val)
            if self.show_graphs:
                now = time.time()
                if now - self._last_graph_draw_ts >= 2.0:
                    self.draw_graphs()
                    self._last_graph_draw_ts = now

            if self.show_stats:
                self.lbl_cpu.config(text=f"CPU:{c_val:>3.0f}%", fg="#ff4c4c" if c_val > 85 else self.color_cpu)
                self.lbl_gpu.config(text=f"GPU:{g_val:>3.0f}%", fg="#ff4c4c" if g_val > 85 else self.color_gpu)
                self.lbl_ram.config(text=f"RAM:{r_val:>3.0f}%", fg="#ff4c4c" if r_val > 85 else self.color_ram)
                self.lbl_disk.config(text=f"DSK:{d_val:>3.0f}%", fg="#ff4c4c" if d_val > 85 else self.color_disk)

            curr = psutil.net_io_counters()
            d = (curr.bytes_recv - self.last_recv) * 8 / 1000000
            u = (curr.bytes_sent - self.last_sent) * 8 / 1000000
            self.last_recv, self.last_sent = curr.bytes_recv, curr.bytes_sent
            if self.show_network:
                self.lbl_down.config(text=f"{d:.1f} Mbps" if d >= 1 else f"{int(d * 1000)} Kbps")
                self.lbl_up.config(text=f"{u:.1f} Mbps" if u >= 1 else f"{int(u * 1000)} Kbps")
                high_speed_fg = "#ff0000" if self.current_theme == "light" else "#ff4d4d"
                normal_fg = "#000000" if self.current_theme == "light" else "white"
                self.arr_down.config(fg=high_speed_fg if d >= 10 else normal_fg)
                self.arr_up.config(fg=high_speed_fg if u >= 10 else normal_fg)

        except Exception:
            pass
        self.root.after(1000, self.update_loop)

    def start_move(self, event):
        self.x, self.y = event.x, event.y
        
    def do_move(self, event):
        self.has_moved = True
        self.root.geometry(f"+{self.root.winfo_x() + (event.x - self.x)}+{self.root.winfo_y() + (event.y - self.y)}")
        
    def dock_top_right(self):
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"{self.width}x{self.current_height}+{sw-self.width-20}+20")
        if self.arrow_visible:
            self.arrow_canvas.place(x=0, y=self.current_height-16, width=self.width, height=16)
        if self.shortcuts_visible:
            self.shortcuts_canvas.place(x=0, y=self.current_height-self.shortcuts_height_val, width=self.width, height=self.shortcuts_height_val)

    def run_tray_thread(self):
        self.qt_app = QApplication(sys.argv)
        self.qt_app.setQuitOnLastWindowClosed(False)
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rectangle((8, 12, 56, 42), outline="white", width=4)
        d.rectangle((28, 42, 36, 50), fill="white")
        d.rectangle((20, 50, 44, 54), fill="white")
        path = os.path.join(os.environ.get('TEMP', ''), 'tray_monitor_icon.png')
        img.save(path)
        self.tray = QSystemTrayIcon(QIcon(path))
        self.tray_menu = QMenu()
        
        self.tray_menu.setStyleSheet("""
            QMenu { background-color: #2d2d2d; color: white; border: 1px solid #444; border-radius: 6px; padding: 5px; }
            QMenu::item { padding: 5px 20px; border-radius: 4px; }
            QMenu::item:selected { background-color: #0078d4; color: white; }
        """)
        
        self.tray_menu.addAction("Show/Hide", self.safe_toggle)
        self.tray_menu.addAction("Keys", self.safe_toggle_keys)
        self.tray_menu.addAction("Settings", self.safe_toggle_colors)
        self.tray_menu.addAction("About", self.safe_toggle_about)

        # Dynamic Island integration (single toggle button)
        self.tray_menu.addSeparator()
        self.island_action = self.tray_menu.addAction("Island: Off (click to start)", lambda: self.root.after(0, self.toggle_island))
        self.island_control_action = self.tray_menu.addAction("Island Control Panel", lambda: self.root.after(0, self.request_island_control_panel))
        self.island_control_action.setEnabled(False)

        self.tray_menu.addAction("Exit", self.exit_app)
        
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.handle_click)
        self.tray.show()
        self.update_island_action_label()
        self.qt_app.exec()

    def handle_click(self, r):
        if r == QSystemTrayIcon.Trigger:
              self.safe_toggle()
            
    def safe_toggle(self):
        self.root.after(0, self.toggle)
        
    def safe_toggle_keys(self):
        def action():
            if not self.visible:
                self.show()
            if not self.show_hotkey_editor:
                self.toggle_hotkey_editor()
        self.root.after(0, action)

    def safe_toggle_colors(self):
        def action():
            if not self.visible:
                self.show()
            if not self.show_extended_colors:
                self.toggle_colors()
        self.root.after(0, action)

    def safe_toggle_about(self):
        def action():
            if not self.visible:
                self.show()
            if not self.show_about:
                self.toggle_about()
        self.root.after(0, action)

    # --- DYNAMIC ISLAND CONTROL ---
    def start_island(self, force_trial=False, allow_locked_launch=False):
        if self.is_island_running():
            self.update_island_action_label()
            return

        trial_remaining = 0
        if force_trial:
            trial_remaining = 120
        elif not self.is_island_activated:
            trial_remaining = max(0, int(self.island_trial_until - time.time()))

        if (not self.is_island_activated) and trial_remaining <= 0 and (not allow_locked_launch):
            messagebox.showinfo("Dynamic Island", "Island is locked. Use ACTIVATION key or TRY 2 MIN from Settings.")
            self.update_island_action_label()
            return

        target = None
        if os.path.exists(self.island_path):
            target = self.island_path

        if not target:
            expected_name = "island.pyw" if test == 1 else "island.exe"
            messagebox.showwarning("Dynamic Island", f"Island file not found ({expected_name}).")
            return

        try:
            creation_flags = 0
            if sys.platform.startswith("win"):
                # DETACHED_PROCESS to avoid console window
                creation_flags = 0x00000008

            if target.lower().endswith(".pyw") or target.lower().endswith(".py"):
                args = [sys.executable, target, "--no-tray"]
                if trial_remaining > 0 and not self.is_island_activated:
                    args.extend(["--trial-2m", f"--trial-seconds={trial_remaining}"])
                self.island_proc = subprocess.Popen(
                    args,
                    creationflags=creation_flags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            else:
                args = [target]
                if trial_remaining > 0 and not self.is_island_activated:
                    args.extend(["--trial-2m", f"--trial-seconds={trial_remaining}", "--no-tray"])
                self.island_proc = subprocess.Popen(
                    args,
                    creationflags=creation_flags,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
        except Exception as e:
            messagebox.showerror("Dynamic Island", f"Failed to start Island: {e}")
        finally:
            self.update_island_action_label()

    def stop_island(self):
        if self.island_proc and (self.island_proc.poll() is None):
            try:
                self.island_proc.terminate()
                try:
                    self.island_proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.island_proc.kill()
            except Exception:
                pass
        self.island_proc = None
        # Also kill any separately launched Island processes so exiting SmartMonitor cleans them up
        self.kill_orphan_island()
        self.update_island_action_label()

    def kill_orphan_island(self):
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    if proc.pid == os.getpid():
                        continue
                    name = (proc.info.get('name') or '').lower()
                    cmd = " ".join(proc.info.get('cmdline') or []).lower()
                    if name in ("island.exe", "island") or "island.pyw" in cmd or "island.py" in cmd:
                        try:
                            proc.terminate()
                            proc.wait(timeout=2)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            pass

    def restart_island(self):
        self.stop_island()
        self.start_island()

    def is_island_running(self):
        if self.island_proc is not None and (self.island_proc.poll() is None):
            return True
        # Detect externally launched Island (exe or py/pyw)
        try:
            for proc in psutil.process_iter(['name', 'cmdline']):
                try:
                    name = (proc.info.get('name') or '').lower()
                    if name in ("island.exe", "island", "dynamic island.exe", "dynamicisland.exe"):
                        return True
                    cmd = " ".join(proc.info.get('cmdline') or []).lower()
                    if "island.pyw" in cmd or "island.py" in cmd:
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception:
            pass
        return False

    def toggle_island(self):
        if self.is_island_running():
            self.stop_island()
        else:
            # Tray toggle should always attempt to start Island directly.
            # If not activated, Island will show its own activation window.
            self.start_island(allow_locked_launch=True)

    def request_island_control_panel(self):
        if not self.is_island_running():
            messagebox.showinfo("Dynamic Island", "Island is not running.")
            return
        try:
            app_data = os.getenv('LOCALAPPDATA') or os.path.expanduser('~')
            flag_dir = os.path.join(app_data, "Smart Monitor")
            os.makedirs(flag_dir, exist_ok=True)
            flag_path = os.path.join(flag_dir, "open_island_panel.flag")
            with open(flag_path, 'w') as f:
                f.write('open')
        except Exception as e:
            messagebox.showerror("Dynamic Island", f"Failed to request control panel: {e}")

    def update_island_action_label(self):
        if self.island_action:
            lbl = "Island: On (click to stop)" if self.is_island_running() else "Island: Off (click to start)"
            self.island_action.setText(lbl)
        if self.island_control_action:
            running = self.is_island_running()
            self.island_control_action.setEnabled(running)
            self.island_control_action.setVisible(running)

    def exit_app(self):
        try:
            if self.arrow_timer: self.root.after_cancel(self.arrow_timer)
            if self.arrow_cycle_timer: self.root.after_cancel(self.arrow_cycle_timer)
            if self.shortcuts_timer: self.root.after_cancel(self.shortcuts_timer)
            if self.shortcuts_cycle_timer: self.root.after_cancel(self.shortcuts_cycle_timer)
        except:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except:
            pass
        # Ensure Island subprocess is stopped on exit
        try:
            self.stop_island()
        except:
            pass
        try:
            self.tray.hide()
            self.tray.deleteLater()
        except:
            pass
        try:
            if hasattr(self, 'qt_app'):
                self.qt_app.quit()
        except:
            pass
        sys.exit(0)

# ================= MANUAL & LAUNCHER LOGIC =================
MANUAL_APPROVED = False

def launch_main_app_internal(root_win):
    global MANUAL_APPROVED
    MANUAL_APPROVED = True
    root_win.destroy()

def show_manual():
    root = tk.Tk()
    root.title("JAKS 360 - User Manual")
    root.geometry("700x650")
    root.configure(bg="#fcfcfc")

    def get_correct_path(filename):
        if getattr(sys, 'frozen', False):
            base_path = os.path.dirname(sys.executable)
        else:
            base_path = os.path.abspath(".")
        return os.path.join(base_path, filename)

    icon_path = get_correct_path("icon.ico")

    try:
        if os.path.exists(icon_path):
            root.iconbitmap(icon_path)
        else:
            tp = tk.PhotoImage(width=1, height=1, data='R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
            root.iconphoto(False, tp)
    except:
        pass

    header = tk.Label(root, text="SMART MONITOR", font=("Segoe UI Variable Display", 22, "bold"), bg="#fcfcfc", fg="#0078d4")
    header.pack(pady=(25, 5))
    tk.Label(root, text="Version v1.5", font=("Segoe UI", 10), bg="#fcfcfc", fg="#666666").pack()

    text_area = scrolledtext.ScrolledText(root, wrap=tk.WORD, width=70, height=20, font=("Segoe UI", 10),
                                          padx=20, pady=20, borderwidth=0, highlightthickness=1,
                                          highlightbackground="#e0e0e0", bg="#ffffff")

    manual_content = """WELCOME TO SMART MONITOR
===================================

Developed by: Jaideep Krishna A
Company: JAKS 360
Version: v1.5

Smart Monitor is a professional-grade system telemetry application designed for both desktop and laptop environments.

It delivers real-time, precision tracking of critical system metrics including Battery Health, CPU/GPU Utilization, RAM Allocation, and Network Throughput.

QUICK START GUIDE & HOTKEYS:
-----------------------------------------

• Use [ ctrl + K ] to edit your shortcuts key or select keys option in Tray incon menu at taskbar

Default keys:

• [ Ctrl + B ]       : Toggle Monitor Visibility
• [ Ctrl + M ]       : Toggle Hardware Stats (CPU, GPU, RAM, Disk)
• [ Ctrl + I ]       : Toggle Network Speed Metrics
• [ Ctrl + G ]       : Toggle CPU/GPU Performance Graphs
• [ Ctrl + K ]       : Edit Custom Shortcuts
• [ Ctrl + H ]       : View Advanced Hardware Properties
• [ Ctrl + Alt + S ] : Open Color Configuration
• [ Ctrl + Alt + R ] : Toggle RGB Border Animation
• [ Ctrl + Alt + T ] : Cycle Theme (Dark <-> Light <-> Transparent)
• [ Ctrl + Alt + A ] : Show About & Help Information
• [ Ctrl + Alt + B ] : Terminate Application
• [ Ctrl + Shift + B ] : One-Click Boost (Clear RAM & Temp)
• [ Ctrl + Shift + T ] : Mini Task Killer (Top RAM Eaters)
• [ Ctrl + Shift + O ] : Startup App Optimizer

ADVANCED FEATURES:
-----------------------------------------
• Dynamic RGB Border: An animated, color-cycling border designed for high-end gaming setups. Toggle via Ctrl+Alt+R.

• Transparent Overlay Mode: A specialized borderless mode ideal for seamless integration during game or application testing.

• Adaptive Theming: Intelligent light and dark modes with auto-adjusting text contrast for optimal readability in any environment.

• Battery Intelligence: Predictive charging analytics and discharge estimates ensuring maximum laptop battery longevity.

JAKS 360 System Monitor seamlessly bridges the gap between raw hardware data and an elegant, unobtrusive user interface.

Copyright © 2026 JAKS 360. All Rights Reserved.

Unauthorized copying, modification, or distribution of this software is strictly prohibited.

SUPPORT: jaideepkrishna2008@gmail.com

CONTACT: jaideepk2008@gmail.com
"""
    text_area.insert(tk.INSERT, manual_content)
    text_area.configure(state='disabled')
    text_area.pack(padx=40, pady=15)

    tk.Label(root, text="Created by Jaideep Krishna | JAKS 360 company",
             font=("Segoe UI Semibold", 9), bg="#fcfcfc", fg="#333333").pack()

    btn = tk.Button(root, text="I UNDERSTAND & START APP", command=lambda: launch_main_app_internal(root),
                    bg="#0078d4", fg="white", font=("Segoe UI", 11, "bold"),
                    padx=40, pady=12, relief="flat", cursor="hand2")
    btn.pack(pady=20)

    root.update()
    win_width = root.winfo_reqwidth()
    win_height = root.winfo_reqheight()
    if win_height < 650:
        win_height = 650
    x = (root.winfo_screenwidth() // 2) - (win_width // 2)
    y = (root.winfo_screenheight() // 2) - (win_height // 2)
    root.geometry(f"{win_width}x{win_height}+{x}+{y}")
    root.mainloop()

# ================= EXECUTION ORCHESTRATION =================
if __name__ == "__main__":
    app_data = os.getenv('LOCALAPPDATA')
    if not app_data:
        app_data = os.path.expanduser("~")

    save_folder = os.path.join(app_data, "Smart Monitor")
    flag_file = os.path.join(save_folder, "manual_seen_v1_5.tag")

    start_monitor = False
    if not os.path.exists(flag_file):
        if not os.path.exists(save_folder):
            try:
                os.makedirs(save_folder)
            except:
                pass
        try:
            with open(flag_file, "w") as f:
                f.write("seen")
        except:
            pass
        show_manual()
        if MANUAL_APPROVED:
            start_monitor = True
    else:
        start_monitor = True

    if start_monitor:
        StableStartMonitor()
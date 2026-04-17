import os
import sys
import ctypes
import time
import json
import threading
import webbrowser
from collections import deque
import winreg
import socket
import subprocess

try:
    import win32gui
    import win32process
except Exception:
    win32gui = None
    win32process = None

try:
    import win32pdh
except Exception:
    win32pdh = None
import psutil
import keyboard  # global hotkey recorder (SmartMonitor style)
from PIL import Image, ImageDraw

os.environ['QT_LOGGING_RULES'] = 'qt.qpa.window=false'
from PySide6.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QWidget, QHBoxLayout,
    QVBoxLayout, QLabel, QPushButton, QSlider, QCheckBox,
    QComboBox, QGroupBox, QFormLayout, QLineEdit, QColorDialog, QInputDialog, QMessageBox
)
from PySide6.QtGui import QIcon, QPainter, QColor, QFont, QPen
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QRect, QThread, Signal, QEvent

# =============== HIGH DPI FIX (crisp on all displays) =================
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# =============== MEDIA KEYS ==========================================
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
VK_MEDIA_NEXT_TRACK = 0xB0
VK_SHIFT = 0x10
VK_K = 0x4B
VK_N = 0x4E
VK_P = 0x50

# TEST switch:
# 0 = auto detect laptop/desktop using battery presence
# 1 = force desktop mode (show CPU% instead of battery)
TEST = 0

TRIAL_SECONDS_DEFAULT = 120
ACTIVATION_KEY = "jaks360@"
ACTIVATION_REG_PATH = r"Software\JAKS360\SmartMonitor"
ACTIVATION_REG_VALUE = "IslandKey"


def _app_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.abspath(".")


def _resolve_app_icon_path():
    base_dir = _app_base_dir()
    file_dir = os.path.dirname(os.path.abspath(__file__))
    cwd = os.getcwd()
    candidates = [
        os.path.join(base_dir, "icon.ico"),
        os.path.join(base_dir, "desktop.ico"),
        os.path.join(file_dir, "icon.ico"),
        os.path.join(file_dir, "desktop.ico"),
        os.path.join(cwd, "icon.ico"),
        os.path.join(cwd, "desktop.ico"),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


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


def _trial_state_path():
    app_data = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(app_data, "Smart Monitor", "island_trial_state.json")


def _read_trial_state():
    try:
        path = _trial_state_path()
        if not os.path.exists(path):
            return {"trial_until": 0.0, "trial_used": False}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "trial_until": float(data.get("trial_until", 0.0) or 0.0),
            "trial_used": bool(data.get("trial_used", False)),
        }
    except Exception:
        return {"trial_until": 0.0, "trial_used": False}


def _write_trial_state(state):
    try:
        path = _trial_state_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "trial_until": float(state.get("trial_until", 0.0) or 0.0),
                "trial_used": bool(state.get("trial_used", False)),
            }, f)
        return True
    except Exception:
        return False


def _set_trial_expiry(epoch_ts):
    state = _read_trial_state()
    state["trial_until"] = float(epoch_ts)
    return _write_trial_state(state)


def _mark_trial_consumed():
    state = _read_trial_state()
    state["trial_until"] = 0.0
    state["trial_used"] = True
    return _write_trial_state(state)


def _is_trial_consumed():
    return bool(_read_trial_state().get("trial_used", False))


def _clear_trial_runtime_only():
    state = _read_trial_state()
    state["trial_until"] = 0.0
    return _write_trial_state(state)


def _set_trial_start_if_allowed(seconds):
    if _is_trial_consumed():
        return 0
    expiry = time.time() + max(1, int(seconds))
    _set_trial_expiry(expiry)
    return max(1, int(expiry - time.time()))


def _get_trial_expiry():
    try:
        return float(_read_trial_state().get("trial_until", 0.0) or 0.0)
    except Exception:
        return 0.0


def _get_trial_remaining_seconds():
    if _is_trial_consumed():
        return 0
    remaining = int(_get_trial_expiry() - time.time())
    return max(0, remaining)

def press_media_key(key_code):
    ctypes.windll.user32.keybd_event(key_code, 0, 0, 0)
    ctypes.windll.user32.keybd_event(key_code, 0, 2, 0)

def press_combo(mod_vk, key_vk):
    ctypes.windll.user32.keybd_event(mod_vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(key_vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(key_vk, 0, 2, 0)
    ctypes.windll.user32.keybd_event(mod_vk, 0, 2, 0)

# =============== MEDIA TRACKER THREAD ===============================
class MediaTrackerThread(QThread):
    media_updated = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.last_title = "Ready to Play"
        self.last_active_source = ""

    def classify_title(self, title):
        tl = title.lower()
        if "spotify" in tl: return "spotify"
        if "youtube music" in tl: return "ytm"
        if "youtube" in tl: return "yt"
        if "vlc" in tl: return "vlc"
        return "other"

    def run(self):
        while not self.isInterruptionRequested():
            has_media, title = self.get_now_playing()
            self.media_updated.emit(has_media, title)
            for _ in range(10):
                if self.isInterruptionRequested():
                    break
                self.msleep(100)

    def get_now_playing(self):
        try:
            spotify_pids = set()
            for proc in psutil.process_iter(['pid', 'name']):
                try:
                    if (proc.info['name'] or '').lower() == 'spotify.exe':
                        spotify_pids.add(proc.info['pid'])
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass

            if win32gui is None or win32process is None:
                return False, ""

            windows = []
            def enum_windows_proc(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
                        text = buf.value
                        if text:
                            _, pid = win32process.GetWindowThreadProcessId(hwnd)
                            windows.append((text, pid))
                return True
            win32gui.EnumWindows(enum_windows_proc, 0)

            active_title = ""
            paused_media_found = False
            paused_candidates = []

            for text, pid in windows:
                if any(x in text for x in [" - YouTube", "YouTube Music", " • ", "VLC media player", "Spotify"]):
                    clean_text = (
                        text.replace(" - Google Chrome", "")
                            .replace(" - Mozilla Firefox", "")
                            .replace(" - Microsoft Edge", "")
                            .replace(" - Brave", "")
                            .replace(" - YouTube Music", "")
                            .replace(" - YouTube", "")
                            .replace(" - Spotify", "")
                            .replace(" - VLC media player", "")
                            .strip()
                    )
                    if clean_text and clean_text != "Spotify":
                        active_title = clean_text
                        self.last_active_source = self.classify_title(active_title)
                        break
                    elif clean_text:
                        paused_candidates.append(clean_text)

                if pid in spotify_pids:
                    invalid = {
                        "Spotify Premium", "Spotify Free", "Spotify",
                        "Default IME", "MSCTFIME UI", "GDI+ Window (Spotify.exe)"
                    }
                    if text not in invalid:
                        active_title = text
                        self.last_active_source = self.classify_title(active_title)
                        break
                    elif text:
                        paused_candidates.append(text)

                if any(x in text for x in ["Spotify", "YouTube", "YouTube Music", "VLC media player"]):
                    paused_media_found = True
                    if text:
                        paused_candidates.append(text)

            if active_title:
                self.last_title = active_title
                return True, active_title

            if paused_candidates:
                for c in paused_candidates:
                    if self.classify_title(c) == self.last_active_source:
                        self.last_title = c
                        return True, self.last_title
                def rank(t):
                    src = self.classify_title(t)
                    return {"spotify": 0, "ytm": 1, "yt": 2}.get(src, 3)
                paused_candidates.sort(key=rank)
                self.last_title = paused_candidates[0]
                return True, self.last_title

            if paused_media_found:
                return True, self.last_title
        except Exception:
            pass
        return False, ""

class PingTrackerThread(QThread):
    ping_updated = Signal(float)

    def run(self):
        while not self.isInterruptionRequested():
            started = time.perf_counter()
            ping_ms = -1.0
            try:
                sock = socket.create_connection(("1.1.1.1", 443), timeout=0.8)
                sock.close()
                ping_ms = (time.perf_counter() - started) * 1000.0
            except Exception:
                ping_ms = -1.0

            self.ping_updated.emit(ping_ms)
            for _ in range(20):
                if self.isInterruptionRequested():
                    break
                self.msleep(100)

class GpuTrackerThread(QThread):
    gpu_updated = Signal(float)
    
    def __init__(self, island):
        super().__init__()
        self.island = island
        self._pdh_query = None
        self._gpu_h = None
        self._last_gpu_fallback_ts = 0.0
        self._last_gpu_fallback_val = 0.0
        self._init_pdh_gpu_counter()

    def _init_pdh_gpu_counter(self):
        if win32pdh is None:
            return
        try:
            self._pdh_query = win32pdh.OpenQuery()
            for p in [r"\GPU Engine(*)\Utilization Percentage", r"\GPU Engine(*)\UtilizationPercentage"]:
                try:
                    self._gpu_h = win32pdh.AddCounter(self._pdh_query, p)
                    break
                except Exception:
                    continue
        except Exception:
            self._pdh_query = None
            self._gpu_h = None

    def _query_gpu_util_smartmonitor(self):
        if win32pdh is None or self._pdh_query is None or self._gpu_h is None:
            return 0.0
        try:
            win32pdh.CollectQueryData(self._pdh_query)
            data = win32pdh.GetFormattedCounterArray(self._gpu_h, win32pdh.PDH_FMT_DOUBLE)
            engine_totals = {}

            items = []
            if hasattr(data, "items"):
                items = data.items()
            elif isinstance(data, (list, tuple)):
                if len(data) == 2 and isinstance(data[1], (list, tuple)):
                    items = data[1]
                else:
                    items = data

            for entry in items:
                try:
                    if isinstance(entry, tuple) and len(entry) >= 2:
                        key, val = entry[0], entry[1]
                    else:
                        continue
                    key_s = str(key).lower()
                    if 'engtype_' in key_s:
                        etype = key_s.split('engtype_')[-1]
                        engine_totals[etype] = engine_totals.get(etype, 0.0) + float(val)
                except Exception:
                    continue

            if engine_totals:
                return float(max(engine_totals.values()))
        except Exception:
            return 0.0
        return 0.0

    def _query_gpu_util_nvidia(self):
        try:
            out = subprocess.check_output(
                ['nvidia-smi', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits'],
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=0.6
            )
            return float(out.decode().split('\n')[0].strip())
        except Exception:
            return None
        
    def run(self):
        while not self.isInterruptionRequested():
            if self.island.pc_metric_type == 3:
                try:
                    val = self._query_gpu_util_smartmonitor()
                    if val <= 0.0 and (time.time() - self._last_gpu_fallback_ts) > 4.0:
                        nv = self._query_gpu_util_nvidia()
                        if nv is not None:
                            self._last_gpu_fallback_val = float(nv)
                        self._last_gpu_fallback_ts = time.time()
                    if val <= 0.0:
                        val = self._last_gpu_fallback_val
                    val = max(0.0, min(100.0, float(val)))
                    self.island.last_gpu_util = val
                    self.gpu_updated.emit(val)
                except Exception:
                    self.gpu_updated.emit(max(0.0, float(getattr(self.island, 'last_gpu_util', 0.0))))
            for _ in range(10):
                if self.isInterruptionRequested():
                    break
                self.msleep(100)

# =============== WIDGETS ============================================
class BatteryWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedSize(36, 16)
        self.percent = 100
        self.is_charging = False
        self.style_idx = 0
        self.setStyleSheet("background-color: transparent;")
        self.setAttribute(Qt.WA_TranslucentBackground)

    def update_battery(self, percent, is_charging):
        self.percent = percent
        self.is_charging = is_charging
        self.update()

    def set_style(self, index):
        self.style_idx = index
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        fill_color = (
            QColor(76, 217, 100) if self.is_charging
            else QColor(255, 59, 48) if self.percent <= 20
            else Qt.white
        )
        if self.style_idx == 0:
            painter.setPen(QPen(Qt.white, 2))
            painter.drawRoundedRect(1, 1, 30, 14, 3, 3)
            painter.setBrush(Qt.white)
            painter.drawRoundedRect(32, 4, 3, 8, 1, 1)
            fill_width = int((self.percent / 100.0) * 26)
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill_color)
            painter.drawRoundedRect(3, 3, fill_width, 10, 1, 1)
        elif self.style_idx == 1:
            painter.setPen(QPen(Qt.white, 1))
            painter.drawRect(0, 1, 32, 14)
            painter.fillRect(33, 5, 2, 6, Qt.white)
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill_color)
            blocks = int(self.percent / 25) + (1 if self.percent % 25 > 0 else 0)
            for i in range(blocks):
                painter.drawRect(2 + (i * 7), 3, 6, 10)
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(50, 50, 50))
            painter.drawRoundedRect(0, 6, 34, 4, 2, 2)
            fill_width = int((self.percent / 100.0) * 34)
            painter.setBrush(fill_color)
            painter.drawRoundedRect(0, 6, fill_width, 4, 2, 2)

# =============== CONTROL PANEL ======================================
class ControlPanelWindow(QWidget):
    def __init__(self, island):
        super().__init__()
        self.island = island
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.initUI()
        self.apply_theme(0)
        self.sync_from_island()

    def initUI(self):
        self.setWindowTitle("Island Control Panel")
        self.setMinimumSize(440, 620)
        self.resize(500, 680)
        icon_path = _resolve_app_icon_path()
        if icon_path and os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        theme_group = QGroupBox("App Theme")
        theme_layout = QFormLayout()
        theme_layout.setVerticalSpacing(6)
        theme_layout.setHorizontalSpacing(10)
        self.combo_theme = QComboBox()
        self.combo_theme.addItems(["Dark Mode", "Light Mode"])
        self.combo_theme.setMinimumHeight(32)
        self.combo_theme.setMaxVisibleItems(8)
        self.combo_theme.currentIndexChanged.connect(self.apply_theme)
        theme_layout.addRow("Control Panel Theme:", self.combo_theme)
        theme_group.setLayout(theme_layout)

        vis_group = QGroupBox("Island Elements")
        vis_layout = QFormLayout()
        vis_layout.setVerticalSpacing(12)
        vis_layout.setHorizontalSpacing(12)
        vis_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        vis_layout.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        self.combo_battery = QComboBox()
        self.combo_battery.addItems(["Classic Wrapper", "Segmented Blocks", "Minimalist Line"])
        self.combo_battery.setMinimumHeight(36)
        self.combo_battery.setMaxVisibleItems(8)
        self.combo_battery.currentIndexChanged.connect(self.on_battery_style_changed)

        self.combo_media = QComboBox()
        self.combo_media.addItems([
            "Classic Emojis (⏮ ▶/⏸ ⏭)",
            "Solid Glyphs (⏴ ⏵/| | ⏵)",
            "Minimal Outlines (◁ ▷/| | ▷)"
        ])
        self.combo_media.setMinimumHeight(36)
        self.combo_media.setMaxVisibleItems(8)
        self.combo_media.currentIndexChanged.connect(self.on_media_style_changed)

        self.combo_net_mode = QComboBox()
        self.combo_net_mode.addItems([
            "Bits/s (Kbps/Mbps/Gbps)",
            "Bytes/s (KB/s/MB/s/GB/s)",
            "Ping (ms)"
        ])
        self.combo_net_mode.setMinimumHeight(36)
        self.combo_net_mode.setMaxVisibleItems(8)
        self.combo_net_mode.currentIndexChanged.connect(self.on_net_mode_changed)

        self.btn_net_color = QPushButton()
        self.btn_net_color.setFixedSize(30, 30)
        self.btn_net_color.clicked.connect(self.on_net_color_clicked)
        self.btn_net_color.setToolTip("Pick Internet Color")
        self.update_color_button_style(self.btn_net_color, self.island.net_color)

        self.net_color_wrap = QWidget()
        self.net_color_wrap.setFixedHeight(36)
        net_color_layout = QHBoxLayout()
        net_color_layout.setContentsMargins(4, 3, 4, 3)
        net_color_layout.setSpacing(6)
        net_color_layout.addWidget(self.btn_net_color, alignment=Qt.AlignVCenter)
        net_color_layout.addStretch()
        self.net_color_wrap.setLayout(net_color_layout)

        self.combo_pc_monitor = QComboBox()
        self.combo_pc_monitor.addItems(["CPU (%)", "RAM (%)", "Disk Space (%)", "GPU (%)"])
        self.combo_pc_monitor.setMinimumHeight(36)
        self.combo_pc_monitor.setMaxVisibleItems(8)
        self.combo_pc_monitor.currentIndexChanged.connect(self.on_pc_monitor_changed)

        self.btn_pc_color = QPushButton()
        self.btn_pc_color.setFixedSize(30, 30)
        self.btn_pc_color.clicked.connect(self.on_pc_color_clicked)
        self.btn_pc_color.setToolTip("Pick PC Metric Color")
        self.update_pc_color_btn()

        self.pc_color_wrap = QWidget()
        self.pc_color_wrap.setFixedHeight(36)
        pc_color_layout = QHBoxLayout()
        pc_color_layout.setContentsMargins(4, 3, 4, 3)
        pc_color_layout.setSpacing(6)
        pc_color_layout.addWidget(self.btn_pc_color, alignment=Qt.AlignVCenter)
        pc_color_layout.addStretch()
        self.pc_color_wrap.setLayout(pc_color_layout)

        # --- SmartMonitor-like hotkey UI ---
        self.hotkey_display = QLineEdit()
        self.hotkey_display.setReadOnly(True)
        self.hotkey_display.setAlignment(Qt.AlignCenter)
        self.hotkey_display.setFixedHeight(32)
        self.hotkey_display.setStyleSheet("""
            QLineEdit {
                background-color: #111;
                border: 1px solid #444;
                border-radius: 4px;
                color: #ddd;
                font: 13px 'Segoe UI';
                padding: 4px;
            }
        """)

        self.btn_record_hotkey = QPushButton("Record Toggle Hotkey")
        self.btn_record_hotkey.setMinimumHeight(36)
        self.btn_record_hotkey.clicked.connect(self.start_hotkey_recording)

        self.btn_toggle_now = QPushButton("Show / Hide Island")
        self.btn_toggle_now.setMinimumHeight(36)
        self.btn_toggle_now.clicked.connect(self.island.toggle_visibility)

        self.lbl_hotkey_status = QLabel("Saved: --")
        self.lbl_hotkey_status.setStyleSheet("color: #cccccc; margin-top: 3px;")

        vis_layout.addRow("Battery Style:", self.combo_battery)
        vis_layout.addRow("Media Buttons:", self.combo_media)
        vis_layout.addRow("Internet Monitor:", self.combo_net_mode)
        vis_layout.addRow("Internet Color:", self.net_color_wrap)
        vis_layout.addRow("PC Mode Sensor:", self.combo_pc_monitor)
        vis_layout.addRow("PC Mode Color:", self.pc_color_wrap)
        vis_layout.addRow("Toggle Hotkey:", self.hotkey_display)
        vis_layout.addRow("", self.btn_record_hotkey)
        vis_layout.addRow("", self.btn_toggle_now)
        vis_layout.addRow("", self.lbl_hotkey_status)
        vis_group.setLayout(vis_layout)

        rgb_group = QGroupBox("RGB Edge Lighting")
        rgb_layout = QVBoxLayout()
        rgb_layout.setContentsMargins(20, 12, 20, 12)
        self.chk_rgb = QCheckBox("Enable RGB Border")
        self.chk_rgb.stateChanged.connect(self.toggle_rgb)
        self.chk_rgb.setFocusPolicy(Qt.NoFocus)
        speed_label = QLabel("Animation Speed:")
        speed_label.setAlignment(Qt.AlignCenter)
        self.slider_speed = QSlider(Qt.Horizontal)
        self.slider_speed.setRange(1, 100)
        self.slider_speed.setValue(20)
        self.slider_speed.valueChanged.connect(self.update_rgb_speed)
        rgb_layout.addWidget(self.chk_rgb, alignment=Qt.AlignCenter)
        rgb_layout.addSpacing(10)
        rgb_layout.addWidget(speed_label, alignment=Qt.AlignCenter)
        rgb_layout.addWidget(self.slider_speed)
        rgb_group.setLayout(rgb_layout)

        main_layout.addWidget(theme_group)
        main_layout.addWidget(vis_group)
        main_layout.addWidget(rgb_group)
        main_layout.addStretch()
        self.setLayout(main_layout)

    def ensure_on_screen(self):
        screen = self.screen() or QApplication.primaryScreen()
        if not screen:
            return

        available = screen.availableGeometry()
        target_w = min(self.width(), max(360, available.width() - 20))
        target_h = min(self.height(), max(420, available.height() - 20))
        self.resize(target_w, target_h)

        # Center the window on screen
        x = available.left() + (available.width() - self.width()) // 2
        y = available.top() + (available.height() - self.height()) // 2
        
        # Ensure position is within screen bounds
        x = min(max(x, available.left()), max(available.left(), available.right() - self.width() + 1))
        y = min(max(y, available.top()), max(available.top(), available.bottom() - self.height() + 1))
        self.move(x, y)

    # ----- Hotkey recording like SmartMonitor -----
    def start_hotkey_recording(self):
        if getattr(self, "_recording", False):
            return
        self._recording = True
        self.btn_record_hotkey.setEnabled(False)
        self.btn_record_hotkey.setText("Recording...")
        self.hotkey_display.setText("Recording…")
        self.lbl_hotkey_status.setText("Press keys (Esc to cancel)")
        threading.Thread(target=self._record_hotkey_thread, daemon=True).start()

    def _record_hotkey_thread(self):
        self.island.unregister_toggle_shortcut()
        try:
            event = keyboard.read_hotkey(suppress=False)  # waits for combo
        except Exception:
            event = None

        def finish():
            self._recording = False
            self.island.register_toggle_shortcut()  # restore handler
            self.btn_record_hotkey.setEnabled(True)
            self.btn_record_hotkey.setText("Record Toggle Hotkey")

        if event is None:
            self.lbl_hotkey_status.setText("Recording failed")
            self.hotkey_display.setText(self.island.toggle_hotkey)
            finish()
            return

        if str(event).lower() == "esc":
            self.lbl_hotkey_status.setText(f"Saved: {self.island.toggle_hotkey}")
            self.hotkey_display.setText(self.island.toggle_hotkey)
            finish()
            return

        key_str = str(event)
        self.island.set_toggle_hotkey(key_str)
        self.lbl_hotkey_status.setText(f"Saved: {key_str}")
        self.hotkey_display.setText(key_str)
        finish()
    # ------------------------------------------------------------------

    def update_color_button_style(self, btn, hex_color):
        btn.setStyleSheet(
            f"QPushButton {{ background-color: {hex_color}; border: 1px solid #7a7a7a; border-radius: 4px; padding: 0px; }}"
            f"QPushButton:hover {{ border: 1px solid #9a9a9a; }}"
        )
        btn.setText("")
        btn.setToolTip(hex_color.upper())

    def update_pc_color_btn(self):
        idx = self.island.pc_metric_type
        if idx == 0:
            c = getattr(self.island, 'cpu_color', "#ffffff")
        elif idx == 1:
            c = getattr(self.island, 'ram_color', "#ff69b4")
        elif idx == 2:
            c = getattr(self.island, 'disk_color', "#b088ff")
        else:
            c = getattr(self.island, 'gpu_color', "#4cd964")
        self.update_color_button_style(self.btn_pc_color, c)

    def on_net_color_clicked(self):
        initial = QColor(self.island.net_color)
        color = QColorDialog.getColor(initial, self, "Select Internet Color")
        if color.isValid():
            self.island.net_color = color.name()
            self.update_color_button_style(self.btn_net_color, self.island.net_color)
            self.island.save_island_config()
            self.island.update_stats()

    def on_pc_color_clicked(self):
        idx = self.island.pc_metric_type
        if idx == 0:
            initial = QColor(getattr(self.island, 'cpu_color', "#ffffff"))
        elif idx == 1:
            initial = QColor(getattr(self.island, 'ram_color', "#ff69b4"))
        elif idx == 2:
            initial = QColor(getattr(self.island, 'disk_color', "#b088ff"))
        else:
            initial = QColor(getattr(self.island, 'gpu_color', "#4cd964"))

        color = QColorDialog.getColor(initial, self, "Select PC Metric Color")
        if color.isValid():
            if idx == 0:
                self.island.cpu_color = color.name()
            elif idx == 1:
                self.island.ram_color = color.name()
            elif idx == 2:
                self.island.disk_color = color.name()
            else:
                self.island.gpu_color = color.name()
            self.update_pc_color_btn()
            self.island.save_island_config()
            self.island.update_stats()

    def sync_from_island(self):
        self.chk_rgb.setChecked(self.island.rgb_enabled)
        self.slider_speed.setValue(int(self.island.rgb_speed / 0.0005))
        self.combo_battery.setCurrentIndex(self.island.battery_style)
        self.combo_media.setCurrentIndex(self.island.media_style)
        self.combo_net_mode.setCurrentIndex(self.island.net_display_mode)
        self.update_color_button_style(self.btn_net_color, getattr(self.island, 'net_color', "#00FFCC"))
        self.combo_pc_monitor.setCurrentIndex(self.island.pc_metric_type)
        self.update_pc_color_btn()
        self.combo_theme.setCurrentIndex(self.island.panel_theme)
        self.hotkey_display.setText(self.island.toggle_hotkey)
        self.lbl_hotkey_status.setText(f"Saved: {self.island.toggle_hotkey}")

    def apply_theme(self, index):
        self.island.panel_theme = index
        self.island.save_island_config()

        if index == 0:  # Dark
            self.setStyleSheet("""
                QWidget { background-color: #1E1E1E; color: white; font-family: 'Segoe UI'; font-size: 14px; }
                QGroupBox { border: 1px solid #444; border-radius: 6px; margin-top: 15px; font-weight: bold; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
                QComboBox, QCheckBox { background-color: #2D2D2D; border: 1px solid #555; border-radius: 6px; padding: 5px 10px; min-height: 24px; }
                QComboBox::drop-down { border: none; width: 26px; }
                QComboBox QAbstractItemView { background: #1f1f1f; border: 1px solid #555; border-radius: 6px; padding: 4px; selection-background-color: #0078d4; selection-color: #ffffff; outline: 0; }
                QComboBox QAbstractItemView::item { min-height: 28px; padding: 5px 10px; border-radius: 4px; }
                QComboBox QAbstractItemView::item:hover { background: #0078d4; color: #ffffff; }
                QComboBox QAbstractItemView::item:selected { background: #0078d4; color: #ffffff; }
                QPushButton { background-color: #0f0f0f; color: #ffffff; border: 1px solid #2e2e2e; border-radius: 4px; padding: 8px 12px; min-height: 24px; font: 12px 'Segoe UI'; }
                QPushButton:hover { background-color: #1c1c1c; }
                QPushButton:pressed { background-color: #0b0b0b; }
                QPushButton:disabled { color: #888888; background-color: #151515; }
                QSlider { background-color: #2D2D2D; border: 1px solid #555; border-radius: 4px; padding: 6px 10px; }
                QSlider::groove:horizontal { height: 6px; background: #444; border-radius: 3px; margin: 4px 0px; }
                QSlider::handle:horizontal { background: #0078d4; border: 1px solid #0078d4; width: 14px; height: 14px; margin: -4px 0px; border-radius: 7px; }
                QSlider::sub-page:horizontal { background: #0078d4; border-radius: 3px; }
            """)
            self.hotkey_display.setStyleSheet("""
                QLineEdit {
                    background-color: #111;
                    border: 1px solid #444;
                    border-radius: 4px;
                    color: #ddd;
                    font: 13px 'Segoe UI';
                    padding: 4px;
                }
            """)
            self.lbl_hotkey_status.setStyleSheet("color: #cfcfcf; margin-top: 3px;")
        else:  # Light
            self.setStyleSheet("""
                QWidget { background-color: #f4f6f8; color: #1f2937; font-family: 'Segoe UI'; font-size: 14px; }
                QGroupBox { border: 1px solid #cfd6de; border-radius: 8px; margin-top: 15px; font-weight: bold; }
                QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
                QComboBox, QCheckBox { background-color: #ffffff; border: 1px solid #c6ced8; border-radius: 6px; padding: 5px 10px; min-height: 24px; }
                QComboBox::drop-down { border: none; width: 26px; }
                QComboBox QAbstractItemView { background: #ffffff; border: 1px solid #c6ced8; border-radius: 6px; padding: 4px; selection-background-color: #0078d4; selection-color: #ffffff; outline: 0; }
                QComboBox QAbstractItemView::item { min-height: 28px; padding: 5px 10px; border-radius: 4px; }
                QComboBox QAbstractItemView::item:hover { background: #0078d4; color: #ffffff; }
                QComboBox QAbstractItemView::item:selected { background: #0078d4; color: #ffffff; }
                QPushButton { background-color: #ffffff; color: #1f2937; border: 1px solid #c6ced8; border-radius: 6px; padding: 8px 12px; min-height: 24px; font: 12px 'Segoe UI Semibold'; }
                QPushButton:hover { background-color: #eef3f8; border: 1px solid #b7c1cd; }
                QPushButton:pressed { background-color: #e6edf5; }
                QPushButton:disabled { color: #8b97a6; background-color: #f3f6f9; }
                QSlider { background-color: #ffffff; border: 1px solid #c6ced8; border-radius: 6px; padding: 6px 10px; }
                QSlider::groove:horizontal { height: 6px; background: #dfe5ec; border-radius: 3px; margin: 4px 0px; }
                QSlider::handle:horizontal { background: #0078d4; border: 1px solid #0078d4; width: 14px; height: 14px; margin: -4px 0px; border-radius: 7px; }
                QSlider::sub-page:horizontal { background: #0078d4; border-radius: 3px; }
            """)
            self.hotkey_display.setStyleSheet("""
                QLineEdit {
                    background-color: #fff;
                    border: 1px solid #c6ced8;
                    border-radius: 6px;
                    color: #1f2937;
                    font: 13px 'Segoe UI';
                    padding: 4px;
                }
            """)
            self.lbl_hotkey_status.setStyleSheet("color: #607286; margin-top: 3px;")

    def toggle_rgb(self, state):
        self.island.rgb_enabled = bool(state)
        self.island.update()
        self.island.save_island_config()

    def update_rgb_speed(self, value):
        self.island.rgb_speed = value * 0.0005
        self.island.save_island_config()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    def on_battery_style_changed(self, index):
        self.island.battery_icon.set_style(index)
        self.island.battery_style = index
        self.island.save_island_config()

    def on_media_style_changed(self, index):
        self.island.set_media_style(index)
        self.island.media_style = index
        self.island.save_island_config()

    def on_net_mode_changed(self, index):
        self.island.net_display_mode = index
        self.island.save_island_config()
        self.island.update_stats()

    def on_pc_monitor_changed(self, index):
        self.island.pc_metric_type = index
        self.update_pc_color_btn()
        self.island.save_island_config()
        self.island.update_stats()

# =============== MAIN WIDGET =========================================
class DynamicIsland(QWidget):
    toggle_signal = Signal()

    def __init__(self, disable_tray=False):
        super().__init__()
        net = psutil.net_io_counters()
        self.last_recv = net.bytes_recv
        self.last_sent = net.bytes_sent
        self.startup_ts = time.perf_counter()
        self.media_is_playing = False
        self.power_mode = None

        self.rgb_enabled = False
        self.rgb_hue = 0.0
        self.rgb_speed = 0.01
        self.battery_style = 0
        self.media_style = 0
        self.toggle_hotkey = "Ctrl+D"
        self.panel_theme = 0
        self.net_display_mode = 1
        self.net_color = "#00FFCC"
        self.cpu_color = "#ffffff"
        self.ram_color = "#ff69b4"
        self.disk_color = "#b088ff"
        self.gpu_color = "#4cd964"
        self.pc_metric_type = 0
        self.last_gpu_util = 0.0
        self.last_ping_ms = -1.0

        self.load_island_config()

        self.current_media_style = self.media_style
        self.is_playing = True
        self.hotkey_shortcut = None
        self.disable_tray = disable_tray
        self.tray = None

        self.initUI()
        self.battery_icon.set_style(self.battery_style)
        self.set_media_style(self.media_style)
        self.register_toggle_shortcut()

        self.toggle_signal.connect(self.toggle_visibility)

        self.media_thread = MediaTrackerThread()
        self.media_thread.media_updated.connect(self.on_media_update)
        self.media_thread.start()

        self.ping_thread = PingTrackerThread()
        self.ping_thread.ping_updated.connect(self.on_ping_update)
        self.ping_thread.start()

        self.gpu_thread = GpuTrackerThread(self)
        self.gpu_thread.gpu_updated.connect(self.on_gpu_update)
        self.gpu_thread.start()
        self.last_gpu_util = -1.0

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop_worker_threads)

        self.last_media_title = ""

        self.panel_flag_timer = QTimer(self)
        self.panel_flag_timer.timeout.connect(self.check_panel_flag)
        self.panel_flag_timer.start(1000)

        self.control_panel = ControlPanelWindow(self)

        self.tray_poll = QTimer(self)
        self.tray_poll.timeout.connect(self.refresh_tray_ownership)
        self.tray_poll.start(500)
        self.refresh_tray_ownership()

    def stop_worker_threads(self):
        threads = [getattr(self, 'media_thread', None), getattr(self, 'ping_thread', None), getattr(self, 'gpu_thread', None)]
        for t in threads:
            try:
                if t is not None and t.isRunning():
                    t.requestInterruption()
            except Exception:
                pass
        for t in threads:
            try:
                if t is not None and t.isRunning():
                    t.wait(3500)
                if t is not None and t.isRunning():
                    t.terminate()
                    t.wait(1000)
            except Exception:
                pass

    def closeEvent(self, event):
        self.stop_worker_threads()
        super().closeEvent(event)

    def toggle_visibility(self):
        self.setVisible(not self.isVisible())

    def show_control_panel(self):
        self.control_panel.sync_from_island()
        self.control_panel.ensure_on_screen()
        self.control_panel.show()
        self.control_panel.ensure_on_screen()
        self.control_panel.raise_()
        self.control_panel.activateWindow()

    def initUI(self):
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        # Lock the island to a stable width so it doesn't slide when values change
        self.base_width = 300
        self.base_height = 42
        self.resize(self.base_width, self.base_height)

        screen_geo = QApplication.primaryScreen().availableGeometry()
        self.screen_width = screen_geo.width()
        self.move(screen_geo.left() + (self.screen_width - self.base_width) // 2, screen_geo.top() + 10)

        self.layout = QHBoxLayout()
        self.layout.setContentsMargins(15, 0, 15, 0)
        self.layout.setSpacing(6)

        self.btn_prev = self.create_button("⏮", self.prev_track)
        self.btn_play_pause = self.create_button("⏸", self.toggle_play_pause)
        self.btn_next = self.create_button("⏭", self.next_track)

        self.song_label = QLabel("")
        self.song_label.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.song_label.setStyleSheet("color: white;")

        self.layout.addWidget(self.btn_prev)
        self.layout.addWidget(self.btn_play_pause)
        self.layout.addWidget(self.btn_next)
        self.layout.addWidget(self.song_label)
        self.toggle_media_ui(False)
        self.layout.addSpacing(4)

        if self.net_display_mode == 0:
            initial_net = "↓ 0 Kbps"
        elif self.net_display_mode == 1:
            initial_net = "↓ 0 KB/s"
        else:
            initial_net = "Ping -- ms"
        self.net_label = QLabel(initial_net)
        self.net_label.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.net_label.setStyleSheet("color: #00FFCC;")
        self.net_label.setFixedWidth(88)
        self.net_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.layout.addWidget(self.net_label, alignment=Qt.AlignVCenter)

        # PC metric label (shows CPU/RAM/DSK/GPU alongside battery on laptops)
        self.pc_metric_label = QLabel("")
        self.pc_metric_label.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.pc_metric_label.setStyleSheet(f"color: {self.cpu_color};")
        self.pc_metric_label.setFixedWidth(62)
        self.pc_metric_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.pc_metric_label.setVisible(False)
        self.layout.addWidget(self.pc_metric_label, alignment=Qt.AlignVCenter)

        battery_container = QHBoxLayout()
        battery_container.setSpacing(4)
        battery_container.setContentsMargins(0, 0, 0, 0)

        self.battery_text = QLabel("100%")
        self.battery_text.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.battery_text.setStyleSheet("color: white;")
        self.battery_text.setFixedWidth(44)
        self.battery_text.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.battery_icon = BatteryWidget()

        battery_container.addWidget(self.battery_text, alignment=Qt.AlignVCenter)
        battery_container.addWidget(self.battery_icon, alignment=Qt.AlignVCenter)

        self.layout.addLayout(battery_container)
        self.setLayout(self.layout)
        self.setFixedSize(self.base_width, self.base_height)

        self.anim = QPropertyAnimation(self, b"geometry")
        self.anim.setDuration(300)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_stats)
        self.timer.start(1000)

        self.rgb_timer = QTimer(self)
        self.rgb_timer.timeout.connect(self.animate_rgb)
        self.rgb_timer.start(30)

        self.update_stats()

    def create_button(self, text, callback):
        btn = QPushButton(text)
        btn.setFont(QFont("Segoe UI Emoji", 10))
        btn.setFixedSize(25, 25)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            "QPushButton { background-color: transparent; color: white; border: none; } "
            "QPushButton:hover { background-color: #333333; border-radius: 12px; }"
        )
        btn.clicked.connect(callback)
        return btn

    # ------------- Media controls ----------------
    def toggle_play_pause(self):
        press_media_key(VK_MEDIA_PLAY_PAUSE)
        if self.is_probably_youtube():
            ctypes.windll.user32.keybd_event(VK_K, 0, 0, 0)
            ctypes.windll.user32.keybd_event(VK_K, 0, 2, 0)
        self.is_playing = not self.is_playing
        self.update_play_pause_icon()

    def update_play_pause_icon(self):
        play_icons = ["▶", "⏵", "▷"]
        pause_icons = ["⏸", "| |", "| |"]
        self.btn_play_pause.setText(pause_icons[self.current_media_style] if self.is_playing else play_icons[self.current_media_style])

    def set_media_style(self, index):
        self.current_media_style = index
        styles = [("⏮", "⏭"), ("⏴", "⏵"), ("◁", "▷")]
        prev, nxt = styles[index]
        self.btn_prev.setText(prev)
        self.btn_next.setText(nxt)
        self.update_play_pause_icon()

    def prev_track(self):
        press_media_key(VK_MEDIA_PREV_TRACK)
        if self.is_probably_youtube():
            press_combo(VK_SHIFT, VK_P)

    def next_track(self):
        press_media_key(VK_MEDIA_NEXT_TRACK)
        if self.is_probably_youtube():
            press_combo(VK_SHIFT, VK_N)

    def toggle_media_ui(self, is_visible):
        self.media_is_playing = is_visible
        self.btn_prev.setVisible(is_visible)
        self.btn_play_pause.setVisible(is_visible)
        self.btn_next.setVisible(is_visible)
        self.song_label.setVisible(is_visible)

    def on_media_update(self, has_media, title):
        if has_media and title:
            self.last_media_title = title
            display_title = title if len(title) < 22 else title[:19] + "..."
            self.song_label.setText(f"🎵 {display_title}")
            self.toggle_media_ui(True)
        else:
            self.toggle_media_ui(False)
        self.update_stats()

    def on_ping_update(self, ping_ms):
        self.last_ping_ms = ping_ms
        if self.net_display_mode == 2:
            if ping_ms >= 0:
                self.net_label.setText(f"Ping {int(round(ping_ms))} ms")
            else:
                self.net_label.setText("Ping -- ms")

    def on_gpu_update(self, val):
        self.last_gpu_util = val

    def is_probably_youtube(self):
        return any(x in self.last_media_title.lower() for x in ["youtube", "youtube music"])

    # ------------- RGB ----------------
    def animate_rgb(self):
        if self.rgb_enabled:
            self.rgb_hue += self.rgb_speed
            if self.rgb_hue > 1.0:
                self.rgb_hue -= 1.0
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setBrush(QColor(15, 15, 15, 245))
        if self.rgb_enabled:
            color = QColor.fromHsvF(self.rgb_hue, 1.0, 1.0)
            painter.setPen(QPen(color, 2))
            painter.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 19, 19)
        else:
            # Keep a subtle outline so the island edge is visible on dark backgrounds.
            painter.setPen(QPen(QColor(70, 70, 70, 220), 2))
            painter.drawRoundedRect(1, 1, self.width() - 2, self.height() - 2, 20, 20)

    # ------------- Network & battery ----------------
    def _wifi_iface(self):
        stats = psutil.net_if_stats()
        for name, st in stats.items():
            if st.isup and any(k in name.lower() for k in ["wi-fi", "wifi", "wlan", "wireless"]):
                return name
        return None

    def _wifi_counters(self):
        iface = self._wifi_iface()
        if not iface:
            return None
        return psutil.net_io_counters(pernic=True).get(iface)

    def update_stats(self):
        battery = None if TEST == 1 else psutil.sensors_battery()
        if self.pc_metric_type == 0:
            pc_color = getattr(self, 'cpu_color', "#ffffff")
            pc_value = psutil.cpu_percent(interval=None)
            pc_text = f"CPU {int(round(pc_value))}%"
        elif self.pc_metric_type == 1:
            pc_color = getattr(self, 'ram_color', "#ff69b4")
            pc_value = psutil.virtual_memory().percent
            pc_text = f"RAM {int(round(pc_value))}%"
        elif self.pc_metric_type == 2:
            pc_color = getattr(self, 'disk_color', "#b088ff")
            pc_value = psutil.disk_usage(os.path.abspath(os.sep)).percent
            pc_text = f"DSK {int(round(pc_value))}%"
        else:
            pc_color = getattr(self, 'gpu_color', "#4cd964")
            pc_value = max(0.0, getattr(self, 'last_gpu_util', 0.0))
            pc_text = f"GPU {int(round(pc_value))}%"

        if battery:
            if self.power_mode != "battery":
                self.battery_icon.setVisible(True)
                self.battery_text.setStyleSheet("color: white;")
                self.power_mode = "battery"
            self.battery_text.setText(f"{int(battery.percent)}%")
            self.battery_icon.update_battery(battery.percent, battery.power_plugged)

            self.pc_metric_label.setText(pc_text)
            self.pc_metric_label.setStyleSheet(f"color: {pc_color};")
            self.pc_metric_label.setVisible(True)
        else:
            current_mode = f"pc_{self.pc_metric_type}_{pc_color}"
            if self.power_mode != current_mode:
                self.battery_icon.setVisible(False)
                self.battery_text.setStyleSheet(f"color: {pc_color};")
                self.power_mode = current_mode

            self.battery_text.setText(pc_text)
            self.pc_metric_label.setVisible(False)

        curr = psutil.net_io_counters()
        d = max(0.0, (curr.bytes_recv - self.last_recv) * 8 / 1000000)
        u = max(0.0, (curr.bytes_sent - self.last_sent) * 8 / 1000000)
        self.last_recv, self.last_sent = curr.bytes_recv, curr.bytes_sent

        if self.net_display_mode == 0:
            if d >= 1000:
                self.net_label.setText(f"↓ {d / 1000:.1f} Gbps")
            elif d >= 1:
                self.net_label.setText(f"↓ {d:.1f} Mbps")
            else:
                self.net_label.setText(f"↓ {int(d * 1000)} Kbps")
        elif self.net_display_mode == 1:
            down_bytes = (d * 1_000_000) / 8.0
            if down_bytes >= 1_000_000_000:
                self.net_label.setText(f"↓ {down_bytes / 1_000_000_000:.2f} GB/s")
            elif down_bytes >= 1_000_000:
                self.net_label.setText(f"↓ {down_bytes / 1_000_000:.2f} MB/s")
            elif down_bytes >= 1_000:
                self.net_label.setText(f"↓ {down_bytes / 1_000:.1f} KB/s")
            else:
                self.net_label.setText(f"↓ {int(down_bytes)} B/s")
        else:
            if self.last_ping_ms >= 0:
                self.net_label.setText(f"Ping {int(round(self.last_ping_ms))} ms")
            else:
                self.net_label.setText("Ping -- ms")
        
        try:
            self.net_label.setStyleSheet(f"color: {getattr(self, 'net_color', '#00FFCC')};")
        except:
            pass

        # Keep the island width fixed so it doesn't shift as values/text change.
        target_w = 480 if self.media_is_playing else self.base_width
        is_animating = 0 < self.anim.currentTime() < self.anim.duration()
        
        if not is_animating:
            screen_geo = QApplication.primaryScreen().availableGeometry()
            target_x = int(screen_geo.left() + (screen_geo.width() - target_w) / 2)
            if self.width() != target_w or self.height() != self.base_height or self.x() != target_x:
                self.setGeometry(target_x, self.y(), target_w, self.base_height)
                self.setFixedSize(target_w, self.base_height)

    def animate_resize(self, target_width):
        self.setMinimumSize(0, self.base_height)
        self.setMaximumSize(9999, self.base_height)
        
        screen_geo = QApplication.primaryScreen().availableGeometry()
        current_rect = self.geometry()
        target_x = int(screen_geo.left() + (screen_geo.width() - target_width) / 2)
        target_rect = QRect(target_x, current_rect.y(), target_width, self.base_height)

        # Avoid startup flicker by skipping animation until the window is stable.
        if (time.perf_counter() - self.startup_ts) < 1.2:
            self.setGeometry(target_rect)
            self.setFixedSize(target_width, self.base_height)
            return

        self.anim.setStartValue(current_rect)
        self.anim.setEndValue(target_rect)
        self.anim.start()

    # ------------- Config persistence ----------------
    def detect_windows_panel_theme(self):
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            ) as key:
                apps_use_light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return 1 if int(apps_use_light) == 1 else 0
        except Exception:
            return 0

    def _normalize_hotkey(self, key_str):
        if not key_str:
            return "Ctrl+D"
        parts = [p.strip() for p in str(key_str).replace(" ", "").split("+") if p.strip()]
        alias = {
            "control": "ctrl",
            "leftctrl": "ctrl",
            "rightctrl": "ctrl",
            "leftalt": "alt",
            "rightalt": "alt",
            "leftshift": "shift",
            "rightshift": "shift",
            "win": "windows"
        }
        return "+".join(alias.get(p.lower(), p.lower()) for p in parts)

    def get_island_config_path(self):
        app_data = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(app_data, "Smart Monitor", "island_config.json")

    def save_island_config(self):
        data = {
            "rgb_enabled": self.rgb_enabled,
            "rgb_speed": self.rgb_speed,
            "battery_style": self.battery_style,
            "media_style": self.media_style,
            "toggle_hotkey": self.toggle_hotkey,
            "panel_theme": self.panel_theme,
            "net_display_mode": self.net_display_mode,
            "net_color": getattr(self, 'net_color', "#00FFCC"),
            "cpu_color": getattr(self, 'cpu_color', "#ffffff"),
            "ram_color": getattr(self, 'ram_color', "#ff69b4"),
            "disk_color": getattr(self, 'disk_color', "#b088ff"),
            "gpu_color": getattr(self, 'gpu_color', "#4cd964"),
            "pc_metric_type": getattr(self, 'pc_metric_type', 0)
        }
        try:
            config_path = self.get_island_config_path()
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception as e:
            print(f"Failed to save Island settings: {e}")

    def load_island_config(self):
        try:
            config_path = self.get_island_config_path()
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.rgb_enabled = data.get("rgb_enabled", False)
                    self.rgb_speed = data.get("rgb_speed", 0.01)
                    self.battery_style = data.get("battery_style", 0)
                    self.media_style = data.get("media_style", 0)
                    self.toggle_hotkey = data.get("toggle_hotkey", self.toggle_hotkey)
                    self.panel_theme = data.get("panel_theme", self.detect_windows_panel_theme())
                    self.net_display_mode = data.get("net_display_mode", 1)
                    self.net_color = data.get("net_color", "#00FFCC")
                    self.cpu_color = data.get("cpu_color", "#ffffff")
                    self.ram_color = data.get("ram_color", "#ff69b4")
                    self.disk_color = data.get("disk_color", "#b088ff")
                    self.gpu_color = data.get("gpu_color", "#4cd964")
                    self.pc_metric_type = data.get("pc_metric_type", 0)
            else:
                self.panel_theme = self.detect_windows_panel_theme()
        except Exception as e:
            print(f"Failed to load Island settings: {e}")

    def register_toggle_shortcut(self):
        self.unregister_toggle_shortcut()
        self.toggle_hotkey = self._normalize_hotkey(self.toggle_hotkey)
        try:
            self.hotkey_shortcut = keyboard.add_hotkey(
                self.toggle_hotkey,
                lambda: self.toggle_signal.emit(),
                suppress=False,
                trigger_on_release=False
            )
        except Exception:
            self.toggle_hotkey = "ctrl+d"
            self.hotkey_shortcut = keyboard.add_hotkey(
                self.toggle_hotkey,
                lambda: self.toggle_signal.emit(),
                suppress=False,
                trigger_on_release=False
            )

    def unregister_toggle_shortcut(self):
        if self.hotkey_shortcut is None:
            return
        try:
            keyboard.remove_hotkey(self.hotkey_shortcut)
        except Exception:
            pass
        self.hotkey_shortcut = None

    def set_toggle_hotkey(self, key_str):
        self.toggle_hotkey = self._normalize_hotkey(key_str)
        self.register_toggle_shortcut()
        # Persist immediately so SmartMonitor can pick up the latest key
        self.save_island_config()

    # ------------- Tray & flags ----------------
    def get_panel_flag_path(self):
        app_data = os.getenv("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(app_data, "Smart Monitor", "open_island_panel.flag")

    def check_panel_flag(self):
        try:
            flag_path = self.get_panel_flag_path()
            if os.path.exists(flag_path):
                os.remove(flag_path)
                self.show_control_panel()
        except Exception:
            pass

    def is_smartmonitor_running(self):
        for proc in psutil.process_iter(['name', 'cmdline']):
            try:
                name = (proc.info.get('name') or '').lower()
                if name in ("smartmonitor.exe", "smart monitor.exe", "smartmonitor"):
                    return True
                cmdline = " ".join(proc.info.get('cmdline') or []).lower()
                if "smartmonitor.pyw" in cmdline or "smartmonitor.py" in cmdline:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return False

    def ensure_tray(self):
        if self.tray is not None:
            return
        icon_path = create_tray_icon()
        self.tray = QSystemTrayIcon(QIcon(icon_path), self)
        tray_menu = QMenu()
        tray_menu.setStyleSheet("""
            QMenu { background-color: #2d2d2d; color: white; border: 1px solid #444; border-radius: 6px; padding: 5px; }
            QMenu::item { padding: 5px 20px; border-radius: 4px; }
            QMenu::item:selected { background-color: #0078d4; color: white; }
        """)
        tray_menu.addAction("Show/Hide Island", self.toggle_visibility)
        tray_menu.addAction("Island Control Panel", self.show_control_panel)
        tray_menu.addSeparator()
        tray_menu.addAction("Exit", QApplication.instance().quit)
        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(lambda r: self.toggle_visibility() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()

    def drop_tray(self):
        if self.tray is None:
            return
        self.tray.hide()
        self.tray.deleteLater()
        self.tray = None

    def refresh_tray_ownership(self):
        if self.disable_tray:
            self.drop_tray()
            return
        if self.is_smartmonitor_running():
            self.drop_tray()
        else:
            self.ensure_tray()

# =============== TRAY ICON HELPER ====================================
def create_tray_icon():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((8, 12, 56, 42), outline="white", width=4)
    d.rectangle((28, 42, 36, 50), fill="white")
    d.rectangle((20, 50, 44, 54), fill="white")
    path = os.path.join(os.environ.get("TEMP", ""), "tray_island_icon.png")
    img.save(path)
    return path

# =============== ACTIVATION ==========================================
def check_island_activation():
    stored = _read_secure_activation_key()
    if stored == ACTIVATION_KEY:
        return True
    return _migrate_legacy_registry_key()

def activate_island_key(key_str):
    if (key_str or "").strip() != ACTIVATION_KEY:
        return False
    return _write_secure_activation_key(ACTIVATION_KEY)

class IslandActivationWindow(QWidget):
    def __init__(self, disable_tray=False):
        super().__init__()
        self.disable_tray = disable_tray
        self._island_instance = None
        self.setWindowTitle("Activate Dynamic Island")
        self.setFixedSize(350, 160)
        self.setStyleSheet("background-color: #1e1e1e; color: white; font-family: 'Segoe UI'; font-size: 14px;")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Dialog)
        icon_path = _resolve_app_icon_path()
        if icon_path and os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        layout = QVBoxLayout()
        lbl = QLabel("Dynamic Island requires an activation key.\n\nGet yours today! Activate in unlimited device")
        lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(lbl)
        
        btn_layout = QHBoxLayout()
        btn_buy = QPushButton("BUY")
        btn_buy.setCursor(Qt.PointingHandCursor)
        btn_buy.setStyleSheet("background-color: #0078d4; font-weight: bold; padding: 8px; border-radius: 4px;")
        btn_buy.clicked.connect(self.buy)
        
        btn_activate = QPushButton("Activate using key")
        btn_activate.setCursor(Qt.PointingHandCursor)
        btn_activate.setStyleSheet("background-color: #444444; font-weight: bold; padding: 8px; border-radius: 4px;")
        btn_activate.clicked.connect(self.activate_key)

        btn_trial = QPushButton("Try 2 mins")
        btn_trial.setCursor(Qt.PointingHandCursor)
        btn_trial.setStyleSheet("background-color: #2f6f3e; font-weight: bold; padding: 8px; border-radius: 4px;")
        btn_trial.clicked.connect(self.start_trial)
        if _is_trial_consumed():
            btn_trial.setEnabled(False)
            btn_trial.setText("Trial Used")
        
        btn_layout.addWidget(btn_buy)
        btn_layout.addWidget(btn_activate)
        btn_layout.addWidget(btn_trial)
        layout.addLayout(btn_layout)
        self.setLayout(layout)
        
    def buy(self):
        webbrowser.open("https://jaks-360-aboutme.netlify.app/")
        
    def activate_key(self):
        dlg = QInputDialog(self)
        dlg.setWindowTitle("Activate Island")
        dlg.setLabelText("Enter activation key:")
        dlg.setTextEchoMode(QLineEdit.Password)
        dlg.setStyleSheet("background-color: #1e1e1e; color: white; font-family: 'Segoe UI'; font-size: 12px;")
        icon_path = _resolve_app_icon_path()
        if icon_path and os.path.exists(icon_path):
            dlg.setWindowIcon(QIcon(icon_path))
        ok = dlg.exec()
        entered = dlg.textValue() if ok else ""
        if not ok:
            return
        if activate_island_key(entered):
            QMessageBox.information(self, "Activated", "Dynamic Island activated successfully.")
            self.launch_island(trial_seconds=0)
            return
        QMessageBox.warning(self, "Invalid Key", "Activation key is invalid.")

    def start_trial(self):
        if _is_trial_consumed():
            QMessageBox.information(self, "Trial", "2-minute trial already used on this device/user.")
            return
        remaining = _set_trial_start_if_allowed(TRIAL_SECONDS_DEFAULT)
        if remaining <= 0:
            QMessageBox.information(self, "Trial", "2-minute trial already used on this device/user.")
            return
        self.launch_island(trial_seconds=remaining)

    def launch_island(self, trial_seconds=0):
        app = QApplication.instance()
        if app:
            app.setQuitOnLastWindowClosed(False)
        self.hide()
        self._island_instance = DynamicIsland(disable_tray=self.disable_tray)
        self._island_instance.show()
        if trial_seconds > 0 and app:
            _set_trial_expiry(time.time() + max(1, int(trial_seconds)))
            def quit_trial():
                try:
                    if self._island_instance is not None:
                        self._island_instance.stop_worker_threads()
                except Exception:
                    pass
                _mark_trial_consumed()
                app.quit()
            QTimer.singleShot(max(1, int(trial_seconds)) * 1000, quit_trial)

# =============== MAIN ================================================
def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    no_tray = "--no-tray" in sys.argv
    trial_mode = "--trial-2m" in sys.argv
    trial_seconds = TRIAL_SECONDS_DEFAULT

    for arg in sys.argv:
        if arg.startswith("--trial-seconds="):
            try:
                parsed = int(arg.split("=", 1)[1])
                if parsed > 0:
                    trial_seconds = parsed
            except Exception:
                pass

    trial_remaining = _get_trial_remaining_seconds()
    if _is_trial_consumed() and trial_mode:
        trial_mode = False
        trial_seconds = 0
    if trial_remaining > 0:
        trial_mode = True
        trial_seconds = max(trial_seconds, trial_remaining)

    app = QApplication(sys.argv)
    app.setStyle("windowsvista")
    icon_path = _resolve_app_icon_path()
    if icon_path and os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    
    if (not trial_mode) and (not check_island_activation()):
        app.setQuitOnLastWindowClosed(False)
        win = IslandActivationWindow(disable_tray=no_tray)
        win.show()
        sys.exit(app.exec())

    app.setQuitOnLastWindowClosed(False)

    island = DynamicIsland(disable_tray=no_tray)
    island.show()
    if trial_mode:
        _set_trial_expiry(time.time() + max(1, int(trial_seconds)))
        def quit_trial_main():
            try:
                island.stop_worker_threads()
            except Exception:
                pass
            _mark_trial_consumed()
            app.quit()
        QTimer.singleShot(max(1, trial_seconds) * 1000, quit_trial_main)
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
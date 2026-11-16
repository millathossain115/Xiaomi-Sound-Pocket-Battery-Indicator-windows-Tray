"""
Xiaomi Sound Pocket / Pocket Speaker 2 Battery Monitor for Windows System Tray
Automated Tray Presence, Native Windows 11 Fluent Multi-Style High-Visibility Rendering,
Type-C Wired Charging Detection, and Single-Instance Win32 Mutex.
"""

import os
import sys
import time
import threading
import contextlib
import tempfile
import winreg
import ctypes
from ctypes import wintypes
import uuid
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import pystray
from pystray import MenuItem as item
import psutil

# Win32 APIs
kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
cfgmgr32 = ctypes.windll.cfgmgr32

# Enable Per-Monitor v2 DPI awareness for native pixel rendering
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

# Monkey-patch pystray._win32 for razor-sharp native DPI icon loading
try:
    import pystray._win32 as _pystray_win32

    @contextlib.contextmanager
    def _custom_serialized_image(image, format, extension=None):
        fd, path = tempfile.mkstemp('.%s' % (extension or format.lower()))
        try:
            with os.fdopen(fd, 'wb') as f:
                if str(format).upper() == 'ICO':
                    image.save(f, format='ICO', sizes=[(image.width, image.height)])
                else:
                    image.save(f, format=format)
            yield path
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

    _pystray_win32.serialized_image = _custom_serialized_image

    def _custom_assert_icon_handle(self):
        if self._icon_handle:
            return
        with _custom_serialized_image(self.icon, 'ICO') as icon_path:
            self._icon_handle = _pystray_win32.win32.LoadImage(
                None,
                icon_path,
                _pystray_win32.win32.IMAGE_ICON,
                self.icon.width,
                self.icon.height,
                _pystray_win32.win32.LR_LOADFROMFILE
            )

    _pystray_win32.Icon._assert_icon_handle = _custom_assert_icon_handle
except Exception:
    pass

# Device Property Structures
class DEVPROPKEY(ctypes.Structure):
    _fields_ = [('fmtid', ctypes.c_byte * 16), ('pid', wintypes.ULONG)]

# Windows PnP DEVPKEY_Device_BatteryLevel
bat_key = DEVPROPKEY()
ctypes.memmove(bat_key.fmtid, uuid.UUID('{104ea319-6ee2-4701-bd47-8ddbf425bbe5}').bytes_le, 16)
bat_key.pid = 2

# Windows PnP DEVPKEY_Device_FriendlyName
name_key = DEVPROPKEY()
ctypes.memmove(name_key.fmtid, uuid.UUID('{a45c254e-df1c-4efd-8020-67d146a850e0}').bytes_le, 16)
name_key.pid = 14

# Windows PnP DEVPKEY_Device_IsConnected ({83da6326-97a6-4088-9453-a1923f573b29}, 15)
conn_key = DEVPROPKEY()
ctypes.memmove(conn_key.fmtid, uuid.UUID('{83da6326-97a6-4088-9453-a1923f573b29}').bytes_le, 16)
conn_key.pid = 15

APP_NAME = "Xiaomi Speaker Battery Tray"
MUTEX_NAME = r"Local\Xiaomi_Sound_Pocket_Battery_Monitor_Mutex"
STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_NAME = "XiaomiSpeakerBatteryTray"
CONFIG_REG_KEY = r"Software\XiaomiSpeakerBatteryTray"

# Hardware IDs for Xiaomi Audio & Type-C Chipset
XIAOMI_VID = 0x05D6
XIAOMI_PID = 0x000A
JIELI_VID = 0x4C4A   # JieLi (JL) USB SoC used in Xiaomi Sound Pocket
JIELI_PID = 0x4A55


def ensure_single_instance():
    """Terminate existing instances so new process starts cleanly with single mutex."""
    try:
        curr_pid = os.getpid()
        parent_pid = os.getppid()
        killed_any = False
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                pid = p.info['pid']
                if pid in (curr_pid, parent_pid):
                    continue
                pname = str(p.info['name']).lower()
                cmdline = ' '.join(p.info['cmdline'] or []).lower()
                if 'xiaomi_speaker_battery' in pname or 'xiaomi_speaker_battery.py' in cmdline:
                    p.kill()
                    killed_any = True
            except Exception:
                pass
        if killed_any:
            time.sleep(0.5)
    except Exception:
        pass

    mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return mutex_handle


class XiaomiBatteryMonitor:
    def __init__(self, target_name="Xiaomi Sound Pocket"):
        self.target_name = target_name
        self.battery_pct = 100
        self.is_connected = False
        self.is_wired = False
        self.low_battery_warned = False
        self.poll_interval = 3  # Fast 3-second poll for instant connection & wire detection
        self.running = True
        self.icon = None
        self.tray_size = self._get_native_tray_size()
        self.current_style = self._load_saved_style()

    def _get_native_tray_size(self):
        """Query physical taskbar icon metric for exact 1:1 pixel rendering."""
        try:
            sz = user32.GetSystemMetrics(49)  # SM_CXSMICON
            if 16 <= sz <= 64:
                return sz
        except Exception:
            pass
        return 20  # Fallback for standard 125% DPI display

    def _load_saved_style(self):
        """Load preferred style from registry (defaults to 'gauge')."""
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, CONFIG_REG_KEY, 0, winreg.KEY_READ)
            val, _ = winreg.QueryValueEx(key, "IconStyle")
            winreg.CloseKey(key)
            if val in ("gauge", "speaker", "waves", "brackets", "topbar", "clean"):
                return val
        except Exception:
            pass
        return "gauge"  # Default: typical laptop/mobile battery shape + progress (no numbers)

    def _save_style(self, style_name):
        try:
            key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, CONFIG_REG_KEY)
            winreg.SetValueEx(key, "IconStyle", 0, winreg.REG_SZ, style_name)
            winreg.CloseKey(key)
        except Exception:
            pass

    def check_wired_connection(self):
        """Check if speaker is wired directly to PC via USB Type-C cable."""
        try:
            found_wired = False

            def scan_usb(dn):
                nonlocal found_wired
                if found_wired:
                    return
                id_buf = (ctypes.c_wchar * 512)()
                if cfgmgr32.CM_Get_Device_IDW(dn, id_buf, 512, 0) == 0:
                    dev_id = id_buf.value.lower()
                    if ("vid_4c4a" in dev_id and "pid_4a55" in dev_id) or \
                       "vid_4c4a" in dev_id or \
                       "vid_05d6" in dev_id or \
                       ("usb" in dev_id and ("pocket" in dev_id or "xiaomi" in dev_id)):
                        found_wired = True
                        return

                child = wintypes.DWORD()
                if cfgmgr32.CM_Get_Child(ctypes.byref(child), dn, 0) == 0:
                    scan_usb(child.value)
                sibling = wintypes.DWORD()
                if cfgmgr32.CM_Get_Sibling(ctypes.byref(sibling), dn, 0) == 0:
                    scan_usb(sibling.value)

            root = wintypes.DWORD()
            if cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(root), None, 0) == 0:
                scan_usb(root.value)

            return found_wired
        except Exception:
            return False

    def read_battery(self):
        """Scan Windows DevNode tree for Bluetooth connection state, battery level & wired status."""
        is_bluetooth_connected = False
        battery_pct = None

        def scan(dn):
            nonlocal is_bluetooth_connected, battery_pct
            name_buf = (ctypes.c_wchar * 256)()
            name_size = wintypes.ULONG(512)
            propType = wintypes.ULONG()

            if cfgmgr32.CM_Get_DevNode_PropertyW(dn, ctypes.byref(name_key), ctypes.byref(propType), name_buf, ctypes.byref(name_size), 0) == 0:
                raw_name = name_buf.value
                if self.target_name.lower() in raw_name.lower():
                    id_buf = (ctypes.c_wchar * 512)()
                    cfgmgr32.CM_Get_Device_IDW(dn, id_buf, 512, 0)
                    dev_id = id_buf.value.lower()

                    # 1. Check IsConnected on the main Bluetooth Device Node (BTHENUM\DEV_...)
                    if 'bthenum\\dev_' in dev_id:
                        c_buf = (ctypes.c_byte * 16)()
                        c_size = wintypes.ULONG(16)
                        if cfgmgr32.CM_Get_DevNode_PropertyW(dn, ctypes.byref(conn_key), ctypes.byref(propType), c_buf, ctypes.byref(c_size), 0) == 0:
                            if c_buf[0] != 0:
                                is_bluetooth_connected = True

                    # 2. Check Battery Level (on Hands-Free AG or any child node)
                    if battery_pct is None:
                        bat_buf = (ctypes.c_byte * 64)()
                        bat_size = wintypes.ULONG(64)
                        if cfgmgr32.CM_Get_DevNode_PropertyW(dn, ctypes.byref(bat_key), ctypes.byref(propType), bat_buf, ctypes.byref(bat_size), 0) == 0:
                            battery_pct = int(bat_buf[0])

            child = wintypes.DWORD()
            if cfgmgr32.CM_Get_Child(ctypes.byref(child), dn, 0) == 0:
                scan(child.value)
            sibling = wintypes.DWORD()
            if cfgmgr32.CM_Get_Sibling(ctypes.byref(sibling), dn, 0) == 0:
                scan(sibling.value)

        root = wintypes.DWORD()
        if cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(root), None, 0) == 0:
            scan(root.value)

        wired = self.check_wired_connection()

        if is_bluetooth_connected:
            pct = battery_pct if battery_pct is not None else self.battery_pct
            return True, pct, wired
        elif wired:
            return True, self.battery_pct, True
        return False, None, False

    def _get_font(self, size, bold=True):
        candidates = [
            r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
            r"C:\Windows\Fonts\seguisb.ttf",
            r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        ]
        for p in candidates:
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    pass
        return ImageFont.load_default()

    def _apply_alpha_lut(self, img):
        r, g, b, a = img.split()
        lut = []
        for i in range(256):
            if i < 25:
                lut.append(0)
            elif i < 110:
                lut.append(int((i - 25) * (185.0 / 85.0)))
            else:
                v = int(185 + (i - 110) * (70.0 / 145.0) * 1.5)
                lut.append(min(255, v))
        return Image.merge('RGBA', (r, g, b, a.point(lut)))

    def generate_icon(self, percentage, connected, is_wired=False):
        """Generate icon based on the user's currently selected style."""
        scale = 4
        buf_size = self.tray_size * scale
        img = Image.new('RGBA', (buf_size, buf_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        white = (255, 255, 255, 255)
        accent_blue = (56, 185, 255, 255)
        warning_red = (255, 75, 75, 255)

        if not connected:
            frame_color = (130, 135, 145, 255)
            num_color = frame_color
            bolt_color = frame_color
        elif is_wired:
            frame_color = accent_blue
            num_color = accent_blue
            bolt_color = accent_blue
        elif percentage <= 15:
            frame_color = warning_red
            num_color = warning_red
            bolt_color = warning_red
        else:
            frame_color = white
            num_color = white
            bolt_color = white

        center_x = buf_size // 2
        center_y = buf_size // 2
        sw = max(2, int(scale * 1.0))  # 1 physical screen pixel

        # STYLE 1: TYPICAL LAPTOP / MOBILE BATTERY GAUGE (Elongated Landscape Capsule)
        if self.current_style == "gauge":
            sw = max(2, int(scale * 1.15))  # Crisp 1.15px outline

            # Maximum edge-to-edge horizontal width: 19.2px total
            b_left = int(scale * 0.4)
            b_right = int(scale * 17.0)

            # Elongated landscape height (10.8px tall for an authentic ~1.8:1 battery ratio)
            half_h = int(scale * 5.4)
            b_top = center_y - half_h
            b_bot = center_y + half_h
            b_rad = int(scale * 2.0)

            # Prominent terminal nub on right
            n_left = b_right
            n_right = min(buf_size - 1, int(scale * 19.6))
            nub_half = int(scale * 2.8)
            n_top = center_y - nub_half
            n_bot = center_y + nub_half
            n_rad = int(scale * 0.8)

            draw.rounded_rectangle([b_left, b_top, b_right, b_bot], radius=b_rad, outline=frame_color, width=sw)
            draw.rounded_rectangle([n_left, n_top, n_right, n_bot], radius=n_rad, fill=frame_color)

            # Interior progress bar (fills proportionally 0%..100%)
            gap = int(scale * 1.0)
            f_left = b_left + sw + gap
            f_top = b_top + sw + gap
            f_right_max = b_right - sw - gap
            f_bot = b_bot - sw - gap

            total_w = f_right_max - f_left
            fill_w = max(0, int(total_w * (percentage / 100.0)))

            if fill_w > 0:
                fill_right = f_left + fill_w
                f_rad = max(1, int(scale * 0.7))
                draw.rounded_rectangle([f_left, f_top, fill_right, f_bot], radius=f_rad, fill=frame_color)

        # STYLE 2: MINI HI-FI SPEAKER (🔊 80)
        elif self.current_style == "speaker":
            # Speaker box
            draw.rounded_rectangle([int(scale * 1.0), int(scale * 7.5), int(scale * 2.8), int(scale * 12.5)], radius=int(scale * 0.5), fill=frame_color)
            # Speaker cone
            cone = [
                (int(scale * 2.8), int(scale * 7.5)),
                (int(scale * 5.8), int(scale * 4.5)),
                (int(scale * 5.8), int(scale * 15.5)),
                (int(scale * 2.8), int(scale * 12.5))
            ]
            draw.polygon(cone, fill=frame_color)

            if is_wired:
                # Active charging lightning bolt next to cone
                draw.line([(int(scale * 6.5), int(scale * 5.0)), (int(scale * 7.5), int(scale * 10.0)), (int(scale * 6.5), int(scale * 15.0))], fill=bolt_color, width=sw)
            else:
                # Sound wave arc
                draw.arc([int(scale * 5.0), int(scale * 6.0), int(scale * 7.5), int(scale * 14.0)], start=-60, end=60, fill=frame_color, width=sw)

            f_size = int(scale * 13.0) if percentage < 100 else int(scale * 10.5)
            font = self._get_font(f_size, bold=True)
            draw.text((int(scale * 14.0), center_y), str(percentage), font=font, fill=num_color, anchor="mm")

        # STYLE 2: EQUALIZER AUDIO WAVES (ılı 80)
        elif self.current_style == "waves":
            b_w = int(scale * 1.4)
            draw.rounded_rectangle([int(scale * 1.0), int(scale * 8.0), int(scale * 1.0) + b_w, int(scale * 12.0)], radius=int(scale * 0.7), fill=frame_color)
            draw.rounded_rectangle([int(scale * 3.4), int(scale * 3.5), int(scale * 3.4) + b_w, int(scale * 16.5)], radius=int(scale * 0.7), fill=frame_color)
            draw.rounded_rectangle([int(scale * 5.8), int(scale * 5.5), int(scale * 5.8) + b_w, int(scale * 14.5)], radius=int(scale * 0.7), fill=frame_color)

            if is_wired:
                draw.line([(int(scale * 7.8), int(scale * 4.5)), (int(scale * 7.8), int(scale * 15.5))], fill=bolt_color, width=sw)

            f_size = int(scale * 13.0) if percentage < 100 else int(scale * 10.5)
            font = self._get_font(f_size, bold=True)
            draw.text((int(scale * 13.8), center_y), str(percentage), font=font, fill=num_color, anchor="mm")

        # STYLE 3: SPEAKER DRIVER / DISC (◎ 80)
        elif self.current_style == "audio_disc":
            draw.ellipse([int(scale * 1.0), int(scale * 4.0), int(scale * 7.0), int(scale * 16.0)], outline=frame_color, width=sw)
            draw.ellipse([int(scale * 3.0), int(scale * 8.5), int(scale * 5.0), int(scale * 11.5)], fill=frame_color)

            f_size = int(scale * 13.0) if percentage < 100 else int(scale * 10.5)
            font = self._get_font(f_size, bold=True)
            draw.text((int(scale * 13.8), center_y), str(percentage), font=font, fill=num_color, anchor="mm")

        # STYLE 4: OPEN BATTERY BRACKETS [ 80 ]
        elif self.current_style == "brackets":
            draw.line([(int(scale * 3.5), int(scale * 3.0)), (int(scale * 1.5), int(scale * 3.0))], fill=frame_color, width=sw)
            draw.line([(int(scale * 1.5), int(scale * 3.0)), (int(scale * 1.5), int(scale * 17.0))], fill=frame_color, width=sw)
            draw.line([(int(scale * 1.5), int(scale * 17.0)), (int(scale * 3.5), int(scale * 17.0))], fill=frame_color, width=sw)

            draw.line([(int(scale * 16.5), int(scale * 3.0)), (int(scale * 18.5), int(scale * 3.0))], fill=frame_color, width=sw)
            draw.line([(int(scale * 18.5), int(scale * 3.0)), (int(scale * 18.5), int(scale * 17.0))], fill=frame_color, width=sw)
            draw.line([(int(scale * 18.5), int(scale * 17.0)), (int(scale * 16.5), int(scale * 17.0))], fill=frame_color, width=sw)
            draw.rounded_rectangle([int(scale * 18.5), int(scale * 7.0), min(buf_size - 1, int(scale * 19.8)), int(scale * 13.0)], radius=int(scale * 0.8), fill=frame_color)

            if is_wired:
                pts = [
                    (int(scale * 4.0), center_y - int(scale * 4.5)),
                    (int(scale * 2.5), center_y + int(scale * 0.2)),
                    (int(scale * 4.2), center_y + int(scale * 0.2)),
                    (int(scale * 3.0), center_y + int(scale * 5.0)),
                    (int(scale * 6.3), center_y - int(scale * 1.0)),
                    (int(scale * 4.5), center_y - int(scale * 1.0)),
                ]
                draw.polygon(pts, fill=bolt_color)
                f_size = int(scale * 11.0) if percentage < 100 else int(scale * 9.0)
                font = self._get_font(f_size, bold=True)
                draw.text((center_x + int(scale * 2.0), center_y), str(percentage), font=font, fill=num_color, anchor="mm")
            else:
                f_size = int(scale * 12.5) if percentage < 100 else int(scale * 10.0)
                font = self._get_font(f_size, bold=True)
                draw.text((center_x, center_y), str(percentage), font=font, fill=num_color, anchor="mm")

        # STYLE 5: TOP ACCENT BAR (— 80 —)
        elif self.current_style == "topbar":
            bar_y1 = int(scale * 2.0)
            bar_y2 = int(scale * 3.5)
            draw.rounded_rectangle([int(scale * 1.5), bar_y1, int(scale * 17.5), bar_y2], radius=int(scale * 0.8), fill=frame_color)
            draw.rounded_rectangle([int(scale * 17.5), bar_y1 + 1, min(buf_size - 1, int(scale * 18.8)), bar_y2 - 1], radius=int(scale * 0.4), fill=frame_color)

            if is_wired:
                pts = [
                    (int(scale * 2.5), center_y - int(scale * 4.0)),
                    (int(scale * 1.0), center_y + int(scale * 0.5)),
                    (int(scale * 2.8), center_y + int(scale * 0.5)),
                    (int(scale * 1.8), center_y + int(scale * 5.0)),
                    (int(scale * 4.8), center_y - int(scale * 0.5)),
                    (int(scale * 3.0), center_y - int(scale * 0.5)),
                ]
                draw.polygon(pts, fill=bolt_color)
                f_size = int(scale * 12.0) if percentage < 100 else int(scale * 10.0)
                font = self._get_font(f_size, bold=True)
                draw.text((center_x + int(scale * 1.8), center_y + int(scale * 1.0)), str(percentage), font=font, fill=num_color, anchor="mm")
            else:
                f_size = int(scale * 13.5) if percentage < 100 else int(scale * 11.0)
                font = self._get_font(f_size, bold=True)
                draw.text((center_x, center_y + int(scale * 1.0)), str(percentage), font=font, fill=num_color, anchor="mm")

        # STYLE 6: CLEAN BORDERLESS DIGITS (80)
        else:
            if is_wired:
                bolt_pts = [
                    (int(scale * 3.8), int(scale * 2.5)),
                    (int(scale * 1.6), int(scale * 7.0)),
                    (int(scale * 3.8), int(scale * 7.0)),
                    (int(scale * 2.2), int(scale * 12.0)),
                    (int(scale * 6.2), int(scale * 5.5)),
                    (int(scale * 4.0), int(scale * 5.5)),
                ]
                draw.polygon(bolt_pts, fill=bolt_color)
                f_size = int(scale * 13.0) if percentage < 100 else int(scale * 10.5)
                font = self._get_font(f_size, bold=True)
                draw.text((center_x + int(scale * 2.2), center_y), str(percentage), font=font, fill=num_color, anchor="mm")
            else:
                f_size = int(scale * 14.0) if percentage < 100 else int(scale * 11.0)
                font = self._get_font(f_size, bold=True)
                draw.text((center_x, center_y), str(percentage), font=font, fill=num_color, anchor="mm")

        down = img.resize((self.tray_size, self.tray_size), Image.Resampling.LANCZOS)
        return self._apply_alpha_lut(down)

    def set_style(self, style_name):
        self.current_style = style_name
        self._save_style(style_name)
        self._update_ui()

    def send_notification(self, title, message):
        """Send native Windows toast notification asynchronously."""
        def _show():
            try:
                from win11toast import toast
                toast(title, message)
            except Exception:
                pass
        threading.Thread(target=_show, daemon=True).start()

    def check_startup_enabled(self):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_READ)
            val, _ = winreg.QueryValueEx(key, STARTUP_REG_NAME)
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            return False

    def toggle_startup(self, icon, item):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_KEY, 0, winreg.KEY_ALL_ACCESS)
            if self.check_startup_enabled():
                winreg.DeleteValue(key, STARTUP_REG_NAME)
            else:
                exe_path = sys.executable
                if getattr(sys, 'frozen', False):
                    cmd = f'"{exe_path}"'
                else:
                    script_path = os.path.abspath(__file__)
                    cmd = f'"{exe_path}" "{script_path}"'
                winreg.SetValueEx(key, STARTUP_REG_NAME, 0, winreg.REG_SZ, cmd)
            winreg.CloseKey(key)
        except Exception as e:
            print("Startup toggle error:", e)

    def promote_taskbar_icon(self):
        """Automatically promote icon to always show on Windows 11 taskbar instead of overflow menu."""
        def _promote():
            time.sleep(1.5)
            try:
                current_exe = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
                current_name = os.path.basename(current_exe).lower()
                base = r"Control Panel\NotifyIconSettings"
                k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base, 0, winreg.KEY_READ)
                i = 0
                target_keys = []
                while True:
                    try:
                        sub = winreg.EnumKey(k, i)
                        i += 1
                        sub_k = winreg.OpenKey(k, sub, 0, winreg.KEY_READ)
                        try:
                            exe_path, _ = winreg.QueryValueEx(sub_k, "ExecutablePath")
                            if "xiaomi" in exe_path.lower() or os.path.basename(exe_path).lower() == current_name:
                                target_keys.append(sub)
                        except Exception:
                            pass
                        winreg.CloseKey(sub_k)
                    except OSError:
                        break
                winreg.CloseKey(k)

                for sub in target_keys:
                    try:
                        sub_k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\{sub}", 0, winreg.KEY_ALL_ACCESS)
                        winreg.SetValueEx(sub_k, "IsPromoted", 0, winreg.REG_DWORD, 1)
                        winreg.CloseKey(sub_k)
                    except Exception:
                        pass
            except Exception:
                pass

        threading.Thread(target=_promote, daemon=True).start()

    def _get_status_text(self):
        if not self.is_connected:
            return f"{self.target_name}: Disconnected"
        status = f"{self.target_name}: {self.battery_pct}%"
        if self.is_wired:
            status += " ⚡ (Charging / Wired)"
        return status

    def _update_ui(self):
        if not self.icon:
            return

        if self.is_connected:
            self.icon.icon = self.generate_icon(self.battery_pct, True, self.is_wired)
            self.icon.title = self._get_status_text()
            if not self.icon.visible:
                self.icon.visible = True
                self.promote_taskbar_icon()
        else:
            if self.icon.visible:
                self.icon.visible = False

    def poll_loop(self):
        """Continuous background monitor with auto-show / auto-hide automation."""
        while self.running:
            success, pct, wired = self.read_battery()
            was_connected = self.is_connected

            if success:
                self.is_connected = True
                self.battery_pct = pct
                self.is_wired = wired

                if not was_connected:
                    msg = f"Connected ({pct}%)"
                    if wired:
                        msg += " ⚡ (Wired / Charging)"
                    self.send_notification("Xiaomi Speaker Connected", msg)

                if not self.is_wired and pct <= 20 and not self.low_battery_warned:
                    self.send_notification(
                        "Xiaomi Speaker Low Battery",
                        f"Speaker battery is low ({pct}%). Please recharge soon!"
                    )
                    self.low_battery_warned = True
                elif self.is_wired or pct > 30:
                    self.low_battery_warned = False
            else:
                self.is_connected = False
                self.is_wired = False

            self._update_ui()
            time.sleep(self.poll_interval)

    def manual_refresh(self, icon, item):
        success, pct, wired = self.read_battery()
        if success:
            self.is_connected = True
            self.battery_pct = pct
            self.is_wired = wired
        else:
            self.is_connected = False
            self.is_wired = False
        self._update_ui()

    def toggle_wired_mode(self, icon, item):
        """Allow manual toggle of wired charging status for testing or power-only cables."""
        self.is_wired = not self.is_wired
        self._update_ui()

    def exit_app(self, icon, item):
        self.running = False
        icon.stop()

    def on_setup(self, icon):
        """Called once icon message pump has started."""
        success, pct, wired = self.read_battery()
        if success:
            self.is_connected = True
            self.battery_pct = pct
            self.is_wired = wired
            icon.visible = True
            self.promote_taskbar_icon()
        else:
            self.is_connected = False
            self.is_wired = False
            icon.visible = False

        worker = threading.Thread(target=self.poll_loop, daemon=True)
        worker.start()

    def run(self):
        success, pct, wired = self.read_battery()
        if success:
            self.is_connected = True
            self.battery_pct = pct
            self.is_wired = wired

        init_icon = self.generate_icon(self.battery_pct, self.is_connected, self.is_wired)

        menu = pystray.Menu(
            item(lambda text: self._get_status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Style", pystray.Menu(
                item("Typical Battery Gauge (Shape & Progress) [Default]", lambda: self.set_style("gauge"), checked=lambda item: self.current_style == "gauge", radio=True),
                item("Mini Hi-Fi Speaker (🔊 80)", lambda: self.set_style("speaker"), checked=lambda item: self.current_style == "speaker", radio=True),
                item("Equalizer Audio Waves (ılı 80)", lambda: self.set_style("waves"), checked=lambda item: self.current_style == "waves", radio=True),
                item("Open Battery Brackets [ 80 ]", lambda: self.set_style("brackets"), checked=lambda item: self.current_style == "brackets", radio=True),
                item("Speaker Driver Disc (◎ 80)", lambda: self.set_style("audio_disc"), checked=lambda item: self.current_style == "audio_disc", radio=True),
                item("Clean Digits (80)", lambda: self.set_style("clean"), checked=lambda item: self.current_style == "clean", radio=True),
            )),
            item("Refresh Now", self.manual_refresh),
            item("Wired / Charging Mode", self.toggle_wired_mode, checked=lambda item: self.is_wired),
            item("Start with Windows", self.toggle_startup, checked=lambda item: self.check_startup_enabled()),
            pystray.Menu.SEPARATOR,
            item("Exit", self.exit_app)
        )

        self.icon = pystray.Icon("XiaomiSpeakerBattery", init_icon, self._get_status_text(), menu)
        self.icon.run(setup=self.on_setup)


if __name__ == "__main__":
    mutex = ensure_single_instance()
    app = XiaomiBatteryMonitor()
    app.run()

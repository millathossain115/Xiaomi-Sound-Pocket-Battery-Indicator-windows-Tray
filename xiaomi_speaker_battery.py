"""
Xiaomi Sound Pocket / Pocket Speaker 2 Battery Monitor for Windows System Tray
Automated Tray Presence, Native Windows 11 Fluent Horizontal Battery Capsule,
Type-C Wired Charging Detection, and Single-Instance Win32 Mutex.
"""

import os
import sys
import time
import threading
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
    pass

# Patch pystray's win32 implementation for razor-sharp native DPI icon loading
try:
    import pystray._win32 as _pystray_win32

    def _custom_serialized_image(image, format):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=f'.{format.lower()}')
        os.close(fd)
        try:
            image.save(path, format=format)
            with open(path, 'rb') as fp:
                yield path
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass

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

    _pystray_win32.serialized_image = _custom_serialized_image
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

APP_NAME = "Xiaomi Speaker Battery Tray"
MUTEX_NAME = r"Local\Xiaomi_Sound_Pocket_Battery_Monitor_Mutex"
STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_NAME = "XiaomiSpeakerBatteryTray"

# Hardware IDs for Xiaomi Audio
XIAOMI_VID = 0x05D6
XIAOMI_PID = 0x000A


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

    def _get_native_tray_size(self):
        """Query physical taskbar icon metric for exact 1:1 pixel rendering."""
        try:
            sz = user32.GetSystemMetrics(49)  # SM_CXSMICON
            if 16 <= sz <= 64:
                return sz
        except Exception:
            pass
        return 20  # Fallback for standard 125% DPI display

    def check_wired_connection(self):
        """Check if speaker is wired directly to PC via USB Type-C cable."""
        try:
            # Check USB DevNodes for Xiaomi Vendor ID or Device Name
            found_wired = False

            def scan_usb(dn):
                nonlocal found_wired
                if found_wired:
                    return
                # Get device ID
                id_buf = (ctypes.c_wchar * 512)()
                if cfgmgr32.CM_Get_Device_IDW(dn, id_buf, 512, 0) == 0:
                    dev_id = id_buf.value.lower()
                    if "usb" in dev_id and ("vid_05d6" in dev_id or "pocket" in dev_id or "xiaomi" in dev_id):
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
        """Scan Windows DevNode tree for Bluetooth Hands-Free AG battery level & wired status."""
        found_pct = None

        def scan(dn):
            nonlocal found_pct
            if found_pct is not None:
                return
            buf = (ctypes.c_byte * 64)()
            bufSize = wintypes.ULONG(64)
            propType = wintypes.ULONG()
            if cfgmgr32.CM_Get_DevNode_PropertyW(dn, ctypes.byref(bat_key), ctypes.byref(propType), buf, ctypes.byref(bufSize), 0) == 0:
                name_buf = (ctypes.c_wchar * 256)()
                name_size = wintypes.ULONG(512)
                cfgmgr32.CM_Get_DevNode_PropertyW(dn, ctypes.byref(name_key), ctypes.byref(propType), name_buf, ctypes.byref(name_size), 0)
                raw_name = name_buf.value
                if self.target_name.lower() in raw_name.lower():
                    found_pct = int(buf[0])
                    return

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

        if found_pct is not None:
            return True, found_pct, wired
        elif wired:
            # Wired directly without Bluetooth active
            return True, self.battery_pct, True
        return False, None, False

    def _draw_lightning_bolt(self, draw, x, y, scale=1.0, color=(56, 185, 255, 255)):
        """Draw high-contrast geometric lightning bolt."""
        pts = [
            (int(x + 7 * scale), int(y + 0 * scale)),
            (int(x + 0 * scale), int(y + 22 * scale)),
            (int(x + 6 * scale), int(y + 22 * scale)),
            (int(x + 2 * scale), int(y + 44 * scale)),
            (int(x + 15 * scale), int(y + 16 * scale)),
            (int(x + 8 * scale), int(y + 16 * scale)),
        ]
        draw.polygon(pts, fill=color)

    def generate_horizontal_battery_icon(self, percentage, connected, is_wired=False):
        """
        Generate pixel-perfect native resolution icon:
        - Landscape aspect ratio matching authentic horizontal battery proportions
        - Generous horizontal clearance (>15px inner margins) so numbers never crowd
        - Segoe UI Semibold typography with subpixel Lanczos supersampling
        - Vibrant Windows 11 Accent Blue (56, 185, 255) + lightning badge when Type-C wired charging
        """
        scale = 4
        buf_size = self.tray_size * scale
        img = Image.new('RGBA', (buf_size, buf_size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Disconnected fallback (though auto-hidden by default)
        if not connected:
            muted = (140, 145, 155, 255)
            b_top = int(buf_size * 0.17)
            b_bot = int(buf_size * 0.83)
            b_left = int(buf_size * 0.02)
            b_right = int(buf_size * 0.90)
            n_left = b_right
            n_right = int(buf_size * 0.98)
            n_top = int(buf_size * 0.35)
            n_bot = int(buf_size * 0.65)
            sw = max(2, int(scale * 1.5))

            draw.rounded_rectangle([b_left, b_top, b_right, b_bot], radius=int(scale * 3.5), outline=muted, width=sw)
            draw.rounded_rectangle([n_left, n_top, n_right, n_bot], radius=int(scale * 1.2), fill=muted)
            try:
                font = ImageFont.truetype(r"C:\Windows\Fonts\seguisb.ttf", int(scale * 10))
            except Exception:
                font = ImageFont.load_default()
            draw.text(((b_left + b_right) // 2, (b_top + b_bot) // 2), "?", font=font, fill=muted, anchor="mm")
            return img.resize((self.tray_size, self.tray_size), Image.Resampling.LANCZOS)

        # Color palette
        white = (255, 255, 255, 255)
        accent_blue = (56, 185, 255, 255)  # Windows 11 Active Accent Blue
        warning_red = (255, 75, 75, 255)

        if is_wired:
            frame_color = accent_blue
            num_color = white
            bolt_color = accent_blue
        elif percentage <= 15:
            frame_color = warning_red
            num_color = warning_red
            bolt_color = warning_red
        else:
            frame_color = white
            num_color = white
            bolt_color = white

        # 1. Proportional landscape battery capsule dimensions
        # Width occupies 90% of tile, height occupies 66% (sleek horizontal landscape)
        b_top = int(buf_size * 0.17)
        b_bot = int(buf_size * 0.83)
        b_left = max(1, int(buf_size * 0.02))
        b_right = int(buf_size * 0.90)
        n_left = b_right
        n_right = min(buf_size - 1, int(buf_size * 0.98))
        n_top = int(buf_size * 0.35)
        n_bot = int(buf_size * 0.65)
        sw = max(2, int(scale * 1.5))

        draw.rounded_rectangle([b_left, b_top, b_right, b_bot], radius=int(scale * 3.5), outline=frame_color, width=sw)
        draw.rounded_rectangle([n_left, n_top, n_right, n_bot], radius=int(scale * 1.2), fill=frame_color)

        # 2. Typography & badges with generous horizontal space
        font_path = r"C:\Windows\Fonts\seguisb.ttf"
        if not os.path.exists(font_path):
            font_path = r"C:\Windows\Fonts\segoeui.ttf"

        center_x = (b_left + b_right) // 2
        center_y = (b_top + b_bot) // 2

        if is_wired:
            # Display lightning bolt badge + clean digits
            if percentage == 100:
                bolt_x = b_left + int(scale * 1.8)
                bolt_y = center_y - int(scale * 4.8)
                self._draw_lightning_bolt(draw, bolt_x, bolt_y, scale=scale * 0.22, color=bolt_color)
                try:
                    font = ImageFont.truetype(font_path, int(scale * 8.2))
                except Exception:
                    font = ImageFont.load_default()
                draw.text((center_x + int(scale * 2.2), center_y), "100", font=font, fill=num_color, anchor="mm")
            else:
                bolt_x = b_left + int(scale * 2.4)
                bolt_y = center_y - int(scale * 5.0)
                self._draw_lightning_bolt(draw, bolt_x, bolt_y, scale=scale * 0.24, color=bolt_color)
                try:
                    font = ImageFont.truetype(font_path, int(scale * 9.5))
                except Exception:
                    font = ImageFont.load_default()
                draw.text((center_x + int(scale * 2.5), center_y), str(percentage), font=font, fill=num_color, anchor="mm")
        else:
            # Normal battery: Centered numbers with generous horizontal breathing space
            if percentage == 100:
                try:
                    font = ImageFont.truetype(font_path, int(scale * 8.8))
                except Exception:
                    font = ImageFont.load_default()
                draw.text((center_x, center_y), "100", font=font, fill=num_color, anchor="mm")
            else:
                try:
                    font = ImageFont.truetype(font_path, int(scale * 10.8))
                except Exception:
                    font = ImageFont.load_default()
                draw.text((center_x, center_y), str(percentage), font=font, fill=num_color, anchor="mm")

        # Downscale with Lanczos for razor-sharp subpixel edges
        final_icon = img.resize((self.tray_size, self.tray_size), Image.Resampling.LANCZOS)

        # Alpha boost LUT for solid 100% white luminance on display
        r, g_ch, b, a = final_icon.split()
        lut = []
        for i in range(256):
            if i < 35:
                lut.append(0)
            elif i < 110:
                lut.append(int((i - 35) * (180.0 / 75.0)))
            else:
                v = int(180 + (i - 110) * (75.0 / 145.0) * 1.5)
                lut.append(min(255, v))
        a_boosted = a.point(lut)
        return Image.merge('RGBA', (r, g_ch, b, a_boosted))

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
            # Generate new icon
            self.icon.icon = self.generate_horizontal_battery_icon(self.battery_pct, True, self.is_wired)
            self.icon.title = self._get_status_text()
            if not self.icon.visible:
                self.icon.visible = True
        else:
            # Automated taskbar behavior: completely hide when disconnected
            if self.icon.visible:
                self.icon.visible = False

    def poll_loop(self):
        """Continuous background monitor with auto-show / auto-hide automation."""
        while self.running:
            success, pct, wired = self.read_battery()
            was_connected = self.is_connected
            was_wired = self.is_wired

            if success:
                self.is_connected = True
                self.battery_pct = pct
                self.is_wired = wired

                # Notify on fresh connection
                if not was_connected:
                    msg = f"Connected ({pct}%)"
                    if wired:
                        msg += " ⚡ (Wired / Charging)"
                    self.send_notification("Xiaomi Speaker Connected", msg)

                # Low battery alert
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
        # Initial read
        success, pct, wired = self.read_battery()
        if success:
            self.is_connected = True
            self.battery_pct = pct
            self.is_wired = wired
            icon.visible = True
        else:
            self.is_connected = False
            self.is_wired = False
            # When disconnected at boot, stay invisible until speaker turns on
            icon.visible = False

        # Launch polling thread
        worker = threading.Thread(target=self.poll_loop, daemon=True)
        worker.start()

    def run(self):
        # Initial read for startup icon
        success, pct, wired = self.read_battery()
        if success:
            self.is_connected = True
            self.battery_pct = pct
            self.is_wired = wired

        init_icon = self.generate_horizontal_battery_icon(self.battery_pct, self.is_connected, self.is_wired)

        menu = pystray.Menu(
            item(lambda text: self._get_status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
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

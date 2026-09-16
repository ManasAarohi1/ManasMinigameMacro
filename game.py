"""Everything that touches Roblox: settings, its log, keys and mouse, AutoHotkey, the screen,
getting into and out of rounds, rejoining, and the item and aura menus."""
import atexit
import ctypes
import datetime
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import winreg
from ctypes import wintypes
from time import sleep as _sleep

import numpy as np
import psutil
from mousekey import MouseKey
from PIL import Image, ImageGrab
from pynput.keyboard import Controller as KeyboardController, Key
from pynput.mouse import Button, Controller as MouseController

import solver

# ================================================================ settings
APP_DIR = os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"), "ManasMinigameMacro")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
HISTORY_FILE = os.path.join(APP_DIR, "history.jsonl")
STATS_FILE = os.path.join(APP_DIR, "stats.json")
DEFAULTS = {
    "webhook_url": "",
    "cooldown_seconds": 300,
    "rounds": "1",
    "loop": False,            # Continuous loop: rounds until Stop
    "give_up_seconds": "120",
    "auto_items": False,
    "click_mode": False,
    "clicks": {},
    "ahk_path": "",           # AutoHotkey 1.1, if the search does not find it
    "private_server": "",
    "auto_reconnect": False,
    "abyssal_mode": False,    # older setting, read once into speed_mode
    "speed_mode": "",         # nonvip, vip or abyssal
    "low_end": False,
    "rejoin_browser": False,
    "menu_key": "\\",         # Roblox's UI navigation toggle
}


def load_settings():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            cfg.update(stored)
    except (OSError, ValueError):
        pass
    return cfg


def speed_mode(cfg):
    mode = cfg.get('speed_mode')
    if mode in ('nonvip', 'vip', 'abyssal'):
        return mode
    return 'abyssal' if cfg.get('abyssal_mode') else 'vip'


def save_settings(cfg):
    """Merge into what is stored, so one writer cannot drop another's keys."""
    merged = load_settings()
    merged.update(cfg)
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp, CONFIG_FILE)
    return merged


def screenshot(name):
    """Save the Roblox client to the app folder; never raises."""
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        Image.fromarray(grab(client_box((0.0, 0.0, 1.0, 1.0)))).save(os.path.join(APP_DIR, name))
    except Exception:
        pass


# ================================================================ Roblox's log
LOG_DIR = os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"), "Roblox", "logs")
RPC_MARKER = "[BloxstrapRPC]"
_decoder = json.JSONDecoder()


def _presence(line):
    """The `data` of a SetRichPresence RPC line, or None."""
    at = line.find(RPC_MARKER)
    if at < 0:
        return None
    try:
        payload, _ = _decoder.raw_decode(line[at + len(RPC_MARKER):].strip())
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("command") != "SetRichPresence":
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def biome_from_line(line):
    data = _presence(line)
    if data is None:
        return None
    name = (data.get("largeImage") or {}).get("hoverText")
    return name.strip() if isinstance(name, str) and name.strip() else None


def aura_from_line(line):
    """The equipped aura (`Equipped "X"` or bare `Equipped _None_`), or None."""
    data = _presence(line)
    state = data.get("state") if data else None
    m = re.match(r'\s*Equipped\s+(?:"(.*)"|(\S+))\s*$', state) if isinstance(state, str) else None
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else m.group(2)


def time_from_line(line):
    """Epoch seconds from the line's leading timestamp, or None."""
    try:
        stamp = datetime.datetime.strptime(line[:24], "%Y-%m-%dT%H:%M:%S.%fZ")
    except (ValueError, TypeError):
        return None
    return stamp.replace(tzinfo=datetime.timezone.utc).timestamp()


def player_logs(log_dir=LOG_DIR):
    """Client logs, newest first."""
    try:
        names = [os.path.join(log_dir, n) for n in os.listdir(log_dir) if n.endswith(".log") and "_Player_" in n]
    except OSError:
        return []
    return sorted(names, key=os.path.getmtime, reverse=True)


def newest_log(log_dir=LOG_DIR):
    names = player_logs(log_dir)
    return names[0] if names else None


def last_aura(log_dir=LOG_DIR, logs=10):
    """(aura, epoch seconds) from the lowest aura line in the newest log that has one, or None.
    _None_ is skipped: it also shows on the title screen."""
    for path in player_logs(log_dir)[:logs]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            aura = aura_from_line(line) if RPC_MARKER in line else None
            if aura not in (None, "_None_"):
                return aura, time_from_line(line) or os.path.getmtime(path)
    return None


SPAWN_MARK = "Form Shift function:"   # printed each time the character spawns; a finished round respawns it


def spawn_times(log_dir=LOG_DIR, tail=1 << 17):
    """Epoch seconds of the character spawns in the end of the newest log."""
    path = newest_log(log_dir)
    if path is None:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - tail))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return [t for t in (time_from_line(l) for l in text.splitlines() if SPAWN_MARK in l) if t]


def left_round(since, wait=2.0, log_dir=LOG_DIR):
    """Has the character respawned since `since`? True when the log shows no spawns at all (nothing to check)."""
    end = time.time() + wait
    while True:
        times = spawn_times(log_dir)
        if not times or max(times) > since:
            return True
        if time.time() >= end:
            return False
        time.sleep(0.25)


def follow(stop, log_dir=LOG_DIR, poll=1.0):
    """Yield new log lines across client restarts; None on an idle poll."""
    path, handle = None, None
    try:
        while not stop():
            latest = newest_log(log_dir)
            if latest != path and latest is not None:
                first = path is None
                if handle:
                    handle.close()
                path = latest
                handle = open(path, "r", encoding="utf-8", errors="replace")
                if first:       # a client started later is read from the top, so its first biome is not missed
                    handle.seek(0, os.SEEK_END)
            line = handle.readline() if handle else None
            yield line or None
            if not line:
                time.sleep(poll)
    finally:
        if handle:
            handle.close()


# ================================================================ window, keys and mouse
STEP_DELAY = 0.06
SETTLE_S = 0.3           # menus, the reset prompt and a window coming to the front need a moment
ROUND_EXIT_S = 2.5       # after a round ends, before a reset: keys sent while leaving the minigame are lost
PACE = 1.0               # every paced wait is multiplied by this; Potato PC raises it
ROBLOX_EXE = 'robloxplayerbeta.exe'

u32 = ctypes.WinDLL('user32', use_last_error=True)
KEYUP, SCANCODE = 0x0002, 0x0008
VK = {'w': 0x57, 'a': 0x41, 's': 0x53, 'd': 0x44, 'q': 0x51, 'e': 0x45, 'f': 0x46,
      ' ': 0x20, 'shift': 0xA0, 'ctrl': 0xA2, 'r': 0x52, 'enter': 0x0D, 'esc': 0x1B,
      '\\': 0xDC, 'oem5': 0xDC}
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KI(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MI(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class IN(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KI), ("mi", MI)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
u32.GetWindowThreadProcessId.restype = wintypes.DWORD
u32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
u32.GetKeyboardLayout.restype = wintypes.HKL
u32.MapVirtualKeyExW.argtypes = [wintypes.UINT, wintypes.UINT, wintypes.HKL]
u32.MapVirtualKeyExW.restype = wintypes.UINT


class NotFocused(RuntimeError):
    pass


def paced(s):
    _sleep(s * PACE)


def slower():
    global PACE
    PACE = min(3.0, PACE + 0.5)
    print('   timings slowed to x%.1f' % PACE)


MENU_KEY = 'ui navigation'   # stands for the key set in the window; tap() presses it
SETTLE = object()
MENU_SEQUENCE = ([MENU_KEY, SETTLE] + [Key.up, Key.right] * 4 + [Key.up] * 2 + [Key.down] * 4
                 + [Key.left] * 4 + [Key.up] * 4 + [Key.enter, SETTLE] + [Key.up] * 10
                 + [Key.enter, SETTLE] + [MENU_KEY])

kb = KeyboardController()
mouse = MouseController()
mkey = MouseKey()


def _layout():
    """The keyboard layout of the window in front, so W is the key that types W there."""
    try:
        return u32.GetKeyboardLayout(u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), None))
    except Exception:
        return 0


def in_front():
    try:
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(pid))
        return psutil.Process(pid.value).name().lower() == ROBLOX_EXE
    except Exception:
        return False


def wait_for_front(grace=3.0):
    end = time.monotonic() + grace
    while not in_front():
        if time.monotonic() > end:
            raise NotFocused('Roblox is not the window in front; key not sent')
        time.sleep(0.05)


def _send(vk, flags):
    if not flags & KEYUP:
        wait_for_front()
    sc = u32.MapVirtualKeyExW(vk, 0, _layout()) or u32.MapVirtualKeyW(vk, 0)
    inp = IN(type=1, ki=KI(wVk=0, wScan=sc, dwFlags=flags | SCANCODE, time=0, dwExtraInfo=0))
    if not u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp)):
        raise ctypes.WinError(ctypes.get_last_error())


_down = set()


def down(key):
    _send(VK[key], 0)
    _down.add(key)


def up(key):
    _send(VK[key], KEYUP)
    _down.discard(key)


def release_all(focus=True):
    """Every key up, into Roblox if something is held."""
    if focus and _down:
        try:
            focus_roblox(verify=False)
            time.sleep(0.15)
        except Exception:
            pass
    for vk in VK.values():
        try:
            _send(vk, KEYUP)
        except OSError:
            pass
        time.sleep(0.005)
    _down.clear()


_armed = False
_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def arm():
    """Release every key however the program ends."""
    global _armed
    if _armed:
        return
    atexit.register(release_all)
    for s in _SIGNALS:
        try:
            signal.signal(s, lambda *a: (release_all(), sys.exit(1)))
        except (ValueError, OSError):
            pass
    _armed = True


def disarm():
    global _armed
    _armed = False
    try:
        atexit.unregister(release_all)
    except Exception:
        pass
    for s in _SIGNALS:
        try:
            signal.signal(s, signal.SIG_DFL)
        except (ValueError, OSError):
            pass


def tap(key, hold=None, gap=None):
    """One key press; MENU_KEY is the UI navigation key set in the window."""
    if key == MENU_KEY:
        key = menu_press_key()
    wait_for_front()
    kb.press(key)
    paced(0.03 if hold is None else hold)
    kb.release(key)
    paced(STEP_DELAY if gap is None else gap)


def move_to(x, y):
    """Absolute pointer move in screen pixels, on any monitor."""
    vx, vy, vw, vh = (ctypes.windll.user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    mi = MI(dx=round((x - vx) * 65535 / max(1, vw - 1)), dy=round((y - vy) * 65535 / max(1, vh - 1)),
            mouseData=0, dwFlags=0x0001 | 0x8000 | 0x4000, time=0, dwExtraInfo=0)
    inp = IN(type=0, mi=mi)
    u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def client_rect(window):
    """(left, top, width, height) of the client area on screen."""
    left, right, top, bottom = window.coords_client
    p = wintypes.POINT(0, 0)
    ctypes.windll.user32.ClientToScreen(window.hwnd, ctypes.byref(p))
    return p.x, p.y, right - left, bottom - top


def find_roblox_window():
    pids = set()
    for process in psutil.process_iter(['name']):
        try:
            if 'roblox' in process.info['name'].lower():
                pids.add(process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    windows = mkey.get_all_windows()
    big = lambda w: min(w.dim_win) >= 200 or _minimised(w)
    candidates = [w for w in windows if w.pid in pids and w.status == 'visible' and big(w)]
    if not candidates:
        candidates = [w for w in windows if w.status == 'visible' and (w.title or '').strip() == 'Roblox' and big(w)]
    if not candidates:
        return None
    exact = [w for w in candidates if w.class_name == 'WINDOWSCLIENT']
    return max(exact or candidates, key=lambda w: w.dim_win[0] * w.dim_win[1])


def _minimised(w):
    try:
        return bool(ctypes.windll.user32.IsIconic(w.hwnd))
    except Exception:
        return False


def foreground_hwnd():
    return ctypes.windll.user32.GetForegroundWindow()


def force_foreground(hwnd):
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    current = kernel32.GetCurrentThreadId()
    target = user32.GetWindowThreadProcessId(hwnd, None)
    active = user32.GetWindowThreadProcessId(foreground_hwnd(), None)
    attached = {t for t in (target, active) if t and t != current}
    for thread_id in attached:
        user32.AttachThreadInput(current, thread_id, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for thread_id in attached:
            user32.AttachThreadInput(current, thread_id, False)
    return foreground_hwnd() == hwnd


def focus_roblox(verify=True):
    window = find_roblox_window()
    if window is None:
        raise RuntimeError('No Roblox game window. Is it running and not minimised?')
    if foreground_hwnd() == window.hwnd:
        paced(SETTLE_S)
        return window
    force_foreground(window.hwnd)
    paced(SETTLE_S)
    if verify and foreground_hwnd() != window.hwnd:
        raise RuntimeError('Roblox would not come forward (foreground is %s, wanted %s)'
                           % (foreground_hwnd(), window.hwnd))
    return window


def take_foreground(tries=5, gap=0.35):
    """Get Roblox in front, retrying; the window, or None."""
    last = None
    for _ in range(max(1, tries)):
        try:
            return focus_roblox(verify=True)
        except RuntimeError as e:
            last = e
            paced(gap)
    print('   Roblox would not come to the front: %s' % last)
    return None


def steady_front(hold=0.6, tries=5):
    """Roblox in front and still in front hold seconds later; retakes it when it slips. False if it never stays."""
    for _ in range(max(1, tries)):
        if take_foreground(tries=2) is None:
            continue
        end = time.monotonic() + hold * PACE
        while in_front():
            if time.monotonic() > end:
                return True
            time.sleep(0.05)
    return False


def move_camera():
    window = find_roblox_window()
    if window is None:
        width, height = mkey.get_screen_resolution()
        left = top = 0
    else:
        left, top, width, height = client_rect(window)
    x = round(left + width * 700 / 1920)
    mkey.move_to(x, round(top + height * 200 / 1080))
    paced(STEP_DELAY)
    mouse.press(Button.right)
    paced(STEP_DELAY)
    mkey.move_to(x, round(top + height * 900 / 1080))
    paced(STEP_DELAY)
    mouse.release(Button.right)
    paced(STEP_DELAY)
    mouse.scroll(0, 300)
    paced(STEP_DELAY)
    mouse.scroll(0, -75)


def respawn():
    """Always Give up first, then Esc R Enter."""
    if give_up():
        paced(ROUND_EXIT_S)
    tap(Key.esc, gap=SETTLE_S)
    tap('r', gap=SETTLE_S)
    tap(Key.enter, gap=SETTLE_S)


def align_camera(stop=None):
    """Respawn, set the camera through the settings menu, then drag and zoom."""
    halt = lambda: bool(stop and stop())
    if halt():
        return False
    focus_roblox()
    if halt():
        return False
    respawn()
    paced(STEP_DELAY)
    if halt():
        return False
    for step in MENU_SEQUENCE:
        if step is SETTLE:
            paced(SETTLE_S)
        else:
            tap(step)
    paced(STEP_DELAY)
    if halt():
        return False
    move_camera()
    return True


# ================================================================ AutoHotkey
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
RELEASE = ('w', 'a', 's', 'd', 'q', 'e', 'f', 'Space')
FOCUS_WAIT = 5           # s a script waits for Roblox to come forward before refusing to walk
AHK_EXES = ('AutoHotkeyU64.exe', 'AutoHotkeyU32.exe', 'AutoHotkeyA32.exe', 'AutoHotkey.exe')


def _version(path):
    m = re.search(r'v?(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?', path)
    return tuple(int(g) for g in m.groups(default='0')) if m else (0,)


def _roots():
    """Folders AutoHotkey may be in: the usual installs, the registry, package managers, PATH."""
    env = os.environ.get
    out = [os.path.join(base, 'AutoHotkey') for base in
           (env('PROGRAMFILES'), env('PROGRAMFILES(X86)'), env('PROGRAMW6432'),
            os.path.join(env('LOCALAPPDATA', ''), 'Programs'), env('APPDATA')) if base]
    try:
        def value(hive, key, name, view):
            try:
                with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as k:
                    return winreg.QueryValueEx(k, name)[0]
            except OSError:
                return None

        found = []
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                found.append(value(hive, r'SOFTWARE\AutoHotkey', 'InstallDir', view))
                u = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\AutoHotkey'
                found += [value(hive, u, 'InstallLocation', view), value(hive, u, 'DisplayIcon', view)]
                for exe in AHK_EXES:
                    found.append(value(hive, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s' % exe, '', view))
                progid = value(hive, r'SOFTWARE\Classes\.ahk', '', view)
                for pid in {'AutoHotkeyScript', progid if isinstance(progid, str) and progid else 'AutoHotkeyScript'}:
                    for verb in ('Open', 'Run'):
                        found.append(value(hive, r'SOFTWARE\Classes\%s\Shell\%s\Command' % (pid, verb), '', view))
        for s in found:
            if not isinstance(s, str) or not s.strip():
                continue
            m = re.search(r'([A-Za-z]:\\[^"<>|*?]*?\.exe)', s, re.I)
            d = os.path.dirname(m.group(1)) if m else s.strip().strip('"')
            out.append(d)
            if os.path.basename(d).lower() in ('ux', 'v2') or _version(os.path.basename(d)) != (0,):
                out.append(os.path.dirname(d))
    except ImportError:
        pass
    home = os.path.expanduser('~')
    out += [os.path.join(home, 'scoop', 'apps', 'autohotkey', 'current'),
            os.path.join(env('PROGRAMDATA', ''), 'chocolatey', 'lib', 'autohotkey', 'tools'),
            os.path.join(env('PROGRAMDATA', ''), 'chocolatey', 'lib', 'autohotkey.portable', 'tools')]
    out += [part for part in env('PATH', '').split(os.pathsep) if part and 'autohotkey' in part.lower()]
    seen, roots = set(), []
    for p in out:
        key = os.path.normcase(os.path.abspath(p)) if p else ''
        if key and key not in seen and os.path.isdir(p):
            seen.add(key)
            roots.append(p)
    return roots


def _scan(base, match, depth=3):
    top = base.rstrip('\\/').count(os.sep)
    for root, dirs, files in os.walk(base):
        if root.count(os.sep) - top >= depth:
            dirs[:] = []
        for f in files:
            if match(f.lower()):
                yield os.path.join(root, f)


def interpreters():
    """Every AutoHotkey 1.1 on this machine, newest first."""
    names = {n.lower() for n in AHK_EXES}
    seen, out = set(), []
    for p in (h for h in (shutil.which(n) for n in AHK_EXES) if h):
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            if not _is_v2(p):
                out.append(p)
    for base in _roots():
        for p in _scan(base, names.__contains__):
            key = os.path.normcase(os.path.abspath(p))
            if key not in seen:
                seen.add(key)
                if not _is_v2(p):
                    out.append(p)
    order = [n.lower() for n in AHK_EXES]
    out.sort(key=lambda p: (file_version(p) or _version(p), -order.index(os.path.basename(p).lower())), reverse=True)
    return out


def _is_v2(path):
    ver = file_version(path)
    if ver and ver[0]:
        return ver[0] == 2
    return bool(re.search(r'[\\/]v?2\.', path) or re.search(r'[\\/]v2[\\/]', path))


def file_version(path):
    """The exe's own version tuple, or None."""
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf)
        val, length = ctypes.c_void_p(), ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buf, u'\\', ctypes.byref(val), ctypes.byref(length)):
            return None
        ffi = ctypes.cast(val, ctypes.POINTER(ctypes.c_uint * 4)).contents
        return ffi[2] >> 16, ffi[2] & 0xFFFF, ffi[3] >> 16, ffi[3] & 0xFFFF
    except Exception:
        return None


def other_versions():
    """AutoHotkey installs that are not 1.1."""
    seen, out = set(), []
    for base in _roots():
        for p in _scan(base, lambda f: f.startswith('autohotkey') and f.endswith('.exe') and 'ux' not in f):
            key = os.path.normcase(os.path.abspath(p))
            if key not in seen:
                seen.add(key)
                if _is_v2(p):
                    out.append(p)
    return out


def interpreter():
    """The settings' ahk_path if it is 1.1, else the best one found, else None."""
    try:
        chosen = (load_settings().get('ahk_path') or '').strip()
        if chosen and os.path.isfile(chosen) and not _is_v2(chosen):
            return chosen
    except Exception:
        pass
    found = interpreters()
    return found[0] if found else None


def walk_body(name, text=None):
    """The inside of a walk's RunPath()."""
    text = solver.walk_text(name) if text is None else text
    m = re.search(r'RunPath\(\)\s*\{', text)
    if not m:
        raise ValueError('%s has no RunPath()' % name)
    depth, out = 1, []
    for line in text[m.end():].splitlines():
        depth += line.count('{') - line.count('}')
        if depth <= 0:
            break
        out.append(line)
    if not out:
        raise ValueError('%s has an empty RunPath()' % name)
    return '\n'.join(out)


def walk_script(name, marker=None, text=None):
    """The walk as a script that runs at once, only with Roblox in front, and stops if it loses focus."""
    release = 'for i, k in ["%s"]' % '", "'.join(RELEASE)
    return '\n'.join([
        '#NoEnv', '#SingleInstance Force',
        'SendMode Input', 'SetKeyDelay, -1, -1', 'SetBatchLines, -1',
        'DetectHiddenWindows, Off',
        'WinActivate, ahk_exe RobloxPlayerBeta.exe',
        'WinWaitActive, ahk_exe RobloxPlayerBeta.exe,, %d' % FOCUS_WAIT,
        'if ErrorLevel',
        '    ExitApp, 2',
        'Sleep, 400',
        'SetTimer, FocusGuard, 100',
        ('FileAppend, go, %s' % marker) if marker else '',
        walk_body(name, text),
        release,
        '    Send, {%k% Up}',
        'ExitApp, 0',
        '',
        'FocusGuard:',
        'IfWinNotActive, ahk_exe RobloxPlayerBeta.exe',
        '{',
        '    ' + release,
        '        Send, {%k% Up}',
        '    ExitApp, 3',
        '}',
        'return',
    ])


def menu_press_key(key=None):
    """The UI navigation key set in the window, as pynput presses it: a character, or a key name like Tab."""
    key = str(load_settings().get('menu_key') if key is None else key).strip() or '\\'
    return key if len(key) == 1 else getattr(Key, key.lower(), key)


WALK_PREFIX = 'path_'


def our_walks():
    """Every process running a walk script this program wrote, from this run or an earlier one."""
    tmp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    out = []
    for p in psutil.process_iter(['cmdline']):
        try:
            for arg in p.info['cmdline'] or ():
                arg = os.path.normcase(os.path.abspath(arg))
                if (arg.endswith('.ahk') and os.path.dirname(arg) == tmp
                        and os.path.basename(arg).startswith(WALK_PREFIX)):
                    out.append(p)
                    break
        except (psutil.Error, ValueError, OSError):
            continue
    return out


def kill_walks():
    """Kill leftover walks; other AutoHotkey scripts are left alone."""
    for p in our_walks():
        try:
            p.kill()
        except psutil.Error:
            pass


def start_walk(name, text=None):
    """Begin walking (after killing any walk still running); returns the process."""
    exe = interpreter()
    if not exe:
        raise RuntimeError('AutoHotkey v1 not found; the paths are v1 syntax')
    kill_walks()
    mark = os.path.join(tempfile.gettempdir(), 'ahk_go_%d.txt' % os.getpid())
    if os.path.exists(mark):
        os.remove(mark)
    fd, tmp = tempfile.mkstemp(suffix='.ahk', prefix=WALK_PREFIX, dir=tempfile.gettempdir())
    with os.fdopen(fd, 'w') as f:
        f.write(walk_script(name, mark, text))
    proc = subprocess.Popen([exe, '/f', tmp], creationflags=NO_WINDOW)
    proc._tmp, proc._mark = tmp, mark
    return proc


def walking(proc, timeout=FOCUS_WAIT + 3.0):
    """Block until the script says it has set off. True if it did."""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        if os.path.exists(proc._mark):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.02)
    return False


def stop_walk(proc):
    """Kill the walk and let go of every key."""
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        kill_walks()
        release_all()
        for f in (proc._tmp, getattr(proc, '_mark', None)):
            try:
                os.remove(f)
            except (OSError, TypeError):
                pass


def stop_all(focus=True):
    """Kill every walk this program started, and let go of the keys."""
    kill_walks()
    release_all(focus=focus)


def run_walk(name, timeout=None):
    """Walk the whole path and wait; None if it overran."""
    proc = start_walk(name)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    finally:
        stop_walk(proc)


# ================================================================ the screen
HINT_BOX = (0.332, 0.125, 0.684, 0.174)
TEXT_HUE_MAX = 85        # the green end of the six phrases
TEXT_HUE_MIN = 168       # the red end, wrapping past 180
MIN_FRACTION = 0.009     # less of the strip than this is not a phrase
MAX_FRACTION = 0.06      # more is scenery
CUTS = [1, 10, 21, 31, 52]
INVENTORY_TITLE = (0.46, 0.262, 0.54, 0.29)
INVENTORY_X = (0.7285, 0.2639, 0.7426, 0.2889)


def client_box(fractions):
    """A rectangle given as fractions of the Roblox client, in screen pixels."""
    try:
        w = find_roblox_window()
    except Exception:
        w = None
    if w is None:
        cw, ch, x0, y0 = 2560, 1440, 0, 0
    else:
        x0, y0, cw, ch = client_rect(w)
    l, t, r, b = fractions
    box = (int(x0 + l * cw), int(y0 + t * ch), int(x0 + r * cw), int(y0 + b * ch))
    return box[0], box[1], max(box[0] + 1, box[2]), max(box[1] + 1, box[3])


def area_scale():
    l, t, r, b = client_box((0.0, 0.0, 1.0, 1.0))
    return max(0.05, (r - l) * (b - t) / (2560.0 * 1440.0))


def grab(box):
    """A screen rectangle as RGB; dark if the grab fails."""
    try:
        return np.asarray(ImageGrab.grab(bbox=box, all_screens=True).convert('RGB'))
    except Exception:
        return np.zeros((max(1, box[3] - box[1]), max(1, box[2] - box[0]), 3), np.uint8)


def hsv(rgb):
    """H 0..179, S and V 0..255."""
    a = rgb.astype(np.int32)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    v = a.max(axis=-1)
    c = v - a.min(axis=-1)
    safe = np.where(c == 0, 1, c)
    h = np.where(v == r, 30 * (g - b) / safe,
                 np.where(v == g, 60 + 30 * (b - r) / safe, 120 + 30 * (r - g) / safe))
    h = np.where(c == 0, 0, h) % 180
    s = np.where(v == 0, 0, 255 * c / np.where(v == 0, 1, v))
    return h, s, v


def blobs(rgb, colour, tol, min_area=1, origin=(0, 0)):
    """Patches of one colour as (x, y, area) in screen pixels, biggest first."""
    m = np.abs(rgb.astype(np.int16) - np.asarray(colour, np.int16)).max(-1) <= tol
    return _group(m, origin, 8, 40, min_area)


def red_blobs(rgb, origin, sat, val, min_area):
    h, s, v = hsv(rgb)
    return _group(((h <= 8) | (h >= 172)) & (s >= sat) & (v >= val), origin, 8, 40, min_area)


def hues(rgb=None):
    """Median hue of the hint phrase and how many pixels voted; (None, n) if unreadable."""
    if rgb is None:
        rgb = grab(client_box(HINT_BOX))
    h, s, v = hsv(rgb)
    texty = (s > 120) & (v > 120) & ((h <= TEXT_HUE_MAX) | (h >= TEXT_HUE_MIN))
    n = int(texty.sum())
    if not (texty.size * MIN_FRACTION <= n <= texty.size * MAX_FRACTION):
        return None, n
    hh = h[texty].astype(float)
    hh[hh > 150] -= 180.0
    return float(np.median(hh)), n


def tier(hue):
    """0 = farthest phrase, 5 = closest."""
    return int(np.searchsorted(CUTS, hue))


def inventory_open():
    def shares(box):
        g = grab(client_box(box)).mean(axis=2)
        return (g < 30).mean(), (g > 200).mean()

    td, tb = shares(INVENTORY_TITLE)
    xd, xb = shares(INVENTORY_X)
    return bool(td >= 0.6 and 0.08 <= tb <= 0.45 and xd >= 0.5 and 0.05 <= xb <= 0.45)


def _group(mask, origin, row_gap, col_gap, min_area):
    out = []
    rows = np.flatnonzero(mask.any(axis=1))
    if not rows.size:
        return out
    for r0, r1 in runs(rows, row_gap):
        band = mask[r0:r1 + 1]
        for c0, c1 in runs(np.flatnonzero(band.any(axis=0)), col_gap):
            area = int(band[:, c0:c1 + 1].sum())
            if area >= min_area:
                ys, xs = np.nonzero(band[:, c0:c1 + 1])
                out.append((origin[0] + c0 + xs.mean(), origin[1] + r0 + ys.mean(), area))
    out.sort(key=lambda b: -b[2])
    return out


def runs(idx, gap):
    """Split sorted indices wherever they jump by more than gap."""
    cuts = np.flatnonzero(np.diff(idx) > gap)
    start = 0
    for c in cuts:
        yield int(idx[start]), int(idx[c])
        start = c + 1
    yield int(idx[start]), int(idx[-1])


class Motion:
    """Is the world still scrolling past? Unsure, too dark, or a flash all count as moving."""
    BOX = (0.06, 0.42, 0.94, 0.62)
    DOWN = 8
    DECAY = 0.96
    STILL = 0.30
    FLOOR = 0.8
    QUIET = 3
    DARK = 12.0
    FLASH = 3.0

    def __init__(self):
        self.forget()

    def forget(self):
        self.box, self.prev, self.loud, self.quiet, self.bright = None, None, 0.0, 0, None

    def frame(self):
        if self.box is None:
            self.box = client_box(self.BOX)
        a = grab(self.box)[::self.DOWN, ::self.DOWN].astype(np.int16)
        n = a.shape[1] // 3
        return np.concatenate((a[:, :n], a[:, -n:]), axis=1)

    def moving(self):
        f = self.frame()
        bright = float(f.mean())
        if self.prev is None or f.shape != self.prev.shape:
            self.prev, self.quiet, self.bright = f, 0, bright
            return True
        d = float(np.abs(f - self.prev).mean())
        last = self.bright if self.bright is not None else bright
        self.prev, self.bright = f, bright
        if bright < self.DARK or max(bright, last) > self.FLASH * max(min(bright, last), 1.0):
            self.quiet = 0
            return True
        self.loud = max(d, self.loud * self.DECAY)
        if self.loud < self.FLOOR:
            return True
        self.quiet = self.quiet + 1 if d < self.loud * self.STILL else 0
        return self.quiet < self.QUIET


# ================================================================ getting into a round, and giving up
MINIGAME_AHK = 'lime_path.ahk'
BUTTON_RGB = (170, 170, 255)
TOL = 3
WIDE_TOL = 14
MIN_AREA = 1500
BUTTON_BOX = (0.234, 0.750, 0.859, 0.972)
WAIT_BUTTON = 4.5
POLL = 0.12
AFTER_TICKET = 3.0
TRIES = 3
GIVEUP_BOX = (0.352, 0.174, 0.664, 0.299)
GIVEUP_SETTLE = 1.0      # s after pressing Give up before anything else
GIVEUP_WAIT = 8.0
GIVEUP_LOOK = 1.0
GIVEUP_GONE = 6          # reads in a row without the button before the round counts as over
GIVEUP_MIN_AREA = 300
GIVEUP_SPOT = (0.5, 0.193)   # where Give up sits in the client, pressed even when it isn't seen


def buttons():
    """Dialogue button patches, biggest first."""
    b = client_box(BUTTON_BOX)
    rgb = grab(b)
    area = MIN_AREA * area_scale()
    return blobs(rgb, BUTTON_RGB, TOL, area, (b[0], b[1])) or blobs(rgb, BUTTON_RGB, WIDE_TOL, area, (b[0], b[1]))


def move_path(start, end, jitter=3.0):
    """Pointer path along a bowed, eased arc to a point near the target."""
    sx, sy = start
    tx = end[0] + random.uniform(-jitter, jitter)
    ty = end[1] + random.uniform(-jitter, jitter)
    dist = math.hypot(tx - sx, ty - sy)
    steps = max(8, min(40, int(dist / 18)))
    bow = random.uniform(-1, 1) * dist * 0.06
    nx, ny = (-(ty - sy) / dist, (tx - sx) / dist) if dist else (0.0, 0.0)
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        k = math.sin(math.pi * t) * bow
        out.append((sx + (tx - sx) * e + nx * k + random.uniform(-0.6, 0.6),
                    sy + (ty - sy) * e + ny * k + random.uniform(-0.6, 0.6)))
    out[-1] = (tx, ty)
    return out


def click(x, y, park=True):
    """Focus Roblox, move there, click. Not sent at all if Roblox will not come forward."""
    if take_foreground() is None:
        return False
    time.sleep(random.uniform(0.15, 0.30))
    for px, py in move_path(mouse.position, (x, y)):
        move_to(int(round(px)), int(round(py)))
        time.sleep(random.uniform(0.006, 0.018))
    time.sleep(random.uniform(0.06, 0.18) * PACE)
    mouse.press(Button.left)
    time.sleep(random.uniform(0.06, 0.12) * PACE)
    mouse.release(Button.left)
    if park:
        time.sleep(0.3 * PACE)
        move_to(int(x + random.uniform(-40, 40)), int(y + random.uniform(140, 200)))
    return True


def wait_for(what, prev=None, timeout=WAIT_BUTTON):
    """The next dialogue button once it is fully drawn (and not the one just clicked), or None."""
    t0 = time.perf_counter()
    timeout *= PACE
    while time.perf_counter() - t0 < timeout:
        found = buttons()
        b = found[0] if found else None
        if b and (prev is None or math.hypot(b[0] - prev[0], b[1] - prev[1]) > 25 or abs(b[2] - prev[2]) > 200):
            print('   %s after %.1fs' % (what, time.perf_counter() - t0))
            return b
        time.sleep(POLL)
    return None


def walk_to_npc():
    if take_foreground() is None:
        return False
    paced(0.3)
    code = run_walk(MINIGAME_AHK, timeout=30)
    if code != 0:
        print('   the walk to the NPC did not run (%s)' % ('timed out' if code is None else 'exit %s' % code))
    return code == 0


def enter_round(ready=None):
    """From anywhere to inside a round. Respawns and retries when Lime does not answer.
    `ready` is asked before every try; False calls the entry off."""
    stop = solver.out_of_time
    bot = solver.Bot(quiet=True)
    for n in range(TRIES):
        if stop() or (ready is not None and not ready()):
            return False
        if n:
            slower()
            print('no dialogue -- closing menus, respawning, realigning and retrying (%d of %d)' % (n + 1, TRIES))
            close_guis()
        if not reset_and_align(stop, align=bool(n)):     # a retry means the walk failed: align again
            return False
        paced(0.5)
        print('walking to the NPC')
        if not walk_to_npc():
            continue
        print('talking to it')
        bot.tap('e')
        if talk_to_lime(bot):
            if close_inventory() and inventory_open():
                close_guis()
            return True
    screenshot('missed_entry.png')
    print('   could not start a round after %d tries' % TRIES)
    return False


def talk_to_lime(bot):
    """Pick [Minigame] then the ticket: click mode's calibrated spots if both are set, else found by colour."""
    points = load_points()
    if all(name in points for name, _ in LIME_POINTS) and click_mode_on():
        paced(LIME_WAIT_S)
        press_point('minigame', settle=1.0)
        press_point('ticket', settle=0.3)
        return entered()
    first = wait_for('[Minigame]')
    if not first:
        bot.tap('e')
        first = wait_for('[Minigame]', timeout=2.5)
    if not first:
        print('   never saw the dialogue')
        return False
    click(first[0], first[1])
    paced(0.2)
    second = wait_for('[-1 Minigame Ticket]', prev=first)
    if not second:
        print('   the ticket prompt never came up')
        return False
    click(second[0], second[1])
    entered()
    return True


def entered():
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < AFTER_TICKET * 2 * PACE:
        if in_round():
            time.sleep(0.3)
            return True
        time.sleep(POLL)
    return False


def close_guis():
    """Open and close a menu with UI navigation, which shuts any GUI left open. Never stops half way."""
    if solver.ABORT or not steady_front():
        return False
    print('   closing any open menu with UI navigation')
    for step in CLOSE_GUIS:
        if step is SETTLE:
            paced(SETTLE_S)
        else:
            tap(step)
    paced(SETTLE_S)
    return True


LIME_POINTS = (('minigame', '[Minigame] button'), ('ticket', '[-1 Minigame Ticket]'))
LIME_WAIT_S = 1.5        # after E, before the calibrated [Minigame] click
CLOSE_GUIS = ([MENU_KEY, SETTLE] + [Key.up, Key.right] * 4 + [Key.up] * 2 + [Key.left] * 2
              + [Key.enter, SETTLE, Key.enter, SETTLE, MENU_KEY])


ALIGNED = False          # the camera was set this run; later entries only respawn


def reset_and_align(stop, align=True):
    """Respawn (which gives up first) and align the camera, or only respawn once it has been aligned."""
    global ALIGNED
    if not align and ALIGNED:
        if stop():
            return False
        focus_roblox()
        respawn()
        paced(STEP_DELAY)
        return True
    ALIGNED = click_align(stop=stop) if click_mode_on() else align_camera(stop=stop)
    return ALIGNED


def find_give_up():
    """(x, y, area) of the red Give up button under the hint, or None."""
    g = client_box(GIVEUP_BOX)
    c = client_box((0, 0, 1, 1))
    cw, ch = c[2] - c[0], c[3] - c[1]
    found = red_blobs(grab(g), (g[0], g[1]), sat=80, val=70, min_area=GIVEUP_MIN_AREA * area_scale())
    for x, y, area in found:
        if abs((x - c[0]) / cw - 0.5) < 0.05 and abs((y - c[1]) / ch - 0.193) < 0.04:
            return x, y, area
    return None


def in_round():
    return find_give_up() is not None


def focused():
    try:
        w = find_roblox_window()
        return w is not None and foreground_hwnd() == w.hwnd
    except Exception:
        return True


def round_over():
    """The button stayed gone for several reads with Roblox in front."""
    for _ in range(GIVEUP_GONE):
        if not focused() or find_give_up() is not None:
            return False
        time.sleep(POLL)
    return True


def give_up():
    """Press Give up until the round is confirmed over. False if it could not be."""
    pressed = False
    now = time.perf_counter()
    look, end = now + GIVEUP_LOOK * PACE, now + GIVEUP_WAIT * PACE
    while time.perf_counter() < end:
        found = find_give_up()
        if found is None:
            if pressed and round_over():
                return True
            if not pressed and time.perf_counter() > look:
                c = client_box((0, 0, 1, 1))
                print('   no Give up button seen - pressing its spot anyway')
                click(int(c[0] + GIVEUP_SPOT[0] * (c[2] - c[0])), int(c[1] + GIVEUP_SPOT[1] * (c[3] - c[1])))
                time.sleep(GIVEUP_SETTLE * PACE)
                return False
            time.sleep(POLL)
            continue
        print('   pressing Give up')
        if not click(found[0], found[1]):
            take_foreground()
            time.sleep(0.5)
            continue
        pressed = True
        time.sleep(GIVEUP_SETTLE * PACE)
    print('   the round is still on after Give up' if pressed else '   no Give up button found')
    return False


def close_inventory():
    """Close the Inventory if the camera align opened it. True if it was open."""
    if take_foreground() is None or not inventory_open():
        return False
    b = client_box(INVENTORY_X)
    print('   the Inventory is open -- closing it')
    for _ in range(3):
        click((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        time.sleep(0.5)
        if not inventory_open():
            return True
    print('   the Inventory would not close')
    return True


# ================================================================ staying connected
QUIET_S = 240.0          # no new log line for this long: frozen
JOIN_S = 150.0
READY_S = 90.0
JOIN_SETTLE_S = 8.0
PLAY_POINT = (("play", "Play button on the title screen"),)
PLAY_PRESSES = 2        # Play clicks (found or calibrated) before the game counts as started
PLAY_AFTER_S = 45.0     # after opening the private server, press the calibrated Play button this long later
CLOSED_SETTLE_S = 2.0   # after Roblox has fully closed, before the deeplink opens a new one
PLAY_BOX = (0.0, 0.72, 0.4, 1.0)     # the title screen puts Play at the bottom left
PLAY_RGB = (0x7e, 0xff, 0x91)        # its green
PLAY_TOL = 30                        # red and blue may be off by this much at the green's brightness
LOST = ('Lost connection with reason', 'Client has been disconnected with reason',
        'Disconnection Notification', 'ID_CONNECTION_LOST', 'leaveUGCGameInternal')
JOINED = ('Connection accepted from', 'Replicator created for player')
GAME_OUTPUT = '[FLog::Creator'
FROZEN = 'frozen'
REJOIN_GRACE_S = 30.0   # the game's own auto rejoin gets this long before the macro opens the server
_GAME = tuple(re.compile(r'https?://(?:www\.)?roblox\.com/%s/(\d+)(?:/[^?\s]*)?'
                         r'(\?privateServerLinkCode=([a-zA-Z0-9]+))?' % kind, re.I)
              for kind in ('games', 'game-places'))
_SHARE = re.compile(r'https?://(?:www\.)?roblox\.com/share\?code=([a-f0-9]{32})&type=Server', re.I)


def deeplink(text):
    """The roblox:// link for a game or private server link, or None."""
    text = text or ''
    for pattern in _GAME:
        m = pattern.search(text)
        if m:
            if m.group(3):
                return 'roblox://placeId=%s&linkCode=%s' % (m.group(1), m.group(3))
            return 'roblox://placeId=%s' % m.group(1)
    m = _SHARE.search(text)
    if m:
        return 'roblox://navigation/share_links?code=%s&type=Server' % m.group(1)
    return None


def _lost_reason(line):
    m = re.search(r'reason\s*:?\s*(.+)$', line.strip(), re.I)
    return (m.group(1) if m else line.strip())[-120:]


class Watch:
    """Follows the newest client log. `state` is 'joined', 'lost' or None."""

    def __init__(self, log_dir=None):
        self.log_dir = log_dir or LOG_DIR
        self.path, self.pos, self.tail = None, 0, ''
        self.state, self.why = None, ''
        self.aura = None                      # last equipped aura the log reported
        self.aura_since = None
        self.in_menu = False                  # on the title screen
        self.fresh = time.time()              # when the log last grew
        self._lock = threading.RLock()        # the connection thread and the round both poll

    def poll(self, now=None):
        with self._lock:
            return self._poll(now)

    def _poll(self, now=None):
        now = time.time() if now is None else now
        newest = newest_log(self.log_dir)
        if newest is None:
            return self.state
        if newest != self.path:
            self.path, self.pos, self.tail = newest, 0, ''
            self.state, self.why = None, ''
            self.aura, self.aura_since, self.in_menu = None, None, False
            self.fresh = now
        try:
            size = os.path.getsize(self.path)
            if size < self.pos:
                self.pos, self.tail = 0, ''
            if size > self.pos:
                with open(self.path, 'rb') as f:
                    f.seek(self.pos)
                    data = f.read()
                    self.pos = f.tell()
                self.fresh = now
                lines = (self.tail + data.decode('utf-8', 'replace')).split('\n')
                self.tail = lines.pop()
                for line in lines:
                    if RPC_MARKER in line:
                        aura = aura_from_line(line)
                        if aura not in (None, '_None_'):
                            self.in_menu = False
                        elif 'In Main Menu' in line:
                            self.in_menu = True
                        if aura is not None and aura != self.aura:
                            self.aura = aura
                            self.aura_since = time_from_line(line) or now
                    if GAME_OUTPUT in line:         # game scripts (auras, the minigame) print here
                        continue
                    if any(k in line for k in LOST):
                        if self.state != 'lost':
                            self.state, self.why = 'lost', _lost_reason(line)
                    elif any(k in line for k in JOINED):
                        self.state, self.why = 'joined', ''
        except OSError:
            pass
        return self.state

    def aura_for(self, now=None):
        """Seconds the current aura has been equipped, by the log's clock."""
        if self.aura_since is None:
            return 0.0
        return (time.time() if now is None else now) - self.aura_since

    def quiet_for(self, now=None):
        return (time.time() if now is None else now) - self.fresh

    def trouble(self, quiet_s=None, check_process=True):
        """Why the client needs rejoining, or None."""
        quiet_s = QUIET_S if quiet_s is None else quiet_s
        if check_process and not roblox_running():
            return 'Roblox is not running'
        if self.poll() == 'lost':
            return 'disconnected (%s)' % self.why
        if self.path and self.quiet_for() > quiet_s:
            return '%s: no new Roblox log lines for %d minutes' % (FROZEN, quiet_s // 60)
        return None


def roblox_running():
    for p in psutil.process_iter(['name']):
        try:
            if (p.info['name'] or '').lower() == ROBLOX_EXE:
                return True
        except psutil.Error:
            continue
    return False


def play_mask(rgb):
    """Pixels that are the Play green (#7eff91), dimmed or anti-aliased down to 60% brightness."""
    a = rgb.astype(np.int32)
    g = a[..., 1]
    k = g / 255.0
    return ((g >= 150) & (np.abs(a[..., 0] - PLAY_RGB[0] * k) <= PLAY_TOL)
            & (np.abs(a[..., 2] - PLAY_RGB[2] * k) <= PLAY_TOL))


def find_play():
    """Screen position of the green Play button on the title screen (centre of the biggest patch), or None."""
    box = client_box(PLAY_BOX)
    found = _group(play_mask(grab(box)), (box[0], box[1]), 8, 40, max(20, 150 * area_scale()))
    if not found:
        return None
    x, y, _ = found[0]
    return int(x), int(y)


def in_game(watch):
    """A real aura in the log and not on the menu ('Equipped _None_' also shows on the title screen)."""
    return watch.aura not in (None, '_None_') and not watch.in_menu


def calibrated_play(watch, due, say=lambda m: None):
    """Once `due` (epoch s) has passed, press the calibrated Play button unless the game already started.
    Returns (the next due time, whether it pressed): `due` while waiting, None once handled."""
    if due is None or time.time() < due:
        return due, False
    if not in_game(watch) and 'play' in load_points():
        say('pressing the calibrated Play button')
        press_point('play', settle=1.0)
        return None, True
    return None, False


def press_play(watch, stop=lambda: False, say=lambda m: None, timeout=90.0, play_due=None, presses=0):
    """Click Play while it is on screen, at most PLAY_PRESSES times in all, then take the game as started.
    True once it has."""
    end, gone, saved = time.time() + timeout, 0, False
    while time.time() < end and not stop():
        watch.poll()
        play_due, pressed = calibrated_play(watch, play_due, say)
        presses += pressed
        at = find_play() if presses < PLAY_PRESSES else None
        if at is not None:
            gone = 0
            say('pressing Play')
            click(*at)
            presses += 1
        else:
            gone += 1
            if in_game(watch) or (watch.aura is not None and not watch.in_menu and gone >= 3):
                return True
            if watch.in_menu and not saved:
                saved = True
                screenshot('title_screen.png')
        if presses >= PLAY_PRESSES:
            say('pressed Play %d times - taking the game as started' % presses)
            return True
        time.sleep(3.0)
    return in_game(watch)


def roblox_processes():
    """Every Roblox client process (player, launcher, crash handler); never Roblox Studio."""
    out = []
    for p in psutil.process_iter(['name']):
        try:
            name = (p.info['name'] or '').lower()
            if 'roblox' in name and 'studio' not in name:
                out.append(p)
        except psutil.Error:
            continue
    return out


def close_roblox(wait=15.0):
    """Kill every Roblox client process and wait until none is left. True once they are all gone."""
    end = time.time() + wait
    while True:
        left = roblox_processes()
        if not left:
            return True
        if time.time() >= end:
            return False
        for p in left:
            try:
                p.kill()
            except psutil.Error:
                pass
        time.sleep(0.5)


BROWSERS = {'chrome.exe', 'msedge.exe', 'brave.exe', 'opera.exe', 'firefox.exe', 'vivaldi.exe', 'arc.exe',
            'chromium.exe', 'librewolf.exe', 'waterfox.exe', 'floorp.exe', 'zen.exe', 'thorium.exe', 'browser.exe'}


def window_title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def browser_windows():
    """{hwnd: title} of every visible browser window."""
    pids = set()
    for p in psutil.process_iter(['name']):
        try:
            if (p.info['name'] or '').lower() in BROWSERS:
                pids.add(p.pid)
        except psutil.Error:
            continue
    return {w.hwnd: window_title(w.hwnd) for w in mkey.get_all_windows() if w.pid in pids and w.status == 'visible'}


def close_join_tabs(before):
    """Ctrl+W on each browser window that turned to a Roblox page after the link was opened. How many it closed."""
    closed = 0
    for hwnd, title in browser_windows().items():
        if 'roblox' not in title.lower() or before.get(hwnd) == title:
            continue
        if not force_foreground(hwnd):
            continue
        time.sleep(0.3)
        if foreground_hwnd() != hwnd or 'roblox' not in window_title(hwnd).lower():
            continue
        with kb.pressed(Key.ctrl):
            kb.press('w')
            kb.release('w')
        closed += 1
        time.sleep(0.3)
    return closed


def came_back(log_dir, stop, wait):
    """True once the newest log shows the client joined again and in game (the game's own auto rejoin)."""
    end = time.time() + wait
    while time.time() < end and not stop():
        watch = Watch(log_dir)
        watch.poll()
        if watch.state == 'joined' and in_game(watch):
            return True
        time.sleep(3.0)
    return False


def rejoin(link, say=lambda m: None, stop=lambda: False, log_dir=None, browser=False, frozen=False):
    """Open the private server again and wait until the game is talking. True if in.
    Roblox is only closed by force when it is frozen; a new launch replaces a live client by itself."""
    target = deeplink(link)
    if not target:
        say('the private server link is not a Roblox link')
        return False
    if browser:
        target = link.strip()
    log_dir = log_dir or LOG_DIR
    if frozen:
        say('Roblox is frozen - closing it')
        if not close_roblox():
            say('Roblox would not fully close')
            return False
        time.sleep(CLOSED_SETTLE_S)
    elif roblox_running():
        say('waiting for the game to rejoin by itself')
        if came_back(log_dir, stop, REJOIN_GRACE_S):
            say('the game rejoined by itself')
            return True
    watch = Watch(log_dir)
    watch.poll()        # history is skipped; only what comes after the launch counts, in this log or a new one
    watch.state, watch.why, watch.aura, watch.in_menu = None, '', None, False
    if stop():
        return False
    tabs = browser_windows() if browser else None
    say('opening the private server%s' % (' in the browser' if browser else ''))
    os.startfile(target)
    t0, joined_at = time.time(), None
    play_due, presses = t0 + PLAY_AFTER_S, 0
    while not stop():
        watch.poll()
        now = time.time()
        play_due, pressed = calibrated_play(watch, play_due, say)
        presses += pressed
        # Roblox logs leaveUGCGameInternal while handing a launch over to the server, before the join
        if watch.state == 'lost' and (joined_at is not None or 'leaveUGCGameInternal' not in watch.why):
            say('the new session was dropped too (%s)' % watch.why)
            return False
        if joined_at is None and watch.state == 'joined':
            joined_at = now
            say('joined - waiting for the game to load')
            if tabs is not None:
                try:
                    if close_join_tabs(tabs):
                        say('closed the browser tab')
                        take_foreground(tries=2)
                except Exception as e:
                    say('could not close the browser tab (%s)' % e)
        if joined_at is None and now - t0 > JOIN_S:
            say('the new client never joined')
            return False
        if joined_at is not None and (watch.in_menu or watch.aura is not None or now - joined_at > READY_S):
            if not in_game(watch) and not press_play(watch, stop, say, play_due=play_due, presses=presses):
                say('the game did not start after pressing Play')
                return False
            end = time.time() + JOIN_SETTLE_S
            while time.time() < end and not stop():
                time.sleep(0.2)
            return not stop()
        time.sleep(1.0)
    return False


# ================================================================ item and aura menus
POINTS = (
    ('collection', 'Collection button'),
    ('exit_collection', 'Close Collections button'),
    ('inventory', 'Inventory button'),
    ('items', 'Items tab'),
    ('search', 'Search box'),
    ('slot1', 'First item slot'),
    ('slot2', 'Second item slot'),
    ('use', 'Use button'),
)
NAMES = [name for name, _ in POINTS]
AURA_POINTS = (
    ('aura_storage', 'Auras button'),
    ('aura_search', 'Aura search button'),
    ('aura_slot', 'First aura slot'),
    ('aura_equip', 'Equip button'),
)
AURA_NAMES = [name for name, _ in AURA_POINTS]
ABYSSAL = 'Abyssal'      # the logged aura must contain this
ABYSSAL_TERM = 'abyssal hunter'
SC, BR = 'strange controller', 'biome randomizer'
SLOTS = 2
CLEAR_TAPS = 20          # backspaces before typing, so the last search term is gone
RAPID_HOLD = RAPID_GAP = 0.004


class Rapid:
    def __init__(self, key, times):
        self.key, self.times = key, times


TO_INVENTORY = ([MENU_KEY, SETTLE] + [Key.up, Key.right] * 4 + [Key.up] * 2 + [Rapid(Key.down, 19)]
                + [Key.left] * 4 + [Key.up] * 3 + [Key.enter, SETTLE] + [Key.up, Key.right] * 4 + [Key.up] * 2
                + [Key.left, Key.down, Key.right] + [Key.enter, SETTLE] + [Key.down] + [Key.enter, SETTLE])
TO_FIRST_SLOT = [Key.enter, SETTLE, Key.down, Key.down]
USE = [Key.enter, SETTLE, Key.down, Key.up, Key.left, Key.up, Key.right, Key.enter, SETTLE]
NOT_FOUND = [Key.left] * 3 + [Key.down, Key.enter, SETTLE]
TO_SEARCH_AGAIN = [Key.right, Key.up, Key.enter, SETTLE]
FINISH = [Key.left, Key.left, Key.up, Key.enter, SETTLE, MENU_KEY, SETTLE]
TO_AURA_SEARCH = ([MENU_KEY, SETTLE] + [Key.up, Key.right] * 4 + [Key.up] * 2 + [Key.left] * 2
                  + [Key.enter, SETTLE] + [Key.right, Key.up, SETTLE] + [Key.enter, SETTLE])
AURA_EQUIP = ([Key.enter, SETTLE] + [Key.down] * 2 + [Key.enter, SETTLE] + [Rapid(Key.down, 10)] + [Key.left] * 3
              + [Key.up] * 4 + [Key.enter, SETTLE] + [Key.left] + [Key.up] * 4 + [Key.enter, SETTLE] + [MENU_KEY, SETTLE])

SEL_HUE = (85, 115)      # the selected slot's blue outline
SEL_SAT = 90
SEL_VAL = 140
GREY_MIN = 150           # item names are light grey
GREYS = np.array([(0x9a, 0x9b, 0x9b), (0xbd, 0xbd, 0xbd), (0x8c, 0x8c, 0x8c), (0xba, 0xba, 0xba)], np.int16)
GREY_TOL = 10
MIN_SEL_FRAC = 0.03
MAX_SEL_FRAC = 0.25
SQUARISH = 2.2
INK = 0.004
TINT_SAT = 90            # a name drawn in colour is a different item
TINT = 0.004
STEPS = 5
GREY_RADIUS = 45         # px round a slot's centre checked for the grey name, at a 1440 px tall window


def stopped():
    return solver.out_of_time()


_nav_toggles = 0         # navigation toggles pressed this pass; odd means it is still on


def run_keys(steps):
    global _nav_toggles
    for step in steps:
        if stopped():
            return False
        if step is SETTLE:
            paced(SETTLE_S)
        elif isinstance(step, Rapid):
            for _ in range(step.times):
                tap(step.key, hold=RAPID_HOLD, gap=RAPID_GAP)
        else:
            if step == MENU_KEY:
                _nav_toggles += 1
            tap(step)
    return True


def type_term(term):
    for _ in range(CLEAR_TAPS):
        tap(Key.backspace, hold=0.01, gap=0.01)
    for ch in term:
        tap(ch)


def equip_by_keys():
    """Re-equip Abyssal Hunter by menu keys; UI navigation is left off however it ends."""
    global _nav_toggles
    _nav_toggles = 0
    try:
        if not steady_front():
            return False
        if not run_keys(TO_AURA_SEARCH) or not run_keys([Rapid(Key.backspace, 2)]):
            return False
        type_term(ABYSSAL_TERM)
        screenshot('equip_1_searched.png')
        if not run_keys(AURA_EQUIP[:-2]):
            return False
        screenshot('equip_2_pressed_equip.png')
        return run_keys(AURA_EQUIP[-2:])
    finally:
        if _nav_toggles % 2:
            tap(MENU_KEY)


def search_here(term):
    type_term(term)
    run_keys(TO_FIRST_SLOT)


def walk_and_use(term, say, steps=STEPS):
    """Press Use if the selected slot holds the wanted item; otherwise go straight to NOT_FOUND."""
    if stopped():
        return False
    paced(SETTLE_S)
    rgb = _grab_window()
    sel, what = read_slot(rgb)
    save_slot(rgb, sel, term, 1, what)
    if what == 'wanted':
        paced(0.3)
        what = read_slot()[1]
    if what == 'wanted':
        run_keys(USE)
        say('used one %s' % term)
        return True
    say('%s: not detected (%s)' % (term, 'nothing selected' if sel is None else what))
    run_keys(NOT_FOUND)
    return False


def save_slot(rgb, sel, term, slot, what):
    """The frame a slot was judged on, for items.log: APP_DIR/item_<term>_<slot>.png."""
    try:
        img = rgb.copy()
        if sel is not None:
            x0, y0, x1, y1 = sel
            img[y0:y1 + 1, [x0, x1]] = (255, 0, 255)
            img[[y0, y1], x0:x1 + 1] = (255, 0, 255)
        Image.fromarray(img).save(os.path.join(APP_DIR, 'item_%s_%d_%s.png' % (term.replace(' ', '_'), slot, what)))
    except Exception:
        pass


def item_pass(say=print):
    """One pass: a Biome Randomizer, then a Strange Controller. Returns what was used."""
    global _nav_toggles
    _nav_toggles = 0
    try:
        return _item_pass(say)
    finally:
        if _nav_toggles % 2:
            tap(MENU_KEY)


def _item_pass(say):
    if stopped():
        return []
    if not steady_front():
        say('Roblox would not stay in front - nothing used')
        return []
    paced(SETTLE_S)
    used = []
    if click_mode_on():
        for term in (BR, SC):
            if click_use(term, say):
                used.append(term)
            else:
                say('%s: not used' % term)
            if not stopped():
                press_point('inventory', settle=0.6)     # close it again
        return used
    run_keys(TO_INVENTORY)
    search_here(BR)
    if not walk_and_use(BR, say, SLOTS):
        if not stopped():
            say('%s: not used - shutting the menu' % BR)
            run_keys([MENU_KEY])
        return used
    used.append(BR)
    if stopped():
        return used
    run_keys(TO_SEARCH_AGAIN)
    search_here(SC)
    if walk_and_use(SC, say, SLOTS):
        used.append(SC)
    else:
        say('%s: not used' % SC)
    if not stopped():
        run_keys(FINISH)
    return used


def load_points():
    """Calibrated click points as {name: (fx, fy)} fractions of the Roblox window."""
    try:
        raw = load_settings().get('clicks') or {}
    except Exception:
        return {}
    out = {}
    for name, value in raw.items():
        try:
            out[name] = (float(value[0]), float(value[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def save_point(name, point):
    stored = dict(load_points())
    stored[name] = [round(point[0], 5), round(point[1], 5)]
    save_settings({'clicks': stored})
    return stored


def points_missing(stored=None):
    stored = load_points() if stored is None else stored
    return [n for n in NAMES if n not in stored]


def aura_missing(stored=None):
    stored = load_points() if stored is None else stored
    return [n for n in AURA_NAMES if n not in stored]


def points_ready():
    return not points_missing()


def click_mode_on():
    """Click mode on and fully calibrated."""
    try:
        if not load_settings().get('click_mode'):
            return False
    except Exception:
        return False
    return points_ready()


def _window_rect():
    box = client_box((0.0, 0.0, 1.0, 1.0))
    return box[0], box[1], max(1, box[2] - box[0]), max(1, box[3] - box[1])


def to_fraction(x, y):
    left, top, w, h = _window_rect()
    return (x - left) / float(w), (y - top) / float(h)


def to_screen(point):
    left, top, w, h = _window_rect()
    return int(round(left + point[0] * w)), int(round(top + point[1] * h))


def press_point(name, settle=0.45):
    """Click a calibrated button. False if it is not calibrated or Roblox would not come forward."""
    stored = load_points()
    if name not in stored:
        return False
    if not click(*to_screen(stored[name]), park=False):
        return False
    paced(settle)
    return True


def click_align(stop=None):
    """Respawn, open and close Collections to settle the camera, then drag and zoom."""
    halt = lambda: bool(stop and stop())
    if halt():
        return False
    focus_roblox()
    if halt():
        return False
    respawn()
    paced(0.8)
    if halt() or not press_point('collection', settle=0.8):
        return False
    if halt() or not press_point('exit_collection', settle=0.8):
        return False
    if halt():
        return False
    move_camera()
    return True


def click_use(term, say):
    """Open Inventory -> Items, search, and use the item by clicking; the same slot check as the keyboard route."""
    if stopped() or not press_point('inventory', settle=0.7):
        return False
    if stopped() or not press_point('items', settle=0.5):
        return False
    if stopped() or not press_point('search', settle=0.4):
        return False
    type_term(term)
    paced(0.6)
    stored = load_points()
    for name in ('slot1', 'slot2'):
        if stopped() or not press_point(name, settle=0.4):
            return False
        left, top, _, _ = _window_rect()
        x, y = to_screen(stored[name])
        what = kind_at((x - left, y - top))
        if what == 'wanted':
            paced(0.3)
            what = kind_at((x - left, y - top))
        if what == 'wanted':
            if stopped() or not press_point('use', settle=0.6):
                return False
            say('used one %s (%s)' % (term, name))
            return True
        say('  %s is %s' % (name, {'other': 'a different item', 'empty': 'empty'}.get(what, what)))
        if what == 'empty':
            return False
    return False


def equip_by_clicks(stop=None):
    """Auras, search, type, Enter, first slot, Equip, Auras again to close."""
    halted = lambda: bool(stop and stop())
    for name, settle in (('aura_storage', 0.8), ('aura_search', 0.4)):
        if halted() or not press_point(name, settle=settle):
            return False
    type_term(ABYSSAL_TERM)
    tap(Key.enter)
    paced(0.6)
    for name, settle in (('aura_slot', 0.5), ('aura_equip', 0.6), ('aura_storage', 0.5)):
        if halted() or not press_point(name, settle=settle):
            return False
    return True


def _grab_window():
    return grab(client_box((0.0, 0.0, 1.0, 1.0)))


def selection(rgb):
    """The slot outlined in blue, as (x0, y0, x1, y1) inside the grab, or None."""
    h, s, v = hsv(rgb)
    blue = (s > SEL_SAT) & (v > SEL_VAL) & (h > SEL_HUE[0]) & (h < SEL_HUE[1])
    lo, hi = MIN_SEL_FRAC * rgb.shape[0], MAX_SEL_FRAC * rgb.shape[0]
    best = None
    rows = np.flatnonzero(blue.any(axis=1))
    for r0, r1 in (runs(rows, 4) if rows.size else ()):
        band = blue[r0:r1 + 1]
        for c0, c1 in runs(np.flatnonzero(band.any(axis=0)), 4):
            w, ht = c1 - c0, r1 - r0
            if not (lo <= ht <= hi and lo <= w <= hi):
                continue
            if not (1 / SQUARISH <= (w / ht if ht else 0) <= SQUARISH):
                continue
            if best is None or ht * w > (best[3] - best[1]) * (best[2] - best[0]):
                best = (c0, r0, c1, r1)
    return best


def kind_at(centre, rgb=None):
    """Within GREY_RADIUS of this point: 'wanted' (grey name), 'other' (only a coloured name) or 'empty'."""
    if rgb is None:
        rgb = _grab_window()
    r = max(3.0, GREY_RADIUS * rgb.shape[0] / 1440.0)
    x, y = int(centre[0]), int(centre[1])
    y0, y1 = max(0, int(y - r)), min(rgb.shape[0], int(y + r) + 1)
    x0, x1 = max(0, int(x - r)), min(rgb.shape[1], int(x + r) + 1)
    if y1 <= y0 or x1 <= x0:
        return 'empty'
    yy, xx = np.ogrid[y0:y1, x0:x1]
    disc = (xx - x) ** 2 + (yy - y) ** 2 <= r * r
    crop = rgb[y0:y1, x0:x1].astype(np.int16)
    _, sat, val = hsv(crop)
    bright = (val > GREY_MIN) & disc
    grey = (disc & (np.abs(crop[..., None, :] - GREYS) <= GREY_TOL).all(-1).any(-1)).sum()
    tinted = (bright & (sat >= TINT_SAT)).sum()
    if grey >= INK * disc.sum() and grey >= tinted:
        return 'wanted'
    return 'other' if tinted >= TINT * disc.sum() else 'empty'


def read_slot(rgb=None):
    """(selected box, what is in it)."""
    if rgb is None:
        rgb = _grab_window()
    sel = selection(rgb)
    return sel, kind_at(((sel[0] + sel[2]) / 2, (sel[1] + sel[3]) / 2), rgb) if sel is not None else 'empty'

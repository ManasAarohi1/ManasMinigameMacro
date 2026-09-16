import ctypes
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox
import urllib.error
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pynput import keyboard, mouse  # noqa: E402

import game  # noqa: E402
import solver  # noqa: E402

BG, PANEL, SUNK, EDGE = '#101116', '#16181f', '#0c0d11', '#272b36'
TEXT, DIM, ACCENT, GOOD, BAD = '#e6e8f0', '#767d92', '#aaaaff', '#7ee08a', '#ef6b6b'
UI, UIB = 'Segoe UI', 'Segoe UI Semibold'
SMALL_BUTTON = dict(bg=SUNK, fg=TEXT, activebackground=SUNK, activeforeground=ACCENT, relief='flat',
                    font=(UIB, 9), pady=4, cursor='hand2', borderwidth=0,
                    highlightthickness=1, highlightbackground=EDGE)

TITLE = 'Manas Minigame Macro'
VERSION = '1.1.1'
RELEASES = (os.environ.get('MMM_UPDATE_URL')
            or 'https://api.github.com/repos/ManasAarohi1/ManasMinigameMacro/releases/latest')
INVITE = 'https://discord.gg/oppression'
AHK_DOWNLOAD = 'https://www.autohotkey.com/download/1.1/'
ITEM_EVERY = 3                          # rounds between item passes
LOOP_ROUNDS = 10 ** 9                   # Continuous loop: keeps going until Stop
ENTER_WAITS = (5, 15, 30, 60, 120, 120)  # s between failed entries before the run is called off
REJOIN_AFTER = 3                        # missed entries in a row before auto reconnect rejoins
REJOIN_WAITS = (30, 60, 120, 300, 300)
WATCH_EVERY = 0.5
CRASH_WAITS = (30, 60, 120, 300)        # s before a crashed run starts again (the last one repeats)
AURA_BLIP_S = 3.0                       # another aura counts once the log has shown it this long
STATS = game.STATS_FILE

def note(tag, msg):
    """Add a timestamped, tagged line to logs.txt in the app folder; never raises."""
    try:
        os.makedirs(game.APP_DIR, exist_ok=True)
        with open(os.path.join(game.APP_DIR, 'logs.txt'), 'a', encoding='utf-8') as f:
            f.write('%s  [%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), tag, msg))
    except OSError:
        pass


def rolled_away(watch):
    """The aura the log shows if it is not Abyssal Hunter and has lasted AURA_BLIP_S, else None."""
    aura = watch.aura
    if not aura or game.ABYSSAL.lower() in aura.lower() or watch.aura_for() < AURA_BLIP_S:
        return None
    return aura


# ================================================================ Discord
FOOTER = 'Manas Minigame Macro'
EMBED_GOOD, EMBED_BAD, EMBED_INFO, EMBED_BIOME = 5763719, 15548997, 10921727, 5814783
PING = {'GLITCHED', 'DREAMSPACE', 'CYBERSPACE'}


def is_rare(name):
    squashed = name.upper().replace(' ', '')
    return any(p in squashed for p in PING)


class BiomeWatcher:
    """A biome change is an event; a rare one pings, even if it is already running at start, once per cooldown."""

    def __init__(self, cooldown=300.0):
        self.cooldown = cooldown
        self.current = None
        self.started = True
        self._last_alert = {}

    def feed(self, name, now=None):
        """(biome, alert) on a change, else None."""
        if not name:
            return None
        name = name.upper()
        if name == self.current:
            return None
        now = time.time() if now is None else now
        self.started = False
        self.current = name
        last = self._last_alert.get(name)
        alert = is_rare(name) and (last is None or now - last >= self.cooldown)
        if alert:
            self._last_alert[name] = now
        return name, alert


def pretty(name):
    return ' '.join(w.capitalize() for w in name.split())


def _post(webhook_url, payload, on_error=None):
    """Fire and forget on a thread."""
    def run():
        try:
            request = urllib.request.Request(webhook_url, data=json.dumps(payload).encode(),
                                             headers={'Content-Type': 'application/json',
                                                      'User-Agent': 'SolsBiomeLogger/1.0'})
            urllib.request.urlopen(request, timeout=15).close()
        except (urllib.error.URLError, OSError, ValueError) as e:
            if on_error:
                on_error('webhook failed: %s: %s' % (type(e).__name__, e))
    threading.Thread(target=run, daemon=True).start()


def _timestamp():
    return time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())


def server_field(link):
    link = (link or '').strip()
    if link.startswith(('https://', 'http://')) and ' ' not in link:
        return '[Join the private server](%s)' % link[:900]
    return link[:1000]


def post_biome(webhook_url, name, ping=True, server=''):
    """Every biome is posted; only `ping` ones mention @everyone."""
    if not webhook_url:
        return
    payload = {'embeds': [{
        'title': 'Biome Started - %s' % pretty(name),
        'description': '**%s** has started.' % pretty(name),
        'color': EMBED_BIOME,
        'footer': {'text': FOOTER},
        'timestamp': _timestamp(),
        'fields': [{'name': 'Private server',
                    'value': server_field(server) if server and server.strip() else 'No private server inserted'}],
    }]}
    if ping:
        payload['content'] = '@everyone'
        payload['allowed_mentions'] = {'parse': ['everyone']}
    _post(webhook_url, payload)


def post_event(webhook_url, title, description='', colour=EMBED_INFO):
    if not webhook_url:
        return
    join = '[Join Manas Biome Hunt!](%s)' % INVITE
    _post(webhook_url, {'embeds': [{
        'title': title,
        'url': INVITE,
        'description': '%s\n\n%s' % (description, join) if description else join,
        'color': colour,
        'footer': {'text': FOOTER},
        'timestamp': _timestamp(),
    }]})


def append_history(biome):
    os.makedirs(os.path.dirname(game.HISTORY_FILE), exist_ok=True)
    with open(game.HISTORY_FILE, 'a') as f:
        f.write(json.dumps({'ts': time.time(), 'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'biome': biome}) + '\n')


# ================================================================ updates
SWAP_BAT = '''@echo off
set n=0
:retry
move /y "{new}" "{old}" >nul 2>&1 && goto done
set /a n+=1
if %n% geq 60 goto done
ping -n 2 127.0.0.1 >nul
goto retry
:done
start "" "{old}"
del "%~f0"
'''


def version_tuple(text):
    return tuple(int(n) for n in re.findall(r'\d+', text or '')[:3])


def latest_release():
    """(tag, exe url, size) of the newest release, or None if it has no exe."""
    req = urllib.request.Request(RELEASES, headers={'User-Agent': TITLE, 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.load(r)
    exe = next((a for a in d.get('assets', []) if a.get('name', '').lower().endswith('.exe')), None)
    return (d.get('tag_name', ''), exe['browser_download_url'], int(exe.get('size') or 0)) if exe else None


def download(url, dest, size, progress=lambda f: None):
    got = 0
    req = urllib.request.Request(url, headers={'User-Agent': TITLE})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, 'wb') as f:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if size:
                progress(got / size)
    if size and got != size:
        os.remove(dest)
        raise IOError('download cut short (%d of %d bytes)' % (got, size))


def install(new_exe, old_exe):
    """Once this process has exited, put the new exe in place and start it."""
    bat = os.path.join(tempfile.gettempdir(), 'mmm_update_%d.bat' % os.getpid())
    with open(bat, 'w') as f:
        f.write(SWAP_BAT.format(new=new_exe, old=old_exe))
    subprocess.Popen(['cmd', '/c', bat], creationflags=0x00000008 | 0x00000200, close_fds=True)


# ================================================================ widgets
class Check(tk.Canvas):
    """A drawn checkbox"""
    SIDE, R = 20, 5

    def __init__(self, parent, variable, side=None):
        self.pad = 3
        if side:
            self.SIDE, self.R = side, max(3, side // 4)
        tk.Canvas.__init__(self, parent, width=self.SIDE + 6, height=self.SIDE + 6, bg=PANEL,
                           highlightthickness=0, bd=0, cursor='hand2')
        self.var = variable
        self.bind('<Button-1>', lambda e: self.var.set(not self.var.get()))
        self.bind('<Enter>', lambda e: self._draw(True))
        self.bind('<Leave>', lambda e: self._draw(False))
        self.var.trace_add('write', lambda *a: self._draw())
        self._draw()

    def _draw(self, hover=False):
        self.delete('all')
        on = bool(self.var.get())
        p, s, r = self.pad, self.SIDE, self.R
        fill, line = (ACCENT if on else SUNK), (ACCENT if on or hover else EDGE)
        x0, y0, x1, y1 = p, p, p + s, p + s
        corners = ((x0, y0, 90), (x1 - 2 * r, y0, 0), (x0, y1 - 2 * r, 180), (x1 - 2 * r, y1 - 2 * r, 270))
        for cx, cy, start in corners:
            self.create_arc(cx, cy, cx + 2 * r, cy + 2 * r, start=start, extent=90, style='pieslice',
                            fill=fill, outline=fill)
        self.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill, outline=fill)
        self.create_rectangle(x0, y0 + r, x1, y1 - r, fill=fill, outline=fill)
        for edge in ((x0 + r, y0, x1 - r, y0), (x0 + r, y1, x1 - r, y1), (x0, y0 + r, x0, y1 - r), (x1, y0 + r, x1, y1 - r)):
            self.create_line(*edge, fill=line)
        for cx, cy, start in corners:
            self.create_arc(cx, cy, cx + 2 * r, cy + 2 * r, start=start, extent=90, style='arc', outline=line)
        if on:
            self.create_line(p + s * 0.26, p + s * 0.52, p + s * 0.44, p + s * 0.70, p + s * 0.76, p + s * 0.30,
                             fill=BG, width=2, capstyle='round', joinstyle='round')


class Switch(tk.Canvas):
    """A three-way switch; the highlight slides to the picked side."""
    W, H, SLIDE_MS, STEP_MS = 72, 30, 180, 12

    def __init__(self, parent, variable, options):
        self.options = options
        k = parent.winfo_fpixels('1i') / 96.0
        self.w, self.h = int(self.W * k), int(self.H * k)
        tk.Canvas.__init__(self, parent, width=self.w * len(options) + 2, height=self.h + 2, bg=PANEL,
                           highlightthickness=0, bd=0, cursor='hand2')
        self.var = variable
        self.x = float(self._target())
        self.hover = None
        self._job = None
        self.bind('<Button-1>', lambda e: self.var.set(self.options[self._at(e.x)][0]))
        self.bind('<Motion>', lambda e: self._hover(self._at(e.x)))
        self.bind('<Leave>', lambda e: self._hover(None))
        self.var.trace_add('write', lambda *a: self._slide())
        self._draw()

    def _at(self, x):
        return max(0, min(len(self.options) - 1, int((x - 1) // self.w)))

    def _target(self):
        keys = [k for k, _ in self.options]
        value = self.var.get()
        return 1 + self.w * (keys.index(value) if value in keys else 0)

    def _hover(self, i):
        if i != self.hover:
            self.hover = i
            self._draw()

    def _slide(self):
        if self._job is not None:
            self.after_cancel(self._job)
        start, end, t0 = self.x, self._target(), time.perf_counter()

        def step():
            f = min(1.0, (time.perf_counter() - t0) * 1000.0 / self.SLIDE_MS)
            self.x = start + (end - start) * (1 - (1 - f) ** 3)
            self._draw()
            self._job = self.after(self.STEP_MS, step) if f < 1 else None
        step()

    def _pill(self, x0, y0, x1, y1, r, **kw):
        pts = (x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
               x0, y1, x0, y1 - r, x0, y0 + r, x0, y0)
        return self.create_polygon(pts, smooth=True, **kw)

    def _draw(self):
        self.delete('all')
        n, w, h = len(self.options), self.w, self.h
        r = h // 2
        self._pill(0, 0, n * w + 1, h + 1, r, fill=SUNK, outline=EDGE)
        self._pill(self.x + 2, 3, self.x + w - 2, h - 2, r - 3, fill=ACCENT, outline=ACCENT)
        picked = self._at(self.x + w / 2)
        for i, (_, text) in enumerate(self.options):
            colour = BG if i == picked else TEXT if i == self.hover else DIM
            self.create_text(1 + w * i + w / 2, h / 2 + 1, text=text, fill=colour, font=(UIB, 9))


def card(parent, **pack):
    f = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=EDGE, highlightcolor=EDGE)
    f.pack(**pack)
    return f


def rule(parent, padx=22):
    tk.Frame(parent, bg=EDGE, height=1).pack(fill='x', padx=padx)


def entry(parent, var, **kw):
    return tk.Entry(parent, textvariable=var, bg=SUNK, fg=TEXT, insertbackground=ACCENT, relief='flat',
                    highlightthickness=1, highlightbackground=EDGE, highlightcolor=ACCENT, **kw)


def clock(secs):
    secs = int(max(0, secs))
    return '%d:%02d' % (secs // 60, secs % 60)


def load_stats():
    """(caught, seconds) from earlier sessions; zeros if missing or mangled."""
    try:
        with open(STATS, encoding='utf-8') as f:
            d = json.load(f)
        return max(0, int(d['caught'])), max(0.0, float(d['seconds']))
    except Exception:
        return 0, 0.0


def save_stats(caught, seconds):
    try:
        os.makedirs(os.path.dirname(STATS), exist_ok=True)
        with open(STATS, 'w', encoding='utf-8') as f:
            json.dump({'caught': caught, 'seconds': round(seconds, 1)}, f)
    except Exception:
        pass


def rounds_file():
    return os.path.join(game.APP_DIR, 'rounds.jsonl')


def record_round(got, secs, why):
    """One finished round for the Stats window; never raises."""
    try:
        os.makedirs(game.APP_DIR, exist_ok=True)
        with open(rounds_file(), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'ts': round(time.time(), 1), 'got': bool(got), 'secs': round(secs, 1), 'why': why}) + '\n')
    except OSError:
        pass


def load_rounds():
    rows = []
    try:
        with open(rounds_file(), encoding='utf-8') as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return rows


def round_stats(rows, since=0.0, now=None, hours=None):
    """Streaks, rates and times from the recorded rounds (oldest first). `hours` is how long the macro ran."""
    now = time.time() if now is None else now
    got = [r for r in rows if r.get('got')]
    times = sorted(r['secs'] for r in got)
    streak = best = 0
    for r in rows:
        streak = streak + 1 if r.get('got') else 0
        best = max(best, streak)
    session = [r for r in rows if r['ts'] >= since]
    s_caught = sum(1 for r in session if r.get('got'))
    if hours is None:
        hours = (session[-1]['ts'] - session[0]['ts'] + session[0]['secs']) / 3600.0 if session else 0.0
    day = time.strftime('%Y-%m-%d', time.localtime(now))
    misses = {}
    for r in rows:
        if not r.get('got'):
            misses[r.get('why', 'other')] = misses.get(r.get('why', 'other'), 0) + 1
    last10 = [r['secs'] for r in got[-10:]]
    return {'rounds': len(rows), 'caught': len(got), 'rate': 100.0 * len(got) / len(rows) if rows else None,
            'streak': streak, 'best': best, 'fastest': times[0] if times else None,
            'median': times[len(times) // 2] if times else None,
            'last10': sum(last10) / len(last10) if last10 else None,
            'today': sum(1 for r in got if time.strftime('%Y-%m-%d', time.localtime(r['ts'])) == day),
            's_rounds': len(session), 's_caught': s_caught, 's_hour': s_caught / hours if hours > 0 else None,
            'misses': misses}


class Calibrate(tk.Toplevel):
    """One row per button; Set hides this window and records the next click, which still reaches Roblox."""

    def __init__(self, app, points=None):
        tk.Toplevel.__init__(self, app.root, bg=BG)
        self.app = app
        self.only = points is not None          # just these buttons (the Play button); click mode is left alone
        self.points = points or game.POINTS + game.LIME_POINTS + (game.AURA_POINTS if app.speed.get() == 'abyssal'
                                                                  else ())
        self.capturing = None
        self.banner = None
        self.title('Calibrate click mode')
        self.geometry('460x%d' % (220 + 50 * len(self.points)))
        self.minsize(420, 120 + 50 * len(self.points))
        self.configure(bg=BG)
        self.transient(app.root)
        self.protocol('WM_DELETE_WINDOW', self.finish)

        head = tk.Frame(self, bg=BG)
        head.pack(fill='x', padx=22, pady=(20, 12))
        tk.Label(head, text='Point at each button', bg=BG, fg=TEXT, font=(UIB, 14)).pack(anchor='w')
        tk.Label(head, text='Press Set, then click that button in Roblox. Your click goes through to the game as normal.',
                 bg=BG, fg=DIM, font=(UI, 9), wraplength=400, justify='left', anchor='w').pack(fill='x', pady=(4, 0))

        body = card(self, fill='both', expand=True, padx=22)
        self.rows = {}
        for i, (name, what) in enumerate(self.points):
            heading = {game.AURA_POINTS[0][0]: 'Abyssal mode',
                       game.LIME_POINTS[0][0]: 'Lime buttons (optional)'}.get(name) if not self.only else None
            if heading:
                tk.Label(body, text=heading, bg=PANEL, fg=TEXT, font=(UIB, 11), anchor='w').pack(
                    fill='x', padx=18, pady=(14, 2))
            elif i:
                rule(body, 18)
            self.rows[name] = self._row(body, name, what)

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill='x', padx=22, pady=16)
        self.done_b = tk.Button(bar, text='Done', command=self.finish, bg=ACCENT, fg=BG, activebackground=ACCENT,
                                activeforeground=BG, relief='flat', font=(UIB, 10), pady=9, cursor='hand2',
                                borderwidth=0)
        self.done_b.pack(fill='x')
        self._refresh()

    def _row(self, parent, name, what):
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=18, pady=6)
        tick = tk.Label(row, text='', bg=PANEL, fg=GOOD, font=(UIB, 11), width=2)
        tick.pack(side='left')
        tk.Label(row, text=what, bg=PANEL, fg=TEXT, font=(UI, 10), anchor='w').pack(side='left', fill='x', expand=True)
        b = tk.Button(row, text='Set', command=lambda: self.capture(name), padx=14, **SMALL_BUTTON)
        b.pack(side='right')
        tk.Button(row, text='Test', command=lambda: self.show(name), bg=SUNK, fg=DIM, activebackground=SUNK,
                  activeforeground=ACCENT, relief='flat', font=(UI, 9), padx=10, pady=4, cursor='hand2',
                  borderwidth=0).pack(side='right', padx=(0, 6))
        return tick, b

    def show(self, name):
        """Put the pointer where the macro will click, without clicking."""
        stored = game.load_points()
        if name in stored:
            game.move_to(*game.to_screen(stored[name]))

    def _refresh(self):
        stored = game.load_points()
        for name, (tick, b) in self.rows.items():
            done = name in stored
            tick.configure(text='✓' if done else '•', fg=GOOD if done else DIM)
            b.configure(text='Redo' if done else 'Set')
        optional = set() if self.only else {n for n, _ in game.LIME_POINTS}
        left = [n for n, _ in self.points if n not in stored and n not in optional]
        self.done_b.configure(text='Done' if not left else 'Done  (%d still to set)' % len(left))

    def capture(self, name):
        if self.capturing:
            return
        self.capturing = name
        self.withdraw()
        self.banner = self._make_banner('Click %s      (Esc to cancel)' % dict(self.points)[name])
        self._got = []

        def on_click(x, y, button, pressed):
            if pressed and button == mouse.Button.left:
                self._got.append((x, y))
                return False

        self._listener = mouse.Listener(on_click=on_click)
        self._listener.start()
        self.after(60, self._poll)

    def _make_banner(self, text):
        top = tk.Toplevel(self, bg=ACCENT)
        top.overrideredirect(True)
        top.attributes('-topmost', True)
        tk.Label(top, text=text, bg=ACCENT, fg=BG, font=(UIB, 12), padx=26, pady=12).pack()
        top.update_idletasks()
        top.geometry('+%d+%d' % (max(0, (top.winfo_screenwidth() - top.winfo_reqwidth()) // 2), 40))
        top.bind('<Escape>', lambda e: self._cancel())
        self.bind_all('<Escape>', lambda e: self._cancel())
        return top

    def _poll(self):
        if not self.capturing:
            return
        if self._got:
            try:
                game.save_point(self.capturing, game.to_fraction(*self._got[0]))
            except Exception:
                pass
            self._end_capture()
        elif not self._listener.running:
            self._end_capture()
        else:
            self.after(60, self._poll)

    def _cancel(self):
        if self.capturing:
            self._end_capture()

    def _end_capture(self):
        self.capturing = None
        try:
            self._listener.stop()
        except Exception:
            pass
        if self.banner is not None:
            self.banner.destroy()
            self.banner = None
        self.unbind_all('<Escape>')
        self.deiconify()
        self.lift()
        self._refresh()

    def finish(self):
        self._cancel()
        if getattr(self, 'only', False):
            self.app._cal_window = None
            self.destroy()
            return
        left = game.points_missing()
        if left:
            if messagebox.askyesno('Calibrate everything', 'Click mode needs every button set, %d still missing.\n\n'
                                   'Keep calibrating?' % len(left), parent=self):
                return
        on = not left
        game.save_settings({'click_mode': on})
        self.app.click_mode.set(on)
        self.app.phase('click mode calibrated and on' if on else 'click mode off, not every button is set',
                       GOOD if on else BAD)
        self.app._cal_window = None
        self.destroy()


MISS_KINDS = ('timed out', 'disconnected', 'aura swap')
STATS_LAYOUT = (
    ('WATERMELONS', (('total', 'Total caught'), ('avg', 'Average time each'), ('today', 'Caught today'))),
    ('STREAKS', (('streak', 'Current streak'), ('best', 'Best streak'))),
    ('ROUNDS', (('rounds', 'Rounds played'), ('rate', 'Collect rate'), ('fastest', 'Fastest'),
                ('median', 'Median time'), ('last10', 'Average of the last 10'))),
    ('THIS SESSION', (('s_caught', 'Caught'), ('s_rounds', 'Rounds'), ('s_hour', 'Per hour'))),
    ('MISSED ROUNDS', (('timed out', 'Timed out'), ('disconnected', 'Disconnected'), ('aura swap', 'Aura swapped'),
                       ('other', 'Other'))),
)


class Stats(tk.Toplevel):
    """Streaks, rates and times; refreshed after every round while open."""

    def __init__(self, app):
        tk.Toplevel.__init__(self, app.root, bg=BG)
        self.app = app
        self.title('Stats')
        self.configure(bg=BG)
        self.transient(app.root)
        self.resizable(False, False)
        self.protocol('WM_DELETE_WINDOW', self.close)
        tk.Label(self, text='Stats', bg=BG, fg=TEXT, font=(UIB, 14)).pack(anchor='w', padx=22, pady=(18, 10))
        self.values = {}
        for title, rows in STATS_LAYOUT:
            box = card(self, fill='x', padx=22, pady=(0, 10))
            tk.Label(box, text=title, bg=PANEL, fg=DIM, font=(UIB, 8), anchor='w').pack(fill='x', padx=18, pady=(10, 2))
            for key, label in rows:
                row = tk.Frame(box, bg=PANEL)
                row.pack(fill='x', padx=18, pady=3)
                tk.Label(row, text=label, bg=PANEL, fg=TEXT, font=(UI, 10)).pack(side='left')
                self.values[key] = tk.Label(row, text='-', bg=PANEL, fg=ACCENT, font=(UIB, 10))
                self.values[key].pack(side='right', padx=(24, 0))
            tk.Frame(box, bg=PANEL, height=6).pack()
        self.refresh()

    def refresh(self):
        s = round_stats(load_rounds(), since=self.app.opened, hours=self.app.hours_run())
        caught, seconds = self.app.caught, self.app.seconds
        fmt = lambda v, f: '-' if v is None else f(v)
        show = {'total': str(caught), 'avg': clock(seconds / caught) if caught else '-', 'today': str(s['today']),
                'streak': str(s['streak']), 'best': str(s['best']), 'rounds': str(s['rounds']),
                'rate': fmt(s['rate'], lambda v: '%.0f%%' % v), 'fastest': fmt(s['fastest'], clock),
                'median': fmt(s['median'], clock), 'last10': fmt(s['last10'], clock),
                's_caught': str(s['s_caught']), 's_rounds': str(s['s_rounds']),
                's_hour': fmt(s['s_hour'], lambda v: '%.1f' % v),
                'other': str(sum(n for k, n in s['misses'].items() if k not in MISS_KINDS))}
        for k in MISS_KINDS:
            show[k] = str(s['misses'].get(k, 0))
        for key, label in self.values.items():
            label.configure(text=show[key])

    def close(self):
        self.app._stats_window = None
        self.destroy()


# ================================================================ the window
class App:
    def __init__(self, root):
        self.root = root
        self.running = False
        self.biome_stop = None
        self._last_hotkey = {}
        self._closing = False
        self._cal_window = None
        self.url = ''
        self.use_items = False
        self.server_link = ''
        self.reconnect_on = False
        self.browser_rejoin = False
        self.abyssal_on = False
        self.mode = 'vip'
        self.slow_pc = False
        self.need_rejoin = None     # why the client has to be rejoined
        self.rejoining = False
        self.watch = None
        self.aura_lost = None       # what a roll equipped in place of Abyssal Hunter
        self.check_aura = False
        self.equipping = False
        self.pause_failed = False
        self.updates = queue.Queue()
        self.cfg = game.load_settings()
        self.caught, self.seconds = load_stats()
        self.opened = time.time()
        self.ran_s = 0.0            # time runs took this session; Per hour counts only this
        self.run_began = None
        self._stats_window = None
        self.updating = False

        root.title(TITLE)
        try:
            root.iconbitmap(default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico'))
        except tk.TclError:
            pass
        root.configure(bg=BG)
        k = root.winfo_fpixels('1i') / 96.0
        w = min(int(470 * k), root.winfo_screenwidth() - 40)
        root.geometry('%dx%d' % (w, int(900 * k)))
        self.body = self._scroller(root)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.report_callback_exception = self._oops

        self._header()
        self._settings_card()
        self._buttons()
        self._biome_card()
        self._footer()
        root.update_idletasks()
        h = min(self.body.winfo_reqheight(), root.winfo_screenheight() - 80)
        root.geometry('%dx%d+%d+%d' % (w, h, max(0, (root.winfo_screenwidth() - w) // 2), 20))
        root.minsize(min(w, int(360 * k)), min(h, int(300 * k)))

        self.phase('idle', DIM)
        self.remember()
        self._autosave()
        self._hotkeys()
        self.check_update()
        root.after(100, self._pump)

    # ----------------------------------------------------------------- layout
    def _scroller(self, root):
        canvas = tk.Canvas(root, bg=BG, highlightthickness=0, yscrollincrement=20)
        canvas.pack(fill='both', expand=True)
        body = tk.Frame(canvas, bg=BG)
        win = canvas.create_window((0, 0), window=body, anchor='nw')
        taller = lambda: body.winfo_reqheight() > canvas.winfo_height()

        def fit(_=None):
            canvas.configure(scrollregion=canvas.bbox('all'))
            if not taller():
                canvas.yview_moveto(0)
        body.bind('<Configure>', fit)
        canvas.bind('<Configure>', lambda e: (canvas.itemconfigure(win, width=e.width), fit()))
        root.bind('<MouseWheel>', lambda e: taller() and canvas.yview_scroll(int(-e.delta / 40), 'units'))
        return body

    def _header(self):
        bar = tk.Frame(self.body, bg=BG)
        bar.pack(fill='x', padx=16, pady=(12, 0))
        tk.Button(bar, text='Stats', command=self.open_stats, padx=16, **SMALL_BUTTON).pack(side='right', anchor='n',
                                                                                          pady=(6, 0))
        tk.Label(bar, text=TITLE, bg=BG, fg=TEXT, font=(UIB, 18)).pack(anchor='w')
        tk.Label(bar, text='Macro to find watermelon in Summer event minigame.', bg=BG, fg=DIM,
                 font=(UI, 9)).pack(anchor='w', pady=(3, 0))

    def open_stats(self):
        if self._stats_window is not None and self._stats_window.winfo_exists():
            self._stats_window.lift()
            return
        self._stats_window = Stats(self)

    def _settings_card(self):
        box = card(self.body, fill='x', padx=16, pady=10)
        self.rounds = self._field(box, 'Rounds', str(self.cfg['rounds']), 'how many tickets to spend')
        self.loop = tk.BooleanVar(value=bool(self.cfg.get('loop')))
        foot = tk.Frame(box, bg=PANEL)
        foot.pack(fill='x', padx=16, pady=(0, 8))
        Check(foot, self.loop, side=13).pack(side='left')
        tip = tk.Label(foot, text='Continuous loop  (ignores Rounds, runs until Stop)', bg=PANEL, fg=DIM, font=(UI, 8),
                       cursor='hand2')
        tip.pack(side='left', padx=(3, 0))
        tip.bind('<Button-1>', lambda e: self.loop.set(not self.loop.get()))
        rule(box)
        self.giveup = self._field(box, 'Give up after', str(self.cfg['give_up_seconds']),
                                  'seconds to spend on one round before pressing Give up')
        rule(box)
        self.auto_items = self._toggle(box, 'Auto SC + BR', 'use a Strange Controller and a Biome Randomizer at the '
                                       'start, then every %d rounds' % ITEM_EVERY, 'auto_items')
        rule(box)
        self.click_mode = tk.BooleanVar(value=bool(self.cfg.get('click_mode')))
        row, self._click_text = self._row(box, 'Click mode', 'point at the buttons once, then use clicks instead of '
                                          'menu keys', lambda r: Check(r, self.click_mode))
        self.cal_b = tk.Button(row, text='Calibrator', command=self.calibrate, padx=12, **SMALL_BUTTON)
        self._show_calibrator()
        self.click_mode.trace_add('write', self._click_mode_changed)
        rule(box)
        self.auto_reconnect = tk.BooleanVar(value=bool(self.cfg.get('auto_reconnect')))
        row, _ = self._row(box, 'Auto reconnect', 'rejoin the private server after a disconnect; Set Play is clicked '
                           '%d s after it opens' % game.PLAY_AFTER_S, lambda r: Check(r, self.auto_reconnect))
        tk.Button(row, text='Set Play', command=lambda: self.calibrate(game.PLAY_POINT), padx=12,
                  **SMALL_BUTTON).pack(side='right', padx=(12, 0))
        rule(box)
        self.rejoin_browser = self._toggle(box, 'Rejoin through browser', 'for PCs where the Roblox link does not open '
                                           'the game: the private server opens in your browser instead', 'rejoin_browser')
        rule(box)
        self.speed = tk.StringVar(value=game.speed_mode(self.cfg))
        self._row(box, 'Walk speed', 'Abyssal means Abyssal Hunter with VIP, and re-equips it',
                  lambda r: Switch(r, self.speed, (('nonvip', 'Non-VIP'), ('vip', 'VIP'), ('abyssal', 'Abyssal'))))
        rule(box)
        self.menu_key = self._field(box, 'UI navigation key', str(self.cfg.get('menu_key') or '\\'),
                                    'the key that turns on UI navigation in Roblox')
        rule(box)
        self.low_end = self._toggle(box, 'Potato PC', 'slower resets, menus and clicks for PCs that lag', 'low_end')

    def _row(self, parent, label, hint, widget, **pack):
        """Label and hint on the left, a control on the right."""
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill='x', padx=18, pady=6)
        widget(row).pack(side='right', padx=(12, 0), **pack)
        text = tk.Frame(row, bg=PANEL)
        text.pack(side='left', fill='x', expand=True)
        tk.Label(text, text=label, bg=PANEL, fg=TEXT, font=(UIB, 10), anchor='w').pack(fill='x')
        note = tk.Label(text, text=hint, bg=PANEL, fg=DIM, font=(UI, 8), anchor='w', justify='left', wraplength=300)
        note.pack(fill='x', pady=(2, 0))
        text.bind('<Configure>', lambda e: note.configure(wraplength=max(100, e.width)))
        return row, text

    def _field(self, parent, label, default, hint):
        var = tk.StringVar(value=default)
        self._row(parent, label, hint, lambda r: entry(r, var, width=6, justify='center', font=('Consolas', 12)),
                  ipady=6)
        return var

    def _toggle(self, parent, label, hint, key):
        var = tk.BooleanVar(value=bool(self.cfg.get(key)))
        self._row(parent, label, hint, lambda r: Check(r, var))
        return var

    def _show_calibrator(self):
        if self.click_mode.get():
            if self.cal_b.winfo_manager() != 'pack':
                self.cal_b.pack(side='right', padx=(12, 0), before=self._click_text)
        else:
            self.cal_b.pack_forget()

    def _buttons(self):
        bar = tk.Frame(self.body, bg=BG)
        bar.pack(fill='x', padx=16, pady=(0, 10))
        self.start_b = self._button(bar, 'Start', self.start, ACCENT, BG)
        self.stop_b = self._button(bar, 'Stop', self.stop, PANEL, TEXT)
        self.stop_b.configure(state='disabled')

    def _button(self, parent, text, cmd, bg, fg):
        b = tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                      relief='flat', disabledforeground=DIM, font=(UIB, 10), pady=9, cursor='hand2', borderwidth=0,
                      highlightthickness=1, highlightbackground=EDGE)
        b.pack(side='left', fill='x', expand=True, padx=(0, 10))
        return b

    def _biome_card(self):
        box = card(self.body, fill='x', padx=16, pady=(0, 10))
        tk.Label(box, text='Discord webhook', bg=PANEL, fg=TEXT, font=(UIB, 10), anchor='w').pack(fill='x', padx=18, pady=(10, 3))
        self.webhook = tk.StringVar(value=self.cfg.get('webhook_url', ''))
        entry(box, self.webhook, show='•', font=('Consolas', 9)).pack(fill='x', padx=22, ipady=6)
        tk.Label(box, text='Biome and failsafe notifier', bg=PANEL, fg=DIM, font=(UI, 8), anchor='w').pack(fill='x', padx=18, pady=(4, 8))
        tk.Label(box, text='Private server link', bg=PANEL, fg=TEXT, font=(UIB, 10), anchor='w').pack(fill='x', padx=22, pady=(0, 4))
        self.server = tk.StringVar(value=self.cfg.get('private_server', ''))
        entry(box, self.server, font=('Consolas', 9)).pack(fill='x', padx=22, ipady=6)
        tk.Label(box, text='Used for auto reconnect and for biome notifications', bg=PANEL, fg=DIM, font=(UI, 8),
                 anchor='w').pack(fill='x', padx=18, pady=(4, 8))
        tk.Frame(box, bg=PANEL, height=4).pack()

    def _footer(self):
        bar = tk.Frame(self.body, bg=BG)
        bar.pack(fill='x', padx=16, pady=(2, 10))
        strip = tk.Frame(bar, bg=BG)
        strip.pack()
        tk.Label(strip, text='F1 start   F2 stop        Made by Manas  |  ', bg=BG, fg=DIM, font=(UI, 9)).pack(side='left')
        link = tk.Label(strip, text=INVITE, bg=BG, fg=ACCENT, font=(UI, 9), cursor='hand2')
        link.pack(side='left')
        link.bind('<Button-1>', lambda e: webbrowser.open(INVITE))
        link.bind('<Enter>', lambda e: link.configure(font=(UI, 9, 'underline')))
        link.bind('<Leave>', lambda e: link.configure(font=(UI, 9)))

    # --------------------------------------------------------------- settings
    def _autosave(self):
        """Save the fields shortly after they stop changing."""
        self._pending = None

        def later(*_):
            if self._pending is not None:
                self.root.after_cancel(self._pending)
            self._pending = self.root.after(800, self.remember)

        for var in (self.rounds, self.loop, self.giveup, self.auto_items, self.webhook, self.click_mode,
                    self.auto_reconnect, self.server, self.speed, self.low_end, self.menu_key, self.rejoin_browser):
            var.trace_add('write', later)

    def remember(self):
        try:
            game.save_settings({'rounds': self.rounds.get(),
                                'loop': bool(self.loop.get()),
                                'click_mode': bool(self.click_mode.get()),
                                'give_up_seconds': self.giveup.get(),
                                'auto_items': bool(self.auto_items.get()),
                                'webhook_url': self.webhook.get().strip(),
                                'private_server': self.server.get().strip(),
                                'auto_reconnect': bool(self.auto_reconnect.get()),
                                'rejoin_browser': bool(self.rejoin_browser.get()),
                                'speed_mode': self.speed.get(),
                                'abyssal_mode': self.speed.get() == 'abyssal',
                                'low_end': bool(self.low_end.get()),
                                'menu_key': self.menu_key.get().strip() or '\\'})
        except Exception:
            pass

    def _oops(self, kind, value, tb):
        try:
            self.phase('%s: %s' % (kind.__name__, value), BAD)
        except Exception:
            pass

    def _hotkeys(self):
        """F1/F2 from any window: a non-suppressing listener, restarted if the OS drops it."""
        self._keyboard = keyboard

        def watch():
            while not self._closing:
                try:
                    with keyboard.Listener(on_press=self.on_key, suppress=False) as listener:
                        listener.join()
                except Exception:
                    pass
                if self._closing:
                    return
                time.sleep(2.0)

        threading.Thread(target=watch, daemon=True).start()

    def on_key(self, key, *_):
        """Every keystroke passes here: compare, queue, never raise, never suppress."""
        try:
            kb = self._keyboard
            if key is not kb.Key.f1 and key is not kb.Key.f2:
                return True
            now = time.perf_counter()
            which = 'start' if key is kb.Key.f1 else 'stop'
            if now - self._last_hotkey.get(which, 0.0) < 0.75:     # a held key repeats
                return True
            self._last_hotkey[which] = now
            self.updates.put({'action': which})
        except Exception:
            pass
        return True

    def _click_mode_changed(self, *_):
        self._show_calibrator()
        if not self.click_mode.get():
            game.save_settings({'click_mode': False})
        elif game.points_ready():
            game.save_settings({'click_mode': True})
            self.phase('click mode on, using the buttons you pointed at', GOOD)
        else:
            self.calibrate()

    def calibrate(self, points=None):
        try:
            if game.find_roblox_window() is None:
                raise RuntimeError('no Roblox window')
        except Exception:
            messagebox.showinfo(TITLE, 'Open Roblox first, then calibrate.')
            if points is None and not game.points_ready():
                self.click_mode.set(False)
            return
        if self._cal_window is not None and self._cal_window.winfo_exists():
            self._cal_window.lift()
            return
        self._cal_window = Calibrate(self, points)

    # ----------------------------------------------------------- biome alerts
    def start_biomes(self, url):
        if self.biome_stop is not None or not url.startswith('https://'):
            return
        self.biome_stop = threading.Event()
        threading.Thread(target=self._watch_biomes, args=(url,), daemon=True).start()

    def _watch_biomes(self, url):
        cfg = game.load_settings()
        stop = self.biome_stop
        failed = None
        post_event(url, '🟢 Started', '', EMBED_GOOD)
        self._set(biome=('watching', GOOD))
        try:
            watcher = BiomeWatcher(float(cfg.get('cooldown_seconds') or 300))
            for line in game.follow(stop.is_set):
                change = watcher.feed(game.biome_from_line(line)) if line else None
                if change:
                    name, alert = change
                    append_history(name)
                    server = self.server_link or game.load_settings().get('private_server', '')
                    post_biome(url, name, ping=alert, server=server)
                    self._set(biome=(pretty(name), ACCENT if alert else DIM))
        except Exception as e:
            failed = '%s: %s' % (type(e).__name__, e)
        finally:
            post_event(url, '🔴 Stopped', '', EMBED_BAD)
            self.biome_stop = None
            self._set(biome=(failed, BAD) if failed else ('', DIM))

    # --------------------------------------------------------------- plumbing
    def phase(self, text, colour=TEXT):
        """Status goes in the title bar."""
        text = (text or '').replace(' -- ', ', ').replace(' - ', ', ')
        self.root.title(TITLE if not text or text == 'idle' else '%s  |  %s' % (TITLE, text))

    def _show_stats(self):
        if self._stats_window is not None and self._stats_window.winfo_exists():
            self._stats_window.refresh()

    def _set(self, **kw):
        """Widget updates from worker threads go through a queue drained on the main thread."""
        self.updates.put(kw)

    def _pump(self):
        if not self.root.winfo_exists():
            return
        try:
            while True:
                self._apply(self.updates.get_nowait())
        except queue.Empty:
            pass
        except tk.TclError:
            return
        except Exception as e:
            self._oops(type(e), e, None)
        self.root.after(100, self._pump)

    def _apply(self, kw):
        if 'phase' in kw:
            self.phase(kw['phase'], kw.get('colour', TEXT))
        if 'stats' in kw:
            self._show_stats()
        if 'biome' in kw:
            self.phase(*kw['biome'])
        if 'click_mode' in kw:
            self.click_mode.set(bool(kw['click_mode']))
        if 'update' in kw:
            self.offer_update(kw['update'])
        if kw.get('action') == 'start' and not self.running:
            self.start()
        elif kw.get('action') == 'stop' and self.running:
            self.stop()
        elif kw.get('action') == 'quit':
            self.close()
        elif kw.get('action') == 'idle':
            self.start_b.configure(state='normal')
            self.stop_b.configure(state='disabled')

    # ---------------------------------------------------------------- actions
    def start(self):
        if self.running or self.updating:
            return
        try:
            n = LOOP_ROUNDS if self.loop.get() else max(1, int(self.rounds.get()))
            limit = max(0.0, float(self.giveup.get()))
        except ValueError:
            self.phase('rounds and give up must be numbers', BAD)
            self.refused('Rounds and Give up after must be numbers.')
            return
        self.server_link = self.server.get().strip()
        self.reconnect_on = bool(self.auto_reconnect.get())
        self.browser_rejoin = bool(self.rejoin_browser.get())
        self.mode = self.speed.get()
        self.abyssal_on = self.mode == 'abyssal'
        self.slow_pc = bool(self.low_end.get())
        if self.needs_calibration():
            return
        missing = self.whats_missing()
        if missing:
            self.phase(missing, BAD)
            if 'AutoHotkey' in missing:
                self.offer_ahk_install(missing)
            else:
                self.refused(missing)
            return
        self.remember()
        self.url = self.webhook.get().strip()
        self.use_items = bool(self.auto_items.get())
        self.start_biomes(self.url)
        self.running = True
        self.start_b.configure(state='disabled')
        self.stop_b.configure(state='normal')
        threading.Thread(target=self._guard, args=(n, limit), daemon=True).start()

    def needs_calibration(self):
        """Click mode with buttons never set: offer the Calibrator instead of starting."""
        if not self.click_mode.get():
            return False
        gaps = game.points_missing() + (game.aura_missing() if self.abyssal_on else [])
        if not gaps:
            return False
        self.phase('click mode needs calibrating first', BAD)
        self.ask_calibrate(len(gaps))
        return True

    def ask_calibrate(self, count):
        what = 'the Abyssal Hunter buttons' if self.abyssal_on and not game.points_missing() else 'all the buttons'
        if messagebox.askyesno('Calibrate first', 'Click mode is on, but %s are not calibrated yet (%d missing).\n\n'
                               'Open the Calibrator now?' % (what, count), parent=self.root):
            self.calibrate()

    def check_update(self):
        """Built exe only: a newer GitHub release offers to update; no internet or no release does nothing."""
        if not getattr(sys, 'frozen', False):
            return

        def run():
            try:
                found = latest_release()
            except Exception:
                return
            if found and version_tuple(found[0]) > version_tuple(VERSION):
                self._set(update=found)
        threading.Thread(target=run, daemon=True).start()

    def offer_update(self, found):
        tag, url, size = found
        if self.running or self.updating:
            return
        if not messagebox.askyesno(TITLE, 'Version %s is out (you have %s).\n\nUpdate now? The macro restarts by '
                                   'itself.' % (tag.lstrip('v'), VERSION), parent=self.root):
            return
        self.updating = True
        self.start_b.configure(state='disabled')
        new = sys.executable + '.new'

        def run():
            try:
                download(url, new, size, progress=lambda f: self._set(phase='downloading update %d%%' % round(100 * f)))
                install(new, sys.executable)
                self._set(action='quit')
            except Exception as e:
                self.updating = False
                self._set(phase='update failed: %s' % e, colour=BAD, action='idle')
        threading.Thread(target=run, daemon=True).start()

    def refused(self, why):
        try:
            messagebox.showwarning('Cannot start', why[:1].upper() + why[1:], parent=self.root)
        except Exception:
            pass

    def tell(self, title, detail='', colour=None):
        """Discord and the title bar at once."""
        post_event(self.url, title, detail, colour if colour is not None else EMBED_INFO)
        self._set(phase='%s%s' % (title, ' - ' + detail if detail else ''))

    def offer_ahk_install(self, why):
        if messagebox.askyesno(TITLE, why + '\n\nOpen the AutoHotkey 1.1 download page?\n\nInstall it, then press Start again.'):
            webbrowser.open(AHK_DOWNLOAD)

    def whats_missing(self):
        """Anything that would make this run fail, as a message; None if ready."""
        try:
            return self._whats_missing()
        except Exception as e:
            return '%s: %s' % (type(e).__name__, e)

    def _whats_missing(self):
        if not os.path.exists(solver.DATA):
            return 'a data file is missing from this build (data.npz)'
        if game.interpreter() is None:
            others = game.other_versions()
            if others:
                return ('found AutoHotkey %s, but the paths need 1.1 - install 1.1 alongside it'
                        % '.'.join(str(n) for n in (game.file_version(others[0]) or ('2',))[:2]))
            return 'AutoHotkey 1.1 is not installed'
        if self.abyssal_on and self.click_mode.get() and game.aura_missing():
            return ('abyssal mode with click mode - press Calibrator and set the 4 Abyssal '
                    'buttons (Auras, search, first slot, Equip)')
        if self.reconnect_on and not game.deeplink(self.server_link):
            return 'auto reconnect is on - paste a Roblox private server link below'
        try:
            if game.find_roblox_window() is None:
                return None if self.reconnect_on else 'no Roblox window - open the game first'
        except Exception:
            return None if self.reconnect_on else 'could not find the Roblox window - is the game open?'
        return None

    def hours_run(self):
        began = self.run_began
        return (self.ran_s + (time.time() - began if began is not None else 0.0)) / 3600.0

    def _guard(self, n, limit):
        """Run until Stop: a crash starts the run again after a wait."""
        self.run_began = time.time()
        try:
            for k in itertools.count():
                try:
                    self.play(n, limit)
                    return
                except Exception as e:
                    try:
                        game.release_all()
                    except Exception:
                        pass
                    why = '%s: %s' % (type(e).__name__, e)
                    note('crash', why)
                    if solver.ABORT:
                        self.tell('Macro crashed', why, EMBED_BAD)
                        return
                    wait = CRASH_WAITS[min(k, len(CRASH_WAITS) - 1)]
                    self.tell('Macro crashed', '%s - starting again in %ds' % (why, wait), EMBED_BAD)
                    if not self.nap(wait):
                        return
        finally:
            self.ran_s += time.time() - self.run_began
            self.run_began = None
            self.running = False
            if self.biome_stop is not None:
                self.biome_stop.set()
            self._set(action='idle')

    def nap(self, seconds):
        """Wait, but answer Stop. False if it was pressed."""
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            if solver.ABORT:
                return False
            time.sleep(0.2)
        return not solver.ABORT

    def get_in(self, head):
        """Get into a round, retrying further apart each time until Stop; rejoins and re-equips on the way."""
        misses, k = 0, -1
        while True:
            k += 1
            if solver.ABORT:
                return False
            if self.reconnect_on and not self.need_rejoin and not game.roblox_running():
                self.need_rejoin = 'Roblox is not running'
            if self.need_rejoin and not self.reconnect():
                return False
            try:
                if game.enter_round(ready=self.check_abyssal):
                    return True
                why = 'no dialogue'
            except Exception as e:
                why = '%s: %s' % (type(e).__name__, e)
            misses += 1
            # a missed entry in abyssal mode may mean the aura is gone: re-equip only if the log shows another one
            # (never while it shows Abyssal Hunter: Equip on an equipped aura takes it off)
            if self.abyssal_on and not solver.ABORT and not self.need_rejoin and self.watch is not None:
                self.wearing_abyssal(wait=3.0)
                self.aura_lost = self.aura_lost or rolled_away(self.watch)
                if self.aura_lost and not self.reequip():
                    return False
            if self.reconnect_on and misses % REJOIN_AFTER == 0 and not self.connected():
                self.need_rejoin = 'no minigame button after %d entries' % misses
            if self.need_rejoin:
                continue
            wait = ENTER_WAITS[min(k, len(ENTER_WAITS) - 1)]
            note('connection', 'could not get in (%s), trying again in %ds' % (why, wait))
            self._set(phase='%s - could not get in (%s), trying again in %ds' % (head, why, wait), colour=BAD)
            if not self.nap(wait):
                return False

    def play(self, n, limit):
        solver.ABORT = False
        game.ALIGNED = False
        game.PACE = 2.0 if self.slow_pc else 1.0
        self._set(phase='reading the path', colour=DIM)
        solver.set_mode(self.mode)
        bot = solver.Bot(quiet=True)
        played = won = 0
        self.need_rejoin = None
        self.watch = None
        self.aura_lost = None
        self.check_aura = self.abyssal_on
        solver.PAUSE = self if self.abyssal_on else None
        if self.reconnect_on or self.abyssal_on:
            self.watch = game.Watch()
            self.watch.poll()
            if self.abyssal_on and (self.watch.aura is None or self.watch.in_menu):
                found = game.last_aura(self.watch.log_dir)      # the newest aura any log shows decides the equip
                if found:
                    self.watch.aura, self.watch.aura_since = found
                note('abyssal', 'at start the logs show %s' % (found[0] if found else 'no aura'))
            if self.reconnect_on and not game.roblox_running():
                self.need_rejoin = 'Roblox is not running'
            if self.abyssal_on and not self.need_rejoin and rolled_away(self.watch):
                self.aura_lost = rolled_away(self.watch)
            threading.Thread(target=self._watch_connection, daemon=True).start()

        if not self.ready_to_play():
            solver.PAUSE = None
            game.stop_all()
            self._set(stats=1)
            return

        for i in range(1, n + 1):
            if solver.ABORT:
                break
            head = 'Round %d' % i if n >= LOOP_ROUNDS else 'Round %d of %d' % (i, n)
            self._set(phase='%s - getting into a round' % head)
            began = time.perf_counter()
            solver.clear_deadline()
            if self.need_rejoin and not self.reconnect():
                self.tell('Could not get into a round', 'stopped after %d rounds' % (i - 1), EMBED_BAD)
                break
            if self.use_items and i == 1 and not solver.ABORT:
                self.spend_items()
            self.pause_failed = False
            if self.check_aura or self.abyssal_on:
                first, self.check_aura = self.check_aura, False
                wearing = self.wearing_abyssal(wait=10.0 if first else 2.0)
                aura = self.watch.aura if self.watch else None
                if aura is None:
                    if first:
                        self.tell('Could not read the equipped aura',
                                  'the Roblox log shows none yet - make sure Abyssal Hunter is on', EMBED_BAD)
                elif not wearing and rolled_away(self.watch):   # the 1 s _None_ a join or respawn shows is not a swap
                    self.aura_lost = self.aura_lost or aura
            if self.aura_lost and not self.reequip():
                break
            if not self.get_in(head):
                self.tell('Could not get into a round', 'stopped after %d rounds' % (i - 1), EMBED_BAD)
                break
            played += 1
            solver.start_clock(limit)
            self._set(phase='%s - hunting' % head)
            try:
                got = solver.play_round(bot)
            except Exception as e:
                self.tell('Round %d failed' % i, '%s: %s' % (type(e).__name__, e), EMBED_BAD)
                got = False
            spent = time.perf_counter() - began
            lost = self.need_rejoin
            swapped = ('a roll replaced Abyssal Hunter with %s' % self.aura_lost) if self.aura_lost else None
            ran_out = solver.out_of_time() and not solver.ABORT and not lost and not swapped
            solver.clear_deadline()
            if not solver.ABORT:     # a round you stopped yourself does not count
                record_round(got, spent, 'collected' if got else 'disconnected' if lost else 'aura swap' if swapped
                             else 'timed out' if ran_out else 'other')
            if got:
                self.seconds += spent
                won += 1
                self.caught += 1
                self._set(stats=1)
                self.tell('Watermelon collected', 'round %d in %s - %d caught in total' % (i, clock(spent), self.caught),
                          EMBED_GOOD)
                self.nap(game.ROUND_EXIT_S * game.PACE)
            else:
                self._set(stats=1)
                if lost:
                    self.tell('Round ended', lost, EMBED_BAD)
                elif swapped:
                    self.tell('Round ended', swapped, EMBED_BAD)
                elif ran_out:
                    self.tell('Pressing Give up', 'round %d hit the %ds limit' % (i, int(limit)), EMBED_BAD)
                else:
                    self.tell('Stopped without the item', 'round %d' % i, EMBED_BAD)
                if not solver.ABORT and not lost:
                    try:
                        if game.give_up():
                            self.nap(game.ROUND_EXIT_S * game.PACE)
                    except Exception as e:
                        self._set(phase='%s - could not press Give up (%s)' % (head, type(e).__name__), colour=BAD)
            save_stats(self.caught, self.seconds)
            if self.use_items and not solver.ABORT and not self.need_rejoin and i % ITEM_EVERY == 0:
                self.spend_items()

        solver.PAUSE = None
        game.stop_all()
        self._set(stats=1)
        self.tell('Macro finished', '%d of %d rounds collected' % (won, played), EMBED_GOOD if won else EMBED_INFO)

    def _watch_connection(self):
        """While a run is going: end the round early on a disconnect, flag a lasting rolled aura."""
        while self.running and not self._closing:
            watch = self.watch
            if watch is not None and not self.rejoining and not self.equipping and self.need_rejoin is None:
                try:
                    if self.reconnect_on:
                        why = watch.trouble()
                    else:
                        watch.poll()
                        why = None
                except Exception:
                    why = None
                if why:
                    self.need_rejoin = why
                    solver.DEADLINE = time.perf_counter()
                    try:
                        game.stop_all()
                        game.release_all()
                    except Exception:
                        pass
                if self.abyssal_on and self.aura_lost is None and rolled_away(watch):
                    self.aura_lost = rolled_away(watch)
            time.sleep(WATCH_EVERY)

    def connected(self):
        """The log shows the client joined and in game, so a missed entry is not a disconnect."""
        watch = game.Watch()
        watch.poll()
        return game.roblox_running() and watch.state == 'joined' and game.in_game(watch)

    def ready_to_play(self):
        """Roblox open and connected. Play is only looked for after the macro itself rejoins."""
        watch = self.watch or game.Watch()
        why = watch.trouble(quiet_s=float('inf'))
        if why:
            self.need_rejoin = why
            if not self.reconnect():
                self.tell('Roblox is not ready', why if self.reconnect_on else
                          why + ' - open it, or turn on Auto reconnect with a private server link', EMBED_BAD)
                return False
        return True

    def reconnect(self):
        """Open the private server again until it works or Stop is pressed (Roblox is only closed if frozen).
        True if back in."""
        why, self.need_rejoin = self.need_rejoin, None
        if not (self.reconnect_on and game.deeplink(self.server_link)):
            return False
        note('connection', 'reconnecting: %s' % (why or ''))
        self.tell('Reconnecting', why or '', EMBED_BAD)
        k = -1
        while True:
            k += 1
            if solver.ABORT:
                return False
            self.rejoining = True
            failed = ''
            try:
                ok = game.rejoin(self.server_link, stop=lambda: solver.ABORT, browser=self.browser_rejoin,
                                 frozen=(why or '').startswith(game.FROZEN),
                                 say=lambda m: (note('connection', m), self._set(phase='reconnecting - %s' % m, colour=DIM)))
            except Exception as e:
                ok, failed = False, '%s: %s' % (type(e).__name__, e)
            finally:
                self.watch = game.Watch()
                self.rejoining = False
            solver.clear_deadline()
            if ok:
                game.ALIGNED = False
                self.aura_lost = None
                self.check_aura = self.abyssal_on
                note('connection', 'rejoined')
                self.tell('Rejoined the private server', '', EMBED_GOOD)
                return True
            wait = REJOIN_WAITS[min(k, len(REJOIN_WAITS) - 1)]
            self.tell('Could not rejoin', '%strying again in %ds' % (failed + ' - ' if failed else '', wait), EMBED_BAD)
            if not self.nap(wait):
                return False

    def wearing_abyssal(self, wait=10.0):
        """Does the log say Abyssal Hunter is equipped? Waits a little for a first aura line."""
        if self.watch is None:
            return False
        end = time.time() + wait
        while True:
            self.watch.poll()
            if self.watch.aura is not None or time.time() >= end:
                break
            time.sleep(0.5)
        return game.ABYSSAL.lower() in (self.watch.aura or '').lower()

    def check_abyssal(self):
        """Before every entry try: Abyssal Hunter still on, or put back if a roll replaced it."""
        if not self.abyssal_on or self.watch is None:
            return True
        self.watch.poll()
        aura, age = self.watch.aura, self.watch.aura_for()
        if aura and game.ABYSSAL.lower() not in aura.lower() and age < AURA_BLIP_S:
            if not self.nap(AURA_BLIP_S - age):
                return False
            self.watch.poll()
        if not rolled_away(self.watch):
            return True
        self.aura_lost = self.aura_lost or self.watch.aura
        return self.reequip()

    def pause_pending(self):
        """Asked by the round between key holds: a re-equip to do now?"""
        return (self.running and self.aura_lost is not None and not self.equipping
                and not self.pause_failed and not solver.ABORT)

    def pause_run(self):
        """Mid-round re-equip; the pause does not count against the give-up timer."""
        game.release_all()
        began, deadline = time.perf_counter(), solver.DEADLINE
        ok = self.reequip(mid_round=True)
        if deadline is not None:
            solver.DEADLINE = deadline + (time.perf_counter() - began)
        if not ok:
            self.pause_failed = True
            solver.DEADLINE = time.perf_counter()

    def reequip(self, mid_round=False):
        """Put Abyssal Hunter back on; True once the log shows it. Never presses Equip while it is already on
        (Equip on an equipped aura takes it off)."""
        def wearing():
            if self.watch is None:
                return False
            self.watch.poll()
            return game.ABYSSAL.lower() in (self.watch.aura or '').lower()

        if wearing():
            self.aura_lost = None
            return True
        rolled, self.equipping = self.aura_lost, True
        try:
            self.tell('New aura rolled', '%s replaced Abyssal Hunter - re-equipping' % rolled, EMBED_INFO)
            note('abyssal', 'the log shows %s: re-equipping%s' % (rolled, ' mid round' if mid_round else ''))
            for attempt in range(2):
                if solver.ABORT:
                    return False
                if attempt and wearing():
                    self.aura_lost = None
                    return True
                if not mid_round:
                    solver.clear_deadline()
                route = 'clicks' if game.click_mode_on() and not game.aura_missing() else 'menu keys'
                try:
                    done = game.equip_by_clicks(stop=lambda: solver.ABORT) if route == 'clicks' else game.equip_by_keys()
                except Exception as e:
                    done = False
                    note('abyssal', 'attempt %d by %s crashed: %s: %s' % (attempt + 1, route, type(e).__name__, e))
                note('abyssal', 'attempt %d by %s: steps %s' % (attempt + 1, route, 'done' if done else 'did not finish'))
                if not done:
                    self.close_guis()
                    continue
                end = time.time() + 12.0
                while time.time() < end and not solver.ABORT:
                    self.watch.poll()
                    if game.ABYSSAL.lower() in (self.watch.aura or '').lower():
                        self.aura_lost = None
                        self.tell('Abyssal Hunter re-equipped', '', EMBED_GOOD)
                        note('abyssal', 're-equipped: the log shows %s' % self.watch.aura)
                        return True
                    time.sleep(0.5)
                note('abyssal', 'attempt %d: the log still shows %s after 12 s' % (attempt + 1, self.watch.aura))
                self.close_guis()
            # keep going without it: the normal paths fit the speed of whatever is on now
            note('abyssal', 'could not re-equip, switched to the normal paths')
            self.tell('Could not re-equip Abyssal Hunter', 'switched to the normal paths for the rest of the run', EMBED_BAD)
            self.abyssal_on, self.check_aura, self.aura_lost = False, False, None
            solver.PAUSE = None
            solver.set_mode(False)
            return True
        finally:
            self.equipping = False

    def spend_items(self):
        """One Strange Controller + Biome Randomizer pass, between rounds; logged to items.log."""
        say = lambda m: note('items', m)
        try:
            got = game.item_pass(say=say)
        except Exception as e:
            say('failed - %s: %s' % (type(e).__name__, e))
            self.tell('Auto SC + BR failed', '%s: %s' % (type(e).__name__, e), EMBED_BAD)
            self.close_guis()
            return
        if len(got) == 2:
            self.tell('Used a Strange Controller and a Biome Randomizer')
        elif got:
            missed = game.BR if got[0] == game.SC else game.SC
            self.tell('Used a %s' % pretty(got[0]), "%s wasn't used" % pretty(missed), EMBED_INFO)
        else:
            self.tell('Auto SC + BR found nothing to use', '', EMBED_INFO)
        if len(got) < 2:
            self.close_guis()

    def close_guis(self):
        try:
            game.close_guis()
        except Exception as e:
            note('menus', 'closing menus failed: %s: %s' % (type(e).__name__, e))

    def stop(self):
        solver.ABORT = True
        if self.biome_stop is not None:
            self.biome_stop.set()
        self.phase('stopping after the current read', BAD)
        self.stop_b.configure(state='disabled')
        threading.Thread(target=game.stop_all, daemon=True).start()

    def close(self):
        """X stops a run if one is going, else closes at once."""
        if self.running:
            self.stop()
            return
        self._closing = True
        if self.biome_stop is not None:
            self.biome_stop.set()
        self.remember()
        try:
            solver.ABORT = True
            game.disarm()
            game.stop_all(focus=False)
        except Exception:
            pass
        self.root.destroy()
        os._exit(0)     # the keyboard hook thread would keep a frozen build alive


def dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


if __name__ == '__main__':
    dpi_aware()
    root = tk.Tk()
    App(root)
    root.mainloop()

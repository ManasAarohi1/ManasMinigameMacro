"""The round solver: the walks, the particle filter that tracks position and spawn, the nav graph,
the bot that plays a round, and the round itself."""
import bisect
import heapq
import math
import os
import re
import statistics
import time

import numpy as np

import game

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.npz')

# ================================================================ walks (.ahk)
PX_PER_STUD = 1.8378
BASE_SPEED = 16.0 * 1.25
ABYSSAL_SPEED = 16.0 * 1.5 * 1.25
SPEED = BASE_SPEED
MS_PER_PX = 1000.0 / (SPEED * PX_PER_STUD)
PROBE_AHK = 'probe.ahk'
JUMP_EARLY_MS = 130     # Abyssal speed: jumps inside a leg take off this much sooner (about 3.9 studs)
ABYSSAL_AFTER_JUMP = 823   # Abyssal holds S this long after the probe's first jump (normal keeps 523)

DIR = {'w': (0, -1), 's': (0, 1), 'a': (-1, 0), 'd': (1, 0)}
AFTER_JUMP = '    Send, {Space Up}' + chr(10) + '    Sleep, %d' + chr(10) + '    Send, {s Up}'
_SEND = re.compile(r'\s*Send,?\s*\{(\w+)\s+(Down|Up)\}', re.I)
_SLEEP = re.compile(r'\s*Sleep,?\s*(\d+)', re.I)
_ALIGNED = re.compile(r';\s*aligned\s+([\d.]+)\s+([\d.]+)', re.I)


def walk_text(name):
    """A walk's AutoHotkey text, retimed for the current speed."""
    name = os.path.basename(name)
    text = WALKS[name]
    if name == PROBE_AHK and SPEED != BASE_SPEED:
        text = text.replace(AFTER_JUMP % 523, AFTER_JUMP % ABYSSAL_AFTER_JUMP)
    return text if name.endswith('_abyssal.ahk') else retime(text, BASE_SPEED / SPEED)


def retime(text, factor):
    """Scale every Sleep inside RunPath() taken while a movement key is held."""
    head, sep, body = text.partition('RunPath()')
    if not sep or factor == 1.0:
        return text
    held, lines, scaled = set(), body.split('\n'), set()
    for i, line in enumerate(lines):
        if line.strip() == '}':
            break
        m = _SEND.match(line)
        if m:
            k = m.group(1).lower()
            held.add(k) if m.group(2).lower() == 'down' else held.discard(k)
            continue
        m = _SLEEP.match(line)
        if m and held & set(DIR):
            lines[i] = line[:m.start(1)] + str(max(1, round(int(m.group(1)) * factor))) + line[m.end(1):]
            scaled.add(i)
    if factor < 1:
        # faster: a jump covers more ground before its peak, so take off sooner (the Space hold gets the time)
        for i, line in enumerate(lines):
            m = _SEND.match(line)
            if m and m.group(1).lower() == 'space' and m.group(2).lower() == 'down' and {i - 1, i + 1} <= scaled:
                before, during = _SLEEP.match(lines[i - 1]), _SLEEP.match(lines[i + 1])
                k = min(JUMP_EARLY_MS, int(before.group(1)) - 1)
                lines[i - 1] = lines[i - 1][:before.start(1)] + str(int(before.group(1)) - k) + lines[i - 1][before.end(1):]
                lines[i + 1] = lines[i + 1][:during.start(1)] + str(int(during.group(1)) + k) + lines[i + 1][during.end(1):]
    return head + sep + '\n'.join(lines)


def remainder(text, done_ms):
    """The rest of a walk after done_ms as a RunPath() body, or None when nothing is left."""
    out, held, t, started = [], frozenset(), 0.0, False
    for _, _, ms, keys in legs(text, with_keys=True):
        if not started:
            if t + ms <= done_ms:
                t += ms
                continue
            ms, started = t + ms - done_ms, True
        out += ['    Send, {%s Down}' % k for k in sorted(keys - held)]
        out += ['    Send, {%s Up}' % k for k in sorted(held - keys)]
        held = keys
        out.append('    Sleep, %d' % max(1, round(ms)))
    if not started:
        return None
    out += ['    Send, {%s Up}' % k for k in sorted(held)]
    return 'RunPath()\n{\n' + '\n'.join(out) + '\n}\n'


def set_mode(abyssal):
    """Walk speed for this run: walk timings, the filter, the nav graph and the NPC walk."""
    global SPEED, MS_PER_PX, FILE
    SPEED = ABYSSAL_SPEED if abyssal else BASE_SPEED
    MS_PER_PX = 1000.0 / (SPEED * PX_PER_STUD)
    FILE = 'nav_abyssal.npz' if abyssal else 'nav.npz'
    game.MINIGAME_AHK = 'lime_path_abyssal.ahk' if abyssal else 'lime_path.ahk'


def legs(text, with_keys=False):
    """Every (vx, vy, ms[, held keys]) the walk holds."""
    body = text.split('RunPath()', 1)[1]
    held, out = set(), []
    for line in body.splitlines()[1:]:
        if line.strip() == '}':
            break
        m = _SEND.match(line)
        if m:
            k, d = m.group(1).lower(), m.group(2).lower()
            held.add(k) if d == 'down' else held.discard(k)
            continue
        m = _SLEEP.match(line)
        if m:
            vx = sum(DIR[k][0] for k in held if k in DIR)
            vy = sum(DIR[k][1] for k in held if k in DIR)
            n = math.hypot(vx, vy)
            if n:
                vx, vy = vx / n, vy / n
            ms = int(m.group(1))
            out.append((vx, vy, ms, frozenset(held)) if with_keys else (vx, vy, ms))
    return out


def settle_ms(text):
    """ms spent pushing into the start corner before the `; aligned` mark, or 0."""
    m = _ALIGNED.search(text)
    return sum(l[2] for l in legs(text[:m.start()] + '\n}\n')) if m else 0.0


# ================================================================ spawns and hint rings
K = 1.838
BORD = np.array([10.0, 20.0, 50.0, 80.0, 130.0]) * K   # outer edge of tiers 5..1, map px


def load_spawns():
    """Spawns as (x, y) in map px."""
    w = world()
    return np.column_stack(to_px(w['SW'][:, 0], w['SW'][:, 2]))


# ================================================================ particle filter
N = 256
DT = 1.0 / 30
LOST_SURPRISE = 4.0
LOST_HITS, LOST_WINDOW = 2, 10
TRAIL_S = 30.0
INJECT = 0.2
INJECT_STUDS = 6.0
STEP_UP, JUMP_H, G, BODY, SHOULDER = 2.0, 7.2, 196.2, 5.0, 1.0
REACH, HRP = 9.5, 3.0
START_R = 3.0
SPEED_SD = 0.03
HEAD_SD = 1.5
ROUGH = 0.35
SPEED_DRIFT = 0.01
HEAD_DRIFT = 0.3
MODE_PX = 10.0
STALL_HAZARD = 0.01
STALL_S = 0.8
POS_SD = 0.6
EDGES = np.array([10.0, 20.0, 50.0, 80.0, 130.0])
FLIP, FLIP_EDGE, EDGE, FLOOR = 0.04, 0.3, 2.0, 0.01
FILE_STUDS = 2.0
MISS = 0.3
PRESS_LAG = 0.3
BAD_SCALE = 6.25

_W = None


def world():
    global _W
    if _W is None:
        d = np.load(DATA)
        FL, CE = d['world_FL'].copy(), d['world_CE']
        H, Wd, L = FL.shape
        FL[d['world_blocked']] = np.nan
        deg, sc, tx, ty, mx, mz = [float(v) for v in d['world_fit']]
        th = math.radians(deg)
        _W = dict(FL=FL.reshape(-1, L), CE=CE.reshape(-1, L), FLraw=d['world_FL'].reshape(-1, L), H=H, W=Wd,
                  X0=float(d['world_X0']), Z0=float(d['world_Z0']), c=math.cos(th), s=math.sin(th), sc=sc,
                  tx=tx, ty=ty, mx=mx, mz=mz, SW=_spawns(d), START=d['world_START_PX'].astype(float))
    return _W


def _spawns(d):
    """Each spawn read back from its hint map: the middle of where the hint reads very close or closer,
    standing on the floor level kept for it."""
    X0, Z0, FL = float(d['world_X0']), float(d['world_Z0']), d['world_FL']
    out = []
    for m, k in zip(d['spawn_hints'], d['spawn_floor']):
        r, c = np.nonzero(m >= 4)
        rm, cm = r.mean() + 0.5, c.mean() + 0.5
        out.append((X0 + cm, float(FL[int(rm), int(cm), k]) + 0.5, Z0 + rm))
    return np.array(out, float)


def to_px(x, z):
    w = world()
    dx, dz = x - w['mx'], z - w['mz']
    return (dx * w['c'] - dz * w['s']) * w['sc'] + w['tx'], (dx * w['s'] + dz * w['c']) * w['sc'] + w['ty']


def to_w(px, py):
    w = world()
    ux, uy = (px - w['tx']) / w['sc'], (py - w['ty']) / w['sc']
    return ux * w['c'] + uy * w['s'] + w['mx'], -ux * w['s'] + uy * w['c'] + w['mz']


def _dir(keys):
    """Held keys -> unit world direction, or None."""
    vx = sum(DIR[k][0] for k in keys if k in DIR)
    vy = sum(DIR[k][1] for k in keys if k in DIR)
    n = math.hypot(vx, vy)
    if not n:
        return None
    w = world()
    ux, uy = vx / n, vy / n
    wx, wz = ux * w['c'] + uy * w['s'], -ux * w['s'] + uy * w['c']
    m = math.hypot(wx, wz)
    return wx / m, wz / m


def _columns(x, z):
    w = world()
    r = (z - (w['Z0'] - 1.0)).astype(np.int64) - 1
    c = (x - (w['X0'] - 1.0)).astype(np.int64) - 1
    inb = (r.astype(np.uint64) < w['H']) & (c.astype(np.uint64) < w['W'])
    return np.where(inb, r * w['W'] + c, 0), inb


def landing(x, z, feet, tol):
    """Highest floor each body can stand on at (x, z) from feet height; -inf where none."""
    w = world()
    i, inb = _columns(x, z)
    fl, ce = w['FL'][i], w['CE'][i]
    ok = (fl <= (feet + tol)[:, None]) & (ce >= np.maximum(fl, feet[:, None]) + BODY) & inb[:, None]
    return np.where(ok, fl, -np.inf).max(1)


def solid(x, y, z):
    """Inside geometry at each (x, y, z)."""
    w = world()
    i, inb = _columns(x, z)
    fl, ce = w['FLraw'][i], w['CE'][i]
    hit = y < fl[:, 0] - 0.05
    for k in range(fl.shape[1] - 1):
        hit |= (ce[:, k] < y) & (y < fl[:, k + 1] - 0.05)
    return hit & inb


def advance_bodies(x, z, f, y, vy, mx, mz, space):
    """Advance bodies one DT in place (walk, jump, slide along walls). -> which bodies moved."""
    n = len(x)
    if not np.any(space) and not np.any(mx) and not np.any(mz) and not y.any() and not vy.any():
        return np.zeros(n, bool)
    grounded = (y <= 1e-6) & (vy <= 0)
    jumpers = grounded & space
    if np.any(jumpers):
        vy[jumpers] = math.sqrt(2 * G * JUMP_H)
        grounded = grounded & ~jumpers
    air = ~grounded
    if air.any():
        vy[air] -= G * DT
        y[air] += vy[air] * DT
    done = np.zeros(n, bool)
    if np.any(mx) or np.any(mz):
        feet = f + np.maximum(y, 0)
        tol = np.where(grounded, STEP_UP, 0.25)
        feet5, tol5 = np.concatenate((feet,) * 5), np.concatenate((tol,) * 5)
        zero = np.zeros(n)
        for vx_, vz_ in ((mx, mz), (mx, zero), (zero, mz)):
            todo = ~done & (np.abs(vx_) + np.abs(vz_) > 1e-9)
            if not todo.any():
                break
            nx, nz = x + vx_, z + vz_
            lf = landing(np.concatenate((nx, nx + SHOULDER, nx - SHOULDER, nx, nx)),
                         np.concatenate((nz, nz, nz, nz + SHOULDER, nz - SHOULDER)),
                         feet5, tol5).reshape(5, n)
            fc = lf[0]
            need = np.concatenate((todo, vx_ > 0, vx_ < 0, vz_ > 0, vz_ < 0)).reshape(5, n)
            ok = todo & (np.isfinite(lf) | ~need).all(0)
            if ok.any():
                feet_now = f[ok] + np.maximum(y[ok], 0)
                x[ok], z[ok] = nx[ok], nz[ok]
                fn = fc[ok]
                ch = fn != f[ok]
                ynew = np.where(ch, np.maximum(feet_now - fn, 0), y[ok])
                vy[ok] = np.where(ch & (ynew == 0) & (vy[ok] < 0), 0.0, vy[ok])
                f[ok], y[ok] = fn, ynew
            done |= ok
    low = y <= 0
    y[low] = 0.0
    vy[low & (vy < 0)] = 0.0
    return done


class Locator:
    def __init__(self, now, seed=0):
        w = world()
        self.now = now
        self.rng = np.random.default_rng(seed)
        self.t = now()
        sx, sz = to_w(*w['START'])
        a = self.rng.uniform(0, 2 * math.pi, N)
        r = START_R * np.sqrt(self.rng.uniform(0, 1, N))
        self.x, self.z = sx + r * np.cos(a), sz + r * np.sin(a)
        self.f = landing(self.x, self.z, np.full(N, 1e4), 0.0)
        self.f[~np.isfinite(self.f)] = 89.5
        self.y, self.vy = np.zeros(N), np.zeros(N)
        self.spd = 1.0 + SPEED_SD * self.rng.standard_normal(N)
        h = np.radians(HEAD_SD) * self.rng.standard_normal(N)
        self.ca, self.sa = np.cos(h), np.sin(h)
        self.S = len(w['SW'])
        self.L = np.zeros((N, self.S))            # log-likelihood of every spawn, per particle
        self.base = np.full(N, math.log(self.S))
        self.keys, self.space_until, self.sched = (), -1.0, None
        self.recent = np.zeros(N, bool)
        self.press, self.filed, self._p = None, None, None
        self._n = 0
        self.hits = []
        self.snag = np.zeros(N)
        self.trail = []                           # (t, x, z, feet) of the belief every few steps

    def set_keys(self, keys):
        self.advance()
        self.keys = tuple(('space' if k == ' ' else k) for k in keys if k in DIR or k in (' ', 'space'))

    def jump(self, secs=0.04):
        self.advance()
        self.space_until = self.t + secs

    def schedule(self, t0, text):
        """An AutoHotkey walk starting at t0 drives the keys till it ends."""
        self.advance(t0)   # up to the walk's start, only what was really held
        ends, keys, t = [], [], t0
        for _, _, ms, held in legs(text, with_keys=True):
            t += ms / 1000.0
            ends.append(t)
            keys.append(tuple(held))
        self.sched = (ends, keys)

    def end_schedule(self):
        self.advance()
        self.sched, self.keys = None, ()

    def pause_schedule(self, at, gap):
        if self.sched is None:
            return
        ends, keys = self.sched
        i = bisect.bisect_right(ends, at)
        if i < len(ends):
            self.sched = (ends[:i] + [at, at + gap] + [e + gap for e in ends[i:]],
                          keys[:i] + [keys[i], ()] + keys[i:])

    def pressed(self):
        self.advance()
        if self.press is None:
            self.press = (self.t, self.x.copy(), self.z.copy(), self.f + np.maximum(self.y, 0) + HRP)

    def saw(self, tier):
        """One hint reading, 0..5."""
        self.advance()
        SW = world()['SW']
        changed = False
        if self.press is not None and self.t - self.press[0] >= PRESS_LAG:
            _, px_, pz_, ph = self.press
            d = np.sqrt((px_[:, None] - SW[:, 0]) ** 2 + (pz_[:, None] - SW[:, 2]) ** 2 + (ph[:, None] - SW[:, 1]) ** 2)
            self.L += np.where(d <= REACH - 0.5, math.log(MISS), 0.0)
            self.press, changed = None, True
        mx, mz = self._mean_w()
        if not (self.filed is not None and self.filed[0] == tier
                and math.hypot(mx - self.filed[1], mz - self.filed[2]) < FILE_STUDS):
            self.filed = (tier, mx, mz)
            feet = self.f + np.maximum(self.y, 0)
            d = np.sqrt((self.x[:, None] - SW[:, 0]) ** 2 + (self.z[:, None] - SW[:, 2]) ** 2
                        + (feet[:, None] - SW[:, 1]) ** 2)
            k = np.searchsorted(EDGES, d)
            ep = np.concatenate(([-np.inf], EDGES, [np.inf]))
            near = np.minimum(d - ep[k], ep[k + 1] - d)
            flip = np.where(near < EDGE, FLIP_EDGE, FLIP)
            off = np.abs(5 - k - tier)
            lik = np.where(off == 0, 1 - flip, np.where(off == 1, flip / 2, FLOOR))
            post = np.exp(self.L - self._lse()[:, None])
            surprise = -math.log(float(self.weights() @ (post * lik).sum(1)) + 1e-12)
            self.hits = (self.hits + [surprise > LOST_SURPRISE])[-LOST_WINDOW:]
            if INJECT and surprise > LOST_SURPRISE and self.trail:
                self._inject(int(round(INJECT * N)))
            self.L += np.log(lik)
            changed = True
        if changed:
            self._p = None
            if self.press is None:
                self._resample_if_needed()

    def weights(self):
        lw = self._lse() - self.base
        lw -= lw.max()
        wt = np.exp(lw)
        return wt / wt.sum()

    def _mode(self):
        """Weights of the heaviest cluster, so a belief split round a corner is not read inside the wall."""
        wt = self.weights()
        X, Y = to_px(self.x, self.z)
        mx, my = wt @ X, wt @ Y
        if wt @ ((X - mx) ** 2 + (Y - my) ** 2) <= MODE_PX ** 2:
            return wt, X, Y
        d2 = (X[:, None] - X[None]) ** 2 + (Y[:, None] - Y[None]) ** 2
        k = int((np.exp(-d2 / (2 * MODE_PX ** 2)) @ wt).argmax())
        m = wt * (d2[k] <= (2 * MODE_PX) ** 2)
        return m / m.sum(), X, Y

    def px(self):
        self.advance()
        m, X, Y = self._mode()
        return np.array([m @ X, m @ Y])

    def predict(self, keys, secs):
        """Spread in px of a copy of the belief after holding keys for secs."""
        self.advance()
        x, z, f, y, vy = self.x.copy(), self.z.copy(), self.f.copy(), self.y.copy(), self.vy.copy()
        d = _dir(keys)
        sp = SPEED * self.spd * DT
        mx = (d[0] * self.ca - d[1] * self.sa) * sp
        mz = (d[0] * self.sa + d[1] * self.ca) * sp
        for _ in range(int(round(secs / DT))):
            advance_bodies(x, z, f, y, vy, mx, mz, False)
        return self._spread(*to_px(x, z))

    def spread(self):
        return self._spread(*to_px(self.x, self.z))

    def _spread(self, X, Y):
        wt = self.weights()
        cx, cy = wt @ X, wt @ Y
        return math.sqrt(wt @ ((X - cx) ** 2 + (Y - cy) ** 2))

    def widen(self, studs):
        """Spread the belief out again on the same floor."""
        self.advance()
        jx = self.x + studs * self.rng.standard_normal(N)
        jz = self.z + studs * self.rng.standard_normal(N)
        feet = self.f + np.maximum(self.y, 0)
        fl = landing(jx, jz, feet, STEP_UP)
        ok = np.isfinite(fl) & (fl >= feet - STEP_UP)
        self.x[ok], self.z[ok], self.f[ok] = jx[ok], jz[ok], fl[ok]
        self.y[ok], self.vy[ok] = 0.0, 0.0
        self._p = None

    def lost(self):
        return len(self.hits) >= LOST_WINDOW and sum(self.hits) >= LOST_HITS

    def _near_trail(self, n, studs, back):
        pts = [p for p in self.trail if p[0] >= self.t - back] or self.trail[-1:]
        pick = self.rng.integers(0, len(pts), n)
        tx = np.array([pts[i][1] for i in pick]); tz = np.array([pts[i][2] for i in pick])
        tf = np.array([pts[i][3] for i in pick])
        a = self.rng.uniform(0, 2 * math.pi, n)
        r = studs * np.sqrt(self.rng.uniform(0, 1, n))
        jx, jz = tx + r * np.cos(a), tz + r * np.sin(a)
        return jx, jz, landing(jx, jz, tf + 3.0, 6.0)

    def _inject(self, n):
        """Move the n least likely particles back onto the recent track, with a likely one's spawn evidence."""
        if n <= 0:
            return
        order = np.argsort(self.weights())
        low, donors = order[:n], order[-n:]
        jx, jz, fl = self._near_trail(n, INJECT_STUDS, TRAIL_S)
        ok = np.isfinite(fl)
        low, donors = low[ok], donors[ok]
        self.x[low], self.z[low], self.f[low] = jx[ok], jz[ok], fl[ok]
        self.y[low], self.vy[low] = 0.0, 0.0
        self.L[low] = self.L[donors]
        self._p = None

    def rescatter(self, studs, back=0.0):
        """Spread the belief over floor within studs of anywhere it was in the last back seconds."""
        self.advance()
        if back and self.trail:
            jx, jz, fl = self._near_trail(N, studs, back)
        else:
            a = self.rng.uniform(0, 2 * math.pi, N)
            r = studs * np.sqrt(self.rng.uniform(0, 1, N))
            jx, jz = self.x + r * np.cos(a), self.z + r * np.sin(a)
            fl = landing(jx, jz, self.f + np.maximum(self.y, 0) + 3.0, 6.0)
        ok = np.isfinite(fl)
        self.x[ok], self.z[ok], self.f[ok] = jx[ok], jz[ok], fl[ok]
        self.y[ok], self.vy[ok] = 0.0, 0.0
        self.hits, self.filed, self._p = [], None, None

    def airborne(self):
        self.advance()
        return float(self.weights() @ (self.y > 0.05)) > 0.5

    def feet(self):
        m, _, _ = self._mode()
        return float(m @ (self.f + np.maximum(self.y, 0)))

    def odo(self):
        p = self.px()
        return (p[0] * MS_PER_PX, p[1] * MS_PER_PX)

    def moving(self):
        """Did most of the belief move since last asked?"""
        self.advance()
        m = float(self.weights() @ self.recent)
        self.recent[:] = False
        return m >= 0.5

    def spawn_probs(self):
        if self._p is None:
            post = np.exp(self.L - self._lse()[:, None])
            self._p = self.weights() @ post
        return self._p

    def badness(self):
        """-log spawn belief, scaled to px."""
        p = self.spawn_probs()
        return -BAD_SCALE * np.log(np.maximum(p / p.max(), 1e-300))

    def _lse(self):
        m = self.L.max(1)
        return m + np.log(np.exp(self.L - m[:, None]).sum(1))

    def _mean_w(self):
        wt = self.weights()
        return float(wt @ self.x), float(wt @ self.z)

    def _keys_at(self, t):
        if self.sched is not None:
            ends, keys = self.sched
            i = bisect.bisect_right(ends, t)
            k = keys[i] if i < len(keys) else ()
        else:
            k = self.keys
        return k, ('space' in k) or t < self.space_until

    def advance(self, t=None):
        t = self.now() if t is None else t
        while self.t + DT <= t:
            self._step(*self._keys_at(self.t))
            self.t += DT
            if self._n % 6 == 0:
                wt = self.weights()
                self.trail.append((self.t, float(wt @ self.x), float(wt @ self.z), float(wt @ (self.f + np.maximum(self.y, 0)))))
                if self.trail[0][0] < self.t - TRAIL_S:
                    del self.trail[0]

    def _step(self, keys, space):
        d = _dir(keys)
        if d is None:
            mx = mz = np.zeros(N)
        else:
            sp = SPEED * self.spd * DT
            mx = (d[0] * self.ca - d[1] * self.sa) * sp
            mz = (d[0] * self.sa + d[1] * self.ca) * sp
            if STALL_HAZARD:
                new = self.rng.random(N) < STALL_HAZARD * DT
                self.snag[new] = self.t + STALL_S
                held = self.snag > self.t
                if held.any():
                    mx, mz = np.where(held, 0.0, mx), np.where(held, 0.0, mz)
        moved = advance_bodies(self.x, self.z, self.f, self.y, self.vy, mx, mz, space)
        self.recent |= moved
        self._n += 1
        if POS_SD and self._n % 5 == 0 and moved.any():
            s = POS_SD * math.sqrt(5 * DT)
            jx = self.x + s * self.rng.standard_normal(N)
            jz = self.z + s * self.rng.standard_normal(N)
            fl = landing(jx, jz, self.f + np.maximum(self.y, 0), STEP_UP)
            ok = moved & (np.abs(fl - self.f) < 1e-6)
            self.x[ok], self.z[ok] = jx[ok], jz[ok]

    def _resample_if_needed(self):
        wt = self.weights()
        if 1.0 / (wt @ wt) >= N / 2:
            return
        u = (self.rng.random() + np.arange(N)) / N
        idx = np.minimum(np.searchsorted(np.cumsum(wt), u), N - 1)
        for name in ('x', 'z', 'f', 'y', 'vy', 'spd', 'ca', 'sa', 'snag'):
            setattr(self, name, getattr(self, name)[idx].copy())
        self.L = self.L[idx]
        self.base = self._lse()
        self.recent = self.recent[idx]
        grounded = self.y <= 1e-6
        jx = self.x + ROUGH * self.rng.standard_normal(N)
        jz = self.z + ROUGH * self.rng.standard_normal(N)
        fl = landing(jx, jz, self.f, 0.3)
        keep = grounded & (np.abs(fl - self.f) < 1e-6)
        self.x[keep], self.z[keep] = jx[keep], jz[keep]
        self.spd *= np.exp(SPEED_DRIFT * self.rng.standard_normal(N))
        h = np.arctan2(self.sa, self.ca) + math.radians(HEAD_DRIFT) * self.rng.standard_normal(N)
        self.ca, self.sa = np.cos(h), np.sin(h)
        self._p = None


class Tracked:
    """A bot whose keys, readings and E presses also feed a Locator. `odo` is the belief's."""

    def __init__(self, bot, seed=0):
        clock = bot.now if hasattr(bot, 'now') else time.perf_counter
        object.__setattr__(self, '_b', bot)
        object.__setattr__(self, 'loc', Locator(clock, seed))

    @property
    def __class__(self):
        return type(self._b)

    def __getattr__(self, name):
        return getattr(self._b, name)

    def __setattr__(self, name, value):
        if name != 'odo':
            setattr(self._b, name, value)

    @property
    def odo(self):
        return self.loc.odo()

    def now(self):
        return self.loc.now()

    def hold(self, keys, ms):
        if PAUSE is not None and PAUSE.pause_pending():
            self.loc.set_keys(())
            self._b.let_go()
            PAUSE.pause_run()
        keys = tuple(keys)
        self.loc.set_keys(keys)
        self._b.hold(keys, ms)

    def release(self):
        self.loc.set_keys(())
        self._b.release()

    def let_go(self):
        self.loc.set_keys(())
        self._b.let_go()

    def tap(self, key):
        if key == ' ':
            self.loc.jump(0.04)
        if self._b.tap(key) is not False and key == 'e':
            self.loc.pressed()

    def stab(self, key):
        if self._b.stab(key) is not False and key == 'e':
            self.loc.pressed()

    def look(self):
        self.loc.set_keys(())
        return self._b.look()

    def peek(self):
        h = self._b.peek()
        if h is not None:
            self.loc.saw(h)
        return h

    def moving(self):
        return self.loc.moving() and self._b.moving()

    def walk(self, legs, ahk_file=None):
        text = walk_text(ahk_file) if ahk_file else None
        gen = self._b.walk(legs, ahk_file)
        base = None
        try:
            for t in gen:
                now = self.loc.now()
                if base is None and text is not None:
                    base = now - t
                    self.loc.schedule(base, text)
                elif base is not None and PAUSE is not None and now - t - base > 0.5:
                    gap = now - t - base
                    self.loc.pause_schedule(now - gap, gap)
                    base += gap
                yield t
        finally:
            gen.close()
            self.loc.end_schedule()


# ================================================================ nav graph
FILE = 'nav.npz'        # which graph: 'nav.npz' or 'nav_abyssal.npz' (keys prefixed in data.npz)
REACH_STUDS = 6.0       # a goal node is anywhere E reaches the spawn from
JUMP_REACH_PX = 40.0    # extra cost of a goal node that only reaches it at the top of a jump
_NAV = None


def nav_graph():
    global _NAV
    if _NAV is None or _NAV.file != FILE:
        _NAV = Nav(FILE[:-4] + '_')
        _NAV.file = FILE
    return _NAV


class Nav:
    def __init__(self, prefix):
        npz = np.load(DATA)
        d = {k: npz[prefix + k] for k in ('CELL', 'GW', 'GH', 'node_cell', 'node_f', 'node_of', 'src', 'dst', 'cost',
                                          'dir', 'jump')}
        self.cell, self.gw, self.gh = float(d['CELL']), int(d['GW']), int(d['GH'])
        self.node_cell, self.node_f, self.node_of = d['node_cell'], d['node_f'].astype(float), d['node_of']
        n = len(self.node_cell)
        src, dst, cost = d['src'], d['dst'], d['cost'].astype(float)
        o = np.argsort(dst, kind='stable')
        ptr = np.searchsorted(dst[o], np.arange(n + 1))
        s_, c_ = src[o].tolist(), cost[o].tolist()
        self.rev = [list(zip(s_[a:b], c_[a:b])) for a, b in zip(ptr[:-1].tolist(), ptr[1:].tolist())]
        o = np.argsort(src, kind='stable')
        ptr = np.searchsorted(src[o], np.arange(n + 1))
        e = list(zip(dst[o].tolist(), cost[o].tolist(), d['dir'][o].tolist(), d['jump'][o].tolist()))
        self.fwd = [e[a:b] for a, b in zip(ptr[:-1].tolist(), ptr[1:].tolist())]
        self.cx = (self.node_cell % self.gw + 0.5) * self.cell
        self.cy = (self.node_cell // self.gw + 0.5) * self.cell
        self._fields, self._pen, self._goals = {}, {}, {}
        self._removed = []      # edges blocked this round
        self._seen = None
        self.last_edge = None   # (u, v) the last leg() starts with

    def block(self, u, v):
        """An edge that did not get there: out of the graph till restore()."""
        f = [e for e in self.fwd[u] if e[0] == v]
        r = [e for e in self.rev[v] if e[0] == u]
        self.fwd[u] = [e for e in self.fwd[u] if e[0] != v]
        self.rev[v] = [e for e in self.rev[v] if e[0] != u]
        self._removed.append((u, v, f, r))
        self._fields.clear()

    def restore(self):
        for u, v, f, r in self._removed:
            self.fwd[u] += f
            self.rev[v] += r
        if self._removed:
            self._removed = []
            self._fields.clear()
        return self

    def near(self, p, feet, rings=2):
        """Nodes round p whose floor the feet could be on, nearest first."""
        for r in (rings, rings + 2, rings + 5):
            out = self._near(p, feet, r)
            if out:
                return out
        return []

    def _near(self, p, feet, rings):
        i0, j0 = int(p[0] // self.cell), int(p[1] // self.cell)
        out = []
        for j in range(j0 - rings, j0 + rings + 1):
            for i in range(i0 - rings, i0 + rings + 1):
                if 0 <= i < self.gw and 0 <= j < self.gh:
                    for u in self.node_of[j * self.gw + i]:
                        if u >= 0 and self.node_f[u] <= feet + STEP_UP + 0.5 and self.node_f[u] >= feet - 8.0:
                            out.append((math.hypot(self.cx[u] - p[0], self.cy[u] - p[1]) + 2.0 * abs(self.node_f[u] - feet), int(u)))
        out.sort()
        return [u for _, u in out]

    def goal(self, j):
        """Every node E reaches spawn j from (with line of sight), as a hashable key; None if none."""
        w = world()
        x, fy, z = w['SW'][j]
        px, py = to_px(x, z)
        sc = w['sc']
        rings = int(math.ceil(REACH_STUDS * sc / self.cell)) + 1
        i0, j0 = int(px // self.cell), int(py // self.cell)
        if j in self._goals:
            return self._goals[j]
        pen, root_at = {}, {}
        for jj in range(j0 - rings, j0 + rings + 1):
            for ii in range(i0 - rings, i0 + rings + 1):
                if 0 <= ii < self.gw and 0 <= jj < self.gh:
                    for u in self.node_of[jj * self.gw + ii]:
                        if u < 0:
                            continue
                        dh = math.hypot(self.cx[u] - px, self.cy[u] - py) / sc
                        root = self.node_f[u] + HRP
                        if math.hypot(dh, root - fy) <= REACH_STUDS:
                            pen[int(u)], root_at[int(u)] = 0.0, root
                        elif math.hypot(dh, max(0.0, fy - root - 0.9 * JUMP_H, root - fy)) <= REACH_STUDS:
                            pen[int(u)] = JUMP_REACH_PX
                            root_at[int(u)] = min(max(root, fy), root + 0.9 * JUMP_H)
        us = [u for u in pen]
        if us:
            nx, nz = to_w(self.cx[us], self.cy[us])
            ny = np.array([root_at[u] for u in us])
            d = np.sqrt((x - nx) ** 2 + (fy - ny) ** 2 + (z - nz) ** 2)
            k = np.arange(1, 64)[None, :]
            n = np.maximum(2, d.astype(int))[:, None]
            f = k / n
            use = (k < n) & (d[:, None] * (1 - f) >= 1.0)
            hit = solid((nx[:, None] + (x - nx)[:, None] * f)[use], (ny[:, None] + (fy - ny)[:, None] * f)[use],
                        (nz[:, None] + (z - nz)[:, None] * f)[use])
            blocked = np.zeros(use.shape, bool)
            blocked[use] = hit
            for u, b in zip(us, blocked.any(1)):
                if b:
                    del pen[u]
        seen = self._from_start()
        if not any(seen[u] for u in pen):
            # nothing reachable gets E onto it: stand as near as the reachable floor goes and hop
            d = np.hypot(self.cx - px, self.cy - py)
            near = [int(u) for u in np.argsort(d)[:400] if seen[u]][:6]
            pen = {u: JUMP_REACH_PX for u in near}
        key = tuple(sorted(pen)) if pen else None
        if key:
            self._pen[key] = pen
        self._goals[j] = key
        return key

    def _from_start(self):
        if self._seen is None:
            seen = np.zeros(len(self.node_cell), bool)
            stack = self.near(np.asarray(world()['START'], float), 89.5)[:1]
            for u in stack:
                seen[u] = True
            while stack:
                for e in self.fwd[stack.pop()]:
                    if not seen[e[0]]:
                        seen[e[0]] = True
                        stack.append(e[0])
            self._seen = seen
        return self._seen

    def field(self, goal):
        """Distance to the nearest goal node from every node (Dijkstra, cached)."""
        if goal not in self._fields:
            dist = [math.inf] * len(self.node_cell)
            q = []
            c0 = (float(self.cx[list(goal)].mean()), float(self.cy[list(goal)].mean()))
            pen = self._pen.get(goal, {})
            for u in goal:
                dist[u] = 0.3 * math.hypot(self.cx[u] - c0[0], self.cy[u] - c0[1]) + pen.get(u, 0.0)
                q.append((dist[u], u))
            heapq.heapify(q)
            rev = self.rev
            while q:
                du, u = heapq.heappop(q)
                if du > dist[u]:
                    continue
                for v, c in rev[u]:
                    nd = du + c
                    if nd < dist[v]:
                        dist[v] = nd
                        heapq.heappush(q, (nd, v))
            if len(self._fields) > 24:
                self._fields.clear()
            self._fields[goal] = dist
        return self._fields[goal]

    def arrived(self, p, feet, goal):
        cand = self.near(p, feet)
        return bool(cand) and cand[0] in goal

    def leg(self, p, feet, goal, max_px):
        self.last_edge = None
        dist = self.field(goal)
        cand = self.near(p, feet)
        if not cand:
            return None
        u = min(cand[:6], key=lambda v: dist[v] + math.hypot(self.cx[v] - p[0], self.cy[v] - p[1]))
        if u in goal:
            return 'here'
        if not math.isfinite(dist[u]):
            return None
        left = dist[u]
        run, d0, jump0, total = u, None, False, 0.0
        while True:
            best = min(self.fwd[run], key=lambda e: e[1] + dist[e[0]], default=None)
            if best is None or not math.isfinite(dist[best[0]]):
                break
            v, c, dr, jp = best
            if d0 is None:
                d0, jump0 = dr, jp
                self.last_edge = (run, v)
            elif dr != d0 or jp or jump0:
                break
            total += math.hypot(self.cx[v] - self.cx[run], self.cy[v] - self.cy[run])
            run = v
            if jump0 or total >= max_px or run in goal:
                break
        if d0 is None:
            return None
        return d0, total, jump0, left


# ================================================================ the bot and its moves
TIERS = ["can't feel it anywhere", "barely feel it", "can feel it",
         "close", "very close", "right in front of me"]
SETTLE_MS = 250          # let the character stop before a standing reading
SAMPLES = 5
SLICE_MS = 150           # how finely a walk is chopped up
BACKOFF_MS = 60          # step back off a wall before a new direction
JUMP_EVERY = 1000
SLIDE = (1, -1, 2, -2)   # 45-degree turns to try when blocked
COLLECT_TIER = 4         # "very close": press E while walking
REACH_TIER = 3
STAB_S = 0.012           # a press with no dwell
CLOSE_SLICE_MS = 100
GONE_SAMPLES = 5         # blank reads in a row before believing the hint has gone
COLLECT_TAPS = 30
NEAR_TAPS = 12
COLLECT_GAP = 0.10
E_GAP_S = 0.25           # at least this long between E presses: spamming E gets players kicked
FINISH_RIDE_MS = 500     # keep walking the arrival heading while the hint does not drop
COLLECT_NUDGE = 6
PATH_TRIES = 3

DEADLINE = None
ABORT = False
PAUSE = None             # the window while a mid-round re-equip may be wanted

DIRS = [('w',), ('w', 'd'), ('d',), ('s', 'd'), ('s',), ('s', 'a'), ('a',), ('w', 'a')]


class PathDidNotStart(RuntimeError):
    pass


class Collected(Exception):
    pass


def out_of_time():
    return ABORT or (DEADLINE is not None and time.perf_counter() >= DEADLINE)


def start_clock(seconds):
    global DEADLINE, ABORT
    ABORT = False
    DEADLINE = None if not seconds else time.perf_counter() + float(seconds)


def clear_deadline():
    """Stop the round clock without clearing a Stop."""
    global DEADLINE
    DEADLINE = None


def unit(keys):
    vx = sum(DIR[k][0] for k in keys)
    vy = sum(DIR[k][1] for k in keys)
    n = math.hypot(vx, vy)
    return (vx / n, vy / n) if n else (0.0, 0.0)


def nearest_dir(vx, vy):
    return max(range(8), key=lambda i: vx * unit(DIRS[i])[0] + vy * unit(DIRS[i])[1])


def move(bot, d, ms):
    """Walk direction d, hopping and sliding round whatever is in the way."""
    if d != bot.facing:
        bot.hold(DIRS[(d + 4) % 8], BACKOFF_MS)
        bot.release()
        bot.tap(' ')
        bot.facing = d
        bot.since_jump = 0
        bot.moving()
    going, tries, left = d, 0, ms
    while left > 0:
        chunk = min(JUMP_EVERY - bot.since_jump, left, CLOSE_SLICE_MS if bot.best_band >= COLLECT_TIER else SLICE_MS)
        bot.hold(DIRS[going], chunk)
        left -= chunk
        reach_out(bot)
        if bot.on_slice is not None:
            bot.on_slice(bot)
        bot.since_jump += chunk
        if bot.moving():
            if tries:
                bot.slide = 1 if ((going - d) % 8) < 4 else -1   # keep the side that worked
            tries = 0
            if bot.since_jump >= JUMP_EVERY:
                bot.tap(' ')
                bot.since_jump = 0
            continue
        if tries == 0:
            bot.tap(' ')
            bot.since_jump = 0
        elif tries <= len(SLIDE):
            going = (d + SLIDE[tries - 1] * bot.slide) % 8
        else:
            bot.say('boxed in; cutting the leg short')
            bot.release()
            break
        tries += 1
    bot.facing = going


def nudge(bot, d, ms):
    """A small step: no back-off, no hop."""
    if bot.best_band >= COLLECT_TIER:
        left = ms
        while left > 0:
            chunk = min(CLOSE_SLICE_MS, left)
            bot.hold(DIRS[d], chunk)
            reach_out(bot)
            left -= chunk
    else:
        bot.hold(DIRS[d], ms)
    bot.moving()
    bot.facing = d


def reach_out(bot):
    if bot.best_band >= COLLECT_TIER:
        if bot.best_band >= 5:
            bot.tap('e')
        else:
            bot.stab('e')


def go_back_to(bot, mark):
    """Walk to an earlier bot.odo position: diagonal first, then straight."""
    odo = bot.odo
    dx, dy = mark[0] - odo[0], mark[1] - odo[1]
    if math.hypot(dx, dy) < SLICE_MS:
        return
    moved = False
    diag = min(abs(dx), abs(dy))
    if diag > SLICE_MS:
        move(bot, nearest_dir(math.copysign(1, dx), math.copysign(1, dy)), int(diag * 1.41))
        dx -= math.copysign(diag, dx)
        dy -= math.copysign(diag, dy)
        moved = True
    if abs(dx) > SLICE_MS:
        move(bot, nearest_dir(math.copysign(1, dx), 0), int(abs(dx)))
        moved = True
    if abs(dy) > SLICE_MS:
        move(bot, nearest_dir(0, math.copysign(1, dy)), int(abs(dy)))
        moved = True
    if not moved:
        move(bot, nearest_dir(dx, dy), int(math.hypot(dx, dy)))


def playing(bot):
    """Is a round still on? No only once the hint is gone and the Give up button stays gone."""
    try:
        return bot.in_round() or bot.peek() is not None or not collected(bot)
    except Exception:
        return True


def collected(bot):
    """Several blank reads in a row, the Give up button gone, and the log showing the character respawned out of
    the round (or the screen saying so for UNCONFIRMED_S when the log doesn't)."""
    global _unconfirmed
    for _ in range(GONE_SAMPLES):
        if bot.peek() is not None:
            return False
        bot.wait(0.08)
    if isinstance(bot, Bot):
        if not game.round_over():
            _unconfirmed = None
            return False
        if game.left_round(ROUND_T0):
            return True
        if _unconfirmed is None:
            _unconfirmed = time.perf_counter()
            bot.say('the hint and Give up are gone but the log shows no respawn - still hunting')
        return time.perf_counter() - _unconfirmed >= UNCONFIRMED_S
    try:
        return not bot.in_round()
    except Exception:
        return True


def finish(bot, taps=COLLECT_TAPS):
    """Press E until the hint goes. True if collected."""
    bot.say('%s - collecting' % TIERS[min(5, bot.best_band)])
    keys = tuple(bot.held)
    if keys in DIRS and FINISH_RIDE_MS > 0:
        d, best, left = DIRS.index(keys), bot.peek(), FINISH_RIDE_MS
        while left > 0:
            nudge(bot, d, CLOSE_SLICE_MS)
            bot.stab('e')
            left -= CLOSE_SLICE_MS
            h = bot.peek()
            if h is None:
                if collected(bot):
                    bot.say('the hint is gone - collected')
                    return True
            elif best is None or h > best:
                best = h
            elif h < best:
                break
    bot.release()
    for i in range(taps):
        bot.tap('e')
        bot.wait(COLLECT_GAP)
        if bot.peek() is None and collected(bot):
            bot.say('the hint is gone - collected')
            return True
        if i % COLLECT_NUDGE == COLLECT_NUDGE - 1:
            bot.tap(' ')
            for _ in range(3):
                bot.wait(0.08)
                bot.tap('e')
            if bot.peek() is None and collected(bot):
                bot.say('the hint is gone - collected')
                return True
            move(bot, (i // COLLECT_NUDGE) * 2 % 8, 250)
            bot.release()
    bot.say('still showing a hint - not collected, carrying on')
    return False


ROUND_T0 = 0.0          # wall clock when this round's hunt began
UNCONFIRMED_S = 10.0    # screen-only "round over" for this long counts even without a respawn in the log
_unconfirmed = None


def new_round(bot):
    global ROUND_T0, _unconfirmed
    ROUND_T0, _unconfirmed = time.time(), None
    bot.facing, bot.since_jump, bot.slide, bot.best_band = None, 0, 1, -1
    bot.motion.forget()


def play_round(bot):
    """Play one round. True if the melon was collected."""
    new_round(bot)
    try:
        return probe_hunt(bot)
    except Collected:
        bot.say('the hint is gone - collected')
        return True
    finally:
        try:
            bot.let_go()
        except Exception:
            pass


_last_e = 0.0


def e_ready(now=None):
    """True (and the wait starts again) if an E press is allowed now."""
    global _last_e
    now = time.perf_counter() if now is None else now
    if now - _last_e < E_GAP_S:
        return False
    _last_e = now
    return True


class Bot:
    """Sends keys to Roblox and reads the hint off the screen."""

    def __init__(self, quiet=False, say=None):
        game.arm()
        self.motion = game.Motion()
        self.held = ()
        self.facing, self.since_jump, self.slide, self.best_band = None, 0, 1, -1
        self.on_slice = None
        self.quiet = quiet
        self._say = say

    def say(self, msg):
        if self._say is not None:
            self._say(msg.strip())
        elif not self.quiet:
            print(msg)

    def hold(self, keys, ms):
        for k in keys:
            if k not in self.held:
                game.down(k)
        for k in self.held:
            if k not in keys:
                game.up(k)
        self.held = tuple(keys)
        time.sleep(ms / 1000.0)

    def release(self):
        for k in self.held:
            game.up(k)
        self.held = ()

    def let_go(self):
        self.release()
        game.release_all()

    def moving(self):
        return self.motion.moving()

    def in_round(self):
        return game.in_round()

    def walk(self, legs, ahk_file):
        """AutoHotkey walks the path; yields seconds walked. Retried if it never sets off."""
        proc = game.start_walk(ahk_file)
        try:
            for attempt in range(PATH_TRIES):
                if game.walking(proc) or proc.poll() is None:
                    break
                if attempt == PATH_TRIES - 1:
                    raise PathDidNotStart('the path never set off: Roblox did not come to the front in %d tries'
                                          % PATH_TRIES)
                self.say('the path never set off (exit %s); taking the window and trying again' % proc.poll())
                game.stop_walk(proc)
                try:
                    game.take_foreground()
                except Exception:
                    pass
                proc = game.start_walk(ahk_file)
            t0 = time.perf_counter()
            while proc.poll() is None:
                time.sleep(SLICE_MS / 1000.0)
                if PAUSE is not None and PAUSE.pause_pending():
                    walked = time.perf_counter() - t0
                    game.stop_walk(proc)
                    PAUSE.pause_run()
                    rest = remainder(walk_text(ahk_file), walked * 1000.0)
                    if rest is None:
                        return
                    proc = game.start_walk(ahk_file, text=rest)
                    game.walking(proc)
                    t0 = time.perf_counter() - walked
                yield time.perf_counter() - t0
        finally:
            game.stop_walk(proc)

    def wait(self, seconds):
        time.sleep(seconds)

    def tap(self, key):
        if key == 'e' and not e_ready():
            return False
        game.down(key)
        time.sleep(0.04)
        game.up(key)
        return True

    def stab(self, key):
        if key == 'e' and not e_ready():
            return False
        game.down(key)
        time.sleep(STAB_S)
        game.up(key)
        return True

    def peek(self):
        h, _ = game.hues()
        return None if h is None else game.tier(h)

    def look(self):
        """Stand still, take several readings, return the middle one."""
        self.release()
        time.sleep(SETTLE_MS / 1000.0)
        seen = []
        for _ in range(SAMPLES):
            t = self.peek()
            if t is not None:
                seen.append(t)
            time.sleep(0.05)
        return int(statistics.median(seen)) if seen else 0


# ================================================================ a round
SLACK = 25.0             # px of disagreement a spawn may carry and still fit
RETARGET = 6.0           # a trip's target this much worse than the best: head for the best instead
TRIED = 60.0             # stood on it without collecting: that much worse
PASSED_FROM = 3          # read this close then weaker: walked past it
PASSED_JUMP_MS = 1200    # ...but not while falling back from a jump
HOT_KEEP = True          # a trip whose hint got closer is not dropped for a "better" spawn
RING_TIER = 3            # a reading this close pins the search round there
INREACH_FROM = 3         # "right in front of me" stops the walk after an agreed reading this close,
INREACH_REPEAT = 3       # ...or this many in a row
LOST_STUDS = 6.0         # the belief is lost: scatter it this far...
LOST_BACK = 30.0         # ...round anywhere it was this many seconds back
SEARCH_OUT_MS = 600
SEARCH_ARM_MAX = 1000
CLIMBS = 3               # times a closer reading can move the search centre
HOT_SEARCHES = 2         # searches where it read close before a trip may leave that ring
COLLECT_TRIES = 30
REACH_PUSH_MS = 400
NAV_LEG_PX = 40.0
NAV_STALL = 6            # legs without getting nearer: give that spawn up for now
STUCK_BLOCKS = 2         # ...but first block the legs that got nowhere this many times
RELOC_PX = 25.0          # belief wider than this: push somewhere that pins it
RELOC_S = 1.0
RELOC_TRIES = 3
RELOC_COOL = 1.0
IDLE_VISITS = 3
WIDEN_STUDS = 5.0
EXIT_P = 0.7             # leave the walk once this much belief sits within EXIT_R of one spawn
EXIT_R = 20.0
CLUSTER_PX = 40.0        # or every fitting spawn is this close together


class Shortlist:
    """Spawn disagreement in px, from the filter's spawn belief."""

    def __init__(self, loc):
        self.loc = loc
        self.xy = load_spawns()[:, :2]
        self.penalty = np.zeros(len(self.xy))

    @property
    def bad(self):
        return self.loc.badness() + self.penalty

    def fits(self):
        return np.flatnonzero(self.bad <= self.bad.min() + SLACK)

    def order(self, p, near=None):
        """Fitting spawns, best first, nearest first among equals; `near` = (point, radius) keeps to a ring."""
        bad = self.bad
        ids = np.flatnonzero(bad <= bad.min() + SLACK)
        if near is not None:
            inside = np.flatnonzero(np.hypot(*(self.xy - near[0]).T) <= near[1])
            if len(inside):
                ids = inside[bad[inside] <= bad[inside].min() + SLACK]
        d = np.hypot(*(self.xy[ids] - np.asarray(p, float)).T)
        return [int(i) for i in ids[np.lexsort((d, bad[ids]))]]


class Where:
    def at(self, bot):
        return bot.loc.px()


class InReach(Exception):
    pass


class Passed(Exception):
    pass


class Close(Exception):
    pass


class HintWatch:
    """Reads the hint on every walking step and raises when the walk should stop."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.last, self.best, self.best_odo, self.blanks, self.passed = None, -1, None, 0, True
        self.skip_inreach = self.skip_close = False
        self.target = None
        self.fives = 0

    def __call__(self, bot):
        h = bot.peek()
        if h is None:
            self.blanks += 1
            if self.blanks >= 3 and collected(bot):
                raise Collected
            return
        self.blanks = 0
        if h >= REACH_TIER:
            bot.stab('e')
        self.fives = self.fives + 1 if h >= 5 else 0
        if h >= 5 and not self.skip_inreach and (self.best >= INREACH_FROM or self.fives >= INREACH_REPEAT):
            raise InReach
        agreed, self.last = h == self.last, h
        if not agreed:
            return
        if h > self.best:
            self.best, self.best_odo = h, bot.odo
        elif self.passed and self.best >= PASSED_FROM and h < self.best and bot.since_jump >= PASSED_JUMP_MS:
            raise Passed
        if (h >= COLLECT_TIER and not self.skip_close and self.target is not None
                and np.hypot(*(bot.loc.px() - self.target)) > BORD[5 - h] + SLACK):
            raise Close


def _relocalize(bot, watch):
    """Push the way that most shrinks the belief, if any does. True if it pushed."""
    loc = bot.loc
    now = bot.now()
    if now - getattr(bot, 'reloc_at', -1e9) < RELOC_COOL:
        return False
    bot.reloc_at = now
    before = loc.spread()
    best, best_s = None, before * 0.8
    for d in range(8):
        s = loc.predict(DIRS[d], RELOC_S)
        if s < best_s:
            best, best_s = d, s
    if best is None:
        return False
    bot.say('belief %.0f px wide: pushing %s to pin it' % (before, '+'.join(DIRS[best])))
    ms = RELOC_S * 1000.0
    while ms > 1.0:
        chunk = min(SLICE_MS, ms)
        bot.hold(DIRS[best], chunk)
        ms -= chunk
        watch(bot)
    bot.release()
    return True


def _nav_to(bot, sl, j, watch, pool=None):
    """Walk to spawn j a leg at a time. -> 'arrived', 'ruled out', 'stuck', 'unreachable' or 'time'."""
    nv, loc = nav_graph(), bot.loc
    g = nv.goal(j)
    if g is None:
        return 'unreachable'
    best, stall, relocs, keys = math.inf, 0, 0, ()
    jumped, recent, blocks, first = None, [], 0, None
    while True:
        if out_of_time():
            return 'time'
        landing_for = 0
        while keys and loc.airborne() and landing_for < 8:
            bot.hold(tuple(k for k in keys if k != ' '), SLICE_MS / 3)
            landing_for += 1
            watch(bot)
        if relocs < RELOC_TRIES and loc.spread() > RELOC_PX:
            relocs += 1
            if _relocalize(bot, watch):
                continue
        if LOST_STUDS and loc.lost():
            bot.say('the hint disagrees with where it thinks it is - finding itself again')
            loc.rescatter(LOST_STUDS, LOST_BACK)
            _relocalize(bot, watch)
            best, stall = math.inf, 0
            continue
        p, feet = loc.px(), loc.feet()
        if nv.arrived(p, feet, g):
            return 'arrived'
        leg = nv.leg(p, feet, g, NAV_LEG_PX)
        if leg == 'here':
            return 'arrived'
        if leg is None:
            if relocs < RELOC_TRIES:
                relocs += 1
                loc.widen(WIDEN_STUDS)
                _relocalize(bot, watch)
                continue
            return 'unreachable'
        d, px, jump, left = leg
        if jumped is not None:
            edge, before = jumped
            jumped = None
            if left >= before - 2.0:
                nv.block(*edge)
                bot.say('that jump did not get there - trying another way')
                continue
        if jump and nv.last_edge is not None:
            jumped = (nv.last_edge, left)
        if left < best - 2.0:
            best, stall, recent = left, 0, []
        else:
            stall += 1
            if stall >= NAV_STALL:
                if blocks >= STUCK_BLOCKS or not recent:
                    return 'stuck'
                blocks += 1
                for e in set(recent):
                    nv.block(*e)
                bot.say('blocked here - going round another way')
                best, stall, recent = math.inf, 0, []
                continue
        if nv.last_edge is not None:
            recent.append(nv.last_edge)
        keys = DIRS[d] + ((' ',) if jump else ())
        ms = max(SLICE_MS / 2, px * MS_PER_PX)
        while ms > 1.0:
            chunk = min(SLICE_MS, ms)
            bot.hold(keys, chunk)
            ms -= chunk
            watch(bot)
            if watch.last is not None:
                bot.best_band = max(bot.best_band, watch.last)
                if first is None:
                    first = watch.last
            reach_out(bot)
        hotter = HOT_KEEP and first is not None and (watch.last or -1) > first
        if sl.bad[j] > (sl.bad.min() if pool is None else sl.bad[pool].min()) + RETARGET and not hotter:
            return 'ruled out'


def _tap_if_close(bot):
    h = bot.peek()
    if h is not None and h >= REACH_TIER:
        bot.stab('e')


def _press(bot, tier):
    hook, bot.on_slice = bot.on_slice, None
    try:
        return finish(bot, COLLECT_TAPS if tier >= 5 else NEAR_TAPS)
    finally:
        bot.on_slice = hook


def _collect_here(bot):
    """Walk on a little tapping E, then stand and press, then short steps round about."""
    keys = tuple(bot.held)
    bot.say('right in front of me - pressing E')
    hook, bot.on_slice = bot.on_slice, None
    try:
        if keys:
            for _ in range(REACH_PUSH_MS // 100):
                bot.hold(keys, 100)
                bot.tap('e')
                if bot.peek() is None and collected(bot):
                    raise Collected
        bot.release()
        for i in range(COLLECT_TRIES):
            bot.tap('e')
            bot.wait(COLLECT_GAP)
            if bot.peek() is None and collected(bot):
                raise Collected
            if i % 8 == 7:
                bot.tap(' ')
                for _ in range(3):
                    bot.wait(0.08)
                    bot.tap('e')
        for d in (0, 2, 4, 6):
            nudge(bot, d, 150)
            bot.release()
            for _ in range(3):
                bot.tap('e')
                bot.wait(COLLECT_GAP)
            if bot.peek() is None and collected(bot):
                raise Collected
    finally:
        bot.on_slice = hook


def _close_search(bot, watch, best_odo=None, best=None):
    """Back to where it read best, then out and back along each heading as far as that reading's ring;
    a heading that reads closer becomes the new centre."""
    best_odo = watch.best_odo if best_odo is None else best_odo
    best = watch.best if best is None else best
    bot.say('walked past it (best %s) - searching round there' % TIERS[max(0, best)])
    try:
        watch.passed = False
        watch.target = None
        if best_odo is not None:
            go_back_to(bot, best_odo)
        for _ in range(CLIMBS):
            arm = int(max(SEARCH_OUT_MS, min(SEARCH_ARM_MAX, BORD[5 - min(5, max(best, 3))] * MS_PER_PX)))
            for d in range(8):
                start = bot.odo
                watch.reset()
                watch.passed = False
                move(bot, d, arm)
                bot.release()
                if watch.best > best and watch.best_odo is not None:
                    best = watch.best
                    bot.say('reads %s that way - searching round there' % TIERS[best])
                    go_back_to(bot, watch.best_odo)
                    break
                go_back_to(bot, start)
            else:
                return
    except InReach:
        _collect_here(bot)


def probe_hunt(bot):
    """Walk probe.ahk while the belief narrows, then visit the spawns that fit. True if collected."""
    if getattr(bot, 'loc', None) is None:
        bot = Tracked(bot)
    try:
        return _probe_hunt(bot)
    finally:
        bot.on_slice = None


def _probe_hunt(bot):
    settle = settle_ms(walk_text(PROBE_AHK))
    loc = bot.loc
    sl = Shortlist(loc)
    nav_graph().restore()
    tier, pending, agree, blanks, checked = None, None, 0, 0, time.perf_counter()
    walk = bot.walk((), PROBE_AHK)
    try:
        for t in walk:
            if out_of_time():
                bot.say('out of time')
                return False
            now = time.perf_counter()
            if now - checked > 2.0:
                checked = now
                if not playing(bot):
                    raise Collected
            h = bot.peek()
            if h is None:
                blanks += 1
                if blanks >= GONE_SAMPLES and collected(bot):
                    raise Collected
                continue
            blanks = 0
            if tier is None:
                tier = h
                bot.say('probe: %s' % TIERS[tier])
            elif h != tier:
                agree = agree + 1 if h == pending else 1
                pending = h
                if agree >= (2 if abs(h - tier) == 1 else 5):
                    tier, pending, agree = h, None, 0
                    bot.say('%5.1fs  %-22s %d fit' % (t, TIERS[tier], len(sl.fits())))
            if max(tier, h) >= REACH_TIER:
                bot.stab('e')
            if tier >= COLLECT_TIER:
                bot.say('%5.1fs  %s: leaving the walk' % (t, TIERS[tier]))
                break
            if t * 1000.0 >= settle:
                pr = loc.spawn_probs()
                top = int(pr.argmax())
                if float(pr[np.hypot(*(sl.xy - sl.xy[top]).T) <= EXIT_R].sum()) >= EXIT_P:
                    bot.say('%5.1fs  belief settled: leaving the walk' % t)
                    break
            f = sl.fits()
            if len(f) == 1 or (len(f) <= 4 and np.ptp(sl.xy[f], axis=0).max() <= CLUSTER_PX):
                bot.say('%5.1fs  shortlist settled (%d fit): leaving the walk' % (t, len(f)))
                break
    finally:
        walk.close()
    where = Where()
    bot.best_band = tier if tier is not None else -1
    bot.say('shortlist: %d fit' % len(sl.fits()))

    ring = None                               # (where it read close, radius)
    tried, pressed_for, closed_for = set(), set(), set()
    if tier is not None and tier >= COLLECT_TIER:
        ring = (where.at(bot), BORD[5 - tier] + SLACK)
        if _press(bot, tier):
            return True
    watch = HintWatch()
    bot.on_slice = watch
    idle, last_visit, hot_searches = 0, -1e9, 0
    while True:
        if out_of_time():
            bot.say('out of time')
            return False
        now = bot.now()
        idle, last_visit = (idle + 1 if now - last_visit < 0.3 else 0), now
        if idle >= IDLE_VISITS:
            bot.say('not getting anywhere - stepping out')
            hook, bot.on_slice = bot.on_slice, _tap_if_close
            try:
                move(bot, int(now * 7) % 8, 800)
            finally:
                bot.on_slice = hook
                bot.release()
            loc.widen(WIDEN_STUDS)
            idle = 0
        if not playing(bot):
            raise Collected
        here = where.at(bot)
        order = [j for j in sl.order(here, ring) if j not in tried] or sl.order(here, ring)
        j = order[0]
        goal = sl.xy[j]
        if (ring is not None and ring[1] <= BORD[1] + SLACK and hot_searches < HOT_SEARCHES
                and np.hypot(*(goal - ring[0])) > ring[1]):
            hot_searches += 1
            bot.say('nothing fits where it read close - searching there instead of walking off')
            watch.reset()
            _close_search(bot, watch, tuple(np.asarray(ring[0], float) * MS_PER_PX),
                          5 if ring[1] <= BORD[0] + SLACK else 4)
            continue
        bot.say('-> spawn %d  (%d fit, %.0f px%s)' % (j, len(sl.fits()), np.hypot(*(goal - here)),
                                                        ', inside the ring' if ring else ''))
        tier, reason = -1, 'arrived'
        watch.reset()
        watch.skip_inreach = j in pressed_for
        watch.target, watch.skip_close = goal, j in closed_for
        try:
            reason = _nav_to(bot, sl, j, watch, order)
        except (InReach, Passed, Close) as e:
            bot.release()
            best = watch.best
            if isinstance(e, InReach):
                _collect_here(bot)
                pressed_for.add(j)
                best = 5
            elif isinstance(e, Passed):
                _close_search(bot, watch)
            else:
                closed_for.add(j)
                bot.say('reads %s here, short of the target - searching here instead' % TIERS[best])
            tier = max(tier, best)
            r = BORD[5 - tier] + SLACK if tier >= 1 else None
            if r is not None and (ring is None or r < ring[1] or (isinstance(e, Close) and r <= ring[1])):
                ring = (where.at(bot), r)
            reason = 'searched'
            watch.reset()
        tier = max(tier, bot.best_band)
        if watch.best >= RING_TIER and watch.best_odo is not None:
            r = BORD[5 - watch.best] + SLACK
            if ring is None or r < ring[1]:
                ring = (np.array(watch.best_odo, float) / MS_PER_PX, r)
        if reason in ('stuck', 'unreachable'):
            tried.add(j)
            sl.penalty[j] += TRIED / 2
        if reason == 'arrived':
            if _press(bot, max(tier, COLLECT_TIER)):
                return True
            loc.widen(WIDEN_STUDS)
            sl.penalty[j] += TRIED
            tried.add(j)


# ================================================================ the walks
WALKS = {
    'probe.ahk': """RunPath()
{
    Send, {w Down}
    Send, {d Down}
    Sleep, 650
    Send, {w Up}
    Send, {d Up}
    Sleep, 30
    Send, {d Down}
    Sleep, 4467
    Send, {d Up}
    Sleep, 30
    Send, {s Down}
    Send, {d Down}
    Sleep, 1012
    Send, {s Up}
    Send, {d Up}
    Sleep, 30
    ; aligned 580.2 322.1
    Send, {a Down}
    Sleep, 7400
    Send, {a Up}
    Sleep, 30
    Send, {s Down}
    Send, {a Down}
    Sleep, 1306
    Send, {s Up}
    Send, {a Up}
    Sleep, 30
    Send, {s Down}
    Send, {a Down}
    Sleep, 435
    Send, {s Up}
    Send, {a Up}
    Sleep, 30
    Send, {s Down}
    Send, {a Down}
    Sleep, 435
    Send, {s Up}
    Send, {a Up}
    Sleep, 30
    Send, {s Down}
    Sleep, 2164
    Send, {Space Down}
    Sleep, 400
    Send, {Space Up}
    Sleep, 523
    Send, {s Up}
    Sleep, 30
    Send, {s Down}
    Send, {a Down}
    Sleep, 1306
    Send, {s Up}
    Send, {a Up}
    Sleep, 30
    Send, {d Down}
    Sleep, 4256
    Send, {Space Down}
    Sleep, 1200
    Send, {Space Up}
    Sleep, 203
    Send, {d Up}
    Sleep, 30
    Send, {s Down}
    Send, {d Down}
    Send, {Space Down}
    Sleep, 395
    Send, {Space Up}
    Sleep, 1467
    Send, {Space Down}
    Sleep, 400
    Send, {Space Up}
    Sleep, 1656
    Send, {s Up}
    Send, {d Up}
    Sleep, 30
    Send, {d Down}
    Sleep, 435
    Send, {d Up}
    Sleep, 30
    Send, {s Down}
    Send, {d Down}
    Sleep, 2177
    Send, {s Up}
    Send, {d Up}
    Sleep, 30
    Send, {d Down}
    Sleep, 435
    Send, {d Up}
    Sleep, 30
    Send, {d Down}
    Sleep, 435
    Send, {d Up}
    Sleep, 30
}
""",
    'lime_path.ahk': """RunPath() {
    Send, {s Down}
    Sleep, 400
    Send, {d Down}
    Sleep, 3500
    Send, {d Up}
    Sleep 1100
    Send, {s Up}
}
""",
    'lime_path_abyssal.ahk': """RunPath() {
    Send, {s Down}
    Sleep, 270
    Send, {d Down}
    Sleep, 2250
    Send, {d Up}
    Sleep 730
    Send, {s Up}
}
""",
}

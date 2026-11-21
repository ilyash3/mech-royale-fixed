"""Ground navigation for Mech Royale: a 4 m walk grid built from /arena at hello, and time-sliced A*.

Standard library only. Built once per match (well under a second); each A* call is capped by an
expansion budget so a decision never comes near the 50 ms limit. A search that runs out of budget
continues on the next decision.
"""
import array
import heapq
import json
import math
import os

CELL = 4.0          # grid cell size, m
INFLATE = 2.2       # walls are grown by about a mech radius so paths keep clear of them
MAX_RISE = 3.6      # max ground height change between neighbouring cells (about the 50 deg slope limit over 4 m)
SQ2 = math.sqrt(2.0)


class NavGrid:
    def __init__(self, map_dir):
        w = json.load(open(os.path.join(map_dir, "v2", "world.json")))
        t = w["terrain"]
        self.tminx, self.tminy, self.tstep = t["minX"], t["minY"], t["step"]
        self.tnx, self.tny = t["nx"], t["ny"]
        scale = t.get("scale", 0.01)
        raw = array.array("h")
        with open(os.path.join(map_dir, "v2", t.get("file", "terrain.i16")), "rb") as f:
            raw.frombytes(f.read())
        if array.array("h", [1]).tobytes() != b"\x01\x00":
            raw.byteswap()  # file is little-endian
        self.terr, self.tscale = raw, scale

        b = w["bounds"]
        self.minx, self.miny = b["minX"], b["minY"]
        self.nx = int((b["maxX"] - b["minX"]) / CELL) + 1
        self.ny = int((b["maxY"] - b["minY"]) / CELL) + 1
        n = self.nx * self.ny
        # ground height per cell centre
        self.h = array.array("f", [0.0]) * n
        for j in range(self.ny):
            cy = self.miny + (j + 0.5) * CELL
            for i in range(self.nx):
                self.h[j * self.nx + i] = self.ground(self.minx + (i + 0.5) * CELL, cy)
        self.blocked = bytearray(n)
        for p in w["prisms"]:
            if p["kind"] == "bridge":
                self._bridge(p)
            else:
                self._raster(p)

    # ------------------------------------------------------------------ map
    def ground(self, x, y):
        fx = (x - self.tminx) / self.tstep
        fy = (y - self.tminy) / self.tstep
        i = min(max(int(fx), 0), self.tnx - 2)
        j = min(max(int(fy), 0), self.tny - 2)
        ax, ay = min(max(fx - i, 0.0), 1.0), min(max(fy - j, 0.0), 1.0)
        k = j * self.tnx + i
        t = self.terr
        h00, h10, h01, h11 = t[k], t[k + 1], t[k + self.tnx], t[k + self.tnx + 1]
        return ((h00 * (1 - ax) + h10 * ax) * (1 - ay) + (h01 * (1 - ax) + h11 * ax) * ay) * self.tscale

    def _spans(self, ring, yv):
        """x intervals where the polygon ring covers the horizontal line y = yv."""
        xs = []
        m = len(ring) // 2
        for a in range(m):
            x1, y1 = ring[2 * a], ring[2 * a + 1]
            x2, y2 = ring[2 * ((a + 1) % m)], ring[2 * ((a + 1) % m) + 1]
            if (y1 <= yv < y2) or (y2 <= yv < y1):
                xs.append(x1 + (yv - y1) * (x2 - x1) / (y2 - y1))
        xs.sort()
        return [(xs[k], xs[k + 1]) for k in range(0, len(xs) - 1, 2)]

    def _covered_cells(self, p, grow):
        """Yield cell indices whose centre lies within `grow` m of the prism footprint (approx)."""
        ring = p["ring"]
        ys = ring[1::2]
        j0 = max(0, int((min(ys) - grow - self.miny) / CELL))
        j1 = min(self.ny - 1, int((max(ys) + grow - self.miny) / CELL))
        holes = p.get("holes") or []
        for j in range(j0, j1 + 1):
            cy = self.miny + (j + 0.5) * CELL
            cells = set()
            for yv in (cy - grow, cy, cy + grow):
                spans = self._spans(ring, yv)
                if not spans:
                    continue
                if holes:  # cut hole spans out (holes are not grown)
                    hs = []
                    for hr in holes:
                        hs += self._spans(hr, yv)
                    cut = []
                    for a, b in spans:
                        segs = [(a, b)]
                        for ha, hb in hs:
                            nxt = []
                            for sa, sb in segs:
                                if hb <= sa or ha >= sb:
                                    nxt.append((sa, sb))
                                else:
                                    if ha > sa:
                                        nxt.append((sa, ha))
                                    if hb < sb:
                                        nxt.append((hb, sb))
                            segs = nxt
                        cut += segs
                    spans = cut
                for a, b in spans:
                    i0 = max(0, int((a - grow - self.minx) / CELL))
                    i1 = min(self.nx - 1, int((b + grow - self.minx) / CELL))
                    for i in range(i0, i1 + 1):
                        cells.add(j * self.nx + i)
            for k in cells:
                yield k

    def _raster(self, p):
        for k in self._covered_cells(p, INFLATE):
            g = self.h[k]
            # Solid if it rises above a step and is not an overhead you can walk under (9 m head room).
            if p["top"] > g + 1.2 and p["base"] < g + 9.0:
                self.blocked[k] = 1

    def _bridge(self, p):
        for k in self._covered_cells(p, 0.0):
            if p["top"] > self.h[k]:
                self.h[k] = p["top"]  # walk on the deck

    # ------------------------------------------------------------------ grid helpers
    def cell(self, x, y):
        i = int((x - self.minx) / CELL)
        j = int((y - self.miny) / CELL)
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return j * self.nx + i
        return None

    def centre(self, k):
        return (self.minx + (k % self.nx + 0.5) * CELL, self.miny + (k // self.nx + 0.5) * CELL)

    def free_cell(self, k):
        return k is not None and not self.blocked[k]

    def nearest_free(self, k, rad=4):
        if k is None:
            return None
        if not self.blocked[k]:
            return k
        i0, j0 = k % self.nx, k // self.nx
        for r in range(1, rad + 1):
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    i, j = i0 + di, j0 + dj
                    if 0 <= i < self.nx and 0 <= j < self.ny and not self.blocked[j * self.nx + i]:
                        return j * self.nx + i
        return None


class AStar:
    """Resumable A*: call step(budget) every decision until it returns a path (list of cells) or []."""

    def __init__(self, grid, start, goal):
        self.g_, self.start, self.goal = grid, start, goal
        self.open = [(0.0, start)]
        self.gcost = {start: 0.0}
        self.came = {start: None}
        self.closed = set()
        self.done = None
        gi, gj = goal % grid.nx, goal // grid.nx
        self.gi, self.gj = gi, gj
        self.best, self.best_h = start, self.h(start)

    def h(self, k):
        nx = self.g_.nx
        di, dj = abs(k % nx - self.gi), abs(k // nx - self.gj)
        return (max(di, dj) + (SQ2 - 1) * min(di, dj)) * CELL

    def step(self, budget):
        if self.done is not None:
            return self.done
        G = self.g_
        nx, ny, bl, hh = G.nx, G.ny, G.blocked, G.h
        openl, gcost, came, closed = self.open, self.gcost, self.came, self.closed
        n = 0
        while openl and n < budget:
            _, k = heapq.heappop(openl)
            if k in closed:
                continue
            closed.add(k)
            n += 1
            if k == self.goal:
                self.done = self._path(k)
                return self.done
            hk = self.h(k)
            if hk < self.best_h:
                self.best, self.best_h = k, hk
            i, j = k % nx, k // nx
            gk = gcost[k]
            for di, dj, c in ((1, 0, CELL), (-1, 0, CELL), (0, 1, CELL), (0, -1, CELL),
                              (1, 1, CELL * SQ2), (1, -1, CELL * SQ2), (-1, 1, CELL * SQ2), (-1, -1, CELL * SQ2)):
                ii, jj = i + di, j + dj
                if not (0 <= ii < nx and 0 <= jj < ny):
                    continue
                q = jj * nx + ii
                if bl[q] or q in closed:
                    continue
                if di and dj and (bl[j * nx + ii] or bl[jj * nx + i]):
                    continue  # no corner cutting
                if abs(hh[q] - hh[k]) > MAX_RISE * (SQ2 if di and dj else 1.0):
                    continue  # too steep
                ng = gk + c
                if ng < gcost.get(q, 1e18):
                    gcost[q] = ng
                    came[q] = k
                    heapq.heappush(openl, (ng + self.h(q), q))
        if not openl:
            # Goal unreachable: head for the closest cell we found.
            self.done = self._path(self.best) if self.best != self.start else []
        return self.done

    def _path(self, k):
        out = []
        while k is not None:
            out.append(k)
            k = self.came[k]
        out.reverse()
        return out


class Navigator:
    """Keeps one route to a goal, replans when the goal moves or we drift off, and gives a heading."""

    def __init__(self, grid):
        self.G = grid
        self.goal = None
        self.search = None
        self.path = None
        self.idx = 0
        self.plan_t = -99.0

    def heading(self, x, y, gx, gy, t, budget=1500):
        G = self.G
        if self.goal is None or math.hypot(gx - self.goal[0], gy - self.goal[1]) > 12 or t - self.plan_t > 4.0:
            s = G.nearest_free(G.cell(x, y))
            g = G.nearest_free(G.cell(gx, gy), 6)
            if s is None or g is None:
                self.search = None
                self.path = None
                return None
            self.goal = (gx, gy)
            self.search = AStar(G, s, g)
            self.plan_t = t
            self.path, self.idx = None, 0
        if self.path is None and self.search is not None:
            res = self.search.step(budget)
            if res is not None:
                self.path, self.idx = res, 0
                self.search = None
        if not self.path:
            return None
        # advance along the path; aim a few cells ahead
        best_i, best_d = self.idx, 1e9
        for i in range(self.idx, min(len(self.path), self.idx + 12)):
            cx, cy = G.centre(self.path[i])
            d = math.hypot(cx - x, cy - y)
            if d < best_d:
                best_i, best_d = i, d
        if best_d > 14:  # drifted off the route: replan next time
            self.plan_t = -99.0
        self.idx = best_i
        look = min(len(self.path) - 1, self.idx + 3)
        cx, cy = G.centre(self.path[look])
        if look == len(self.path) - 1 and math.hypot(gx - cx, gy - cy) < CELL * 2:
            cx, cy = gx, gy
        if math.hypot(cx - x, cy - y) < 1.0:
            return None
        return math.atan2(cy - y, cx - x)

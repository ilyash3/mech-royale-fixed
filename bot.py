"""Frozen measured v1 strategy; selected when v2 experiments underperform."""
"""Mech Royale bot v1: survival-first. Zone-aware, wall-aware steering, lead-aimed gun, laser/lightning/mortar use."""
import json
import math
import random
import mechfight as mf

try:
    import nav
except Exception:  # keep the bot running even if nav.py is missing
    nav = None

TAU = math.tau
PICK_PRIORITY = ["plating", "armor", "nanites", "gun_damage", "gun_rate", "laser_damage", "laser_charge",
                 "servos", "gun_mag", "lightning_efficiency", "lightning_chain", "gyros", "reactor",
                 "capacitor", "gun_velocity", "sensors", "mortar_reload", "mortar_splash", "jets",
                 "lightning_range", "laser_pierce", "mortar_velocity"]
# Mortar is never taken: holding one makes enemy laser hits do +60% (cook-off).
WEAPON_RANK = {"laser": 3, "lightning": 2}
BRAWLER_RANK = {"lightning": 3, "laser": 2}
OFFENSE_PRIORITY = ["laser_charge", "laser_damage", "lightning_efficiency", "lightning_chain", "gun_rate",
                    "gun_damage", "plating", "armor", "gyros", "nanites", "lightning_range", "gun_mag",
                    "gun_velocity", "servos", "reactor", "capacitor", "laser_pierce", "sensors", "jets",
                    "mortar_reload", "mortar_splash", "mortar_velocity"]


def wrank(enemy_seen=None):
    if STYLE == "brawler":
        return BRAWLER_RANK
    if STYLE == "adaptive" and enemy_seen:
        # Counter what the field carries: lightning beats laser, laser beats mortar.
        tot = sum(v for k, v in enemy_seen.items() if k != "gun")  # everyone has a gun
        if tot < 2:
            return dict(WEAPON_RANK)
        laser_share = enemy_seen.get("laser", 0) / tot
        return {"lightning": 3.5, "laser": 3} if laser_share > 0.45 else dict(WEAPON_RANK)
    return WEAPON_RANK


def weapon_dps(kind, dist):
    """Rough sustained damage per second of one weapon at a distance (before cubes and armor)."""
    if kind == "gun":
        return 100.0 if dist <= 215 else 0.0
    if kind == "laser":
        return 130.0 if dist <= 280 else 0.0
    if kind == "lightning":
        return 240.0 if dist <= 45 else 0.0
    if kind == "mortar":
        return 75.0 if 12 < dist <= 235 else 0.0
    return 0.0


def matchup_range(ours, theirs, default):
    """Preferred distance against one enemy, from the weapon triangle. Returns (range, close_fast)."""
    if "lightning" in ours and "laser" in theirs and "lightning" not in theirs:
        return 26.0, True    # get inside arc range: every hit shorts their laser
    if "lightning" in theirs and "lightning" not in ours:
        return 75.0, False   # stay outside their 45 m arc
    if "laser" in ours and "mortar" in theirs:
        return 120.0, False  # laser outranges the shell, and cook-off adds +60%
    if "lightning" in ours and "laser" not in ours:
        return 26.0, True
    return default, False

# Feature switches (set to False to turn a behavior off).
PEEK_FIGHT = False     # duck behind cover while the laser cools down, charge there, peek out to fire
EARLY_LOOT = True      # remember seen weapons; go far for a laser/lightning in the first 100 s
LETHAL_RELEASE = True  # fire a partly charged laser when it already kills the target
DROPS_ANY_HP = True    # go for uncontested supply drops from farther away, before loot and cubes
HUNT = False           # when healthy, walk toward visible mechs (up to 230 m) and recent nearby gunfire
HUNT_HP = 0.7          # only hunt above this HP fraction
NPC_FARM = True       # early on, go to drones (2 cubes each) and shoot crates (1 cube, maybe a weapon)
NPC_FARM_UNTIL = 120.0  # seconds of match time to farm
FLEE_COUNT = 3         # retreat when this many mechs are within 100 m
FIGHT_RANGE = 70.0     # preferred distance to the target with a laser or gun
STYLE = "default"      # "default", "brawler" (lightning + laser, drop the gun, fight close), "sniper" (fight far),
                       # "adaptive" (range and loot chosen from our and the enemy's weapons)
NAV = False             # plan routes around buildings (A* on a 4 m grid) for zone, loot, drops, hunting
LASER_DODGE = True    # sidestep when a visible enemy is charging a laser aimed at us
LASER_COVER = False    # instead of sidestepping, run behind the nearest wall (within 16 m) to break its line of sight
FINISH_KILL = False    # focus the most-damaged enemy and chase a hurt one for a few seconds when it breaks sight
ARM_FIRST = False      # gun only and not being hit: keep heading for a laser/lightning (up to 300 m) instead of strafing
RELOCATE = False       # after a kill, move about 60 m away from the fight spot for 8 s
LOS_BLOCK_TOP = 3.0    # a building blocks our line of sight only if its top is this far above our feet (muzzle ~7 m)
GUN_TOL_MULT = 4.0     # multiply the aim tolerance for firing (higher = shoot more freely)
CANARY_NO_MECH_FIRE = False  # DIAGNOSTIC: never fire at enemy mechs (drones/crates still shot)
RECOVER_HP = 0.33      # retreat and heal below this HP fraction (0 = never)
RECOVER_EXIT = 0.85    # ...until back above this
FIGHT_CALC = False     # stay or leave each fight by comparing time-to-kill (ours vs. everyone shooting at us)
STAY_RATIO = 1.0       # stay while (their time to kill us) / (our time to kill target) >= this
LEAVE_COVER_DIST = 16.0  # when losing, run to cover only if a wall is this close
NO_COVER_ACTION = "fight"  # losing with no cover close: "fight" (keep shooting) or "flee" (old retreat)
UPGRADES = "defense"   # "defense" (plating/armor first) or "offense" (weapon damage and rate first)


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def seg_hit(px, py, dx, dy, ax, ay, bx, by):
    """Distance t along ray (px,py)+t*(dx,dy) (|d|=1) to segment ab, or None."""
    ex, ey = bx - ax, by - ay
    den = dx * ey - dy * ex
    if abs(den) < 1e-9:
        return None
    wx, wy = ax - px, ay - py
    t = (wx * ey - wy * ex) / den
    u = (wx * dy - wy * dx) / den
    if t >= 0 and 0 <= u <= 1:
        return t
    return None


class World:
    def __init__(self, path):
        self.pr = []  # (minx,miny,maxx,maxy,base,top,edges)
        try:
            w = json.load(open(path + "/v2/world.json"))
            for p in w["prisms"]:
                edges = []
                for ring in [p["ring"]] + list(p.get("holes") or []):
                    n = len(ring) // 2
                    for i in range(n):
                        j = (i + 1) % n
                        edges.append((ring[2 * i], ring[2 * i + 1], ring[2 * j], ring[2 * j + 1]))
                xs = p["ring"][0::2]
                ys = p["ring"][1::2]
                self.pr.append((min(xs), min(ys), max(xs), max(ys), p["base"], p["top"], edges))
        except Exception as e:  # map is optional
            mf.log("map load failed", e)
        self.near = []

    def refresh(self, x, y, z, r=90):
        self.near = [p for p in self.pr
                     if p[0] - r < x < p[2] + r and p[1] - r < y < p[3] + r and p[4] < z + 9 and p[5] > z + 1.5]

    def free(self, x, y, ang, maxd, lat=2.6):
        """Free distance along heading ang (checks centre + both sides)."""
        dx, dy = math.cos(ang), math.sin(ang)
        best = maxd
        for off in (0.0, lat, -lat):
            px, py = x - dy * off, y + dx * off
            for p in self.near:
                if p[0] - maxd > px or px > p[2] + maxd or p[1] - maxd > py or py > p[3] + maxd:
                    continue
                for e in p[6]:
                    t = seg_hit(px, py, dx, dy, *e)
                    if t is not None and t < best:
                        best = t
        return best

    def los(self, x, y, tx, ty, z):
        d = math.hypot(tx - x, ty - y)
        if d < 1:
            return True
        ang = math.atan2(ty - y, tx - x)
        dx, dy = math.cos(ang), math.sin(ang)
        for p in self.pr:
            if p[4] > z + 9 or p[5] < z + LOS_BLOCK_TOP:
                continue
            if max(x, tx) < p[0] or min(x, tx) > p[2] or max(y, ty) < p[1] or min(y, ty) > p[3]:
                continue
            for e in p[6]:
                t = seg_hit(x, y, dx, dy, *e)
                if t is not None and t < d:
                    return False
        return True


class Bot:
    name = "survivor-v1"

    def __init__(self, hello):
        cfg = hello["config"]
        self.cfg = cfg
        self.rng = random.Random(hello.get("seed", 1))
        self.world = World(hello["map"]["dir"])
        self.nav = None
        if NAV and nav is not None:
            try:
                self.nav = nav.Navigator(nav.NavGrid(hello["map"]["dir"]))
            except Exception as e:
                mf.log("nav build failed", e)
        self.strafe = 1
        self.strafe_until = 0
        self.last_dir = None
        self.stuck = 0
        self.escape_until = -1
        self.escape_dir = 0.0
        self.prev_pos = None
        self.scan = 0.0
        self.sound_bearing = None
        self.sound_t = -99
        self.laser_held = False
        self.dmg_t = -99.0
        self.dmg_bearing = None
        self.recover = False  # low HP: break contact and regenerate before fighting again
        self.reloc = None       # (goal x, goal y, until t): where to move after a kill
        self.fight_r = None     # FIGHT_CALC: last time-to-kill ratio (their TTK / our TTK)
        self.focus_id = None    # the enemy mech we are trying to finish
        self.focus_last = None  # (pos, t, hp_frac) where we last knew it
        self.seen_pickups = {}  # id -> (kind, pos) of weapons seen on the ground
        self.enemy_seen = {}    # weapon kind -> number of distinct enemy mechs seen carrying it
        self.enemy_seen_ids = set()
        self.cover = None       # cached cover point [x, y] against the current target
        self.cover_t = -99.0
        self.cover_tid = None

    # ---------------------------------------------------------------- helpers
    def zone_now(self, z, t):
        if t <= z["shrink_start"]:
            return z["center"], z["radius"]
        if t >= z["shrink_end"]:
            return z["next_center"], z["next_radius"]
        f = (t - z["shrink_start"]) / max(1e-6, z["shrink_end"] - z["shrink_start"])
        c = [z["center"][i] + (z["next_center"][i] - z["center"][i]) * f for i in (0, 1)]
        return c, z["radius"] + (z["next_radius"] - z["radius"]) * f

    def lead(self, src, tpos, tvel, speed):
        r = [tpos[i] - src[i] for i in range(3)]
        a = tvel[0] ** 2 + tvel[1] ** 2 + tvel[2] ** 2 - speed ** 2
        b = 2 * (r[0] * tvel[0] + r[1] * tvel[1] + r[2] * tvel[2])
        c = r[0] ** 2 + r[1] ** 2 + r[2] ** 2
        t = None
        if abs(a) < 1e-6:
            if abs(b) > 1e-6:
                t = -c / b
        else:
            disc = b * b - 4 * a * c
            if disc >= 0:
                s = math.sqrt(disc)
                ts = [x for x in ((-b - s) / (2 * a), (-b + s) / (2 * a)) if x > 0]
                if ts:
                    t = min(ts)
        if t is None or t <= 0 or t > 2:
            t = math.sqrt(c) / speed
        return [tpos[i] + tvel[i] * t for i in range(3)]

    def angles(self, src, dst):
        dx, dy, dz = dst[0] - src[0], dst[1] - src[1], dst[2] - src[2]
        return math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))

    def mortar_aim(self, src, tpos, tvel, speed, g):
        p = list(tpos)
        for _ in range(3):
            R = math.hypot(p[0] - src[0], p[1] - src[1])
            dz = p[2] - src[2]
            v2 = speed * speed
            disc = v2 * v2 - g * (g * R * R + 2 * dz * v2)
            if disc < 0:
                return None
            pitch = math.atan((v2 - math.sqrt(disc)) / (g * R)) if R > 1 else 0.7
            tof = R / (speed * math.cos(pitch))
            p = [tpos[0] + tvel[0] * tof, tpos[1] + tvel[1] * tof, tpos[2]]
        yaw = math.atan2(p[1] - src[1], p[0] - src[0])
        return yaw, pitch

    def steer(self, x, y, want):
        """Pick the heading closest to `want` with room ahead."""
        look = 22.0
        order = [0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6, 7, -7, 8]
        if self.last_dir is not None and self.last_dir < 0:
            order = [0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5, -6, 6, -7, 7, 8]
        bestk, bestfr = 0, -1.0
        for k in order:
            fr = self.world.free(x, y, want + k * 0.35, look)
            if fr >= look - 0.5:
                if k:
                    self.last_dir = k
                return want + k * 0.35
            if fr > bestfr:
                bestk, bestfr = k, fr
        return want + bestk * 0.35

    def find_cover(self, x, y, z, tx, ty, tid, t, zc, zr):
        """A nearby reachable point, inside the zone, with no line of sight to (tx, ty). Cached 0.6 s."""
        if tid == self.cover_tid and t - self.cover_t < 0.6:
            return self.cover
        best, best_s = None, 1e9
        away = math.atan2(y - ty, x - tx)
        for k in range(12):
            a = k * TAU / 12
            for dist in (7.0, 13.0, 20.0):
                cx, cy = x + math.cos(a) * dist, y + math.sin(a) * dist
                if math.hypot(cx - zc[0], cy - zc[1]) > zr - 8:
                    continue
                if self.world.free(x, y, a, dist + 3) < dist + 2.5:
                    continue  # path to it is blocked
                if self.world.los(cx, cy, tx, ty, z):
                    continue  # still exposed there
                # Prefer short moves that do not walk toward the enemy.
                s = dist + 6.0 * abs(wrap(a - away))
                if s < best_s:
                    best, best_s = [cx, cy], s
                break  # nearest blocked distance on this heading is enough
        self.cover, self.cover_t, self.cover_tid = best, t, tid
        return best

    # ---------------------------------------------------------------- main
    def act(self, obs):
        me = obs["self"]
        x, y, z = me["pos"]
        t = obs["t"]
        tick = obs["tick"]
        act = {"tick": tick}
        stats = me["stats"]
        hp_frac = me["hp"] / max(1.0, me["max_hp"])
        self.world.refresh(x, y, z)
        scale = 1 + 0.05 * me["cubes"]
        muzzle_h = stats.get("muzzle_height", 7 * scale)

        # offer
        if obs["offer"]:
            ids = [c["id"] for c in obs["offer"]["choices"]]
            prio = OFFENSE_PRIORITY if UPGRADES == "offense" else PICK_PRIORITY
            pick = min(range(len(ids)), key=lambda i: prio.index(ids[i]) if ids[i] in prio else 99)
            act["pick"] = pick

        for ev in obs.get("events", []):
            if ev.get("type") == "hit" and isinstance(ev.get("target"), int) and ev["target"] < 16:
                self.focus_id = ev["target"]
            if RELOCATE and ev.get("type") == "kill":
                ax, ay = 0.0, 0.0
                for m in obs["mechs"]:
                    if m["kind"] == "mech" and m["id"] != ev.get("target"):
                        dm = max(1.0, math.hypot(m["pos"][0] - me["pos"][0], m["pos"][1] - me["pos"][1]))
                        ax -= (m["pos"][0] - me["pos"][0]) / dm
                        ay -= (m["pos"][1] - me["pos"][1]) / dm
                zc0 = obs["zone"].get("next_center") or obs["zone"]["center"]
                dz = max(1.0, math.hypot(zc0[0] - me["pos"][0], zc0[1] - me["pos"][1]))
                ax += 0.6 * (zc0[0] - me["pos"][0]) / dz
                ay += 0.6 * (zc0[1] - me["pos"][1]) / dz
                n = max(1e-6, math.hypot(ax, ay))
                self.reloc = (me["pos"][0] + 60 * ax / n, me["pos"][1] + 60 * ay / n, obs["t"] + 8.0)
            if ev.get("type") == "kill" and ev.get("target") == self.focus_id:
                self.focus_id, self.focus_last = None, None
            if ev.get("type") == "damaged" and ev.get("weapon") != "zone" and ev.get("bearing") is not None:
                self.dmg_t = t
                self.dmg_bearing = ev["bearing"]

        # stuck detection
        speed = math.hypot(me["vel"][0], me["vel"][1])

        # sounds
        for s in obs["sounds"]:
            if s["kind"] in ("gun", "mortar", "explosion", "laser", "lightning", "step", "boost", "npc_gun") and s["dist"] != "far":
                self.sound_bearing = s["bearing"]
                self.sound_t = t

        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        d2 = lambda p: math.hypot(p[0] - x, p[1] - y)
        mechs.sort(key=lambda m: d2(m["pos"]) - (60 if m.get("hp", 1400) < 400 else 0))
        close = [m for m in mechs if d2(m["pos"]) < 110]

        # Recovery: enter below 33% HP, leave once regenerated to 85%.
        if hp_frac < RECOVER_HP:
            self.recover = True
        elif hp_frac > RECOVER_EXIT or RECOVER_HP <= 0:
            self.recover = False

        zc, zr = self.zone_now(obs["zone"], t)
        nzc, nzr = obs["zone"]["next_center"], obs["zone"]["next_radius"]
        dz_center = math.hypot(x - zc[0], y - zc[1])
        dn_center = math.hypot(x - nzc[0], y - nzc[1])

        # ---------------- weapons
        wk = [w["kind"] if w else None for w in me["weapons"]]

        def slot(kind):
            return wk.index(kind) if kind in wk else None

        # remember weapons on the ground; forget ones that are gone when we are close enough to see
        cur_ids = set()
        for p in obs["pickups"]:
            cur_ids.add(p["id"])
            self.seen_pickups[p["id"]] = (p["kind"], p["pos"])
        for ev in obs.get("events", []):
            if ev.get("type") == "took":
                self.seen_pickups.pop(ev.get("id"), None)
        for pid, (_, ppos) in list(self.seen_pickups.items()):
            if pid not in cur_ids and math.hypot(ppos[0] - x, ppos[1] - y) < 25:
                del self.seen_pickups[pid]

        # pickups
        reach = self.cfg["loot"]["pickup_radius"] - 0.6
        near_pick = [p for p in obs["pickups"] if math.hypot(p["pos"][0] - x, p["pos"][1] - y) < reach and abs(p["pos"][2] - z) < 3.5]
        for m in mechs:
            for wkind in m.get("weapons") or []:
                if (m["id"], wkind) not in self.enemy_seen_ids:
                    self.enemy_seen_ids.add((m["id"], wkind))
                    self.enemy_seen[wkind] = self.enemy_seen.get(wkind, 0) + 1
        rank = wrank(self.enemy_seen)
        for p in sorted(near_pick, key=lambda p: -rank.get(p["kind"], 0)):
            if p["kind"] not in rank or p["kind"] in wk:
                continue
            if wk[1] is None:
                act["take"] = p["id"]
                break
            if wk[0] is None:
                act["take"] = p["id"]
                break
            if STYLE in ("brawler", "adaptive") and "gun" in wk:
                # Swap the gun out to carry laser + lightning.
                act["take"] = p["id"]
                act["drop"] = wk.index("gun")
                break
            have = rank.get(wk[1], 0)
            if wk[1] and rank.get(p["kind"], 0) > have:
                act["take"] = p["id"]
                act["drop"] = 1
                break

        # ---------------- target & fire
        target = None
        fire = [False, False]
        aim = None
        if mechs:
            target = mechs[0]
            if FINISH_KILL:
                def tscore(m):
                    hpf = m.get("hp", 1400) / max(1.0, m.get("max_hp", 1400))
                    return d2(m["pos"]) - 120.0 * (1.0 - hpf) - (60.0 if m["id"] == self.focus_id else 0.0)
                target = min(mechs, key=tscore)
        if FINISH_KILL and self.focus_id is not None:
            for m in mechs:
                if m["id"] == self.focus_id:
                    self.focus_last = (m["pos"], obs["t"], m.get("hp", 1400) / max(1.0, m.get("max_hp", 1400)))
        if target is None and npcs and hp_frac > 0.6:
            n0 = min(npcs, key=lambda m: d2(m["pos"]))
            if d2(n0["pos"]) < 70:
                target = n0
        if NPC_FARM and target is None and hp_frac > 0.6 and obs["t"] < NPC_FARM_UNTIL:
            # Shoot a nearby crate: treat it as a still, low target.
            crs = [c for c in obs["crates"] if d2(c["pos"]) < 60 and self.world.los(x, y, c["pos"][0], c["pos"][1], z)]
            if crs:
                c0 = min(crs, key=lambda c: d2(c["pos"]))
                target = {"id": c0["id"], "kind": "crate", "pos": c0["pos"], "vel": [0.0, 0.0, 0.0], "hp": c0["hp"]}
        tgt_d = None
        visible_los = False
        laser_cd = None       # laser cooldown left, if we hold a laser
        laser_charged = False  # laser fully charged and held
        if target is not None:
            tp = target["pos"]
            ts = 1 + 0.05 * target.get("cubes", 0) if target["kind"] == "mech" else 0.4
            tcenter = [tp[0], tp[1], tp[2] + 4.5 * ts]
            tgt_d = d2(tp)
            src = [x, y, z + muzzle_h]
            visible_los = self.world.los(x, y, tp[0], tp[1], z)
            g = slot("gun")
            lz = slot("laser")
            lt = slot("lightning")
            mo = slot("mortar")
            gun_speed = me["weapons"][g]["stats"]["speed"] if g is not None else 252
            lead_pt = self.lead(src, tcenter, target["vel"], gun_speed)
            yaw_l, pit_l = self.angles(src, lead_pt)
            yaw_d, pit_d = self.angles(src, tcenter)
            aim = [yaw_l, pit_l]
            tol = max(0.025, min(0.14, 2.2 / max(5.0, tgt_d)))
            tol *= GUN_TOL_MULT  # how loose an aim still counts as 'on target'
            cur_yaw, cur_pit = me["aim"]
            err_l = abs(wrap(yaw_l - cur_yaw))
            charging_laser = lz is not None and (me["weapons"][lz].get("charge", 0) > 0)

            laser_ready = False
            if lz is not None:
                w = me["weapons"][lz]
                laser_cd = w["cooldown"]
                # With PEEK_FIGHT the laser may start charging behind cover (the target's position
                # is still tracked for memory_s after it leaves view).
                can_start = w["cooldown"] <= 0.05 and tgt_d < 230 and (
                    visible_los or (PEEK_FIGHT and target["kind"] == "mech" and tgt_d < 140))
                laser_ready = charging_laser or can_start
            if laser_ready:
                lcfg = self.cfg["weapons"]["laser"]
                aim = [yaw_d, pit_d]  # hitscan: no lead
                err = abs(wrap(yaw_d - cur_yaw))
                ch = w.get("charge", 0)
                # Full charge comes from this weapon's stats (after laser_charge upgrades), not the
                # base config. obs rounds charge to 0.01, so allow a small margin below full.
                full_charge = w.get("stats", {}).get("charge", lcfg["charge"])
                full = max(lcfg.get("min_charge", 0.15), full_charge - 0.015)
                laser_charged = ch >= full
                lethal = False
                if LETHAL_RELEASE and ch >= lcfg.get("min_charge", 0.15) and target.get("hp") is not None:
                    mfrac = lcfg.get("min_frac", 0.35)
                    frac = mfrac + (1 - mfrac) * min(1.0, ch / max(1e-3, full_charge))
                    # Conservative: assume the target has the maximum armor (3 x 8%).
                    est = w.get("stats", {}).get("damage", lcfg["damage"]) * frac * 0.76
                    lethal = est >= target["hp"] + 25
                if visible_los and err < tol * 1.3 and (laser_charged or lethal):
                    fire[lz] = False  # release: the beam fires now
                else:
                    fire[lz] = True   # keep charging / holding (auto-fires after hold_s)
                # The gun keeps firing during the charge, but only if the unled aim is on target.
                err_l = err
            if g is not None:
                w = me["weapons"][g]
                if tgt_d < 215 and visible_los and err_l < tol and w.get("ammo", 1) > 0 and w["reload"] <= 0:
                    fire[g] = True
            if lt is not None and tgt_d < 42 and me["energy"] > 12 and visible_los:
                fire[lt] = True
                if tgt_d < 38 and lz is None:
                    aim = [yaw_d, pit_d]
            if mo is not None and 25 < tgt_d < 200 and (not visible_los or tgt_d > 90 or g is None):
                ma = self.mortar_aim([x, y, z + 9.5 * scale], tcenter, target["vel"],
                                     me["weapons"][mo]["stats"].get("speed", 72), self.cfg["weapons"]["mortar"]["gravity"])
                if ma and not (g is not None and fire[g]) and not (lz is not None and fire[lz]):
                    aim = list(ma)
                    if abs(wrap(ma[0] - cur_yaw)) < 0.08 and me["weapons"][mo]["cooldown"] <= 0.05:
                        fire[mo] = True
            # Fight-or-leave estimate: who kills whom first?
            self.fight_r = None
            if FIGHT_CALC and target["kind"] == "mech":
                cube_k = lambda c: 1.0 + 0.04 * min(c or 0, 10)
                our = sum(weapon_dps(k, tgt_d) for k in wk if k)
                if "lightning" in (target.get("weapons") or []) and tgt_d <= 45 and "laser" in wk:
                    our -= weapon_dps("laser", tgt_d) * 0.6  # their arc keeps shorting our laser
                our *= cube_k(me.get("cubes")) * (1.0 if visible_los else 0.3)
                theirs = 0.0
                for m in mechs[:5]:
                    dm = d2(m["pos"])
                    if dm > 235:
                        continue
                    if m["id"] != target["id"] and not self.world.los(x, y, m["pos"][0], m["pos"][1], z):
                        continue
                    if m["id"] == target["id"] and not visible_los:
                        continue
                    md = sum(weapon_dps(k, dm) for k in (m.get("weapons") or ["gun"]))
                    if "lightning" in wk and dm <= 45 and "laser" in (m.get("weapons") or []):
                        md -= weapon_dps("laser", dm) * 0.6
                    theirs += md * cube_k(m.get("cubes"))
                our_ttk = target.get("hp", 1400) / max(1.0, our)
                their_ttk = me["hp"] / max(1.0, theirs)
                self.fight_r = their_ttk / max(0.1, our_ttk)
            stay_fight = self.fight_r is not None and self.fight_r >= STAY_RATIO
            # While recovering, any damage we deal restarts the 5 s regen timer: hold fire unless
            # we are being shot right now or the enemy is on top of us. A laser already charging
            # keeps its trigger state (letting go would fire it anyway).
            if self.recover and not (t - self.dmg_t < 2.0 or tgt_d < 40) and not stay_fight:
                for i, k in enumerate(wk):
                    if k == "laser" and charging_laser:
                        continue
                    fire[i] = False
            if CANARY_NO_MECH_FIRE and target["kind"] == "mech":
                fire = [False, False]
            if aim is not None:
                aim[1] = max(self.cfg["mech"]["min_pitch"], min(self.cfg["mech"]["max_pitch"], aim[1]))
        # reload when calm
        if not close and g_ammo_low(me):
            act["reload"] = True
        # An empty gun must be reloaded explicitly: never sit in a fight with 0 rounds.
        for w in me["weapons"]:
            if w and w["kind"] == "gun" and w.get("ammo", 1) <= 0 and w.get("reload", 0) <= 0:
                act["reload"] = True

        # ---------------- movement
        mx, my = 0.0, 0.0
        boost = False
        mode = "idle"
        nav_goal = None  # long-range destination for route planning
        arm_goal = None  # ARM_FIRST: weapon to reach before taking fights
        if ARM_FIRST and not any(k in ("laser", "lightning") for k in wk):
            cands = [pp for kk, pp in self.seen_pickups.values() if kk in ("laser", "lightning")
                     and math.hypot(pp[0] - x, pp[1] - y) < 300 and math.hypot(pp[0] - zc[0], pp[1] - zc[1]) < zr - 15]
            if cands:
                arm_goal = min(cands, key=lambda pp: math.hypot(pp[0] - x, pp[1] - y))

        def toward(px, py, w=1.0):
            a = math.atan2(py - y, px - x)
            return math.cos(a) * w, math.sin(a) * w

        zone_margin = 18.0
        edge_pressure = max(0.0, (dz_center - (zr - 40)) / 40.0)  # grows near/outside edge
        outside = (not me["in_zone"]) or dz_center > zr - 6
        # next-circle readiness: start heading to it once the phase is about to shrink
        heading_next = t > obs["zone"]["shrink_start"] - 25 or dn_center > nzr * 0.9

        if outside:
            mode = "zone"
            nav_goal = zc
            tx_, ty_ = toward(zc[0], zc[1])
            mx, my = tx_, ty_
            boost = me["energy"] > 15
        elif target is not None and tgt_d is not None and tgt_d < (220 if STYLE == "sniper" else 140) and not (
                arm_goal is not None and target["kind"] == "mech" and t - self.dmg_t > 3.0 and tgt_d > 45):
            mode = "fight"
            ex, ey = target["pos"][0] - x, target["pos"][1] - y
            ed = math.hypot(ex, ey) + 1e-6
            rx, ry = ex / ed, ey / ed
            n_threat = len([m for m in close if d2(m["pos"]) < 100])
            flee = (self.recover and ed > 12) or n_threat >= FLEE_COUNT
            leave_to = None
            if FIGHT_CALC and self.fight_r is not None:
                flee = False
                if self.fight_r < STAY_RATIO:
                    if not visible_los and t - self.dmg_t > 1.5:
                        leave_to = "hide"  # out of its sight and not being hit: stay put and heal
                    else:
                        cov = self.find_cover(x, y, z, target["pos"][0], target["pos"][1], target["id"], t, zc, zr)
                        if cov is not None and math.hypot(cov[0] - x, cov[1] - y) <= LEAVE_COVER_DIST:
                            leave_to = cov
                        elif NO_COVER_ACTION == "flee":
                            flee = True
            if t > self.strafe_until:
                self.strafe = -self.strafe if self.rng.random() < 0.6 else self.strafe
                self.strafe_until = t + self.rng.uniform(0.7, 1.8)
            px_, py_ = -ry * self.strafe, rx * self.strafe
            desired = FIGHT_RANGE
            if NPC_FARM and target["kind"] != "mech":
                desired = 25.0  # close in on drones/crates so their cubes are in reach
            if slot("lightning") is not None and slot("laser") is None:
                desired = 28.0
            if STYLE == "brawler" and slot("lightning") is not None and target["kind"] == "mech":
                desired = 26.0
            if STYLE == "sniper" and target["kind"] == "mech":
                desired = 160.0
            close_fast = False
            if STYLE == "adaptive" and target["kind"] == "mech":
                theirs = set(target.get("weapons") or [])
                desired, close_fast = matchup_range(set(k for k in wk if k), theirs, FIGHT_RANGE)
                if "lightning" in theirs and "lightning" not in wk and ed < 48 and me["energy"] > 20:
                    boost = True  # get out of their arc range fast
            radial = max(-1.0, min(1.0, (ed - desired) / 40.0))
            if leave_to == "hide":
                mode = "hide"
                mx, my = 0.0, 0.0
            elif leave_to is not None:
                mode = "break"  # losing: get behind the nearby wall, still shooting on the way
                mx, my = toward(leave_to[0], leave_to[1])
                boost = me["energy"] > 15
            elif flee:
                mode = "flee"
                mx, my = -rx * 1.0 + px_ * 0.4, -ry * 1.0 + py_ * 0.4
                zx, zy = toward(zc[0], zc[1], 0.5)
                mx += zx
                my += zy
                boost = me["energy"] > 20 and ed < 70
            else:
                mx, my = px_ + rx * radial * 0.8, py_ + ry * radial * 0.8
                zx, zy = toward(zc[0], zc[1], edge_pressure * 1.5)
                mx += zx
                my += zy
                if (FINISH_KILL and target["kind"] == "mech" and not visible_los and not self.recover
                        and n_threat <= 1):
                    thpf = target.get("hp", 1400) / max(1.0, target.get("max_hp", 1400))
                    if thpf < 0.6 and hp_frac > thpf + 0.1:
                        mode = "chase"  # it ducked behind cover hurt: follow before it heals
                        nav_goal = target["pos"]
                        mx, my = toward(target["pos"][0], target["pos"][1])
                if (((STYLE == "brawler" and slot("lightning") is not None) or close_fast)
                        and target["kind"] == "mech" and ed > 45 and me["energy"] > 50 and hp_frac > 0.5):
                    boost = True  # close the gap into lightning range quickly
                # Peek fighting against a lone mech: hide while the laser cools down and charges,
                # then step out with a full charge and fire. Not at lightning range (brawl there).
                lightning_brawl = slot("lightning") is not None and ed < 42
                if (PEEK_FIGHT and laser_cd is not None and target["kind"] == "mech"
                        and n_threat <= 1 and ed > 20 and not lightning_brawl):
                    charging_now = me["weapons"][slot("laser")].get("charge", 0) > 0
                    if laser_charged and not visible_los:
                        mode = "peek"  # step back out toward the target to get the shot
                        mx, my = rx, ry
                    elif (laser_cd > 0.3 or (charging_now and not laser_charged)) and not visible_los:
                        mode = "cover"  # already out of its sight: hold still (silent)
                        mx = my = 0.0
                    elif laser_cd > 0.3 or (charging_now and not laser_charged):
                        cov = self.find_cover(x, y, z, target["pos"][0], target["pos"][1],
                                              target["id"], t, zc, zr)
                        if cov is not None:
                            mode = "cover"
                            dcov = math.hypot(cov[0] - x, cov[1] - y)
                            if dcov > 1.5:
                                mx, my = toward(cov[0], cov[1])
                            else:
                                mx = my = 0.0
        elif self.dmg_bearing is not None and t - self.dmg_t < 3.0 and not (target is not None and tgt_d is not None and tgt_d < 140):
            # Shot by something we cannot see: do not stand still. Run away from the shot's
            # origin with a sideways wobble, biased toward the zone.
            mode = "evade-unseen"
            ax, ay = -math.cos(self.dmg_bearing), -math.sin(self.dmg_bearing)
            if t > self.strafe_until:
                self.strafe = -self.strafe
                self.strafe_until = t + 1.0
            mx, my = ax - ay * 0.5 * self.strafe, ay + ax * 0.5 * self.strafe
            zx, zy = toward(zc[0], zc[1], 0.35)
            mx += zx
            my += zy
            boost = me["energy"] > 40 and hp_frac < 0.6
        else:
            # not fighting: loot, drops, zone positioning
            goal = None
            if RELOCATE and self.reloc is not None:
                if t < self.reloc[2] and math.hypot(self.reloc[0] - x, self.reloc[1] - y) > 5:
                    goal = [self.reloc[0], self.reloc[1]]
                    mode = "relocate"
                else:
                    self.reloc = None
            if goal is None and FINISH_KILL and self.focus_last is not None and not self.recover:
                fpos, ft, fhp = self.focus_last
                if (t - ft < 8.0 and fhp < 0.6 and hp_frac > fhp + 0.1 and len(close) <= 1
                        and math.hypot(fpos[0] - zc[0], fpos[1] - zc[1]) < zr - 10
                        and math.hypot(fpos[0] - x, fpos[1] - y) > 4):
                    goal = fpos
                    mode = "chase"
                elif t - ft >= 8.0:
                    self.focus_last = None
            if goal is None and DROPS_ANY_HP:
                # Uncontested supply drop: 3 cubes + full HP + full energy. Checked before loot.
                for dr in obs["drops"]:
                    dd = d2(dr["pos"])
                    contested = any(math.hypot(m["pos"][0] - dr["pos"][0], m["pos"][1] - dr["pos"][1]) < 50
                                    for m in mechs)
                    if (dd < 350 and not contested and dr["lands_in"] * 11 < dd + 40
                            and dr["holder"] in (None, me["id"])
                            and math.hypot(dr["pos"][0] - zc[0], dr["pos"][1] - zc[1]) < zr - 10):
                        goal = dr["pos"]
                        mode = "drop"
                        if dd < dr["radius"] * 0.5 and dr["lands_in"] <= 0:
                            goal = None
                            mode = "hold-drop"
                            mx = my = 0.0
                        break
            if goal is None and mode != "hold-drop":
                if EARLY_LOOT:
                    pk = [{"kind": k, "pos": pp} for k, pp in self.seen_pickups.values()]
                else:
                    pk = obs["pickups"]
                pk = [p for p in pk if p["kind"] in rank and p["kind"] not in wk
                      and math.hypot(p["pos"][0] - zc[0], p["pos"][1] - zc[1]) < zr - 15]
                # Early on, with no laser or lightning yet, a far weapon is worth the walk.
                unarmed = not any(k in rank for k in wk)
                loot_range = 250 if (EARLY_LOOT and unarmed and t < 100) else 90
                if ARM_FIRST and unarmed:
                    loot_range = 300
                want_slot = wk[1] is None or wk[0] is None or (STYLE in ("brawler", "adaptive") and "gun" in wk)
                if pk and want_slot and not self.recover:
                    p = min(pk, key=lambda p: d2(p["pos"]))
                    if d2(p["pos"]) < loot_range:
                        goal = p["pos"]
                        mode = "loot"
            if goal is None and mode != "hold-drop":
                cube_r = 80 if NPC_FARM else 45
                cu = [c for c in obs["cubes"] if d2(c["pos"]) < cube_r and math.hypot(c["pos"][0] - zc[0], c["pos"][1] - zc[1]) < zr - 12]
                if cu and not self.recover:
                    goal = min(cu, key=lambda c: d2(c["pos"]))["pos"]
                    mode = "cube"
            if goal is None and mode != "hold-drop" and not self.recover:
                in_zone_pt = lambda p: math.hypot(p[0] - zc[0], p[1] - zc[1]) < zr - 15
                if NPC_FARM and t < NPC_FARM_UNTIL and hp_frac > 0.6:
                    far_npcs = [n for n in npcs if d2(n["pos"]) < 200 and in_zone_pt(n["pos"])]
                    if far_npcs:
                        goal = min(far_npcs, key=lambda n: d2(n["pos"]))["pos"]
                        mode = "farm"
                if goal is None and HUNT and hp_frac > HUNT_HP:
                    prey = [m for m in mechs if d2(m["pos"]) < 230 and in_zone_pt(m["pos"])]
                    if prey:
                        goal = prey[0]["pos"]  # nearest (low-HP mechs sort first)
                        mode = "hunt"
                    elif self.sound_bearing is not None and t - self.sound_t < 3:
                        sb = self.sound_bearing
                        cand = [x + math.cos(sb) * 40, y + math.sin(sb) * 40]
                        if in_zone_pt(cand):
                            goal = cand
                            mode = "hunt-sound"
            if goal is None and not DROPS_ANY_HP:  # original drop logic, used when the switch is off
                for dr in obs["drops"]:
                    dd = d2(dr["pos"])
                    if dd < 140 and dr["lands_in"] * 11 < dd + 40 and dr["holder"] in (None, me["id"]):
                        if math.hypot(dr["pos"][0] - zc[0], dr["pos"][1] - zc[1]) < zr - 10:
                            goal = dr["pos"]
                            mode = "drop"
                            if dd < dr["radius"] * 0.5 and dr["lands_in"] <= 0:
                                goal = None
                                mode = "hold-drop"
                                mx = my = 0.0
                            break
            if goal is None and mode != "hold-drop":
                # position inside the circle we're heading to
                cx_, cy_ = (nzc if heading_next else zc)
                rr = (nzr if heading_next else zr) * 0.45
                d = math.hypot(x - cx_, y - cy_)
                if d > rr:
                    goal = [cx_, cy_]
                    mode = "zone-move"
            if goal is not None:
                nav_goal = goal
                mx, my = toward(goal[0], goal[1])
                dgoal = math.hypot(goal[0] - x, goal[1] - y)
                if dgoal < 3.5 and mode in ("loot", "cube"):
                    mx, my = mx * 0.2, my * 0.2
            elif mode == "idle":
                mx = my = 0.0

        # route planning: replace the straight-line heading with the next step of an A* route
        if NAV and self.nav is not None and nav_goal is not None and (mx or my):
            dgoal = math.hypot(nav_goal[0] - x, nav_goal[1] - y)
            on_ground = z - self.nav.G.ground(x, y) < 3.0  # skip on roofs
            if dgoal > 10 and on_ground:
                hd = self.nav.heading(x, y, nav_goal[0], nav_goal[1], t, budget=1000)
                if hd is not None:
                    n = max(0.2, min(1.0, math.hypot(mx, my)))
                    mx, my = math.cos(hd) * n, math.sin(hd) * n

        # dodge incoming enemy projectiles
        for pr in obs["projectiles"]:
            px, py = pr["pos"][0] - x, pr["pos"][1] - y
            vx, vy = pr["vel"][0], pr["vel"][1]
            vs = math.hypot(vx, vy) + 1e-6
            along = -(px * vx + py * vy) / vs
            if 0 < along < 30 + vs * 0.25 and math.hypot(px, py) < 90:
                cross = (px * vy - py * vx) / vs
                if abs(cross) < 5:
                    s = 1 if cross <= 0 else -1
                    mx += (-vy / vs) * s * 1.4
                    my += (vx / vs) * s * 1.4
                    if mode == "idle":
                        mode = "dodge"

        # sidestep laser charges aimed at us (the beam is instant once released, so move during the charge)
        if LASER_DODGE:
            my_r = 2.5 * scale
            for m in mechs:
                if not m.get("charging"):
                    continue
                sx_, sy_ = m["pos"][0], m["pos"][1]
                dd = math.hypot(x - sx_, y - sy_)
                if dd > 285 or dd < 1:
                    continue
                err = wrap(math.atan2(y - sy_, x - sx_) - m["aim"][0])
                if abs(err) > 0.5:
                    continue
                lateral = dd * math.sin(err)  # signed miss distance of their current aim line
                if abs(lateral) < my_r + 4.0:
                    if LASER_COVER:
                        if not self.world.los(x, y, sx_, sy_, z):
                            break  # already behind a wall from it
                        cov = self.find_cover(x, y, z, sx_, sy_, m["id"], t, zc, zr)
                        if cov is not None and math.hypot(cov[0] - x, cov[1] - y) < 16:
                            mx, my = toward(cov[0], cov[1])
                            boost = me["energy"] > 12
                            mode = "laser-cover"
                            break
                    ux, uy = (x - sx_) / dd, (y - sy_) / dd   # along their aim, toward us
                    side = 1.0 if lateral >= 0 else -1.0       # step to the side we are already on
                    px2, py2 = -uy * side, ux * side
                    mx, my = px2 * 1.6 + mx * 0.3, py2 * 1.6 + my * 0.3
                    boost = me["energy"] > 12
                    mode = "laser-dodge"
                    break

        # stuck / escape
        moving_cmd = math.hypot(mx, my)
        if moving_cmd > 0.4 and speed < 1.5 and me["grounded"]:
            self.stuck += 1
        else:
            self.stuck = max(0, self.stuck - 1)
        jump = False
        if self.stuck >= 6 and t > self.escape_until:
            self.escape_until = t + 1.2
            self.escape_dir = self.rng.uniform(-math.pi, math.pi)
            self.stuck = 0
        if t < self.escape_until:
            mx, my = math.cos(self.escape_dir), math.sin(self.escape_dir)
            jump = True
            boost = me["energy"] > 30
            mode = "escape"
        elif moving_cmd > 0.05:
            ang = self.steer(x, y, math.atan2(my, mx))
            n = min(1.0, moving_cmd)
            mx, my = math.cos(ang) * n, math.sin(ang) * n

        # laser charging halves speed anyway; boost only when energy ok
        if boost and me["energy"] < 8:
            boost = False

        # ---------------- aim when no target: scan toward sounds, else slow sweep
        if aim is None:
            if self.dmg_bearing is not None and t - self.dmg_t < 3:
                # Face whoever just hit us: rear hits deal x1.5.
                aim = [self.dmg_bearing, 0.0]
            elif self.sound_bearing is not None and t - self.sound_t < 3:
                aim = [self.sound_bearing, 0.0]
            elif moving_cmd > 0.3 and mode in ("zone", "zone-move", "loot", "cube", "drop", "escape"):
                aim = [math.atan2(my, mx), 0.0]
            else:
                self.scan += 0.5
                aim = [me["aim"][0] + 0.9, 0.0]

        act.update(move=[round(mx, 3), round(my, 3)], boost=boost, jump=jump,
                   aim=[round(wrap(aim[0]), 4), round(aim[1], 4)], fire=fire)
        mf.draw.label(f"{mode} hp{me['hp']:.0f}" + (f" R{self.fight_r:.2f}" if self.fight_r is not None else ""))
        return act


def g_ammo_low(me):
    w = me["weapons"][0]
    return bool(w and w["kind"] == "gun" and w.get("ammo", 99) < 6 and w.get("reload", 0) <= 0)


class SafeBot(Bot):
    """Keep the v1 decisions while guaranteeing a protocol answer on errors."""

    def act(self, obs):
        try:
            return super().act(obs)
        except Exception as exc:
            t = obs.get("t", 0)
            if t - getattr(self, "_last_error_t", -99) >= 5:
                mf.log("v1 fallback", type(exc).__name__, str(exc)[:100])
                self._last_error_t = t
            action = {"tick": obs.get("tick", 0), "move": [0, 0], "fire": [False, False]}
            if obs.get("offer"):
                action["pick"] = 0
            return action


if __name__ == "__main__":
    mf.run(SafeBot)

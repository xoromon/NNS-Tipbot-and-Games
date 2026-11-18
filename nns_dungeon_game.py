import os
import json
import time
import sqlite3
import random
import asyncio
from typing import Optional, Dict, Any, List, Tuple

from decimal import Decimal, ROUND_DOWN
from dotenv import load_dotenv

import discord
from discord import app_commands, ui
from discord.errors import NotFound

load_dotenv()

# ---------------------------
# Config
# ---------------------------
DISCORD_TOKEN = os.environ.get("NNS_DUNGEON_BOT_DISCORD_TOKEN", "").strip()
DUNGEON_DB_PATH = os.environ.get("DUNGEON_DB", "dungeon.db").strip()
TIPBOT_DB_PATH = os.environ.get("TIPBOT_DB", "tipbot.db").strip()

GUILD_ID = os.environ.get("NNS_DUNGEON_BOT_GUILD_ID", "").strip()

DUNGEON_ALLOWED_CHANNEL_ID = int(os.environ.get("NNS_DUNGEON_ALLOWED_CHANNEL_ID", "0").strip() or "0")
DUNGEON_TREASURY_ID = int(os.environ.get("NNS_DUNGEON_TREASURY_ID", "").strip() or "0")

DUNGEON_WIDTH = max(7, int(os.environ.get("DUNGEON_WIDTH", "11")))
DUNGEON_HEIGHT = max(7, int(os.environ.get("DUNGEON_HEIGHT", "11")))
DUNGEON_VIEW_TIMEOUT_SECONDS = int(os.environ.get("DUNGEON_VIEW_TIMEOUT_SECONDS", "1800"))
DUNGEON_MOVE_COOLDOWN_MS = max(0, int(os.environ.get("DUNGEON_MOVE_COOLDOWN_MS", "400")))
DUNGEON_FAST_FORWARD_STEP_MS = max(50, int(os.environ.get("DUNGEON_FAST_FORWARD_STEP_MS", "300")))

DUNGEON_BRAID_CHANCE_PERCENT = max(0, min(100, int(os.environ.get("DUNGEON_BRAID_CHANCE_PERCENT", "8"))))
DUNGEON_MONSTER_MIN_START_DISTANCE = max(0, int(os.environ.get("DUNGEON_MONSTER_MIN_START_DISTANCE", "4")))

#DUNGEON_EXIT_XP = int(os.environ.get("DUNGEON_EXIT_XP", "1"))
DUNGEON_VISION_BASE = int(os.environ.get("DUNGEON_VISION_BASE", "5"))
DUNGEON_VISION_MAX_BONUS = int(os.environ.get("DUNGEON_VISION_MAX_BONUS", "4"))
DUNGEON_VISION_COST_BASE_SAT = int(
    Decimal(os.environ.get("DUNGEON_VISION_COST_BASE", "100.00000000")).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    ) * Decimal("100000000")
)

DUNGEON_TELEPORT_COST_SAT = int(Decimal(os.environ.get("DUNGEON_TELEPORT_COST", "5.00000000")).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN) * Decimal("100000000"))
DUNGEON_MAX_XP_STARS = max(0, int(os.environ.get("DUNGEON_MAX_XP_STARS", "3")))
DUNGEON_MAX_DIAMONDS = max(0, int(os.environ.get("DUNGEON_MAX_DIAMONDS", "2")))


def get_vision_upgrade_cost_sat(next_bonus_level: int) -> int:
    return int(DUNGEON_VISION_COST_BASE_SAT) * max(1, int(next_bonus_level))


NNS_SATS = 100_000_000

# Keep XP levels aligned with the existing tipbot.
NNS_LEVEL_XP_THRESHOLDS = [0, 50, 125, 225, 350, 500, 700, 950, 1250, 1600, 2000, 2500, 3100, 3800, 4600, 5500]

# Logical tiles
WALL = "#"
FLOOR = "."
PLAYER = "@"
EXIT = "E"
UNKNOWN = "·"

# Visual tiles for Discord rendering
VIS_PLAYER = "🤴🏻"
VIS_PLAYER_DEAD = "☠️"
VIS_EXIT = "🚪"
VIS_WALL = "🪨"

VIS_FLOOR_DARK = "◾"
VIS_UNKNOWN_DARK = "🌫"
VIS_FLOOR_LIGHT = "◽"
VIS_UNKNOWN_LIGHT = "🌫"

VIS_XP_STAR = "✨"
VIS_DIAMOND = "💎"

MONSTER_CLASS_DEFS = {
    1: {"icon": "🧟‍♂️", "sight": 2, "min_level": 3, "base_spawn": 60, "decay_per_level": 5},
    2: {"icon": "🧌", "sight": 3, "min_level": 5, "base_spawn": 60, "decay_per_level": 5},
    3: {"icon": "🧛🏻‍♀️", "sight": 4, "min_level": 7, "base_spawn": 60, "decay_per_level": 5},
    4: {"icon": "👹", "sight": 5, "min_level": 9, "base_spawn": 75, "decay_per_level": 0},
    5: {"icon": "😈", "sight": 6, "min_level": 11, "base_spawn": 75, "decay_per_level": 0},
    6: {"icon": "🐲", "sight": 7, "min_level": 15, "base_spawn": 75, "decay_per_level": 0},
    7: {"icon": "💀", "sight": 9, "min_level": 18, "base_spawn": 75, "decay_per_level": 0},
}
MAX_MONSTERS_PER_RUN = 3


def normalize_dungeon_theme(value: Optional[str]) -> str:
    raw = str(value or "dark").strip().lower()
    return "light" if raw == "light" else "dark"


# ---------------------------
# Generic helpers
# ---------------------------
def now_ts() -> int:
    return int(time.time())


def format_sat_to_nns(sat: int) -> str:
    return f"{(Decimal(int(sat)) / Decimal(NNS_SATS)):.8f}"


def format_sat_to_nns_int(sat: int) -> str:
    return str(int(Decimal(int(sat)) / Decimal(NNS_SATS)))


def compute_nns_level_from_xp(xp_total: int) -> int:
    xp_total = max(0, int(xp_total or 0))
    level = 1
    for idx, threshold in enumerate(NNS_LEVEL_XP_THRESHOLDS, start=1):
        if xp_total >= int(threshold):
            level = idx
        else:
            break
    return min(16, max(1, level))


def dungeon_db() -> sqlite3.Connection:
    con = sqlite3.connect(DUNGEON_DB_PATH, timeout=30, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con


def tipbot_db() -> sqlite3.Connection:
    con = sqlite3.connect(TIPBOT_DB_PATH, timeout=30, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con


# ---------------------------
# Dungeon DB init
# ---------------------------
def init_dungeon_db() -> None:
    con = dungeon_db()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS dungeon_runs (
              discord_id INTEGER PRIMARY KEY,
              map_json TEXT NOT NULL,
              monsters_json TEXT NOT NULL DEFAULT '[]',
              stars_json TEXT NOT NULL DEFAULT '[]',
              diamonds_json TEXT NOT NULL DEFAULT '[]',
              pending_star_xp INTEGER NOT NULL DEFAULT 0,
              pending_diamond_sat INTEGER NOT NULL DEFAULT 0,
              pos_x INTEGER NOT NULL,
              pos_y INTEGER NOT NULL,
              exit_x INTEGER NOT NULL,
              exit_y INTEGER NOT NULL,
              width INTEGER NOT NULL,
              height INTEGER NOT NULL,
              level_num INTEGER NOT NULL DEFAULT 1,
              steps INTEGER NOT NULL DEFAULT 0,
              teleport_uses INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL DEFAULT 'active',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            """
        )
        try:
            con.execute("ALTER TABLE dungeon_runs ADD COLUMN level_num INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass

        try:
            con.execute("ALTER TABLE dungeon_runs ADD COLUMN monsters_json TEXT NOT NULL DEFAULT '[]'")
        except Exception:
            pass

        try:
            con.execute("ALTER TABLE dungeon_runs ADD COLUMN stars_json TEXT NOT NULL DEFAULT '[]'")
        except Exception:
            pass

        try:
            con.execute("ALTER TABLE dungeon_runs ADD COLUMN diamonds_json TEXT NOT NULL DEFAULT '[]'")
        except Exception:
            pass

        try:
            con.execute("ALTER TABLE dungeon_runs ADD COLUMN pending_star_xp INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass

        try:
            con.execute("ALTER TABLE dungeon_runs ADD COLUMN pending_diamond_sat INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass

        try:
            con.execute("ALTER TABLE dungeon_runs ADD COLUMN teleport_uses INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS dungeon_upgrades (
              discord_id INTEGER PRIMARY KEY,
              vision_bonus INTEGER NOT NULL DEFAULT 0,
              max_hp_bonus INTEGER NOT NULL DEFAULT 0,
              attack_bonus INTEGER NOT NULL DEFAULT 0,
              defense_bonus INTEGER NOT NULL DEFAULT 0,
              render_theme TEXT NOT NULL DEFAULT 'dark',
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS dungeon_stats (
              discord_id INTEGER PRIMARY KEY,
              runs_started INTEGER NOT NULL DEFAULT 0,
              runs_completed INTEGER NOT NULL DEFAULT 0,
              deaths INTEGER NOT NULL DEFAULT 0,
              total_steps INTEGER NOT NULL DEFAULT 0,
              current_level INTEGER NOT NULL DEFAULT 1,
              highest_level INTEGER NOT NULL DEFAULT 1,
              updated_at INTEGER NOT NULL
            );
            """
        )
        try:
            con.execute("ALTER TABLE dungeon_stats ADD COLUMN current_level INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass

        try:
            con.execute("ALTER TABLE dungeon_stats ADD COLUMN highest_level INTEGER NOT NULL DEFAULT 1")
        except Exception:
            pass

        try:
            con.execute("ALTER TABLE dungeon_upgrades ADD COLUMN render_theme TEXT NOT NULL DEFAULT 'dark'")
        except Exception:
            pass

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS dungeon_shop_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts INTEGER NOT NULL,
              discord_id INTEGER NOT NULL,
              item_key TEXT NOT NULL,
              cost_sat INTEGER NOT NULL,
              note TEXT
            );
            """
        )
    finally:
        con.close()


# ---------------------------
# Tipbot DB bridge helpers
# ---------------------------
def init_tipbot_bridge_tables() -> None:
    con = tipbot_db()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS nns_profiles (
              discord_id INTEGER PRIMARY KEY,
              xp_total INTEGER NOT NULL DEFAULT 0,
              level INTEGER NOT NULL DEFAULT 1,
              updated_at INTEGER NOT NULL
            );
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS nns_xp_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts INTEGER NOT NULL,
              discord_id INTEGER NOT NULL,
              source TEXT NOT NULL,
              xp_amount INTEGER NOT NULL,
              note TEXT
            );
            """
        )
    finally:
        con.close()


def get_or_create_tipbot_user(con: sqlite3.Connection, discord_id: int) -> Dict[str, Any]:
    row = con.execute(
        "SELECT discord_id, nns_internal_sat, created_at, updated_at FROM users WHERE discord_id=?",
        (int(discord_id),),
    ).fetchone()

    if row:
        return {
            "discord_id": int(row[0]),
            "nns_internal_sat": int(row[1] or 0),
            "created_at": int(row[2] or 0),
            "updated_at": int(row[3] or 0),
        }

    ts = now_ts()
    con.execute(
        "INSERT INTO users(discord_id, address, website_secret, balance, veco_internal_sat, veco_deposit_address, "
        "nns_internal_sat, nns_deposit_address, last_nns_claim_at, created_at, updated_at, last_withdraw_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(discord_id), None, None, 0, 0, None, 0, None, 0, ts, ts, 0),
    )
    return {
        "discord_id": int(discord_id),
        "nns_internal_sat": 0,
        "created_at": ts,
        "updated_at": ts,
    }


def get_or_create_nns_profile(con: sqlite3.Connection, discord_id: int) -> Dict[str, Any]:
    row = con.execute(
        "SELECT discord_id, xp_total, level, updated_at FROM nns_profiles WHERE discord_id=?",
        (int(discord_id),),
    ).fetchone()

    if row:
        return {
            "discord_id": int(row[0]),
            "xp_total": int(row[1] or 0),
            "level": int(row[2] or 1),
            "updated_at": int(row[3] or 0),
        }

    ts = now_ts()
    con.execute(
        "INSERT INTO nns_profiles(discord_id, xp_total, level, updated_at) VALUES(?,?,?,?)",
        (int(discord_id), 0, 1, ts),
    )
    return {
        "discord_id": int(discord_id),
        "xp_total": 0,
        "level": 1,
        "updated_at": ts,
    }


def grant_nns_xp_tipbot(discord_id: int, source: str, xp_amount: int, note: Optional[str] = None) -> Dict[str, Any]:
    xp_amount = int(xp_amount or 0)
    con = tipbot_db()
    try:
        con.execute("BEGIN IMMEDIATE;")
        profile = get_or_create_nns_profile(con, int(discord_id))

        old_xp = int(profile.get("xp_total") or 0)
        old_level = int(profile.get("level") or 1)

        if xp_amount <= 0:
            con.execute("COMMIT;")
            return {
                "xp_total": old_xp,
                "level": old_level,
                "leveled_up": False,
                "old_level": old_level,
                "new_level": old_level,
                "xp_added": 0,
            }

        new_xp = old_xp + xp_amount
        new_level = compute_nns_level_from_xp(new_xp)
        ts = now_ts()

        con.execute(
            "UPDATE nns_profiles SET xp_total=?, level=?, updated_at=? WHERE discord_id=?",
            (int(new_xp), int(new_level), ts, int(discord_id)),
        )
        con.execute(
            "INSERT INTO nns_xp_log(ts, discord_id, source, xp_amount, note) VALUES(?,?,?,?,?)",
            (ts, int(discord_id), str(source), int(xp_amount), note or ""),
        )
        con.execute("COMMIT;")

        return {
            "xp_total": int(new_xp),
            "level": int(new_level),
            "leveled_up": bool(new_level > old_level),
            "old_level": int(old_level),
            "new_level": int(new_level),
            "xp_added": int(xp_amount),
        }
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        con.close()


def debit_nns_internal_tipbot(discord_id: int, amount_sat: int, note: str) -> int:
    amount_sat = int(amount_sat or 0)
    if amount_sat <= 0:
        raise ValueError("Invalid debit amount.")

    con = tipbot_db()
    try:
        con.execute("BEGIN IMMEDIATE;")
        user = get_or_create_tipbot_user(con, int(discord_id))
        balance_sat = int(user.get("nns_internal_sat") or 0)
        if balance_sat < amount_sat:
            con.execute("ROLLBACK;")
            raise ValueError(
                f"Insufficient NNS balance. You need **{format_sat_to_nns_int(amount_sat)} NNS** but only have **{format_sat_to_nns(balance_sat)} NNS**."
            )

        ts = now_ts()
        treasury_id = int(DUNGEON_TREASURY_ID or 0)

        con.execute(
            "UPDATE users SET nns_internal_sat = nns_internal_sat - ?, updated_at=? WHERE discord_id=?",
            (amount_sat, ts, int(discord_id)),
        )

        tx_to_id = None
        if treasury_id > 0 and treasury_id != int(discord_id):
            get_or_create_tipbot_user(con, treasury_id)
            con.execute(
                "UPDATE users SET nns_internal_sat = nns_internal_sat + ?, updated_at=? WHERE discord_id=?",
                (amount_sat, ts, treasury_id),
            )
            tx_to_id = treasury_id

        con.execute(
            "INSERT INTO tx_log(ts, type, from_id, to_id, amount, note, status) VALUES(?,?,?,?,?,?,?)",
            (ts, "dungeon_upgrade_nns", int(discord_id), tx_to_id, int(amount_sat), note, "ok"),
        )
        con.execute("COMMIT;")
        return balance_sat - amount_sat
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        con.close()


def get_tipbot_balance_sat(discord_id: int) -> int:
    con = tipbot_db()
    try:
        user = get_or_create_tipbot_user(con, int(discord_id))
        return int(user.get("nns_internal_sat") or 0)
    finally:
        con.close()


# ---------------------------
# Dungeon state helpers
# ---------------------------
def get_or_create_upgrades(con: sqlite3.Connection, discord_id: int) -> Dict[str, Any]:
    row = con.execute(
        "SELECT discord_id, vision_bonus, max_hp_bonus, attack_bonus, defense_bonus, render_theme, created_at, updated_at "
        "FROM dungeon_upgrades WHERE discord_id=?",
        (int(discord_id),),
    ).fetchone()

    if row:
        return {
            "discord_id": int(row[0]),
            "vision_bonus": int(row[1] or 0),
            "max_hp_bonus": int(row[2] or 0),
            "attack_bonus": int(row[3] or 0),
            "defense_bonus": int(row[4] or 0),
            "render_theme": normalize_dungeon_theme(row[5]),
            "created_at": int(row[6] or 0),
            "updated_at": int(row[7] or 0),
        }

    ts = now_ts()
    con.execute(
        "INSERT INTO dungeon_upgrades(discord_id, vision_bonus, max_hp_bonus, attack_bonus, defense_bonus, render_theme, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (int(discord_id), 0, 0, 0, 0, "dark", ts, ts),
    )
    return {
        "discord_id": int(discord_id),
        "vision_bonus": 0,
        "max_hp_bonus": 0,
        "attack_bonus": 0,
        "defense_bonus": 0,
        "render_theme": "dark",
        "created_at": ts,
        "updated_at": ts,
    }

def touch_dungeon_stats(con: sqlite3.Connection, discord_id: int) -> None:
    ts = now_ts()
    con.execute(
        "INSERT INTO dungeon_stats(discord_id, runs_started, runs_completed, deaths, total_steps, current_level, highest_level, updated_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(discord_id) DO NOTHING",
        (int(discord_id), 0, 0, 0, 0, 1, 1, ts),
    )


def set_saved_current_level(con: sqlite3.Connection, discord_id: int, level_num: int) -> None:
    touch_dungeon_stats(con, int(discord_id))
    con.execute(
        "UPDATE dungeon_stats SET current_level=?, updated_at=? WHERE discord_id=?",
        (int(level_num), now_ts(), int(discord_id)),
    )


def record_highest_level(con: sqlite3.Connection, discord_id: int, level_num: int) -> None:
    touch_dungeon_stats(con, int(discord_id))
    row = con.execute(
        "SELECT highest_level FROM dungeon_stats WHERE discord_id=?",
        (int(discord_id),),
    ).fetchone()
    current_high = max(1, int(row[0] or 1)) if row else 1
    new_high = max(current_high, max(1, int(level_num)))
    con.execute(
        "UPDATE dungeon_stats SET highest_level=?, updated_at=? WHERE discord_id=?",
        (new_high, now_ts(), int(discord_id)),
    )


def increment_deaths_and_reset_progress(con: sqlite3.Connection, discord_id: int) -> None:
    touch_dungeon_stats(con, int(discord_id))
    con.execute(
        "UPDATE dungeon_stats SET deaths = deaths + 1, current_level=1, updated_at=? WHERE discord_id=?",
        (now_ts(), int(discord_id)),
    )


def increment_runs_started(con: sqlite3.Connection, discord_id: int) -> None:
    touch_dungeon_stats(con, discord_id)
    con.execute(
        "UPDATE dungeon_stats SET runs_started = runs_started + 1, updated_at=? WHERE discord_id=?",
        (now_ts(), int(discord_id)),
    )



def increment_runs_completed(con: sqlite3.Connection, discord_id: int, steps: int) -> None:
    touch_dungeon_stats(con, discord_id)
    con.execute(
        "UPDATE dungeon_stats SET runs_completed = runs_completed + 1, total_steps = total_steps + ?, updated_at=? WHERE discord_id=?",
        (int(steps), now_ts(), int(discord_id)),
    )


def get_saved_current_level(con: sqlite3.Connection, discord_id: int) -> int:
    touch_dungeon_stats(con, int(discord_id))
    row = con.execute(
        "SELECT current_level FROM dungeon_stats WHERE discord_id=?",
        (int(discord_id),),
    ).fetchone()
    if not row:
        return 1
    return max(1, int(row[0] or 1))


def get_active_run(con: sqlite3.Connection, discord_id: int) -> Optional[Dict[str, Any]]:
    row = con.execute(
        "SELECT discord_id, map_json, monsters_json, stars_json, diamonds_json, pending_star_xp, pending_diamond_sat, pos_x, pos_y, exit_x, exit_y, width, height, level_num, steps, teleport_uses, status, created_at, updated_at "
        "FROM dungeon_runs WHERE discord_id=?",
        (int(discord_id),),
    ).fetchone()

    if not row:
        return None

    return {
        "discord_id": int(row[0]),
        "map": json.loads(row[1]),
        "monsters": json.loads(row[2] or "[]"),
        "stars": json.loads(row[3] or "[]"),
        "diamonds": json.loads(row[4] or "[]"),
        "pending_star_xp": int(row[5] or 0),
        "pending_diamond_sat": int(row[6] or 0),
        "pos_x": int(row[7]),
        "pos_y": int(row[8]),
        "exit_x": int(row[9]),
        "exit_y": int(row[10]),
        "width": int(row[11]),
        "height": int(row[12]),
        "level_num": int(row[13] or 1),
        "steps": int(row[14]),
        "teleport_uses": int(row[15] or 0),
        "status": str(row[16]),
        "created_at": int(row[17]),
        "updated_at": int(row[18]),
    }


def save_run(con: sqlite3.Connection, run: Dict[str, Any]) -> None:
    ts = now_ts()
    con.execute(
        """
        INSERT INTO dungeon_runs(
          discord_id, map_json, monsters_json, stars_json, diamonds_json, pending_star_xp, pending_diamond_sat, pos_x, pos_y, exit_x, exit_y, width, height, level_num, steps, teleport_uses, status, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(discord_id) DO UPDATE SET
          map_json=excluded.map_json,
          monsters_json=excluded.monsters_json,
          stars_json=excluded.stars_json,
          diamonds_json=excluded.diamonds_json,
          pending_star_xp=excluded.pending_star_xp,
          pending_diamond_sat=excluded.pending_diamond_sat,
          pos_x=excluded.pos_x,
          pos_y=excluded.pos_y,
          exit_x=excluded.exit_x,
          exit_y=excluded.exit_y,
          width=excluded.width,
          height=excluded.height,
          level_num=excluded.level_num,
          steps=excluded.steps,
          teleport_uses=excluded.teleport_uses,
          status=excluded.status,
          updated_at=excluded.updated_at
        """,
        (
            int(run["discord_id"]),
            json.dumps(run["map"]),
            json.dumps(run.get("monsters") or []),
            json.dumps(run.get("stars") or []),
            json.dumps(run.get("diamonds") or []),
            int(run.get("pending_star_xp") or 0),
            int(run.get("pending_diamond_sat") or 0),
            int(run["pos_x"]),
            int(run["pos_y"]),
            int(run["exit_x"]),
            int(run["exit_y"]),
            int(run["width"]),
            int(run["height"]),
            int(run.get("level_num") or 1),
            int(run["steps"]),
            int(run.get("teleport_uses") or 0),
            str(run["status"]),
            int(run.get("created_at") or ts),
            ts,
        ),
    )


# XP Star helpers
def spawn_xp_stars_for_level(grid: List[List[str]], exit_x: int, exit_y: int, monsters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    occupied = {(1, 1), (int(exit_x), int(exit_y))}
    for monster in monsters:
        occupied.add((int(monster["x"]), int(monster["y"])))

    positions: List[Tuple[int, int]] = []
    for y in range(len(grid)):
        for x in range(len(grid[0])):
            if grid[y][x] != FLOOR:
                continue
            if (x, y) in occupied:
                continue
            positions.append((x, y))

    random.shuffle(positions)
    stars: List[Dict[str, Any]] = []
    for _ in range(int(DUNGEON_MAX_XP_STARS)):
        if not positions:
            break
        if random.randint(0, 1) == 1:
            sx, sy = positions.pop()
            stars.append({"x": int(sx), "y": int(sy)})
    return stars


def collect_xp_star_if_present(run: Dict[str, Any]) -> int:
    px = int(run["pos_x"])
    py = int(run["pos_y"])
    stars = list(run.get("stars") or [])
    remaining: List[Dict[str, Any]] = []
    collected = 0
    for star in stars:
        if int(star["x"]) == px and int(star["y"]) == py:
            collected += 1
        else:
            remaining.append(star)
    run["stars"] = remaining
    return collected


# Credit NNS from treasury to player (for diamonds)
def credit_nns_from_treasury_tipbot(discord_id: int, amount_sat: int, note: str) -> int:
    amount_sat = int(amount_sat or 0)
    if amount_sat <= 0:
        raise ValueError("Invalid credit amount.")

    treasury_id = int(DUNGEON_TREASURY_ID or 0)
    if treasury_id <= 0:
        raise ValueError("Dungeon treasury is not configured.")
    if treasury_id == int(discord_id):
        raise ValueError("Dungeon treasury cannot pay the same account.")

    con = tipbot_db()
    try:
        con.execute("BEGIN IMMEDIATE;")
        treasury_user = get_or_create_tipbot_user(con, treasury_id)
        treasury_balance_sat = int(treasury_user.get("nns_internal_sat") or 0)
        if treasury_balance_sat < amount_sat:
            con.execute("ROLLBACK;")
            raise ValueError("Dungeon treasury has insufficient NNS.")

        get_or_create_tipbot_user(con, int(discord_id))
        ts = now_ts()
        con.execute(
            "UPDATE users SET nns_internal_sat = nns_internal_sat - ?, updated_at=? WHERE discord_id=?",
            (amount_sat, ts, treasury_id),
        )
        con.execute(
            "UPDATE users SET nns_internal_sat = nns_internal_sat + ?, updated_at=? WHERE discord_id=?",
            (amount_sat, ts, int(discord_id)),
        )
        con.execute(
            "INSERT INTO tx_log(ts, type, from_id, to_id, amount, note, status) VALUES(?,?,?,?,?,?,?)",
            (ts, "dungeon_diamond_nns", treasury_id, int(discord_id), int(amount_sat), note, "ok"),
        )
        con.execute("COMMIT;")
        return amount_sat
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        con.close()


def spawn_diamond_for_level(grid: List[List[str]], exit_x: int, exit_y: int, monsters: List[Dict[str, Any]], stars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    occupied = {(1, 1), (int(exit_x), int(exit_y))}
    for monster in monsters:
        occupied.add((int(monster["x"]), int(monster["y"])))
    for star in stars:
        occupied.add((int(star["x"]), int(star["y"])))

    positions: List[Tuple[int, int]] = []
    for y in range(len(grid)):
        for x in range(len(grid[0])):
            if grid[y][x] != FLOOR:
                continue
            if (x, y) in occupied:
                continue
            positions.append((x, y))

    if not positions:
        return []

    random.shuffle(positions)
    diamonds: List[Dict[str, Any]] = []
    for _ in range(int(DUNGEON_MAX_DIAMONDS)):
        if not positions:
            break
        if random.randint(0, 1) == 1:
            sx, sy = positions.pop()
            diamonds.append({"x": int(sx), "y": int(sy)})
    return diamonds


# Diamond collect helper
def collect_diamond_if_present(run: Dict[str, Any]) -> int:
    px = int(run["pos_x"])
    py = int(run["pos_y"])
    diamonds = list(run.get("diamonds") or [])
    remaining: List[Dict[str, Any]] = []
    collected = 0
    for diamond in diamonds:
        if int(diamond["x"]) == px and int(diamond["y"]) == py:
            collected += 1
        else:
            remaining.append(diamond)
    run["diamonds"] = remaining
    return collected


def delete_run(con: sqlite3.Connection, discord_id: int) -> None:
    con.execute("DELETE FROM dungeon_runs WHERE discord_id=?", (int(discord_id),))


# ---------------------------
# Maze generation
# ---------------------------
def ensure_odd(n: int) -> int:
    return n if n % 2 == 1 else n + 1


def carve_maze(width: int, height: int) -> List[List[str]]:
    width = ensure_odd(width)
    height = ensure_odd(height)

    grid = [[WALL for _ in range(width)] for _ in range(height)]

    for y in range(1, height, 2):
        for x in range(1, width, 2):
            grid[y][x] = FLOOR

    stack = [(1, 1)]
    visited = {(1, 1)}
    directions = [(2, 0), (-2, 0), (0, 2), (0, -2)]

    while stack:
        x, y = stack[-1]
        random.shuffle(directions)
        carved = False

        for dx, dy in directions:
            nx, ny = x + dx, y + dy
            if not (1 <= nx < width - 1 and 1 <= ny < height - 1):
                continue
            if (nx, ny) in visited:
                continue

            visited.add((nx, ny))
            grid[y + dy // 2][x + dx // 2] = FLOOR
            grid[ny][nx] = FLOOR
            stack.append((nx, ny))
            carved = True
            break

        if not carved:
            stack.pop()

    braid_chance = int(DUNGEON_BRAID_CHANCE_PERCENT)
    if braid_chance > 0:
        candidates: List[Tuple[int, int]] = []
        for y in range(1, height - 1):
            for x in range(1, width - 1):
                if grid[y][x] != WALL:
                    continue

                left_open = grid[y][x - 1] == FLOOR
                right_open = grid[y][x + 1] == FLOOR
                up_open = grid[y - 1][x] == FLOOR
                down_open = grid[y + 1][x] == FLOOR

                horizontal_bridge = left_open and right_open and not up_open and not down_open
                vertical_bridge = up_open and down_open and not left_open and not right_open

                if horizontal_bridge or vertical_bridge:
                    candidates.append((x, y))

        random.shuffle(candidates)
        for x, y in candidates:
            if random.randint(1, 100) <= braid_chance:
                grid[y][x] = FLOOR

    return grid


def create_new_run(discord_id: int, level_num: int = 1, con: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    width = ensure_odd(DUNGEON_WIDTH)
    height = ensure_odd(DUNGEON_HEIGHT)
    grid = carve_maze(width, height)
    ts = now_ts()
    exit_x = width - 2
    exit_y = height - 2
    monsters = spawn_monsters_for_level(grid, int(level_num), exit_x, exit_y)

    own_con = False
    if con is None:
        con = dungeon_db()
        own_con = True

    try:
        touch_dungeon_stats(con, int(discord_id))
        row = con.execute(
            "SELECT highest_level FROM dungeon_stats WHERE discord_id=?",
            (int(discord_id),),
        ).fetchone()
        highest_level = max(1, int(row[0] or 1)) if row else 1
        reward_min_level = min(max(2, highest_level), 3)
    finally:
        if own_con:
            con.close()

    if int(level_num) >= int(reward_min_level):
        stars = spawn_xp_stars_for_level(grid, exit_x, exit_y, monsters)
        diamonds = spawn_diamond_for_level(grid, exit_x, exit_y, monsters, stars)
    else:
        stars = []
        diamonds = []

    return {
        "discord_id": int(discord_id),
        "map": grid,
        "monsters": monsters,
        "stars": stars,
        "diamonds": diamonds,
        "pending_star_xp": 0,
        "pending_diamond_sat": 0,
        "pos_x": 1,
        "pos_y": 1,
        "exit_x": exit_x,
        "exit_y": exit_y,
        "width": width,
        "height": height,
        "level_num": int(level_num),
        "steps": 0,
        "teleport_uses": 0,
        "status": "active",
        "created_at": ts,
        "updated_at": ts,
    }


def get_monster_spawn_chance(level_num: int, class_id: int) -> int:
    info = MONSTER_CLASS_DEFS[int(class_id)]
    min_level = int(info["min_level"])
    if int(level_num) < min_level:
        return 0

    base_spawn = int(info["base_spawn"])
    decay = int(info.get("decay_per_level") or 0)
    if decay <= 0:
        return base_spawn

    steps_above = int(level_num) - min_level
    return max(0, base_spawn - decay * steps_above)


def get_floor_positions_for_monsters(grid: List[List[str]], exit_x: int, exit_y: int) -> List[Tuple[int, int]]:
    positions: List[Tuple[int, int]] = []
    min_start_distance = int(DUNGEON_MONSTER_MIN_START_DISTANCE)
    start_x, start_y = 1, 1

    for y in range(len(grid)):
        for x in range(len(grid[0])):
            if grid[y][x] != FLOOR:
                continue
            if (x, y) in ((start_x, start_y), (exit_x, exit_y)):
                continue
            if abs(x - start_x) + abs(y - start_y) < min_start_distance:
                continue
            positions.append((x, y))

    return positions


# Helper to get valid teleport positions for the potion
def get_valid_teleport_positions(run: Dict[str, Any]) -> List[Tuple[int, int]]:
    occupied = {
        (int(run["pos_x"]), int(run["pos_y"])),
        (int(run["exit_x"]), int(run["exit_y"])),
    }
    for monster in (run.get("monsters") or []):
        occupied.add((int(monster["x"]), int(monster["y"])))

    positions: List[Tuple[int, int]] = []
    for y in range(int(run["height"])):
        for x in range(int(run["width"])):
            if run["map"][y][x] != FLOOR:
                continue
            if (x, y) in occupied:
                continue
            positions.append((x, y))
    return positions


def spawn_monsters_for_level(grid: List[List[str]], level_num: int, exit_x: int, exit_y: int) -> List[Dict[str, Any]]:
    candidates: List[int] = []
    for class_id in sorted(MONSTER_CLASS_DEFS.keys()):
        chance = get_monster_spawn_chance(int(level_num), int(class_id))
        if chance <= 0:
            continue
        if random.randint(1, 100) <= chance:
            candidates.append(int(class_id))

    if not candidates:
        return []

    random.shuffle(candidates)
    candidates = candidates[:MAX_MONSTERS_PER_RUN]

    positions = get_floor_positions_for_monsters(grid, int(exit_x), int(exit_y))
    if not positions:
        return []
    random.shuffle(positions)

    monsters: List[Dict[str, Any]] = []
    for class_id in candidates:
        if not positions:
            break
        mx, my = positions.pop()
        info = MONSTER_CLASS_DEFS[int(class_id)]
        monsters.append(
            {
                "class_id": int(class_id),
                "icon": str(info["icon"]),
                "sight": int(info["sight"]),
                "x": int(mx),
                "y": int(my),
            }
        )
    return monsters


def monster_can_see_player(monster: Dict[str, Any], player_x: int, player_y: int) -> bool:
    return abs(int(monster["x"]) - int(player_x)) + abs(int(monster["y"]) - int(player_y)) <= int(monster.get("sight") or 0)


def is_monster_on_player(run: Dict[str, Any]) -> bool:
    px = int(run["pos_x"])
    py = int(run["pos_y"])
    for monster in run.get("monsters") or []:
        if int(monster["x"]) == px and int(monster["y"]) == py:
            return True
    return False


def find_next_step_towards_player(run: Dict[str, Any], monster: Dict[str, Any], occupied: set[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    from collections import deque

    start = (int(monster["x"]), int(monster["y"]))
    goal = (int(run["pos_x"]), int(run["pos_y"]))
    width = int(run["width"])
    height = int(run["height"])
    grid = run["map"]

    queue = deque([start])
    prev: Dict[Tuple[int, int], Optional[Tuple[int, int]]] = {start: None}
    dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]

    while queue:
        x, y = queue.popleft()
        if (x, y) == goal:
            break

        for dx, dy in dirs:
            nx, ny = x + dx, y + dy
            if nx < 0 or ny < 0 or nx >= width or ny >= height:
                continue
            if grid[ny][nx] == WALL:
                continue
            if (nx, ny) in prev:
                continue
            if (nx, ny) in occupied and (nx, ny) != goal:
                continue
            prev[(nx, ny)] = (x, y)
            queue.append((nx, ny))

    if goal not in prev:
        return None

    cur = goal
    while prev[cur] is not None and prev[cur] != start:
        cur = prev[cur]

    if prev[cur] is None:
        return None
    return cur


def move_monsters(run: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    monsters = [dict(m) for m in (run.get("monsters") or [])]
    player_x = int(run["pos_x"])
    player_y = int(run["pos_y"])

    for idx, monster in enumerate(monsters):
        if not monster_can_see_player(monster, player_x, player_y):
            continue

        occupied = {
            (int(other["x"]), int(other["y"]))
            for j, other in enumerate(monsters)
            if j != idx
        }
        next_step = find_next_step_towards_player(run, monster, occupied)
        if next_step is None:
            continue

        monster["x"], monster["y"] = int(next_step[0]), int(next_step[1])
        monsters[idx] = monster
        if int(monster["x"]) == player_x and int(monster["y"]) == player_y:
            run["monsters"] = monsters
            run["status"] = "dead"
            return run, True

    run["monsters"] = monsters
    return run, False


# ---------------------------
# Rendering
# ---------------------------
def get_theme_tiles(theme: str) -> Tuple[str, str]:
    normalized = normalize_dungeon_theme(theme)
    if normalized == "light":
        return VIS_FLOOR_LIGHT, VIS_UNKNOWN_LIGHT
    return VIS_FLOOR_DARK, VIS_UNKNOWN_DARK


def render_run(run: Dict[str, Any], vision_radius: int, theme: str = "dark") -> str:
    px = int(run["pos_x"])
    py = int(run["pos_y"])
    ex = int(run["exit_x"])
    ey = int(run["exit_y"])
    width = int(run["width"])
    height = int(run["height"])
    monster_positions = {
        (int(monster["x"]), int(monster["y"])): str(monster.get("icon") or "👾")
        for monster in (run.get("monsters") or [])
    }
    star_positions = {
        (int(star["x"]), int(star["y"]))
        for star in (run.get("stars") or [])
    }
    diamond_positions = {
        (int(diamond["x"]), int(diamond["y"]))
        for diamond in (run.get("diamonds") or [])
    }
    vis_floor, vis_unknown = get_theme_tiles(theme)
    rows: List[str] = []
    for y in range(height):
        line: List[str] = []
        for x in range(width):
            dist = abs(px - x) + abs(py - y)
            if dist > vision_radius:
                line.append(vis_unknown)
                continue

            if x == px and y == py:
                if str(run.get("status") or "") == "dead":
                    line.append(VIS_PLAYER_DEAD)
                else:
                    line.append(VIS_PLAYER)
            elif (x, y) in monster_positions:
                line.append(monster_positions[(x, y)])
            elif (x, y) in star_positions:
                line.append(VIS_XP_STAR)
            elif (x, y) in diamond_positions:
                line.append(VIS_DIAMOND)
            elif x == ex and y == ey:
                line.append(VIS_EXIT)
            elif run["map"][y][x] == WALL:
                line.append(VIS_WALL)
            else:
                line.append(vis_floor)

        rows.append(" ".join(line))
    return "\n".join(rows)


# Get total XP for a Discord user from tipbot DB.
def get_tipbot_xp_total(discord_id: int) -> int:
    con = tipbot_db()
    try:
        profile = get_or_create_nns_profile(con, int(discord_id))
        return int(profile.get("xp_total") or 0)
    finally:
        con.close()


def get_run_embed(user: discord.abc.User, run: Dict[str, Any], vision_radius: int, extra: Optional[str] = None) -> discord.Embed:
    total_monsters = len(run.get("monsters") or [])
    xp_total = get_tipbot_xp_total(int(run.get("discord_id") or 0))
    nns_balance_sat = get_tipbot_balance_sat(int(run.get("discord_id") or 0))

    con = dungeon_db()
    try:
        upgrades = get_or_create_upgrades(con, int(run.get("discord_id") or 0))
        current_vision_bonus = int(upgrades.get("vision_bonus") or 0)
    finally:
        con.close()

    if current_vision_bonus >= int(DUNGEON_VISION_MAX_BONUS):
        next_vision_cost_text = "max"
    else:
        next_vision_cost_text = format_sat_to_nns(get_vision_upgrade_cost_sat(current_vision_bonus + 1))

    render_theme = normalize_dungeon_theme(upgrades.get("render_theme"))
    desc = render_run(run, vision_radius, render_theme)
    desc += f"\nLevel: **{int(run.get('level_num') or 1)}**"
    desc += f"\nVision: **{vision_radius}**"
    desc += f"\nTheme: **{render_theme}**"
    # desc += f"\nNext vision cost: **{next_vision_cost_text}**"
    desc += f"\nTeleport uses: **{int(run.get('teleport_uses') or 0)}/5**"
    # desc += f"\nMonsters: **{total_monsters}**"
    # desc += f"\nXP Stars: **{len(run.get('stars') or [])}**"
    # desc += f"\nDiamonds: **{len(run.get('diamonds') or [])}**"
    # desc += f"\nBanked XP: **{int(run.get('pending_star_xp') or 0)}**"
    # desc += f"\nBanked NNS: **{format_sat_to_nns_int(int(run.get('pending_diamond_sat') or 0))}**"
    desc += f"\nXP: **{xp_total}**"
    desc += f"\nNNS: **{format_sat_to_nns_int(nns_balance_sat)}**"
    if extra:
        desc += f"\n{extra}"

    embed = discord.Embed(
        title="🗺️ Dungeon Maze v1.0-alpha",
        description=desc,
        color=discord.Color.blurple(),
    )
    # embed.set_footer(text=f"Player: {user.display_name}")
    return embed


# ---------------------------
# Game logic
# ---------------------------
def try_move(run: Dict[str, Any], dx: int, dy: int) -> Tuple[Dict[str, Any], str, bool, int, int, bool]:
    if run["status"] != "active":
        return run, "This run is already finished.", True, 0, 0, False

    nx = int(run["pos_x"]) + int(dx)
    ny = int(run["pos_y"]) + int(dy)

    if nx < 0 or ny < 0 or nx >= int(run["width"]) or ny >= int(run["height"]):
        return run, "You hit the boundary.", False, 0, 0, False

    if run["map"][ny][nx] == WALL:
        return run, "A wall blocks your path.", False, 0, 0, False

    run["pos_x"] = nx
    run["pos_y"] = ny
    run["steps"] = int(run["steps"]) + 1

    extra_xp = collect_xp_star_if_present(run)
    diamonds_collected = collect_diamond_if_present(run)

    if is_monster_on_player(run):
        run["status"] = "dead"
        return run, "💀 You ran straight into a monster!", True, extra_xp, diamonds_collected, True

    run, monster_hit = move_monsters(run)
    if monster_hit:
        return run, "💀 A monster spotted you and caught you!", True, extra_xp, diamonds_collected, True

    if nx == int(run["exit_x"]) and ny == int(run["exit_y"]):
        return run, "🚪 You are standing on the exit. Press **Go to Next Level** to continue.", False, extra_xp, diamonds_collected, True

    return run, "Moved.", False, extra_xp, diamonds_collected, True


# ---------------------------
# Discord bot
# ---------------------------
intents = discord.Intents.default()


class NNSDungeonBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        init_dungeon_db()
        init_tipbot_bridge_tables()

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"[nns_dungeon_bot] Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            print(f"[nns_dungeon_bot] Synced {len(synced)} global commands")


bot = NNSDungeonBot()


class DungeonView(ui.View):
    @ui.button(label="Buy Teleport Potion", style=discord.ButtonStyle.primary, row=2)
    async def buy_teleport_button(self, interaction: discord.Interaction, button: ui.Button):
        if self._busy:
            await self._try_busy_notice(interaction)
            return

        self._busy = True
        try:
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except NotFound:
                    return

            con = dungeon_db()
            try:
                con.execute("BEGIN IMMEDIATE;")
                run = get_active_run(con, self.owner_id)
                if not run:
                    con.execute("ROLLBACK;")
                    await interaction.followup.send("No active dungeon run found.", ephemeral=True)
                    return

                if int(run.get("teleport_uses") or 0) >= 5:
                    con.execute("ROLLBACK;")
                    upgrades = get_or_create_upgrades(con, self.owner_id)
                    vision_radius = int(DUNGEON_VISION_BASE) + int(upgrades.get("vision_bonus") or 0)
                    embed = get_run_embed(
                        interaction.user,
                        run,
                        vision_radius,
                        "You already used the maximum of **5 teleport potions** on this level.",
                    )
                    await interaction.edit_original_response(embed=embed, view=self)
                    return

                valid_positions = get_valid_teleport_positions(run)
                if not valid_positions:
                    con.execute("ROLLBACK;")
                    upgrades = get_or_create_upgrades(con, self.owner_id)
                    vision_radius = int(DUNGEON_VISION_BASE) + int(upgrades.get("vision_bonus") or 0)
                    embed = get_run_embed(
                        interaction.user,
                        run,
                        vision_radius,
                        "No valid teleport destination found on this level.",
                    )
                    await interaction.edit_original_response(embed=embed, view=self)
                    return

                try:
                    debit_nns_internal_tipbot(
                        self.owner_id,
                        int(DUNGEON_TELEPORT_COST_SAT),
                        f"dungeon teleport potion level={int(run.get('level_num') or 1)} use={int(run.get('teleport_uses') or 0) + 1}",
                    )
                except Exception as e:
                    con.execute("ROLLBACK;")
                    upgrades = get_or_create_upgrades(con, self.owner_id)
                    vision_radius = int(DUNGEON_VISION_BASE) + int(upgrades.get("vision_bonus") or 0)
                    embed = get_run_embed(
                        interaction.user,
                        run,
                        vision_radius,
                        str(e),
                    )
                    await interaction.edit_original_response(embed=embed, view=self)
                    return

                new_x, new_y = random.choice(valid_positions)
                run["pos_x"] = int(new_x)
                run["pos_y"] = int(new_y)
                run["teleport_uses"] = int(run.get("teleport_uses") or 0) + 1
                save_run(con, run)
                con.execute(
                    "INSERT INTO dungeon_shop_log(ts, discord_id, item_key, cost_sat, note) VALUES(?,?,?,?,?)",
                    (now_ts(), int(self.owner_id), "teleport_potion", int(DUNGEON_TELEPORT_COST_SAT), f"teleport to {new_x},{new_y} level={int(run.get('level_num') or 1)}"),
                )
                con.execute("COMMIT;")
            except Exception:
                try:
                    con.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            finally:
                con.close()

            con2 = dungeon_db()
            try:
                upgrades = get_or_create_upgrades(con2, self.owner_id)
                vision_radius = int(DUNGEON_VISION_BASE) + int(upgrades.get("vision_bonus") or 0)
            finally:
                con2.close()

            remaining_sat = get_tipbot_balance_sat(self.owner_id)
            embed = get_run_embed(
                interaction.user,
                run,
                vision_radius,
                f"Teleport potion used ✅ Teleported to a random free tile. Cost: **{format_sat_to_nns_int(DUNGEON_TELEPORT_COST_SAT)} NNS** | Remaining NNS: **{format_sat_to_nns(remaining_sat)}**",
            )
            current_bonus = int(upgrades.get("vision_bonus") or 0)
            if current_bonus >= int(DUNGEON_VISION_MAX_BONUS):
                self.buy_vision_button.label = "Vision (max)"
            else:
                self.buy_vision_button.label = f"Buy Vision ({format_sat_to_nns_int(get_vision_upgrade_cost_sat(current_bonus + 1))} NNS)"
            self.buy_teleport_button.label = f"Teleport ({format_sat_to_nns_int(DUNGEON_TELEPORT_COST_SAT)} NNS)"
            try:
                await interaction.edit_original_response(embed=embed, view=self)
            except NotFound:
                return
        finally:
            self._busy = False

    @ui.button(label="Shop", style=discord.ButtonStyle.secondary, row=2, disabled=True)
    async def spacer_top_left(self, interaction: discord.Interaction, button: ui.Button):
        pass

    @ui.button(label="Buy Vision", style=discord.ButtonStyle.primary, row=2)
    async def buy_vision_button(self, interaction: discord.Interaction, button: ui.Button):
        if self._busy:
            await self._try_busy_notice(interaction)
            return

        self._busy = True
        try:
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except NotFound:
                    return

            con = dungeon_db()
            try:
                run = get_active_run(con, self.owner_id)
                if not run:
                    await interaction.followup.send("No active dungeon run found.", ephemeral=True)
                    return

                upgrades = get_or_create_upgrades(con, self.owner_id)
                current_bonus = int(upgrades.get("vision_bonus") or 0)
            finally:
                con.close()

            if current_bonus >= int(DUNGEON_VISION_MAX_BONUS):
                vision_radius = int(DUNGEON_VISION_BASE) + int(current_bonus)
                embed = get_run_embed(
                    interaction.user,
                    run,
                    vision_radius,
                    f"Your vision upgrade is already at the maximum bonus (**{DUNGEON_VISION_MAX_BONUS}**).",
                )
                self.buy_vision_button.label = "Vision (max)"
                self.buy_teleport_button.label = f"Teleport ({format_sat_to_nns_int(DUNGEON_TELEPORT_COST_SAT)} NNS)"
                await interaction.edit_original_response(embed=embed, view=self)
                return

            next_bonus = current_bonus + 1
            cost_sat = get_vision_upgrade_cost_sat(next_bonus)

            try:
                debit_nns_internal_tipbot(
                    self.owner_id,
                    int(cost_sat),
                    f"dungeon vision upgrade to level {next_bonus}",
                )
            except Exception as e:
                vision_radius = int(DUNGEON_VISION_BASE) + int(current_bonus)
                embed = get_run_embed(
                    interaction.user,
                    run,
                    vision_radius,
                    str(e),
                )
                self.buy_vision_button.label = f"Buy Vision ({format_sat_to_nns_int(cost_sat)} NNS)"
                self.buy_teleport_button.label = f"Teleport ({format_sat_to_nns_int(DUNGEON_TELEPORT_COST_SAT)} NNS)"
                await interaction.edit_original_response(embed=embed, view=self)
                return

            con2 = dungeon_db()
            try:
                con2.execute("BEGIN IMMEDIATE;")
                run = get_active_run(con2, self.owner_id)
                if not run:
                    con2.execute("ROLLBACK;")
                    await interaction.followup.send("No active dungeon run found.", ephemeral=True)
                    return

                upgrades = get_or_create_upgrades(con2, self.owner_id)
                ts = now_ts()
                con2.execute(
                    "UPDATE dungeon_upgrades SET vision_bonus=?, updated_at=? WHERE discord_id=?",
                    (int(next_bonus), ts, int(self.owner_id)),
                )
                con2.execute(
                    "INSERT INTO dungeon_shop_log(ts, discord_id, item_key, cost_sat, note) VALUES(?,?,?,?,?)",
                    (ts, int(self.owner_id), "vision_bonus", int(cost_sat), f"vision bonus -> {next_bonus}"),
                )
                con2.execute("COMMIT;")
            except Exception:
                try:
                    con2.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            finally:
                con2.close()

            remaining_sat = get_tipbot_balance_sat(self.owner_id)
            vision_radius = int(DUNGEON_VISION_BASE) + int(next_bonus)
            embed = get_run_embed(
                interaction.user,
                run,
                vision_radius,
                f"Vision upgraded ✅ New bonus: **+{next_bonus}** | Cost: **{format_sat_to_nns_int(cost_sat)} NNS** | Remaining NNS: **{format_sat_to_nns(remaining_sat)}**",
            )
            if next_bonus >= int(DUNGEON_VISION_MAX_BONUS):
                self.buy_vision_button.label = "Vision (max)"
            else:
                self.buy_vision_button.label = f"Buy Vision ({format_sat_to_nns_int(get_vision_upgrade_cost_sat(next_bonus + 1))} NNS)"
            self.buy_teleport_button.label = f"Teleport ({format_sat_to_nns_int(DUNGEON_TELEPORT_COST_SAT)} NNS)"
            try:
                await interaction.edit_original_response(embed=embed, view=self)
            except NotFound:
                return
        finally:
            self._busy = False

    def __init__(self, owner_id: int):
        super().__init__(timeout=DUNGEON_VIEW_TIMEOUT_SECONDS)
        self.owner_id = int(owner_id)
        self.cleared_level_num: Optional[int] = None
        self._busy = False
        self._last_move_ts = 0.0
        self._auto_stop_requested = False
        self.next_level_button.disabled = True
        self.restart_game_button.disabled = True
        self.stop_auto_button.disabled = True
        self.buy_teleport_button.label = f"Teleport ({format_sat_to_nns_int(DUNGEON_TELEPORT_COST_SAT)} NNS)"
        self.buy_vision_button.label = f"Buy Vision ({format_sat_to_nns_int(get_vision_upgrade_cost_sat(1))} NNS)"

    async def _try_move_cooldown_notice(self, interaction: discord.Interaction, remaining_ms: int) -> None:
        try:
            msg = f"Too fast. Please wait **{max(1, int(remaining_ms))} ms**."
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            pass

    def disable_movement_buttons(self) -> None:
        self.up_button.disabled = True
        self.left_button.disabled = True
        self.right_button.disabled = True
        self.down_button.disabled = True
        self.fast_up_button.disabled = True
        self.fast_left_button.disabled = True
        self.fast_right_button.disabled = True
        self.fast_down_button.disabled = True

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, ui.Button):
                item.disabled = True

    def set_active_controls(self, on_exit_tile: bool = False) -> None:
        self.up_button.disabled = False
        self.left_button.disabled = False
        self.right_button.disabled = False
        self.down_button.disabled = False
        self.fast_up_button.disabled = False
        self.fast_left_button.disabled = False
        self.fast_right_button.disabled = False
        self.fast_down_button.disabled = False
        self.exit_game_button.disabled = False
        self.buy_vision_button.disabled = False
        self.buy_teleport_button.disabled = False
        self.stop_auto_button.disabled = True
        self.next_level_button.disabled = not on_exit_tile
        self.restart_game_button.disabled = True

    def set_dead_controls(self) -> None:
        self.disable_all()
        self.stop_auto_button.disabled = True
        self.restart_game_button.disabled = False
        self.exit_game_button.disabled = False

    @ui.button(label="⏹️", style=discord.ButtonStyle.danger, row=2)
    async def stop_auto_button(self, interaction: discord.Interaction, button: ui.Button):
        if not self._busy:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except Exception:
                pass
            return

        self._auto_stop_requested = True
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception:
            pass

    async def _try_busy_notice(self, interaction: discord.Interaction) -> None:
        try:
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except NotFound:
                    return

            con = dungeon_db()
            try:
                run = get_active_run(con, self.owner_id)
                if not run:
                    await interaction.followup.send("Please wait a moment...", ephemeral=True)
                    return
                upgrades = get_or_create_upgrades(con, self.owner_id)
                vision_radius = int(DUNGEON_VISION_BASE) + int(upgrades.get("vision_bonus") or 0)
            finally:
                con.close()

            embed = get_run_embed(interaction.user, run, vision_radius, "Please wait a moment...")
            await interaction.edit_original_response(embed=embed, view=self)
        except Exception:
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message("Please wait a moment...", ephemeral=True)
                else:
                    await interaction.followup.send("Please wait a moment...", ephemeral=True)
            except Exception:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("This is not your dungeon run.", ephemeral=True)
            return False
        return True

    async def handle_move(self, interaction: discord.Interaction, dx: int, dy: int):
        if self._busy:
            await self._try_busy_notice(interaction)
            return

        now_monotonic = time.monotonic()
        cooldown_seconds = max(0, int(DUNGEON_MOVE_COOLDOWN_MS)) / 1000.0
        if cooldown_seconds > 0 and self._last_move_ts > 0:
            elapsed = now_monotonic - self._last_move_ts
            if elapsed < cooldown_seconds:
                remaining_ms = int((cooldown_seconds - elapsed) * 1000)
                await self._try_move_cooldown_notice(interaction, remaining_ms)
                return

        self._busy = True
        self._last_move_ts = now_monotonic
        try:
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except NotFound:
                    return

            con = dungeon_db()
            try:
                con.execute("BEGIN IMMEDIATE;")
                run = get_active_run(con, self.owner_id)
                if not run:
                    con.execute("ROLLBACK;")
                    await interaction.followup.send("No active dungeon run found.", ephemeral=True)
                    return

                upgrades = get_or_create_upgrades(con, self.owner_id)
                vision_radius = int(DUNGEON_VISION_BASE) + int(upgrades.get("vision_bonus") or 0)

                run, msg, finished, bonus_xp, diamonds_collected, did_move = try_move(run, dx, dy)
                cleared_level_num = int(run.get("level_num") or 1)
                on_exit_tile = int(run.get("pos_x") or 0) == int(run.get("exit_x") or -1) and int(run.get("pos_y") or 0) == int(run.get("exit_y") or -1)

                banked_star_xp = 0
                banked_diamond_sat = 0
                if int(bonus_xp) > 0:
                    banked_star_xp = int(bonus_xp)
                    run["pending_star_xp"] = int(run.get("pending_star_xp") or 0) + int(banked_star_xp)
                if int(diamonds_collected) > 0:
                    for _ in range(int(diamonds_collected)):
                        payout_sat = random.randint(1, 10) * NNS_SATS
                        banked_diamond_sat += int(payout_sat)
                    run["pending_diamond_sat"] = int(run.get("pending_diamond_sat") or 0) + int(banked_diamond_sat)

                if finished and run["status"] == "dead":
                    increment_deaths_and_reset_progress(con, self.owner_id)
                    delete_run(con, self.owner_id)
                elif did_move:
                    save_run(con, run)

                con.execute("COMMIT;")
            except Exception:
                try:
                    con.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            finally:
                con.close()

            level_up_text = ""
            extra_text = msg
            if int(banked_star_xp) > 0:
                extra_text += f"\n✨ Collected XP star: **+{int(banked_star_xp)} XP banked** for level clear"
            if int(banked_diamond_sat) > 0:
                extra_text += f"\n💎 Collected diamond: **+{format_sat_to_nns_int(banked_diamond_sat)} NNS banked** for level clear"

            if finished and run["status"] == "dead":
                self.cleared_level_num = None
                self.set_dead_controls()
                extra_text += "\n❌ Game over. Press **Restart Game** to try again."
            else:
                self.set_active_controls(on_exit_tile=on_exit_tile)
                if on_exit_tile:
                    extra_text += "\n▶ Press **Go to Next Level** to continue."
                extra_text += level_up_text

            embed = get_run_embed(interaction.user, run, vision_radius, extra_text)
            try:
                await interaction.edit_original_response(embed=embed, view=self)
            except NotFound:
                return
        finally:
            self._busy = False

    @ui.button(label="slow", style=discord.ButtonStyle.secondary, row=0, disabled=True)
    async def spacer_top_left(self, interaction: discord.Interaction, button: ui.Button):
        pass

    @ui.button(label="▲", style=discord.ButtonStyle.secondary, row=0)
    async def up_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.handle_move(interaction, 0, -1)

    @ui.button(label="▼", style=discord.ButtonStyle.secondary, row=0)
    async def down_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.handle_move(interaction, 0, 1)

    @ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def left_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.handle_move(interaction, -1, 0)

    @ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def right_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.handle_move(interaction, 1, 0)

    @ui.button(label="fast", style=discord.ButtonStyle.secondary, row=1, disabled=True)
    async def spacer_mid(self, interaction: discord.Interaction, button: ui.Button):
        pass

    @ui.button(label="⏫", style=discord.ButtonStyle.secondary, row=1)
    async def fast_up_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.run_fast_forward(interaction, 0, -1)

    @ui.button(label="⏬", style=discord.ButtonStyle.secondary, row=1)
    async def fast_down_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.run_fast_forward(interaction, 0, 1)

    @ui.button(label="⏪", style=discord.ButtonStyle.secondary, row=1)
    async def fast_left_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.run_fast_forward(interaction, -1, 0)

    @ui.button(label="⏩", style=discord.ButtonStyle.secondary, row=1)
    async def fast_right_button(self, interaction: discord.Interaction, button: ui.Button):
        await self.run_fast_forward(interaction, 1, 0)

    async def run_fast_forward(self, interaction: discord.Interaction, dx: int, dy: int):
        if self._busy:
            await self._try_busy_notice(interaction)
            return

        self._busy = True
        self._auto_stop_requested = False
        try:
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except NotFound:
                    return

            con0 = dungeon_db()
            try:
                run0 = get_active_run(con0, self.owner_id)
                if not run0:
                    await interaction.followup.send("No active dungeon run found.", ephemeral=True)
                    return
                upgrades0 = get_or_create_upgrades(con0, self.owner_id)
                vision_radius0 = int(DUNGEON_VISION_BASE) + int(upgrades0.get("vision_bonus") or 0)
            finally:
                con0.close()

            self.disable_all()
            self.stop_auto_button.disabled = False
            try:
                await interaction.edit_original_response(
                    embed=get_run_embed(interaction.user, run0, vision_radius0, "⏩ Fast-forwarding... Press **⏹️** to stop."),
                    view=self,
                )
            except NotFound:
                return

            while True:
                if self._auto_stop_requested:
                    con_stop = dungeon_db()
                    try:
                        run_stop = get_active_run(con_stop, self.owner_id)
                        if not run_stop:
                            await interaction.followup.send("No active dungeon run found.", ephemeral=True)
                            return
                        upgrades_stop = get_or_create_upgrades(con_stop, self.owner_id)
                        vision_radius_stop = int(DUNGEON_VISION_BASE) + int(upgrades_stop.get("vision_bonus") or 0)
                    finally:
                        con_stop.close()

                    on_exit_tile_stop = int(run_stop.get("pos_x") or 0) == int(run_stop.get("exit_x") or -1) and int(run_stop.get("pos_y") or 0) == int(run_stop.get("exit_y") or -1)
                    self.set_active_controls(on_exit_tile=on_exit_tile_stop)
                    embed_stop = get_run_embed(interaction.user, run_stop, vision_radius_stop, "⏹️ Auto-run stopped.")
                    try:
                        await interaction.edit_original_response(embed=embed_stop, view=self)
                    except NotFound:
                        return
                    return
                con = dungeon_db()
                try:
                    con.execute("BEGIN IMMEDIATE;")
                    run = get_active_run(con, self.owner_id)
                    if not run:
                        con.execute("ROLLBACK;")
                        await interaction.followup.send("No active dungeon run found.", ephemeral=True)
                        return

                    upgrades = get_or_create_upgrades(con, self.owner_id)
                    vision_radius = int(DUNGEON_VISION_BASE) + int(upgrades.get("vision_bonus") or 0)

                    run, msg, finished, bonus_xp, diamonds_collected, did_move = try_move(run, dx, dy)
                    cleared_level_num = int(run.get("level_num") or 1)
                    on_exit_tile = int(run.get("pos_x") or 0) == int(run.get("exit_x") or -1) and int(run.get("pos_y") or 0) == int(run.get("exit_y") or -1)

                    banked_star_xp = 0
                    banked_diamond_sat = 0
                    if int(bonus_xp) > 0:
                        banked_star_xp = int(bonus_xp)
                        run["pending_star_xp"] = int(run.get("pending_star_xp") or 0) + int(banked_star_xp)
                    if int(diamonds_collected) > 0:
                        for _ in range(int(diamonds_collected)):
                            payout_sat = random.randint(1, 10) * NNS_SATS
                            banked_diamond_sat += int(payout_sat)
                        run["pending_diamond_sat"] = int(run.get("pending_diamond_sat") or 0) + int(banked_diamond_sat)

                    if finished and run["status"] == "dead":
                        increment_deaths_and_reset_progress(con, self.owner_id)
                        delete_run(con, self.owner_id)
                    elif did_move:
                        save_run(con, run)

                    con.execute("COMMIT;")
                except Exception:
                    try:
                        con.execute("ROLLBACK;")
                    except Exception:
                        pass
                    raise
                finally:
                    con.close()

                level_up_text = ""
                extra_text = msg
                if int(banked_star_xp) > 0:
                    extra_text += f"\n✨ Collected XP star: **+{int(banked_star_xp)} XP banked** for level clear"
                if int(banked_diamond_sat) > 0:
                    extra_text += f"\n💎 Collected diamond: **+{format_sat_to_nns_int(banked_diamond_sat)} NNS banked** for level clear"

                stop_auto = False
                if finished and run["status"] == "dead":
                    self.cleared_level_num = None
                    self.set_dead_controls()
                    extra_text += "\n❌ Game over. Press **Restart Game** to try again."
                    stop_auto = True
                elif not did_move:
                    self.set_active_controls(on_exit_tile=False)
                    extra_text = f"⏹️ Auto-run stopped. {msg}"
                    stop_auto = True
                elif on_exit_tile or int(bonus_xp) > 0 or int(diamonds_collected) > 0:
                    self.set_active_controls(on_exit_tile=on_exit_tile)
                    if on_exit_tile:
                        extra_text += "\n▶ Press **Go to Next Level** to continue."
                    extra_text += level_up_text
                    stop_auto = True
                else:
                    self.disable_all()
                    self.stop_auto_button.disabled = False
                    extra_text = "⏩ Fast-forwarding... Press **⏹️** to stop."

                embed = get_run_embed(interaction.user, run, vision_radius, extra_text)
                try:
                    await interaction.edit_original_response(embed=embed, view=self)
                except NotFound:
                    return

                if stop_auto:
                    return

                await asyncio.sleep(max(0.05, int(DUNGEON_FAST_FORWARD_STEP_MS) / 1000.0))
        finally:
            self._busy = False
            self._auto_stop_requested = False

    @ui.button(label="Exit Game", style=discord.ButtonStyle.danger, row=3)
    async def exit_game_button(self, interaction: discord.Interaction, button: ui.Button):
        con = dungeon_db()
        try:
            con.execute("BEGIN IMMEDIATE;")
            run = get_active_run(con, self.owner_id)
            if run:
                delete_run(con, self.owner_id)
            con.execute("COMMIT;")
        except Exception:
            try:
                con.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            con.close()

        try:
            await interaction.response.defer()
        except Exception:
            pass

        await interaction.delete_original_response()


    @ui.button(label="Restart Game", style=discord.ButtonStyle.danger, row=3)
    async def restart_game_button(self, interaction: discord.Interaction, button: ui.Button):
        if self._busy:
            await self._try_busy_notice(interaction)
            return

        self._busy = True
        try:
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except NotFound:
                    return

            con = dungeon_db()
            try:
                con.execute("BEGIN IMMEDIATE;")
                existing_run = get_active_run(con, self.owner_id)
                if existing_run and str(existing_run.get("status") or "") == "active":
                    con.execute("ROLLBACK;")
                    await interaction.followup.send("You already have an active dungeon run.", ephemeral=True)
                    return

                set_saved_current_level(con, self.owner_id, 1)
                run = create_new_run(self.owner_id, level_num=1, con=con)
                save_run(con, run)
                upgrades = get_or_create_upgrades(con, self.owner_id)
                increment_runs_started(con, self.owner_id)
                con.execute("COMMIT;")
            except Exception:
                try:
                    con.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            finally:
                con.close()

            self.cleared_level_num = None
            self.set_active_controls(on_exit_tile=False)

            current_bonus = int(upgrades.get("vision_bonus") or 0)
            if current_bonus >= int(DUNGEON_VISION_MAX_BONUS):
                self.buy_vision_button.label = "Vision (max)"
            else:
                self.buy_vision_button.label = f"Buy Vision ({format_sat_to_nns_int(get_vision_upgrade_cost_sat(current_bonus + 1))} NNS)"
            self.buy_teleport_button.label = f"Teleport ({format_sat_to_nns_int(DUNGEON_TELEPORT_COST_SAT)} NNS)"

            vision_radius = int(DUNGEON_VISION_BASE) + int(upgrades.get("vision_bonus") or 0)
            embed = get_run_embed(interaction.user, run, vision_radius, "Restarted at **Level 1**. Find the exit.")
            try:
                await interaction.edit_original_response(embed=embed, view=self)
            except NotFound:
                return
        finally:
            self._busy = False

    @ui.button(label="Go to Next Level", style=discord.ButtonStyle.success, row=3)
    async def next_level_button(self, interaction: discord.Interaction, button: ui.Button):
        if self._busy:
            await self._try_busy_notice(interaction)
            return

        self._busy = True
        try:
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except NotFound:
                    return

            con = dungeon_db()
            try:
                con.execute("BEGIN IMMEDIATE;")
                run = get_active_run(con, self.owner_id)
                if not run:
                    con.execute("ROLLBACK;")
                    await interaction.followup.send("No active dungeon run found.", ephemeral=True)
                    return

                if not (
                        int(run.get("pos_x") or 0) == int(run.get("exit_x") or -1)
                        and int(run.get("pos_y") or 0) == int(run.get("exit_y") or -1)
                ):
                    con.execute("ROLLBACK;")
                    await interaction.followup.send("You need to stand on the exit tile first.", ephemeral=True)
                    return

                cleared_level_num = int(run.get("level_num") or 1)
                banked_star_xp = int(run.get("pending_star_xp") or 0)
                banked_diamond_sat = int(run.get("pending_diamond_sat") or 0)
                increment_runs_completed(con, self.owner_id, int(run.get("steps") or 0))
                record_highest_level(con, self.owner_id, int(cleared_level_num))
                next_level_num = int(cleared_level_num) + 1
                set_saved_current_level(con, self.owner_id, next_level_num)
                new_run = create_new_run(self.owner_id, level_num=next_level_num, con=con)
                save_run(con, new_run)
                upgrades = get_or_create_upgrades(con, self.owner_id)
                increment_runs_started(con, self.owner_id)
                con.execute("COMMIT;")
            except Exception:
                try:
                    con.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            finally:
                con.close()

            exit_xp = max(0, int(cleared_level_num))
            xp_awarded = int(exit_xp)
            level_up_text = ""
            if int(banked_star_xp) > 0:
                xp_awarded += int(banked_star_xp)

            if int(xp_awarded) > 0:
                xp_info = grant_nns_xp_tipbot(
                    self.owner_id,
                    "dungeon_exit_nns",
                    int(xp_awarded),
                    f"maze exit level={cleared_level_num} exit_xp={int(exit_xp)} star_xp={int(banked_star_xp)}",
                )
                if xp_info.get("leveled_up"):
                    level_up_text = f"\n🚀 Level up! Reached **Level {int(xp_info['new_level'])}**."

            diamond_payout_text = ""
            if int(banked_diamond_sat) > 0:
                try:
                    credited_sat = credit_nns_from_treasury_tipbot(
                        self.owner_id,
                        int(banked_diamond_sat),
                        f"dungeon diamond payout level={cleared_level_num}",
                    )
                    diamond_payout_text = f"\n💎 Banked diamonds paid out: **+{format_sat_to_nns_int(credited_sat)} NNS**"
                except Exception as e:
                    diamond_payout_text = f"\n💎 Banked diamonds payout failed: {e}"

            self.cleared_level_num = None
            self.set_active_controls(on_exit_tile=False)

            current_bonus = int(upgrades.get("vision_bonus") or 0)
            if current_bonus >= int(DUNGEON_VISION_MAX_BONUS):
                self.buy_vision_button.label = "Vision (max)"
            else:
                self.buy_vision_button.label = f"Buy Vision ({format_sat_to_nns_int(get_vision_upgrade_cost_sat(current_bonus + 1))} NNS)"
            self.buy_teleport_button.label = f"Teleport ({format_sat_to_nns_int(DUNGEON_TELEPORT_COST_SAT)} NNS)"

            vision_radius = int(DUNGEON_VISION_BASE) + int(upgrades.get("vision_bonus") or 0)
            extra_text = (
                f"🎉 You cleared **Level {cleared_level_num}** and entered **Level {next_level_num}**."
                f"\n⭐ XP gained: **{xp_awarded}**"
                f"\n✨ Banked star XP paid out: **{int(banked_star_xp)}**"
                f"\n🚪 Exit XP paid out: **{int(exit_xp)}**"
                f"{diamond_payout_text}"
                f"{level_up_text}"
            )
            embed = get_run_embed(interaction.user, new_run, vision_radius, extra_text)
            try:
                await interaction.edit_original_response(embed=embed, view=self)
            except NotFound:
                return
        finally:
            self._busy = False

    async def on_timeout(self):
        self.disable_all()


# ---------------------------
# Commands
# ---------------------------
@bot.tree.command(name="dungeon_start", description="Start a new dungeon maze run.")
async def dungeon_start(interaction: discord.Interaction):
    if int(DUNGEON_ALLOWED_CHANNEL_ID or 0) > 0 and int(interaction.channel_id or 0) != int(DUNGEON_ALLOWED_CHANNEL_ID):
        await interaction.response.send_message(
            f"This command can only be used in <#{int(DUNGEON_ALLOWED_CHANNEL_ID)}>.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=False)

    con = dungeon_db()
    try:
        con.execute("BEGIN IMMEDIATE;")
        existing_run = get_active_run(con, interaction.user.id)
        if existing_run and str(existing_run.get("status") or "") == "active":
            run = existing_run
            upgrades = get_or_create_upgrades(con, interaction.user.id)
            con.execute("COMMIT;")
            resumed_existing = True
        else:
            saved_level = get_saved_current_level(con, interaction.user.id)
            run = create_new_run(interaction.user.id, level_num=saved_level, con=con)
            save_run(con, run)
            set_saved_current_level(con, interaction.user.id, saved_level)
            upgrades = get_or_create_upgrades(con, interaction.user.id)
            increment_runs_started(con, interaction.user.id)
            con.execute("COMMIT;")
            resumed_existing = False
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        con.close()

    vision_radius = int(DUNGEON_VISION_BASE) + int(upgrades.get("vision_bonus") or 0)
    if resumed_existing:
        status_text = (
            f"Reopened your active run at **Level {int(run.get('level_num') or 1)}**. "
            f"Find the exit. Spawned monsters: **{len(run.get('monsters') or [])}**"
        )
    else:
        status_text = (
            f"Started **Level {int(run.get('level_num') or 1)}**. "
            f"Find the exit. Spawned monsters: **{len(run.get('monsters') or [])}**"
        )

    embed = get_run_embed(
        interaction.user,
        run,
        vision_radius,
        status_text,
    )
    view = DungeonView(interaction.user.id)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="dungeon_status", description="Show your current dungeon run.")
async def dungeon_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=False)

    con = dungeon_db()
    try:
        run = get_active_run(con, interaction.user.id)
        if not run:
            await interaction.followup.send("You do not have an active dungeon run.", ephemeral=True)
            return

        upgrades = get_or_create_upgrades(con, interaction.user.id)
        vision_radius = int(DUNGEON_VISION_BASE) + int(upgrades.get("vision_bonus") or 0)
        embed = get_run_embed(interaction.user, run, vision_radius, f"Status: **{run['status']}**")
        await interaction.followup.send(embed=embed, ephemeral=True)
    finally:
        con.close()


@bot.tree.command(name="dungeon_end", description="End and delete your current dungeon run.")
async def dungeon_end(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=False)

    con = dungeon_db()
    try:
        con.execute("BEGIN IMMEDIATE;")
        run = get_active_run(con, interaction.user.id)
        if not run:
            con.execute("ROLLBACK;")
            await interaction.followup.send("You do not have an active dungeon run.", ephemeral=True)
            return

        delete_run(con, interaction.user.id)
        con.execute("COMMIT;")
        await interaction.followup.send("Your dungeon run has been ended and deleted.", ephemeral=True)
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        con.close()


@bot.tree.command(name="dungeon_theme", description="Change your dungeon render theme.")
@app_commands.describe(theme="Choose dark or light floor/fog tiles")
@app_commands.choices(theme=[
    app_commands.Choice(name="dark", value="dark"),
    app_commands.Choice(name="light", value="light"),
])
async def dungeon_theme(interaction: discord.Interaction, theme: app_commands.Choice[str]):
    chosen_theme = normalize_dungeon_theme(theme.value)

    con = dungeon_db()
    try:
        con.execute("BEGIN IMMEDIATE;")
        get_or_create_upgrades(con, interaction.user.id)
        con.execute(
            "UPDATE dungeon_upgrades SET render_theme=?, updated_at=? WHERE discord_id=?",
            (chosen_theme, now_ts(), int(interaction.user.id)),
        )
        con.execute("COMMIT;")
    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        con.close()

    await interaction.response.send_message(
        f"Dungeon theme updated ✅ Your theme is now **{chosen_theme}**.",
        ephemeral=True,
    )

@bot.tree.command(name="dungeon_help", description="Show dungeon bot help.")
async def dungeon_help(interaction: discord.Interaction):
    text = (
        "**NNS Dungeon Bot**\n\n"
        "• `/dungeon_start` – start a new run or reopen your active run\n"
        "• `/dungeon_status` – show your current run\n"
        "• `/dungeon_end` – end / reset your current run in case the game was closed prematurely\n"
        "• Buy Vision button – buy +1 vision with internal NNS\n"
        "• Teleport – teleport to a random free tile to escape from monsters.\n\n"
        "Notes:\n"
        "• Exit reward: **XP equal to the cleared level number**.\n"
        f"• Vision upgrade cost: **{format_sat_to_nns_int(DUNGEON_VISION_COST_BASE_SAT)} NNS × next bonus level**.\n"
        f"• Teleport potion cost: **{format_sat_to_nns_int(DUNGEON_TELEPORT_COST_SAT)} NNS**, max 5 uses per level.\n"
        "• Game Over resets your resumable progress to Level 1, but your highest level is kept as a high score.\n"
        "• Stand on the exit tile and press **Go to Next Level** to continue.\n"
        "• After Game Over, you can use the **Restart Game** button for a fresh Level 1 run.\n"
        "• `/dungeon_theme <dark|light>` changes your floor/fog theme.\n"
    )
    await interaction.response.send_message(text, ephemeral=True)


def main():
    if not DISCORD_TOKEN:
        raise SystemExit("Missing NNS_DUNGEON_BOT_DISCORD_TOKEN")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()

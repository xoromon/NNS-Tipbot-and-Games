#!/usr/bin/env python3
import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


NNS_SATS = 100_000_000

WALL = "#"
FLOOR = "."

# ASCII fallback tiles
ASCII_PLAYER = "@"
ASCII_PLAYER_DEAD = "X"
ASCII_EXIT = "E"
ASCII_WALL = "#"
ASCII_FLOOR = "."
ASCII_XP_STAR = "*"
ASCII_DIAMOND = "$"
ASCII_MONSTER_DEFAULT = "M"

MONSTER_ICON_TO_ASCII = {
    "🧟‍♂️": "Z",
    "🧌": "T",
    "🧛🏻‍♀️": "V",
    "👹": "O",
    "😈": "I",
    "🐲": "D",
    "💀": "S",
}

# Emoji tiles (matching the game logic/UI as closely as practical)
EMOJI_PLAYER = "🤴🏻"
EMOJI_PLAYER_DEAD = "☠️"
EMOJI_EXIT = "🚪"
EMOJI_WALL = "🪨"
EMOJI_FLOOR = "⬜"
EMOJI_XP_STAR = "✨"
EMOJI_DIAMOND = "💎"
EMOJI_UNKNOWN = "🌫"
EMOJI_MONSTER_DEFAULT = "👾"


def format_sat_to_nns(sat: Optional[int]) -> str:
    sat = int(sat or 0)
    return f"{sat / NNS_SATS:.8f}"


def connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def safe_query(con: sqlite3.Connection, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    try:
        return list(con.execute(sql, params).fetchall())
    except sqlite3.Error:
        return []


def load_tipbot_data(tipbot_path: Optional[Path]) -> Dict[int, Dict[str, Any]]:
    if not tipbot_path or not tipbot_path.exists():
        return {}

    con = connect_ro(tipbot_path)
    try:
        users: Dict[int, Dict[str, Any]] = {}
        if table_exists(con, "users"):
            for row in safe_query(
                con,
                "SELECT discord_id, nns_internal_sat, nns_deposit_address, created_at, updated_at FROM users",
            ):
                users[int(row["discord_id"])] = {
                    "nns_internal_sat": int(row["nns_internal_sat"] or 0),
                    "nns_deposit_address": row["nns_deposit_address"],
                    "tipbot_created_at": row["created_at"],
                    "tipbot_updated_at": row["updated_at"],
                }

        profiles: Dict[int, Dict[str, Any]] = {}
        if table_exists(con, "nns_profiles"):
            for row in safe_query(
                con,
                "SELECT discord_id, xp_total, level, updated_at FROM nns_profiles",
            ):
                profiles[int(row["discord_id"])] = {
                    "xp_total": int(row["xp_total"] or 0),
                    "nns_level": int(row["level"] or 1),
                    "profile_updated_at": row["updated_at"],
                }

        merged: Dict[int, Dict[str, Any]] = {}
        for discord_id in set(users) | set(profiles):
            merged[discord_id] = {}
            merged[discord_id].update(users.get(discord_id, {}))
            merged[discord_id].update(profiles.get(discord_id, {}))
        return merged
    finally:
        con.close()


def monster_ascii(monster: Dict[str, Any]) -> str:
    icon = str(monster.get("icon") or "")
    if icon in MONSTER_ICON_TO_ASCII:
        return MONSTER_ICON_TO_ASCII[icon]
    class_id = int(monster.get("class_id") or 0)
    if class_id > 0:
        return str(class_id % 10)
    return ASCII_MONSTER_DEFAULT


def monster_emoji(monster: Dict[str, Any]) -> str:
    icon = str(monster.get("icon") or "").strip()
    if icon:
        return icon
    return EMOJI_MONSTER_DEFAULT


def render_ascii_map(
    grid: List[List[str]],
    pos_x: int,
    pos_y: int,
    exit_x: int,
    exit_y: int,
    monsters: List[Dict[str, Any]],
    stars: List[Dict[str, Any]],
    diamonds: List[Dict[str, Any]],
    run_status: str,
) -> str:
    monster_positions = {
        (int(monster.get("x", -1)), int(monster.get("y", -1))): monster_ascii(monster)
        for monster in monsters
    }
    star_positions = {
        (int(star.get("x", -1)), int(star.get("y", -1)))
        for star in stars
    }
    diamond_positions = {
        (int(diamond.get("x", -1)), int(diamond.get("y", -1)))
        for diamond in diamonds
    }

    rows: List[str] = []
    for y in range(len(grid)):
        line: List[str] = []
        for x in range(len(grid[y])):
            if x == int(pos_x) and y == int(pos_y):
                line.append(ASCII_PLAYER_DEAD if str(run_status or "") == "dead" else ASCII_PLAYER)
            elif (x, y) in monster_positions:
                line.append(monster_positions[(x, y)])
            elif (x, y) in star_positions:
                line.append(ASCII_XP_STAR)
            elif (x, y) in diamond_positions:
                line.append(ASCII_DIAMOND)
            elif x == int(exit_x) and y == int(exit_y):
                line.append(ASCII_EXIT)
            elif grid[y][x] == WALL:
                line.append(ASCII_WALL)
            else:
                line.append(ASCII_FLOOR)
        rows.append("".join(line))
    return "\n".join(rows)


def render_emoji_map(
    grid: List[List[str]],
    pos_x: int,
    pos_y: int,
    exit_x: int,
    exit_y: int,
    monsters: List[Dict[str, Any]],
    stars: List[Dict[str, Any]],
    diamonds: List[Dict[str, Any]],
    run_status: str,
) -> str:
    monster_positions = {
        (int(monster.get("x", -1)), int(monster.get("y", -1))): monster_emoji(monster)
        for monster in monsters
    }
    star_positions = {
        (int(star.get("x", -1)), int(star.get("y", -1)))
        for star in stars
    }
    diamond_positions = {
        (int(diamond.get("x", -1)), int(diamond.get("y", -1)))
        for diamond in diamonds
    }

    rows: List[str] = []
    for y in range(len(grid)):
        line: List[str] = []
        for x in range(len(grid[y])):
            if x == int(pos_x) and y == int(pos_y):
                line.append(EMOJI_PLAYER_DEAD if str(run_status or "") == "dead" else EMOJI_PLAYER)
            elif (x, y) in monster_positions:
                line.append(monster_positions[(x, y)])
            elif (x, y) in star_positions:
                line.append(EMOJI_XP_STAR)
            elif (x, y) in diamond_positions:
                line.append(EMOJI_DIAMOND)
            elif x == int(exit_x) and y == int(exit_y):
                line.append(EMOJI_EXIT)
            elif grid[y][x] == WALL:
                line.append(EMOJI_WALL)
            else:
                line.append(EMOJI_FLOOR)
        rows.append(" ".join(line))
    return "\n".join(rows)


def summarize_run(row: sqlite3.Row, use_ascii: bool = False) -> Dict[str, Any]:
    try:
        monsters = json.loads(row["monsters_json"] or "[]")
    except Exception:
        monsters = []
    try:
        stars = json.loads(row["stars_json"] or "[]")
    except Exception:
        stars = []
    try:
        diamonds = json.loads(row["diamonds_json"] or "[]")
    except Exception:
        diamonds = []
    try:
        grid = json.loads(row["map_json"] or "[]")
    except Exception:
        grid = []

    run_status = str(row["status"] or "active")
    pos_x = int(row["pos_x"])
    pos_y = int(row["pos_y"])
    exit_x = int(row["exit_x"])
    exit_y = int(row["exit_y"])

    rendered_map = ""
    if grid:
        if use_ascii:
            rendered_map = render_ascii_map(
                grid=grid,
                pos_x=pos_x,
                pos_y=pos_y,
                exit_x=exit_x,
                exit_y=exit_y,
                monsters=monsters,
                stars=stars,
                diamonds=diamonds,
                run_status=run_status,
            )
        else:
            rendered_map = render_emoji_map(
                grid=grid,
                pos_x=pos_x,
                pos_y=pos_y,
                exit_x=exit_x,
                exit_y=exit_y,
                monsters=monsters,
                stars=stars,
                diamonds=diamonds,
                run_status=run_status,
            )

    return {
        "run_level": int(row["level_num"] or 1),
        "run_status": run_status,
        "pos": [pos_x, pos_y],
        "exit": [exit_x, exit_y],
        "steps": int(row["steps"] or 0),
        "teleport_uses": int(row["teleport_uses"] or 0),
        "pending_star_xp": int(row["pending_star_xp"] or 0),
        "pending_diamond_sat": int(row["pending_diamond_sat"] or 0),
        "pending_diamond_nns": format_sat_to_nns(row["pending_diamond_sat"]),
        "monster_count": len(monsters),
        "star_count": len(stars),
        "diamond_count": len(diamonds),
        "rendered_map": rendered_map,
        "map_mode": "ascii" if use_ascii else "emoji",
        "run_created_at": row["created_at"],
        "run_updated_at": row["updated_at"],
    }


def load_dungeon_data(dungeon_path: Path, use_ascii: bool = False) -> List[Dict[str, Any]]:
    con = connect_ro(dungeon_path)
    try:
        stats_rows = safe_query(
            con,
            "SELECT discord_id, runs_started, runs_completed, deaths, total_steps, current_level, highest_level, updated_at FROM dungeon_stats ORDER BY highest_level DESC, runs_completed DESC, discord_id ASC",
        )
        upgrades_rows = {
            int(r["discord_id"]): r
            for r in safe_query(
                con,
                "SELECT discord_id, vision_bonus, max_hp_bonus, attack_bonus, defense_bonus, created_at, updated_at FROM dungeon_upgrades",
            )
        }
        runs_rows = {
            int(r["discord_id"]): r
            for r in safe_query(
                con,
                "SELECT discord_id, map_json, monsters_json, stars_json, diamonds_json, pos_x, pos_y, exit_x, exit_y, level_num, steps, teleport_uses, pending_star_xp, pending_diamond_sat, status, created_at, updated_at FROM dungeon_runs",
            )
        }

        results: List[Dict[str, Any]] = []
        seen = set()
        for row in stats_rows:
            discord_id = int(row["discord_id"])
            seen.add(discord_id)
            entry: Dict[str, Any] = {
                "discord_id": discord_id,
                "runs_started": int(row["runs_started"] or 0),
                "runs_completed": int(row["runs_completed"] or 0),
                "deaths": int(row["deaths"] or 0),
                "total_steps": int(row["total_steps"] or 0),
                "current_level": int(row["current_level"] or 1),
                "highest_level": int(row["highest_level"] or 1),
                "stats_updated_at": row["updated_at"],
            }
            up = upgrades_rows.get(discord_id)
            if up:
                entry["vision_bonus"] = int(up["vision_bonus"] or 0)
                entry["max_hp_bonus"] = int(up["max_hp_bonus"] or 0)
                entry["attack_bonus"] = int(up["attack_bonus"] or 0)
                entry["defense_bonus"] = int(up["defense_bonus"] or 0)
            run = runs_rows.get(discord_id)
            if run:
                entry["active_run"] = summarize_run(run, use_ascii=use_ascii)
            results.append(entry)

        for discord_id, run in runs_rows.items():
            if discord_id in seen:
                continue
            results.append({
                "discord_id": discord_id,
                "runs_started": 0,
                "runs_completed": 0,
                "deaths": 0,
                "total_steps": 0,
                "current_level": 1,
                "highest_level": 1,
                "active_run": summarize_run(run, use_ascii=use_ascii),
            })

        return results
    finally:
        con.close()


def merge_records(dungeon_records: List[Dict[str, Any]], tipbot_data: Dict[int, Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for rec in dungeon_records:
        discord_id = int(rec["discord_id"])
        out = dict(rec)
        out.update(tipbot_data.get(discord_id, {}))
        if "nns_internal_sat" in out:
            out["nns_internal_nns"] = format_sat_to_nns(out["nns_internal_sat"])
        merged.append(out)
    return merged


def print_table(records: List[Dict[str, Any]]) -> None:
    headers = [
        "discord_id",
        "highest",
        "current",
        "started",
        "completed",
        "deaths",
        "vision",
        "xp",
        "nns",
        "run",
    ]
    rows = []
    for rec in records:
        run = rec.get("active_run")
        run_txt = "-"
        if isinstance(run, dict):
            run_txt = (
                f"L{run.get('run_level', '?')} @ {tuple(run.get('pos', ['?', '?']))} "
                f"stars={run.get('star_count', 0)} diamonds={run.get('diamond_count', 0)} "
                f"bankedXP={run.get('pending_star_xp', 0)} bankedNNS={run.get('pending_diamond_nns', '0')}"
            )
        rows.append([
            str(rec.get("discord_id", "")),
            str(rec.get("highest_level", 1)),
            str(rec.get("current_level", 1)),
            str(rec.get("runs_started", 0)),
            str(rec.get("runs_completed", 0)),
            str(rec.get("deaths", 0)),
            str(rec.get("vision_bonus", 0)),
            str(rec.get("xp_total", 0)),
            str(rec.get("nns_internal_nns", "0.00000000")),
            run_txt,
        ])

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = min(max(widths[i], len(cell)), 80)

    def clip(s: str, w: int) -> str:
        return s if len(s) <= w else s[: w - 1] + "…"

    print("  ".join(clip(h, widths[i]).ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(clip(row[i], widths[i]).ljust(widths[i]) for i in range(len(headers))))

    for rec in records:
        run = rec.get("active_run")
        if not isinstance(run, dict):
            continue
        rendered_map = str(run.get("rendered_map") or "").strip("\n")
        if not rendered_map:
            continue
        print()
        print(f"discord_id={rec.get('discord_id')} active_run_map ({run.get('map_mode', 'emoji')}):")
        print(rendered_map)


def main() -> None:
    parser = argparse.ArgumentParser(description="Dump dungeon progress and active runs.")
    parser.add_argument("--dungeon-db", default="dungeon.db", help="Path to dungeon.db")
    parser.add_argument("--tipbot-db", default="tipbot.db", help="Path to tipbot.db")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a text table")
    parser.add_argument("--only-active", action="store_true", help="Show only players with an active run")
    parser.add_argument("--ascii", action="store_true", help="Render active maps in ASCII instead of emoji")
    args = parser.parse_args()

    dungeon_path = Path(args.dungeon_db)
    tipbot_path = Path(args.tipbot_db)

    if not dungeon_path.exists():
        raise SystemExit(f"dungeon db not found: {dungeon_path}")

    dungeon_records = load_dungeon_data(dungeon_path, use_ascii=args.ascii)
    merged = merge_records(dungeon_records, load_tipbot_data(tipbot_path if tipbot_path.exists() else None))

    if args.only_active:
        merged = [r for r in merged if r.get("active_run")]

    if args.json:
        print(json.dumps(merged, indent=2, ensure_ascii=False))
    else:
        print_table(merged)


if __name__ == "__main__":
    main()
import os
import time
import sqlite3
import random
import secrets
from decimal import Decimal, ROUND_DOWN
from typing import Dict, Any, Optional, List, Tuple

from dotenv import load_dotenv
from nns_tipbot import NNS_LEVEL_XP_THRESHOLDS
import discord
from discord import app_commands, ui

load_dotenv()

# ---------------------------
# Config
# ---------------------------
DISCORD_TOKEN = os.environ.get("NNS_TILE_GAME_DISCORD_TOKEN", "").strip()
DB_PATH = os.environ.get("TIPBOT_DB", "tipbot.db").strip()

# Optional: restrict command sync to one guild for faster testing
GUILD_ID = os.environ.get("NNS_TILE_GAME_GUILD_ID", "").strip()
ALLOWED_CHANNEL_ID = os.environ.get("NNS_TILE_GAME_ALLOWED_CHANNEL_ID", "").strip()

NNS_SATS = 100_000_000

# Board size: Discord max is 25 buttons = 5 rows x 5 buttons
NNS_TILE_GAME_TILES = int(os.environ.get("NNS_TILE_GAME_TILES", "25"))
NNS_TILE_GAME_TILES = max(4, min(25, NNS_TILE_GAME_TILES))


NNS_TILE_GAME_TIMEOUT_SECONDS = int(os.environ.get("NNS_TILE_GAME_TIMEOUT_SECONDS", "300"))
NNS_TILE_GAME_COOLDOWN_SECONDS = int(os.environ.get("NNS_TILE_GAME_COOLDOWN_SECONDS", str(4 * 60 * 60)))
NNS_TILE_SHOW_ESTIMATED_RETURN = os.environ.get("NNS_TILE_SHOW_ESTIMATED_RETURN", "0").strip().lower() in ("1", "true", "yes", "on")
NNS_TILE_SHOW_BALANCE = os.environ.get("NNS_TILE_SHOW_BALANCE", "0").strip().lower() in ("1", "true", "yes", "on")

# Zonk: lose a random configured NNS amount. If min/max are not set, fall back to NNS_TILE_ZONK_LOSS.
def env_nns_to_sat(name: str, default_value: str) -> int:
    return int(
        Decimal(os.environ.get(name, default_value)).quantize(
            Decimal("0.00000001"),
            rounding=ROUND_DOWN,
        ) * Decimal(NNS_SATS)
    )


NNS_TILE_ZONK_LOSS_DEFAULT_SAT = env_nns_to_sat("NNS_TILE_ZONK_LOSS", "20.00000000")
NNS_TILE_ZONK_LOSS_MIN_SAT = env_nns_to_sat(
    "NNS_TILE_ZONK_LOSS_MIN",
    format(Decimal(NNS_TILE_ZONK_LOSS_DEFAULT_SAT) / Decimal(NNS_SATS), "f"),
)
NNS_TILE_ZONK_LOSS_MAX_SAT = env_nns_to_sat(
    "NNS_TILE_ZONK_LOSS_MAX",
    format(Decimal(NNS_TILE_ZONK_LOSS_DEFAULT_SAT) / Decimal(NNS_SATS), "f"),
)
if NNS_TILE_ZONK_LOSS_MIN_SAT > NNS_TILE_ZONK_LOSS_MAX_SAT:
    NNS_TILE_ZONK_LOSS_MIN_SAT, NNS_TILE_ZONK_LOSS_MAX_SAT = NNS_TILE_ZONK_LOSS_MAX_SAT, NNS_TILE_ZONK_LOSS_MIN_SAT
# Backward-compatible representative value for text/estimates.
NNS_TILE_ZONK_LOSS_SAT = NNS_TILE_ZONK_LOSS_MAX_SAT

# Gold bag: + random 1-10% of current NNS balance, capped at 200 NNS
NNS_TILE_GOLD_MIN_PERCENT = int(os.environ.get("NNS_TILE_GOLD_MIN_PERCENT", "1"))
NNS_TILE_GOLD_MAX_PERCENT = int(os.environ.get("NNS_TILE_GOLD_MAX_PERCENT", "10"))
NNS_TILE_GOLD_MAX_REWARD_SAT = int(
    Decimal(os.environ.get("NNS_TILE_GOLD_MAX_REWARD", "200.00000000")).quantize(
        Decimal("0.00000001"),
        rounding=ROUND_DOWN,
    ) * Decimal(NNS_SATS)
)

# XP boost: random 1-10 XP
NNS_TILE_XP_MIN = int(os.environ.get("NNS_TILE_XP_MIN", "1"))
NNS_TILE_XP_MAX = int(os.environ.get("NNS_TILE_XP_MAX", "10"))

# Tile distribution weights
# Empty should be common; skull should be rare.

NNS_TILE_WEIGHT_EMPTY = int(os.environ.get("NNS_TILE_WEIGHT_EMPTY", "40"))
NNS_TILE_WEIGHT_ZONK = int(os.environ.get("NNS_TILE_WEIGHT_ZONK", "15"))
NNS_TILE_WEIGHT_GOLD = int(os.environ.get("NNS_TILE_WEIGHT_GOLD", "8"))
NNS_TILE_WEIGHT_XP = int(os.environ.get("NNS_TILE_WEIGHT_XP", "17"))
NNS_TILE_WEIGHT_SKULL = int(os.environ.get("NNS_TILE_WEIGHT_SKULL", "20"))

# Helper to compute tile probability as percent string
def tile_weight_total() -> int:
    return (
        max(0, NNS_TILE_WEIGHT_EMPTY) +
        max(0, NNS_TILE_WEIGHT_ZONK) +
        max(0, NNS_TILE_WEIGHT_GOLD) +
        max(0, NNS_TILE_WEIGHT_XP) +
        max(0, NNS_TILE_WEIGHT_SKULL)
    )


def tile_probability_percent(weight: int) -> str:
    total = tile_weight_total()
    if total <= 0:
        return "0.0"
    return f"{(Decimal(max(0, int(weight))) * Decimal(100) / Decimal(total)):.1f}"


def random_zonk_loss_sat() -> int:
    min_sat = max(0, int(NNS_TILE_ZONK_LOSS_MIN_SAT))
    max_sat = max(0, int(NNS_TILE_ZONK_LOSS_MAX_SAT))
    if max_sat < min_sat:
        min_sat, max_sat = max_sat, min_sat
    if min_sat == max_sat:
        return min_sat
    return random.randint(min_sat, max_sat)


def expected_zonk_loss_sat_for_balance(balance_sat: int) -> int:
    balance_sat = max(0, int(balance_sat or 0))
    min_sat = max(0, int(NNS_TILE_ZONK_LOSS_MIN_SAT))
    max_sat = max(0, int(NNS_TILE_ZONK_LOSS_MAX_SAT))
    if max_sat < min_sat:
        min_sat, max_sat = max_sat, min_sat
    if max_sat <= 0:
        return 0

    # Approximation using the average configured Zonk loss, capped by the current unstaked balance.
    avg_configured_loss = (min_sat + max_sat) // 2
    return min(balance_sat, avg_configured_loss)


def format_zonk_loss_range() -> str:
    min_txt = format_sat_to_nns(NNS_TILE_ZONK_LOSS_MIN_SAT)
    max_txt = format_sat_to_nns(NNS_TILE_ZONK_LOSS_MAX_SAT)
    if int(NNS_TILE_ZONK_LOSS_MIN_SAT) == int(NNS_TILE_ZONK_LOSS_MAX_SAT):
        return min_txt
    return f"{min_txt}–{max_txt}"


def expected_gold_reward_for_balance_sat(balance_sat: int) -> int:
    balance_sat = max(0, int(balance_sat or 0))
    pct_min = min(NNS_TILE_GOLD_MIN_PERCENT, NNS_TILE_GOLD_MAX_PERCENT)
    pct_max = max(NNS_TILE_GOLD_MIN_PERCENT, NNS_TILE_GOLD_MAX_PERCENT)
    pct_values = list(range(pct_min, pct_max + 1))
    if not pct_values:
        return 0

    rewards = []
    for pct in pct_values:
        reward_sat = (balance_sat * int(pct)) // 100
        reward_sat = min(int(reward_sat), int(NNS_TILE_GOLD_MAX_REWARD_SAT))
        rewards.append(max(0, int(reward_sat)))

    return int(sum(rewards) // len(rewards))


def expected_reveals_before_game_end() -> Decimal:
    """Expected number of clicked tiles before skull or full board.

    This is an approximation based on the configured tile weights.
    It intentionally ignores the small board-fix in build_tile_deck() that guarantees
    at least one gold, one XP, and one skull tile.
    """
    total = tile_weight_total()
    if total <= 0 or NNS_TILE_WEIGHT_SKULL <= 0:
        return Decimal(NNS_TILE_GAME_TILES)

    p_continue = Decimal(total - max(0, NNS_TILE_WEIGHT_SKULL)) / Decimal(total)
    expected = Decimal(0)
    survival = Decimal(1)
    for _ in range(int(NNS_TILE_GAME_TILES)):
        expected += survival
        survival *= p_continue
    return expected


def expected_value_for_balance_sat(balance_sat: int) -> int:
    """Approximate expected NNS-satoshi value of one full tile game.

    The player is assumed to click until skull or until all tiles are revealed.
    The estimate uses the starting balance for gold rewards and zonk affordability.
    """
    total = tile_weight_total()
    if total <= 0:
        return 0

    expected_clicks = expected_reveals_before_game_end()

    p_gold = Decimal(max(0, NNS_TILE_WEIGHT_GOLD)) / Decimal(total)
    p_zonk = Decimal(max(0, NNS_TILE_WEIGHT_ZONK)) / Decimal(total)

    avg_gold_sat = Decimal(expected_gold_reward_for_balance_sat(balance_sat))
    zonk_loss_sat = Decimal(expected_zonk_loss_sat_for_balance(int(balance_sat or 0)))

    ev_sat = (expected_clicks * p_gold * avg_gold_sat) - (expected_clicks * p_zonk * zonk_loss_sat)
    return int(ev_sat.to_integral_value(rounding=ROUND_DOWN))


def format_ev_sat(ev_sat: int) -> str:
    sign = "+" if int(ev_sat) >= 0 else "-"
    return f"{sign}{format_sat_to_nns(abs(int(ev_sat)))}"


def hidden_tile_counts(tiles: List[str], revealed: set) -> Tuple[int, int, int, int, int]:
    remaining = [tile for idx, tile in enumerate(tiles) if idx not in revealed]
    return (
        remaining.count("empty"),
        remaining.count("zonk"),
        remaining.count("gold"),
        remaining.count("xp"),
        remaining.count("skull"),
    )


def expected_remaining_return_from_counts(balance_sat: int, counts: Tuple[int, int, int, int, int], memo: Optional[Dict[Tuple[int, Tuple[int, int, int, int, int]], Decimal]] = None) -> Decimal:
    """Fast approximate expected remaining monetary return for the current hidden board.

    The previous exact recursive version branched over remaining tiles and over every
    possible Gold Bag percent roll. With 25 tiles this can become expensive enough to
    block the Discord event loop and trigger heartbeat warnings.

    This version is intentionally lightweight:
    - it uses the real remaining tile composition,
    - estimates how many Gold/Zonk tiles are likely to appear before the first Skull,
    - uses the current unstaked balance for the average Gold reward,
    - treats XP and Empty tiles as zero direct NNS value.
    """
    balance_sat = max(0, int(balance_sat or 0))
    empty_count, zonk_count, gold_count, xp_count, skull_count = tuple(max(0, int(x)) for x in counts)
    total_hidden = empty_count + zonk_count + gold_count + xp_count + skull_count
    if total_hidden <= 0:
        return Decimal(0)

    # In a random order, the expected number of a given non-skull tile type seen
    # before the first skull is count / (skull_count + 1). If there is no skull left,
    # all remaining tiles can be revealed.
    if skull_count > 0:
        reveal_factor = Decimal(1) / Decimal(skull_count + 1)
    else:
        reveal_factor = Decimal(1)

    expected_gold_tiles = Decimal(gold_count) * reveal_factor
    expected_zonk_tiles = Decimal(zonk_count) * reveal_factor

    avg_gold_sat = Decimal(expected_gold_reward_for_balance_sat(balance_sat))

    avg_zonk_loss_sat = Decimal(expected_zonk_loss_sat_for_balance(balance_sat))

    return (expected_gold_tiles * avg_gold_sat) - (expected_zonk_tiles * avg_zonk_loss_sat)


def expected_remaining_return_for_game(balance_sat: int, tiles: List[str], revealed: set) -> int:
    counts = hidden_tile_counts(tiles, revealed)
    ev = expected_remaining_return_from_counts(balance_sat, counts)
    return int(ev.to_integral_value(rounding=ROUND_DOWN))

# In-memory only: no new DB table.
ACTIVE_GAMES: Dict[int, str] = {}
ACTIVE_GAME_VIEWS: Dict[int, "TileGameView"] = {}


# ---------------------------
# DB helpers
# ---------------------------
def now_ts() -> int:
    return int(time.time())


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con



def format_sat_to_nns(sat: int) -> str:
    value = Decimal(int(sat)) / Decimal(NNS_SATS)
    return f"{value.quantize(Decimal('0.1'), rounding=ROUND_DOWN)}"


def balance_text(balance_sat: int, prefix: str = "Unstaked balance") -> str:
    if not NNS_TILE_SHOW_BALANCE:
        return ""
    return f"\n{prefix}: **{format_sat_to_nns(balance_sat)} NNS**"


def format_cooldown(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def get_last_tile_game_ts(con: sqlite3.Connection, discord_id: int) -> int:
    row = con.execute(
        """
        SELECT MAX(ts)
        FROM tx_log
        WHERE type LIKE 'tile_%_nns'
          AND (from_id = ? OR to_id = ?)
        """,
        (int(discord_id), int(discord_id)),
    ).fetchone()
    return int(row[0] or 0) if row else 0



def get_tile_game_cooldown_remaining(con: sqlite3.Connection, discord_id: int) -> int:
    last_ts = get_last_tile_game_ts(con, int(discord_id))
    if last_ts <= 0:
        return 0
    elapsed = now_ts() - int(last_ts)
    remaining = int(NNS_TILE_GAME_COOLDOWN_SECONDS) - int(elapsed)
    return max(0, int(remaining))


def log_tile_game_manual_end(discord_id: int, game_id: str) -> None:
    """Log a manual Tile Game end so the normal cooldown still applies."""
    ts = now_ts()
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE;")
        get_or_create_user(con, int(discord_id))
        con.execute(
            """
            INSERT INTO tx_log(ts, type, from_id, to_id, amount, note, status)
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                ts,
                "tile_end_nns",
                int(discord_id),
                int(discord_id),
                0,
                f"tile game {game_id}: manually ended by player",
                "ok",
            ),
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


# ---------------------------
# Channel restriction helpers
# ---------------------------
def is_allowed_channel(interaction: discord.Interaction) -> bool:
    if not ALLOWED_CHANNEL_ID:
        return True
    try:
        return int(interaction.channel_id or 0) == int(ALLOWED_CHANNEL_ID)
    except Exception:
        return False


async def reject_wrong_channel(interaction: discord.Interaction) -> bool:
    """Return True if the interaction was rejected because it is in the wrong channel."""
    if is_allowed_channel(interaction):
        return False

    channel_text = f"<#{int(ALLOWED_CHANNEL_ID)}>" if ALLOWED_CHANNEL_ID.isdigit() else "the configured Tile Game channel"
    await interaction.response.send_message(
        f"The NNS Tile Game can only be used in {channel_text}.",
        ephemeral=True,
    )
    return True


def get_or_create_user(con: sqlite3.Connection, discord_id: int) -> Dict[str, Any]:
    row = con.execute(
        "SELECT discord_id, nns_internal_sat, created_at, updated_at "
        "FROM users WHERE discord_id=?",
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


def compute_nns_level_from_xp(xp_total: int) -> int:
    xp_total = max(0, int(xp_total or 0))
    level = 1
    for idx, threshold in enumerate(NNS_LEVEL_XP_THRESHOLDS, start=1):
        if xp_total >= int(threshold):
            level = idx
        else:
            break
    return min(16, max(1, level))


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


def grant_nns_xp(con: sqlite3.Connection, discord_id: int, source: str, xp_amount: int, note: str = "") -> Dict[str, Any]:
    xp_amount = max(0, int(xp_amount or 0))
    profile = get_or_create_nns_profile(con, int(discord_id))

    old_xp = int(profile["xp_total"])
    old_level = int(profile["level"])

    if xp_amount <= 0:
        return {
            "xp_added": 0,
            "xp_total": old_xp,
            "old_level": old_level,
            "new_level": old_level,
            "leveled_up": False,
        }

    ts = now_ts()
    new_xp = old_xp + xp_amount
    new_level = compute_nns_level_from_xp(new_xp)

    con.execute(
        "UPDATE nns_profiles SET xp_total=?, level=?, updated_at=? WHERE discord_id=?",
        (int(new_xp), int(new_level), ts, int(discord_id)),
    )
    con.execute(
        "INSERT INTO nns_xp_log(ts, discord_id, source, xp_amount, note) VALUES(?,?,?,?,?)",
        (ts, int(discord_id), str(source), int(xp_amount), note),
    )

    return {
        "xp_added": int(xp_amount),
        "xp_total": int(new_xp),
        "old_level": int(old_level),
        "new_level": int(new_level),
        "leveled_up": bool(new_level > old_level),
    }


# ---------------------------
# Game logic
# ---------------------------
def build_tile_deck(tile_count: int) -> List[str]:
    population = (
        ["empty"] * max(0, NNS_TILE_WEIGHT_EMPTY) +
        ["zonk"] * max(0, NNS_TILE_WEIGHT_ZONK) +
        ["gold"] * max(0, NNS_TILE_WEIGHT_GOLD) +
        ["xp"] * max(0, NNS_TILE_WEIGHT_XP) +
        ["skull"] * max(0, NNS_TILE_WEIGHT_SKULL)
    )

    if not population:
        population = ["empty", "zonk", "gold", "xp", "skull"]

    tiles = [random.choice(population) for _ in range(tile_count)]

    # Make sure the board is not completely boring.
    if "gold" not in tiles:
        tiles[random.randrange(tile_count)] = "gold"
    if "xp" not in tiles:
        tiles[random.randrange(tile_count)] = "xp"
    if "skull" not in tiles:
        tiles[random.randrange(tile_count)] = "skull"

    return tiles


def perform_tile_reveal(discord_id: int, game_id: str, tile_kind: str) -> Dict[str, Any]:
    ts = now_ts()
    con = db()

    try:
        con.execute("BEGIN IMMEDIATE;")
        user = get_or_create_user(con, int(discord_id))
        balance_sat = int(user.get("nns_internal_sat") or 0)

        amount_sat = 0
        xp_added = 0
        note = ""
        result_text = ""
        level_text = ""
        game_over = False

        if tile_kind == "empty":
            note = f"tile game {game_id}: empty"
            result_text = "Empty tile. Nothing happened."

            con.execute(
                "INSERT INTO tx_log(ts, type, from_id, to_id, amount, note, status) VALUES(?,?,?,?,?,?,?)",
                (ts, "tile_empty_nns", int(discord_id), int(discord_id), 0, note, "ok"),
            )

        elif tile_kind == "zonk":
            configured_loss_sat = int(random_zonk_loss_sat())
            loss_sat = min(balance_sat, configured_loss_sat) if configured_loss_sat > 0 else 0

            if loss_sat > 0:
                amount_sat = -loss_sat
                con.execute(
                    "UPDATE users SET nns_internal_sat = nns_internal_sat - ?, updated_at=? WHERE discord_id=?",
                    (loss_sat, ts, int(discord_id)),
                )
            else:
                amount_sat = 0

            if balance_sat <= configured_loss_sat:
                game_over = True
                result_text = (
                    f"Zonk! Your remaining unstaked balance (**{format_sat_to_nns(loss_sat)} NNS**) "
                    "was deducted. Game over."
                )
                note = f"tile game {game_id}: zonk final balance loss game over"
            else:
                result_text = f"Zonk! You lost **{format_sat_to_nns(loss_sat)} NNS**."
                note = f"tile game {game_id}: zonk loss"

            con.execute(
                "INSERT INTO tx_log(ts, type, from_id, to_id, amount, note, status) VALUES(?,?,?,?,?,?,?)",
                (ts, "tile_zonk_nns", int(discord_id), None, int(abs(amount_sat)), note, "ok"),
            )

        elif tile_kind == "gold":
            pct_min = min(NNS_TILE_GOLD_MIN_PERCENT, NNS_TILE_GOLD_MAX_PERCENT)
            pct_max = max(NNS_TILE_GOLD_MIN_PERCENT, NNS_TILE_GOLD_MAX_PERCENT)
            pct = random.randint(pct_min, pct_max)

            reward_sat = (balance_sat * pct) // 100
            reward_sat = min(int(reward_sat), int(NNS_TILE_GOLD_MAX_REWARD_SAT))
            reward_sat = max(0, int(reward_sat))

            amount_sat = reward_sat

            if reward_sat > 0:
                con.execute(
                    "UPDATE users SET nns_internal_sat = nns_internal_sat + ?, updated_at=? WHERE discord_id=?",
                    (reward_sat, ts, int(discord_id)),
                )

            result_text = (
                f"Gold bag! You found **{pct}%** of your unstaked balance: "
                f"**{format_sat_to_nns(reward_sat)} NNS**."
            )
            note = f"tile game {game_id}: gold pct={pct}"

            con.execute(
                "INSERT INTO tx_log(ts, type, from_id, to_id, amount, note, status) VALUES(?,?,?,?,?,?,?)",
                (ts, "tile_gold_nns", None, int(discord_id), int(reward_sat), note, "ok"),
            )

        elif tile_kind == "xp":
            xp_min = min(NNS_TILE_XP_MIN, NNS_TILE_XP_MAX)
            xp_max = max(NNS_TILE_XP_MIN, NNS_TILE_XP_MAX)
            xp_added = random.randint(xp_min, xp_max)

            xp_info = grant_nns_xp(
                con,
                int(discord_id),
                "tile_xp_nns",
                int(xp_added),
                f"tile game {game_id}",
            )

            result_text = f"XP boost! You gained **{xp_added} XP**."
            if xp_info.get("leveled_up"):
                level_text = f"\n🚀 Level up! You reached **Level {int(xp_info['new_level'])}**."

            con.execute(
                "INSERT INTO tx_log(ts, type, from_id, to_id, amount, note, status) VALUES(?,?,?,?,?,?,?)",
                (ts, "tile_xp_nns", None, int(discord_id), 0, f"tile game {game_id}: xp={xp_added}", "ok"),
            )

        elif tile_kind == "skull":
            result_text = "Game over!"
            con.execute(
                "INSERT INTO tx_log(ts, type, from_id, to_id, amount, note, status) VALUES(?,?,?,?,?,?,?)",
                (ts, "tile_skull_nns", int(discord_id), None, 0, f"tile game {game_id}: skull game over", "ok"),
            )

        else:
            raise ValueError("Unknown tile kind.")

        row = con.execute(
            "SELECT nns_internal_sat FROM users WHERE discord_id=?",
            (int(discord_id),),
        ).fetchone()
        new_balance_sat = int(row[0] or 0) if row else 0

        con.execute("COMMIT;")

        return {
            "tile_kind": tile_kind,
            "amount_sat": int(amount_sat),
            "xp_added": int(xp_added),
            "result_text": result_text + level_text,
            "new_balance_sat": int(new_balance_sat),
            "game_over": bool(game_over),
        }

    except Exception:
        try:
            con.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        con.close()


def tile_emoji(tile_kind: str) -> str:
    if tile_kind == "empty":
        return "⬜"
    if tile_kind == "zonk":
        return "💩"
    if tile_kind == "gold":
        return "💰"
    if tile_kind == "xp":
        return "⭐"
    if tile_kind == "skull":
        return "☠️"
    return "❔"


def tile_label(tile_kind: str) -> str:
    if tile_kind == "empty":
        return "Empty"
    if tile_kind == "zonk":
        return "Zonk"
    if tile_kind == "gold":
        return "Gold"
    if tile_kind == "xp":
        return "XP"
    if tile_kind == "skull":
        return "Skull"
    return "Tile"


def make_game_embed(owner: discord.abc.User, revealed_count: int, total_tiles: int, status: str) -> discord.Embed:
    embed = discord.Embed(
        title="🧩 NNS Tile Game",
        description=(
            f"Player: {owner.mention}\n"
            f"Tiles revealed: **{revealed_count}/{total_tiles}**\n\n"
            f"{status}"
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Only the player who started the game can click the tiles.")
    return embed


class TileButton(ui.Button):
    def __init__(self, index: int):
        super().__init__(
            label="",
            emoji="❔",
            style=discord.ButtonStyle.secondary,
            custom_id=f"nns_tile_pending:{index}:{secrets.token_hex(4)}",
            row=index // 5,
        )
        self.index = int(index)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, TileGameView):
            await interaction.response.send_message("This game is no longer available.", ephemeral=True)
            return

        await view.handle_tile_click(interaction, self.index)


class TileGameView(ui.View):
    def __init__(self, owner_id: int, owner_name: str, tile_count: int):
        super().__init__(timeout=NNS_TILE_GAME_TIMEOUT_SECONDS)
        self.owner_id = int(owner_id)
        self.owner_name = str(owner_name)
        self.game_id = secrets.token_hex(6)
        self.tiles = build_tile_deck(tile_count)
        self.revealed = set()
        self.finished = False
        self.last_status = "Click a hidden tile to reveal it."

        ACTIVE_GAMES[self.owner_id] = self.game_id
        ACTIVE_GAME_VIEWS[self.owner_id] = self

        for idx in range(tile_count):
            self.add_item(TileButton(idx))

    def estimated_remaining_return_text(self, balance_sat: int) -> str:
        if self.finished or not NNS_TILE_SHOW_ESTIMATED_RETURN:
            return ""
        ev_sat = expected_remaining_return_for_game(
            balance_sat=int(balance_sat),
            tiles=self.tiles,
            revealed=self.revealed,
        )
        return f"\nEstimated remaining return based on unstaked balance: **{format_ev_sat(ev_sat)} NNS**"

    def make_first_click_safe(self, index: int) -> None:
        """Ensure the first revealed tile is never Skull.

        Zonk is allowed on the first click. If the selected first tile is Skull,
        swap it with another hidden non-Skull tile. This preserves the board
        composition while preventing instant game over on the first click.
        """
        if self.revealed:
            return
        if index < 0 or index >= len(self.tiles):
            return
        if self.tiles[index] != "skull":
            return

        safe_indices = [
            i for i, tile in enumerate(self.tiles)
            if i != index and tile != "skull"
        ]
        if not safe_indices:
            # Extremely defensive fallback; normally build_tile_deck() guarantees
            # at least one Gold and one XP tile.
            self.tiles[index] = "empty"
            return

        swap_index = random.choice(safe_indices)
        self.tiles[index], self.tiles[swap_index] = self.tiles[swap_index], self.tiles[index]

    def balance_line_text(self, balance_sat: int) -> str:
        return balance_text(balance_sat, "Unstaked balance")

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, ui.Button):
                item.disabled = True

    def reveal_button(self, index: int, tile_kind: str) -> None:
        for item in self.children:
            if isinstance(item, TileButton) and item.index == int(index):
                # Keep all tile buttons compact and visually same-sized: emoji only, no text.
                item.label = ""
                item.emoji = tile_emoji(tile_kind)
                item.disabled = True

                if tile_kind == "gold":
                    item.style = discord.ButtonStyle.success
                elif tile_kind in ("zonk", "skull"):
                    item.style = discord.ButtonStyle.danger
                elif tile_kind == "xp":
                    item.style = discord.ButtonStyle.primary
                else:
                    item.style = discord.ButtonStyle.secondary
                return

    async def handle_tile_click(self, interaction: discord.Interaction, index: int):
        if int(interaction.user.id) != int(self.owner_id):
            await interaction.response.send_message(
                "Only the player who started this Tile Game can click these tiles.",
                ephemeral=True,
            )
            return

        if self.finished:
            await interaction.response.send_message("This Tile Game is already finished.", ephemeral=True)
            return

        if index in self.revealed:
            await interaction.response.send_message("This tile was already revealed.", ephemeral=True)
            return

        self.make_first_click_safe(index)
        tile_kind = self.tiles[index]
        self.revealed.add(index)

        try:
            result = perform_tile_reveal(
                discord_id=int(interaction.user.id),
                game_id=self.game_id,
                tile_kind=tile_kind,
            )
        except Exception as e:
            await interaction.response.send_message(f"Tile action failed: `{e}`", ephemeral=True)
            return

        self.reveal_button(index, tile_kind)

        result_text = str(result.get("result_text") or "")
        new_balance_sat = int(result.get("new_balance_sat") or 0)
        game_over = bool(result.get("game_over"))

        if tile_kind == "skull" or game_over:
            self.finished = True
            self.disable_all()
            ACTIVE_GAMES.pop(self.owner_id, None)
            ACTIVE_GAME_VIEWS.pop(self.owner_id, None)
            self.last_status = (
                f"{tile_emoji(tile_kind)} **{result_text}**"
                f"{self.balance_line_text(new_balance_sat)}"
            )
        elif len(self.revealed) >= len(self.tiles):
            self.finished = True
            self.disable_all()
            ACTIVE_GAMES.pop(self.owner_id, None)
            ACTIVE_GAME_VIEWS.pop(self.owner_id, None)
            self.last_status = (
                f"{tile_emoji(tile_kind)} {result_text}\n\n"
                f"✅ All tiles revealed. Game finished."
                f"{self.balance_line_text(new_balance_sat)}"
            )
        else:
            self.last_status = (
                f"{tile_emoji(tile_kind)} {result_text}"
                f"{self.balance_line_text(new_balance_sat)}"
                f"{self.estimated_remaining_return_text(new_balance_sat)}"
            )

        embed = make_game_embed(
            owner=interaction.user,
            revealed_count=len(self.revealed),
            total_tiles=len(self.tiles),
            status=self.last_status,
        )

        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        if self.finished:
            return

        self.finished = True
        self.disable_all()
        ACTIVE_GAMES.pop(self.owner_id, None)
        ACTIVE_GAME_VIEWS.pop(self.owner_id, None)


# ---------------------------
# Discord bot
# ---------------------------
intents = discord.Intents.default()


class NNSTileGameBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"[nns_tile_game] Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            print(f"[nns_tile_game] Synced {len(synced)} global commands")


bot = NNSTileGameBot()



@bot.tree.command(name="tile_game", description="Start a simple NNS tile game.")
async def tile_game(interaction: discord.Interaction):
    if await reject_wrong_channel(interaction):
        return
    existing = ACTIVE_GAMES.get(int(interaction.user.id))
    if existing:
        await interaction.response.send_message(
            "You already have an active Tile Game. Finish it first or wait for timeout.",
            ephemeral=True,
        )
        return

    con = db()
    try:
        user = get_or_create_user(con, int(interaction.user.id))
        balance_sat = int(user.get("nns_internal_sat") or 0)
        cooldown_remaining = get_tile_game_cooldown_remaining(con, int(interaction.user.id))
    finally:
        con.close()

    if cooldown_remaining > 0:
        await interaction.response.send_message(
            f"You can play the NNS Tile Game again in **{format_cooldown(cooldown_remaining)}**.",
            ephemeral=True,
        )
        return

    view = TileGameView(
        owner_id=int(interaction.user.id),
        owner_name=str(interaction.user),
        tile_count=int(NNS_TILE_GAME_TILES),
    )

    estimated_return_line = ""
    if NNS_TILE_SHOW_ESTIMATED_RETURN:
        estimated_ev_sat = expected_remaining_return_for_game(
            balance_sat=balance_sat,
            tiles=view.tiles,
            revealed=view.revealed,
        )
        estimated_return_line = f"\nEstimated remaining return based on unstaked balance: **{format_ev_sat(estimated_ev_sat)} NNS**"
    start_balance_line = balance_text(balance_sat, "Unstaked balance used for this game")

    embed = make_game_embed(
        owner=interaction.user,
        revealed_count=0,
        total_tiles=int(NNS_TILE_GAME_TILES),
        status=(
            f"Click a tile. The first click is never Skull. Possible results:\n"
            f"{tile_probability_percent(NNS_TILE_WEIGHT_EMPTY)}% ⬜ Empty\n"
            f"{tile_probability_percent(NNS_TILE_WEIGHT_ZONK)}% 💩 Zonk: You lose **{format_zonk_loss_range()} NNS**; if your unstaked balance is lower, the remaining balance is deducted and the game ends\n"
            f"{tile_probability_percent(NNS_TILE_WEIGHT_GOLD)}% 💰 Gold bag: You win **{NNS_TILE_GOLD_MIN_PERCENT}–{NNS_TILE_GOLD_MAX_PERCENT}%** of your unstaked balance, capped at **{format_sat_to_nns(NNS_TILE_GOLD_MAX_REWARD_SAT)} NNS**\n"
            f"{tile_probability_percent(NNS_TILE_WEIGHT_XP)}% ⭐ XP boost: You win **{NNS_TILE_XP_MIN}–{NNS_TILE_XP_MAX} XP**\n"
            f"{tile_probability_percent(NNS_TILE_WEIGHT_SKULL)}% ☠️ Skull: Game over!"
            f"{start_balance_line}"
            f"{estimated_return_line}"
        ),
    )

    await interaction.response.send_message(embed=embed, view=view)


# New slash command to allow ending a tile game manually (without bypassing cooldown)
@bot.tree.command(name="tile_game_end", description="End your active NNS Tile Game without bypassing cooldown.")
async def tile_game_end(interaction: discord.Interaction):
    if await reject_wrong_channel(interaction):
        return

    discord_id = int(interaction.user.id)
    game_id = ACTIVE_GAMES.get(discord_id)
    view = ACTIVE_GAME_VIEWS.get(discord_id)

    if not game_id and not view:
        await interaction.response.send_message(
            "You do not have an active Tile Game.",
            ephemeral=True,
        )
        return

    if view:
        view.finished = True
        view.last_status = "Game manually ended by the player."
        view.disable_all()

    try:
        log_tile_game_manual_end(
            discord_id=discord_id,
            game_id=str(game_id or "unknown"),
        )
    except Exception as e:
        await interaction.response.send_message(
            f"Could not end Tile Game: `{e}`",
            ephemeral=True,
        )
        return

    ACTIVE_GAMES.pop(discord_id, None)
    ACTIVE_GAME_VIEWS.pop(discord_id, None)

    await interaction.response.send_message(
        "Your active Tile Game was ended. The normal cooldown still applies.",
        ephemeral=True,
    )


@bot.tree.command(name="tile_rules", description="Show NNS Tile Game rules.")
async def tile_rules(interaction: discord.Interaction):
    if await reject_wrong_channel(interaction):
        return
    await interaction.response.send_message(
        "**NNS Tile Game Rules**\n"
        f"• Board size: **{NNS_TILE_GAME_TILES} tiles**\n"
        f"• {tile_probability_percent(NNS_TILE_WEIGHT_EMPTY)}% ⬜ Empty: nothing happens\n"
        f"• {tile_probability_percent(NNS_TILE_WEIGHT_ZONK)}% 💩 Zonk: You lose **{format_zonk_loss_range()} NNS**. If your unstaked balance is lower, the remaining balance is deducted and the game ends\n"
        f"• {tile_probability_percent(NNS_TILE_WEIGHT_GOLD)}% 💰 Gold bag: You win **{NNS_TILE_GOLD_MIN_PERCENT}–{NNS_TILE_GOLD_MAX_PERCENT}%** of your unstaked balance, capped at **{format_sat_to_nns(NNS_TILE_GOLD_MAX_REWARD_SAT)} NNS**\n"
        f"• {tile_probability_percent(NNS_TILE_WEIGHT_XP)}% ⭐ XP boost: You win **{NNS_TILE_XP_MIN}–{NNS_TILE_XP_MAX} XP**\n"
        f"• {tile_probability_percent(NNS_TILE_WEIGHT_SKULL)}% ☠️ Skull: ends the game\n"
        "• Staked NNS are not counted for Gold Bag rewards or estimated return.\n"
        f"• Estimated return display: {'enabled' if NNS_TILE_SHOW_ESTIMATED_RETURN else 'disabled'}.\n"
        f"• Balance display: {'enabled' if NNS_TILE_SHOW_BALANCE else 'disabled'} for privacy.\n"
        f"• Cooldown: one game every **{format_cooldown(NNS_TILE_GAME_COOLDOWN_SECONDS)}**.\n",
        ephemeral=True,
    )


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("NNS_TILE_GAME_DISCORD_TOKEN is not set.")
    bot.run(DISCORD_TOKEN)
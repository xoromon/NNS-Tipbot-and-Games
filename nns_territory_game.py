import os
import time
import sqlite3
import random
import secrets
from decimal import Decimal, ROUND_DOWN
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from nns_tipbot import NNS_LEVEL_CLAIM_MULTIPLIERS

import discord
from discord import app_commands, ui

load_dotenv()

# ---------------------------
# Base config / shared helpers
# ---------------------------
DISCORD_TOKEN = os.environ.get("NNS_TERRITORY_GAME_DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    # Fallback if you want to reuse the same token env as the tile game bot.
    DISCORD_TOKEN = os.environ.get("NNS_TILE_GAME_DISCORD_TOKEN", "").strip()

DB_PATH = os.environ.get("TIPBOT_DB", "tipbot.db").strip()
GUILD_ID = os.environ.get("NNS_TERRITORY_GAME_GUILD_ID", os.environ.get("NNS_TILE_GAME_GUILD_ID", "")).strip()
ALLOWED_CHANNEL_ID = os.environ.get("NNS_TERRITORY_GAME_ALLOWED_CHANNEL_ID", os.environ.get("NNS_TILE_GAME_ALLOWED_CHANNEL_ID", "")).strip()

NNS_SATS = 100_000_000


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


def get_or_create_user(con: sqlite3.Connection, discord_id: int) -> Dict[str, int]:
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

    channel_text = f"<#{int(ALLOWED_CHANNEL_ID)}>" if ALLOWED_CHANNEL_ID.isdigit() else "the configured Territory Game channel"
    await interaction.response.send_message(
        f"The 996 Territory Game can only be used in {channel_text}.",
        ephemeral=True,
    )
    return True


intents = discord.Intents.default()


class NNSTerritoryGameBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"[nns_territory_game] Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            print(f"[nns_territory_game] Synced {len(synced)} global commands")


bot = NNSTerritoryGameBot()

# ---------------------------
# Territory Game
# ---------------------------
TERRITORY_SIZE = int(os.environ.get("NNS_TERRITORY_SIZE", "5"))
TERRITORY_SIZE = max(3, min(5, TERRITORY_SIZE))

NNS_TERRITORY_COINS = int(os.environ.get("NNS_TERRITORY_COINS", "5"))
NNS_TERRITORY_COINS = max(1, min(8, NNS_TERRITORY_COINS))

NNS_TERRITORY_WIN_REWARD_BASE_SAT = int(
    Decimal(os.environ.get("NNS_TERRITORY_WIN_REWARD_BASE", "99.60000000")).quantize(
        Decimal("0.00000001"),
        rounding=ROUND_DOWN,
    ) * Decimal(NNS_SATS)
)

NNS_TERRITORY_COOLDOWN_SECONDS = int(os.environ.get("NNS_TERRITORY_COOLDOWN_SECONDS", "30"))
NNS_TERRITORY_TIMEOUT_SECONDS = int(os.environ.get("NNS_TERRITORY_TIMEOUT_SECONDS", "300"))

ACTIVE_TERRITORY_GAMES: Dict[int, str] = {}
ACTIVE_TERRITORY_GAME_VIEWS: Dict[int, "TerritoryGameView"] = {}


def territory_get_last_game_ts(con: sqlite3.Connection, discord_id: int) -> int:
    row = con.execute(
        """
        SELECT MAX(ts)
        FROM tx_log
        WHERE type LIKE 'territory_%_nns'
          AND (from_id = ? OR to_id = ?)
        """,
        (int(discord_id), int(discord_id)),
    ).fetchone()
    return int(row[0] or 0) if row else 0



def territory_cooldown_remaining(con: sqlite3.Connection, discord_id: int) -> int:
    last_ts = territory_get_last_game_ts(con, int(discord_id))
    if last_ts <= 0:
        return 0
    return max(0, int(NNS_TERRITORY_COOLDOWN_SECONDS) - (now_ts() - last_ts))


def log_territory_game_manual_end(discord_id: int, game_id: str) -> None:
    """Log a manual Territory Game end so the normal cooldown still applies."""
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
                "territory_end_nns",
                int(discord_id),
                int(discord_id),
                0,
                f"territory game {game_id}: manually ended by player",
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


def get_nns_level(con: sqlite3.Connection, discord_id: int) -> int:
    row = con.execute(
        "SELECT level FROM nns_profiles WHERE discord_id=?",
        (int(discord_id),),
    ).fetchone()
    if not row:
        return 1
    return max(1, min(16, int(row[0] or 1)))


def territory_win_reward_sat_for_level(level: int) -> Tuple[int, Decimal]:
    level = max(1, min(16, int(level or 1)))
    multiplier = Decimal(NNS_LEVEL_CLAIM_MULTIPLIERS.get(level, Decimal("1.00")))
    reward_sat = int(
        (Decimal(NNS_TERRITORY_WIN_REWARD_BASE_SAT) * multiplier).to_integral_value(
            rounding=ROUND_DOWN,
        )
    )
    return reward_sat, multiplier


def territory_neighbors(pos: Tuple[int, int]) -> List[Tuple[int, int]]:
    r, c = pos
    out = []
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            if dr == 0 and dc == 0:
                continue
            rr, cc = r + dr, c + dc
            if 0 <= rr < TERRITORY_SIZE and 0 <= cc < TERRITORY_SIZE:
                out.append((rr, cc))
    return out


def territory_score(board: Dict[Tuple[int, int], str], coins: List[Tuple[int, int]]) -> Tuple[int, int, int]:
    player_coins = 0
    ai_coins = 0
    neutral_coins = 0

    for coin in coins:
        p = 0
        a = 0
        for n in territory_neighbors(coin):
            if board.get(n) == "player":
                p += 1
            elif board.get(n) == "ai":
                a += 1

        if p > a:
            player_coins += 1
        elif a > p:
            ai_coins += 1
        else:
            neutral_coins += 1

    return player_coins, ai_coins, neutral_coins


def territory_cell_emoji(board: Dict[Tuple[int, int], str], coins: List[Tuple[int, int]], pos: Tuple[int, int]) -> str:
    if pos in coins:
        return "🪙"
    v = board.get(pos)
    if v == "player":
        return "🔵"
    if v == "ai":
        return "🔴"
    return "⬜"


def territory_free_cells(board: Dict[Tuple[int, int], str], coins: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    out = []
    for r in range(TERRITORY_SIZE):
        for c in range(TERRITORY_SIZE):
            pos = (r, c)
            if pos in coins:
                continue
            if pos in board:
                continue
            out.append(pos)
    return out


def territory_meaningful_free_cells(board: Dict[Tuple[int, int], str], coins: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """Return free cells that can still influence at least one coin.

    A move only matters if it is placed on one of the 8 neighboring cells of a coin.
    Once no such free cell remains, the game can end even if unrelated empty cells exist.
    """
    coin_neighbor_cells = set()
    for coin in coins:
        coin_neighbor_cells.update(territory_neighbors(coin))

    return [pos for pos in territory_free_cells(board, coins) if pos in coin_neighbor_cells]


def territory_ai_choose_move(board: Dict[Tuple[int, int], str], coins: List[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
    free = territory_meaningful_free_cells(board, coins)
    if not free:
        return None

    best_move = None
    best_score = None

    for move in free:
        test_board = dict(board)
        test_board[move] = "ai"

        ai_score = 0

        # Reward moves adjacent to many coins.
        for coin in coins:
            if move in territory_neighbors(coin):
                ai_score += 3

        # Evaluate territory after move.
        p_coins, a_coins, neutral = territory_score(test_board, coins)
        ai_score += (a_coins - p_coins) * 10

        # Slight preference for center-ish fields.
        center = (TERRITORY_SIZE - 1) / 2
        dist = abs(move[0] - center) + abs(move[1] - center)
        ai_score -= int(dist)

        if best_score is None or ai_score > best_score:
            best_score = ai_score
            best_move = move

    return best_move


def territory_make_embed(owner: discord.abc.User, board: Dict[Tuple[int, int], str], coins: List[Tuple[int, int]], status: str) -> discord.Embed:
    player_coins, ai_coins, neutral_coins = territory_score(board, coins)

    rows = []
    for r in range(TERRITORY_SIZE):
        rows.append("".join(territory_cell_emoji(board, coins, (r, c)) for c in range(TERRITORY_SIZE)))

    embed = discord.Embed(
        title="🪙 996 Territory Game",
        description=(
            f"Player: {owner.mention}\n\n"
            f"{chr(10).join(rows)}\n\n"
            f"Coins controlled: 🔵 **{player_coins}** | 🔴 **{ai_coins}** | Neutral **{neutral_coins}**\n\n"
            f"{status}"
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Win the match to earn {format_sat_to_nns(NNS_TERRITORY_WIN_REWARD_BASE_SAT)} NNS × your level multiplier.")
    return embed


class TerritoryButton(ui.Button):
    def __init__(self, index: int, row: int, col: int, emoji: str, disabled: bool):
        super().__init__(
            label="",
            emoji=emoji,
            style=discord.ButtonStyle.secondary,
            custom_id=f"nns_territory:{index}:{secrets.token_hex(4)}",
            row=row,
            disabled=disabled,
        )
        self.r = row
        self.c = col

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, TerritoryGameView):
            await interaction.response.send_message("This game is no longer available.", ephemeral=True)
            return
        await view.handle_player_move(interaction, (self.r, self.c))


class TerritoryGameView(ui.View):
    def __init__(self, owner_id: int, owner_name: str):
        super().__init__(timeout=NNS_TERRITORY_TIMEOUT_SECONDS)
        self.owner_id = int(owner_id)
        self.owner_name = str(owner_name)
        self.game_id = secrets.token_hex(6)
        self.board: Dict[Tuple[int, int], str] = {}
        self.finished = False

        all_cells = [(r, c) for r in range(TERRITORY_SIZE) for c in range(TERRITORY_SIZE)]
        self.coins = random.sample(all_cells, min(NNS_TERRITORY_COINS, len(all_cells)))

        ACTIVE_TERRITORY_GAMES[self.owner_id] = self.game_id
        ACTIVE_TERRITORY_GAME_VIEWS[self.owner_id] = self

        self.ai_started = random.choice([True, False])
        if self.ai_started:
            ai_move = territory_ai_choose_move(self.board, self.coins)
            if ai_move is not None:
                self.board[ai_move] = "ai"

        self.rebuild_buttons()

    def rebuild_buttons(self):
        self.clear_items()

        meaningful_free = set(territory_meaningful_free_cells(self.board, self.coins))

        idx = 0
        for r in range(TERRITORY_SIZE):
            for c in range(TERRITORY_SIZE):
                pos = (r, c)
                emoji = territory_cell_emoji(self.board, self.coins, pos)
                disabled = pos in self.coins or pos in self.board or self.finished or pos not in meaningful_free

                btn = TerritoryButton(
                    index=idx,
                    row=r,
                    col=c,
                    emoji=emoji,
                    disabled=disabled,
                )

                if self.board.get(pos) == "player":
                    btn.style = discord.ButtonStyle.primary
                elif self.board.get(pos) == "ai":
                    btn.style = discord.ButtonStyle.danger
                elif pos in self.coins:
                    btn.style = discord.ButtonStyle.success
                else:
                    btn.style = discord.ButtonStyle.secondary

                self.add_item(btn)
                idx += 1

    def finish_if_done(self) -> bool:
        if not territory_meaningful_free_cells(self.board, self.coins):
            self.finished = True
            ACTIVE_TERRITORY_GAMES.pop(self.owner_id, None)
            ACTIVE_TERRITORY_GAME_VIEWS.pop(self.owner_id, None)
            return True
        return False

    def finish_status_text(self, reward_sat: int, player_coins: int, ai_coins: int, neutral: int, level: int, multiplier: Decimal) -> str:
        if player_coins > ai_coins:
            result = "You win!"
            reward_line = (
                f"Reward: **{format_sat_to_nns(NNS_TERRITORY_WIN_REWARD_BASE_SAT)} NNS** "
                f"× Level {int(level)} multiplier **{multiplier}** = "
                f"**{format_sat_to_nns(reward_sat)} NNS**"
            )
        elif ai_coins > player_coins:
            result = "AI wins!"
            reward_line = "Reward: **0.0 NNS**"
        else:
            result = "Draw!"
            reward_line = "Reward: **0.0 NNS**"

        return (
            f"Game finished. **{result}**\n"
            f"Final score: 🔵 {player_coins} | 🔴 {ai_coins} | Neutral {neutral}\n"
            f"{reward_line}"
        )

    def award_reward(self, discord_id: int) -> Tuple[int, int, int, int, int, Decimal]:
        player_coins, ai_coins, neutral = territory_score(self.board, self.coins)

        con = db()
        try:
            con.execute("BEGIN IMMEDIATE;")
            get_or_create_user(con, int(discord_id))

            level = get_nns_level(con, int(discord_id))
            reward_sat, multiplier = territory_win_reward_sat_for_level(level)
            if player_coins <= ai_coins:
                reward_sat = 0

            if reward_sat > 0:
                con.execute(
                    "UPDATE users SET nns_internal_sat = nns_internal_sat + ?, updated_at=? WHERE discord_id=?",
                    (reward_sat, now_ts(), int(discord_id)),
                )

            con.execute(
                "INSERT INTO tx_log(ts, type, from_id, to_id, amount, note, status) VALUES(?,?,?,?,?,?,?)",
                (
                    now_ts(),
                    "territory_reward_nns",
                    None,
                    int(discord_id),
                    int(reward_sat),
                    f"territory game {self.game_id}: player_coins={player_coins} ai_coins={ai_coins} neutral={neutral} level={level} multiplier={multiplier}",
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

        return reward_sat, player_coins, ai_coins, neutral, level, multiplier

    async def handle_player_move(self, interaction: discord.Interaction, pos: Tuple[int, int]):
        if int(interaction.user.id) != int(self.owner_id):
            await interaction.response.send_message("Only the player who started this game can play.", ephemeral=True)
            return

        if self.finished:
            await interaction.response.send_message("This Territory Game is already finished.", ephemeral=True)
            return

        if pos in self.coins or pos in self.board:
            await interaction.response.send_message("This field is not available.", ephemeral=True)
            return

        if pos not in territory_meaningful_free_cells(self.board, self.coins):
            await interaction.response.send_message("This field is not next to a coin and cannot affect the score.", ephemeral=True)
            return

        self.board[pos] = "player"

        status = "You placed a marker."

        if not self.finish_if_done():
            ai_move = territory_ai_choose_move(self.board, self.coins)
            if ai_move is not None:
                self.board[ai_move] = "ai"
                status += " The AI placed a marker."

        if self.finish_if_done():
            reward_sat, player_coins, ai_coins, neutral, level, multiplier = self.award_reward(int(interaction.user.id))
            status = self.finish_status_text(reward_sat, player_coins, ai_coins, neutral, level, multiplier)

        self.rebuild_buttons()

        embed = territory_make_embed(
            owner=interaction.user,
            board=self.board,
            coins=self.coins,
            status=status,
        )

        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        if self.finished:
            return
        self.finished = True
        ACTIVE_TERRITORY_GAMES.pop(self.owner_id, None)
        ACTIVE_TERRITORY_GAME_VIEWS.pop(self.owner_id, None)
        self.rebuild_buttons()



@bot.tree.command(name="territory_game", description="Start a 996 Territory Game against the AI.")
async def territory_game(interaction: discord.Interaction):
    if await reject_wrong_channel(interaction):
        return

    existing = ACTIVE_TERRITORY_GAMES.get(int(interaction.user.id))
    if existing:
        await interaction.response.send_message(
            "You already have an active Territory Game. Finish it first or wait for timeout.",
            ephemeral=True,
        )
        return

    con = db()
    try:
        get_or_create_user(con, int(interaction.user.id))
        cd = territory_cooldown_remaining(con, int(interaction.user.id))
    finally:
        con.close()

    if cd > 0:
        await interaction.response.send_message(
            f"You can play Territory Game again in **{format_cooldown(cd)}**.",
            ephemeral=True,
        )
        return

    view = TerritoryGameView(
        owner_id=int(interaction.user.id),
        owner_name=str(interaction.user),
    )

    if view.ai_started:
        status = "Random start: **AI begins** and has already placed one marker."
    else:
        status = "Random start: **You begin**. Place your first marker."

    embed = territory_make_embed(
        owner=interaction.user,
        board=view.board,
        coins=view.coins,
        status=status,
    )

    await interaction.response.send_message(embed=embed, view=view)


# Allow users to manually end their active game, and apply normal cooldown.
@bot.tree.command(name="territory_game_end", description="End your active 996 Territory Game without bypassing cooldown.")
async def territory_game_end(interaction: discord.Interaction):
    if await reject_wrong_channel(interaction):
        return

    discord_id = int(interaction.user.id)
    game_id = ACTIVE_TERRITORY_GAMES.get(discord_id)
    view = ACTIVE_TERRITORY_GAME_VIEWS.get(discord_id)

    if not game_id and not view:
        await interaction.response.send_message(
            "You do not have an active Territory Game.",
            ephemeral=True,
        )
        return

    if view:
        view.finished = True
        view.rebuild_buttons()

    try:
        log_territory_game_manual_end(
            discord_id=discord_id,
            game_id=str(game_id or "unknown"),
        )
    except Exception as e:
        await interaction.response.send_message(
            f"Could not end Territory Game: `{e}`",
            ephemeral=True,
        )
        return

    ACTIVE_TERRITORY_GAMES.pop(discord_id, None)
    ACTIVE_TERRITORY_GAME_VIEWS.pop(discord_id, None)

    await interaction.response.send_message(
        "Your active Territory Game was ended. The normal cooldown still applies.",
        ephemeral=True,
    )


@bot.tree.command(name="territory_rules", description="Show 996 Territory Game rules.")
async def territory_rules(interaction: discord.Interaction):
    if await reject_wrong_channel(interaction):
        return

    await interaction.response.send_message(
        "**996 Territory Game Rules**\n"
        f"• Board: **{TERRITORY_SIZE}×{TERRITORY_SIZE}**\n"
        f"• Coins: **{NNS_TERRITORY_COINS}**\n"
        "• Start is random: either you or the AI begins.\n"
        "• You place 🔵 markers, the AI places 🔴 markers.\n"
        "• Coin fields 🪙 cannot be occupied.\n"
        "• Only fields next to at least one coin can be played.\n"
        "• The game ends when no score-relevant fields remain.\n"
        "• At the end, each coin checks its 8 neighboring fields.\n"
        "• More 🔵 neighbors = coin belongs to you.\n"
        "• More 🔴 neighbors = coin belongs to the AI.\n"
        "• Equal neighbors = neutral coin.\n"
        f"• If you win, you earn **{format_sat_to_nns(NNS_TERRITORY_WIN_REWARD_BASE_SAT)} NNS × your level multiplier**.\n"
        "• If the AI wins or the game ends in a draw, there is no NNS reward.\n"
        f"• Cooldown: **{format_cooldown(NNS_TERRITORY_COOLDOWN_SECONDS)}**.",
        ephemeral=True,
    )


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("NNS_TERRITORY_GAME_DISCORD_TOKEN or NNS_TILE_GAME_DISCORD_TOKEN is not set.")
    bot.run(DISCORD_TOKEN)
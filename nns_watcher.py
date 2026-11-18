#!/usr/bin/env python3
import os
import json
import time
import sqlite3
import asyncio
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ---------------------------
# Config
# ---------------------------
DB_PATH = os.environ.get("TIPBOT_DB", "tipbot.db").strip()

NNS_RPC_URL = os.environ.get("NNS_RPC_URL", "http://127.0.0.1:48931/").strip()
NNS_RPC_USER = os.environ.get("NNS_RPC_USER", "").strip()
NNS_RPC_PASSWORD = os.environ.get("NNS_RPC_PASSWORD", "").strip()

NNS_SATS = 100_000_000
NNS_DEPOSIT_CONFS = int(os.environ.get("NNS_DEPOSIT_CONFS", "6"))

# Deposit safety:
# - Skip wallet-internal transactions where the same txid appears as both send and receive.
# - Optional strict label check. Keep disabled by default for compatibility with old/imported addresses.
NNS_DEPOSIT_SKIP_WALLET_INTERNAL_TX = os.environ.get("NNS_DEPOSIT_SKIP_WALLET_INTERNAL_TX", "1").strip() == "1"
NNS_DEPOSIT_REQUIRE_LABEL_MATCH = os.environ.get("NNS_DEPOSIT_REQUIRE_LABEL_MATCH", "0").strip() == "1"

POLL_SECONDS = int(os.environ.get("NNS_POLL_SECONDS", "300"))

DEPOSIT_POLL_SECONDS = int(os.environ.get("NNS_DEPOSIT_POLL_SECONDS", "300"))
# If the watcher has no stored lastblockhash (fresh DB/reset), never call
# listsinceblock without a block hash. Bootstrap from a recent block instead
# so a large staking wallet does not return its complete transaction history.
NNS_DEPOSIT_BOOTSTRAP_LOOKBACK_BLOCKS = int(
    os.environ.get("NNS_DEPOSIT_BOOTSTRAP_LOOKBACK_BLOCKS", "1000")
)
CONFIRMATION_REFRESH_INTERVAL_SECONDS = int(os.environ.get("NNS_CONFIRMATION_REFRESH_INTERVAL_SECONDS", "60"))
ENABLE_EXPLORER_REBROADCAST = os.environ.get("NNS_ENABLE_EXPLORER_REBROADCAST", "1").strip() == "1"

LOG_HEARTBEAT_SECONDS = int(os.environ.get("NNS_LOG_HEARTBEAT_SECONDS", "600"))
WITHDRAW_BATCH = int(os.environ.get("NNS_WITHDRAW_BATCH", os.environ.get("WITHDRAW_BATCH", "1")))

NNS_WITHDRAW_FEE_ADDRESS = os.environ.get("NNS_WITHDRAW_FEE_ADDRESS", "").strip()
# Fee is already deducted by the tipbot before the withdrawal reaches the watcher.
# The watcher therefore sends only the net amount on-chain and keeps the fee implicitly in the hot wallet.

DEPOSIT_REFRESH_BATCH = int(os.environ.get("NNS_DEPOSIT_REFRESH_BATCH", os.environ.get("DEPOSIT_REFRESH_BATCH", "2")))

WITHDRAW_SLEEP_BETWEEN = float(os.environ.get("NNS_WITHDRAW_SLEEP", "30"))

EXPLORER_SENDRAWTX_URL = os.environ.get("EXPLORER_SENDRAWTX_URL", "https://explorer.996coin.com/api/sendrawtransaction").strip()
EXPLORER_TX_API_KEY = os.environ.get("EXPLORER_TX_API_KEY", "AllesWasEntstehtIstWertDasEsZugrundeGeht").strip()

EXPLORER_REBROADCAST_TIMEOUT = int(os.environ.get("EXPLORER_REBROADCAST_TIMEOUT", "20"))
NNS_RPC_TIMEOUT = int(os.environ.get("NNS_RPC_TIMEOUT", "90"))
NNS_RPC_RETRIES = int(os.environ.get("NNS_RPC_RETRIES", "0"))
NNS_RPC_RETRY_DELAY = float(os.environ.get("NNS_RPC_RETRY_DELAY", "1.5"))

NNS_CONFIRMATION_REFRESH_SLEEP = float(os.environ.get("NNS_CONFIRMATION_REFRESH_SLEEP", "2"))
NNS_WITHDRAW_RETRY_BASE_SECONDS = int(os.environ.get("NNS_WITHDRAW_RETRY_BASE_SECONDS", "300"))
NNS_WITHDRAW_RETRY_MAX_SECONDS = int(os.environ.get("NNS_WITHDRAW_RETRY_MAX_SECONDS", "7200"))

NNS_WITHDRAW_MAX_RETRIES = int(os.environ.get("NNS_WITHDRAW_MAX_RETRIES", "8"))
NNS_NODE_BUSY_BASE_SECONDS = int(os.environ.get("NNS_NODE_BUSY_BASE_SECONDS", "600"))
NNS_NODE_BUSY_MAX_SECONDS = int(os.environ.get("NNS_NODE_BUSY_MAX_SECONDS", "3600"))
NNS_WITHDRAW_BUSY_BASE_SECONDS = int(os.environ.get("NNS_WITHDRAW_BUSY_BASE_SECONDS", "1800"))
NNS_WITHDRAW_BUSY_MAX_SECONDS = int(os.environ.get("NNS_WITHDRAW_BUSY_MAX_SECONDS", "7200"))


# ---------------------------
# Helpers
# ---------------------------
def now_ts() -> int:
    return int(time.time())


def should_log_status(seen: int, credited: int, sent: int, failed: int, last_log_ts: int) -> bool:
    if seen or credited or sent or failed:
        return True
    hb = int(LOG_HEARTBEAT_SECONDS)
    if hb <= 0:
        return False
    return (now_ts() - int(last_log_ts)) >= hb


def format_sat_to_nns(sat: int) -> str:
    return f"{(Decimal(int(sat)) / Decimal(NNS_SATS)):.8f}"


def parse_amount_to_sat(amount_any: Any) -> int:
    d = Decimal(str(amount_any)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    sat = int(d * Decimal(NNS_SATS))
    return sat


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con


def init_db() -> None:
    con = db()
    con.execute("""
    CREATE TABLE IF NOT EXISTS nns_watcher_state (
      id INTEGER PRIMARY KEY CHECK(id=1),
      lastblockhash TEXT,
      updated_at INTEGER NOT NULL
    );
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS nns_deposits (
      txid TEXT NOT NULL,
      vout INTEGER NOT NULL,
      discord_id INTEGER NOT NULL,
      address TEXT NOT NULL,
      amount_sat INTEGER NOT NULL,
      confirmations INTEGER NOT NULL DEFAULT 0,
      credited INTEGER NOT NULL DEFAULT 0,
      first_seen_ts INTEGER NOT NULL,
      credited_ts INTEGER,
      last_update_ts INTEGER NOT NULL,
      PRIMARY KEY(txid, vout)
    );
    """)

    # Deposit hardening columns. Existing DBs are migrated in-place.
    try:
        con.execute("ALTER TABLE nns_deposits ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0;")
    except Exception:
        pass
    try:
        con.execute("ALTER TABLE nns_deposits ADD COLUMN ignored_reason TEXT;")
    except Exception:
        pass
    try:
        con.execute("ALTER TABLE nns_deposits ADD COLUMN ignored_ts INTEGER;")
    except Exception:
        pass

    con.execute("""
    CREATE INDEX IF NOT EXISTS idx_nns_deposits_credited_ignored
    ON nns_deposits(credited, ignored, confirmations);
    """)
    con.execute("""
    CREATE INDEX IF NOT EXISTS idx_nns_deposits_address
    ON nns_deposits(address);
    """)
    con.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_users_nns_deposit_address_unique
    ON users(nns_deposit_address)
    WHERE nns_deposit_address IS NOT NULL AND TRIM(nns_deposit_address) != '';
    """)

    try:
        con.execute("ALTER TABLE nns_withdrawals ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;")
    except Exception:
        pass
    try:
        con.execute("ALTER TABLE nns_withdrawals ADD COLUMN next_retry_at INTEGER;")
    except Exception:
        pass
    try:
        con.execute("ALTER TABLE nns_withdrawals ADD COLUMN last_attempt_ts INTEGER;")
    except Exception:
        pass

    row = con.execute("SELECT id FROM nns_watcher_state WHERE id=1").fetchone()
    if not row:
        con.execute(
            "INSERT INTO nns_watcher_state(id, lastblockhash, updated_at) VALUES(1, NULL, ?)",
            (now_ts(),)
        )
    con.close()


def get_lastblockhash(con: sqlite3.Connection) -> Optional[str]:
    row = con.execute("SELECT lastblockhash FROM nns_watcher_state WHERE id=1").fetchone()
    if not row:
        return None
    return row[0] if row[0] else None


def set_lastblockhash(con: sqlite3.Connection, bh: Optional[str]) -> None:
    con.execute(
        "UPDATE nns_watcher_state SET lastblockhash=?, updated_at=? WHERE id=1",
        (bh, now_ts())
    )


_RPC_SESSION: Optional[aiohttp.ClientSession] = None
_RPC_LOCK = asyncio.Lock()


async def get_rpc_session() -> aiohttp.ClientSession:
    global _RPC_SESSION
    if _RPC_SESSION is None or _RPC_SESSION.closed:
        auth = aiohttp.BasicAuth(NNS_RPC_USER, NNS_RPC_PASSWORD)
        timeout = aiohttp.ClientTimeout(total=NNS_RPC_TIMEOUT)
        connector = aiohttp.TCPConnector(limit=8, limit_per_host=8, keepalive_timeout=30)
        _RPC_SESSION = aiohttp.ClientSession(auth=auth, timeout=timeout, connector=connector)
    return _RPC_SESSION


async def close_rpc_session() -> None:
    global _RPC_SESSION
    if _RPC_SESSION is not None and not _RPC_SESSION.closed:
        await _RPC_SESSION.close()
    _RPC_SESSION = None


class RPCConnectError(RuntimeError):
    """Connection establishment failed before an RPC request could be sent."""


class RPCResponseError(RuntimeError):
    """The node returned an explicit JSON-RPC error response."""


class RPCAmbiguousError(RuntimeError):
    """No definitive response; a mutating request may have executed."""


def is_ambiguous_send_error(message: str) -> bool:
    """
    Return True when sendtoaddress may already have been accepted by the node
    even though the watcher did not receive a definitive result. Such failures
    must NEVER be retried or refunded automatically.
    """
    msg = (message or "").lower()
    return (
        "timeout" in msg
        or "timed out" in msg
        or "server disconnected" in msg
        or "cannot connect to host" in msg
        or "connection reset" in msg
        or "connection aborted" in msg
        or "connection closed" in msg
        or "broken pipe" in msg
        or "clientconnectionerror" in msg
        or "clientoserror" in msg
        or "serverdisconnectederror" in msg
        or "http 502" in msg
        or "http 503" in msg
        or "service unavailable" in msg
        or "work queue depth exceeded" in msg
    )


def is_transient_withdraw_error(message: str) -> bool:
    """
    Errors that are safe to retry only after a definitive RPC error response.
    Transport/timeout uncertainty is excluded by is_ambiguous_send_error().
    """
    msg = (message or "").lower()
    if is_ambiguous_send_error(msg):
        return False
    return (
        "temporarily unavailable" in msg
        or "wallet is busy" in msg
        or "wallet busy" in msg
        or "database is busy" in msg
    )


def compute_withdraw_retry_delay(retry_count: int) -> int:
    base = max(30, int(NNS_WITHDRAW_RETRY_BASE_SECONDS))
    max_delay = max(base, int(NNS_WITHDRAW_RETRY_MAX_SECONDS))
    delay = base * (2 ** max(0, int(retry_count)))
    return min(delay, max_delay)


# ---------------------------
# Node busy/backoff helpers
# ---------------------------
_NODE_BUSY_UNTIL_TS = 0
_WITHDRAW_BUSY_UNTIL_TS = 0
_NODE_BUSY_STREAK = 0
_WITHDRAW_BUSY_STREAK = 0
_NODE_RPC_HEALTHY = False
_WITHDRAW_RPC_HEALTHY = False


def is_node_busy_error(message: str) -> bool:
    msg = (message or "").lower()
    return (
        "work queue depth exceeded" in msg
        or "http 503" in msg
        or "service unavailable" in msg
        or "server disconnected" in msg
        or "cannot connect to host" in msg
        or "timeout" in msg
        or "temporar" in msg
        or "busy" in msg
    )


def _compute_busy_delay(base_seconds: int, max_seconds: int, streak: int) -> int:
    base = max(30, int(base_seconds))
    ceiling = max(base, int(max_seconds))
    delay = base * (2 ** max(0, int(streak)))
    return min(delay, ceiling)


def note_node_busy(reason: str, *, withdraw_only: bool = False) -> None:
    global _NODE_BUSY_UNTIL_TS, _WITHDRAW_BUSY_UNTIL_TS, _NODE_BUSY_STREAK, _WITHDRAW_BUSY_STREAK

    global _NODE_RPC_HEALTHY, _WITHDRAW_RPC_HEALTHY
    _WITHDRAW_RPC_HEALTHY = False
    if not withdraw_only:
        _NODE_RPC_HEALTHY = False

    now = now_ts()
    if withdraw_only:
        delay = _compute_busy_delay(NNS_WITHDRAW_BUSY_BASE_SECONDS, NNS_WITHDRAW_BUSY_MAX_SECONDS, _WITHDRAW_BUSY_STREAK)
        _WITHDRAW_BUSY_UNTIL_TS = max(_WITHDRAW_BUSY_UNTIL_TS, now + delay)
        _WITHDRAW_BUSY_STREAK += 1
        print(
            f"[busy] withdraw RPC backoff for {delay}s (until {_WITHDRAW_BUSY_UNTIL_TS}) due to: {reason}",
            flush=True,
        )
        return

    delay = _compute_busy_delay(NNS_NODE_BUSY_BASE_SECONDS, NNS_NODE_BUSY_MAX_SECONDS, _NODE_BUSY_STREAK)
    _NODE_BUSY_UNTIL_TS = max(_NODE_BUSY_UNTIL_TS, now + delay)
    _WITHDRAW_BUSY_UNTIL_TS = max(_WITHDRAW_BUSY_UNTIL_TS, now + delay)
    _NODE_BUSY_STREAK += 1
    _WITHDRAW_BUSY_STREAK += 1
    print(
        f"[busy] global RPC backoff for {delay}s (until {_NODE_BUSY_UNTIL_TS}) due to: {reason}",
        flush=True,
    )


def note_node_recovered() -> None:
    global _NODE_BUSY_STREAK, _WITHDRAW_BUSY_STREAK
    # A quiet loop or successful deposit RPC does not prove withdrawals recovered.
    if _NODE_RPC_HEALTHY and not is_global_backoff_active():
        _NODE_BUSY_STREAK = 0
    if _WITHDRAW_RPC_HEALTHY and not is_withdraw_backoff_active():
        _WITHDRAW_BUSY_STREAK = 0


def is_global_backoff_active(ts_now: Optional[int] = None) -> bool:
    ts_now = int(ts_now or now_ts())
    return ts_now < int(_NODE_BUSY_UNTIL_TS)


def is_withdraw_backoff_active(ts_now: Optional[int] = None) -> bool:
    ts_now = int(ts_now or now_ts())
    return ts_now < int(_WITHDRAW_BUSY_UNTIL_TS)


# Wallet-mutating RPCs are intentionally never retried inside nns_rpc_call.
# If the HTTP response is lost after the node accepted the request, repeating
# sendtoaddress can create a second valid payment.
_MUTATING_RPC_METHODS = {"sendtoaddress", "sendmany", "sendrawtransaction"}


async def nns_rpc_call(method: str, params: Optional[List[Any]] = None) -> Any:
    global _NODE_RPC_HEALTHY, _WITHDRAW_RPC_HEALTHY
    if not NNS_RPC_URL or not NNS_RPC_USER or not NNS_RPC_PASSWORD:
        raise RuntimeError("996-Coin RPC not configured (NNS_RPC_URL/USER/PASSWORD)")

    payload = {
        "jsonrpc": "1.0",
        "id": "nns-watcher",
        "method": method,
        "params": params or [],
    }

    method_lc = str(method or "").lower()
    attempts = 1 if method_lc in _MUTATING_RPC_METHODS else max(1, int(NNS_RPC_RETRIES) + 1)
    last_err: Optional[Exception] = None

    async with _RPC_LOCK:
        for attempt in range(1, attempts + 1):
            try:
                session = await get_rpc_session()
                async with session.post(NNS_RPC_URL, json=payload) as r:
                    txt = await r.text()
                    if r.status != 200:
                        raise RPCAmbiguousError(
                            f"RPC method={method}: HTTP {r.status}: {txt}"
                        )
                    data = json.loads(txt)
                    if data.get("error"):
                        raise RPCResponseError(
                            f"RPC method={method}: {data['error']}"
                        )
                    if "result" not in data:
                        raise RPCAmbiguousError("RPC response missing result")
                    if method_lc == "sendtoaddress" and (
                        not isinstance(data["result"], str) or not data["result"].strip()
                    ):
                        raise RPCAmbiguousError("withdraw broadcast returned invalid txid")
                    _NODE_RPC_HEALTHY = True
                    if method_lc == "sendtoaddress":
                        _WITHDRAW_RPC_HEALTHY = True
                    return data.get("result")

            except aiohttp.ClientConnectorError as e:
                # Typed connector failure only: never infer safe retry from text.
                last_err = e
                if attempt >= attempts:
                    raise RPCConnectError(f"RPC method={method}: connection not established: {e}") from e
                await asyncio.sleep(max(0.0, float(NNS_RPC_RETRY_DELAY)))

            except (asyncio.TimeoutError, TimeoutError) as e:
                last_err = e
                msg = f"RPC method={method}: timeout after {NNS_RPC_TIMEOUT}s"
                if attempt >= attempts:
                    raise RPCAmbiguousError(msg) from e
                await asyncio.sleep(max(0.0, float(NNS_RPC_RETRY_DELAY)))

            except aiohttp.ClientError as e:
                last_err = e
                detail = str(e).strip() or type(e).__name__
                msg = f"RPC method={method}: {detail}"
                if attempt >= attempts:
                    raise RPCAmbiguousError(msg) from e
                await asyncio.sleep(max(0.0, float(NNS_RPC_RETRY_DELAY)))

            except Exception as e:
                last_err = e
                detail = str(e).strip() or type(e).__name__
                msg = f"RPC method={method}: {detail}"
                transient = (
                    "work queue depth exceeded" in msg.lower()
                    or "cannot connect to host" in msg.lower()
                    or "server disconnected" in msg.lower()
                    or "http 503" in msg.lower()
                    or "service unavailable" in msg.lower()
                )
                if attempt >= attempts or not transient:
                    if isinstance(e, RPCResponseError):
                        raise
                    raise RPCAmbiguousError(msg) from e
                await asyncio.sleep(max(0.0, float(NNS_RPC_RETRY_DELAY)))

    detail = str(last_err).strip() if last_err is not None else "unknown RPC error"
    if not detail:
        detail = type(last_err).__name__ if last_err else "unknown RPC error"
    raise RuntimeError(f"RPC method={method}: {detail}")


# ---------------------------
# Explorer rebroadcast helpers
# ---------------------------
async def get_raw_tx_hex(txid: str) -> str:
    """
    Fetch the wallet transaction and return its raw hex for best-effort rebroadcast.
    """
    res = await nns_rpc_call("gettransaction", [str(txid), True])
    if not isinstance(res, dict):
        raise RuntimeError("gettransaction returned invalid result")
    raw_hex = str(res.get("hex") or "").strip()
    if not raw_hex:
        raise RuntimeError("transaction hex not available from gettransaction")
    return raw_hex


async def explorer_rebroadcast_raw_tx(raw_hex: str) -> Dict[str, Any]:
    """
    Best-effort rebroadcast via explorer backend.
    Returns the parsed JSON response. Raises on transport or HTTP errors.
    """
    if not EXPLORER_SENDRAWTX_URL:
        raise RuntimeError("EXPLORER_SENDRAWTX_URL not configured")

    headers = {"Content-Type": "application/json"}
    if EXPLORER_TX_API_KEY:
        headers["X-API-Key"] = EXPLORER_TX_API_KEY

    payload: Dict[str, Any] = {"hex": str(raw_hex).strip()}

    timeout = aiohttp.ClientTimeout(total=EXPLORER_REBROADCAST_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(EXPLORER_SENDRAWTX_URL, json=payload, headers=headers) as r:
            txt = await r.text()
            if r.status >= 400:
                raise RuntimeError(f"explorer rebroadcast HTTP {r.status}: {txt}")
            try:
                data = json.loads(txt)
            except Exception as e:
                raise RuntimeError(f"explorer rebroadcast returned invalid JSON: {e}")
            if not isinstance(data, dict):
                raise RuntimeError("explorer rebroadcast returned invalid response type")
            return data


def load_address_map(con: sqlite3.Connection) -> Dict[str, int]:
    rows = con.execute(
        "SELECT discord_id, nns_deposit_address FROM users WHERE nns_deposit_address IS NOT NULL AND TRIM(nns_deposit_address) != ''"
    ).fetchall()
    m: Dict[str, int] = {}
    duplicate_addresses = set()

    for discord_id, addr in rows:
        a = (addr or "").strip()
        if not a:
            continue
        if a in m and int(m[a]) != int(discord_id):
            duplicate_addresses.add(a)
            print(
                f"[deposit-warning] duplicate deposit address in users table addr={a} "
                f"first_discord_id={m[a]} duplicate_discord_id={int(discord_id)}",
                flush=True,
            )
            continue
        m[a] = int(discord_id)

    if duplicate_addresses:
        print(
            f"[deposit-warning] duplicate deposit addresses ignored count={len(duplicate_addresses)}",
            flush=True,
        )

    return m


def upsert_deposit_seen(
    con: sqlite3.Connection,
    txid: str,
    vout: int,
    discord_id: int,
    address: str,
    amount_sat: int,
    confirmations: int
) -> None:
    ts = now_ts()
    con.execute("""
    INSERT INTO nns_deposits(
      txid, vout, discord_id, address, amount_sat, confirmations,
      credited, first_seen_ts, credited_ts, last_update_ts,
      ignored, ignored_reason, ignored_ts
    )
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(txid, vout) DO UPDATE SET
      confirmations=excluded.confirmations,
      last_update_ts=excluded.last_update_ts
    """, (
        txid,
        int(vout),
        int(discord_id),
        address,
        int(amount_sat),
        int(confirmations),
        0,
        ts,
        None,
        ts,
        0,
        None,
        None,
    ))


def mark_deposit_ignored(
    con: sqlite3.Connection,
    txid: str,
    vout: Optional[int],
    reason: str,
) -> None:
    ts = now_ts()
    reason = str(reason)[:300]
    if vout is None:
        con.execute(
            """
            UPDATE nns_deposits
            SET ignored=1, ignored_reason=?, ignored_ts=?, last_update_ts=?
            WHERE txid=? AND credited=0
            """,
            (reason, ts, ts, str(txid)),
        )
    else:
        con.execute(
            """
            UPDATE nns_deposits
            SET ignored=1, ignored_reason=?, ignored_ts=?, last_update_ts=?
            WHERE txid=? AND vout=? AND credited=0
            """,
            (reason, ts, ts, str(txid), int(vout)),
        )


def try_credit_deposit(con: sqlite3.Connection, txid: str, vout: int) -> bool:
    row = con.execute(
        """
        SELECT discord_id, amount_sat, confirmations, credited, COALESCE(ignored, 0)
        FROM nns_deposits
        WHERE txid=? AND vout=?
        """,
        (txid, int(vout))
    ).fetchone()
    if not row:
        return False

    discord_id = int(row[0])
    amount_sat = int(row[1])
    confirmations = int(row[2])
    credited = int(row[3])
    ignored = int(row[4])

    if credited:
        return False
    if ignored:
        return False
    if confirmations < NNS_DEPOSIT_CONFS:
        return False

    ts = now_ts()
    con.execute(
        "UPDATE users SET nns_internal_sat = nns_internal_sat + ?, updated_at=? WHERE discord_id=?",
        (int(amount_sat), ts, int(discord_id))
    )
    con.execute(
        "UPDATE nns_deposits SET credited=1, credited_ts=?, last_update_ts=? WHERE txid=? AND vout=?",
        (ts, ts, txid, int(vout))
    )
    return True


# In-memory attempt order keeps failing transactions from starving other deposits.
# No schema change; a restart simply starts with the oldest stored observations.
_CONFIRMATION_ATTEMPTS: Dict[str, int] = {}
_CONFIRMATION_REFRESH_FAILED = False


async def refresh_uncredited_confirmations(con: sqlite3.Connection) -> int:
    global _CONFIRMATION_REFRESH_FAILED
    _CONFIRMATION_REFRESH_FAILED = False
    rows = con.execute(
        """
        SELECT txid, MIN(last_update_ts)
        FROM nns_deposits
        WHERE credited=0 AND COALESCE(ignored, 0)=0
        GROUP BY txid
        """
    ).fetchall()
    pending = {str(row[0]) for row in rows}
    for txid in list(_CONFIRMATION_ATTEMPTS):
        if txid not in pending:
            del _CONFIRMATION_ATTEMPTS[txid]
    rows.sort(key=lambda row: (
        _CONFIRMATION_ATTEMPTS.get(str(row[0]), 0), int(row[1]), str(row[0])
    ))

    updated = 0
    for txid, _ in rows[:max(1, int(DEPOSIT_REFRESH_BATCH))]:
        if is_global_backoff_active():
            _CONFIRMATION_REFRESH_FAILED = True
            break
        txid = str(txid)
        _CONFIRMATION_ATTEMPTS[txid] = time.monotonic_ns()
        try:
            gt = await nns_rpc_call("gettransaction", [txid])
            if not isinstance(gt, dict) or "confirmations" not in gt:
                raise ValueError("gettransaction returned no confirmations")
            confs = max(0, int(gt["confirmations"]))
        except Exception as e:
            _CONFIRMATION_REFRESH_FAILED = True
            print(f"[deposit-refresh] txid={txid}: {e}", flush=True)
            if isinstance(e, (RPCConnectError, RPCAmbiguousError)) or is_node_busy_error(str(e)):
                note_node_busy(str(e))
                break  # Do not continue a batch against an unavailable node.
            if NNS_CONFIRMATION_REFRESH_SLEEP > 0:
                await asyncio.sleep(NNS_CONFIRMATION_REFRESH_SLEEP)
            continue

        cur = con.execute(
            """
            UPDATE nns_deposits
            SET confirmations=?, last_update_ts=?
            WHERE txid=? AND credited=0 AND COALESCE(ignored, 0)=0
            """,
            (confs, now_ts(), txid)
        )
        if cur.rowcount:
            updated += int(cur.rowcount)

        if NNS_CONFIRMATION_REFRESH_SLEEP > 0:
            await asyncio.sleep(NNS_CONFIRMATION_REFRESH_SLEEP)

    return updated


async def process_confirmation_refresh() -> int:
    """Refresh known deposits independently of discovery, then credit atomically."""
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE;")
        await refresh_uncredited_confirmations(con)
        rows = con.execute(
            "SELECT txid, vout FROM nns_deposits "
            "WHERE credited=0 AND COALESCE(ignored, 0)=0 AND confirmations >= ?",
            (int(NNS_DEPOSIT_CONFS),)
        ).fetchall()
        credited = sum(try_credit_deposit(con, str(txid), int(vout)) for txid, vout in rows)
        con.execute("COMMIT;")
        return credited
    except Exception:
        con.execute("ROLLBACK;")
        raise
    finally:
        con.close()


async def get_deposit_bootstrap_blockhash() -> str:
    """Return a recent block hash for a bounded listsinceblock bootstrap."""
    tip_height = await nns_rpc_call("getblockcount", [])
    try:
        tip_height = int(tip_height)
    except (TypeError, ValueError):
        raise RuntimeError(f"getblockcount returned invalid result: {tip_height!r}")

    lookback = max(1, int(NNS_DEPOSIT_BOOTSTRAP_LOOKBACK_BLOCKS))
    bootstrap_height = max(0, tip_height - lookback)
    blockhash = await nns_rpc_call("getblockhash", [bootstrap_height])

    if not isinstance(blockhash, str) or not blockhash.strip():
        raise RuntimeError(
            f"getblockhash returned invalid result for height {bootstrap_height}: {blockhash!r}"
        )

    blockhash = blockhash.strip()
    print(
        f"[deposit-bootstrap] no stored lastblockhash; tip={tip_height}, "
        f"lookback={lookback}, starting at height={bootstrap_height} hash={blockhash}",
        flush=True,
    )
    return blockhash


async def process_deposits(do_confirmation_refresh: bool = False) -> Tuple[int, int]:
    con = db()
    try:
        addr_map = load_address_map(con)
        last_bh = get_lastblockhash(con)
    finally:
        con.close()

    if not addr_map:
        return (0, 0)

    try:
        if not last_bh:
            # IMPORTANT: listsinceblock([]) can return the entire wallet history
            # and become enormous on an active staking wallet. Bootstrap from
            # the configured bounded recent window instead.
            last_bh = await get_deposit_bootstrap_blockhash()

            con_bootstrap = db()
            try:
                con_bootstrap.execute("BEGIN IMMEDIATE;")
                set_lastblockhash(con_bootstrap, last_bh)
                con_bootstrap.execute("COMMIT;")
            except Exception:
                try:
                    con_bootstrap.execute("ROLLBACK;")
                except Exception:
                    pass
                raise
            finally:
                con_bootstrap.close()

        res = await nns_rpc_call("listsinceblock", [last_bh])
    except Exception as e:
        msg = str(e)
        # Busy/backoff is recorded once by the outer main loop. Recording it
        # here as well would double the exponential backoff for one RPC failure.
        if last_bh and "block not found" in msg.lower():
            con_reset = db()
            try:
                con_reset.execute("BEGIN IMMEDIATE;")
                set_lastblockhash(con_reset, None)
                con_reset.execute("COMMIT;")
            except Exception:
                try:
                    con_reset.execute("ROLLBACK;")
                except Exception:
                    pass
            finally:
                con_reset.close()
            print(f"[process_deposits] stored lastblockhash not found, resetting watcher state: {last_bh}", flush=True)
            # Do not immediately fire a second fallback RPC against an already unhappy node.
            # The next scheduled loop will retry from the reset state.
            raise
        raise

    txs = res.get("transactions") if isinstance(res, dict) else None
    new_last = res.get("lastblock") if isinstance(res, dict) else None
    if not isinstance(txs, list):
        txs = []

    wallet_send_txids = set()
    if NNS_DEPOSIT_SKIP_WALLET_INTERNAL_TX:
        wallet_send_txids = {
            str(t.get("txid") or "").strip()
            for t in txs
            if isinstance(t, dict)
            and str(t.get("category") or "").lower() == "send"
            and str(t.get("txid") or "").strip()
        }

    seen = 0
    credited = 0

    con2 = db()
    try:
        con2.execute("BEGIN IMMEDIATE;")

        if isinstance(new_last, str) and new_last:
            set_lastblockhash(con2, new_last)

        for t in txs:
            if not isinstance(t, dict):
                continue
            if str(t.get("category") or "").lower() != "receive":
                continue

            addr = str(t.get("address") or "").strip()
            if not addr or addr not in addr_map:
                continue

            txid = str(t.get("txid") or "").strip()
            if not txid:
                continue

            vout = int(t.get("vout") or 0)
            confs = int(t.get("confirmations") or 0)
            amt_sat = parse_amount_to_sat(t.get("amount", 0))

            if amt_sat <= 0:
                continue

            if txid in wallet_send_txids:
                reason = "wallet-internal tx: same txid also has category=send"
                upsert_deposit_seen(con2, txid, vout, addr_map[addr], addr, amt_sat, confs)
                mark_deposit_ignored(con2, txid, vout, reason)
                print(
                    f"[deposit-skip] {reason} txid={txid} vout={vout} addr={addr} amount={format_sat_to_nns(amt_sat)}",
                    flush=True,
                )
                continue

            if NNS_DEPOSIT_REQUIRE_LABEL_MATCH:
                label = str(t.get("label") or "").strip()
                expected_label = f"nns-tipbot:{int(addr_map[addr])}"
                if label != expected_label:
                    reason = f"label mismatch: got={label!r} expected={expected_label!r}"
                    upsert_deposit_seen(con2, txid, vout, addr_map[addr], addr, amt_sat, confs)
                    mark_deposit_ignored(con2, txid, vout, reason)
                    print(
                        f"[deposit-skip] {reason} txid={txid} vout={vout} addr={addr} amount={format_sat_to_nns(amt_sat)}",
                        flush=True,
                    )
                    continue

            upsert_deposit_seen(con2, txid, vout, addr_map[addr], addr, amt_sat, confs)
            seen += 1

        if do_confirmation_refresh:
            await refresh_uncredited_confirmations(con2)

        rows = con2.execute(
            """
            SELECT txid, vout
            FROM nns_deposits
            WHERE credited=0
              AND COALESCE(ignored, 0)=0
              AND confirmations >= ?
            """,
            (int(NNS_DEPOSIT_CONFS),)
        ).fetchall()

        for txid, vout in rows:
            if try_credit_deposit(con2, str(txid), int(vout)):
                credited += 1

        con2.execute("COMMIT;")

    except Exception:
        try:
            con2.execute("ROLLBACK;")
        except Exception:
            pass
        raise
    finally:
        con2.close()

    return (seen, credited)


async def process_withdrawals() -> Tuple[int, int]:
    con = db()
    try:
        rows = con.execute(
            "SELECT id, discord_id, to_address, amount_sat, fee_sat, COALESCE(retry_count, 0), next_retry_at "
            "FROM nns_withdrawals "
            "WHERE status IN ('pending','retry') "
            "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
            "ORDER BY id ASC LIMIT ?",
            (now_ts(), max(1, WITHDRAW_BATCH))
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return (0, 0)

    if is_withdraw_backoff_active():
        return (0, 0)

    sent = 0
    failed = 0

    for wid, discord_id, to_addr, amount_sat, fee_sat, retry_count, next_retry_at in rows:
        wid = int(wid)
        discord_id = int(discord_id)
        to_addr = str(to_addr).strip()
        amount_sat = int(amount_sat)
        fee_sat = int(fee_sat or 0)
        retry_count = int(retry_count or 0)

        txid: Optional[str] = None
        err: Optional[str] = None
        send_error: Optional[Exception] = None
        rebroadcast_note: Optional[str] = None
        try:
            # `amount_sat` already stores the net user amount after fee deduction in the tipbot.
            # Send only this net amount on-chain and keep the fee implicitly in the hot wallet.
            amount_str = format_sat_to_nns(amount_sat)
            result = await nns_rpc_call("sendtoaddress", [to_addr, amount_str])

            if not isinstance(result, str) or not result.strip():
                raise RPCAmbiguousError("withdraw broadcast returned invalid txid")
            txid = result.strip()

            if ENABLE_EXPLORER_REBROADCAST and EXPLORER_SENDRAWTX_URL and not is_global_backoff_active() and not is_withdraw_backoff_active():
                try:
                    raw_hex = await get_raw_tx_hex(txid)
                    explorer_res = await explorer_rebroadcast_raw_tx(raw_hex)
                    if explorer_res.get("ok") is True:
                        rebroadcast_note = f"explorer rebroadcast ok txid={explorer_res.get('txid', txid)}"
                    else:
                        rebroadcast_note = f"explorer rebroadcast not ok: {explorer_res.get('error', 'unknown error')}"
                except Exception as re:
                    rebroadcast_note = f"explorer rebroadcast failed: {str(re)[:300]}"
            elif ENABLE_EXPLORER_REBROADCAST and EXPLORER_SENDRAWTX_URL:
                rebroadcast_note = "explorer rebroadcast skipped: node busy backoff active"
        except Exception as e:
            send_error = e
            err = str(e)[:500]

        con2 = db()
        try:
            con2.execute("BEGIN IMMEDIATE;")
            row = con2.execute(
                "SELECT status, discord_id, amount_sat, fee_sat, COALESCE(retry_count, 0) FROM nns_withdrawals WHERE id=?",
                (wid,)
            ).fetchone()
            if not row:
                con2.execute("ROLLBACK;")
                continue

            status_now = str(row[0])
            did_now = int(row[1])
            amt_now = int(row[2])
            fee_now = int(row[3] or 0)
            retry_count_now = int(row[4] or 0)

            if status_now not in ("pending", "retry"):
                con2.execute("ROLLBACK;")
                continue

            if txid:
                con2.execute(
                    "UPDATE nns_withdrawals SET status='sent', txid=?, error=?, retry_count=0, next_retry_at=NULL, last_attempt_ts=? WHERE id=?",
                    (txid, rebroadcast_note, now_ts(), wid)
                )
                con2.execute("COMMIT;")
                sent += 1
                if rebroadcast_note:
                    print(f"[withdraw {wid}] {rebroadcast_note}", flush=True)
            else:
                err_msg = err or "unknown error"
                ts_now = now_ts()

                # CRITICAL SAFETY RULE:
                # A timeout/transport failure after sendtoaddress is ambiguous:
                # the node may have created and broadcast the transaction while
                # the HTTP response was lost. Never retry and never refund here.
                connect_failed = isinstance(send_error, RPCConnectError)
                definitive = isinstance(send_error, RPCResponseError)
                if not connect_failed and (not definitive or is_ambiguous_send_error(err_msg)):
                    note_node_busy(err_msg, withdraw_only=True)
                    safe_err = (
                        "AMBIGUOUS sendtoaddress result; automatic retry and refund disabled; "
                        "manual wallet/on-chain review required: " + err_msg
                    )[:500]
                    con2.execute(
                        "UPDATE nns_withdrawals "
                        "SET status='broadcast_unknown', error=?, next_retry_at=NULL, last_attempt_ts=? "
                        "WHERE id=?",
                        (safe_err, ts_now, wid)
                    )
                    con2.execute("COMMIT;")
                    print(
                        f"[withdraw {wid}] BROADCAST UNKNOWN -> manual review required; "
                        f"NO retry, NO refund: {err_msg}",
                        flush=True,
                    )
                    continue

                transient = connect_failed or is_transient_withdraw_error(err_msg)
                if connect_failed or is_node_busy_error(err_msg):
                    note_node_busy(err_msg, withdraw_only=True)

                if transient and retry_count_now < int(NNS_WITHDRAW_MAX_RETRIES):
                    next_retry = ts_now + compute_withdraw_retry_delay(retry_count_now)
                    con2.execute(
                        "UPDATE nns_withdrawals SET status='retry', error=?, retry_count=?, next_retry_at=?, last_attempt_ts=? WHERE id=?",
                        (err_msg, retry_count_now + 1, next_retry, ts_now, wid)
                    )
                    con2.execute("COMMIT;")
                    print(
                        f"[withdraw {wid}] request not sent or definitive transient RPC failure -> retry {retry_count_now + 1}/{int(NNS_WITHDRAW_MAX_RETRIES)} in {max(0, next_retry - ts_now)}s: {err_msg}",
                        flush=True,
                    )
                else:
                    # Explicit RPC failure or proven connection-establishment failure:
                    # no payment occurred, so refunding the internal balance is safe.
                    con2.execute(
                        "UPDATE nns_withdrawals SET status='failed', error=?, next_retry_at=NULL, last_attempt_ts=? WHERE id=?",
                        (err_msg, ts_now, wid)
                    )
                    con2.execute(
                        "UPDATE users SET nns_internal_sat = nns_internal_sat + ?, updated_at=? WHERE discord_id=?",
                        (int(amt_now) + int(fee_now), ts_now, did_now)
                    )
                    con2.execute("COMMIT;")
                    failed += 1

        except Exception:
            try:
                con2.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            con2.close()

        await asyncio.sleep(WITHDRAW_SLEEP_BETWEEN)

    return (sent, failed)


async def main_loop() -> None:
    init_db()
    print("nns_watcher started")
    print(f"DB={DB_PATH}")
    print(f"RPC={NNS_RPC_URL}")
    print(
        f"DEPOSIT_CONFS={NNS_DEPOSIT_CONFS} "
        f"NNS_DEPOSIT_SKIP_WALLET_INTERNAL_TX={NNS_DEPOSIT_SKIP_WALLET_INTERNAL_TX} "
        f"NNS_DEPOSIT_REQUIRE_LABEL_MATCH={NNS_DEPOSIT_REQUIRE_LABEL_MATCH} "
        f"POLL_SECONDS={POLL_SECONDS} "
        f"DEPOSIT_POLL_SECONDS={DEPOSIT_POLL_SECONDS} "
        f"DEPOSIT_BOOTSTRAP_LOOKBACK_BLOCKS={NNS_DEPOSIT_BOOTSTRAP_LOOKBACK_BLOCKS} "
        f"CONFIRMATION_REFRESH_INTERVAL_SECONDS={CONFIRMATION_REFRESH_INTERVAL_SECONDS} "
        f"WITHDRAW_BATCH={max(1, WITHDRAW_BATCH)} DEPOSIT_REFRESH_BATCH={DEPOSIT_REFRESH_BATCH} "
        f"NNS_RPC_RETRIES={NNS_RPC_RETRIES} ENABLE_EXPLORER_REBROADCAST={ENABLE_EXPLORER_REBROADCAST} "
        f"NNS_WITHDRAW_MAX_RETRIES={NNS_WITHDRAW_MAX_RETRIES}"
    )

    last_log_ts = 0
    last_deposit_poll_ts = 0
    next_confirmation_refresh_ts = 0
    last_busy_log_ts = 0

    try:
        while True:
            try:
                sent = 0
                failed = 0
                seen = 0
                credited = 0
                now = now_ts()

                if is_global_backoff_active(now):
                    if (now - int(last_busy_log_ts)) >= max(60, int(LOG_HEARTBEAT_SECONDS)):
                        print(
                            f"[busy] global backoff active until {_NODE_BUSY_UNTIL_TS}; withdraw backoff until {_WITHDRAW_BUSY_UNTIL_TS}",
                            flush=True,
                        )
                        last_busy_log_ts = now
                else:
                    print("[loop] checking withdrawals", flush=True)
                    sent, failed = await process_withdrawals()

                    now = now_ts()
                    should_poll_deposits = (now - int(last_deposit_poll_ts)) >= max(60, int(DEPOSIT_POLL_SECONDS))
                    should_refresh_confirmations = now >= next_confirmation_refresh_ts

                    if should_poll_deposits:
                        print(
                            "[loop] checking deposits",
                            flush=True,
                        )
                        seen, credited = await process_deposits(do_confirmation_refresh=False)
                        last_deposit_poll_ts = now

                    if should_refresh_confirmations and not is_global_backoff_active():
                        credited += await process_confirmation_refresh()
                        # Failed batches are eligible again in a minute; global
                        # node backoff still takes precedence over this schedule.
                        delay = 60 if _CONFIRMATION_REFRESH_FAILED else max(60, int(CONFIRMATION_REFRESH_INTERVAL_SECONDS))
                        next_confirmation_refresh_ts = now_ts() + delay

                    note_node_recovered()

                if should_log_status(seen, credited, sent, failed, last_log_ts):
                    print(
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"deposits: seen={seen} credited={credited} | withdrawals: sent={sent} failed={failed}",
                        flush=True,
                    )
                    last_log_ts = now_ts()
            except Exception as e:
                msg = str(e)
                if is_node_busy_error(msg):
                    note_node_busy(msg)
                print(f"[ERROR] {e}", flush=True)

            await asyncio.sleep(POLL_SECONDS)
    finally:
        await close_rpc_session()


if __name__ == "__main__":
    asyncio.run(main_loop())

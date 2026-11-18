#!/usr/bin/env python3
"""Conservatively consolidate small 996-Coin wallet UTXOs.

Dry-run is the default. Broadcasting requires --broadcast. The program creates
and signs a raw transaction, records its TXID before sending, broadcasts once,
then exits. An ambiguous send error is never retried and leaves inputs locked.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

COIN = Decimal("0.00000001")


class RpcError(RuntimeError):
    pass


def amount(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(COIN, rounding=ROUND_DOWN)


class CliRpc:
    def __init__(self, cli: str, wallet: str, timeout: float, datadir: str = "/opt/nns-tipbot/node/data"):
        # This fork is normally a single-wallet CLI: coin996-cli selects the
        # wallet loaded by coin996d and rejects the daemon's -wallet option.
        # --wallet remains available for newer multi-wallet-compatible builds.
        self.prefix = [cli, f"-datadir={datadir}"]
        if wallet:
            self.prefix.append(f"-rpcwallet={wallet}")
        self.timeout = timeout

    def call(self, method: str, *params: Any, timeout: float | None = None) -> Any:
        call_timeout = self.timeout if timeout is None else timeout
        cmd = self.prefix + [method]
        for p in params:
            cmd.append(json.dumps(p, separators=(",", ":")) if isinstance(p, (dict, list, bool)) else str(p))
        started = time.monotonic()
        try:
            cp = subprocess.run(cmd, text=True, capture_output=True, timeout=call_timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RpcError(f"RPC {method} timed out after {call_timeout}s") from exc
        elapsed = time.monotonic() - started
        if cp.returncode:
            raise RpcError(f"RPC {method} failed ({elapsed:.1f}s): {cp.stderr.strip() or cp.stdout.strip()}")
        text = cp.stdout.strip()
        if elapsed > max(5.0, call_timeout * 0.80):
            raise RpcError(f"RPC {method} was too slow ({elapsed:.1f}s); stopping conservatively")
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cli", default="/opt/nns-tipbot/node/bin/coin996-cli")
    p.add_argument("--datadir", default="/opt/nns-tipbot/node/data", help="node data/config directory passed explicitly to every CLI call")
    p.add_argument("--wallet", default="", help="optional RPC wallet name; leave empty for this fork's single-wallet CLI")
    p.add_argument("--min-conf", type=int, default=100)
    p.add_argument("--max-inputs", type=int, default=10)
    p.add_argument("--min-inputs", type=int, default=2, help="do not create a transaction with fewer inputs")
    p.add_argument("--target-amount", type=Decimal, default=Decimal("996"))
    p.add_argument("--max-utxo", type=Decimal, default=Decimal("110"), help="only select UTXOs at or below this value")
    p.add_argument(
        "--auto-tiers",
        default="",
        help="comma-separated maximum UTXO tiers tried smallest-first, e.g. 110,1000,11000",
    )
    p.add_argument(
        "--highest-tier-max-total",
        type=Decimal,
        default=Decimal("0"),
        help="strict input-sum ceiling for the highest automatic tier; 0 disables it",
    )
    p.add_argument("--fee", type=Decimal, default=Decimal("0.01"), help="fixed fee; verify against network policy")
    p.add_argument("--sleep", type=float, default=300, help="seconds after a successful broadcast")
    p.add_argument("--rpc-timeout", type=float, default=20)
    p.add_argument("--scan-timeout", type=float, default=120, help="longer timeout used only for listunspent")
    p.add_argument("--scan-count", type=int, default=120, help="maximum UTXOs requested from listunspent server-side")
    p.add_argument("--destination", help="existing address owned by this wallet; otherwise getnewaddress is used")
    p.add_argument("--state-file", type=Path, default=Path("./consolidate_996_state.json"))
    p.add_argument("--tipbot-db", type=Path, default=Path("/opt/nns-tipbot/tipbot.db"), help="existing database checked read-only to exclude user deposit destinations")
    p.add_argument("--broadcast", action="store_true", help="sign and broadcast exactly one transaction")
    p.add_argument("--auto-advance", action="store_true", help="archive a resolved state and continue only after enough confirmations")
    p.add_argument("--required-confirmations", type=int, default=6)
    return p.parse_args()


def require_wallet_destination(rpc: CliRpc, destination: str) -> None:
    """Reject invalid or non-wallet destinations across old/new Core RPC APIs."""
    validation = rpc.call("validateaddress", destination)
    if not isinstance(validation, dict) or not validation.get("isvalid"):
        raise SystemExit("--destination must be a valid address")

    # Older Bitcoin-Core-like forks expose ownership via validateaddress.
    # Newer ones moved wallet-specific fields such as ismine to getaddressinfo.
    if "ismine" in validation:
        if validation.get("ismine") is True:
            return
        raise SystemExit("--destination is valid but is not owned by this wallet")

    try:
        address_info = rpc.call("getaddressinfo", destination)
    except RpcError as exc:
        raise SystemExit(
            "--destination is valid, but wallet ownership could not be verified: "
            "validateaddress returned no ismine field and getaddressinfo failed "
            f"({exc})"
        ) from exc

    if not isinstance(address_info, dict) or address_info.get("ismine") is not True:
        raise SystemExit("--destination is valid but is not owned by this wallet")


def require_unassigned_destination(path: Path, destination: str) -> None:
    """Fail closed if the address belongs to a user or the database cannot be checked."""
    try:
        con = sqlite3.connect(path.expanduser().resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT discord_id FROM users WHERE TRIM(nns_deposit_address) = ? LIMIT 1",
                (destination.strip(),),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise SystemExit(f"Cannot verify consolidator destination against tipbot DB: {exc}") from exc
    if row is not None:
        raise SystemExit("Consolidator destination is assigned to a tipbot user; refusing consolidation")


def eligible(u: dict[str, Any], min_conf: int, max_utxo: Decimal, locked: set[tuple[str, int]]) -> bool:
    outpoint = (str(u.get("txid", "")), int(u.get("vout", -1)))
    return (
        outpoint not in locked
        and int(u.get("confirmations", 0)) >= min_conf
        and bool(u.get("spendable", True))
        and bool(u.get("solvable", True))
        and bool(u.get("safe", True))
        and not bool(u.get("generated", False))
        and not bool(u.get("coinbase", False))
        and not bool(u.get("stake", False))
        and not bool(u.get("coinstake", False))
        and amount(u.get("amount", 0)) <= max_utxo
    )


def save_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def check_previous(rpc: CliRpc, path: Path, auto_advance: bool, required_confirmations: int) -> bool:
    if not path.exists():
        return True
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("status") not in {"prepared", "broadcast_unknown", "broadcast", "broadcast_seen"}:
        return True
    txid = state["txid"]
    try:
        tx = rpc.call("gettransaction", txid)
    except RpcError:
        raise SystemExit(
            f"STOP: unresolved previous TXID {txid}. Inputs remain wallet-locked. "
            "Check gettransaction/getrawtransaction and the chain before changing the state file."
        )
    confirmations = int(tx.get("confirmations", 0)) if isinstance(tx, dict) else 0
    state["status"] = "broadcast_seen"
    state["confirmations"] = confirmations
    save_state(path, state)
    if not auto_advance:
        print(f"Previous TXID {txid} is visible; confirmations={confirmations}. No new batch made.")
        return False
    if confirmations < required_confirmations:
        print(
            f"Previous TXID {txid} has {confirmations}/{required_confirmations} confirmations. No new batch made."
        )
        return False
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = path.with_name(f"{path.stem}.{stamp}.{txid}.confirmed{path.suffix}")
    os.replace(path, archived)
    print(f"Previous TXID {txid} has {confirmations} confirmations; state archived as {archived}.")
    return True


def main() -> int:
    a = parse_args()
    if a.max_inputs < 2 or a.max_inputs > 200:
        raise SystemExit("--max-inputs must be between 2 and 200")
    if a.min_inputs < 2 or a.min_inputs > a.max_inputs:
        raise SystemExit("--min-inputs must be between 2 and --max-inputs")
    if a.scan_count < a.max_inputs or a.scan_count > 1000:
        raise SystemExit("--scan-count must be between --max-inputs and 1000")
    if a.required_confirmations < 1:
        raise SystemExit("--required-confirmations must be at least 1")
    if a.target_amount <= 0 or a.max_utxo <= 0 or a.fee <= 0:
        raise SystemExit("amounts and fee must be positive")
    if a.highest_tier_max_total < 0:
        raise SystemExit("--highest-tier-max-total cannot be negative")
    if 0 < a.highest_tier_max_total <= a.fee:
        raise SystemExit("--highest-tier-max-total must exceed the fee")

    if a.auto_tiers:
        try:
            tiers = [amount(x.strip()) for x in a.auto_tiers.split(",") if x.strip()]
        except Exception as exc:
            raise SystemExit(f"invalid --auto-tiers value: {exc}") from exc
        if not tiers or any(x <= 0 for x in tiers) or tiers != sorted(set(tiers)):
            raise SystemExit("--auto-tiers must contain unique positive ascending amounts")
    else:
        tiers = [a.max_utxo]

    rpc = CliRpc(a.cli, a.wallet, a.rpc_timeout, a.datadir)
    rpc.call("getblockchaininfo")  # latency/correct-daemon health gate
    if not check_previous(rpc, a.state_file, a.auto_advance, a.required_confirmations):
        return 0

    locked_rows = rpc.call("listlockunspent") or []
    locked = {(str(x["txid"]), int(x["vout"])) for x in locked_rows}
    # Try the smallest tier that can fill a complete batch. All filtering and
    # result caps are applied inside coin996d, avoiding the full wallet result.
    rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    selected_tier: Decimal | None = None
    selected_min_inputs = a.min_inputs
    selected_total_cap = Decimal("0")
    for tier in tiers:
        query_options = {
            "maximumAmount": float(tier),
            "maximumCount": a.scan_count,
        }
        tier_rows = rpc.call(
            "listunspent", a.min_conf, 9999999, [], False, query_options,
            timeout=a.scan_timeout,
        ) or []
        tier_candidates = sorted(
            (u for u in tier_rows if eligible(u, a.min_conf, tier, locked)),
            key=lambda u: (amount(u["amount"]), u["txid"], int(u["vout"])),
        )

        tier_min_inputs = a.min_inputs
        tier_total_cap = Decimal("0")
        if tier == tiers[-1] and a.highest_tier_max_total > 0:
            # The highest tier moves larger values. Build the largest useful
            # prefix whose input sum stays strictly below the configured cap.
            tier_total_cap = a.highest_tier_max_total
            capped: list[dict[str, Any]] = []
            capped_total = Decimal("0")
            for u in tier_candidates:
                value = amount(u["amount"])
                if len(capped) >= a.max_inputs or capped_total + value >= tier_total_cap:
                    break
                capped.append(u)
                capped_total += value
            tier_candidates = capped
            tier_min_inputs = 2

        print(
            f"Tier <= {tier}: server returned {len(tier_rows)}/{a.scan_count}; "
            f"selectable={len(tier_candidates)}; required={tier_min_inputs}; "
            f"input_sum_cap={'none' if tier_total_cap == 0 else f'< {tier_total_cap}'}"
        )
        if len(tier_candidates) >= tier_min_inputs:
            rows = tier_rows
            candidates = tier_candidates
            selected_tier = tier
            selected_min_inputs = tier_min_inputs
            selected_total_cap = tier_total_cap
            break

    if selected_tier is None:
        print("No tier can fill a safe batch under the configured limits; nothing to do.")
        return 0

    print(f"Selected automatic tier <= {selected_tier}")
    chosen: list[dict[str, Any]] = []
    total = Decimal("0")
    for u in candidates:
        value = amount(u["amount"])
        if selected_total_cap > 0 and total + value >= selected_total_cap:
            break
        chosen.append(u)
        total += value
        if len(chosen) >= a.max_inputs or total >= a.target_amount:
            break
    if len(chosen) < selected_min_inputs or total <= a.fee:
        print(
            f"No safe consolidation batch available: selected {len(chosen)}, "
            f"require at least {selected_min_inputs}."
        )
        return 0

    inputs = [{"txid": u["txid"], "vout": int(u["vout"])} for u in chosen]
    destination = a.destination
    if destination:
        require_wallet_destination(rpc, destination)
    elif a.broadcast:
        destination = str(rpc.call("getnewaddress", "utxo-consolidation"))
    else:
        destination = "DRY_RUN_FRESH_WALLET_ADDRESS"
    if destination != "DRY_RUN_FRESH_WALLET_ADDRESS":
        require_unassigned_destination(a.tipbot_db, destination)
    output_value = (total - a.fee).quantize(COIN, rounding=ROUND_DOWN)

    print(json.dumps({
        "mode": "BROADCAST" if a.broadcast else "DRY-RUN",
        "selected_tier": str(selected_tier),
        "input_sum_cap": None if selected_total_cap == 0 else f"< {selected_total_cap}",
        "inputs": inputs, "input_count": len(inputs), "input_total": str(total),
        "fee": str(a.fee), "destination": destination, "output": str(output_value),
    }, indent=2))
    if not a.broadcast:
        print("Dry-run only. Re-run with --broadcast after reviewing the batch and fee.")
        return 0

    if rpc.call("lockunspent", False, inputs) is not True:
        raise SystemExit("Could not lock selected inputs; nothing was sent")
    try:
        raw = str(rpc.call("createrawtransaction", inputs, {destination: float(output_value)}))
        try:
            signed = rpc.call("signrawtransactionwithwallet", raw)
        except RpcError as modern_error:
            print(f"signrawtransactionwithwallet unavailable ({modern_error}); trying legacy signrawtransaction", file=sys.stderr)
            signed = rpc.call("signrawtransaction", raw)
        if not isinstance(signed, dict) or not signed.get("complete"):
            raise RpcError(f"transaction signing incomplete: {signed}")
        signed_hex = signed["hex"]
        decoded = rpc.call("decoderawtransaction", signed_hex)
        txid = decoded["txid"]
        state = {"status": "prepared", "txid": txid, "inputs": inputs, "destination": destination,
                 "output": str(output_value), "fee": str(a.fee), "raw_transaction": signed_hex}
        save_state(a.state_file, state)
        try:
            returned_txid = str(rpc.call("sendrawtransaction", signed_hex))  # exactly once
        except RpcError as exc:
            state["status"] = "broadcast_unknown"
            state["error"] = str(exc)
            save_state(a.state_file, state)
            print(f"BROADCAST OUTCOME UNKNOWN for {txid}: {exc}", file=sys.stderr)
            print("STOP. Do not resend or unlock inputs until this exact TXID is checked on-chain.", file=sys.stderr)
            return 2
        if returned_txid != txid:
            raise RpcError(f"daemon returned unexpected TXID {returned_txid}, expected {txid}")
        state["status"] = "broadcast"
        save_state(a.state_file, state)
        print(f"Broadcast once: {txid}. Inputs remain locked until wallet marks them spent.")
        time.sleep(max(0, a.sleep))
        return 0
    except Exception:
        # Unlock only if no signed transaction reached the prepared state.
        prepared = a.state_file.exists() and json.loads(a.state_file.read_text()).get("status") in {"prepared", "broadcast_unknown", "broadcast"}
        if not prepared:
            try:
                rpc.call("lockunspent", True, inputs)
            except RpcError:
                pass
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RpcError as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        raise SystemExit(1)
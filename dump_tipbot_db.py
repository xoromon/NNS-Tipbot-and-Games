#!/usr/bin/env python3
"""Read-only NNS support report. Configuration is via CLI, not .env."""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

SATS = Decimal(100_000_000)
SECTIONS = {
    'users': ('users', ['nns_internal_sat', 'updated_at']),
    'deposits': ('nns_deposits', ['first_seen_ts', 'txid', 'vout']),
    'withdrawals': ('nns_withdrawals', ['id', 'ts']),
    'nns_stakes': ('nns_stakes', ['updated_at', 'discord_id']),
    'watcher': ('nns_watcher_state', ['id']),
}
USER_COLUMNS = (
    'discord_id', 'nns_internal_sat', 'nns_deposit_address',
    'last_nns_claim_at', 'last_withdraw_at', 'created_at', 'updated_at',
)


def identifier(name):
    return '"' + name.replace('"', '""') + '"'


def columns(con, table):
    return [row['name'] for row in con.execute('PRAGMA table_info(' + identifier(table) + ')')]


def deposit_state(row):
    if row.get('credited'):
        return 'credited'
    if row.get('ignored'):
        return 'ignored'
    return 'pending'


def collect_section(con, section, args):
    table, ordering = SECTIONS[section]
    cols = columns(con, table)
    if not cols:
        return {'table': table, 'available': False, 'reason': 'Table not found', 'rows': []}
    selected = [c for c in USER_COLUMNS if c in cols] if section == 'users' else cols
    if not selected:
        raise ValueError('No supported NNS columns in ' + table)
    where, params = [], []
    if args.user_id is not None and section != 'watcher':
        if 'discord_id' not in cols:
            raise ValueError(table + ' cannot be filtered: discord_id is missing')
        where.append('discord_id = ?')
        params.append(args.user_id)
    if section == 'deposits' and args.deposit_status:
        if 'credited' not in cols:
            raise ValueError(table + ' cannot be filtered: credited is missing')
        ignored = 'COALESCE(ignored, 0)' if 'ignored' in cols else '0'
        condition = {
            'credited': 'COALESCE(credited, 0) != 0',
            'pending': 'COALESCE(credited, 0) = 0 AND ' + ignored + ' = 0',
            'ignored': 'COALESCE(credited, 0) = 0 AND ' + ignored + ' != 0',
        }[args.deposit_status]
        where.append('(' + condition + ')')
    if section == 'withdrawals' and args.withdraw_status:
        if 'status' not in cols:
            raise ValueError(table + ' cannot be filtered: status is missing')
        where.append('status = ?')
        params.append(args.withdraw_status)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    count = con.execute('SELECT COUNT(*) FROM ' + identifier(table) + clause, params).fetchone()[0]
    query = 'SELECT ' + ', '.join(map(identifier, selected)) + ' FROM ' + identifier(table) + clause
    order = [c for c in ordering if c in cols]
    if order:
        query += ' ORDER BY ' + ', '.join(identifier(c) + ' DESC' for c in order)
    if args.limit:
        query += ' LIMIT ?'
        params = params + [args.limit]
    rows = [dict(row) for row in con.execute(query, params)]
    if section == 'deposits':
        for row in rows:
            row['deposit_state'] = deposit_state(row)
    result = {'table': table, 'available': True, 'matching_rows': count, 'shown_rows': len(rows), 'rows': rows}
    if section in ('users', 'nns_stakes'):
        amounts = ['nns_internal_sat'] if section == 'users' else ['staked_sat', 'accrued_reward_sat']
        # Sum with Python integers to avoid SQLite's signed-64-bit SUM overflow.
        totals = {}
        for col in amounts:
            if col in cols:
                values = con.execute('SELECT ' + identifier(col) + ' FROM ' + identifier(table) + clause,
                                     params[:-1] if args.limit else params)
                totals[col] = sum(int(row[0] or 0) for row in values)
        result['totals_sat'] = totals
    return result


def display_value(key, value):
    if value is None:
        return ''
    if key.endswith('_sat'):
        return f'{Decimal(str(value)) / SATS:.8f} NNS'
    if key == 'reward_remainder':
        return str(value) + ' fractional satoshis'
    if key == 'ts' or key.endswith(('_at', '_ts')):
        if not value:
            return ''
        try:
            return datetime.fromtimestamp(int(value), timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return str(value).replace('\n', '\\n').replace('\r', '\\r')


def print_report(report):
    print('NNS database report: ' + report['database'])
    print('Times: UTC. Amounts: NNS; staking remainder: fractional satoshis.')
    for name, section in report['sections'].items():
        print('\n' + name.upper())
        if not section['available']:
            print('  ' + section['reason'] + ': ' + section['table'])
            continue
        print(f"  Showing {section['shown_rows']} of {section['matching_rows']} matching rows")
        for key, value in section.get('totals_sat', {}).items():
            print('  Total ' + key + ': ' + display_value(key, value))
        for row in section['rows']:
            print('  ---')
            for key, value in row.items():
                print('  ' + key + ': ' + display_value(key, value))


def main():
    parser = argparse.ArgumentParser(description='Read-only NNS overview and deposit/withdrawal diagnostics.')
    parser.add_argument('--db', default='tipbot.db', help='Database path (default: tipbot.db; .env is not loaded)')
    for section in SECTIONS:
        parser.add_argument('--' + section.replace('_', '-'), action='store_true', help='Include ' + section.replace('_', ' '))
    parser.add_argument('--user-id', type=int, help='Filter user-specific sections by Discord user ID; watcher stays global')
    parser.add_argument('--deposit-status', choices=('pending', 'credited', 'ignored'), help='Filter deposits; automatically includes deposits')
    parser.add_argument('--withdraw-status', help='Exact stored withdrawal status; automatically includes withdrawals')
    parser.add_argument('--limit', type=int, default=50, help='Rows per section; 0 = all (default: 50)')
    parser.add_argument('--json', action='store_true', help='Raw JSON: amounts in satoshis and timestamps in Unix seconds')
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('--limit must be nonnegative')
    if args.user_id is not None and args.user_id <= 0:
        parser.error('--user-id must be positive')
    chosen = [key for key in SECTIONS if getattr(args, key)]
    for condition, key in ((args.deposit_status, 'deposits'), (args.withdraw_status, 'withdrawals')):
        if condition and key not in chosen:
            chosen.append(key)
    if not chosen:
        chosen = list(SECTIONS)
    path = Path(args.db).expanduser().resolve()
    try:
        con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)
        try:
            con.row_factory = sqlite3.Row
            con.execute('PRAGMA query_only=ON')
            con.execute('BEGIN')
            report = {'database': str(path), 'sections': {key: collect_section(con, key, args) for key in chosen}}
        finally:
            con.close()
    except (sqlite3.Error, ValueError) as exc:
        print('NNS report failed: ' + str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_report(report)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

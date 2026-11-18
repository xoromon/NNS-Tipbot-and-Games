# NNS Tipbot — Linux installation

This package contains the Discord bot `nns_tipbot.py` with neutral Discord ID defaults, the deposit/withdrawal worker `nns_watcher.py`, the game bots `nns_tilegame.py`, `nns_territory_game.py`, and `nns_dungeon_game.py`, an annotated `.env.example`, and Python dependency requirements. The instructions below use Debian/Ubuntu, Python 3.10 or newer, and a dedicated Linux user. Run only one instance of each component against the shared database.

## Package contents and installation order

| Component | File / service |
| --- | --- |
| Discord tipbot | `nns_tipbot.py` / `nns-tipbot.service` |
| Deposit/withdrawal watcher | `nns_watcher.py` / `nns-watcher.service` |
| Tile Game | `nns_tilegame.py` / `nns-tilegame.service` |
| Territory Game | `nns_territory_game.py` / `nns-territory-game.service` |
| Dungeon Game | `nns_dungeon_game.py` / `nns-dungeon-game.service` |
| APR updater | `update_nns_apr_cache.py` / `cron/nns-apr-cache.crontab` |
| Read-only reports | `dump_tipbot_db.py`, `dump_dungeon_db.py` |
| UTXO consolidator | `consolidate_996_utxos.py` / `996coin-consolidator.timer` + service |
| Confirmed-state archival | `archive_consolidator_states.py` (manual, preview by default) |
| 996Coin node | `systemd/996coind.service`; binary/wallet supplied separately |

For a fresh deployment: install Python dependencies and configure `.env`; complete [NNS node preparation](#nns-node-preparation), including RPC and the essential consolidator service/timer; start the tipbot once to initialize the shared schema; then start the watcher and configured games. If using staking, run the APR updater successfully and install its cron job. All five application units are supplied under `systemd/`; only enable components you intend to run. All components use the same service account, `nns-tipbot`, and the directory layout below.

## Scope and prerequisites

You need a Discord bot token, permission to install the bot in your Discord server, and an NNS wallet RPC endpoint for deposit-address creation. Install the bot with the `bot` and `applications.commands` scopes and grant access to its channels, including View Channel, Send Messages, Embed Links, and Read Message History. The script uses default Discord intents.

The included watcher scans deposits, credits confirmed deposits, processes queued withdrawals, and optionally rebroadcasts transactions through the explorer. Run one bot and one watcher against the same SQLite database and NNS wallet. Start the bot first: its initialization creates tables required by the watcher. Claims and staking rewards credit internal balances; arrange the corresponding wallet funding separately. The included `update_nns_apr_cache.py` produces the staking APR cache when invoked manually or by cron.

The Tile Game, Territory Game, and Dungeon bots are included. The node-side consolidator and its optional archive helper are also included. Both `dump_dungeon_db.py` and the NNS-only `dump_tipbot_db.py` are included for read-only inspection.

## NNS node preparation

### Why the consolidator is important

**Treat the UTXO consolidator as an essential part of this staking-node deployment, not an optional housekeeping task.** In staking deployments, the accumulation of small outputs has been observed to affect node stability. Staking produces large numbers of small UTXOs. As these accumulate, wallet operations become increasingly expensive and the node can become unstable or crash, especially when processing withdrawals requested through `nns_tipbot` and broadcast by `nns_watcher.py`. The consolidator reduces this fragmentation by periodically combining eligible outputs into larger wallet-owned outputs.

Install, configure, and verify the consolidator timer as part of node preparation before enabling production withdrawals. Keep it enabled during normal staking operation and monitor failures or unresolved transactions. Consolidation mitigates the observed UTXO-related instability; it does not guarantee that unrelated node crashes cannot occur.

Preparation order: verify the node account, binary and existing wallet; install/start the daemon service; verify RPC; install and review the consolidator service and timer; verify their operation; then initialize and enable the bot stack.

### 996Coin daemon service

For a new installation, create the shared service account once (skip if it already exists):

```bash
sudo useradd --system --user-group --home-dir /opt/nns-tipbot --create-home --shell /usr/sbin/nologin nns-tipbot
```

The node, consolidator, Discord bots, watcher, and APR cron job all use this account.

| Directory | Contents |
| --- | --- |
| `/opt/nns-tipbot` | Python scripts, `.env`, bot databases, virtual environment |
| `/opt/nns-tipbot/node/bin` | `coin996d` and `coin996-cli` |
| `/opt/nns-tipbot/node/data` | Node configuration, blockchain, and wallet data |
| `/opt/nns-tipbot/node/consolidator` | Active state, confirmed snapshots, and monthly archives |

Create the node directories before starting either node-side service:

```bash
sudo install -d -o nns-tipbot -g nns-tipbot -m 0700 /opt/nns-tipbot/node/bin /opt/nns-tipbot/node/data /opt/nns-tipbot/node/consolidator
```

Install the trusted binaries from your existing 996Coin distribution into `node/bin` (replace `/path/to/996coin_linux3`):

```bash
sudo install -o nns-tipbot -g nns-tipbot -m 0755 /path/to/996coin_linux3/coin996d /path/to/996coin_linux3/coin996-cli /opt/nns-tipbot/node/bin/
```

For migration, stop the old consolidator timer, let its active invocation finish, stop wallet-writing applications, and cleanly stop the old daemon before copying data. Back up and copy the **complete existing node data directory**, including its configuration and wallet, into `node/data`; preserve the consolidator's current `state.json` and archives in `node/consolidator`. Set ownership of the copied files to `nns-tipbot:nns-tipbot`. Do not start the new services against an empty replacement wallet or a missing unresolved state. Update the node configuration's absolute paths if it contains any. These instructions do not move existing data automatically.

The node service is provided at `systemd/996coind.service` and uses the shared account/path layout. Its generic deployment settings are:

| Setting | Supplied value |
| --- | --- |
| Service user/group | `nns-tipbot` / `nns-tipbot` |
| Working directory | `/opt/nns-tipbot` |
| Daemon binary | `/opt/nns-tipbot/node/bin/coin996d` |
| Node data directory | `/opt/nns-tipbot/node/data` |
| Wallet argument | `-wallet=discord` |

The node runs under the same `nns-tipbot` account as the application services. This package does not include the daemon binary, node configuration, or wallet. Ensure the user/group exist, the daemon is executable, and the node account can access its data directory and wallet. Preserve the existing wallet and node data during setup.

### Hosting: a VPS is not required

The complete stack can run on an always-on Linux computer or home server with sufficient storage, memory, and reliable Internet connectivity. A VPS is optional. Node and bots can share one machine, or the node can run on a separate trusted LAN host. Discord bots connect outbound; they do not require an Internet-exposed RPC port. The consolidator runs on the node host, while the bot `.env` points to the chosen RPC host.

### Node configuration: `996coin.conf`

With the daemon's default data directory, the usual configuration path is `~/.996coin/996coin.conf`, where `~` belongs to the account running the node. **This project's service and CLI explicitly use `-datadir=/opt/nns-tipbot/node/data`, so put the configuration at `/opt/nns-tipbot/node/data/996coin.conf`.** Editing only `~/.996coin/996coin.conf` will not configure this deployment.

The template is supplied as `config/996coin.conf.example`. For a new installation, from the staging project directory:

```bash
sudo install -o nns-tipbot -g nns-tipbot -m 0600 config/996coin.conf.example /opt/nns-tipbot/node/data/996coin.conf
sudo nano /opt/nns-tipbot/node/data/996coin.conf
```

For an existing node, merge the settings into its configuration; do not overwrite its other settings. Replace every placeholder before starting:

```ini
server=1
daemon=0
listen=1
rpcbind=127.0.0.1
rpcallowip=127.0.0.1
# Add another trusted client IP only when needed:
# rpcallowip=192.168.1.50
rpcport=REPLACE_WITH_RPC_PORT
rpcuser=REPLACE_WITH_RPC_USER
rpcpassword=REPLACE_WITH_STRONG_RPC_PASSWORD
staking=1
txindex=1
```

Use a valid numeric RPC port and a strong unique RPC password. Set the matching `.env` values:

```dotenv
NNS_RPC_URL=http://127.0.0.1:REPLACE_WITH_RPC_PORT/
NNS_RPC_USER=REPLACE_WITH_RPC_USER
NNS_RPC_PASSWORD=REPLACE_WITH_STRONG_RPC_PASSWORD
```

`server=1` enables RPC; `daemon=0` keeps systemd in control of the foreground process. `listen=1` concerns peer-to-peer connections, not RPC access. Staking also depends on wallet readiness and any wallet unlock requirements. Enabling `txindex` on an existing chain may require index building or reindexing depending on the installed 996Coin version; check its startup log.

#### RPC access from a home network

If everything runs on one machine, keep `rpcbind=127.0.0.1`. For a separate trusted LAN client, bind RPC to the node's LAN interface (and retain loopback access for the local CLI) and add the client's IP with `rpcallowip`. Confirm the installed fork supports the chosen bind configuration.

For this guide's single-machine setup, use only `rpcbind=127.0.0.1` and `rpcallowip=127.0.0.1`; no LAN RPC access or router port forwarding is needed. If you later separate node and bots, explicitly configure the node's LAN interface and permitted client IPs, and restrict the RPC port with the host firewall. Do not expose RPC to the public Internet. See the upstream [Bitcoin Core RPC security principles](https://github.com/bitcoin/bitcoin/blob/master/doc/JSON-RPC-interface.md), and verify supported options against the installed 996Coin fork.

For a LAN node, use its LAN IP in `NNS_RPC_URL`, not `127.0.0.1` (which would refer to the client itself). Restart the node after configuration changes and verify local CLI and intended-client RPC access before resuming withdrawals.

#### Install and start the daemon

From this project folder, install the unit. On a different server, adjust `User`, `Group`, `WorkingDirectory`, and all paths in `ExecStart` before enabling it:

```bash
sudo install -m 0644 systemd/996coind.service /etc/systemd/system/996coind.service
sudo systemd-analyze verify /etc/systemd/system/996coind.service
sudo systemctl daemon-reload
sudo systemctl enable --now 996coind
sudo systemctl status 996coind
sudo journalctl -u 996coind -f
```

If the node is already running outside systemd with the same data directory, shut that instance down cleanly before starting the service. If this service is already running and you change its unit, use `sudo systemctl daemon-reload` followed by `sudo systemctl restart 996coind` to apply the change.

#### Process supervision

`-daemon=0` keeps the node in the foreground so `Type=simple` tracks its actual process. The unit requests shutdown using SIGTERM, allows up to 300 seconds, and permits a final SIGKILL if it does not exit. There is no CLI-based `ExecStop`. Failed processes restart after 30 seconds; the start limit permits three starts within 15 minutes. After diagnosing a start-limit failure, clear the failed state and start again:

```bash
sudo systemctl reset-failed 996coind
sudo systemctl start 996coind
```

The unit retains `LimitNOFILE=65535`, `UMask=0077`, and `TimeoutStartSec=120s`. The start timeout is not a blockchain synchronization or RPC-readiness check. `TimeoutStopSec=300s` controls the shutdown grace period, including when stopping for reboot.

#### RPC configuration for the bot stack

Configure the node's RPC credentials and set the shared `.env` values `NNS_RPC_URL`, `NNS_RPC_USER`, and `NNS_RPC_PASSWORD` to match. The service command does not specify an RPC port or RPC credentials. Verify the actual endpoint rather than inferring a port from the service file; the supplied Python scripts have different fallback ports. Confirm that the endpoint addresses the intended `discord` wallet for the installed daemon's RPC behavior.

Start and verify the node before the bot, watcher, and APR updater. An active systemd status means the process is running; it does not establish that RPC is ready or the chain is synchronized. The supplied bot services and APR cron job are not automatically coupled to this unit, allowing the wallet RPC to run on another host. In particular, the cron updater can run while the node is unavailable and then report an RPC failure.

For an orderly maintenance shutdown, stop the watcher before stopping the node so it does not initiate new wallet requests during shutdown:

```bash
sudo systemctl stop nns-watcher
sudo systemctl stop 996coind
```

After maintenance, start the node, verify RPC readiness, then start the watcher again. Installing this unit and verifying live daemon behavior must be done on the Linux server; no server configuration was changed while preparing this package.

### Install the conservative UTXO consolidator

`consolidate_996_utxos.py` consolidates eligible wallet outputs and checks that the destination is not assigned to a tipbot user. Its service and timer are under `systemd/` and run under the shared `nns-tipbot` account. Both this script and the optional archive helper use only the Python standard library and CLI options; they do not load `.env`. System Python must be 3.10 or newer.

The service enables `--broadcast` and includes the destination placeholder `REPLACE_WITH_NEW_UNASSIGNED_WALLET_ADDRESS`. Generate a new wallet-owned address as described below before enabling the timer. Never use a user deposit address. Its default CLI is `/opt/nns-tipbot/node/bin/coin996-cli`. Every CLI invocation includes `-datadir=/opt/nns-tipbot/node/data`; the shared bot `.env` does not configure the CLI. The service explicitly passes `--cli` and `--datadir`, matching the script defaults. This fork's single-wallet CLI normally requires no `--wallet`; do not copy the daemon's `-wallet=discord` option into the CLI command.

#### Operating parameters

| Parameter | Service value / meaning |
| --- | --- |
| Input confirmations | At least 100 |
| Input count | 100 for lower tiers; at most 100 overall |
| Automatic UTXO tiers | 110, 1000, 11000 NNS, tried smallest first |
| Highest-tier total | Strictly below 150000 NNS; this tier permits a minimum of two inputs |
| Target amount | 100000000 NNS selection target, not a required output amount; count/tier caps take precedence |
| Fee | Fixed 0.01 NNS |
| RPC / scan timeouts | 20 / 120 seconds |
| Server-side scan cap | 300 UTXOs per tier |
| Previous-transaction gate | At least six confirmations before auto-advancing |
| Post-send sleep | Zero seconds |
| Invocation | At most one transaction; no resend after an ambiguous broadcast |

The timer runs approximately ten minutes after boot, then fifteen minutes after the service becomes inactive, with up to 60 seconds of randomized delay and 30 seconds of timer accuracy. It is not a fixed wall-clock quarter-hour schedule. `Persistent=false` does not request catch-up execution.

`Restart=no` disables service-level automatic restart, but the timer can invoke the service again later. On those invocations the saved TXID is checked; an unresolved transaction blocks a new batch, and a sufficiently confirmed one can auto-advance. Inspect errors in the journal and stop the timer if intervention is needed. The oneshot timeout is five minutes and applies to the entire invocation.

#### Detailed installation or update on the node host

Run these steps on the host running `996coind.service`. The examples use the generic node service account `nns-tipbot`; adapt every path and both unit files consistently if your setup differs. Create the shared account during daemon setup above; it must own/access its wallet data. The node-side scripts use system Python, not the bot virtual environment.

1. Install/check the prerequisites on Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y python3
/usr/bin/python3 --version
id nns-tipbot
sudo -u nns-tipbot test -x /opt/nns-tipbot/node/bin/coin996-cli
sudo systemctl status 996coind
sudo -u nns-tipbot /opt/nns-tipbot/node/bin/coin996-cli -datadir=/opt/nns-tipbot/node/data getblockchaininfo
```

Python must be 3.10 or newer. Confirm the CLI reaches the intended running node and the chain is synchronized. The daemon and CLI binaries are not installed by this package.

2. Generate a dedicated consolidation address on the node host:

```bash
sudo -u nns-tipbot /opt/nns-tipbot/node/bin/coin996-cli -datadir=/opt/nns-tipbot/node/data getnewaddress
```

Copy the returned address and replace `REPLACE_WITH_NEW_UNASSIGNED_WALLET_ADDRESS` in `systemd/996coin-consolidator.service` before installing it. Generate it once for this deployment and keep it dedicated to consolidation. Do not assign it to a user or insert it into `users.nns_deposit_address` in `tipbot.db`.

The consolidator checks both wallet ownership and the destination against `users.nns_deposit_address` before locking inputs or creating a new transaction. Its `--tipbot-db /opt/nns-tipbot/tipbot.db` argument must point to the same database used by the bot/watcher (adjust it if `TIPBOT_DB` differs). An assigned address, missing database, or unreadable/incompatible schema stops consolidation. The database is opened read-only; no user mapping is created or changed. This check also applies to automatically generated addresses when `--destination` is omitted during a broadcast invocation.

Initialize the tipbot database before enabling the consolidator timer. You can initialize its schema offline, after installing the application dependencies and configuring `.env`, without starting Discord or withdrawals:

```bash
cd /opt/nns-tipbot
sudo -u nns-tipbot .venv/bin/python -c 'from nns_tipbot import init_db; init_db()'
```

The node and schema must be ready before enabling consolidation; production withdrawals should remain stopped until consolidation is configured and verified. Keep the dedicated address unassigned in future as well. The script checks current database contents, but does not reserve the address against later manual assignment.

3. Use a staging copy of this project on the node host. Before replacing a running installation, stop the timer and allow any current service invocation to finish; confirm it is inactive. Do not run a manual instance alongside the service. Preserve the existing state directory, especially `state.json`.

```bash
sudo systemctl stop 996coin-consolidator.timer
sudo systemctl status 996coin-consolidator.service
```

4. For a first installation, the initial stop/status may report that the units do not exist yet. Create the directories, install the scripts and units, and review the target account, paths and destination before enabling the timer:

```bash
sudo install -d -o nns-tipbot -g nns-tipbot -m 0700 /opt/nns-tipbot /opt/nns-tipbot/node/consolidator
sudo install -o nns-tipbot -g nns-tipbot -m 0700 consolidate_996_utxos.py archive_consolidator_states.py /opt/nns-tipbot/
sudo install -m 0644 systemd/996coin-consolidator.service systemd/996coin-consolidator.timer /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/996coin-consolidator.service /etc/systemd/system/996coin-consolidator.timer
sudo systemctl daemon-reload
sudo systemctl enable --now 996coin-consolidator.timer
sudo systemctl list-timers --all 996coin-consolidator.timer
sudo journalctl -u 996coin-consolidator.service -n 100
```

5. Verify the installed configuration and follow the first scheduled run:

```bash
sudo systemctl cat 996coin-consolidator.service 996coin-consolidator.timer
sudo systemctl is-enabled 996coin-consolidator.timer
sudo systemctl status 996coin-consolidator.timer
sudo journalctl -u 996coin-consolidator.service -f
```

Check that the timer is active/waiting, that the complete `ExecStart` matches the intended parameters, and that the journal shows successful scans or a reason no eligible batch is available. A successful oneshot normally returns to inactive after it finishes; it is the timer that remains active. After a broadcast, the next invocations must wait until the previous TXID has the required confirmations. An existing unresolved state must be investigated before treating consolidation as operational. Do not delete the state or manually resend a transaction to bypass this check.

Enable the **timer**, not the oneshot service. Enabling the timer authorizes the configured broadcast runs; depending on uptime, its first invocation may be immediate. The service requires `996coind.service`, but service ordering alone does not prove wallet RPC readiness. On a migration, preserve the active state and point every invocation at the same state file. Starting without an unresolved existing state removes the previous-transaction gate.

Omitting `--broadcast` prevents creating/signing/sending a new transaction. It is not a completely read-only preview: `check_previous` can update or archive the current state, especially with `--auto-advance`. Never use a separate empty state file to bypass a pending transaction.

#### Optional: archive confirmed states

For tidier storage, `archive_consolidator_states.py` bundles confirmed snapshots older than 30 days into verified monthly ZIPs. This is optional and does not affect normal operation. The active `state.json` is never archived.

Stop the consolidator timer and wait for its service to finish. Preview the archive operation:

```bash
sudo -u nns-tipbot /usr/bin/python3 /opt/nns-tipbot/archive_consolidator_states.py --state-dir /opt/nns-tipbot/node/consolidator
```

Add `--apply` to archive the files, then restart `996coin-consolidator.timer`. Use `--keep-days N` to change the retention period for individual snapshots.

## 1. Install system packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nano
python3 --version
# Account nns-tipbot was created during NNS node preparation.
id nns-tipbot
```

Use Python 3.10 or newer. Create the shared account in the node preparation section if not already present.

## 2. Copy the project and create the virtual environment

Upload this folder to a staging directory on the server, separate from `/opt/nns-tipbot`. From inside the uploaded `nns-tipbot` folder:

```bash
sudo cp nns_tipbot.py nns_watcher.py nns_tilegame.py nns_territory_game.py nns_dungeon_game.py update_nns_apr_cache.py dump_dungeon_db.py dump_tipbot_db.py requirements.txt .env.example /opt/nns-tipbot/
sudo cp README.md /opt/nns-tipbot/
sudo cp -R systemd cron config tests /opt/nns-tipbot/
sudo chown -R nns-tipbot:nns-tipbot /opt/nns-tipbot
sudo -u nns-tipbot python3 -m venv /opt/nns-tipbot/.venv
sudo -u nns-tipbot /opt/nns-tipbot/.venv/bin/python -m pip install --upgrade pip
sudo -u nns-tipbot /opt/nns-tipbot/.venv/bin/python -m pip install -r /opt/nns-tipbot/requirements.txt
sudo -u nns-tipbot /opt/nns-tipbot/.venv/bin/python -m pip check
```

Dependency versions are bounded by major version, not locked to a tested deployment. After validating your deployment, record the installed versions with `python -m pip freeze` using the virtual environment interpreter.

## 3. Configure the environment

On first installation only:

```bash
sudo -u nns-tipbot cp /opt/nns-tipbot/.env.example /opt/nns-tipbot/.env
sudo chmod 600 /opt/nns-tipbot/.env
sudo nano /opt/nns-tipbot/.env
```

Fill in `NNS_TIPBOT_DISCORD_TOKEN` and verify the shared RPC credentials and `NNS_RPC_URL`. The template contains RPC credential placeholders, not usable credentials. Replace them with the same values configured in `996coin.conf`. The bot, watcher, and APR updater have empty RPC credential defaults; configure the shared `.env` explicitly. An explicitly empty value overrides a default.

The scripts have different fallback RPC ports: the bot uses `19996`, the watcher uses `48931`. Set `NNS_RPC_URL` to the actual wallet endpoint so both processes use the same wallet. The template uses an example endpoint on port `19996`; verify it before starting. Put values containing spaces or `#` in quotes. Avoid unintended `${NAME}` expressions: python-dotenv supports environment-variable interpolation. Do not commit the real `.env` file.

**Configure Discord IDs for your own deployment before enabling the bots.** Fill in your own guild, channel, allowed-role, donation-bot, blocked-recipient, and dungeon-treasury IDs where needed. Claim and chest channel defaults are `0` (unrestricted), and the blocked-recipient list is empty (no blocked users). Tile/Territory channel restrictions use an empty value for unrestricted access, not `0`; follow each setting's comments. IDs must belong to your server/accounts; tokens are configured separately. Optionally set `NNS_TIPBOT_GUILD_ID` for command synchronization to one server. Leave it empty for global synchronization. Role restrictions are optional.

The template includes four token fields. Fill a distinct application token for each Discord bot you enable; leave unused game services disabled. Feature defaults still enable claims, chests, and withdrawals; Discord ID defaults are neutral. Review these settings before starting. Boolean flags use `1` and `0`; monetary amounts are in NNS. Do not leave numeric settings blank. All environment settings have English comments in `.env.example`.

Keep `NNS_STAKING_ENABLED=0` unless staking is configured. When enabled, the bot can read a JSON file such as `{"apr_percent": "5.0"}` at `NNS_STAKING_APR_CACHE_FILE`. That example represents a 5% annual rate. If the file is unavailable, the current rate is retained, initially the fallback. The APR updater uses the block-time and APR-factor settings to calculate the cache value; the bot reads the resulting APR without applying the factor again.

Relative database and cache paths resolve from the working directory. The service below uses `/opt/nns-tipbot`. The service user needs write access to the database directory for SQLite WAL files. Existing process environment variables take precedence over values loaded from `.env`.

## 4. Test in the foreground

```bash
cd /opt/nns-tipbot
sudo -u nns-tipbot .venv/bin/python -u nns_tipbot.py
```

Check for a command synchronization message and confirm the bot's slash commands appear in Discord. Test `/help` and `/balances`; test `/deposit` after configuring RPC. Stop the foreground process with Ctrl+C before starting the service. Creating a deposit address alone does not verify deposit crediting or withdrawal processing. After the bot has initialized its tables, start the watcher in a second terminal:

```bash
cd /opt/nns-tipbot
sudo -u nns-tipbot .venv/bin/python -u nns_watcher.py
```

Check its startup database and RPC endpoint. Stop both foreground processes before enabling their services. A fresh watcher scans from a recent block (default lookback: 1000 blocks), rather than scanning the complete wallet history.

For a regular login user running their own copy, the equivalent virtual environment workflow is:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python nns_tipbot.py
# After stopping the bot:
deactivate
```

## 5. Run continuously with systemd

Install the five provided application service files (this does not enable them):

```bash
sudo install -m 0644 /opt/nns-tipbot/systemd/nns-*.service /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/nns-*.service
```

The files match the unit definitions below. If using another account or directory, update every application unit before installation. Enable only services whose tokens/configuration are ready. The node unit is installed separately in the node section.

Create `/etc/systemd/system/nns-tipbot.service` only if entering the provided unit manually:

```ini
[Unit]
Description=NNS Discord Tipbot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=nns-tipbot
Group=nns-tipbot
WorkingDirectory=/opt/nns-tipbot
ExecStart=/opt/nns-tipbot/.venv/bin/python -u /opt/nns-tipbot/nns_tipbot.py
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=multi-user.target
```

The program loads `.env` itself. The service uses the virtual environment interpreter directly, so no activation command is needed.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nns-tipbot
sudo systemctl status nns-tipbot
sudo journalctl -u nns-tipbot -f
```

The provided `/etc/systemd/system/nns-watcher.service` is used after the initial bot startup has successfully initialized the database:

```ini
[Unit]
Description=NNS Deposit and Withdrawal Watcher
Wants=network-online.target nns-tipbot.service
After=network-online.target nns-tipbot.service

[Service]
Type=simple
User=nns-tipbot
Group=nns-tipbot
WorkingDirectory=/opt/nns-tipbot
ExecStart=/opt/nns-tipbot/.venv/bin/python -u /opt/nns-tipbot/nns_watcher.py
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=multi-user.target
```

`After=` orders process startup; it does not wait for the bot's database initialization. Complete the foreground initialization step first.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nns-watcher
sudo systemctl status nns-watcher
sudo journalctl -u nns-watcher -f
```

For the two core services, after changing shared `.env` settings or updating Python files:

```bash
sudo systemctl restart nns-tipbot nns-watcher
```

Also restart each enabled game service when shared configuration changes. Cron reads the new `.env` on its next run.

To stop both processes:

```bash
sudo systemctl stop nns-tipbot nns-watcher
```

## Troubleshooting and maintenance

- **Missing token or login failure:** check the token in `/opt/nns-tipbot/.env` and restart.
- **Commands missing:** check the configured guild ID, installation scopes, and synchronization logs. Global commands may take time to appear.
- **Channel or role rejection:** review the IDs and role names in `.env`.
- **RPC connection failure:** check the wallet process, endpoint, credentials, and connectivity from the server. The configured default points to localhost.
- **Withdrawals stay pending:** check `nns-watcher` logs, wallet connectivity, and retry/backoff state; restarting the Discord bot alone will not broadcast them.
- **Permission errors:** ensure `nns-tipbot` owns the working directory and can read `.env`.
- **Import errors:** install requirements with `/opt/nns-tipbot/.venv/bin/python -m pip`, and confirm the service uses that same interpreter.

Back up the SQLite database before upgrades. For a file-copy backup, stop the bot and every worker that writes to the database, and preserve any existing `-wal` and `-shm` files together with the database. Back up both `TIPBOT_DB` and `DUNGEON_DB` if Dungeon is enabled. Keep `.env` backups private. Start the services again after the backup.

## Watcher defaults and overrides

The watcher defaults are proven starting values from a working deployment; adjust them only when node performance, workload, or operational requirements differ.

The explorer API key and URL, timeouts, retry settings, and polling/batch defaults are built into `nns_watcher.py`. RPC credentials have been removed from source defaults and must be configured in `.env`. Optional overrides are documented as commented entries in `.env.example`. Uncomment only the settings you want to override. No additional Python packages are needed for the watcher.

- Main-loop sleep: 300 seconds; RPC timeout: 90 seconds; RPC retries: 0; retry delay: 1.5 seconds.
- Deposit confirmation refresh batch: 2; withdrawal batch: 1; withdrawal sleep: 30 seconds; confirmation request sleep: 2 seconds.
- Explorer rebroadcast timeout: 20 seconds. The configured URL is a plain URL without Markdown formatting.
- `DEPOSIT_REFRESH_BATCH` and `WITHDRAW_BATCH` are accepted as aliases. The `NNS_DEPOSIT_REFRESH_BATCH` and `NNS_WITHDRAW_BATCH` keys take precedence when both forms are set.
- The withdrawal query honors the configured batch size, with a minimum of one; the startup log reports that effective value.

The main-loop sleep adds to processing time. A confirmation refresh interval of 60 seconds does not produce one-minute refreshes when the loop sleeps for 300 seconds. The watcher prevents automatic retries of ambiguous mutating RPC calls.

`NNS_WITHDRAW_ENABLED` controls new requests in the Discord bot; it does not stop the watcher from processing withdrawals already queued. The watcher retains withdrawal fees in the hot wallet rather than transferring them to `NNS_WITHDRAW_FEE_ADDRESS`.

## Tile Game setup and operation

`nns_tilegame.py` runs as a separate Discord bot and shares `TIPBOT_DB` with the tipbot and watcher. Keep all Python files in `/opt/nns-tipbot`: the game imports `NNS_LEVEL_XP_THRESHOLDS` from `nns_tipbot.py`. This import loads the tipbot's configuration but does not start its Discord connection. The existing `requirements.txt` already provides all required third-party packages.

For an existing installation, copy `nns_tilegame.py` into `/opt/nns-tipbot`, set its owner to `nns-tipbot:nns-tipbot`, and merge the Tile Game section of `.env.example` into your existing `.env`. Do not replace your populated `.env` with the template.

### Discord and database configuration

Create and install a separate Discord application for the Tile Game, using the same installation scopes and channel permissions described above. Set `NNS_TILE_GAME_DISCORD_TOKEN` to that application's token. Do not reuse `NNS_TIPBOT_DISCORD_TOKEN`: both scripts synchronize their own command trees, so sharing an application can replace the other component's commands.

Optionally set `NNS_TILE_GAME_GUILD_ID` for guild-specific synchronization. Set `NNS_TILE_GAME_ALLOWED_CHANNEL_ID` to the intended game channel, or leave it empty to allow all channels. Unlike the tipbot's numeric channel settings, `0` does not disable this restriction. The tile game does not apply the tipbot's allowed-role restrictions.

Start the tipbot successfully at least once before using the Tile Game. The game does not create its database tables; it uses the existing users, transaction log, and XP/profile tables. Use the same database path and working directory for all components.

### Settings and game state

All 21 Tile Game environment variables are documented in `.env.example`. Defaults provide 25 tiles, a 300-second inactivity timeout, and a four-hour cooldown based on the latest tile transaction log entry. Public balance and estimated-return displays are disabled by default. Gold rewards use unstaked balances; Zonk losses affect those same internal balances.

`NNS_TILE_ZONK_LOSS_MIN` and `NNS_TILE_ZONK_LOSS_MAX` are optional. Each omitted bound inherits `NNS_TILE_ZONK_LOSS`, which defaults to 20 NNS. Keep those override lines commented if you want changing the base loss to affect both bounds.

Tile weights are relative random-selection weights, not guaranteed outcome percentages. Board adjustments and first-click protection also affect outcomes. Use nonnegative weights with at least one positive weight, nonnegative reward/loss settings, positive timeouts, and an XP minimum no greater than its maximum.

Active boards and views are stored in memory and do not resume after a process restart. Completed balance/XP changes and logged cooldown events remain in SQLite. Run one Tile Game instance and avoid restarting it during active games when possible.

### Foreground check

```bash
cd /opt/nns-tipbot
sudo -u nns-tipbot .venv/bin/python -u nns_tilegame.py
```

Confirm the command synchronization message. In Discord, test `/tile_rules` in the allowed channel. `/tile_game` starts a game, and `/tile_game_end` ends your active game while preserving the normal cooldown. Playing can change real internal NNS balances and XP. Stop the foreground process with Ctrl+C before enabling its service.

### Tile Game systemd service

Create `/etc/systemd/system/nns-tilegame.service`:

```ini
[Unit]
Description=NNS Discord Tile Game
Wants=network-online.target nns-tipbot.service
After=network-online.target nns-tipbot.service

[Service]
Type=simple
User=nns-tipbot
Group=nns-tipbot
WorkingDirectory=/opt/nns-tipbot
ExecStart=/opt/nns-tipbot/.venv/bin/python -u /opt/nns-tipbot/nns_tilegame.py
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=multi-user.target
```

Complete the tipbot's first database initialization before starting this service; `After=` does not wait for schema creation.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nns-tilegame
sudo systemctl status nns-tilegame
sudo journalctl -u nns-tilegame -f
```

After changing Tile Game settings, run `sudo systemctl restart nns-tilegame`. After changing shared database settings, restart all installed components. Stop every installed service before an offline database backup (include both Territory Game and Dungeon if enabled):

```bash
sudo systemctl stop nns-tipbot nns-watcher nns-tilegame nns-territory-game nns-dungeon-game
# Back up both tipbot.db and dungeon.db with any existing WAL/SHM files.
sudo systemctl start nns-tipbot nns-watcher nns-tilegame nns-territory-game nns-dungeon-game
```

If the game reports a missing table, verify that the tipbot initialized the same database. For missing commands, check the Tile Game token, guild ID, application installation, and service logs. For channel rejection, verify its channel setting is either a valid channel ID or empty.

## Territory Game setup and operation

`nns_territory_game.py` runs as a separate Discord bot against the same `TIPBOT_DB`. Keep it beside `nns_tipbot.py`: it imports `NNS_LEVEL_CLAIM_MULTIPLIERS` for level-based win rewards. The existing virtual environment and `requirements.txt` provide all required packages. No additional dependency is needed.

For an existing installation, copy `nns_territory_game.py` into `/opt/nns-tipbot`, set its owner to `nns-tipbot:nns-tipbot`, and merge the Territory Game section of `.env.example` into the existing `.env`. Preserve your populated configuration.

### Token, channel, and database setup

Create and install a separate Discord application using the scopes and permissions described above. Set `NNS_TERRITORY_GAME_DISCORD_TOKEN` to its own token before starting the service. The code falls back to `NNS_TILE_GAME_DISCORD_TOKEN` when the territory token is absent **or empty**. Do not rely on that fallback while running both games: each process synchronizes its own command tree, which can replace the other game's commands when they share a Discord application.

Guild and channel fallbacks behave differently: `NNS_TERRITORY_GAME_GUILD_ID` and `NNS_TERRITORY_GAME_ALLOWED_CHANNEL_ID` inherit the matching Tile Game setting only when the territory key is absent. The template explicitly includes empty values, so its defaults are global command synchronization and no channel restriction. Use a valid channel ID to restrict play; `0` does not mean unrestricted. The Territory Game does not enforce the tipbot's role restrictions.

Initialize the shared database by starting the tipbot successfully before using this game. Territory Game does not create its own schema; it reads/writes existing users, profiles, and transaction logs. Use the same database path and working directory as the other components.

### Settings and state

The template documents all eight Territory Game settings. Defaults are a 5×5 board, five coins, a 99.6 NNS base win reward, a 30-second cooldown, and a 300-second inactivity timeout. Board size is clamped to 3–5 and coin count to 1–8. Use a nonnegative reward and cooldown, and a positive timeout.

A win credits the base reward multiplied by the player's level claim multiplier to their internal NNS balance. AI wins and draws award no NNS. The cooldown reads the latest `territory_%_nns` transaction-log timestamp; manual game ending also writes an event. Active boards and views live in memory and are lost on restart; persisted rewards and transaction-log entries remain. Run one Territory Game instance per deployment.

### Foreground check and commands

```bash
cd /opt/nns-tipbot
sudo -u nns-tipbot .venv/bin/python -u nns_territory_game.py
```

Confirm the command synchronization message and test `/territory_rules` in the configured channel. `/territory_game` starts a game against the AI. `/territory_game_end` ends the current game and applies the normal cooldown. Playing can credit real internal NNS rewards. Stop the foreground process with Ctrl+C before enabling the service.

### Territory Game systemd service

Create `/etc/systemd/system/nns-territory-game.service`:

```ini
[Unit]
Description=NNS Discord Territory Game
Wants=network-online.target nns-tipbot.service
After=network-online.target nns-tipbot.service

[Service]
Type=simple
User=nns-tipbot
Group=nns-tipbot
WorkingDirectory=/opt/nns-tipbot
ExecStart=/opt/nns-tipbot/.venv/bin/python -u /opt/nns-tipbot/nns_territory_game.py
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=multi-user.target
```

Complete the initial tipbot database initialization first; service ordering does not wait for schema creation.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nns-territory-game
sudo systemctl status nns-territory-game
sudo journalctl -u nns-territory-game -f
```

After territory-specific settings change, run `sudo systemctl restart nns-territory-game`. Include this service when restarting all components after shared configuration changes, and stop it along with all other database writers for offline backups. Avoid restarts during active games when possible.

For missing tables, verify that `TIPBOT_DB` points to the database initialized by the tipbot. For missing/replaced commands, check that the territory token is populated and belongs to a separate application. For channel rejection, check both the territory setting and any inherited Tile Game setting.

## Staking APR cache updater

`update_nns_apr_cache.py` is a one-shot program. It queries `getmininginfo` and `getblockchaininfo`, calculates the annualized reward rate, applies `NNS_STAKING_APR_FACTOR`, and writes the cache. It creates the destination directory and file if missing and replaces an existing cache atomically using a temporary file in the same directory. It does not schedule itself.

The updater requires `requests`, included in `requirements.txt`. For an existing installation, copy the script into `/opt/nns-tipbot`, set its owner to `nns-tipbot:nns-tipbot`, and install the updated requirements in the existing virtual environment:

```bash
sudo -u nns-tipbot /opt/nns-tipbot/.venv/bin/python -m pip install -r /opt/nns-tipbot/requirements.txt
```

### Configuration and first run

The updater uses the shared `NNS_RPC_USER`, `NNS_RPC_PASSWORD`, `NNS_RPC_URL`, `NNS_STAKING_BLOCK_TIME_SECONDS`, `NNS_STAKING_APR_FACTOR`, and `NNS_STAKING_APR_CACHE_FILE` settings. Its only additional setting is `NNS_HTTP_TIMEOUT` (default: 20 seconds per request).

Set `NNS_RPC_URL` to a complete URL with scheme and port, such as the verified wallet endpoint in your shared `.env`. The updater's fallback `127.0.0.1` is not a valid Requests URL. `NNS_RPC_PORT` is commented out in the source and is not used. The updater needs read access to `.env` and write access to the cache directory.

```bash
cd /opt/nns-tipbot
sudo -u nns-tipbot .venv/bin/python update_nns_apr_cache.py
cat nns_staking_apr.json
```

Use the configured path for the last command if you changed the cache location. Run this once successfully before enabling staking to provide an initial rate.

The calculation is:

```text
block_reward_coins = blockvalue / 100000000
calculated_apr_percent = block_reward_coins * (31536000 / block_time_seconds) / moneysupply * 100
apr_percent = clamp(calculated_apr_percent * apr_factor, 0, 1000)
```

The script assumes the node returns `blockvalue` in satoshis and `moneysupply` in coins.

### Cron: twice per hour

The cron template runs at minutes **7 and 37 of every hour**, using the server's cron timezone. The generic configuration below uses this guide's `/opt/nns-tipbot` installation, an explicit log path, and a lock file inside the service-owned project directory.

For Debian/Ubuntu, install and enable cron and the command-line utilities if needed:

```bash
sudo apt install -y cron util-linux coreutils
sudo systemctl enable --now cron
sudo crontab -u nns-tipbot -e
```

Merge `cron/nns-apr-cache.crontab` into that user's existing crontab, preserving unrelated jobs. It sets `SHELL=/bin/sh` explicitly for the service account whose login shell is `nologin`. This is a user crontab template, not an `/etc/cron.d` file (which would require a username column). Its scheduled command is:

```cron
7,37 * * * * /usr/bin/flock -n /opt/nns-tipbot/update_nns_apr_cache.lock -c 'cd /opt/nns-tipbot && /usr/bin/timeout 120 /opt/nns-tipbot/.venv/bin/python /opt/nns-tipbot/update_nns_apr_cache.py >> /opt/nns-tipbot/nns_apr_cache.log 2>&1'
```

The lock prevents overlapping invocations using the same lock file. The outer timeout normally terminates the Python process after 120 seconds; it is not a guaranteed hard-kill deadline if a process ignores SIGTERM. Both standard output and errors from the wrapped command are appended to the log. No virtual environment activation is needed. Use a single scheduled job: when migrating, remove or disable the old job in the original user's crontab. For an existing deployment at another path, use that installation path consistently instead of mixing it with `/opt/nns-tipbot`.

```bash
sudo crontab -u nns-tipbot -l
sudo tail -n 50 /opt/nns-tipbot/nns_apr_cache.log
```

Arrange log rotation if retaining this append-only log long term. The cron configuration above is documentation only; it has not been installed on your server.

### Cache failures and refresh timing

If an RPC request or calculation fails before replacement, the previous cache remains in place and the updater exits with status 1. A failed first run leaves no cache. Check the log for missing credentials, URL errors, node RPC failures, and filesystem permissions.

With staking enabled, the bot reads the cache on startup and periodically afterward according to `NNS_STAKING_APR_REFRESH_SECONDS` (default: 1800 seconds; minimum: 300). This schedule is independent of cron, so a newly written APR may not be applied immediately. The bot reads `apr_percent` but does not validate `updated_at` or expire stale values. If the updater stops running, the last cached APR can continue to be used.

## Dungeon Game setup and operation

`nns_dungeon_game.py` runs as a separate Discord bot. The existing virtual environment requirements cover its dependencies.

For an existing installation, copy the file to `/opt/nns-tipbot`, set its owner to `nns-tipbot:nns-tipbot`, and merge the Dungeon section of `.env.example` into your populated `.env`.

### Discord, databases, and treasury

Set `NNS_DUNGEON_BOT_DISCORD_TOKEN` to a separate Discord application's token and install that application with the scopes and permissions described above. There is no token fallback. Optionally set `NNS_DUNGEON_BOT_GUILD_ID` for guild-specific synchronization. `NNS_DUNGEON_ALLOWED_CHANNEL_ID` restricts `/dungeon_start` only; use `0` or an empty value for unrestricted starts. Other slash commands do not apply this channel check. The tipbot's role restrictions are not applied by this game.

The bot uses two databases:

- `DUNGEON_DB` (default `dungeon.db`) stores runs, upgrades, statistics, and shop logs. The dungeon bot creates and migrates these tables on startup.
- `TIPBOT_DB` is the shared tipbot database for internal balances and XP. The dungeon bot initializes its profile/XP bridge tables, but does not create all required tipbot tables. Start the tipbot successfully first to initialize users and transaction logs.

Keep both database paths consistent across restarts, and grant the service user write access to their directories for SQLite WAL files. Back up **both databases** and any existing WAL/SHM files with all database-writing services stopped.

Set `NNS_DUNGEON_TREASURY_ID` to the Discord account ID whose internal tipbot balance funds diamond payouts. This is an account ID, not a wallet address. Fund that account before enabling normal play with diamonds. Payouts fail if the treasury is unconfigured, underfunded, or the same account as the player. Purchases credit the configured treasury when it is a different account; without such a treasury, the player's balance is still debited but no treasury account is credited.

### Configuration and persisted state

All 18 new settings are described in `.env.example`; `TIPBOT_DB` is reused. Defaults include an 11×11 maze, a 30-minute view inactivity timeout, 400 ms manual-move cooldown, and 300 ms fast-forward steps. Maze dimensions have a minimum of seven and are normalized to odd numbers. Keep dimensions modest for Discord message rendering.

Vision upgrades cost `DUNGEON_VISION_COST_BASE` (default 100 NNS) multiplied by the next bonus level. Teleport potions cost 5 NNS by default. Use nonnegative costs, visibility settings, and item counts, and a positive view timeout. `DUNGEON_EXIT_XP` is commented out in the source and is not an active environment setting; the help describes exit XP as the cleared level number.

Runs are persisted in `dungeon.db`. After a timeout or restart, use `/dungeon_start` to reopen an active saved run with fresh controls. `/dungeon_status` displays status but does not recreate the controls. Run one Dungeon bot instance per deployment.

### Foreground check and commands

```bash
cd /opt/nns-tipbot
sudo -u nns-tipbot .venv/bin/python -u nns_dungeon_game.py
```

Check the command synchronization message and test `/dungeon_help`. Available commands are `/dungeon_start`, `/dungeon_status`, `/dungeon_end`, `/dungeon_theme`, and `/dungeon_help`. `/dungeon_end` deletes the current run. Gameplay, purchases, and rewards modify stored progress, XP, and internal NNS balances. Stop the foreground process before enabling its service.

### Dungeon systemd service

Create `/etc/systemd/system/nns-dungeon-game.service`:

```ini
[Unit]
Description=NNS Discord Dungeon Game
Wants=network-online.target nns-tipbot.service
After=network-online.target nns-tipbot.service

[Service]
Type=simple
User=nns-tipbot
Group=nns-tipbot
WorkingDirectory=/opt/nns-tipbot
ExecStart=/opt/nns-tipbot/.venv/bin/python -u /opt/nns-tipbot/nns_dungeon_game.py
Restart=on-failure
RestartSec=10
UMask=0077

[Install]
WantedBy=multi-user.target
```

Complete initial tipbot database setup before starting this service; `After=` does not wait for schema initialization.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nns-dungeon-game
sudo systemctl status nns-dungeon-game
sudo journalctl -u nns-dungeon-game -f
```

After changing dungeon settings, run `sudo systemctl restart nns-dungeon-game`. Include it when restarting all installed components after shared configuration changes or stopping all database writers for backups. For missing tables, check the shared database path and initial tipbot startup. For diamond payment errors, check the treasury configuration and its internal balance. For expired controls, reopen the run with `/dungeon_start`.

## Dungeon database inspection

`dump_dungeon_db.py` is a read-only command-line report tool. It uses only the Python standard library, requires no Discord token, and needs no background service or additional packages.

For an existing installation, copy the script into `/opt/nns-tipbot` and make it readable by the service user. It does **not** load `.env` or read `DUNGEON_DB`/`TIPBOT_DB` from the environment. Pass custom database paths explicitly. Its defaults are `dungeon.db` and `tipbot.db` relative to the current working directory.

```bash
cd /opt/nns-tipbot
sudo -u nns-tipbot .venv/bin/python dump_dungeon_db.py --help
sudo -u nns-tipbot .venv/bin/python dump_dungeon_db.py
sudo -u nns-tipbot .venv/bin/python dump_dungeon_db.py --only-active --ascii
sudo -u nns-tipbot .venv/bin/python dump_dungeon_db.py --dungeon-db /opt/nns-tipbot/dungeon.db --tipbot-db /opt/nns-tipbot/tipbot.db --json
```

| Option | Behavior |
| --- | --- |
| `--dungeon-db PATH` | Required existing dungeon database; defaults to `dungeon.db`. |
| `--tipbot-db PATH` | Optional enrichment database; defaults to `tipbot.db`. A missing file skips balance/XP enrichment. |
| `--json` | Print a JSON report instead of a text table and maps. |
| `--only-active` | Include records with a saved run. This checks presence of a run, not whether its status equals `active`; dead or other stored runs can also appear. |
| `--ascii` | Render maps with ASCII symbols instead of emoji, including maps embedded in JSON. |

The report includes dungeon statistics and saved maps, with balance, XP, level, and deposit-address data from the tipbot database when available. Maps show the full stored layout rather than the player's limited visibility. To save a JSON report, redirect output to a writable file:

```bash
sudo -u nns-tipbot /opt/nns-tipbot/.venv/bin/python /opt/nns-tipbot/dump_dungeon_db.py --dungeon-db /opt/nns-tipbot/dungeon.db --tipbot-db /opt/nns-tipbot/tipbot.db --json > dungeon-report.json
```

The shell running that command creates `dungeon-report.json` in its current directory. Both database connections use SQLite read-only mode. Reports can be generated while services run, but the separate queries and databases do not form a single consistent snapshot. This is a diagnostic report, not a complete restorable database backup. Query errors are silently treated as empty results, so missing fields or empty reports can indicate schema incompatibility rather than absent data.

## NNS support and database overview

`dump_tipbot_db.py` is a read-only NNS diagnostic tool. It reports NNS user balances and deposit addresses, deposits, withdrawals, staking positions, and the watcher's stored block cursor. The report focuses on NNS records and excludes the transaction log. The bots use that log internally; do not remove it from the database.

It requires only Python's standard library. Copy it into `/opt/nns-tipbot` for an existing deployment. It does not load `.env`; pass a custom database location with `--db`. It opens an existing database in SQLite read-only mode, enables query-only access, and uses one read transaction for a consistent report snapshot. A missing database produces an error instead of creating an empty file. Missing optional tables are identified explicitly; query errors fail with a diagnostic and nonzero exit code.

### Common support commands

```bash
cd /opt/nns-tipbot
# General overview (50 rows per section).
sudo -u nns-tipbot .venv/bin/python dump_tipbot_db.py

# All available NNS records for one Discord account; replace the example ID.
sudo -u nns-tipbot .venv/bin/python dump_tipbot_db.py --user-id 123456789012345678 --limit 0

# Uncredited deposits that have not been ignored.
sudo -u nns-tipbot .venv/bin/python dump_tipbot_db.py --deposit-status pending

# Deposits excluded by watcher checks, including their stored reason.
sudo -u nns-tipbot .venv/bin/python dump_tipbot_db.py --deposit-status ignored

# Withdrawals waiting for a retry, with stored error and retry scheduling.
sudo -u nns-tipbot .venv/bin/python dump_tipbot_db.py --withdraw-status retry

# Watcher cursor and update timestamp.
sudo -u nns-tipbot .venv/bin/python dump_tipbot_db.py --watcher

# Structured report with explicit database path.
sudo -u nns-tipbot .venv/bin/python dump_tipbot_db.py --db /opt/nns-tipbot/tipbot.db --json
```

### Options and output

| Option | Behavior |
| --- | --- |
| `--db PATH` | Existing database path; default `tipbot.db` relative to the current directory. |
| `--users` | Only shared user IDs, NNS balance/address fields, and relevant timestamps. Sorted by NNS balance descending when available. |
| `--deposits` | NNS deposit records, including confirmations, credit/ignore flags and reasons. |
| `--withdrawals` | NNS withdrawal records, including fee, transaction ID, status, error, and available retry fields. |
| `--nns-stakes` | NNS principal, accrued reward, fractional-satoshi remainder, and available staking timestamps. |
| `--watcher` | Stored watcher block hash and update timestamp; always global. |
| `--user-id ID` | Filter user-specific sections by Discord account ID. |
| `--deposit-status STATE` | `pending`, `credited`, or `ignored`; automatically includes deposits. |
| `--withdraw-status STATUS` | Exact stored status; automatically includes withdrawals. |
| `--limit N` | Maximum rows per section, default 50; `0` means all; negative values are rejected. |
| `--json` | Machine-readable output preserving raw satoshi amounts and Unix timestamps. |

Section flags can be combined. With no section flags or status filters, all five sections are shown. Status filters select the corresponding section unless other sections are requested explicitly. Totals for liquid NNS, staked principal, and accrued rewards cover all matching users, even when displayed rows are limited. Totals are database accounting figures, not a wallet reconciliation.

Text output uses UTC timestamps and eight-decimal NNS amounts, while `reward_remainder` remains in fractional satoshis. Records are printed as field/value blocks to preserve complete transaction IDs, addresses, and error messages. JSON retains raw fields and includes matching/shown row counts. The derived deposit state is `credited` if credited, otherwise `ignored` if ignored, otherwise `pending`; the original flags remain visible for diagnosis.

The tool reads stored state only. It does not query the wallet or explorer, retry transactions, or prove that the watcher is currently healthy. The watcher timestamp describes its stored cursor update rather than a dedicated heartbeat. This report is not a restorable database backup.

Offline regression checks can be run from the project directory:

```bash
python3 -m unittest discover -s tests -v
```

## Validation and references

The package includes 16 offline tests covering configuration coverage, dependency declarations, service and cron consistency, NNS reports, the consolidation confirmation gate, destination checks, and archive integrity. Run them with the command above. RPC-related tests use simulated responses and do not broadcast transactions.

Requirements installation and module imports were checked in a Python 3.12 virtual environment on macOS. Linux systemd, cron, Discord connectivity, and wallet RPC must be verified on the deployment host using the steps in this guide.

The systemd service lifecycle is documented in [systemd.service](https://www.freedesktop.org/software/systemd/man/256/systemd.service.html).

Installation references: [discord.py installation](https://discordpy.readthedocs.io/en/stable/intro.html) and [python-dotenv environment loading](https://pypi.org/project/python-dotenv/).

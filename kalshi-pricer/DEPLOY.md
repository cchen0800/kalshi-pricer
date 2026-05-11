# Deploy & operations runbook

This runbook covers `kalshi-pricer` (BTC, this repo) and the parallel
`eth-pricer` and `sol-pricer` trees, which share a single VPS and the
same architecture. Read CLAUDE.md first for the broader system overview —
this file is narrowly about *how the code gets onto the VPS and stays running*.

## VPS layout

- Host alias: `personal-vps` (defined in `~/.ssh/config` — see auto-memory).
- User: `chris`. All services run unprivileged under this user.
- Project trees:
  - `~/kalshi-pricer/` — git clone of `cchen0800/kalshi-pricer` on `main`.
  - `~/eth-pricer/` — **not** a git repo. Hand-mirrored from the local
    `eth-pricer/` tree via `scp`/`rsync`. Treat the local directory as the
    source of truth; the VPS copy is a deploy artifact.
  - `~/sol-pricer/` — same as eth-pricer: hand-mirrored via `rsync`.
- Each project has its own `.venv/`, `.env`, `.secrets/`, and SQLite DBs
  (`pricer.db` for selective, `pricer.aggressive.db` for aggressive).
- Reverse proxy: Caddy (admin API on `127.0.0.1:2019`) fronts all dashboards
  under `trade.baggaar.com` with path-based routing:
  - `/` → BTC (port 5051)
  - `/eth/` → ETH (port 5052)
  - `/sol/` → SOL (port 5053)
  The trade.py bots have no listening port — they only make outbound calls.

## Services

Nine systemd `--user` units run the whole system. BTC and ETH definitions
live in `deploy/systemd/` in this repo; SOL definitions live in
`sol-pricer/deploy/systemd/`.

| Unit | Project | Purpose | Telegram listener |
|------|---------|---------|-------------------|
| `kalshi-pricer-selective.service`  | BTC | `trade.py --live --profile selective` | yes (only one allowed) |
| `kalshi-pricer-aggressive.service` | BTC | `trade.py --live --profile aggressive` | no |
| `kalshi-pricer-web.service`        | BTC | `dashboard.py --port 5051 --no-engine` | n/a |
| `eth-pricer-selective.service`     | ETH | `trade.py --live --profile selective` | no |
| `eth-pricer-aggressive.service`    | ETH | `trade.py --live --profile aggressive` | no |
| `eth-pricer-web.service`           | ETH | `dashboard.py --port 5052 --no-engine` | n/a |
| `sol-pricer-selective.service`     | SOL | `trade.py --live --profile selective` | no |
| `sol-pricer-aggressive.service`    | SOL | `trade.py --live --profile aggressive` | no |
| `sol-pricer-web.service`           | SOL | `dashboard.py --port 5053 --no-engine` | n/a |

Only one process can hold Kalshi's Telegram long-poll (else 409 Conflict).
**`kalshi-pricer-selective` is the designated listener** — every other unit
passes `--no-telegram-listen`. If you ever swap which bot owns the listener,
the unit ExecStart lines must be updated in lockstep.

A bundle target `kalshi-bots.target` exists so you can `start`/`stop` all
nine at once.

## Initial install (one-time)

```bash
ssh personal-vps
cd ~/kalshi-pricer
git pull
bash deploy/install_systemd.sh
# If `loginctl show-user $USER` shows Linger=no, also run:
sudo loginctl enable-linger chris
# Then migrate from any legacy screen sessions:
bash deploy/migrate_from_screen.sh
```

Lingering is what lets user services survive logout and start on boot.
The install script enables units but does not start them; migration runs
the start.

## Day-to-day commands (run on VPS)

```bash
# Status of everything
systemctl --user status kalshi-bots.target --no-pager
systemctl --user list-units 'kalshi-pricer-*' 'eth-pricer-*' 'sol-pricer-*'

# Restart one bot (after a config change, etc.)
systemctl --user restart kalshi-pricer-selective.service

# Stop all bots immediately (graceful)
systemctl --user stop kalshi-bots.target
# Or, less graceful but instant: use the kill files
touch ~/kalshi-pricer/.kill           # selective
touch ~/kalshi-pricer/.kill.aggressive

# Live logs
journalctl --user -u kalshi-pricer-selective.service -f
tail -f ~/kalshi-pricer/logs/selective.log

# All unit logs at once
journalctl --user -u 'kalshi-pricer-*' -u 'eth-pricer-*' -u 'sol-pricer-*' -f
```

Note: stdout/stderr is captured both to `journalctl` and appended to
`logs/<bot>.log` in each project root, matching the previous screen-based
convention so any tail-grep automation keeps working.

## Code deploy (after a commit on `main`)

### Kalshi (git-based)

```bash
ssh personal-vps
cd ~/kalshi-pricer
# Stash any local-only modifications (rare — should normally be clean now).
git stash push -u -m "pre-deploy" 2>/dev/null || true
git pull --ff-only
git stash pop 2>/dev/null || true
# Restart the units whose code actually changed:
systemctl --user restart kalshi-pricer-web.service       # template/dashboard.py
systemctl --user restart kalshi-pricer-selective.service # trade.py / engine / executor
systemctl --user restart kalshi-pricer-aggressive.service
```

### ETH / SOL (manual sync)

ETH and SOL live outside git, so deploy by `rsync` of changed files.
From your laptop:

```bash
# Full sync (initial deploy or major changes)
rsync -av --exclude='.venv' --exclude='.env' --exclude='.secrets' \
  --exclude='pricer.db*' --exclude='logs/' \
  eth-pricer/ personal-vps:~/kalshi-pricers/eth-pricer/
rsync -av --exclude='.venv' --exclude='.env' --exclude='.secrets' \
  --exclude='pricer.db*' --exclude='logs/' \
  sol-pricer/ personal-vps:~/kalshi-pricers/sol-pricer/

# Restart the relevant units
ssh personal-vps 'systemctl --user restart eth-pricer-selective.service eth-pricer-aggressive.service eth-pricer-web.service'
ssh personal-vps 'systemctl --user restart sol-pricer-selective.service sol-pricer-aggressive.service sol-pricer-web.service'
```

When in doubt, restart all units for the project — there's no live
state worth preserving across a 5-second bounce.

## Rolling back

`git pull` deploys atomically per file but the running Python processes
hold the old code in memory until restarted. If a deploy goes bad:

```bash
ssh personal-vps
cd ~/kalshi-pricer
git log --oneline -5            # find the last good commit
git reset --hard <good-sha>     # only safe because we own the deploy clone
systemctl --user restart kalshi-bots.target
```

For ETH/SOL, keep the previous version locally and `rsync` it back.

## Adding a new bot or unit

1. Add `<service-name>.service` to `deploy/systemd/`.
2. Add it to `kalshi-bots.target`'s `Wants=` list.
3. Add the screen-name → unit pair to `deploy/migrate_from_screen.sh`
   *if* it has a legacy screen counterpart.
4. Add a row to the table at the top of this file.
5. Commit, `git pull` on VPS, run `deploy/install_systemd.sh`,
   then `systemctl --user start <new-unit>`.

## CI/CD — current stance

We deliberately do **not** auto-deploy on push to main. Reasoning:

- Single VPS, single operator. Webhook complexity > value.
- These bots hold real money in open positions. An auto-deploy that
  breaks during a market window is more expensive than an extra `ssh`.
- All deploys are now ≤3 commands (pull, restart, verify).

If we ever do want automation, the right shape is:

1. **GitHub Actions on PR**: run `pytest` + import smoke check. (Not yet
   set up — worth a small investment when the test suite matures.)
2. **Manual deploy command** on the VPS, not push-triggered. Could be a
   single `~/deploy.sh main` that does `git pull && systemctl --user
   restart kalshi-bots.target`. Triggered by SSH or a Telegram `/deploy`
   command — never by an external webhook.

Don't reach for full GitOps / Argo / Ansible here. The system is three
projects on one box. Keep the deploy primitives boring.

## Troubleshooting

**A unit refuses to start.** `journalctl --user -u <unit> -n 100`. Common
causes: missing `.env` keys (KALSHI / TELEGRAM / SCREENER), expired
Kalshi key, dashboard port already taken (left-over screen session —
`screen -ls` and `screen -S <name> -X quit`).

**Telegram says 409 Conflict.** Two processes are trying to long-poll the
same bot. Verify only `kalshi-pricer-selective` runs without
`--no-telegram-listen`. If you flipped which bot owns the listener but
forgot to add the flag elsewhere, you'll see this.

**The dashboard tile shows the old layout after a deploy.** You restarted
the trader but not the web service. The template is read once at startup.
`systemctl --user restart kalshi-pricer-web.service eth-pricer-web.service sol-pricer-web.service`.

**Lingering keeps reverting to no.** Some VPS providers reset it on image
rebuild. Re-run `sudo loginctl enable-linger chris` and verify with
`loginctl show-user chris | grep Linger`.

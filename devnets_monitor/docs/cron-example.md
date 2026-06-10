# Scheduling periodic data collection

Run `dv collect <devnet> all` on a schedule to keep the SQLite store current.
The working directory **must** be the repo root so that `config/` and `data/`
resolve correctly.

## crontab (every 15 minutes)

```cron
*/15 * * * * cd /path/to/ethrex-devnets && uv run dv collect glamsterdam-devnet-5 all >> /tmp/dv-collect.log 2>&1
```

Replace `/path/to/ethrex-devnets` with the absolute path to this repo.

## systemd user timer

Create `~/.config/systemd/user/dv-collect.service`:

```ini
[Unit]
Description=ethrex-devnets data collection

[Service]
Type=oneshot
WorkingDirectory=/path/to/ethrex-devnets
ExecStart=uv run dv collect glamsterdam-devnet-5 all
StandardOutput=journal
StandardError=journal
```

Create `~/.config/systemd/user/dv-collect.timer`:

```ini
[Unit]
Description=Run ethrex-devnets collection every 15 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
Persistent=true

[Install]
WantedBy=timers.target
```

Enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now dv-collect.timer
systemctl --user status dv-collect.timer
```

Check recent runs:

```bash
journalctl --user -u dv-collect.service -n 50
```

## Notes

- The timer uses `Persistent=true` so a missed run (e.g. laptop was suspended)
  executes once on the next boot or wakeup.
- SSH connectivity to devnet nodes is required for the `health` sub-collector.
  The other collectors (`forks`, `blobs`, `hive`) use only HTTP and will succeed
  without SSH access.
- To collect only blobs (no SSH), use: `uv run dv collect <devnet> blobs`.

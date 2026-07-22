# systemd Units — Deployment Layout

## How units are installed

All units live in `deploy/` and are **symlinked** into `/etc/systemd/system/`:

```bash
sudo ln -s /opt/berries/deploy/berries-dream.service /etc/systemd/system/berries-dream.service
sudo systemctl daemon-reload
```

Current state (as of 2026-07-15) — all six are symlinks:

| Unit | Linked since |
|---|---|
| `berries-ingest.service` | 2026-04-15 |
| `berries-discord.service` | 2026-04-15 |
| `berries-embed.service` | 2026-05-08 |
| `chroma-server.service` | 2026-05-08 |
| `berries-dream.service` | 2026-07-15 |
| `berries-dream.timer` | 2026-07-15 |

The point is that `deploy/` is the single source of truth: edit the file, `daemon-reload`,
done. No install step to forget, and git tracks what is actually deployed.

`berries-bot.service` is a leftover regular file from March and is not part of this scheme.

## Why this matters — the dream drift

`berries-dream.service` was hand-*copied* into `/etc/` on 2026-05-08 rather than symlinked,
hours before the symlink convention was applied to `chroma-server` and `berries-embed` that
same evening. Commit `bd9a021` (2026-05-18) then rewrote `deploy/berries-dream.service` to add
sandboxing, `EnvironmentFile`, and — per `chromadb-recovery.md` Phase 2 step 3 — the
`Requires=chroma-server.service berries-embed.service` ordering dependency.

None of it reached `/etc/`. Dream ran the stale May 8 definition for two months with no
dependency on chroma-server, which is exactly the hazard step 3 was written to close.
It only worked at all because `shared/config.py` calls `load_dotenv()`, which finds `.env`
via `WorkingDirectory` regardless of `EnvironmentFile`.

If you add a unit, symlink it. A copy will go stale silently.

## FOOTGUN: never `disable` or `reenable` a linked unit

A symlink in `/etc/systemd/system` pointing *outside* systemd's search path makes the unit
**linked**. For linked units, `systemctl disable` deletes the symlink itself — not just the
`*.wants/` entry. And `reenable` is `disable` + `enable`, so:

```bash
sudo systemctl reenable berries-dream.timer
# Failed to reenable unit: Unit file berries-dream.timer does not exist.
```

The `disable` half removes `/etc/systemd/system/berries-dream.timer`, then the `enable` half
cannot find the unit it was about to enable. You are left with a missing unit and a removed
`timers.target.wants/` symlink. This happened on 2026-07-15.

- **Use `systemctl enable`** — safe on linked units, only adds the `*.wants/` symlink.
- **Never `disable` / `reenable`** — recreate the symlink and `enable` instead.
- **To turn a service off**, use `systemctl stop` + `systemctl mask`, or remove the
  `*.wants/` symlink by hand.

Recovery is just recreating the link:

```bash
sudo ln -s /opt/berries/deploy/<unit> /etc/systemd/system/<unit>
sudo systemctl daemon-reload && sudo systemctl enable <unit>
```

## Known tradeoff: `deploy/` is group-writable by `berries`

`/opt/berries/deploy` is `drwxrwxr-x berries:berries`. A group-writable *directory* lets the
`berries` user unlink and replace files inside it regardless of the files' own ownership.
Since root reads these units through the symlinks, a compromised `berries` process — and
`discord_bot` parses untrusted Discord/Twitch input — could rewrite its own unit to
`User=root` and wait for the next `daemon-reload` or reboot.

Copies in `/etc/` owned `root:root` do not have this exposure; the symlinks are what create it.
This is accepted for now (it requires `berries` to be compromised first), but if it ever needs
closing, the fix is an install script that copies units as root and a documented `make install`
step — trading the exposure back for the drift risk described above.

## Permissions

### `UMask=0027` (all units)

Every unit sets `UMask=0027`, so files the services create are `-rw-r-----` and directories
`drwxr-x---`, both `berries:berries`. Since human operators are in the `berries` group, this
makes logs and traces readable without `sudo`:

```bash
python scripts/traces.py     # works as your own user
```

This was previously `UMask=0077` (owner-only), which meant `scripts/traces.py` died with
`PermissionError` on `logs/traces/*.jsonl` for anyone but `berries`.

The umask only governs *newly created* files. Existing files need fixing once:

```bash
sudo chmod -R g+rX /opt/berries/logs   # capital X: +x on dirs only, not on .jsonl files
```

To grant someone trace access, add them to the `berries` group. Note traces contain full
prompts and user messages.

### `.env` must be group-readable by `berries`

`/opt/berries/.env` is `-rw-r----- twig:berries`. **The group must be `berries`**, because
`shared/config.py` calls `load_dotenv()` at import — every service reads `.env` as the
`berries` user at runtime, in addition to systemd's `EnvironmentFile=`.

`EnvironmentFile=` is read by root *before* privileges drop, so it succeeds even when
`load_dotenv()` cannot. A unit can look correctly configured and still crash on import.

Setting `chmod 640` while the file was still owned `twig:twig` took down all four services
at once on 2026-07-15: `640` grants read to group *twig*, and `berries` — which is in no
group but its own — fell through to "other" and got nothing. Every service died at import
with `PermissionError: '/opt/berries/.env'`.

```bash
sudo chown twig:berries /opt/berries/.env && sudo chmod 640 /opt/berries/.env
```

Check ownership, not just mode, before touching this file.

## Verifying a change

```bash
# Are units linked, or has one drifted back to a copy?
for s in berries-ingest berries-discord berries-embed chroma-server berries-dream; do
  printf "%-18s %s\n" "$s" "$(systemctl show -p FragmentPath --value $s)"
done

# Did everything survive?
for s in chroma-server berries-embed berries-ingest berries-discord; do
  printf "%-16s %s\n" "$s" "$(systemctl is-active $s)"
done

# Timer scheduled?
systemctl is-enabled berries-dream.timer && systemctl list-timers berries-dream.timer --no-pager
```

Restart order follows the dependency chain: `chroma-server`, `berries-embed`, then
`berries-ingest`, `berries-discord`.

## Open items

- **`UMask=0027` is unproven.** As of 2026-07-15 no trace file has been *created* since the
  restart — the readable ones were fixed by the `chmod` above, not by the umask. Confirm the
  next `logs/traces/YYYY-MM-DD.jsonl` lands `-rw-r-----`, not `-rw-------`.
- **Dream's sandboxing is half-tested.** The 2026-07-15 one-shot exited in under a second with
  no interaction data, skipping Phases 3 and 4 — so it never reached the embedding or
  chroma-upsert path. `MemoryDenyWriteExecute=yes` and `SystemCallFilter=@system-service` have
  not met PyTorch yet (the unit's own comment notes torch calls `@resources` during init).
  Check `journalctl -u berries-dream` after the first night with real data.
- **`Requires=` is strict.** If `chroma-server` is down at 03:00, dream now refuses to start
  rather than failing mid-upsert. Given the corruption history that is the safer default, but
  it turns a chroma outage into a silently skipped night. `Wants=` would preserve ordering
  without the hard gate.

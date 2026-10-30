# Upgrading

## Between 2.x releases

```bash
VER=2.3.0     # the new version
cd /tmp
curl -fLO https://github.com/drake4m/FreeMediaPlexOrg/releases/download/v$VER/freemediaplexorg-$VER.tar.gz
curl -fLO https://github.com/drake4m/FreeMediaPlexOrg/releases/download/v$VER/freemediaplexorg-$VER.tar.gz.sha256
sha256sum -c freemediaplexorg-$VER.tar.gz.sha256
tar -xzf freemediaplexorg-$VER.tar.gz
sudo bash freemediaplexorg-$VER/install.sh
sudo mediaorg check
```

The installer:

- validates your configuration with the new version **before** the program or the units change; if it is
  refused, the installed version keeps running as before and the message names the setting to fix (before
  that, the installer only creates the `mediaorg` account if it is missing and sets the owner and mode of
  its own folders);
- waits for a running job to finish (up to 15 minutes), then swaps the program; the previous version is
  kept in `/opt/mediaorg/app.old`;
- rewrites the installed systemd units from the configuration they were written from (the `--config` of
  `service install`, `/etc/mediaorg/config.toml` by default), keeping whether the timers are enabled;
- never touches your configuration, your media, local drop-ins of the units, or an existing
  `tinymediamanager.service`.

Back to the previous version: `sudo bash /opt/mediaorg/install.sh --rollback`. It swaps the program back and
restores that version's systemd units (enabled timers stay enabled). From 2.2.0 on it first checks that the
previous version can read the configuration, and refuses, changing nothing, when it cannot: set the settings
that version does not know back to their defaults with `sudo mediaorg config unset <key>` (the sections below
name them), then run the rollback again. Read the [CHANGELOG](../CHANGELOG.md) for changes of behaviour before
upgrading.

### 2.2.x to 2.3.0

Nothing to change: `review.replace_duplicates` ([USAGE.md](USAGE.md#the-review-folder)) is off until you set
it. Before rolling back to 2.2.x, unset the new settings you set (`sudo mediaorg config unset
review.replace_duplicates review.replaced_retention_days`); 2.2.x leaves `review/replaced` alone (delete it
by hand).

### 2.1.x to 2.2.0

Nothing to change: `review.delete_duplicates` ([USAGE.md](USAGE.md#the-review-folder)) is off until you set
it. The memory of Freebox tasks (`/var/lib/mediaorg/freebox_seen.json`) starts with the first run. Before
rolling back to 2.1.x, unset it (`sudo mediaorg config unset review.delete_duplicates`).

### 2.0.x to 2.1.0

Nothing to change: the two new settings, `completeness.on_seeding = "stop"`
([FREEBOX.md](FREEBOX.md#stop-seeding-when-a-download-is-filed)) and `naming.tmm_staging`
([TMM.md](TMM.md#library-on-an-smbcifs-share-staging-names)), are off until you set them. Before rolling
back to 2.0.x, unset them (`sudo mediaorg config unset completeness.on_seeding naming.tmm_staging`): 2.0.x
refuses a configuration that has them, and does not recognise folders that still end with `[mediaorg]`
(it would create a second folder for the same title).

## From 1.x

2.0 installs over 1.x in the same places (`/opt/mediaorg`, `/etc/mediaorg`, `/var/lib/mediaorg`) and reads
the 1.x configuration file as it is, with two exceptions.

**`completeness.method` must be written out** for a Freebox: its default is now `"quiescence"` (watched
folder) instead of `"freebox"`. A 1.x configuration that sets `freebox.remote_downloads_dir` without that
line is refused by the installer, and the installed 1.x keeps running. Add the line before upgrading (1.x
has no `config set`):

```bash
sudo grep -n '^method' /etc/mediaorg/config.toml || echo 'missing: add  method = "freebox"  under [completeness]'
sudoedit /etc/mediaorg/config.toml
```

A configuration without `freebox.remote_downloads_dir` could not run in Freebox mode in 1.x; 2.0 runs it as
a watched folder.

**The schedule moves into the configuration.** 1.x schedules were changed with drop-ins (`sudo systemctl
edit mediaorg.timer`). Such a drop-in survives the upgrade and overrides the new `schedule.*` settings, and
`sudo mediaorg check` then warns `drop-in files set the timer schedule`. Before upgrading, note what your
drop-ins set:

```bash
systemctl cat mediaorg.timer mediaorg-tmm-sync.timer    # drop-ins appear under "# /etc/systemd/system/<unit>.d/..."
```

After upgrading, put your values into the configuration (here, as an example, every 10 minutes and the
nightly tMM sync at 04:30), then remove the timers' drop-ins; `systemctl revert` keeps the unit files and
reloads systemd:

```bash
sudo mediaorg config set schedule.run_every_minutes=10   # the interval your drop-in set
sudo mediaorg config set schedule.tmm_sync_time=04:30    # with tMM only: the time of the nightly sync
sudo systemctl revert mediaorg.timer                     # and mediaorg-tmm-sync.timer if it has a drop-in
systemctl list-timers 'mediaorg*'                        # the next runs follow the configuration
```

`schedule.tmm_sync_time` is the local time of the machine (`timedatectl` shows its time zone); an
`OnCalendar=` line may have named another time zone.

What else changes:

| Topic | 1.x | 2.0 |
|---|---|---|
| running commands | `cd /tmp && sudo -u mediaorg mediaorg ...` | `sudo mediaorg ...` (media commands switch to the `mediaorg` account by themselves; the old way still works) |
| command names | `freebox-authorize`, `freebox-status`, `tmm-sync`, `tmm-status` | `freebox authorize`, `freebox status`, `tmm sync`, `tmm status` (the old names still work) |
| changing settings | editing `config.toml` by hand | `sudo mediaorg config set KEY=VALUE` (checked, documented, previous file kept as `.bak`, units rewritten when needed); editing by hand still works, then `sudo mediaorg config validate` and `sudo mediaorg service install` |
| systemd units | static files | written from the configuration by `sudo mediaorg service install`: only the state folder and the folder that holds the media folders are writable, stricter sandbox (`ProtectSystem=strict`); the installer rewrites them during the upgrade |
| schedule | edited with drop-ins | `schedule.run_every_minutes`, `schedule.tmm_sync_time` (local time of the machine) |
| journals (undo) | kept forever | journals of finished runs are deleted after `reports.journal_retention_days` (365); `0` keeps them forever |
| run reports | by age only | also at most `reports.max_reports` (2000) |
| free space | not checked | a run that would move something needs `safety.min_free_mb` (100) free in the state folder |
| secrets | read without a permission check | the tMM API key and the Freebox token are refused when every user can read or change them; `sudo mediaorg check` names the `chmod` to run |
| new folders | created with the service's umask | created with the permissions of the folder they are created in |
| log file | journal only | optionally also a size-rotated file (`logging.file`) |
| tinyMediaManager helper | `/opt/mediaorg/app/tmm/` | `/opt/mediaorg/app/contrib/tmm-docker/` ([TMM.md](TMM.md)) |

**tinyMediaManager unit.** The installer never replaces an existing `tinymediamanager.service`, so a tMM
container set up with 1.x keeps running as before. The 2.0 unit reads all its folders from
`/etc/tmm/tmm.conf` (`TMM_DATA`, `TMM_MOVIES`, `TMM_SERIES`, `TMM_REQUIRE_MOUNT`); add those lines before
installing it over an older unit.

After the upgrade:

```bash
sudo mediaorg check                       # every line [ok]; the units match the configuration
sudo mediaorg config get completeness.method
sudo mediaorg service status
sudo systemctl start mediaorg.service && sudo journalctl -u mediaorg.service -n 30 --no-pager   # one timer run now
```

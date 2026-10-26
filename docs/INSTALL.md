# Installation

- [Prerequisites](#prerequisites)
- [Quick install](#quick-install) (5 minutes, local folders)
- [Full guide](#full-guide): plan the folders, mount a share, install, configure, check, automate
- [Set up your download client](#set-up-your-download-client) (watched-folder mode)
- [Upgrade, roll back, uninstall](#upgrade-roll-back-uninstall)
- [What gets installed where](#what-gets-installed-where)

## Prerequisites

| Requirement | Minimum | Check | Install |
|---|---|---|---|
| Debian, Ubuntu or a derivative (amd64, arm64, armhf) | Debian 12, Ubuntu 22.04 | `cat /etc/os-release` | |
| Python with `venv` | 3.11 | `python3 -V` | `sudo apt install python3 python3-venv` (Ubuntu 22.04: `python3.11 python3.11-venv`) |
| systemd (for the timers) | 247 | `systemctl --version` | present on Debian/Ubuntu |
| util-linux (`flock`, `runuser`) | | `flock -V` | present on Debian/Ubuntu |
| `curl` (to download the release) | | | `sudo apt install curl` |
| Network access during installation | | | github.com, pypi.org and files.pythonhosted.org (dependencies, pinned by version and SHA-256) |
| `sudo` rights | | | |

The program is pure Python and needs about 30 MB. The test suite runs on Python 3.11 to 3.14. On Ubuntu
22.04, `python3.11` comes from the universe component, as a build of a 3.11.0 release candidate; Debian 12
and Ubuntu 24.04 or later ship a final Python 3.11 or newer as `python3`.

**Your folders.** Downloads, the movie library and the TV library must be on **one filesystem** (the same
disk, or the same network share): files are moved with instant renames and are never copied. A layout
with downloads on one disk and the library on another is refused by `config init` and `check`. For the
timers they must also be inside one folder other than `/` (such as `/srv/media` or the mount point of a
share): the systemd sandbox makes that one folder writable, and a move must stay inside it.

## Quick install

```bash
sudo apt install python3 python3-venv curl
VER=2.0.0
cd /tmp
curl -fLO https://github.com/drake4m/FreeMediaPlexOrg/releases/download/v$VER/freemediaplexorg-$VER.tar.gz
curl -fLO https://github.com/drake4m/FreeMediaPlexOrg/releases/download/v$VER/freemediaplexorg-$VER.tar.gz.sha256
sha256sum -c freemediaplexorg-$VER.tar.gz.sha256      # must print: OK
tar -xzf freemediaplexorg-$VER.tar.gz
sudo bash freemediaplexorg-$VER/install.sh

sudo mediaorg config init       # asks for the three folders
sudo mediaorg check
sudo mediaorg run               # dry-run
sudo mediaorg run --apply
sudo mediaorg service install --enable
```

Expected: `== INSTALL OK: mediaorg 2.0.0`, then `configuration written to /etc/mediaorg/config.toml`, then
`[ok]` lines from `check` (warnings are advice), then a report of what would be filed.

## Full guide

### 1. Plan the folders

| Setup | Downloads | Movies | TV |
|---|---|---|---|
| local disk | `/srv/media/downloads` | `/srv/media/movies` | `/srv/media/tv` |
| NAS share mounted on `/mnt/nas` | `/mnt/nas/Downloads` | `/mnt/nas/Movies` | `/mnt/nas/TV` |
| Freebox share mounted on `/mnt/freebox` ([guide](FREEBOX.md)) | `/mnt/freebox/Downloads` | `/mnt/freebox/Movies` | `/mnt/freebox/Series` |

- Create the folders if they do not exist yet. An existing library is reused as it is: folders named like
  `Title (Year)` (movies) and `Show (Year)/Season N` (TV, the layout tinyMediaManager, Plex and Jellyfin
  use) are recognised; new ones are created that way.
- Duplicates and leftovers go to a review folder, by default `<downloads>/_review`.
- mediaorg's own state (journals, reports, lock) lives in `/var/lib/mediaorg` on the local disk.

### 2. Storage access for the `mediaorg` account

mediaorg runs as an unprivileged system account, `mediaorg`, created by the installer. That account must
be able to **read and write** Downloads and both libraries. Install first (step 3), then grant access:

**Local disk** - give a shared group the folders, so your download client and media server keep working:

```bash
sudo groupadd -f media
sudo usermod -aG media mediaorg            # also add your download client's and media server's accounts
sudo chgrp -R media /srv/media
sudo chmod -R g+rwX /srv/media
sudo find /srv/media -type d -exec chmod g+s {} +     # new files and folders keep the group
```

(Or with ACLs: `sudo apt install acl`, then `sudo setfacl -R -m u:mediaorg:rwX -m d:u:mediaorg:rwX
/srv/media`.) New folders that mediaorg creates copy the permissions of the folder they are created in.

**Network share (CIFS/SMB)** - mount it so that the files belong to `mediaorg`:

```bash
sudo apt install cifs-utils
sudo install -m 0600 /dev/null /root/.smbcredentials
sudo nano /root/.smbcredentials            # two lines: username=...  and  password=...
sudo mkdir -p /mnt/nas
sudo chattr +i /mnt/nas                     # the EMPTY mount point can never be written into by mistake
echo '//nas.local/media /mnt/nas cifs credentials=/root/.smbcredentials,uid=mediaorg,gid=mediaorg,file_mode=0664,dir_mode=0775,vers=3.0,soft,echo_interval=30,_netdev,nofail,x-systemd.mount-timeout=30s 0 0' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /mnt/nas && findmnt /mnt/nas
```

- `soft,echo_interval=30`: if the NAS disappears, operations fail after about 90 seconds (three times
  `echo_interval`) instead of hanging, and the run stops cleanly; the next run repairs anything
  interrupted.
- `_netdev,nofail`: the machine still boots when the NAS is off. A timer run needs the mount
  (`RequiresMountsFor=`): while the share cannot be mounted, systemd does not start the run
  (`Dependency failed` in the journal), and the next timer run tries the mount again.
- `file_mode=0664,dir_mode=0775`: your media server (another account) can read everything.
- `config init` records this mount, so mediaorg refuses to run while it is not mounted.

**NFS** - `sudo apt install nfs-common`, then mount the export, for example with this `/etc/fstab` line, and
make sure the server maps the `mediaorg` user (or its group) to an account allowed to write:

```text
nas.local:/export/media  /mnt/nas  nfs4  defaults,_netdev,nofail  0 0
```

### 3. Install

```bash
sudo apt install python3 python3-venv curl
VER=2.0.0
cd /tmp
curl -fLO https://github.com/drake4m/FreeMediaPlexOrg/releases/download/v$VER/freemediaplexorg-$VER.tar.gz
curl -fLO https://github.com/drake4m/FreeMediaPlexOrg/releases/download/v$VER/freemediaplexorg-$VER.tar.gz.sha256
sha256sum -c freemediaplexorg-$VER.tar.gz.sha256
tar -xzf freemediaplexorg-$VER.tar.gz
sudo bash freemediaplexorg-$VER/install.sh
```

The installer is idempotent and safe to run again. It:

1. checks the system (Debian/Ubuntu, Python 3.11+ with venv, systemd). `sudo PYTHON=/usr/bin/python3.12
   bash install.sh` uses exactly that interpreter, and stops if it is not a Python 3.11+ with venv;
2. creates the system account `mediaorg` (no login shell; its home is `/var/lib/mediaorg`),
   `/etc/mediaorg` (0750 root:mediaorg), `/var/lib/mediaorg` and `/var/log/mediaorg` (0750 mediaorg);
3. installs the program in `/opt/mediaorg/app` and its dependencies in a private virtual environment
   (`/opt/mediaorg/venv`, exact versions with SHA-256 hashes from `requirements.lock`), and links
   `/usr/local/bin/mediaorg`;
4. on an upgrade: validates your existing configuration with the new version first (if it is not valid,
   the program and its units are not changed), waits for a running job, keeps the previous version in
   `/opt/mediaorg/app.old`, and rewrites the installed systemd units from the configuration they were
   written from (whether the timers are enabled is kept).

It never creates a configuration, never enables timers and never touches your media.

### 4. Configure

```bash
sudo mediaorg config init
```

In a terminal it asks for what is missing; every answer is checked:

```text
Downloads folder (where completed downloads appear): /srv/media/downloads
Movie library folder: /srv/media/movies
TV library folder: /srv/media/tv
How are finished downloads recognised: 'watch' (files unchanged for a while; any download client) or 'freebox' (the Freebox download manager) [watch]:
configuration written to /etc/mediaorg/config.toml
  ...
  the service account 'mediaorg' can read and write all media folders
next:
  1. sudo mediaorg check
  ...
```

The same without questions (for scripts):

```bash
sudo mediaorg config init --non-interactive --downloads /srv/media/downloads \
     --movies /srv/media/movies --series /srv/media/tv [--quiet-minutes 15] [--every 10]
```

- With a Freebox: `--mode freebox`, then follow [FREEBOX.md](FREEBOX.md).
- With tinyMediaManager: `--tmm`, then follow [TMM.md](TMM.md).

Look at the result and change anything with `config set`:

```bash
sudo mediaorg config show                                  # everything in force, defaults included
sudo mediaorg config set completeness.min_quiet_minutes=15
mediaorg config keys --all                                 # every key with its meaning
```

All keys: [CONFIGURATION.md](CONFIGURATION.md).

### 5. Check, then try

```bash
sudo mediaorg check                       # read-only: folders, permissions, mount, free space, Freebox, tMM
sudo mediaorg probe-rename --apply        # proves on your storage that a move never replaces a file
sudo mediaorg run                         # dry-run: what would be filed, and why
sudo mediaorg run --apply                 # do it once by hand, and look at the result
```

`check` must end without `[FAIL]` (exit code 0). The dry-run report lists every download with a status
(`READY`, `WAIT`, `SKIP`, `IGNORED`; see [USAGE.md](USAGE.md)) and the exact moves. Nothing changes without
`--apply`.

### 6. Automate

```bash
sudo mediaorg service install --enable    # writes the systemd units, enables and starts the timers
sudo mediaorg status                      # timers, next run, latest report
sudo journalctl -u mediaorg.service -f    # follow the runs (Ctrl-C to stop following)
```

From now on mediaorg runs every 5 minutes (`schedule.run_every_minutes`) and after every reboot. A run
that finds nothing to do takes well under a second and logs nothing. When you change a setting that the
units depend on (folders, mount, schedule) with `config set`, the units are rewritten automatically.
`service install` also creates the state and review folders when they are missing, for the `mediaorg`
account: the sandbox lets a run write only into folders that exist when it starts.

## Set up your download client

**With a Freebox**, nothing to do: mediaorg asks the Freebox which torrents are finished and seeded
([FREEBOX.md](FREEBOX.md)).

**Watched-folder mode** (any other client, or files copied by hand): a download counts as finished when
none of its files has changed for `completeness.min_quiet_minutes` (default 10) and none has a temporary
extension (`.part`, `.!qb`, `.crdownload`, `.tmp`, `.partial`, `.!ut`, `.aria2`). So:

1. **Unfinished downloads must not look finished.** Let your client keep incomplete downloads in another
   folder, or give them a temporary extension while they download (most clients have an option to append
   one, such as `.!qB` or `.part`).
2. **Seeding torrents must not be moved away** from under the client. Either stop seeding when a torrent
   completes before it lands in the watched folder, or, better, keep seeding and let the client put a
   **hard link** of the finished download into the watched folder: it costs no space (same filesystem),
   the client keeps its own copy, and mediaorg files the link. Most clients can run a command when a
   torrent completes; for example `cp -al "<content path>" /srv/media/downloads/`, where
   `<content path>` is the placeholder your client documents for the downloaded file or folder.
3. **Copying by hand** works as is: the copy is filed once it has stopped changing for the quiet time.

## Upgrade, roll back, uninstall

```bash
# upgrade: download the new release as above, then
sudo bash freemediaplexorg-<version>/install.sh
sudo mediaorg check

# back to the previous version (kept in /opt/mediaorg/app.old)
sudo bash /opt/mediaorg/install.sh --rollback

# remove the program and its timers; keep configuration, state and logs
sudo bash /opt/mediaorg/install.sh --uninstall
# ... and also remove /etc/mediaorg, /var/lib/mediaorg, /var/log/mediaorg and the account
sudo bash /opt/mediaorg/install.sh --uninstall --purge
```

None of these touch your media. A `paths.state_dir` outside `/var/lib/mediaorg` is left in place by
`--purge`; the installer says so. Upgrading from 1.x: [UPGRADING.md](UPGRADING.md).

### Installing from a Git checkout

For testing a branch (`sudo apt install git`): `git clone https://github.com/drake4m/FreeMediaPlexOrg &&
sudo bash FreeMediaPlexOrg/install.sh`. Releases are the supported way to install.

## What gets installed where

| Path | Content | Owner, mode |
|---|---|---|
| `/opt/mediaorg/app/` | the program (and `app.old/`, the previous version) | root, 0755 |
| `/opt/mediaorg/venv/` | Python virtual environment with the pinned dependencies | root, 0755 |
| `/usr/local/bin/mediaorg` | link to the launcher | root |
| `/etc/mediaorg/config.toml` | configuration (written by `config init` / `config set`; previous version `.bak`) | root:mediaorg, 0640 |
| `/etc/mediaorg/tmm.key` | tinyMediaManager API key (`tmm set-key`) | root:mediaorg, 0640 |
| `/etc/mediaorg/freebox-ca.pem` | Freebox root certificates | root:mediaorg, 0644 |
| `/var/lib/mediaorg/` | journals, run reports, lock, caches, Freebox app token (0600) | mediaorg, 0750 |
| `/var/log/mediaorg/` | optional log file (`logging.file`) | mediaorg, 0750 |
| `/etc/systemd/system/mediaorg*.{service,timer}` | units written by `service install` | root, 0644 |

# With a Freebox

A Freebox Server (Free's ISP box) has its own download manager, which writes into a folder on the
Freebox's storage. mediaorg can work on that storage over the network and ask the Freebox, through the
official Freebox OS API, whether each download is really finished, seeding included. Without a Freebox,
use the watched-folder mode instead ([INSTALL.md](INSTALL.md)).

- [How it fits together](#how-it-fits-together)
- [Requirements](#requirements)
- [1. Mount the Freebox share](#1-mount-the-freebox-share)
- [2. Configure](#2-configure)
- [3. Authorize mediaorg on the Freebox](#3-authorize-mediaorg-on-the-freebox)
- [4. Check, then automate](#4-check-then-automate)
- [When is a download finished?](#when-is-a-download-finished)
- [Freebox restart, internet outage](#freebox-restart-internet-outage)

## How it fits together

```text
Freebox download manager ──writes──> <Freebox disk>/Downloads
                                            │ SMB share, mounted on the Linux machine (/mnt/freebox)
                                            ▼
mediaorg ── HTTPS (Freebox OS API, read-only) ──> "is the task of this download done?"
         ── renames on the share ──> <Freebox disk>/Movies, <Freebox disk>/Series
```

- Downloads, Movies and Series are folders of **one** Freebox share, so filing is an instant rename on the
  Freebox's disk: nothing travels over the network.
- The API is used read-only: mediaorg lists download tasks, their files and the download settings. It
  never adds, stops or deletes a task. The app needs only the download manager permission.
- The connection to the Freebox is HTTPS, checked against Free's root certificates
  (`/etc/mediaorg/freebox-ca.pem`, installed by `install.sh`).

## Requirements

- A Freebox Server with Freebox OS and storage (internal disk, or a disk attached to it).
- File sharing over SMB enabled in Freebox OS, preferably with a user name and password.
- The Linux machine on the Freebox's local network: the API answers at `https://mafreebox.freebox.fr`
  (`freebox.base_url`).
- A BitTorrent seeding **stop ratio above 0** in the download manager's settings. With 0, the Freebox seeds
  until a task is stopped by hand, so downloads would never count as finished. A low ratio files downloads
  sooner; a higher one gives more back to other peers. `mediaorg check` and `mediaorg freebox status` show
  the value.

## 1. Mount the Freebox share

Mount the share so that its files belong to the `mediaorg` account (install mediaorg first, it creates the
account):

```bash
sudo apt install cifs-utils
sudo install -m 0600 /dev/null /root/.freebox-smb
sudo nano /root/.freebox-smb        # two lines: username=...  and  password=...  (the Freebox SMB account)
sudo mkdir -p /mnt/freebox
sudo chattr +i /mnt/freebox          # the EMPTY mount point can never be written into by mistake
echo '//mafreebox.freebox.fr/Freebox /mnt/freebox cifs credentials=/root/.freebox-smb,uid=mediaorg,gid=mediaorg,forceuid,forcegid,file_mode=0664,dir_mode=0775,vers=3.0,soft,echo_interval=30,noserverino,_netdev,nofail,x-systemd.mount-timeout=30s 0 0' | sudo tee -a /etc/fstab
sudo systemctl daemon-reload
sudo mount /mnt/freebox && findmnt /mnt/freebox && ls /mnt/freebox
```

- `//mafreebox.freebox.fr/Freebox`: the share is named after the Freebox disk (`Freebox` on recent models).
  To list the shares: `smbclient -L mafreebox.freebox.fr -U <user>` (package `smbclient`). The Freebox's
  LAN address works in place of the name.
- `vers=3.0` was tested on a recent Freebox Server; an older model may need another SMB version.
- `forceuid,forcegid` with 0664/0775: everything on the share belongs to `mediaorg`; other local accounts (a
  media server) can read.
- `soft,echo_interval=30`: when the Freebox stops answering, file operations fail after at most about 90
  seconds (three times `echo_interval`) instead of hanging; the run stops cleanly.
- `noserverino`: the Linux client numbers the files itself instead of using the server's inode numbers.
- `_netdev,nofail`: the machine boots even when the Freebox is not up yet. A timer run needs the mount: until
  it succeeds, systemd does not start the run, and the next timer run tries the mount again.

Create the three folders on the share if they do not exist (for example `/mnt/freebox/Downloads`,
`/mnt/freebox/Movies`, `/mnt/freebox/Series`), and set the Freebox download manager to download into that
`Downloads` folder.

## 2. Configure

```bash
sudo mediaorg config init --mode freebox --downloads /mnt/freebox/Downloads \
     --movies /mnt/freebox/Movies --series /mnt/freebox/Series
```

`config init` adds the mount check (`[mount]`) by itself, because the folders are on their own mount: no
run starts unless `/mnt/freebox` is mounted from the Freebox share.

## 3. Authorize mediaorg on the Freebox

```bash
sudo mediaorg freebox authorize
```

1. The Freebox asks for confirmation on the Freebox Server itself (its front display): accept the request
   for `mediaorg` within 3 minutes. The app token is saved to `/var/lib/mediaorg/freebox_app_token.json`
   (mode 0600, readable by the `mediaorg` account only).
2. In Freebox OS (Paramètres de la Freebox > Gestion des accès > Applications), leave only the download
   manager permission (*Accès au gestionnaire de téléchargements*) for `mediaorg`. `check` warns about any
   other permission.
3. Tell mediaorg the Freebox's own path of the Downloads folder. `freebox status` prints it, with the exact
   command to set it:

   ```console
   $ sudo mediaorg freebox status
   permissions granted to this app: ['downloader']
   download folder (freebox.remote_downloads_dir): '/Freebox/Downloads'
   BitTorrent stop ratio: 1
   ...
   freebox.remote_downloads_dir is ''; to match the Freebox:
     sudo mediaorg config set freebox.remote_downloads_dir=/Freebox/Downloads
   ```

   When the Freebox downloads somewhere else than `freebox.remote_downloads_dir`, a run that has a download
   to judge stops before acting (exit 3, nothing moved, nothing cleaned): otherwise the Freebox's tasks
   would not map to Downloads, and a torrent still seeding could look like a finished download. The
   folder is compared when the Freebox is asked, so a run with nothing in Downloads does not notice;
   `sudo mediaorg check` always does.

To authorize again (a revoked app, another `freebox.app_id`): delete the token file, then run
`freebox authorize` again.

## 4. Check, then automate

```bash
sudo mediaorg check                       # Freebox reachable over TLS, token, permissions, folder, stop ratio
sudo mediaorg probe-rename --apply        # the no-overwrite guarantee, proven on the Freebox share
sudo mediaorg run                         # dry-run
sudo mediaorg run --apply
sudo mediaorg service install --enable
```

## When is a download finished?

Every task whose download folder is Downloads (or a folder below it) is matched to the entry of Downloads
that holds its files. An entry is finished when **all** tasks writing into it are `done`, or stopped with
100 % received; until then it is `WAIT` with the task's state (`downloading`, `seeding`, `checking` ...).
Files you deselected in a torrent, or that are not fully received, are never moved.

Entries that no Freebox task wrote (files you copied there yourself) follow `completeness.untracked`:
`"wait"` (default) never moves them; `"quiescence"` moves them once they have not changed for
`completeness.min_quiet_minutes`:

```bash
sudo mediaorg config set completeness.untracked=quiescence
```

A run asks the Freebox only when Downloads holds something that needs a verdict, and keeps the file lists
of finished tasks in a small cache (`/var/lib/mediaorg/freebox_files.json`), so a timer every 5 minutes
costs the Freebox next to nothing.

## Freebox restart, internet outage

- **Freebox Server restart** while the Linux machine keeps running: the share stays mounted and the client
  reconnects by itself. Meanwhile a run that starts finds the folders unreadable and stops before acting
  (exit 3, `precondition failed`); a run caught during its moves stops at the first operation that fails
  (exit 3, `NAS stopped answering`), and the next run reconciles its journal. Moves are renames on the
  Freebox's disk, so no half-copied file is left.
- **Internet down, Freebox up**: the share keeps working. The API is reached by name
  (`mafreebox.freebox.fr`); if the machine cannot resolve that name without internet, a run that has
  something to judge stops with `cannot determine download completeness (Freebox), nothing moved` (exit 3)
  and cleans nothing; everything resumes once the name resolves again. Torrents do not progress meanwhile
  anyway.
- **Linux machine boots before the Freebox** (after a power cut): the mount is retried by every timer run
  (`RequiresMountsFor=`); runs are skipped until it succeeds.
- **A file listed on the share that cannot be opened or deleted** (`No such file or directory` although
  `ls` shows it): most likely it was deleted while another device or program held it open, and the Freebox
  keeps the name until the last handle is closed (a pending deletion). It disappears when that handle is
  closed, at the latest when the Freebox Server restarts.
- With tinyMediaManager: a Freebox restart during a tMM library update has a cost ([TMM.md](TMM.md#known-limitations)).

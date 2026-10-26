# Security policy

## Supported versions

Security fixes go into the latest 2.x release. Upgrade with the release's `install.sh`
([docs/UPGRADING.md](docs/UPGRADING.md)).

## Reporting a vulnerability

Please report security problems **privately**, through GitHub's private vulnerability reporting: the
**Security** tab of this repository, then **Report a vulnerability**. Do not open a public issue or pull
request for them.

Include what an attacker needs (local account, network position, a crafted file name ...), what they gain,
the version, and steps to reproduce. You will get an answer within a few days; a fix is released as soon
as it is ready and the advisory credits you unless you prefer otherwise.

## Security model

What mediaorg protects, and how:

- **Least privilege.** Only setup writes as root (`config init|set|unset`, `tmm set-key`,
  `service install|remove`, `install.sh`). Everything that reads or moves media, or talks to the Freebox or
  tinyMediaManager, runs as the unprivileged system account `mediaorg`; a root caller is switched to it
  before the configuration is read.
- **systemd sandbox.** The timer runs have no capabilities, no new privileges, a read-only system
  (`ProtectSystem=strict`), private `/tmp` and devices, a system-call filter, restricted address families,
  and write access only to the configured folders.
- **Secrets.** The tinyMediaManager API key and the Freebox app token are files readable by the `mediaorg`
  account only (0640 root:mediaorg, 0600); they are never accepted as command-line arguments, never logged,
  and never written to reports. A configuration, key or token file that other users could change (or read,
  for secrets) is refused each time it is read; the configuration's `.bak` copy is written as a new file
  that replaces the old one, so a link planted under that name is replaced, never followed.
- **Network.** The Freebox API is reached over HTTPS, checked against Free's root certificates shipped with
  the release. Plain HTTP to a tinyMediaManager on another machine is refused unless explicitly allowed.
- **Untrusted file names.** Download names are data: they are never passed to a shell; a folder name built
  from a release name is cleaned to one safe path component (no `/`, `\`, control characters, `.` or
  `..`); symbolic links in Downloads are never followed, and links are never followed when moving or
  deleting (the review cleaning deletes through folder handles opened without following links, so a
  folder swapped for a link while it runs is not followed); names that are not valid UTF-8 are left
  untouched and printed escaped.
- **Data safety.** Dry-run by default, no overwrite (atomic reservation of every target name), deletions
  limited to the cases listed in [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md#safety-rules) and journaled,
  a mount check against an unmounted share, a cap on the operations of one run.
- **Supply chain.** Runtime dependencies are pinned by version and SHA-256 (`requirements.lock`) and
  installed as wheels only; releases are published with a SHA-256 checksum; the optional tinyMediaManager
  container image is pinned by digest.

Out of scope: an attacker who is already root, or who can write to the configuration, the program or the
media folders as the `mediaorg` account; the security of the download client, the NAS, the Freebox and
tinyMediaManager themselves.

## Recommendations for operators

- Keep the tinyMediaManager web UI on `127.0.0.1` and reach it through SSH; do not forward its port on your
  router. The HTTP API stays on `127.0.0.1`.
- Give the Freebox app only the download manager permission (`mediaorg check` warns otherwise).
- Protect an empty mount point with `chattr +i`, and keep the share credentials in a root-only file.
- Back up `/etc/mediaorg` and `/var/lib/mediaorg`; they hold the key, the token and the undo journals.

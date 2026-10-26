# Contributing

Thank you for helping. This project moves people's media files, so the bar is safety first: a change that
makes mediaorg do more must never make it guess, overwrite or delete without saying so.

## Development setup

Python 3.11 or later; Debian or Ubuntu are the reference platforms.

```bash
git clone https://github.com/drake4m/FreeMediaPlexOrg && cd FreeMediaPlexOrg
python3 -m venv .venv
.venv/bin/pip install --require-hashes --no-deps -r requirements.lock
.venv/bin/pip install ruff==0.16.9
sudo apt install shellcheck
```

## Checks (all run in CI)

```bash
.venv/bin/python -m unittest discover -s tests -t .      # the test suite (no root needed)
.venv/bin/ruff check .                                    # lint
shellcheck -S style install.sh contrib/tmm-docker/setup-tmm.sh tools/build-release.sh
.venv/bin/python tools/gen_docs.py --check                # generated docs up to date (Python 3.13 or later)
```

- Run the tests as a normal user. They work in temporary folders; the parts that would need root, systemd
  or the network are simulated. A few tests are skipped when `systemd-analyze` or `shellcheck` is missing.
- `tools/gen_docs.py` regenerates `config/config.example.toml` and the generated parts of
  `docs/CONFIGURATION.md` and `docs/CLI.md` from the code. Run it after changing a configuration key
  (`mediaorg/config.py`, `mediaorg/schema.py`) or a command's help (`mediaorg/cli.py`). The help text
  differs slightly between Python versions; the committed output is from Python 3.13 or later.

## Rules for changes

- **Safety rules stay true.** [docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md#safety-rules) lists them: dry-run
  by default, no overwrite, explicit and journaled deletions, no guessing, one run at a time. A change that
  touches moves, deletions, identification or completeness comes with tests for the new behaviour and for
  what must still not happen.
- **Test data is invented.** Use made-up titles and release names (`Night Harbor`, `A.Film.2023...`),
  `example.com` / `.example` host names, documentation addresses (`192.0.2.0/24`), and temporary folders.
  Never put real release names, the titles of your own library, host names, addresses, user names or paths
  of your machines in tests, docs or issues.
- **No secrets anywhere**: not in tests, fixtures, logs, examples or screenshots.
- **Few dependencies.** The standard library plus guessit (and its own dependencies). A new runtime
  dependency needs a good reason; it is pinned with hashes in `requirements.lock`.
- **Configuration**: every key is declared in `mediaorg/schema.py` with its documentation, and validated in
  `mediaorg/config.py`. Defaults must be safe.
- **Python 3.11** must keep working (`tomllib`, no newer syntax).
- Style: `ruff` with the settings in `pyproject.toml` (line length 120). Explain the reason in a comment
  where the code alone does not show it.
- Add a line to `CHANGELOG.md` under `Unreleased` for anything a user would notice.

## Pull requests

Keep a pull request to one topic. Describe what changes for a user, how you tested it (on what
distribution, which storage), and what could go wrong. Fill in the checklist of the template.

## Releases (maintainers)

1. Set the version in `mediaorg/__init__.py`, date the `CHANGELOG.md` entry, run `tools/gen_docs.py`.
2. Merge into `main` with CI green.
3. `bash tools/build-release.sh` builds `dist/freemediaplexorg-X.Y.Z.tar.gz` and its `.sha256` from the
   committed tree (`git archive`).
4. Tag `vX.Y.Z` and publish a GitHub release with both files and the changelog entry.

## Updating the pinned dependencies

`requirements.lock` pins exact versions with their SHA-256 hashes; `install.sh` installs them with
`pip --require-hashes --only-binary=:all:`. To update one, take the new version's wheel hash from PyPI
(the "Download files" page of the release, or `pip download --no-deps --only-binary=:all: NAME==VERSION`
then `pip hash`), update the line, and run the full checks on Python 3.11 and the newest Python.

## What changes for a user

<!-- One or two sentences. Link the issue if there is one. -->

## How it was tested

<!-- Distribution, Python version, storage (local disk, CIFS, NFS ...), Freebox or watched folder. -->

## Checklist

- [ ] Tests cover the new behaviour and what must still not happen (`python -m unittest discover -s tests -t .`)
- [ ] `ruff check .` and `shellcheck -S style install.sh contrib/tmm-docker/setup-tmm.sh tools/build-release.sh` pass
- [ ] `python tools/gen_docs.py` run after a change to a configuration key or to a command's help
- [ ] Documentation updated; a line in `CHANGELOG.md` under `Unreleased`
- [ ] Only invented names, `example` hosts and documentation addresses in tests and docs; no secrets
- [ ] The safety rules of `docs/HOW-IT-WORKS.md` still hold (dry-run by default, no overwrite, explicit deletions)

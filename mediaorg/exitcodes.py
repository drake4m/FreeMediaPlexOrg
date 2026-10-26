"""Exit codes of every mediaorg command (documented in `mediaorg --help` and docs/USAGE.md)."""

EXIT_OK = 0
EXIT_OP_FAILED = 1  # one or more file operations failed (others may have succeeded)
EXIT_USAGE = 2  # bad arguments or configuration, or broken installation
EXIT_PRECONDITION = 3  # storage not mounted or not answering, path problem, download state unknown, no space
EXIT_LOCKED = 4  # another run is in progress
EXIT_JOURNAL = 5  # an interrupted run left contradictory state: needs a person, then `mediaorg recover`
EXIT_CAP = 6  # the plan exceeds safety.max_moves_per_run
EXIT_INTERRUPTED = 130  # Ctrl-C; the next run reconciles the journal

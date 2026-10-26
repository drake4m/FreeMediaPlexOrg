"""mediaorg - file finished movie and TV downloads into a library, safely.

Layers (each testable on its own):
  identify     - release name -> MediaId (pure; guessit + validation)
  library      - index of the existing library folders (read-only I/O)
  matching     - MediaId x library folders -> match decision (pure)
  completeness - is a Downloads entry finished? (quiescence / Freebox API)
  planner      - items + library + completeness -> plan of operations (pure)
  transfer     - journaled, no-replace renames; recover; undo
  review       - retention of the review folders; tmmtrash - tinyMediaManager's trash
  guards       - mount identity, path checks, run lock
  config       - configuration loading and validation; schema - key documentation and TOML writer
  configcmd    - `mediaorg config` commands; service - systemd units; system - privileges, safe writes
  tmm / freebox - HTTP API clients
"""

__version__ = "2.0.0"

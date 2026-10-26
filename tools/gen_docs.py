#!/usr/bin/env python3
"""Regenerate the files derived from the code, so that they can never drift from it:

  config/config.example.toml     an example configuration, with every key documented
  docs/CONFIGURATION.md          the key reference between the BEGIN/END markers
  docs/CLI.md                    `mediaorg --help` of every command, between the markers

    python3 tools/gen_docs.py            write them
    python3 tools/gen_docs.py --check    exit 1 if one of them is out of date (used by the tests and CI)
"""

from __future__ import annotations

import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mediaorg import __version__, schema  # noqa: E402
from mediaorg.config import SECTIONS  # noqa: E402

EXAMPLE = os.path.join(ROOT, "config", "config.example.toml")
CONFIG_MD = os.path.join(ROOT, "docs", "CONFIGURATION.md")
CLI_MD = os.path.join(ROOT, "docs", "CLI.md")
BEGIN = "<!-- BEGIN GENERATED: tools/gen_docs.py -->"
END = "<!-- END GENERATED -->"

EXAMPLE_RAW = {
    "paths": {"downloads": "/srv/media/downloads", "movies": "/srv/media/movies", "series": "/srv/media/tv"},
    "completeness": {"method": "quiescence"},
}
EXAMPLE_TITLE = ("# mediaorg configuration - EXAMPLE. `sudo mediaorg config init` writes your own "
                 "(docs/INSTALL.md).")


def example() -> str:
    return schema.render_file(EXAMPLE_RAW, __version__, title=EXAMPLE_TITLE)


def _cell(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|").replace("\n", " ")


def _default(key: schema.Key) -> str:
    if key.kind == "commands":
        return "update all, scrape new, rename new"
    d = key.default
    if d is schema.REQUIRED:
        return "required" if key.section != "mount" else "(none: no check)"
    if key.derived and d in ("", None):
        return key.derived
    if d is None:
        return "not set"
    return "`" + schema.toml_value(schema._plain(d), 0, width=10_000) + "`"


def reference_md() -> str:
    out = []
    for section in SECTIONS:
        out.append(f"### `[{section}]`")
        out.append("")
        out.append(schema.SECTION_DOCS[section])
        out.append("")
        out.append("| Key | Type | Default | Meaning |")
        out.append("|---|---|---|---|")
        for key in (k for k in schema.KEYS if k.section == section):
            kind = key.kind + (f" ({' / '.join(key.choices)})" if key.choices else "")
            doc = key.doc + (" *(advanced)*" if key.advanced else "")
            if key.example is not None:
                doc += f" Example: `{schema.toml_value(key.example, 0, width=10_000)}`"
            out.append(f"| `{key.name}` | {_cell(kind)} | {_cell(_default(key))} | {_cell(doc)} |")
        out.append("")
    return "\n".join(out)


def cli_md() -> str:
    from mediaorg.cli import build_parser

    def help_of(argv: list[str]) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            os.environ["COLUMNS"] = "100"
            build_parser().parse_args([*argv, "--help"])
        return buf.getvalue().rstrip()

    commands = [[], ["run"], ["status"], ["check"], ["runs"], ["undo"], ["recover"], ["identify"], ["library"],
                ["probe-rename"], ["config", "init"], ["config", "set"], ["config", "unset"], ["config", "get"],
                ["config", "show"], ["config", "keys"], ["config", "validate"], ["config", "path"],
                ["service"], ["service", "install"], ["freebox"], ["tmm"], ["tmm", "set-key"], ["tmm", "sync"]]
    out = []
    for argv in commands:
        title = "mediaorg " + " ".join(argv) if argv else "mediaorg"
        out += [f"### `{title}`", "", "```text", help_of(argv), "```", ""]
    return "\n".join(out)


def _replace_block(path: str, block: str) -> tuple[str, str]:
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    start, end = text.index(BEGIN) + len(BEGIN), text.index(END)
    return text, text[:start] + "\n\n" + block + "\n" + text[end:]


def main(argv: list[str]) -> int:
    check = "--check" in argv
    targets = [(EXAMPLE, None, example())]
    for path, block in ((CONFIG_MD, reference_md()), (CLI_MD, cli_md())):
        old, new = _replace_block(path, block)
        targets.append((path, old, new))
    stale = []
    for path, old, new in targets:
        if old is None:
            try:
                with open(path, encoding="utf-8") as fh:
                    old = fh.read()
            except FileNotFoundError:
                old = ""
        if old != new:
            stale.append(os.path.relpath(path, ROOT))
            if not check:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(new)
    if check and stale:
        print("out of date (run python3 tools/gen_docs.py): " + ", ".join(stale), file=sys.stderr)
        return 1
    if not check:
        print("updated: " + (", ".join(stale) if stale else "nothing (all up to date)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

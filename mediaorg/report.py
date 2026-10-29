"""Human-readable and JSON reports of a plan / run."""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter

from .model import ItemPlan

_MINUTES = re.compile(r"\d+ min\b")
_TAG = {"ready": "READY", "partial": "PARTIAL", "skip": "SKIP", "wait": "WAIT", "ignored": "IGNORED"}


def _rel(path: str | None, base: str) -> str:
    if not path:
        return ""
    return os.path.relpath(path, base) if path.startswith(base + "/") else path


def new_action(plan: ItemPlan) -> str:
    if plan.status in ("wait", "ignored", "skip"):
        return "leave"
    acts = {d.action for d in plan.decisions}
    if not acts and plan.ops and all(o.kind == "rmdir" for o in plan.ops):
        return "remove empty folder"
    if "move" in acts:
        return "move"
    return "delete" if acts == {"delete"} else "review"


def render_text(plans: list[ItemPlan], base: str, ignored: list[tuple[str, str]], apply: bool,
                quiet: bool = False, summary_of: list[ItemPlan] | None = None,
                review_lines: list[tuple[str, str]] = (), notes: list[str] = ()) -> str:
    """Human report of the given plans and ignored entries, then the summary line.

    quiet (scheduled runs): the caller passes only what is acted on, new or changed, and the summary
    counts every item (summary_of).  review_lines: (tag, text) for review folders ("PURGE": files
    deleted now; "KEPT": files kept for you) and tMM's trash ("TRASH": files deleted now).
    notes: general remarks."""
    lines = []
    for p in plans:
        lines.append(f"[{_TAG[p.status]}] {p.item.name}" + (f"  -- {p.reason}" if p.reason else ""))
        for d in p.decisions:
            what = d.media.label() if d.media else ""
            arrow = f" -> {_rel(d.target, base)}" if d.target else ""
            lines.append(f"    {d.action:<9} {d.file.rel}{arrow}")
            lines.append(f"              {what + ': ' if what else ''}{d.reason}")
            for n in d.notes:
                lines.append(f"              note: {n}")
        if p.ops and p.status in ("ready", "partial"):
            for op in p.ops:
                if op.role in ("leftovers", "cleanup"):
                    lines.append(f"    {op.kind:<9} {_rel(op.src or op.dst, base)}"
                                 + (f" -> {_rel(op.dst, base)}" if op.src else "") + f"  ({op.role})")
                elif op.role == "duplicate-sidecar":
                    lines.append(f"    {'move':<9} {_rel(op.src, base)} -> {_rel(op.dst, base)}  (subtitle of the "
                                 f"duplicate)")
                elif op.role == "empty-placeholder":
                    lines.append(f"    {'delete':<9} {_rel(op.dst, base)}  (empty 0-byte library file, replaced)")
    for name, why in ignored:
        lines.append(f"[IGNORED] {name}  -- {why}")
    for text in notes:
        lines.append(f"[NOTE] {text}")
    for tag, text in review_lines:
        lines.append(f"[{tag}] {text}")
    if lines:
        lines.append("")
    purges = sum(1 for tag, _ in review_lines if tag == "PURGE")
    trash = sum(1 for tag, _ in review_lines if tag == "TRASH")
    lines.append(summary_line(summary_of if summary_of is not None else plans, apply, purges, trash))
    return "\n".join(lines)


def counts(plans: list[ItemPlan]) -> dict:
    st = Counter(p.status for p in plans)
    active = [op for p in plans if p.status in ("ready", "partial") for op in p.ops]
    ops = Counter((op.kind, op.role) for op in active)
    return {
        "items": dict(st),
        "renames": sum(v for (k, _), v in ops.items() if k == "rename"),
        "media": ops[("rename", "media")],
        "sidecars": ops[("rename", "sidecar")] + ops[("rename", "duplicate-sidecar")],
        "duplicates": ops[("rename", "duplicate")], "leftovers": ops[("rename", "leftovers")],
        "deleted": ops[("unlink", "identical-duplicate")], "emptied": ops[("unlink", "empty-placeholder")],
        "dup_deleted": ops[("unlink", "same-duplicate")],
        "mkdir": len({op.dst for op in active if op.kind == "mkdir"}),  # same folder may be listed per item
        "rmdir": sum(v for (k, _), v in ops.items() if k == "rmdir"),
    }


def summary_line(plans: list[ItemPlan], apply: bool, purges: int = 0, trash: int = 0) -> str:
    c = counts(plans)
    items = ", ".join(f"{k} {v}" for k, v in sorted(c["items"].items())) or "none"
    mode = "APPLY" if apply else "DRY-RUN (nothing changed; add --apply to act)"
    return (f"{mode}: items: {items} | renames {c['renames']} (media {c['media']}, subtitles {c['sidecars']}, "
            f"duplicates {c['duplicates']}, leftovers {c['leftovers']}), "
            f"deletes {c['deleted'] + c['dup_deleted'] + c['emptied']} (identical downloads {c['deleted']}, "
            + (f"duplicates {c['dup_deleted']}, " if c["dup_deleted"] else "")
            + f"empty library files {c['emptied']}), mkdir {c['mkdir']}, "
            f"rmdir {c['rmdir']}" + (f" | review folders to purge {purges}" if purges else "")
            + (f" | tMM trash folders to clean {trash}" if trash else ""))


def to_json(plans: list[ItemPlan], meta: dict, purges: list[dict] = ()) -> dict:
    out = {"meta": meta, "summary": counts(plans), "items": [], "review_purge": list(purges)}
    for p in plans:
        out["items"].append({
            "name": p.item.name, "status": p.status, "reason": p.reason,
            "decisions": [{
                "file": d.file.rel, "action": d.action, "reason": d.reason, "target": d.target,
                "media": d.media.label() if d.media else None, "notes": d.notes, "refs": d.refs,
            } for d in p.decisions],
            "ops": [{"op": o.kind, "src": o.src, "dst": o.dst, "role": o.role, **({"ref": o.ref} if o.ref else {})}
                    for o in p.ops] if p.status in ("ready", "partial") else [],
        })
    return out


def fingerprint(plan: ItemPlan) -> str:
    """What a scheduled (--quiet) run reports about an item, without the minute counts that change on
    their own every run ("modified 3 min ago"): an item is listed again only when this changes."""
    parts = [plan.status, plan.reason] + [f"{d.action}:{d.file.rel}:{d.reason}" for d in plan.decisions]
    return _MINUTES.sub("# min", "|".join(parts))


class QuietState:
    """Fingerprints of what the previous --quiet --apply run reported (<state_dir>/quiet_state.json)."""

    def __init__(self, state_dir: str):
        self.path = os.path.join(state_dir, "quiet_state.json")
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.old: dict[str, str] = data if isinstance(data, dict) else {}
        except (FileNotFoundError, ValueError):
            self.old = {}

    def changed(self, key: str, fp: str) -> bool:
        return self.old.get(key) != fp

    def save(self, current: dict[str, str], merge: bool = False) -> None:
        data = {**self.old, **current} if merge else current
        if data == self.old:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", errors="backslashreplace") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.path)


def save_json(report: dict, directory: str, run_id: str) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{run_id}.json")
    tmp = path + ".tmp"
    # backslashreplace: a non-UTF-8 file name becomes a JSON \udcXX escape instead of crashing the run
    with open(tmp, "w", encoding="utf-8", errors="backslashreplace") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def prune_reports(directory: str, retention_days: int, now: float | None = None, max_reports: int = 0) -> int:
    """Delete JSON run reports older than retention_days (0 = no age limit), then the oldest beyond
    max_reports (0 = no count limit).  Returns the number removed.

    Only files named like a run report (<YYYYmmddTHHMMSSZ-xxxx>.json) are considered."""
    if not os.path.isdir(directory) or (retention_days <= 0 and max_reports <= 0):
        return 0
    pattern = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{4}\.json$")
    limit = (now if now is not None else time.time()) - retention_days * 86400
    removed = 0
    kept = []
    for name in sorted(os.listdir(directory)):  # run ids sort by time
        if not pattern.match(name):
            continue
        path = os.path.join(directory, name)
        try:
            if retention_days > 0 and os.lstat(path).st_mtime < limit:
                os.unlink(path)
                removed += 1
            else:
                kept.append(path)
        except FileNotFoundError:
            continue
    if max_reports > 0 and len(kept) > max_reports:
        for path in kept[:len(kept) - max_reports]:
            try:
                os.unlink(path)
                removed += 1
            except FileNotFoundError:
                continue
    return removed

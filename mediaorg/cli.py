"""Command line interface.  Dry-run by default; --apply is required to change files."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import logging.handlers
import os
import secrets
import shutil
import sys
import time
import unicodedata

from . import __version__
from .config import DEFAULT_CONFIG, SERVICE_USER, Config, ConfigError, load
from .exitcodes import (EXIT_CAP, EXIT_INTERRUPTED, EXIT_JOURNAL, EXIT_LOCKED, EXIT_OK, EXIT_OP_FAILED,
                        EXIT_PRECONDITION, EXIT_USAGE)

log = logging.getLogger("mediaorg")

__all__ = ["main", "build_parser", "EXIT_OK", "EXIT_OP_FAILED", "EXIT_USAGE", "EXIT_PRECONDITION", "EXIT_LOCKED",
           "EXIT_JOURNAL", "EXIT_CAP", "EXIT_INTERRUPTED"]


class _JournalFormatter(logging.Formatter):
    """Under systemd (stderr connected to the journal) prefix each line with its syslog priority
    ("<3>" error, "<4>" warning, "<6>" info, "<7>" debug) so `journalctl -p warning` works."""
    PRIO = {logging.CRITICAL: 2, logging.ERROR: 3, logging.WARNING: 4, logging.INFO: 6, logging.DEBUG: 7}

    def format(self, record):
        return f"<{self.PRIO.get(record.levelno, 6)}>" + super().format(record)


def _setup_logging(verbose: bool) -> None:
    fmt = "%(asctime)s %(levelname)s %(message)s" if sys.stderr.isatty() else "%(levelname)s %(message)s"
    handler = logging.StreamHandler(sys.stderr)
    journal = bool(os.environ.get("JOURNAL_STREAM")) and not sys.stderr.isatty()
    handler.setFormatter(_JournalFormatter(fmt) if journal else logging.Formatter(fmt))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=[handler], force=True)


def _add_file_logging(cfg: Config) -> None:
    """[logging] file: a copy of the log in a file that rotates by size, so it never grows without
    bound (max_size_mb x (backups + 1) at most).  Opened after privileges were dropped."""
    path = cfg.logging.file
    if not path:
        return
    try:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=cfg.logging.max_size_mb * 1024 * 1024, backupCount=cfg.logging.backups,
            encoding="utf-8", errors="backslashreplace")
    except OSError as exc:
        log.warning("log file %s not used: %s", path, exc)
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s"))
    logging.getLogger().addHandler(handler)


def _free_mb(path: str) -> int:
    return shutil.disk_usage(path).free // (1024 * 1024)


def _run_id() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)


def _dirs(cfg: Config) -> tuple[str, str, str]:
    s = cfg.paths.state_dir
    return os.path.join(s, "journal"), os.path.join(s, "runs"), os.path.join(s, "lock")


# ------------------------------------------------------------------------------- run
def _reconcile_or_exit(journal_dir: str, dry_run: bool) -> int | None:
    from .transfer import reconcile

    resolved, problems = reconcile(journal_dir, dry_run=dry_run)
    if problems:
        for c in problems:
            log.error("journal: %s", c)
        if any("not answering" in c for c in problems):
            log.error("the NAS does not answer; cannot verify an interrupted run")
            return EXIT_PRECONDITION
        log.error("an interrupted run left contradictory state; fix the paths above by hand, "
                  "then run `mediaorg recover`")
        return EXIT_JOURNAL
    if resolved:
        if dry_run:
            log.warning("%d open journal record(s) from an interrupted run will be reconciled by the "
                        "next --apply run (or `mediaorg recover`)", resolved)
        else:
            log.warning("reconciled %d journal record(s) from an interrupted run", resolved)
    return None


class _LazyAssessor:
    """Opens the Freebox session only when an item actually needs a completeness verdict, so a run
    with nothing to file (the usual case every 5 minutes) makes no Freebox API call at all."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._assess = None
        self._close = None

    def __call__(self, item):
        if self._assess is None:
            from .completeness import make_assessor
            self._assess, self._close = make_assessor(self.cfg)
        return self._assess(item)

    def close(self) -> None:
        if self._close is not None:
            self._close()


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_json_atomic(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", errors="backslashreplace") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:  # caches only: never fatal
        log.debug("could not save %s: %s", path, exc)


def _retention_notes(cfg: Config, plans) -> list[str]:
    """With a retention of 0, what this run moves to review is cleaned at its end: say so up front."""
    roles = {op.role for p in plans if p.status in ("ready", "partial") for op in p.ops}
    notes = []
    if cfg.review.duplicates_retention_days == 0 and not cfg.review.delete_duplicates and roles & {
            "duplicate", "duplicate-sidecar"}:
        notes.append("review.duplicates_retention_days = 0: the duplicates this run moves to review are deleted "
                     "at the end of the run")
    if cfg.review.leftovers_retention_days == 0 and "leftovers" in roles:
        notes.append("review.leftovers_retention_days = 0: the leftovers this run moves to review are cleaned at "
                     "the end of the run (only release clutter and small samples are deleted)")
    return notes


def _due_json(d) -> dict:
    return {"path": d.path, "kind": d.kind, "action": d.action, "age_days": round(d.age_days, 2),
            "delete_files": d.files, "delete_bytes": d.size, "kept": [r for r, _ in d.keep]}


def cmd_run(cfg: Config, args) -> int:
    from . import review
    from .freebox import FreeboxError
    from .guards import LockBusy, PreconditionError, RunLock, check_mount, check_paths
    from .library import Library, LibraryUnstable
    from .model import Readiness
    from .planner import Planner
    from .report import QuietState, fingerprint, prune_reports, render_text, save_json, to_json
    from .scan import scan_downloads
    from .transfer import Unfinished

    journal_dir, runs_dir, lock_path = _dirs(cfg)
    run_id = _run_id()
    apply = bool(args.apply)
    quiet = bool(args.quiet)
    info = log.debug if quiet else log.info  # housekeeping messages: silent in scheduled runs
    try:
        with RunLock(lock_path):
            try:
                check_mount(cfg)
                check_paths(cfg)
            except (PreconditionError, OSError) as exc:
                log.error("precondition failed: %s", exc)
                return EXIT_PRECONDITION
            code = _reconcile_or_exit(journal_dir, dry_run=not apply)
            if code is not None:
                return code
            unfinished = Unfinished(cfg.paths.state_dir)
            if apply:
                unfinished.prune()
            try:
                items, ignored = scan_downloads(cfg)
                lib = Library(cfg.paths.movies, cfg.paths.series, cfg.media.video_extensions)
            except (OSError, LibraryUnstable) as exc:
                log.error("precondition failed: %s", exc)
                return EXIT_PRECONDITION
            if args.only:
                wanted = {unicodedata.normalize("NFC", n).casefold() for n in args.only}
                items = [i for i in items if unicodedata.normalize("NFC", i.name).casefold() in wanted]
                missing = wanted - {unicodedata.normalize("NFC", i.name).casefold() for i in items}
                if missing:
                    log.error("--only: not found in Downloads: %s", ", ".join(sorted(missing)))
                    return EXIT_USAGE
                ignored = []
            compare_path = os.path.join(cfg.paths.state_dir, "identical_cache.json")
            old_compare = {k: v for k, v in _load_json(compare_path).items() if isinstance(v, bool)}
            compare_cache = dict(old_compare)
            assess = _LazyAssessor(cfg)
            try:
                planner = Planner(cfg, lib, run_id, compare_cache,
                                  on_compare=lambda cache: _save_json_atomic(compare_path, cache))
                plans = []
                for item in items:
                    readiness = assess(item) if planner.needs_readiness(item) else Readiness(False, "not assessed")
                    plan = planner.plan_item(item, readiness)
                    if plan.status == "ignored" and item.path in unfinished.items:
                        plan = planner.plan_remains(item, unfinished.items[item.path])
                    plans.append(plan)
            except (FreeboxError, ConfigError) as exc:
                log.error("cannot determine download completeness (Freebox), nothing moved: %s", exc)
                return EXIT_PRECONDITION
            except (OSError, LibraryUnstable) as exc:
                log.error("library could not be read reliably, nothing moved: %s", exc)
                return EXIT_PRECONDITION
            finally:
                assess.close()
            used = {k: compare_cache[k] for k in planner.used_compare_keys if k in compare_cache}
            new_compare = {**old_compare, **used} if args.only else used
            if new_compare != old_compare:
                _save_json_atomic(compare_path, new_compare)
            try:
                review_due = [] if args.only else review.due(cfg)
            except OSError as exc:
                log.warning("review folders not checked for purging: %s", exc)
                review_due = []
            trash_now = time.time()
            trash_scans = _scan_trash(cfg, args, trash_now)

            # will this run act? (checked before printing, so that a scheduled run that cannot act
            # does not report the same READY items every 5 minutes)
            todo = [p for p in plans if p.status in ("ready", "partial") and p.ops]
            nops = sum(1 for p in todo for o in p.ops if o.kind in ("rename", "unlink"))
            cap = args.max_moves or cfg.safety.max_moves_per_run
            blocked, busy = None, None
            if apply and todo:
                if nops > cap:
                    blocked = "cap"
                elif cfg.tmm.enabled and cfg.tmm.defer_moves_while_busy and not args.no_tmm:
                    busy = _tmm_busy(cfg)
                    blocked = "tmm" if busy else None
            will_act = apply and bool(todo) and blocked is None
            if any(p.stop_tasks for p in todo):
                # completeness.on_seeding = "stop": stopped only by a run that files them, before the report
                # is written, so that an item whose task did not stop is reported as WAIT and not moved
                _stop_seeding(cfg, todo, apply, will_act)
                todo = [p for p in todo if p.status in ("ready", "partial")]
                will_act = apply and bool(todo) and blocked is None
            will_purge = apply and not args.only and blocked != "cap"

            # what to print: everything; in scheduled (--quiet) runs only what is acted on or changed
            shown_plans, shown_ignored, shown_review = plans, ignored, review_due
            current: dict[str, str] = {}
            if quiet:
                qstate = QuietState(cfg.paths.state_dir)
                current = {p.item.name: fingerprint(p) for p in plans}
                current.update({n: f"ignored|{why}" for n, why in ignored if why != "ignored name"})
                current.update({f"review:{d.path}": d.fingerprint() for d in review_due if d.action == "kept"})
                acting = {id(p) for p in todo} if will_act else set()
                shown_plans = [p for p in plans if qstate.changed(p.item.name, current[p.item.name])
                               or id(p) in acting]
                shown_ignored = [(n, w) for n, w in ignored if n in current and qstate.changed(n, current[n])]
                shown_review = [d for d in review_due if (d.action == "purge" and will_purge)
                                or (d.action == "kept"
                                    and qstate.changed(f"review:{d.path}", current[f"review:{d.path}"]))]
            review_lines = [("PURGE" if d.action == "purge" else "KEPT", review.describe(d, cfg.base_dir))
                            for d in shown_review]
            trash_secs = cfg.review.tmm_trash_retention_seconds
            if trash_scans and (not quiet or will_purge):
                from .tmmtrash import describe as describe_trash
                review_lines += [("TRASH", t) for t in (describe_trash(sc, trash_secs, cfg.base_dir)
                                                        for sc in trash_scans) if t]
            notes = _retention_notes(cfg, shown_plans)
            if blocked == "tmm":
                notes.append(f"tMM is busy ({busy}): nothing is moved in this run; the next run tries again")
            report, report_meta = None, {"run_id": run_id, "apply": apply, "version": __version__,
                                         "config": cfg.source_file, "only": args.only or []}
            if not quiet or shown_plans or shown_ignored or review_lines:
                report = to_json(plans, report_meta, purges=[_due_json(d) for d in review_due])
                report["tmm_trash"] = _trash_json(trash_scans, trash_secs)
                # flushed before the log lines that follow: under systemd stdout is a buffered pipe and the
                # report would otherwise reach the journal after them
                print(render_text(shown_plans, cfg.base_dir, shown_ignored, apply, quiet=quiet,
                                  summary_of=plans, review_lines=review_lines, notes=notes), flush=True)
                try:
                    log.info("report saved to %s", save_json(report, runs_dir, run_id))
                except OSError as exc:
                    log.warning("could not save the report: %s", exc)
            if quiet and apply:  # a manual dry-run never hides a change from the next scheduled run
                try:
                    qstate.save(current, merge=bool(args.only))
                except OSError as exc:
                    log.warning("could not save %s: %s", qstate.path, exc)
            try:
                pruned = prune_reports(runs_dir, cfg.reports.retention_days, max_reports=cfg.reports.max_reports)
                if pruned:
                    info("removed %d old report(s) (reports.retention_days %d, reports.max_reports %d)", pruned,
                         cfg.reports.retention_days, cfg.reports.max_reports)
            except OSError as exc:
                log.warning("could not prune old reports: %s", exc)
            if apply:
                from .transfer import prune_journals
                try:
                    gone = prune_journals(journal_dir, cfg.reports.journal_retention_days)
                    if gone:
                        info("removed %d journal(s) of finished runs older than %d days", gone,
                             cfg.reports.journal_retention_days)
                except OSError as exc:
                    log.warning("could not prune old journals: %s", exc)
            if not apply:
                return EXIT_OK

            if blocked == "cap":
                log.error("plan has %d moves/deletions, above the safety cap of %d; use --max-moves or --only",
                          nops, cap)
                return EXIT_CAP
            if will_act and cfg.safety.min_free_mb:  # only when something is moved: idle runs write nothing
                free = _free_mb(cfg.paths.state_dir)
                if free < cfg.safety.min_free_mb:
                    log.error("precondition failed: only %d MB free in %s (safety.min_free_mb = %d): every move "
                              "is journaled there first; nothing moved", free, cfg.paths.state_dir,
                              cfg.safety.min_free_mb)
                    return EXIT_PRECONDITION
            if blocked == "tmm":
                info("tMM is busy (%s); moves deferred to the next run", busy)
                code, moved = EXIT_OK, None
            else:
                code, moved = _apply(cfg, args, todo, run_id, unfinished, info)
            queued = True
            if moved is not None:
                # recorded right away, sent after the cleaning: tMM's renames move replaced files into its
                # trash, which should not happen while the trash is being cleaned
                queued = _tmm_queue(cfg, args, moved, state_dir=cfg.paths.state_dir)
            try:
                if code in (EXIT_OK, EXIT_OP_FAILED) and not args.only:
                    # listed again after the moves: with a retention of 0 this run's own folders are due too
                    ok, results = _purge_review(cfg)
                    tok, tresults = _purge_trash(cfg, trash_scans, trash_now, quiet)
                    if not (ok and tok):
                        code = EXIT_OP_FAILED
                    if results or tresults:
                        if report is None:
                            report = to_json(plans, report_meta, purges=[_due_json(d) for d in review_due])
                            report["tmm_trash"] = _trash_json(trash_scans, trash_secs)
                        if results:
                            report["review_purge_done"] = results
                        if tresults:
                            report["tmm_trash_done"] = tresults
                        try:
                            save_json(report, runs_dir, run_id)
                        except OSError as exc:
                            log.warning("could not save the report: %s", exc)
            finally:
                if moved is not None:
                    _tmm_send(cfg, args, moved, state_dir=cfg.paths.state_dir, save_failed=not queued)
            return code
    except LockBusy as exc:
        log.warning("%s", exc)
        return EXIT_LOCKED


def _stop_seeding(cfg: Config, todo, apply: bool, will_act: bool) -> None:
    """Items whose Freebox task still seeds the finished download (completeness.on_seeding = "stop"): a run that
    files them stops the tasks first and files only what the Freebox then lists as stopped; any other run says
    what will happen.  An item whose task did not stop becomes WAIT (not moved in this run)."""
    from .guards import PreconditionError, check_mount

    pending = [p for p in todo if p.stop_tasks]
    cannot = ""
    if will_act:  # the run's last checks before moving, made before anything is stopped
        try:
            check_mount(cfg)
            free = _free_mb(cfg.paths.state_dir)
            if cfg.safety.min_free_mb and free < cfg.safety.min_free_mb:
                cannot = f"only {free} MB free in {cfg.paths.state_dir}"
        except (PreconditionError, OSError) as exc:
            cannot = str(exc)
    if not will_act or cannot:
        for p in pending:
            ids = ", ".join(str(i) for i in sorted(p.stop_tasks))
            note = (f"Freebox task {ids} is seeding: an --apply run stops it, then files the download" if not apply
                    else f"Freebox task {ids} is seeding: not stopped, this run cannot file it ({cannot})" if cannot
                    else f"Freebox task {ids} is seeding: stopped when the download can be filed")
            p.reason = f"{note}; {p.reason}" if p.reason else note
        return
    from .completeness import freebox_session, stop_seeding
    from .freebox import FreeboxError

    def waits(p, why: str) -> None:  # reported like any WAIT item: no planned moves, nothing done
        p.status, p.reason = "wait", why
        p.decisions, p.ops, p.tmm_paths, p.stop_tasks = [], [], {}, frozenset()

    try:
        client = freebox_session(cfg)
    except (FreeboxError, ConfigError) as exc:
        for p in pending:
            waits(p, f"the seeding Freebox task could not be stopped: {exc}")
        return
    try:
        for p in pending:
            try:
                ok, text = stop_seeding(client, p.stop_tasks, names={i: p.item.name for i in p.stop_tasks})
            except FreeboxError as exc:
                ok, text = False, f"the seeding Freebox task could not be stopped: {exc}"
            if ok:
                p.reason = f"{text}; {p.reason}" if p.reason else text
            else:
                waits(p, text)
    finally:
        client.logout()


def _apply(cfg: Config, args, todo, run_id: str, unfinished, info) -> tuple[int, dict[str, set[str]] | None]:
    """Execute the planned items (called with the run lock held, cap and tMM already checked).
    -> (exit code, library folders filed per kind for tMM; None when the run stopped before or during the
    moves)"""
    from .guards import PreconditionError, check_mount
    from .transfer import Journal, StorageUnavailable, execute

    journal_dir, _, _ = _dirs(cfg)
    if not todo:
        info("nothing to do")
        return EXIT_OK, {}  # {}: still sends the calls kept from a run that found tMM down
    try:
        check_mount(cfg)  # planning took time: verify again right before touching files
    except PreconditionError as exc:
        log.error("precondition failed just before moving: %s", exc)
        return EXIT_PRECONDITION, None

    journal = Journal(journal_dir, run_id)
    journal.write({"state": "begin", "kind": "run", "version": __version__, "items": len(todo)})
    try:
        res = execute(todo, journal, unfinished)
    except StorageUnavailable as exc:
        journal.write({"state": "end", "status": "aborted", "error": str(exc), "needs_reconcile": True})
        journal.close()
        log.error("NAS stopped answering during the run (%s); stopped. The next run verifies "
                  "the journal against the disk before doing anything else.", exc)
        return EXIT_PRECONDITION, None
    journal.write({"state": "end", "status": "ok" if not res.failed else "errors",
                   "done": res.done, "failed": res.failed})
    journal.close()
    for e in res.errors:
        log.error("%s", e)
    for name in res.vanished:
        log.warning("%s: gone from Downloads since the scan (deleted?): nothing done", name)
    log.info("run %s: %d operation(s) done, %d failed", run_id, res.done, res.failed)
    return (EXIT_OP_FAILED if res.failed else EXIT_OK), res.moved_paths


def _purge_review(cfg: Config) -> tuple[bool, list[dict]]:
    """Clean the expired review folders (after the moves of an --apply run).
    -> (False if a deletion failed, one result per folder for the JSON report)"""
    from .guards import PreconditionError, check_mount
    from .review import due, human, purge
    from .transfer import StorageUnavailable

    try:
        entries = [e for e in due(cfg) if e.action == "purge"]
    except OSError as exc:
        log.warning("review folders not checked for purging: %s", exc)
        return True, []
    if not entries:
        return True, []
    try:
        check_mount(cfg)
    except PreconditionError as exc:
        log.error("review folders not purged: %s", exc)
        return False, []
    ok, results = True, []
    mp = cfg.base_dir
    for e in entries:
        try:
            done, size, errors, deleted = purge(e)
        except StorageUnavailable as exc:
            log.error("review folders: the NAS stopped answering (%s); purge stopped", exc)
            results.append({"path": e.path, "kind": e.kind, "errors": [str(exc)]})
            return False, results
        rel = os.path.relpath(e.path, mp)
        results.append({"path": e.path, "kind": e.kind, "deleted_files": done, "deleted_bytes": size,
                        "deleted": deleted, "kept": [r for r, _ in e.keep], "errors": errors})
        more = f" (+{len(deleted) - 10} more)" if len(deleted) > 10 else ""
        shown = ", ".join(repr(x) for x in deleted[:10]) + more
        log.info("purged %s: %d file(s), %s deleted%s%s", rel, done, human(size), f": {shown}" if deleted else "",
                 f"; {len(e.keep)} file(s) kept for you" if e.keep else "")
        for err in errors:
            log.error("could not purge %s", err)
        ok = ok and not errors
    return ok, results


def _scan_trash(cfg: Config, args, now: float):
    """tMM's trash folders as they are now, with how long each file has been seen there (read-only), or
    None when not checked (--only, retention -1, or unreadable: then nothing is deleted and the recorded
    state is kept).  `now` is the clock the purge records."""
    from .tmmtrash import load_state, scan
    from .transfer import StorageUnavailable

    if args.only or cfg.review.tmm_trash_retention_seconds < 0:
        return None
    state = load_state(cfg.paths.state_dir)
    try:
        scans = scan(cfg, state, now)
    except (OSError, StorageUnavailable) as exc:
        log.warning("tMM trash folders (.deletedByTMM) not checked: %s", exc)
        return None
    bad = sorted(p for sc in scans for p in sc.unreadable)
    if bad and (not args.quiet or bad != sorted(state.unreadable)):  # scheduled runs: when it changes
        mp = cfg.base_dir
        shown = ", ".join(repr(os.path.relpath(p, mp)) for p in bad[:5]) + (f" (+{len(bad) - 5} more)"
                                                                         if len(bad) > 5 else "")
        log.warning("tMM trash: %d entry(ies) cannot be read or are not files; left alone: %s", len(bad), shown)
    return scans


def _trash_json(scans, seconds: int) -> list[dict]:
    return [{"path": sc.root, "files": len(sc.files), "delete": [f.path for f in sc.due(seconds)],
             "unreadable": sc.unreadable} for sc in scans or ()]


def _purge_trash(cfg: Config, scans, now: float, quiet: bool) -> tuple[bool, list[dict]]:
    """Delete the due files of tMM's trash folders (after the moves of an --apply run) and record how long
    the others have been seen.  -> (False if a deletion failed, one result per trash folder for the report)"""
    from .guards import PreconditionError, check_mount
    from .review import human
    from .tmmtrash import purge, save_state
    from .transfer import StorageUnavailable

    if scans is None:
        return True, []
    secs = cfg.review.tmm_trash_retention_seconds
    ok, results, gone, stuck = True, [], set(), []
    todo = [sc for sc in scans if sc.due(secs) or sc.empty_dirs()]
    if todo:
        try:
            check_mount(cfg)
        except PreconditionError as exc:
            log.error("tMM trash not cleaned: %s", exc)
            return False, []
    for sc in todo:
        rel = os.path.relpath(sc.root, cfg.base_dir)
        try:
            res = purge(sc, secs, now)
        except StorageUnavailable as exc:
            log.error("tMM trash: the NAS stopped answering (%s); cleaning stopped", exc)
            results.append({"path": sc.root, "errors": [str(exc)]})
            return False, results  # state not saved: the next run scans again
        if res.skipped:
            log.warning("tMM trash %s: %s", rel, res.skipped)
            stuck += sc.prev_folder_errors  # not tried: still recorded
            continue
        gone.update(res.deleted)
        failed = sorted(p for p, _ in res.folder_errors)
        stuck += failed
        # an empty folder that cannot be removed is reported when that changes, not at every timer run
        new_failure = bool(failed) and (not quiet or failed != sorted(sc.prev_folder_errors))
        if not (res.deleted or res.errors or res.removed or res.left or new_failure):
            continue  # nothing new: recent empty folders are left for tMM
        names = [os.path.relpath(p, sc.root) for p in res.deleted]
        folders = [os.path.relpath(p, sc.root) for p in res.removed]
        results.append({"path": sc.root, "deleted_files": len(names), "deleted_bytes": res.size, "deleted": names,
                        "removed_folders": folders, "errors": res.errors,
                        "folder_errors": [f"{p}: {why}" for p, why in res.folder_errors],
                        "left": [f"{os.path.relpath(p, sc.root)}: {why}" for p, why in res.left]})
        shown = ", ".join(repr(x) for x in names[:10]) + (f" (+{len(names) - 10} more)" if len(names) > 10 else "")
        if names:
            log.info("cleaned %s: %d file(s), %s deleted: %s%s", rel, len(names), human(res.size), shown,
                     f"; {len(folders)} emptied folder(s) removed" if folders else "")
        elif folders:
            log.info("cleaned %s: %d empty folder(s) removed", rel, len(folders))
        for err in res.errors:
            log.error("could not delete %s", err)
        for p, why in res.left:  # due, but not what the scan saw a moment ago: never deleted blindly
            log.info("tMM trash %s: %r left for the next run: %s", rel, os.path.relpath(p, sc.root), why)
        if new_failure:  # an empty folder left behind is not worth a failed run
            for p, why in res.folder_errors:
                log.warning("could not remove the empty folder %s: %s", p, why)
        ok = ok and not res.errors
    try:
        save_state(cfg.paths.state_dir, scans, gone, now, stuck)
    except OSError as exc:
        log.warning("could not save the tMM trash state: %s", exc)
    return ok, results


# ------------------------------------------------------------------------------- tMM glue
def _tmm_client(cfg: Config):
    from .tmm import TmmClient, read_api_key

    return TmmClient(cfg.tmm, read_api_key(cfg.tmm.api_key_file))


def _tmm_busy(cfg: Config) -> str | None:
    from .tmm import TmmError, TmmUnavailable

    try:
        st = _tmm_client(cfg).status()
    except TmmUnavailable:
        return None  # not running, cannot be renaming anything
    except (TmmError, OSError) as exc:
        log.warning("tMM status unknown (%s); continuing", exc)
        return None
    if st and (st.get("busy") or st.get("mainTaskRunning")):
        return f"busy={st.get('busy')}, queued={st.get('queuedMainTasks')}"
    return None


_TMM_PENDING = "tmm_pending.json"  # library folders filed, per kind, until tMM accepts their call


def _tmm_pending(state_dir: str, moved: dict[str, set[str]]) -> dict[str, set[str]]:
    try:
        with open(os.path.join(state_dir, _TMM_PENDING), encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        data = {}
    pending = {k: set(v) for k, v in data.items() if isinstance(v, list)} if isinstance(data, dict) else {}
    for kind, paths in moved.items():
        pending.setdefault(kind, set()).update(paths)
    return pending


def _tmm_pending_save(state_dir: str, pending: dict[str, set[str]], failure_logged: bool = False) -> bool:
    qpath = os.path.join(state_dir, _TMM_PENDING)
    tmp = qpath + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({k: sorted(v) for k, v in pending.items() if v}, fh)
        os.replace(tmp, qpath)
    except OSError as exc:  # the nightly tmm-sync catches up with what is not recorded
        if not failure_logged:
            log.error("could not record the pending tMM refreshes in %s: %s", qpath, exc)
        return False
    return True


def _tmm_queue(cfg: Config, args, moved: dict[str, set[str]], state_dir: str) -> bool:
    """Record the library folders a run filed, right after the moves: an interrupted run keeps them for
    the next one.  -> False when the list could not be written (logged)."""
    if not cfg.tmm.enabled or args.no_tmm or not any(moved.values()):
        return True
    return _tmm_pending_save(state_dir, _tmm_pending(state_dir, moved))


def _tmm_send(cfg: Config, args, moved: dict[str, set[str]], state_dir: str, save_failed: bool = False) -> None:
    """Send tMM the calls for the recorded folders (TV shows first); keep them if tMM is down.
    `save_failed`: recording them failed in this run already (logged once)."""
    if not cfg.tmm.enabled or args.no_tmm:
        return
    from .tmm import TmmError, TmmUnavailable

    pending = _tmm_pending(state_dir, moved)  # `moved` again: in case recording it failed
    if not any(pending.values()):
        return
    try:
        client = _tmm_client(cfg)
        for kind in ("tvshow", "movie"):
            paths = sorted(pending.get(kind, ()))
            if paths:
                client.refresh(kind, paths)
                log.info("tMM %s refresh queued for %d folder(s)", kind, len(paths))
                pending[kind] = set()
    except TmmUnavailable as exc:
        log.warning("%s; %d folder(s) kept for the next run", exc, sum(len(v) for v in pending.values()))
    except (TmmError, OSError) as exc:
        log.error("tMM refresh failed: %s", exc)
    _tmm_pending_save(state_dir, pending, failure_logged=save_failed)


# ------------------------------------------------------------------------------- others
def cmd_recover(cfg: Config, args) -> int:
    from .guards import LockBusy, RunLock
    from .transfer import reconcile

    from .guards import PreconditionError, check_mount

    journal_dir, _, lock_path = _dirs(cfg)
    try:
        with RunLock(lock_path):
            try:
                check_mount(cfg)
            except PreconditionError as exc:
                log.error("precondition failed: %s", exc)
                return EXIT_PRECONDITION
            resolved, conflicts = reconcile(journal_dir)
    except LockBusy as exc:
        log.warning("%s", exc)
        return EXIT_LOCKED
    for c in conflicts:
        print(f"CONFLICT {c}")
    print(f"reconciled {resolved} record(s); {len(conflicts)} conflict(s)")
    return EXIT_JOURNAL if conflicts else EXIT_OK


def cmd_runs(cfg: Config, args) -> int:
    from .transfer import read_journal

    journal_dir, _, _ = _dirs(cfg)
    names = sorted((n for n in os.listdir(journal_dir) if n.endswith(".jsonl")), reverse=True) \
        if os.path.isdir(journal_dir) else []
    for n in names[: args.limit]:
        begin, ops, end = read_journal(os.path.join(journal_dir, n))
        states = {}
        for r in ops.values():
            states[r.get("state")] = states.get(r.get("state"), 0) + 1
        print(f"{n[:-6]}  {begin.get('kind', '?'):<5} end={end.get('status') if end else 'MISSING'}  {states}")
    return EXIT_OK


def cmd_undo(cfg: Config, args) -> int:
    from .guards import LockBusy, PreconditionError, RunLock, check_mount
    from .transfer import run_undo, undo_plan

    journal_dir, _, lock_path = _dirs(cfg)
    path = os.path.join(journal_dir, f"{args.run_id}.jsonl")
    if not os.path.exists(path):
        log.error("no journal for run %s", args.run_id)
        return EXIT_USAGE
    try:
        with RunLock(lock_path):
            try:
                check_mount(cfg)
            except PreconditionError as exc:
                log.error("precondition failed: %s", exc)
                return EXIT_PRECONDITION
            code = _reconcile_or_exit(journal_dir, dry_run=not args.apply)
            if code is not None:
                return code
            inverse, errors = run_undo(path, bool(args.apply), journal_dir, _run_id() + "-undo")
    except LockBusy as exc:
        log.warning("%s", exc)
        return EXIT_LOCKED
    for op in inverse:
        what = "create (empty file)" if op.kind == "create" else op.kind
        print(f"{'UNDO' if args.apply else 'would undo'}: {what} {op.src + ' -> ' if op.src else ''}{op.dst}")
    for note in undo_plan(path)[1]:
        print(f"note: {note}")
    for e in errors:
        log.error("%s", e)
    if not args.apply:
        print("DRY-RUN: add --apply to reverse these operations")
    return EXIT_OP_FAILED if errors else EXIT_OK


def cmd_identify(args) -> int:
    from .identify import identify_video

    parents = tuple(p for p in (args.folder or "").split("/") if p)  # 'Pack/Season 2' -> season folder rules
    for name in args.names:
        r = identify_video(name, parents[0] if parents else None, not args.multi, parents)
        if r.media:
            print(f"{name}\n    -> {r.media.kind}: {r.media.label()}  [{r.media.source}]"
                  + "".join(f"\n       note: {n}" for n in r.notes))
        else:
            print(f"{name}\n    -> NOT IDENTIFIED: {r.reason}")
    return EXIT_OK


def _whoami() -> str:
    import pwd
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return f"uid {os.geteuid()}"


def cmd_check(cfg: Config, args) -> int:
    """Read-only preflight.  A [FAIL] line makes the exit code 3; [warn] lines are advice."""
    from .config import check_file_permissions
    from .guards import PreconditionError, check_mount, check_paths
    from .transfer import _renameat2

    ok = True

    def line(status, text: str):  # status: True, False or "warn"
        nonlocal ok
        if status is False:
            ok = False
        print(f"[{ {True: 'ok', False: 'FAIL', 'warn': 'warn'}[status] }] {text}")

    line(True, f"mediaorg {__version__}, Python {sys.version.split()[0]}, running as {_whoami()}")
    line(True, f"config {cfg.source_file}")
    try:
        check_file_permissions(cfg.source_file, "the configuration file")
    except ConfigError as exc:
        line(False, str(exc))
    try:
        import guessit
        line(True, f"guessit {guessit.__version__}")
    except ImportError as exc:
        line(False, f"guessit not importable: {exc}")
    line(_renameat2 is not None, "libc renameat2() available")
    try:
        e = check_mount(cfg)
        if e is not None:
            line(True, f"mount {cfg.base_dir}: {e.fstype} {e.source}")
        else:
            from .configcmd import detect_mount
            entry = detect_mount(cfg.paths.movies)
            if entry is not None:
                line("warn", f"no mount check, but the media folders are on the mount {entry.mountpoint} "
                             f"({entry.fstype} {entry.source}); if it were not mounted, mediaorg would see the empty "
                             f"folder below it. Add the check: sudo mediaorg config set "
                             f"mount.mountpoint={entry.mountpoint} mount.fstype={entry.fstype} "
                             f"mount.source={entry.source}")
            else:
                line(True, "no mount check needed: the media folders are on the root filesystem")
        check_paths(cfg)
        line(True, "downloads/movies/series exist, same filesystem, writable by this user")
    except (PreconditionError, OSError) as exc:
        line(False, str(exc))
    try:
        free = _free_mb(cfg.paths.state_dir)
        line(free >= cfg.safety.min_free_mb, f"state_dir {cfg.paths.state_dir}: {free} MB free "
                                             f"(safety.min_free_mb {cfg.safety.min_free_mb})")
    except OSError as exc:
        line(False, f"state_dir {cfg.paths.state_dir}: {exc}")
    if cfg.logging.file:
        d = os.path.dirname(cfg.logging.file)
        line(os.access(d, os.W_OK), f"log file {cfg.logging.file} (rotated at {cfg.logging.max_size_mb} MB, "
                                    f"{cfg.logging.backups} kept)" + ("" if os.access(d, os.W_OK)
                                                                     else f": {d} is not writable"))
    from .service import installed_config, units_up_to_date
    try:
        installed, stale = units_up_to_date(cfg, cfg.source_file)
    except ConfigError as exc:  # a layout the sandbox of the units cannot serve
        line(False, f"systemd units impossible: {exc}")
    else:
        owner = installed_config()
        if not installed:
            line("warn", "systemd timers not installed (after a good dry-run: sudo mediaorg service install --enable)")
        elif owner != cfg.source_file:
            line("warn", f"the installed systemd units run another configuration: {owner}")
        elif stale:
            line("warn", f"systemd units differ from the configuration ({', '.join(stale)}): "
                         f"sudo mediaorg service install")
        else:
            line(True, "systemd units match the configuration")
    from .service import dropins
    schedule_dropins, other_dropins = dropins()
    if schedule_dropins:
        line("warn", f"drop-in files set the timer schedule and override schedule.*: {', '.join(schedule_dropins)} "
                     "(remove them to use the configured schedule; `systemctl cat mediaorg.timer` shows the result)")
    if other_dropins:
        line(True, f"local additions to the units (drop-ins): {', '.join(other_dropins)}")
    if cfg.completeness.method == "freebox":
        try:
            check_file_permissions(cfg.freebox.token_file, "the Freebox app token", secret=True)
        except ConfigError as exc:
            line(False, str(exc))
        from .freebox import FreeboxClient, FreeboxError, load_token, same_remote_dir, unexpected_permissions
        client = FreeboxClient(cfg.freebox.base_url, cfg.freebox.ca_file, cfg.freebox.timeout_seconds)
        try:
            info = client.api_version()
            line(True, f"Freebox API reachable over TLS: api_version {info.get('api_version')}, "
                       f"device {info.get('device_name')}")
        except FreeboxError as exc:
            line(False, str(exc))
        try:
            tok = load_token(cfg.freebox.token_file)
            line(True, f"Freebox app token present ({cfg.freebox.token_file})")
            perms = client.login(tok["app_id"], tok["app_token"])
            try:
                conf = client.download_config()
            finally:
                client.logout()
            extra = unexpected_permissions(perms)
            print(f"[{'ok' if not extra else 'warn'}] Freebox app permissions: "
                  + ("downloader only" if not extra else f"also {extra} - untick them in Freebox OS "
                     "(Gestion des accès > Applications)"))
            ok_dir = bool(cfg.freebox.remote_downloads_dir) and same_remote_dir(
                conf["download_dir"], cfg.freebox.remote_downloads_dir)
            line(ok_dir, f"freebox.remote_downloads_dir {cfg.freebox.remote_downloads_dir!r} matches the "
                         f"Freebox download folder {conf['download_dir']!r}")
            sr = conf["stop_ratio"]
            stop = cfg.completeness.on_seeding == "stop"
            line(isinstance(sr, int) and (sr > 0 or stop),
                 f"BitTorrent stop ratio {format(sr / 100, 'g') if isinstance(sr, int) else sr}"
                 + ((" (0 = seed forever: finished downloads would never move; or set completeness.on_seeding "
                     "= \"stop\")") if sr == 0 and not stop else ""))
            line(True, "completeness.on_seeding = " + (
                "stop: a finished torrent still seeding is stopped when it is filed" if stop
                else "wait: a seeding torrent is filed once the Freebox says done (stop ratio reached)"))
        except FreeboxError as exc:
            line(False, str(exc))
    else:
        line(True, f"completeness: quiescence, {cfg.completeness.min_quiet_minutes} min")
    if cfg.tmm.enabled:
        from .tmm import TmmError, TmmUnavailable
        try:
            check_file_permissions(cfg.tmm.api_key_file, "the tMM API key", secret=True)
        except ConfigError as exc:
            line(False, str(exc))
        try:
            client = _tmm_client(cfg)
            st = client.status()
            line(True, f"tMM reachable: {st if st is not None else 'no /api/system (tMM < 5.3.5)'}")
            paths = client.api_paths()
            if paths is None:
                print("[warn] tMM has no /api/docs (tMM < 5.3.3): endpoint names not verified")
            else:
                for ep in (cfg.tmm.tvshow_endpoint, cfg.tmm.movie_endpoint):
                    line(ep in paths, f"tMM endpoint /api/{ep} listed in /api/docs"
                                      + ("" if ep in paths else f" (listed: {sorted(paths)})"))
        except (TmmError, TmmUnavailable, OSError) as exc:
            line(False, f"tMM: {exc}")
    _check_seeding_and_staging(cfg, line)
    return EXIT_OK if ok else EXIT_PRECONDITION


STAGING_GRACE_SECONDS = 86400  # a staged folder older than this: tMM has evidently not renamed it


def _has_video(folder: str, exts, depth: int = 3) -> bool:
    """Does `folder` hold a file with a video extension, at most `depth` levels down?"""
    base = folder.rstrip(os.sep).count(os.sep)
    for d, dirs, files in os.walk(folder):
        if any(os.path.splitext(n)[1].lower() in exts for n in files):
            return True
        if d.rstrip(os.sep).count(os.sep) - base >= depth - 1:
            dirs[:] = []
    return False


def _staged_folders(cfg: Config, older_than: float = STAGING_GRACE_SECONDS) -> tuple[list[str], list[str]]:
    """Library folders that still carry the staging tag (paths relative to the media folder): (those with a
    video, unchanged for `older_than` seconds: tMM has not renamed them; those without any video file, which
    tMM ignores)."""
    from .normalize import is_staged

    now, stale, empty = time.time(), [], []
    for root in (cfg.paths.movies, cfg.paths.series):
        try:
            with os.scandir(root) as it:
                for e in it:
                    try:
                        if not (e.is_dir(follow_symlinks=False) and is_staged(e.name)):
                            continue
                        rel = os.path.relpath(e.path, cfg.base_dir)
                        if not _has_video(e.path, cfg.media.video_extensions):
                            empty.append(rel)
                        elif now - e.stat(follow_symlinks=False).st_mtime >= older_than:
                            stale.append(rel)
                    except OSError:
                        continue
        except OSError:
            continue
    return sorted(stale), sorted(empty)


def _check_seeding_and_staging(cfg: Config, line) -> None:
    """completeness.on_seeding without the Freebox; naming.tmm_staging: whether it is needed (tMM + an SMB/CIFS
    library), and staged folders tMM left alone."""
    from .configcmd import detect_mount
    from .normalize import STAGING_TAG

    if cfg.completeness.on_seeding == "stop" and cfg.completeness.method != "freebox":
        line("warn", "completeness.on_seeding = stop has no effect: it needs completeness.method = \"freebox\"")
    staging = cfg.naming.tmm_staging
    if staging and not cfg.tmm.enabled:
        line("warn", "naming.tmm_staging is set but tMM is disabled: new folders are named without the tag")
    elif staging:
        line(True, f"naming.tmm_staging: new movie and show folders end with ' {STAGING_TAG}' until tMM renames them")
    elif cfg.tmm.enabled:
        try:
            entry = detect_mount(cfg.paths.movies)
        except OSError:  # reported by the folder checks above
            entry = None
        if entry is not None and entry.fstype in ("cifs", "smb3", "smbfs"):
            line("warn", f"the library is on an SMB share ({entry.fstype}): tMM on Linux cannot rename a folder whose "
                         f"new name differs only in letter case; to avoid it: sudo mediaorg config set "
                         f"naming.tmm_staging=true")
    stale, empty = _staged_folders(cfg)

    def shown(paths: list[str]) -> str:
        return ", ".join(repr(s) for s in paths[:3]) + (f" (+{len(paths) - 3} more)" if len(paths) > 3 else "")

    if stale:
        line("warn", f"{len(stale)} folder(s) still end with ' {STAGING_TAG}' after a day: tMM has not renamed them "
                     f"(scrape and rename them in tMM; is folder renaming on in tMM's renamer settings?): "
                     f"{shown(stale)}")
    if empty:
        line("warn", f"{len(empty)} folder(s) ending with ' {STAGING_TAG}' hold no video file, so tMM ignores them "
                     f"(put the video back, or delete the folder): {shown(empty)}")


def cmd_library(cfg: Config, args) -> int:
    """Read-only: how the tool reads the Movies/Series library (seasons, episodes, anomalies)."""
    from .guards import PreconditionError, check_mount
    from .inventory import build, render
    from .library import Library, LibraryUnstable

    try:
        check_mount(cfg)
        for p in (cfg.paths.series, cfg.paths.movies):
            if not os.path.isdir(p):
                raise PreconditionError(f"{p} is not a directory")
        inv = build(Library(cfg.paths.movies, cfg.paths.series, cfg.media.video_extensions))
    except (PreconditionError, OSError, LibraryUnstable) as exc:
        log.error("cannot read the library: %s", exc)
        return EXIT_PRECONDITION
    print(render(inv, cfg.paths.series, cfg.paths.movies))
    return EXIT_OK


def cmd_probe_rename(cfg: Config, args) -> int:
    """Prove on the real mount that the move primitive never replaces an existing file."""
    import errno as _errno
    from .guards import PreconditionError, check_mount
    from .transfer import _move, rename_noreplace, reserve
    from .model import Op

    class _NullJournal:
        def write(self, rec):
            pass

    try:
        check_mount(cfg)
    except PreconditionError as exc:
        log.error("precondition failed: %s", exc)
        return EXIT_PRECONDITION
    base = os.path.join(cfg.paths.review, ".probe")
    print(f"will create {base}/ with small test files, test moves, then remove only those files")
    if not args.apply:
        print("DRY-RUN: add --apply to run the probe")
        return EXIT_OK
    os.makedirs(base, exist_ok=False)
    a, b, c = (os.path.join(base, n) for n in ("probe-a.txt", "probe-b.txt", "probe-c.txt"))
    upper = os.path.join(base, "PROBE-B.TXT")
    results = []
    try:
        for p, text in ((a, "A"), (b, "B")):
            with open(p, "x", encoding="ascii") as fh:
                fh.write(text)

        def refused(fn, *xs):
            try:
                fn(*xs)
                return False, "allowed"
            except OSError as exc:
                return True, _errno.errorcode.get(exc.errno, str(exc.errno))

        ok, how = refused(reserve, b)
        results.append((f"exclusive create of an existing name is refused ({how})", ok, True))
        ok, how = refused(reserve, upper)
        results.append((f"... also with different letter case ({how}) [informational: the planner also "
                        "compares names case-insensitively]", ok, False))
        ok, how = refused(_move, Op("rename", src=a, dst=b), _NullJournal(), 0)
        with open(b, encoding="ascii") as fh:
            intact = fh.read() == "B"
        results.append((f"mediaorg move onto an existing file is refused ({how}), target intact", ok and intact, True))
        ok, how = refused(rename_noreplace, a, b)
        results.append((f"kernel RENAME_NOREPLACE check refuses ({how}) [informational]", ok, False))
        _move(Op("rename", src=a, dst=c), _NullJournal(), 0)
        with open(c, encoding="ascii") as fh:
            moved = fh.read() == "A"
        results.append(("move to a free name works and keeps content", moved and not os.path.exists(a), True))
        if moved:
            a = c
    finally:
        for p in (a, b, c, upper):
            if os.path.isfile(p):
                with open(p, encoding="ascii") as fh:  # only remove our own probe files
                    ours = fh.read() in ("A", "B", "")
                if ours:
                    os.unlink(p)
        if not os.listdir(base):
            os.rmdir(base)
    for text, good, required in results:
        print(f"[{'ok' if good else ('FAIL' if required else 'note')}] {text}")
    return EXIT_OK if all(g for _, g, req in results if req) else EXIT_PRECONDITION


def cmd_freebox_authorize(cfg: Config, args) -> int:
    from .freebox import FreeboxClient, FreeboxError, authorize_interactive

    import socket

    client = FreeboxClient(cfg.freebox.base_url, cfg.freebox.ca_file, cfg.freebox.timeout_seconds)
    try:
        authorize_interactive(client, cfg.freebox.token_file, cfg.freebox.app_id, cfg.freebox.app_name,
                              __version__, cfg.freebox.device_name or socket.gethostname())
    except FreeboxError as exc:
        log.error("%s", exc)
        return EXIT_PRECONDITION
    print(f"granted; token saved to {cfg.freebox.token_file} (mode 0600).\n"
          "In Freebox OS (Gestion des accès > Applications), leave only the download manager permission "
          "('Accès au gestionnaire de téléchargements') for this app.\n"
          "next: sudo mediaorg freebox status    (shows the folder to put in freebox.remote_downloads_dir)")
    return EXIT_OK


def cmd_freebox_status(cfg: Config, args) -> int:
    from .freebox import FreeboxClient, FreeboxError, load_token

    try:
        tok = load_token(cfg.freebox.token_file)
        client = FreeboxClient(cfg.freebox.base_url, cfg.freebox.ca_file, cfg.freebox.timeout_seconds)
        perms = client.login(tok["app_id"], tok["app_token"])
        try:
            conf = client.download_config()
            tasks = client.tasks()
        finally:
            client.logout()
    except FreeboxError as exc:
        log.error("%s", exc)
        return EXIT_PRECONDITION
    granted = sorted(k for k, v in perms.items() if v)
    print(f"permissions granted to this app: {granted}")
    sr = conf["stop_ratio"]
    print(f"download folder (freebox.remote_downloads_dir): {conf['download_dir']!r}")
    print(f"BitTorrent stop ratio: {format(sr / 100, 'g') if isinstance(sr, int) else sr}"
          + ("  (0 = seed until stopped by hand)" if sr == 0 else ""))
    print(f"watch folder: {conf['watch_dir']!r} ({'enabled' if conf['use_watch_dir'] else 'disabled'})")
    dirs = {}
    for t in tasks:
        dirs[t.download_dir] = dirs.get(t.download_dir, 0) + 1
        ratio = f"{t.stop_ratio / 100:.2f}" if isinstance(t.stop_ratio, int) else "-"
        print(f"{t.id:>5}  {t.status:<11} {t.rx_pct / 100:6.1f}%  stop_ratio {ratio:>5}  {t.type:<4} "
              f"{t.download_dir}{t.name}")
    print(f"tasks: {len(tasks)}; download_dir values seen:")
    for d, n in sorted(dirs.items()):
        print(f"    {d!r}  ({n} task(s))")
    from .freebox import same_remote_dir
    if not same_remote_dir(conf["download_dir"], cfg.freebox.remote_downloads_dir or ""):
        print(f"\nfreebox.remote_downloads_dir is {cfg.freebox.remote_downloads_dir!r}; to match the Freebox:\n"
              f"  sudo mediaorg config set freebox.remote_downloads_dir={_shell_quote(conf['download_dir'])}")
    return EXIT_OK


def _shell_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def cmd_tmm_status(cfg: Config, args) -> int:
    from .tmm import TmmError, TmmUnavailable

    try:
        st = _tmm_client(cfg).status()
    except (TmmError, TmmUnavailable, OSError) as exc:
        log.error("%s", exc)
        return EXIT_PRECONDITION
    print(json.dumps(st, indent=1) if st is not None else "tMM answered but has no /api/system (version < 5.3.5)")
    return EXIT_OK


def cmd_tmm_sync(cfg: Config, args) -> int:
    from .tmm import TmmError, TmmUnavailable, render

    if not cfg.tmm.enabled:
        log.warning("tMM integration is disabled ([tmm] enabled = false); nothing sent")
        return EXIT_OK

    for kind in ("tvshow", "movie"):
        tmpl = cfg.tmm.full_sync_tvshow if kind == "tvshow" else cfg.tmm.full_sync_movie
        print(f"{kind}: {json.dumps(render(tmpl, []))}")
    if not args.apply:
        print("DRY-RUN: add --apply to send these commands")
        return EXIT_OK
    try:
        client = _tmm_client(cfg)
        for kind in ("tvshow", "movie"):
            client.full_sync(kind)
            print(f"{kind}: accepted")
    except (TmmError, TmmUnavailable, OSError) as exc:
        log.error("%s", exc)
        return EXIT_PRECONDITION
    return EXIT_OK


# ------------------------------------------------------------------------------- status
def cmd_status(args) -> int:
    """Version, configuration, timers and the latest run at a glance (read-only)."""
    from .configcmd import config_path
    from .service import status_lines

    path = config_path(args)
    print(f"mediaorg {__version__} (Python {sys.version.split()[0]})")
    cfg = None
    try:
        cfg = load(path)
    except ConfigError as exc:
        print(f"configuration: {exc}")
    if cfg is not None:
        c = cfg.completeness
        print(f"configuration: {path}")
        print(f"  downloads  {cfg.paths.downloads}\n  movies     {cfg.paths.movies}\n  tv         {cfg.paths.series}\n"
              f"  review     {cfg.paths.review}\n  state      {cfg.paths.state_dir}")
        print("  finished downloads: " + (("reported done by the Freebox"
                                           + ("; a torrent still seeding is stopped when it is filed"
                                              if c.on_seeding == "stop" else ""))
                                          if c.method == "freebox" else f"unchanged for {c.min_quiet_minutes} min"))
        print("  mount check: " + (f"{cfg.mount.mountpoint} from {cfg.mount.source} ({cfg.mount.fstype})"
                                   if cfg.mount else "none"))
        print("  tinyMediaManager: " + (f"enabled ({cfg.tmm.base_url})" if cfg.tmm.enabled else "disabled")
              + ("; new folders end with ' [mediaorg]' until tMM renames them"
                 if cfg.tmm.enabled and cfg.naming.tmm_staging else ""))
        r = cfg.review
        if r.delete_duplicates:
            dup = "deleted when the library already has them; a bigger or repaired copy is kept in review"
        else:
            days = r.duplicates_retention_days
            dup = (("byte-identical copies deleted; " if r.delete_identical_duplicates else "")
                   + "others moved to review" + ("" if days < 0 else ", the certain ones deleted "
                                                 + ("at the end of the run" if days == 0 else f"after {days} day(s)")))
        print(f"  duplicates: {dup}")
    print("systemd:")
    for line in status_lines():
        print(f"  {line}")
    if cfg is None:
        return EXIT_USAGE
    journal_dir, runs_dir, _ = _dirs(cfg)
    try:
        reports = sorted(n for n in os.listdir(runs_dir) if n.endswith(".json"))
        journals = [n for n in os.listdir(journal_dir) if n.endswith(".jsonl")] if os.path.isdir(journal_dir) else []
        size = sum(os.path.getsize(os.path.join(runs_dir, n)) for n in reports)
        print(f"state: {len(reports)} report(s) ({size // 1024} KiB), {len(journals)} journal(s), "
              f"{_free_mb(cfg.paths.state_dir)} MB free")
        if reports:
            last = _load_json(os.path.join(runs_dir, reports[-1]))  # {} when unreadable (cut short by a crash)
            meta = last.get("meta") if isinstance(last.get("meta"), dict) else {}
            summ = last.get("summary") if isinstance(last.get("summary"), dict) else {}
            counts = summ.get("items") if isinstance(summ.get("items"), dict) else {}
            items = ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
            print(f"latest report: {meta.get('run_id', reports[-1][:-5])} "
                  f"({'apply' if meta.get('apply') else 'dry-run'}): items {items}; renames {summ.get('renames', 0)}")
        pending = _load_json(os.path.join(cfg.paths.state_dir, "tmm_pending.json"))
        n = sum(len(v) for v in pending.values() if isinstance(v, list))
        if n:
            print(f"tMM: {n} library folder(s) waiting to be sent (tMM was not reachable)")
    except FileNotFoundError:
        print("state: no run yet")
    except PermissionError:
        print(f"state: {cfg.paths.state_dir} is not readable by {_whoami()} (use sudo)")
    return EXIT_OK


# ------------------------------------------------------------------------------- command line
_TOP_DESCRIPTION = f"""\
mediaorg {__version__} - files finished movie and TV downloads into a tidy library, safely.

It watches one Downloads folder. Each finished download is identified (title, year, season,
episode) and moved by an instant rename into
    <movies>/Title (Year)/...      and      <tv>/Show (Year)/Season N/...
Nothing is ever overwritten; whatever is uncertain stays where it is and is reported.
tinyMediaManager can then scrape and rename what was filed. Commands that change files
are dry-runs unless --apply is given."""

_COMMANDS = """\
daily operation
  run              plan the filing of finished downloads; --apply carries it out
  status           version, configuration, timers and the latest run at a glance
  check            read-only preflight: storage, permissions, mount, Freebox, tMM
  runs             recent runs (each one has a journal: it can be undone)
  undo RUN_ID      reverse a run (dry-run unless --apply)
  recover          reconcile the journal of an interrupted run

inspection (read-only)
  identify NAME    how a release name is read: title, year, season, episode
  library          how the library is read: seasons, episodes, anomalies
  probe-rename     prove on the real storage that a move never replaces a file

setup
  config           the configuration: init, set, unset, get, show, keys, validate, path
  service          systemd timers: install [--enable], remove, status, show
  freebox          Freebox download manager: authorize, status
  tmm              tinyMediaManager: set-key, status, sync

`mediaorg COMMAND --help` explains each command, with examples."""

_TOP_EPILOG = f"""\
quick start
  sudo mediaorg config init --downloads /srv/media/downloads \\
                            --movies /srv/media/movies --series /srv/media/tv
  sudo mediaorg check                       # read-only
  sudo mediaorg run                         # dry-run: what would happen
  sudo mediaorg run --apply                 # once by hand
  sudo mediaorg service install --enable    # then every 5 minutes, also after a reboot

privileges
  Use sudo. Commands that read or move media switch to the unprivileged '{SERVICE_USER}'
  account before they do anything. Setup commands (config init/set/unset, tmm set-key,
  service install/remove) write root-owned files. Secrets are never passed as arguments.

files
  {DEFAULT_CONFIG}      configuration (0640 root:{SERVICE_USER}); -c FILE or $MEDIAORG_CONFIG
  /etc/mediaorg/tmm.key          tinyMediaManager API key (written by `tmm set-key`)
  /var/lib/mediaorg/             journals, reports, lock, Freebox token (paths.state_dir)
  /etc/systemd/system/mediaorg*  units written by `service install`

exit codes
  0 ok, 1 an operation failed, 2 usage or configuration error, 3 precondition (storage
  not mounted or not answering, download state unknown, too little space), 4 another run
  holds the lock, 5 an interrupted run needs a person (then `recover`), 6 plan above
  safety.max_moves_per_run, 130 interrupted (the next run reconciles)

documentation: https://github.com/drake4m/FreeMediaPlexOrg"""

_RUN_EPILOG = """\
what the report shows
  [READY]    will be (or was) filed; [PARTIAL] some files filed, the rest stays
  [WAIT]     not finished yet (still downloading, seeding, or changed recently)
  [SKIP]     not identified with certainty: left in Downloads, the reason is given
  [IGNORED]  not a video download (software, documents ...): never touched
  [PURGE] [KEPT] [TRASH]  review folders and tMM's trash cleaned (or kept) by retention

examples
  sudo mediaorg run                               # dry-run of everything
  sudo mediaorg run --apply                       # do it
  sudo mediaorg run --apply --only "Some.Show.S01E02.1080p.WEB.mkv"
  sudo mediaorg run --apply --max-moves 200       # after a long pause

A duplicate of something already in the library is never deleted unless it is byte-for-byte
identical; other duplicates go to the review folder."""

_INIT_EPILOG = f"""\
The folders must exist and be on one filesystem. Missing values are asked for when run in a
terminal. A mount check ([mount]) is added automatically when the media folders are on their
own mount (NAS share, USB disk); --mount none skips it. The file is written to
{DEFAULT_CONFIG} (0640 root:{SERVICE_USER}); an existing file needs --force and is kept as .bak.

examples
  # any download client, or files copied by hand (watched folder)
  sudo mediaorg config init --downloads /srv/media/downloads \\
       --movies /srv/media/movies --series /srv/media/tv

  # a Freebox share mounted on /mnt/freebox, finished downloads reported by the Freebox
  sudo mediaorg config init --mode freebox --downloads /mnt/freebox/Downloads \\
       --movies /mnt/freebox/Movies --series /mnt/freebox/Series

  # with tinyMediaManager in a container that sees the library under /media
  sudo mediaorg config init ... --tmm \\
       --tmm-path-map /srv/media/movies=/media/movies --tmm-path-map /srv/media/tv=/media/series"""

_SET_EPILOG = """\
Values are checked, the whole configuration is validated, then the file is rewritten with its
documentation and the previous version kept as .bak. Units that depend on the change (folders,
mount, schedule) are rewritten too when they are installed. List keys: mediaorg config keys --all

examples
  sudo mediaorg config set paths.movies=/srv/media/movies paths.series=/srv/media/tv
  sudo mediaorg config set completeness.method=quiescence completeness.min_quiet_minutes=15
  sudo mediaorg config set schedule.run_every_minutes=10
  sudo mediaorg config set review.tmm_trash_retention_minutes=30
  sudo mediaorg config set mount.mountpoint=/mnt/nas mount.fstype=cifs mount.source=//nas/media
  sudo mediaorg config set media.video_extensions=.mkv,.mp4,.avi,.m4v,.ts
  sudo mediaorg config set 'tmm.path_map=/srv/media/movies=/media/movies,/srv/media/tv=/media/series'
  sudo mediaorg config set 'tmm.after_move_tvshow=[{action="update", scope={name="all"}}]'
  sudo mediaorg config set logging.file=/var/log/mediaorg/mediaorg.log
  sudo mediaorg config set paths.review=            # empty: back to the default"""

_SERVICE_EPILOG = """\
install writes mediaorg.service and mediaorg.timer (and, when tMM is enabled with a sync time,
mediaorg-tmm-sync.service/.timer) from the configuration: folders it may write, mounts it needs,
schedule, and a strict sandbox. It is safe to run again; only changed units are rewritten.

examples
  sudo mediaorg service install --enable    # write the units, enable and start the timers
  sudo mediaorg service status
  sudo journalctl -u mediaorg.service -f    # follow the runs
  sudo mediaorg service remove              # stop and remove the timers (files are untouched)"""

_TMM_EPILOG = """\
Enable tMM's HTTP API in tinyMediaManager (settings, HTTP API: enable, port, API key), then:
  sudo mediaorg tmm set-key                         # paste the key (hidden input)
  sudo mediaorg tmm set-key < keyfile               # or from standard input
  sudo mediaorg tmm set-key --from-tmm-json /path/to/tmm/data/tmm.json
  sudo mediaorg config set tmm.enabled=true
  sudo mediaorg tmm status
  sudo mediaorg tmm sync --apply                    # what the nightly timer sends"""

_FREEBOX_EPILOG = """\
  sudo mediaorg config set completeness.method=freebox
  sudo mediaorg freebox authorize      # then confirm on the Freebox Server display
  sudo mediaorg freebox status         # download folder, stop ratio, tasks
  sudo mediaorg config set freebox.remote_downloads_dir=/Freebox/Downloads"""

_ALIASES = {"tmm-sync": ["tmm", "sync"], "tmm-status": ["tmm", "status"],
            "freebox-authorize": ["freebox", "authorize"], "freebox-status": ["freebox", "status"]}


def _translate_aliases(argv: list[str]) -> list[str]:
    """Command names of 1.x (tmm-sync, freebox-status ...) keep working."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-c", "--config"):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if tok in _ALIASES:
            return argv[:i] + _ALIASES[tok] + argv[i + 1:]
        break
    return argv


class _Formatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=30, width=min(100, shutil.get_terminal_size((100, 20)).columns))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", metavar="FILE", default=argparse.SUPPRESS,
                        help=f"configuration file (default {DEFAULT_CONFIG} or $MEDIAORG_CONFIG)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="more detail in the log")

    p = argparse.ArgumentParser(prog="mediaorg", description=_TOP_DESCRIPTION, epilog=_TOP_EPILOG,
                                formatter_class=_Formatter)
    p.add_argument("--version", action="version", version=f"mediaorg {__version__}")
    p.add_argument("-c", "--config", metavar="FILE",
                   help=f"configuration file (default {DEFAULT_CONFIG} or $MEDIAORG_CONFIG)")
    p.add_argument("-v", "--verbose", action="store_true", help="more detail in the log")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND", title="commands", description=_COMMANDS)

    def add(name, summary, **kw):
        return sub.add_parser(name, parents=[common], description=summary, formatter_class=_Formatter, **kw)

    r = add("run", "Scan Downloads, identify each entry, check it is finished and plan where each file goes.\n"
                   "Without --apply nothing changes. With --apply the plan is carried out and journaled,\n"
                   "review folders and tMM's trash are cleaned by their retention, and tMM is told.",
            epilog=_RUN_EPILOG)
    r.add_argument("--apply", action="store_true", help="carry out the plan (default: dry-run)")
    r.add_argument("--only", nargs="+", metavar="NAME", help="only these top-level Downloads entries")
    r.add_argument("--max-moves", type=int, metavar="N", help="allow up to N moves and deletions in this run "
                                                               "(default safety.max_moves_per_run)")
    r.add_argument("--no-tmm", action="store_true", help="do not contact tinyMediaManager in this run")
    r.add_argument("--quiet", action="store_true",
                   help="for timers: print only what is acted on or changed since the previous --apply --quiet "
                        "run; nothing at all when nothing changed")

    add("status", "Show the version, the main settings, the systemd timers and the latest run (read-only).")
    add("check", "Read-only preflight: configuration and file permissions, guessit, mount, folders (exist, one "
                 "filesystem,\nwritable by the service account), free space, systemd units, Freebox, tMM. "
                 "Exit 0 when everything\nrequired is fine, 3 otherwise; [warn] lines are advice.")
    lr = add("runs", "List recent runs with their result (newest first).")
    lr.add_argument("--limit", type=int, default=20, metavar="N", help="how many (default 20)")
    u = add("undo", "Reverse a run: moves are undone newest first and never overwrite anything. A download that "
                    "was\ndeleted as a byte-identical copy is not restored (the library file holds its bytes).",
            epilog="example:\n  sudo mediaorg runs\n  sudo mediaorg undo 20260115T101500Z-ab12\n"
                   "  sudo mediaorg undo 20260115T101500Z-ab12 --apply")
    u.add_argument("run_id", metavar="RUN_ID", help="as listed by `mediaorg runs`")
    u.add_argument("--apply", action="store_true", help="carry it out (default: show what would be undone)")
    add("recover", "Reconcile the journal of an interrupted run with what is on disk. Every run does this first; "
                   "use it\nafter exit code 5, once you fixed the paths it names.")

    i = add("identify", "Show how release names are read; no file is accessed.",
            epilog='examples:\n  mediaorg identify "Some.Show.S02E05.1080p.WEB.h264-GRP.mkv"\n'
                   '  mediaorg identify "05 - Pilot.mkv" --folder "Some Show Complete/Season 2"')
    i.add_argument("names", nargs="+", metavar="NAME", help="file or folder names")
    i.add_argument("--folder", metavar="PATH", help="Downloads folder holding the file, as context; a path for files "
                                                    "in sub-folders")
    i.add_argument("--multi", action="store_true", help="the folder holds several main videos")
    add("library", "Show how the library is read: shows, seasons, episodes, and anomalies such as empty files "
                   "(read-only).")
    pr = add("probe-rename", "Prove on the real storage that the move primitive never replaces an existing file: "
                             "creates\n<review>/.probe with three tiny files, tries moves onto them, removes only "
                             "its own files.")
    pr.add_argument("--apply", action="store_true", help="run the probe (default: describe it)")

    # config
    cp = add("config", "Create, change and inspect the configuration.",
             epilog="subcommands: init, set, unset, get, show, keys, validate, path\n"
                    "`mediaorg config SUBCOMMAND --help` for details.")
    csub = cp.add_subparsers(dest="config_cmd", metavar="SUBCOMMAND", required=True)

    def cadd(name, summary, **kw):
        return csub.add_parser(name, parents=[common], description=summary, formatter_class=_Formatter, **kw)

    ci = cadd("init", "Write a new configuration from a few answers (interactive in a terminal).",
              epilog=_INIT_EPILOG)
    ci.add_argument("--downloads", metavar="DIR", help="folder where finished downloads appear")
    ci.add_argument("--movies", metavar="DIR", help="movie library")
    ci.add_argument("--series", metavar="DIR", help="TV library")
    ci.add_argument("--review", metavar="DIR", help="review folder (default <downloads>/_review)")
    ci.add_argument("--state-dir", metavar="DIR", help="local state folder (default /var/lib/mediaorg)")
    ci.add_argument("--mode", choices=("watch", "freebox"),
                    help="watch: a download is finished when its files stop changing (any client); "
                         "freebox: when the Freebox says so")
    ci.add_argument("--quiet-minutes", type=int, metavar="N", help="watch mode: minutes without change (default 10)")
    ci.add_argument("--freebox-downloads-dir", metavar="PATH",
                    help="freebox mode: the Freebox's own path of the downloads folder (see `freebox status`)")
    ci.add_argument("--mount", choices=("auto", "none"), default="auto",
                    help="auto: add a mount check when the folders are on their own mount (default)")
    ci.add_argument("--tmm", action="store_true", help="enable tinyMediaManager calls")
    ci.add_argument("--tmm-url", metavar="URL", help="tMM HTTP API (default http://127.0.0.1:7878)")
    ci.add_argument("--tmm-path-map", action="append", metavar="HOST=TMM",
                    help="a library path and the same path as tMM sees it (repeatable)")
    ci.add_argument("--every", type=int, metavar="MINUTES", help="how often the timer runs (default 5)")
    ci.add_argument("--force", action="store_true", help="replace an existing configuration (kept as .bak)")
    ci.add_argument("--non-interactive", action="store_true", help="never ask; fail when a value is missing")

    cs = cadd("set", "Change one or more keys: KEY=VALUE [KEY=VALUE ...] (or KEY VALUE).", epilog=_SET_EPILOG)
    cs.add_argument("assignments", nargs="+", metavar="KEY=VALUE")
    cu = cadd("unset", "Put keys back to their default.",
              epilog="example:\n  sudo mediaorg config unset review.leftovers_retention_days schedule.tmm_sync_time")
    cu.add_argument("keys", nargs="+", metavar="KEY")
    cg = cadd("get", "Print the value in force for one key (defaults included).",
              epilog="example:\n  mediaorg config get paths.downloads")
    cg.add_argument("key", metavar="KEY")
    csh = cadd("show", "Print the configuration in force, defaults included (or the file itself with --file).")
    csh.add_argument("--file", action="store_true", help="print the file as written, with its comments")
    ck = cadd("keys", "List the keys with their type, default and meaning.",
              epilog="examples:\n  mediaorg config keys\n  mediaorg config keys tmm\n  mediaorg config keys --all")
    ck.add_argument("pattern", nargs="?", metavar="TEXT", help="only keys containing TEXT")
    ck.add_argument("--all", action="store_true", help="include advanced keys")
    cadd("validate", "Check the configuration file (exit 0 when valid, 2 otherwise).")
    cadd("path", "Print the path of the configuration file in use.")

    # service
    sp = add("service", "Install, remove and inspect the systemd timers.", epilog=_SERVICE_EPILOG)
    ssub = sp.add_subparsers(dest="service_cmd", metavar="SUBCOMMAND", required=True)
    si = ssub.add_parser("install", parents=[common], formatter_class=_Formatter,
                         description="Write the units from the configuration (root).")
    si.add_argument("--enable", action="store_true", help="also enable and start the timers")
    ssub.add_parser("remove", parents=[common], formatter_class=_Formatter,
                    description="Stop the timers and remove the units (root). Media and state are untouched.")
    ssub.add_parser("status", parents=[common], formatter_class=_Formatter, description="State of the units.")
    ssub.add_parser("show", parents=[common], formatter_class=_Formatter,
                    description="Print the units the configuration gives, without installing them.")

    # freebox
    fp = add("freebox", "Freebox download manager (completeness.method = \"freebox\").", epilog=_FREEBOX_EPILOG)
    fsub = fp.add_subparsers(dest="freebox_cmd", metavar="SUBCOMMAND", required=True)
    fsub.add_parser("authorize", parents=[common], formatter_class=_Formatter,
                    description="Register mediaorg on the Freebox once; confirm on the Freebox Server display. "
                                "The token is\nsaved in freebox.token_file (0600).")
    fsub.add_parser("status", parents=[common], formatter_class=_Formatter,
                    description="Show the download folder, the stop ratio and the download tasks (read-only).")

    # tmm
    tp = add("tmm", "tinyMediaManager: API key, status, and the nightly sync.", epilog=_TMM_EPILOG)
    tsub = tp.add_subparsers(dest="tmm_cmd", metavar="SUBCOMMAND", required=True)
    tk = tsub.add_parser("set-key", parents=[common], formatter_class=_Formatter,
                         description="Store tMM's API key in tmm.api_key_file (0640 root:mediaorg). The key is read "
                                     "from a hidden\nprompt or standard input, never from the command line.")
    src = tk.add_mutually_exclusive_group()
    src.add_argument("--generate", action="store_true", help="create a new random key and print it once, to enter "
                                                             "in tMM")
    src.add_argument("--from-tmm-json", metavar="FILE", help="read httpApiKey from tMM's settings file tmm.json")
    tsub.add_parser("status", parents=[common], formatter_class=_Formatter,
                    description="Ask tMM for its status (needs tMM 5.3.5 or later for details).")
    ts = tsub.add_parser("sync", parents=[common], formatter_class=_Formatter,
                         description="Send the nightly calls: update the library, scrape and rename new items "
                                     "(TV shows, then movies).")
    ts.add_argument("--apply", action="store_true", help="send them (default: print them)")
    return p


def _safe_stdio() -> None:
    """Never crash on file names that are not valid UTF-8: print them escaped."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")


_MEDIA_HANDLERS = {  # command -> handler name, looked up when called
    "run": "cmd_run", "recover": "cmd_recover", "runs": "cmd_runs", "undo": "cmd_undo", "check": "cmd_check",
    "library": "cmd_library", "probe-rename": "cmd_probe_rename",
}


def _dispatch(args) -> int:
    from . import configcmd

    cmd = args.cmd
    if cmd == "identify":
        return cmd_identify(args)
    if cmd == "config":
        sub = args.config_cmd
        if sub in ("init", "set", "unset"):
            refused = configcmd.guard_admin(configcmd.config_path(args))
            if refused is not None:
                return refused
        return {"init": configcmd.cmd_init, "set": configcmd.cmd_set, "unset": configcmd.cmd_unset,
                "get": configcmd.cmd_get, "show": configcmd.cmd_show, "keys": configcmd.cmd_keys,
                "validate": configcmd.cmd_validate, "path": configcmd.cmd_path}[sub](args)
    if cmd == "service":
        from .service import cmd_service

        def loader():
            path = configcmd.config_path(args)
            return load(path), path
        return cmd_service(loader, args)
    if cmd == "status":
        return cmd_status(args)
    if cmd == "tmm" and args.tmm_cmd == "set-key":
        return configcmd.cmd_tmm_set_key(args)
    # everything else reads or moves media, or talks to the Freebox / tMM: as the service account
    from .system import drop_to_service_user, is_root
    path = configcmd.config_path(args)  # absolute, before the switch of account changes the current folder
    if is_root():
        drop_to_service_user()
    cfg = load(path)
    _add_file_logging(cfg)
    if cmd == "freebox":
        name = {"authorize": "cmd_freebox_authorize", "status": "cmd_freebox_status"}[args.freebox_cmd]
    elif cmd == "tmm":
        name = {"status": "cmd_tmm_status", "sync": "cmd_tmm_sync"}[args.tmm_cmd]
    else:
        name = _MEDIA_HANDLERS[cmd]
    return globals()[name](cfg, args)


def main(argv: list[str] | None = None) -> int:
    _safe_stdio()
    parser = build_parser()
    args = parser.parse_args(_translate_aliases(list(sys.argv[1:] if argv is None else argv)))
    for name, default in (("config", None), ("verbose", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    _setup_logging(args.verbose)
    if args.cmd is None:
        parser.print_help()
        return EXIT_USAGE
    from .system import PrivilegeError
    try:
        return _dispatch(args)
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        return EXIT_USAGE
    except PrivilegeError as exc:
        log.error("%s", exc)
        return EXIT_USAGE
    except ImportError as exc:
        log.error("installation is broken (%s); stopped - run install.sh again", exc)
        return EXIT_USAGE
    except KeyboardInterrupt:
        log.error("interrupted; if files were being moved, the next run reconciles the journal")
        return EXIT_INTERRUPTED
    except OSError as exc:  # e.g. the local disk is full: a clear message instead of a traceback
        log.error("I/O error: %s; if files were being moved, the next run reconciles the journal", exc)
        return EXIT_OP_FAILED


if __name__ == "__main__":
    sys.exit(main())

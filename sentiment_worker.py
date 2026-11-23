"""The sentiment curator: finish marked files while McTrader is running.

A file marked ``<SYMBOL>.new.json`` has articles that still need reading.
For each one this worker:

1. drops every article older than three months and every article that is not
   clearly about the symbol (see :mod:`relevance`);
2. scores the newest fifteen survivors, one request at a time, with the model
   ``llama-server`` reports as loaded at ``127.0.0.1:8080`` (see
   :mod:`llama_client`);
3. writes the mean, a checkable reason and up to ten evidence lines into the
   file's ``scores[local]`` entry — leaving the production reading alone;
4. validates the finished payload against ``schema.json`` and renames the
   marked file to its unmarked name (see :mod:`store`).

It normally runs inside the ``SentimentWorker`` Windows service; the
five-minute scheduled task is an alternative.  Two behaviours follow from
running unattended:

* **A pass with nothing to do is one line.**  McTrader being down ends the pass
  before any queue file is written; otherwise every pass records
  ``poll files=N`` — the heartbeat that tells an idle worker from a dead one.
* **One file is one transaction.**  A file that fails during processing is
  never deleted, half-written or rewritten: a validation or too-thin failure
  renames its bytes to the ``Failed`` state (and records it in
  ``state/failures.json``), while a server-down abort leaves it marked for the
  next pass.  Renaming a ``Failed`` file back is the explicit retry.

Run it by hand:

    python sentiment_worker.py --check          # what the gates see, no work
    python sentiment_worker.py --force          # ignore a down McTrader API
    python sentiment_worker.py --dir D:\\some\\where --limit 1 --verbose
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import config
import curation
import llama_client
import store
import validate

EXIT_OK = 0
EXIT_FAILED = 1


# ── gates ────────────────────────────────────────────────────────────────────


def mctrader_is_up(timeout: float = config.MCTRADER_TIMEOUT_SECONDS) -> bool:
    """True when McTrader's API answers on ``/openapi.json``.

    That path, not ``/api/health``: the API is FastAPI, so ``/openapi.json`` is
    served by the framework and returns 200 whenever the process is up, while
    ``/api/health`` does not exist at all and would read as "down" forever.
    """
    try:
        with urllib.request.urlopen(config.MCTRADER_OPENAPI, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def llama_is_up() -> Tuple[bool, str]:
    """``(up, model_or_error)`` — the loaded model id is the liveness proof."""
    try:
        return True, llama_client.model_name()
    except llama_client.LlamaError as exc:
        return False, str(exc)


# ── one file ─────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    """What happened to one file, for the log line and the exit code."""

    name: str
    symbol: str
    ok: bool
    score: Optional[float] = None
    status: str = ""
    kept: int = 0
    dropped: int = 0
    scored: int = 0
    targets: int = 0
    error: str = ""
    dry_run: bool = False
    seconds: float = 0.0


class FileFailed(RuntimeError):
    """The file was not finished.  It is left exactly as it was found."""


class FileStopped(RuntimeError):
    """A stop was requested while this file was being scored.

    A stop is a request, not a kill: the file in flight gets config.STOP_GRACE_S to
    finish and be written, because its scores are only written once, at the end. Past
    that it is abandoned whole - nothing is written, the file keeps its bytes and its
    .new.json name, and the next pass rescores it from scratch.
    """


def _symbol_matches_file(symbol: str, stem: str) -> bool:
    """Whether the payload's symbol is the one the filename names.

    A symbol like ``BRK/B`` is written as ``BRK_B.json`` (Windows refuses the
    slash), so the comparison allows the same sanitisation McTrader's own writer
    applies rather than failing a file that is in fact correctly named.
    """
    if symbol == stem:
        return True
    sanitised = re.sub(r"[^A-Za-z0-9._-]", "_", symbol).strip("._- ")
    return bool(sanitised) and sanitised == stem


def _log_outcome(outcome: Outcome, *, log_dir: Optional[Path] = None) -> str:
    """The one line per file.  ``key=value`` so a grep is all it takes."""
    if outcome.error:
        line = (
            f"{store.iso(store.now_utc())} {outcome.symbol or outcome.name} FAILED "
            f"kept={outcome.kept} dropped={outcome.dropped} "
            f"scored={outcome.scored} targets={outcome.targets} "
            f"secs={outcome.seconds:.1f} error={json.dumps(outcome.error)}"
        )
    else:
        score = "null" if outcome.score is None else f"{outcome.score:.1f}"
        line = (
            f"{store.iso(store.now_utc())} {outcome.symbol or outcome.name} "
            f"score={score} status={outcome.status} kept={outcome.kept} "
            f"dropped={outcome.dropped} scored={outcome.scored} "
            f"targets={outcome.targets} secs={outcome.seconds:.1f}"
        )
        if outcome.dry_run:
            line += " dry_run=true"
    store.append_log(line, log_dir=log_dir)
    return line


def process_file(
    path: Path,
    *,
    model: str,
    now: datetime,
    dry_run: bool = False,
    verbose: bool = False,
    log_dir: Optional[Path] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Outcome:
    """Trim, score and commit one marked file.

    Raises :class:`llama_client.LlamaDown` when the server itself stops
    answering — the caller abandons the run rather than paying the same timeout
    per file.
    Every other failure comes back as an ``Outcome`` with ``ok=False`` and the
    marked file untouched.
    """
    started = time.monotonic()
    stop_seen_at = None      # monotonic, set the first time a stop is noticed
    name = Path(path).name
    # The ticker only: the country code is part of the filename, not of the payload, and
    # comparing "BAC" against "BAC.us" is what the guard rightly refused.
    stem = config.symbol_of(name) if config.is_pending(name) else Path(path).stem
    finished = Path(path).with_name(config.finished_name(name))
    outcome = Outcome(name=name, symbol=stem, ok=False, dry_run=dry_run)

    try:
        # Taken before the read: an append that lands while this file is being
        # judged is detected at commit time instead of being overwritten.
        expected = store.fingerprint(path)
        payload = store.read_payload(path)

        symbol = str(payload.get("symbol") or "").strip()
        company = str(payload.get("company") or "").strip()
        outcome.symbol = symbol or stem
        if not _symbol_matches_file(symbol, stem):
            raise FileFailed(
                f"the payload is for {symbol!r} but the file is named {stem!r}; "
                f"refusing to write {finished.name}"
            )

        # Renaming a file to .new.json is an instruction to rescore it, so nothing here
        # re-validates the producer's payload: a field the worker does not own - an old local
        # score, a placeholder, a stray value - must not make a file unprocessable. What is
        # checked is only what this pass needs to run at all.
        if not isinstance(payload.get("articles"), list):
            raise FileFailed("the payload has no articles[] list to score")
        if curation.local_entry(payload) is None:
            raise FileFailed("the payload has no scores[] entry with role 'local'")

        # The local reading is output, about to be replaced. The schema documents a pending
        # one as "null while pending" with status 'pending' (schema.json), so it is normalised
        # rather than judged: whatever stale value the file carried is not evidence about it.
        payload["local_sentiment_score"] = None
        payload["local_sentiment_status"] = "pending"

        trimmed = curation.trim(payload, today=now.date())
        outcome.kept = len(trimmed.kept)
        outcome.dropped = trimmed.dropped
        targets = curation.score_targets(trimmed)
        outcome.targets = len(targets)

        verdicts = {}
        failures: List[Tuple[str, str]] = []
        for position, article in enumerate(targets, 1):
            # Between articles, not between files: the file keeps going for up to
            # STOP_GRACE_S after a stop is noticed, so a file that is nearly done is
            # written rather than thrown away. Past the grace it is abandoned whole.
            if should_stop is not None and should_stop():
                if stop_seen_at is None:
                    stop_seen_at = time.monotonic()
                if time.monotonic() - stop_seen_at >= config.STOP_GRACE_S:
                    raise FileStopped(name)
            try:
                verdict = llama_client.score_article(article, model=model)
            except llama_client.LlamaDown:
                raise
            except llama_client.LlamaError as exc:
                failures.append((str(article.get("title") or "")[:120], str(exc)))
                if verbose and sys.stderr is not None:
                    print(f"  [{position}/{len(targets)}] FAILED {exc}", file=sys.stderr)
                continue
            verdicts[int(article.get("index") or 0)] = verdict
            if verbose and sys.stderr is not None:
                print(
                    f"  [{position}/{len(targets)}] {llama_client.describe(article, verdict)}",
                    file=sys.stderr,
                )

        if targets:
            required = max(1, math.ceil(len(targets) * config.MIN_SCORED_FRACTION))
            if len(verdicts) < required:
                first = f"first failure: {failures[0][1]}" if failures else "no failure recorded"
                raise FileFailed(
                    f"only {len(verdicts)} of {len(targets)} articles could be scored "
                    f"({len(failures)} failed, {first}); not writing a thin reading"
                )

        score = curation.aggregate(verdicts.values())
        status = "ok" if score is not None else "no view"
        scored_pairs = [
            (article, verdicts[int(article.get("index") or 0)])
            for article in targets
            if int(article.get("index") or 0) in verdicts
        ]
        if score is None:
            reason = curation.no_view_reason(symbol=symbol, company=company, trim=trimmed)
            evidence: Tuple[str, ...] = ()
        else:
            reason = curation.reason_text(
                symbol=symbol,
                company=company,
                model=model,
                scored=scored_pairs,
                failures=failures,
                kept=len(trimmed.kept),
            )
            evidence = curation.evidence_lines(
                scored_pairs, symbol=symbol, company=company
            )

        local_now, utc_now = curation.now_strings(now)
        reading = curation.Reading(
            score=score,
            status=status,
            model=model,
            reason=reason,
            evidence=evidence,
            as_of=now.date().isoformat(),
            attempts=len(targets),
            failures=tuple(failures),
        )
        updated = curation.apply_reading(
            payload,
            reading=reading,
            trim=trimmed,
            verdicts=verdicts,
            now_local=local_now,
            now_utc=utc_now,
            stamp=store.iso(now),
            targets=len(targets),
        )

        changed = curation.production_is_untouched(payload, updated)
        if changed:
            raise FileFailed(f"refusing to write: {changed}")
        try:
            validate.validate(updated, what=f"{finished.name} (curated)")
        except validate.SchemaError as exc:
            raise FileFailed(str(exc)) from None

        text = json.dumps(updated, ensure_ascii=False, indent=2) + "\n"
        outcome.score = score
        outcome.status = status
        outcome.scored = len(verdicts)

        if dry_run:
            outcome.ok = True
            outcome.seconds = time.monotonic() - started
            _log_outcome(outcome, log_dir=log_dir)
            return outcome

        store.commit(
            path,
            finished,
            text,
            expected=expected,
            validate_text=lambda temporary: validate.validate_file(
                temporary, what=f"{finished.name} on disk"
            ),
        )
        outcome.ok = True
        outcome.seconds = time.monotonic() - started
        _log_outcome(outcome, log_dir=log_dir)
        return outcome

    except llama_client.LlamaDown:
        raise
    except (FileFailed, store.StoreError, validate.SchemaError) as exc:
        outcome.error = str(exc)
        outcome.seconds = time.monotonic() - started
        _log_outcome(outcome, log_dir=log_dir)
        return outcome
    except FileStopped:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.seconds = time.monotonic() - started
        _log_outcome(outcome, log_dir=log_dir)
        return outcome


# ── the run ──────────────────────────────────────────────────────────────────


def _note_llama_down(count: int, error: str, *, log_dir: Optional[Path]) -> None:
    """Say "llama-server is down, N files are queued" at most once an hour.

    The files are not touched and not consumed: they stay marked and the next
    tick tries again.  Logging that every five minutes would bury the log, so
    the notice is throttled and re-sent early only if the queue changed.
    """
    moment = store.now_utc()
    try:
        previous = json.loads((config.STATE_DIR / "llama_down.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    last = previous.get("last_notice_utc") if isinstance(previous, dict) else None
    last_pending = previous.get("pending") if isinstance(previous, dict) else None
    due = True
    if last and last_pending == count:
        try:
            elapsed = (moment - datetime.fromisoformat(str(last).replace("Z", "+00:00"))).total_seconds()
            due = elapsed >= 3600
        except ValueError:
            due = True
    if not due:
        return
    store.append_log(
        f"{store.iso(moment)} llama_unavailable pending={count} "
        f"error={json.dumps(error)} note=files_queued_untouched",
        log_dir=log_dir,
    )
    try:
        store.dump_json(
            config.STATE_DIR / "llama_down.json",
            {"last_notice_utc": store.iso(moment), "pending": count, "error": error},
        )
    except OSError:
        pass


def run(
    *,
    directory: Optional[Path] = None,
    force: bool = False,
    dry_run: bool = False,
    limit: Optional[int] = None,
    only: Optional[Path] = None,
    verbose: bool = False,
    log_dir: Optional[Path] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """One pass.  Returns the process exit code.

    No McTrader ends the pass before anything is written.  Once that gate
    passes, every pass records a ``poll files=N`` heartbeat; no marked files,
    or another run holding the lock, then ends it without touching a file.
    """
    if not force and not mctrader_is_up():
        return EXIT_OK

    if only is not None:
        candidates = [Path(only)]
    else:
        try:
            candidates = store.discover(Path(directory or config.SENTIMENT_DIR), limit=limit)
        except store.StoreError as exc:
            store.append_log(f"{store.iso(store.now_utc())} cannot_list error={json.dumps(str(exc))}", log_dir=log_dir)
            return EXIT_FAILED

    # Every pass says it looked, on the console and in the day's log. The task's
    # window is hidden and its output goes nowhere, so without this a five-minute
    # cadence and a dead worker are indistinguishable from the outside - which is
    # exactly the question that took an hour to answer on 2026-10-23.
    poll_line = (
        f"{store.iso(store.now_utc())} poll files={len(candidates)}"
        + (f" names={','.join(p.name for p in candidates)}" if candidates else "")
    )
    print(poll_line, flush=True)
    store.append_log(poll_line, log_dir=log_dir)

    if not candidates:
        return EXIT_OK

    try:
        with store.instance_lock() as _lock:
            return _run_locked(
                candidates,
                dry_run=dry_run,
                verbose=verbose,
                log_dir=log_dir,
                should_stop=should_stop,
            )
    except store.LockBusy:
        # Another tick is working.  That is the guard doing its job, not an
        # error, and saying so every five minutes would be noise.
        return EXIT_OK


def fresh_work(candidates: Sequence[Path], failures: dict) -> List[Path]:
    """The pending files, which are the work.

    This used to consult ``state/failures.json`` and call a file "stuck, unchanged" when its
    last failure postdated its mtime, so a pass would not retry it. Two things made that the
    wrong rule. A file that cannot be processed is now renamed to the Failed state and leaves
    the pending set by itself, so a file still sitting in it is one the operator put there or
    the producer dropped there. And renaming a Failed file back is exactly the instruction
    "score this again", which a failure recorded before the rename would have vetoed.

    ``failures`` is kept in the signature: the pass still records and clears entries, and a
    caller reading them for a log line should not have to know this function ignores them.
    """
    return list(candidates)


def mark_failed(path: Path, error: str, *, log_dir: Optional[Path] = None) -> None:
    """Leave a file that cannot be processed under the Failed state, in the queue folder.

    The queue is the record and the file stays where the operator looks for it, but a file
    that can never pass validation must stop being retried: APX and NXT reached 1,309
    attempts before this existed. Renaming is what takes it out of the pending set, and the
    line written here names the file it became, so the log and the folder agree.
    """
    target = path.with_name(config.failed_name(path.name))
    try:
        path.rename(target)
    except OSError as exc:
        store.append_log(
            f"{store.iso(store.now_utc())} {path.name} could not be marked Failed: "
            f"{exc.__class__.__name__}: {exc}",
            log_dir=log_dir,
        )
        return
    store.append_log(
        f"{store.iso(store.now_utc())} {path.name} -> {target.name} error={json.dumps(error)}",
        log_dir=log_dir,
    )


def _run_locked(
    candidates: Sequence[Path],
    *,
    dry_run: bool,
    verbose: bool,
    log_dir: Optional[Path],
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    up, detail = llama_is_up()
    if not up:
        _note_llama_down(len(candidates), detail, log_dir=log_dir)
        return EXIT_OK

    model = detail
    failures = store.load_failures()
    exit_code = EXIT_OK

    for path in candidates:
        # A service stop used to wait for the whole pass: 297 s on 2026-11-11, which is
        # what "stuck in Stop Pending" was. The file in flight finishes (it is written by
        # rename, so stopping between them loses nothing), and the rest wait for the next
        # pass. Asked only when a caller can answer it - a command-line pass never is.
        if should_stop is not None and should_stop():
            store.append_log(
                f"{store.iso(store.now_utc())} stopped by request; the remaining files wait for the next pass",
                log_dir=log_dir,
            )
            break
        # A timestamp per file, not per pass: a backlog drains over an hour and
        # every finished file should say when its own reading was made.
        now = store.now_utc()
        processing_line = f"{store.iso(now)} processing {path.name}"
        print(f"  {processing_line}", flush=True)
        store.append_log(processing_line, log_dir=log_dir)
        try:
            outcome = process_file(
                path,
                model=model,
                now=now,
                dry_run=dry_run,
                verbose=verbose,
                log_dir=log_dir,
                should_stop=should_stop,
            )
        except FileStopped:
            store.append_log(
                f"{store.iso(store.now_utc())} stopped by request; {path.name} left for the next pass",
                log_dir=log_dir,
            )
            break
        except llama_client.LlamaDown as exc:
            store.append_log(
                f"{store.iso(store.now_utc())} aborted reason={json.dumps(f'llama-server stopped answering: {exc}')} "
                f"remaining={len(candidates)}",
                log_dir=log_dir,
            )
            _note_llama_down(len(candidates), str(exc), log_dir=log_dir)
            break
        except store.StoreError as exc:
            store.append_log(
                f"{store.iso(store.now_utc())} {path.name} FAILED error={json.dumps(str(exc))}",
                log_dir=log_dir,
            )
            exit_code = EXIT_FAILED
            continue

        if outcome.ok:
            if failures.pop(outcome.symbol, None) is not None:
                store.save_failures(failures)
            continue

        exit_code = EXIT_FAILED
        entry = failures.get(outcome.symbol) or {"attempts": 0}
        entry["attempts"] = int(entry.get("attempts") or 0) + 1
        entry["last_error"] = outcome.error
        entry["last_utc"] = store.iso(store.now_utc())
        entry["file"] = outcome.name
        failures[outcome.symbol] = entry
        store.save_failures(failures)
        # Left in the folder under the Failed state: out of the pending set, still evidence.
        mark_failed(path, outcome.error, log_dir=log_dir)

    return exit_code


# ── the command line ─────────────────────────────────────────────────────────


def check(directory: Optional[Path] = None) -> int:
    """Print what the gates see.  Reads nothing and changes nothing."""
    directory = Path(directory or config.SENTIMENT_DIR)
    api = mctrader_is_up()
    up, detail = llama_is_up()
    try:
        marked = store.discover(directory)
        listing = f"{len(marked)} marked file(s) in {directory}"
    except store.StoreError as exc:
        listing = f"cannot list {directory}: {exc}"
    lock_state = "free"
    if config.LOCK_PATH.exists():
        try:
            holder = json.loads(config.LOCK_PATH.read_text(encoding="utf-8"))
            lock_state = f"file present (pid {holder.get('pid')}, since {holder.get('since')})"
        except (OSError, ValueError):
            lock_state = "file present, unreadable"
    print(f"mcTrader  : {'up' if api else 'DOWN'} ({config.MCTRADER_OPENAPI})")
    print(f"llama.cpp : {'up, model ' + detail if up else 'DOWN: ' + detail}")
    print(f"pending   : {listing}")
    print(f"lock      : {lock_state}")
    print(f"marker    : <symbol>.<cc>{config.PENDING_SUFFIX} (config.PENDING_SUFFIX)")
    return EXIT_OK if api and up else EXIT_FAILED


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Curate marked sentiment files with the local model."
    )
    parser.add_argument("--check", action="store_true", help="report the gates and exit")
    parser.add_argument(
        "--force",
        action="store_true",
        help="work even when McTrader's API is down (for tests and backfills)",
    )
    parser.add_argument("--dry-run", action="store_true", help="score but write nothing")
    parser.add_argument("--limit", type=int, default=None, help="at most N files this pass")
    parser.add_argument("--file", type=Path, default=None, help="process one marked file")
    parser.add_argument("--dir", type=Path, default=None, help="sentiment directory")
    parser.add_argument("--verbose", action="store_true", help="print each article's verdict")
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="the form the scheduled task uses: identical to no arguments",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.check:
        return check(args.dir)

    try:
        return run(
            directory=args.dir,
            force=args.force,
            dry_run=args.dry_run,
            limit=args.limit,
            only=args.file,
            verbose=args.verbose,
        )
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    # The scheduled task runs this under pythonw.exe, so there is no console to
    # catch a traceback.  An unexpected failure is written to the day's log and
    # the process still exits non-zero, which the scheduler records.
    try:
        _code = main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - last resort for a headless process
        import traceback

        store.append_log(
            f"{store.iso(store.now_utc())} unhandled "
            f"error={json.dumps(traceback.format_exc()[-1500:])}"
        )
        _code = EXIT_FAILED
    raise SystemExit(_code)

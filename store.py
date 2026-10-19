"""The filesystem side: one file, one transaction, one lock.

The invariants this module exists to keep:

* **A finished file is written whole or not at all.**  The text goes to a
  temporary file in the same directory, is fsynced, is parsed back and validated
  from disk, and is only then renamed over its target.
* **The marker clears last.**  On success the new payload is written *through*
  the pending name (``BAC.pending.json``) and only then renamed to
  ``BAC.json``.  The pending file is never deleted, so if the process dies there
  is always a complete file under one of the two names.
* **A file that changed while it was being read is not overwritten.**  The
  pending file's size and modification time are compared immediately before the
  first rename; a mismatch abandons the file for this run rather than clobbering
  an append that arrived mid-judgement.
* **The lock never outlives the process.**  It is a byte-range lock held on an
  open handle: Windows releases it when the process dies however it dies, so a
  crash cannot leave a lock that blocks the next tick.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import config


class StoreError(RuntimeError):
    """A file could not be read or replaced, and the caller must not write."""


class LockBusy(StoreError):
    """Another worker holds the single-instance lock."""


class ChangedUnderneath(StoreError):
    """The pending file changed while it was being judged."""


def now_utc() -> datetime:
    """The worker's clock, in UTC.  Every age decision is made against this."""
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """Seconds-precision ISO 8601, the precision the files already use."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_payload(path: Path) -> dict:
    """The file as a JSON object.  ``utf-8-sig`` tolerates a PowerShell BOM."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise StoreError(f"cannot read {path}: {exc}") from None
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise StoreError(f"{Path(path).name} is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise StoreError(f"{Path(path).name} is not a JSON object")
    return payload


def fingerprint(path: Path) -> Tuple[int, int]:
    """``(mtime_ns, size)`` — cheap, and enough to notice an append."""
    stat = Path(path).stat()
    return (int(stat.st_mtime_ns), int(stat.st_size))


def discover(directory: Path, *, limit: Optional[int] = None) -> List[Path]:
    """The marked files, longest-waiting first.

    Marked means the ``config.PENDING_SUFFIX`` convention, case-insensitively.
    Market order comes first, then modification time within a market, so a
    deep .us backlog cannot delay every .au file behind it.
    """
    directory = Path(directory)
    try:
        entries = [entry for entry in directory.iterdir() if entry.is_file()]
    except OSError as exc:
        raise StoreError(f"cannot list {directory}: {exc}") from None
    marked = [entry for entry in entries if config.is_pending(entry.name)]
    # Market first, then longest-waiting within a market. Purely oldest-first would let a
    # deep .us backlog delay every .au file behind it.
    marked.sort(key=lambda entry: (config.country_priority(entry.name),
                                   entry.stat().st_mtime_ns, entry.name))
    return marked[:limit] if limit else marked


@contextmanager
def instance_lock(path: Optional[Path] = None) -> Iterator[Path]:
    """Hold the single-instance lock for the duration of the block.

    The lock is an OS byte-range lock, not a file's existence: if this process is
    killed, Windows drops the lock with the handle and the next tick proceeds.
    The file is removed on a clean exit; a leftover file from a crash is harmless
    because the lock, not the path, is the guard.
    """
    lock_path = Path(path or config.LOCK_PATH)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        try:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            handle.close()
            raise LockBusy(f"another worker holds {lock_path}: {exc}") from None
        except ImportError:  # pragma: no cover - Windows is the target
            pass

        try:
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps({"pid": os.getpid(), "since": iso(now_utc()), "name": config.WORKER_NAME})
            )
            handle.flush()
            os.fsync(handle.fileno())
            yield lock_path
        finally:
            try:
                handle.seek(0)
                handle.truncate()
                handle.flush()
                try:
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, ImportError):
                    pass
            finally:
                handle.close()
                try:
                    lock_path.unlink()
                except OSError:
                    pass
    except LockBusy:
        raise


def _write_temp(directory: Path, name: str, text: str) -> Path:
    """A fsynced temporary beside the target, so the rename is same-volume."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return Path(temporary)


def commit(
    pending: Path,
    finished: Path,
    text: str,
    *,
    expected: Tuple[int, int],
    validate_text,
) -> None:
    """Replace *finished* with *text* and clear the marker, in that order.

    The final two operations are renames, never deletes: the pending file is
    overwritten in place with the finished payload and then renamed to the
    unmarked name.  A crash before the first rename leaves the pending file
    exactly as the search wrote it; a crash between the two leaves a complete,
    validated payload under the pending name, which the next run re-processes.
    """
    pending = Path(pending)
    finished = Path(finished)
    temporary = _write_temp(finished.parent, finished.name, text)
    try:
        # Prove the bytes on disk, not just the dict in memory.
        validate_text(temporary)
        if fingerprint(pending) != expected:
            raise ChangedUnderneath(
                f"{pending.name} changed while it was being judged; left untouched"
            )
        os.replace(temporary, pending)
        os.replace(pending, finished)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def dump_json(path: Path, payload: object) -> None:
    """Small atomic JSON write, used for the worker's own state files."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _write_temp(path.parent, path.name, json.dumps(payload, indent=2) + "\n")
    os.replace(temporary, path)


def load_failures(path: Optional[Path] = None) -> Dict[str, dict]:
    """The per-symbol failure journal, or an empty one."""
    target = Path(path or config.FAILURES_PATH)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_failures(journal: Dict[str, dict], path: Optional[Path] = None) -> None:
    try:
        dump_json(Path(path or config.FAILURES_PATH), journal)
    except OSError:
        pass


def append_log(line: str, *, log_dir: Optional[Path] = None, moment: Optional[datetime] = None) -> None:
    """One line, one file per day.  A day file is its own rotation."""
    directory = Path(log_dir or config.LOG_DIR)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        day = (moment or now_utc()).astimezone().strftime("%Y-%m-%d")
        with open(directory / f"worker-{day}.log", "a", encoding="utf-8") as handle:
            handle.write(line.rstrip("\n") + "\n")
    except OSError:
        # A log that cannot be written must not stop the work; the console and
        # the task scheduler's exit code still carry the outcome.
        pass

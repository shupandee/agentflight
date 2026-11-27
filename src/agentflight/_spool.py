"""A bounded on-disk spool for spans that couldn't be exported.

When an export fails in a way that may work later (server down, timeout, 5xx, 429,
credentials), the spans that weren't stored are written to the spool directory, one file
per failed batch, and sent again at the start of a later export, oldest first. The spool
is bounded by total size and by age; when it is full, the oldest files are deleted.

Files are JSON (``{"v": 1, "spans": [...]}``), the same span JSON ``POST /v1/spans``
accepts, so they hold recorded payloads after ``redact``. The directory is created
``0700`` and files ``0600``. Every operation is fail-open: a full disk, a directory that
can't be written or a corrupt file loses those spans (counted in ``dropped`` and logged
once per kind of problem) and never raises.

Spool I/O happens on the exporter's thread only; the agent's thread never touches it.
"""

from __future__ import annotations

import contextlib
import errno
import itertools
import json
import logging
import os
import stat
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("agentflight")

DEFAULT_SPOOL_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_SPOOL_MAX_AGE_SECONDS = 24 * 3600
MAX_FILES = 10_000
"""A hard cap on files, so listing the directory stays cheap whatever their size."""

SUFFIX = ".spool.json"
_TMP_PREFIX = ".tmp-"
_FORMAT = 1
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class SpoolFile:
    """One spooled batch. ``born_ns`` (from the name) orders files and ages them."""

    path: Path
    born_ns: int
    count: int
    size: int


def _parse_name(name: str) -> tuple[int, int] | None:
    """``<born_ns>-<pid>-<seq>-<count>.spool.json`` -> (born_ns, count)."""
    if not name.endswith(SUFFIX):
        return None
    parts = name[: -len(SUFFIX)].split("-")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[3])


class Spool:
    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        max_bytes: int = DEFAULT_SPOOL_MAX_BYTES,
        max_age_s: float = DEFAULT_SPOOL_MAX_AGE_SECONDS,
    ) -> None:
        if max_bytes < 1:
            raise ValueError("spool max_bytes must be at least 1")
        if max_age_s <= 0:
            raise ValueError("spool max_age_s must be positive")
        self.directory = Path(directory)
        self.max_bytes = max_bytes
        self.max_age_s = max_age_s
        self.dropped = 0
        """Spans the spool gave up on: over its caps, I/O errors or corrupt files."""
        self._lock = threading.Lock()  # file operations
        self._replaying = threading.Lock()  # one replay at a time
        self._seq = itertools.count()
        self._warned: set[str] = set()

    def __repr__(self) -> str:
        return f"Spool({str(self.directory)!r})"

    # -- helpers -------------------------------------------------------------------

    def _warn(self, kind: str, message: str, *args: Any) -> None:
        if kind not in self._warned:
            self._warned.add(kind)
            logger.warning("agentflight: spool %s: " + message, self.directory, *args)

    def _lose(self, n: int) -> None:
        self.dropped += n

    def _ensure_dir(self) -> bool:
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            st = os.lstat(self.directory)
            if not stat.S_ISDIR(st.st_mode):
                self._warn("notdir", "not a directory (a symlink?); spans are not spooled")
                return False
            if hasattr(os, "getuid") and st.st_uid != os.getuid():
                self._warn("owner", "owned by another user; spans are not spooled")
                return False
            if st.st_mode & 0o077:
                os.chmod(self.directory, 0o700)
            return True
        except OSError as exc:
            self._warn("mkdir", "can't be used (%s); spans are not spooled", exc)
            return False

    def files(self) -> list[SpoolFile]:
        """Spooled batches, oldest first."""
        out: list[SpoolFile] = []
        try:
            entries = list(os.scandir(self.directory))
        except OSError:
            return out
        for entry in entries:
            parsed = _parse_name(entry.name)
            if parsed is None:
                continue
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            out.append(SpoolFile(Path(entry.path), parsed[0], parsed[1], size))
        out.sort(key=lambda f: (f.born_ns, f.path.name))
        return out

    def pending_spans(self) -> int:
        return sum(f.count for f in self.files())

    def _remove(self, f: SpoolFile, *, lost: bool) -> None:
        try:
            f.path.unlink()
        except FileNotFoundError:
            return  # another process got there first
        except OSError as exc:
            self._warn("unlink", "can't delete spooled files (%s)", exc)
            return
        if lost:
            self._lose(f.count)

    def _sweep(self, incoming: int) -> None:
        """Delete expired files, stale temporary files and, to fit ``incoming`` more bytes,
        the oldest files."""
        now_ns = time.time_ns()
        max_age_ns = int(self.max_age_s * 1e9)
        try:
            for entry in os.scandir(self.directory):
                if entry.name.startswith(_TMP_PREFIX):
                    try:  # left behind by a crash mid-write
                        if now_ns - entry.stat(follow_symlinks=False).st_mtime_ns > 3600e9:
                            os.unlink(entry.path)
                    except OSError:
                        pass
        except OSError:
            pass
        files = self.files()
        keep: list[SpoolFile] = []
        expired = 0
        for f in files:
            if now_ns - f.born_ns > max_age_ns:
                self._remove(f, lost=True)
                expired += f.count
            else:
                keep.append(f)
        if expired:
            self._warn("age", "spans older than %gs were deleted unsent", self.max_age_s)
        total = sum(f.size for f in keep)
        evicted = 0
        while keep and (total + incoming > self.max_bytes or len(keep) >= MAX_FILES):
            oldest = keep.pop(0)
            self._remove(oldest, lost=True)
            total -= oldest.size
            evicted += oldest.count
        if evicted:
            self._warn(
                "full", "full (%d bytes); the oldest spooled spans were deleted", self.max_bytes
            )

    def _write(self, spans: list[dict[str, Any]], born_ns: int) -> Path | None:
        data = json.dumps({"v": _FORMAT, "spans": spans}, separators=(",", ":")).encode()
        if len(data) > self.max_bytes:
            self._warn("big", "a batch of %d bytes is over the spool's cap", len(data))
            return None
        self._sweep(len(data))
        name = f"{born_ns:020d}-{os.getpid()}-{next(self._seq):06d}-{len(spans)}{SUFFIX}"
        final = self.directory / name
        tmp = self.directory / f"{_TMP_PREFIX}{name}"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view) :]
            finally:
                os.close(fd)
            os.replace(tmp, final)
            return final
        except OSError as exc:
            kind = "nospace" if exc.errno in (errno.ENOSPC, errno.EDQUOT) else "write"
            self._warn(kind, "can't write (%s); spans are lost", exc)
            with contextlib.suppress(OSError):
                tmp.unlink()
            return None

    # -- public --------------------------------------------------------------------

    def put(self, spans: list[dict[str, Any]]) -> bool:
        """Spool a batch for a later export. Never raises; False if it was lost."""
        if not spans:
            return True
        try:
            with self._lock:
                ok = self._ensure_dir() and self._write(spans, time.time_ns()) is not None
        except Exception as exc:  # pragma: no cover - defensive: fail open
            self._warn("bug", "unexpected error (%r)", exc)
            ok = False
        if not ok:
            self._lose(len(spans))
        return ok

    def replay(self) -> Generator[tuple[SpoolFile, list[dict[str, Any]]], None, None]:
        """Yield spooled batches oldest first; the caller must ``settle`` each one.

        Yields nothing if another thread is already replaying. Unreadable or corrupt files
        are deleted (and counted) instead of being yielded.
        """
        if not self._replaying.acquire(blocking=False):
            return
        try:
            with self._lock:
                if not self.directory.is_dir():
                    return
                self._sweep(0)
                files = self.files()
            for f in files:
                spans = self._read(f)
                if spans is not None:
                    yield f, spans
        finally:
            self._replaying.release()

    def _read(self, f: SpoolFile) -> list[dict[str, Any]] | None:
        try:
            fd = os.open(f.path, os.O_RDONLY | _NOFOLLOW)
            with os.fdopen(fd, "rb") as fh:
                doc = json.loads(fh.read())
            spans = doc["spans"] if isinstance(doc, dict) and doc.get("v") == _FORMAT else None
            if not isinstance(spans, list) or not all(isinstance(s, dict) for s in spans):
                raise ValueError("not a spool file")
            return spans
        except FileNotFoundError:
            return None  # sent and deleted by another process meanwhile
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._warn("corrupt", "skipping a corrupt or unreadable file (%r)", exc)
            with self._lock:
                self._remove(f, lost=True)
            return None

    def settle(self, f: SpoolFile, total: int, unsent: list[dict[str, Any]]) -> None:
        """After replaying ``f`` (``total`` spans): delete it, or keep only ``unsent`` in
        its place in line."""
        if len(unsent) == total:
            return  # nothing went through; leave the file as it is
        try:
            with self._lock:
                self._remove(f, lost=False)
                if unsent and self._write(unsent, f.born_ns) is None:
                    self._lose(len(unsent))
        except Exception as exc:  # pragma: no cover - defensive: fail open
            self._warn("bug", "unexpected error (%r)", exc)

"""
Coach folder storage for Intervals.icu MCP Server.

Reads and writes Markdown files inside the directory given by the COACH_DIR
environment variable, and records every write as a commit when that directory
is a git repository. The server is reachable from the Internet, so every path
is resolved with realpath and must stay inside COACH_DIR, end in `.md` and
never go through `.git`.

Writes are atomic (temporary file + os.replace) and serialized with an fcntl
lock held for both the write and the git commit.
"""

import fcntl
import hashlib
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAX_WRITE_BYTES = 256 * 1024
LOCK_FILENAME = ".coach.lock"
GIT_AUTHOR_NAME = "Claude Coach"
GIT_AUTHOR_EMAIL = "coach@tetsuo.local"
GIT_TIMEOUT_SECONDS = 30


class CoachFileError(Exception):
    """A request that cannot be served; the message is safe to return to the client."""


class CoachConflictError(CoachFileError):
    """The file changed since the caller read it (expected_sha256 mismatch)."""

    def __init__(self, path: str, expected_sha256: str, current: "CoachFile | None"):
        self.path = path
        self.expected_sha256 = expected_sha256
        self.current = current
        super().__init__(f"Conflict on {path}")


@dataclass
class CoachFile:
    """Content of a coach file and the sha256 of its bytes."""

    path: str
    content: str
    sha256: str


@dataclass
class CoachFileEntry:
    """One file in a coach folder listing."""

    path: str
    size: int
    modified: datetime


@dataclass
class WriteResult:
    """Outcome of a write or append: the new sha256 and what happened in git."""

    path: str
    size: int
    sha256: str
    git_status: str


def get_coach_dir() -> Path:
    """Return the resolved COACH_DIR, or raise CoachFileError if it is unusable."""
    raw = os.getenv("COACH_DIR", "").strip()
    if not raw:
        raise CoachFileError("COACH_DIR is not set: coach file tools are disabled on this server.")
    if not os.path.isabs(raw):
        raise CoachFileError("COACH_DIR must be an absolute path.")
    if not os.path.isdir(raw):
        raise CoachFileError(f"COACH_DIR does not exist or is not a directory: {raw}")
    return Path(os.path.realpath(raw))


def _check_relative(path: str) -> PurePosixPath:
    """Reject empty, absolute, `..` and `.git` paths before touching the filesystem."""
    if "\x00" in path:
        raise CoachFileError("Invalid path.")
    if os.path.isabs(path) or path.startswith("~"):
        raise CoachFileError(f"Absolute paths are not allowed: {path}")
    rel = PurePosixPath(path)
    if ".." in rel.parts:
        raise CoachFileError(f"'..' is not allowed in paths: {path}")
    if any(part.lower() == ".git" for part in rel.parts):
        raise CoachFileError(f"Paths inside .git are not allowed: {path}")
    return rel


def _resolve_inside(root: Path, rel: PurePosixPath, original: str) -> Path:
    """Resolve symlinks and make sure the result is still inside root and outside .git."""
    full = Path(os.path.realpath(root / rel))
    if full != root and not full.is_relative_to(root):
        raise CoachFileError(f"Path escapes the coach folder: {original}")
    if any(part.lower() == ".git" for part in full.relative_to(root).parts):
        raise CoachFileError(f"Paths inside .git are not allowed: {original}")
    return full


def resolve_file(root: Path, path: str) -> Path:
    """Validate a client-supplied file path and return its real location inside root."""
    if not path.strip():
        raise CoachFileError("A file path is required.")
    rel = _check_relative(path)
    if rel.suffix.lower() != ".md":
        raise CoachFileError(f"Only .md files are allowed: {path}")
    full = _resolve_inside(root, rel, path)
    if full == root or full.suffix.lower() != ".md":
        raise CoachFileError(f"Only .md files are allowed: {path}")
    if full.exists() and not full.is_file():
        raise CoachFileError(f"Not a regular file: {path}")
    return full


def resolve_dir(root: Path, subdir: str) -> Path:
    """Validate a client-supplied sub-directory ("" means the coach folder itself)."""
    if not subdir.strip():
        return root
    full = _resolve_inside(root, _check_relative(subdir), subdir)
    if not full.is_dir():
        raise CoachFileError(f"Directory not found: {subdir}")
    return full


def _relative(root: Path, full: Path) -> str:
    return full.relative_to(root).as_posix()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encode_content(content: str) -> bytes:
    data = content.encode("utf-8")
    if len(data) > MAX_WRITE_BYTES:
        raise CoachFileError(
            f"Content is {len(data)} bytes; the limit per write is {MAX_WRITE_BYTES} bytes."
        )
    return data


def _check_commit_message(commit_message: str) -> str:
    message = commit_message.strip()
    if not message:
        raise CoachFileError("A non-empty commit_message is required.")
    return message


def list_files(root: Path, subdir: str = "") -> list[CoachFileEntry]:
    """List .md files under subdir recursively, skipping .git and anything outside root."""
    start = resolve_dir(root, subdir)
    entries: list[CoachFileEntry] = []
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(d for d in dirnames if d.lower() != ".git")
        for name in filenames:
            if not name.lower().endswith(".md"):
                continue
            rel = _relative(root, Path(dirpath) / name)
            try:
                full = resolve_file(root, rel)
            except CoachFileError:
                continue
            stat = full.stat()
            entries.append(
                CoachFileEntry(
                    path=rel,
                    size=stat.st_size,
                    modified=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
    return sorted(entries, key=lambda entry: entry.path)


def _read_bytes(full: Path) -> bytes | None:
    try:
        return full.read_bytes()
    except FileNotFoundError:
        return None


def _to_coach_file(path: str, data: bytes) -> CoachFile:
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoachFileError(f"File is not valid UTF-8: {path}") from exc
    return CoachFile(path=path, content=content, sha256=_sha256(data))


def read_file(root: Path, path: str) -> CoachFile:
    """Return the content of a coach file and the sha256 of its bytes."""
    full = resolve_file(root, path)
    data = _read_bytes(full)
    if data is None:
        raise CoachFileError(f"File not found: {path}")
    return _to_coach_file(_relative(root, full), data)


@contextmanager
def _locked(root: Path) -> Iterator[None]:
    """Hold an exclusive lock on the coach folder (one writer at a time, across processes)."""
    with open(root / LOCK_FILENAME, "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _atomic_write(full: Path, data: bytes) -> None:
    full.parent.mkdir(parents=True, exist_ok=True)
    mode = full.stat().st_mode & 0o777 if full.exists() else 0o644
    fd, tmp_name = tempfile.mkstemp(dir=full.parent, prefix=f".{full.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, full)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    command = [
        "git",
        "-c", f"safe.directory={root}",
        "-c", f"user.name={GIT_AUTHOR_NAME}",
        "-c", f"user.email={GIT_AUTHOR_EMAIL}",
        "-c", "commit.gpgsign=false",
        "-c", "core.hooksPath=/dev/null",
        "-C", str(root),
        *args,
    ]  # fmt: skip
    return subprocess.run(
        command, capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False
    )


def _git_error(result: subprocess.CompletedProcess[str]) -> str:
    output = (result.stderr or result.stdout).strip().splitlines()
    return output[-1] if output else f"git exited with code {result.returncode}"


def commit_file(root: Path, rel: str, message: str) -> str:
    """Commit one file; return a short status line. Never raises."""
    if not (root / ".git").exists():
        return "not committed: the coach folder is not a git repository."
    try:
        added = _git(root, "add", "--", rel)
        if added.returncode != 0:
            return f"commit FAILED (git add): {_git_error(added)}"
        if _git(root, "diff", "--cached", "--quiet", "--", rel).returncode == 0:
            return "nothing to commit (content unchanged)."
        committed = _git(root, "commit", "-m", message, "--", rel)
        if committed.returncode != 0:
            return f"commit FAILED: {_git_error(committed)}"
        head = _git(root, "rev-parse", "--short", "HEAD")
        return f"committed {head.stdout.strip()}."
    except (OSError, subprocess.SubprocessError) as exc:
        return f"commit FAILED: {exc}"


def write_file(
    root: Path,
    path: str,
    content: str,
    commit_message: str,
    expected_sha256: str | None = None,
) -> WriteResult:
    """Create or replace a coach file, refusing if expected_sha256 no longer matches."""
    data = _encode_content(content)
    message = _check_commit_message(commit_message)
    full = resolve_file(root, path)
    rel = _relative(root, full)
    with _locked(root):
        if expected_sha256 is not None:
            current = _read_bytes(full)
            if current is None or _sha256(current) != expected_sha256.strip().lower():
                raise CoachConflictError(
                    rel,
                    expected_sha256,
                    None if current is None else _to_coach_file(rel, current),
                )
        _atomic_write(full, data)
        git_status = commit_file(root, rel, message)
    return WriteResult(path=rel, size=len(data), sha256=_sha256(data), git_status=git_status)


def append_file(root: Path, path: str, content: str, commit_message: str) -> WriteResult:
    """Append to a coach file (adding a newline first if needed), creating it if missing."""
    addition = _encode_content(content)
    message = _check_commit_message(commit_message)
    full = resolve_file(root, path)
    rel = _relative(root, full)
    with _locked(root):
        current = _read_bytes(full) or b""
        separator = b"\n" if current and not current.endswith(b"\n") else b""
        data = current + separator + addition
        _atomic_write(full, data)
        git_status = commit_file(root, rel, message)
    return WriteResult(path=rel, size=len(data), sha256=_sha256(data), git_status=git_status)

"""
Unit tests for the coach folder storage and its MCP tools.

Covers path safety (traversal, absolute paths, outgoing symlinks, .git), the .md-only rule,
the size limit, read/write/append, the expected_sha256 conflict check and git commits.
"""

import asyncio
import hashlib
import os
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server import coach_files  # pylint: disable=wrong-import-position
from intervals_mcp_server.coach_files import (  # pylint: disable=wrong-import-position
    CoachConflictError,
    CoachFileError,
)
from intervals_mcp_server.mcp_instance import mcp  # pylint: disable=wrong-import-position
from intervals_mcp_server.tools.coach import (  # pylint: disable=wrong-import-position
    append_coach_file,
    list_coach_files,
    read_coach_file,
    write_coach_file,
)


def _git(repo: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    """A coach folder (not a git repo) with one existing file."""
    coach = tmp_path / "coach"
    (coach / "athlete").mkdir(parents=True)
    (coach / "athlete" / "profil.md").write_text("# Profil\n", encoding="utf-8")
    return pathlib.Path(os.path.realpath(coach))


@pytest.fixture
def repo(root: pathlib.Path) -> pathlib.Path:
    """The same coach folder, initialised as a git repository with one commit."""
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=Test", "-c", "user.email=t@example.com", "commit", "-qm", "init")
    return root


# --- path safety ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["../outside.md", "athlete/../../outside.md", "/etc/passwd.md", "~/notes.md", ""],
)
def test_rejects_paths_outside_coach_dir(root, path):
    with pytest.raises(CoachFileError):
        coach_files.resolve_file(root, path)


def test_rejects_absolute_path_even_inside_coach_dir(root):
    with pytest.raises(CoachFileError, match="Absolute"):
        coach_files.read_file(root, str(root / "athlete" / "profil.md"))


@pytest.mark.parametrize("path", [".git/config", ".git/notes.md", "sub/.GIT/x.md"])
def test_rejects_git_paths(repo, path):
    with pytest.raises(CoachFileError):
        coach_files.resolve_file(repo, path)


def test_rejects_symlinked_file_pointing_outside(root, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("secret", encoding="utf-8")
    (root / "link.md").symlink_to(secret)
    with pytest.raises(CoachFileError, match="escapes"):
        coach_files.read_file(root, "link.md")
    with pytest.raises(CoachFileError, match="escapes"):
        coach_files.write_file(root, "link.md", "pwned", "msg")
    assert secret.read_text(encoding="utf-8") == "secret"


def test_rejects_symlinked_dir_pointing_outside(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "evil").symlink_to(outside)
    with pytest.raises(CoachFileError, match="escapes"):
        coach_files.write_file(root, "evil/new.md", "pwned", "msg")
    with pytest.raises(CoachFileError, match="escapes"):
        coach_files.list_files(root, "evil")
    assert not (outside / "new.md").exists()


def test_rejects_symlink_into_git_dir(repo):
    (repo / "config.md").symlink_to(repo / ".git" / "config")
    with pytest.raises(CoachFileError):
        coach_files.read_file(repo, "config.md")


def test_list_skips_symlinks_pointing_outside_and_git(repo, tmp_path):
    (tmp_path / "secret.md").write_text("secret", encoding="utf-8")
    (repo / "link.md").symlink_to(tmp_path / "secret.md")
    (repo / ".git" / "hidden.md").write_text("x", encoding="utf-8")
    paths = [entry.path for entry in coach_files.list_files(repo)]
    assert paths == ["athlete/profil.md"]


@pytest.mark.parametrize("path", ["notes.txt", "athlete/profil", "script.py", "athlete/profil.md.sh"])
def test_rejects_non_markdown_extension(root, path):
    with pytest.raises(CoachFileError, match=r"\.md"):
        coach_files.resolve_file(root, path)


def test_rejects_md_symlink_to_non_md_file(root):
    (root / "notes.txt").write_text("x", encoding="utf-8")
    (root / "notes.md").symlink_to(root / "notes.txt")
    with pytest.raises(CoachFileError, match=r"\.md"):
        coach_files.read_file(root, "notes.md")


def test_rejects_content_over_size_limit(root):
    too_big = "x" * (coach_files.MAX_WRITE_BYTES + 1)
    with pytest.raises(CoachFileError, match="limit"):
        coach_files.write_file(root, "big.md", too_big, "msg")
    with pytest.raises(CoachFileError, match="limit"):
        coach_files.append_file(root, "big.md", too_big, "msg")
    assert not (root / "big.md").exists()


def test_requires_commit_message(root):
    with pytest.raises(CoachFileError, match="commit_message"):
        coach_files.write_file(root, "a.md", "x", "   ")


# --- read / write / append -----------------------------------------------------------------


def test_list_files(root):
    (root / "journal").mkdir()
    (root / "journal" / "2026-10.md").write_text("j", encoding="utf-8")
    (root / "journal" / "photo.jpg").write_bytes(b"\xff")
    entries = coach_files.list_files(root)
    assert [entry.path for entry in entries] == ["athlete/profil.md", "journal/2026-10.md"]
    assert entries[1].size == 1
    assert [entry.path for entry in coach_files.list_files(root, "journal")] == [
        "journal/2026-10.md"
    ]


def test_read_returns_content_and_sha(root):
    result = coach_files.read_file(root, "athlete/profil.md")
    assert result.content == "# Profil\n"
    assert result.sha256 == hashlib.sha256(b"# Profil\n").hexdigest()


def test_read_missing_file(root):
    with pytest.raises(CoachFileError, match="not found"):
        coach_files.read_file(root, "nope.md")


def test_write_creates_subdirectories(root):
    result = coach_files.write_file(root, "methode/new/reperes.md", "Zones\n", "add reperes")
    assert (root / "methode" / "new" / "reperes.md").read_text(encoding="utf-8") == "Zones\n"
    assert result.sha256 == hashlib.sha256(b"Zones\n").hexdigest()
    assert "not a git repository" in result.git_status
    assert not list((root / "methode" / "new").glob("*.tmp"))


def test_write_replaces_and_read_back(root):
    coach_files.write_file(root, "athlete/profil.md", "# Profil v2\n", "update")
    assert coach_files.read_file(root, "athlete/profil.md").content == "# Profil v2\n"


def test_append_adds_newline_when_needed(root):
    coach_files.write_file(root, "journal/2026-10.md", "line 1", "start")
    coach_files.append_file(root, "journal/2026-10.md", "line 2\n", "add")
    coach_files.append_file(root, "journal/2026-10.md", "line 3\n", "add")
    content = (root / "journal" / "2026-10.md").read_text(encoding="utf-8")
    assert content == "line 1\nline 2\nline 3\n"


def test_append_creates_missing_file(root):
    result = coach_files.append_file(root, "journal/2026-11.md", "first\n", "new month")
    assert (root / "journal" / "2026-11.md").read_text(encoding="utf-8") == "first\n"
    assert result.size == len(b"first\n")


# --- expected_sha256 -----------------------------------------------------------------------


def test_write_with_matching_sha(root):
    current = coach_files.read_file(root, "athlete/profil.md")
    coach_files.write_file(root, "athlete/profil.md", "v2\n", "msg", current.sha256)
    assert (root / "athlete" / "profil.md").read_text(encoding="utf-8") == "v2\n"


def test_write_with_stale_sha_is_refused(root):
    stale = coach_files.read_file(root, "athlete/profil.md").sha256
    (root / "athlete" / "profil.md").write_text("changed on the Mac\n", encoding="utf-8")
    with pytest.raises(CoachConflictError) as info:
        coach_files.write_file(root, "athlete/profil.md", "from phone\n", "msg", stale)
    assert info.value.current is not None
    assert info.value.current.content == "changed on the Mac\n"
    assert info.value.current.sha256 == hashlib.sha256(b"changed on the Mac\n").hexdigest()
    assert (root / "athlete" / "profil.md").read_text(encoding="utf-8") == "changed on the Mac\n"


def test_write_with_sha_for_missing_file_is_refused(root):
    with pytest.raises(CoachConflictError) as info:
        coach_files.write_file(root, "new.md", "x", "msg", "0" * 64)
    assert info.value.current is None
    assert not (root / "new.md").exists()


# --- git -----------------------------------------------------------------------------------


def test_write_creates_commit(repo):
    result = coach_files.write_file(repo, "athlete/objectifs.md", "Objectif\n", "Add objectifs")
    assert result.git_status.startswith("committed ")
    assert _git(repo, "log", "-1", "--format=%an <%ae>|%s") == (
        "Claude Coach <coach@tetsuo.local>|Add objectifs"
    )
    assert _git(repo, "show", "--name-only", "--format=", "HEAD") == "athlete/objectifs.md"
    assert _git(repo, "status", "--porcelain") == "?? .coach.lock"


def test_append_creates_commit(repo):
    coach_files.append_file(repo, "journal/2026-10.md", "Séance\n", "Journal 06/10")
    assert _git(repo, "log", "-1", "--format=%s") == "Journal 06/10"
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"


def test_commit_only_touches_written_file(repo):
    (repo / "other.md").write_text("untracked\n", encoding="utf-8")
    coach_files.write_file(repo, "athlete/profil.md", "v2\n", "update profil")
    assert _git(repo, "show", "--name-only", "--format=", "HEAD") == "athlete/profil.md"


def test_unchanged_content_makes_no_commit(repo):
    result = coach_files.write_file(repo, "athlete/profil.md", "# Profil\n", "same")
    assert "nothing to commit" in result.git_status
    assert _git(repo, "rev-list", "--count", "HEAD") == "1"


def test_commit_failure_keeps_write(repo, monkeypatch):
    monkeypatch.setattr(coach_files, "_git", lambda *_args: subprocess.CompletedProcess(
        args=[], returncode=128, stdout="", stderr="fatal: index.lock exists"
    ))
    result = coach_files.write_file(repo, "athlete/profil.md", "v2\n", "msg")
    assert result.git_status == "commit FAILED (git add): fatal: index.lock exists"
    assert (repo / "athlete" / "profil.md").read_text(encoding="utf-8") == "v2\n"


# --- MCP tools -----------------------------------------------------------------------------


def test_tools_are_registered():
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert {"list_coach_files", "read_coach_file", "write_coach_file", "append_coach_file"} <= names
    assert "get_activities" in names


def test_tools_without_coach_dir(monkeypatch):
    monkeypatch.delenv("COACH_DIR", raising=False)
    result = asyncio.run(list_coach_files())
    assert result.startswith("Error: COACH_DIR is not set")


def test_tools_with_missing_coach_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("COACH_DIR", str(tmp_path / "missing"))
    result = asyncio.run(read_coach_file("athlete/profil.md"))
    assert result.startswith("Error: COACH_DIR does not exist")


def test_tools_round_trip(monkeypatch, repo):
    monkeypatch.setenv("COACH_DIR", str(repo))
    assert "athlete/profil.md" in asyncio.run(list_coach_files())

    read = asyncio.run(read_coach_file("athlete/profil.md"))
    sha = read.splitlines()[1].removeprefix("sha256: ")
    assert read.endswith("---\n# Profil\n")

    written = asyncio.run(write_coach_file("athlete/profil.md", "v2\n", "update", sha))
    assert "Wrote athlete/profil.md" in written and "git: committed" in written

    conflict = asyncio.run(write_coach_file("athlete/profil.md", "v3\n", "update", sha))
    assert conflict.startswith("Conflict:") and conflict.endswith("---\nv2\n")

    appended = asyncio.run(append_coach_file("journal/2026-10.md", "J1\n", "journal"))
    assert "Appended to journal/2026-10.md" in appended

    refused = asyncio.run(read_coach_file("../etc/passwd.md"))
    assert refused.startswith("Error:")

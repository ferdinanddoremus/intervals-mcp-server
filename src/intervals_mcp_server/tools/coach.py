"""
Coach folder MCP tools.

These tools let Claude read and update the athlete's coaching notes: Markdown files
stored in the directory given by COACH_DIR (see intervals_mcp_server.coach_files).
If COACH_DIR is missing, they return an error and the rest of the server keeps working.
"""

import asyncio
from collections.abc import Callable

from intervals_mcp_server import coach_files
from intervals_mcp_server.coach_files import CoachConflictError, CoachFileError, WriteResult

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import mcp  # noqa: F401


def _format_write(action: str, result: WriteResult) -> str:
    return (
        f"{action} {result.path} ({result.size} bytes).\n"
        f"sha256: {result.sha256}\n"
        f"git: {result.git_status}"
    )


def _format_conflict(exc: CoachConflictError) -> str:
    if exc.current is None:
        return (
            f"Conflict: {exc.path} does not exist, but expected_sha256 was given. "
            "Nothing was written. Call again without expected_sha256 to create it."
        )
    return (
        f"Conflict: {exc.path} changed since it was read "
        f"(expected sha256 {exc.expected_sha256}, current {exc.current.sha256}). "
        "Nothing was written. Merge your changes into the current content below, "
        "then call write_coach_file again with the current sha256.\n"
        f"sha256: {exc.current.sha256}\n"
        "---\n"
        f"{exc.current.content}"
    )


def _list(subdir: str) -> str:
    root = coach_files.get_coach_dir()
    entries = coach_files.list_files(root, subdir)
    if not entries:
        return f"No .md files in {subdir or 'the coach folder'}."
    lines = [
        f"{entry.path}\t{entry.size} B\t{entry.modified.strftime('%Y-%m-%d %H:%M UTC')}"
        for entry in entries
    ]
    return "\n".join(lines)


def _read(path: str) -> str:
    result = coach_files.read_file(coach_files.get_coach_dir(), path)
    return f"path: {result.path}\nsha256: {result.sha256}\n---\n{result.content}"


def _write(path: str, content: str, commit_message: str, expected_sha256: str | None) -> str:
    root = coach_files.get_coach_dir()
    result = coach_files.write_file(root, path, content, commit_message, expected_sha256)
    return _format_write("Wrote", result)


def _append(path: str, content: str, commit_message: str) -> str:
    root = coach_files.get_coach_dir()
    result = coach_files.append_file(root, path, content, commit_message)
    return _format_write("Appended to", result)


async def _run(func: Callable[..., str], *args: str | None) -> str:
    """Run blocking file/git work off the event loop and turn errors into messages."""
    try:
        return await asyncio.to_thread(func, *args)
    except CoachConflictError as exc:
        return _format_conflict(exc)
    except CoachFileError as exc:
        return f"Error: {exc}"
    except OSError as exc:
        return f"Error: {exc.strerror or exc}"


@mcp.tool()
async def list_coach_files(subdir: str = "") -> str:
    """List the Markdown files of the athlete's coach folder (path, size, last modified UTC).

    Args:
        subdir: Optional sub-folder to list, relative to the coach folder (e.g. "journal")
    """
    return await _run(_list, subdir)


@mcp.tool()
async def read_coach_file(path: str) -> str:
    """Read a Markdown file from the coach folder. Returns its sha256 then its content.

    Args:
        path: File path relative to the coach folder (e.g. "athlete/profil.md")
    """
    return await _run(_read, path)


@mcp.tool()
async def write_coach_file(
    path: str,
    content: str,
    commit_message: str,
    expected_sha256: str | None = None,
) -> str:
    """Create or fully replace a Markdown file in the coach folder, and commit it to git.

    Pass the sha256 from read_coach_file as expected_sha256: if the file changed since
    (e.g. another session wrote it), nothing is written and the current content is returned.

    Args:
        path: File path relative to the coach folder; must end in .md (folders are created)
        content: The complete new file content (max 256 KB)
        commit_message: Short description of the change, used as the git commit message
        expected_sha256: sha256 of the version you read (optional, recommended for existing files)
    """
    return await _run(_write, path, content, commit_message, expected_sha256)


@mcp.tool()
async def append_coach_file(path: str, content: str, commit_message: str) -> str:
    """Append text to the end of a Markdown file in the coach folder (created if missing), and commit it.

    Use this for journal entries (e.g. "journal/2026-10.md") instead of rewriting the whole file.

    Args:
        path: File path relative to the coach folder; must end in .md
        content: Text to add; a newline is inserted first if the file does not end with one (max 256 KB)
        commit_message: Short description of the change, used as the git commit message
    """
    return await _run(_append, path, content, commit_message)

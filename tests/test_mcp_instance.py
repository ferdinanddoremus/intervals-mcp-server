"""
Unit tests for the shared FastMCP instance configuration.
"""

import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from intervals_mcp_server import mcp_instance  # pylint: disable=wrong-import-position


@pytest.fixture(autouse=True)
def restore_mcp_instance():
    """Put the original instance back after each test so later tests are unaffected."""
    original = mcp_instance.mcp
    yield
    mcp_instance.mcp = original


def test_host_and_port_default(monkeypatch: pytest.MonkeyPatch):
    """Without env vars, the server binds to localhost:8000."""
    monkeypatch.delenv("FASTMCP_HOST", raising=False)
    monkeypatch.delenv("FASTMCP_PORT", raising=False)
    module = importlib.reload(mcp_instance)
    assert module.mcp.settings.host == "127.0.0.1"
    assert module.mcp.settings.port == 8000


def test_host_and_port_from_env(monkeypatch: pytest.MonkeyPatch):
    """FASTMCP_HOST and FASTMCP_PORT override the defaults."""
    monkeypatch.setenv("FASTMCP_HOST", "0.0.0.0")
    monkeypatch.setenv("FASTMCP_PORT", "8765")
    module = importlib.reload(mcp_instance)
    assert module.mcp.settings.host == "0.0.0.0"
    assert module.mcp.settings.port == 8765

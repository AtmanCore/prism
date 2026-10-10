"""Shared fixtures: import server.py and tools/setup.py without touching the real install.

Both modules resolve their paths from their own location. Importing ``server`` would read the
real config.json, .env and data/state.json, so those reads are hidden during the import, and every
test then points the module paths and HOME at its own ``tmp_path``.
"""

from __future__ import annotations

import copy
import importlib.util
import queue
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
_HIDDEN = {PROJECT / name for name in ("config.json", "config.local.json", ".env", "data/state.json")}


def _import_server():
    real_exists, real_is_file = Path.exists, Path.is_file
    Path.exists = lambda self, *a, **k: False if self in _HIDDEN else real_exists(self, *a, **k)
    Path.is_file = lambda self, *a, **k: False if self in _HIDDEN else real_is_file(self, *a, **k)
    try:
        sys.path.insert(0, str(PROJECT))
        import server
    finally:
        Path.exists, Path.is_file = real_exists, real_is_file
    return server


def _import_setup():
    spec = importlib.util.spec_from_file_location("prism_setup", PROJECT / "tools" / "setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server_module = _import_server()
setup_module = _import_setup()
_PRISTINE_DEFAULTS = copy.deepcopy(server_module.DEFAULTS)


def _no_subprocess(*args, **kwargs):
    raise AssertionError(f"unexpected subprocess call: {args[0] if args else kwargs}")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PRISM_EXTRA_READ_TOOLS", raising=False)
    monkeypatch.delenv("JIRA_URL", raising=False)
    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    return home


@pytest.fixture
def home(_isolated) -> Path:
    return _isolated


@pytest.fixture
def srv(tmp_path, monkeypatch):
    """server.py with every path, global and piece of state pointed at this test's tmp_path."""
    s = server_module
    root = tmp_path / "root"
    data = root / "data"
    workspace = root / "workspace"
    root.mkdir()
    paths = {
        "ROOT": root,
        "DATA": data,
        "REPORTS": data / "reports",
        "WORKSPACE": workspace,
        "REPOS_DIR": workspace / "repos",
        "PRS_DIR": workspace / "prs",
        "STATE_FILE": data / "state.json",
        "KB_STAMP_FILE": data / "kb_refresh.json",
        "CONFIG_FILE": root / "config.json",
        "DRAIN_FILE": root / "service.drain",
    }
    for name, value in paths.items():
        monkeypatch.setattr(s, name, value)
    monkeypatch.setattr(s, "DEFAULTS", copy.deepcopy(_PRISTINE_DEFAULTS))
    monkeypatch.setattr(s, "CONFIG", copy.deepcopy(_PRISTINE_DEFAULTS))
    monkeypatch.setattr(s, "DOTENV", {})
    monkeypatch.setattr(s, "STORE", s.Store())
    monkeypatch.setattr(s, "ACTIVE_REVIEWS", set())
    monkeypatch.setattr(s, "BUSY", set())
    monkeypatch.setattr(s, "KB_RUN_LOCKS", {})
    monkeypatch.setattr(s, "RESP_QUEUED", set())
    monkeypatch.setattr(s, "RESP_JOBS", queue.Queue())
    monkeypatch.setattr(s, "RUNNING_PROCS", {})
    monkeypatch.setattr(s, "QUEUED", set())
    monkeypatch.setattr(s, "JOBS", queue.Queue())
    monkeypatch.setattr(s, "KB_REFRESH", {})
    monkeypatch.setattr(s, "APPROVE_CACHE", {})
    monkeypatch.setattr(s, "_WARNED_SERVER_RULES", set())
    monkeypatch.setattr(s, "_last_poll_started", 0.0)
    monkeypatch.setattr(s, "IN_DOCKER", False)
    s.CLEARING.clear()
    yield s
    s.CLEARING.clear()


@pytest.fixture
def setup_mod(tmp_path, monkeypatch, home):
    """tools/setup.py with ROOT, .env, config.json and the compose override inside tmp_path."""
    m = setup_module
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(m, "ROOT", root)
    monkeypatch.setattr(m, "ENV_FILE", root / ".env")
    monkeypatch.setattr(m, "CONFIG_FILE", root / "config.json")
    monkeypatch.setattr(m, "OVERRIDE_FILE", root / "docker-compose.override.yml")
    monkeypatch.setattr(m, "http_json", _no_subprocess)
    return m


@pytest.fixture
def answers(monkeypatch):
    """Feed scripted replies to input(); an exhausted script behaves like Ctrl-D (EOFError)."""

    def feed(*replies: str) -> list[str]:
        remaining = list(replies)

        def fake_input(prompt=""):
            if not remaining:
                raise EOFError
            return remaining.pop(0)

        monkeypatch.setattr("builtins.input", fake_input)
        return remaining

    return feed

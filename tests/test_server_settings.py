"""Config writes, the sync interval, local data, PR search queries and MCP permissions."""

from __future__ import annotations

import json
import os
import signal
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


class TestUnlistedUserMcpTools:
    """MCP tools pre-approved in ~/.claude/settings.json but not configured in PRism get denied."""

    @pytest.mark.parametrize("content", [None, "{not json", "[]", '{"permissions": null}'])
    def test_unreadable_or_empty_settings(self, srv, home, content):
        if content is not None:
            (home / ".claude").mkdir()
            (home / ".claude" / "settings.json").write_text(content)
        assert srv.unlisted_user_mcp_tools() == []

    def test_denies_only_unlisted_mcp_rules(self, srv, home, monkeypatch):
        monkeypatch.setenv("PRISM_EXTRA_READ_TOOLS", "mcp__conf__getPage, ")
        srv.CONFIG["tickets"]["read_tools"] = ["mcp__jira__getIssue"]
        (home / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": [
            42, "Bash(ls:*)", "mcp__jira__getIssue", "mcp__jira__*", "mcp__conf", "mcp__slack__post",
            "mcp__github",
        ]}}))
        assert srv.unlisted_user_mcp_tools() == ["mcp__slack__post", "mcp__github"]
        assert srv._WARNED_SERVER_RULES == {"mcp__jira__*", "mcp__conf"}
        assert srv.unlisted_user_mcp_tools() == ["mcp__slack__post", "mcp__github"]
        assert srv.denied_tools()[-2:] == ["mcp__slack__post", "mcp__github"]


class TestSaveConfigKey:
    """One key goes back to config.json; the rest of the user's file is kept."""

    def test_creates_the_file_and_reloads_config(self, srv):
        srv.save_config_key("poll_interval_sec", 120)
        assert json.loads(srv.CONFIG_FILE.read_text()) == {"poll_interval_sec": 120}
        assert srv.CONFIG["poll_interval_sec"] == 120
        assert srv.CONFIG["github"]["search_query"] == srv.DEFAULTS["github"]["search_query"]

    @pytest.mark.parametrize("rename_fails", [False, True])
    def test_keeps_other_keys(self, srv, monkeypatch, rename_fails):
        srv.CONFIG_FILE.write_text(json.dumps({"port": 7000, "github": {"orgs": ["acme"]}}))
        if rename_fails:
            def busy(self, target):
                raise OSError(16, "Device or resource busy")

            monkeypatch.setattr(Path, "replace", busy)
        srv.save_config_key("auto_review", False)
        assert json.loads(srv.CONFIG_FILE.read_text()) == {
            "port": 7000, "github": {"orgs": ["acme"]}, "auto_review": False}
        assert srv.CONFIG_FILE.read_text().endswith("}\n")
        assert not srv.CONFIG_FILE.with_suffix(".tmp").exists()
        assert (srv.CONFIG["port"], srv.CONFIG["github"]["orgs"], srv.CONFIG["auto_review"]) == (7000, ["acme"], False)


class TestSetSyncInterval:
    """The UI's sync interval picker."""

    @pytest.mark.parametrize("minutes", [0, 3, "5", None, 120])
    def test_rejects_unlisted_values(self, srv, minutes):
        with pytest.raises(ValueError, match="choose one of 1, 2, 5"):
            srv.set_sync_interval(minutes)
        assert not srv.CONFIG_FILE.exists()

    @pytest.mark.parametrize(
        "last_poll, next_poll",
        [(0.0, None), (1_800_000_000.0, "2027-01-15T08:10:00+00:00")],
    )
    def test_saves_and_reschedules(self, srv, monkeypatch, last_poll, next_poll):
        monkeypatch.setattr(srv, "_last_poll_started", last_poll)
        assert srv.set_sync_interval(10) == {"ok": True, "poll_interval_sec": 600, "next_poll": next_poll}
        assert json.loads(srv.CONFIG_FILE.read_text()) == {"poll_interval_sec": 600}
        assert srv.CONFIG["poll_interval_sec"] == 600


def _populate(srv):
    run_dir = srv.REPORTS / "org__repo__1" / "20260101-120000"
    run_dir.mkdir(parents=True)
    (run_dir / "findings.json").write_text("x" * 100)
    (srv.REPORTS / "org__repo__1" / "not-a-run").mkdir()
    (srv.REPORTS / "org__repo__1" / "chat.json").write_text("y" * 10)
    (srv.REPOS_DIR / "org__repo").mkdir(parents=True)
    (srv.REPOS_DIR / "org__repo" / "HEAD").write_text("z" * 5)
    (srv.WORKSPACE / "link").symlink_to(srv.REPOS_DIR / "org__repo" / "HEAD")
    srv.STORE.prs["org__repo__1"] = {"key": "org__repo__1", "repo": "org/repo"}
    srv.STORE.save()


class TestDataSummary:
    """What "Clear local data" would delete."""

    def test_empty_install(self, srv):
        assert srv.data_summary() == {"data_dir": str(srv.DATA), "workspace_dir": str(srv.WORKSPACE),
                                      "reports": 0, "prs": 0, "bytes": 0, "running": 0, "busy": 0}
        assert srv.clear_local_data("clear") == {"ok": True, "cancelled": 0}
        assert all(d.is_dir() for d in (srv.REPORTS, srv.REPOS_DIR, srv.PRS_DIR))

    def test_counts_runs_bytes_and_running(self, srv, monkeypatch):
        _populate(srv)
        (srv.DATA / "unreadable").write_text("q" * 1000)
        real_is_file = Path.is_file

        def flaky_is_file(self, *a, **k):
            if self.name == "unreadable":
                raise OSError("permission denied")
            return real_is_file(self, *a, **k)

        monkeypatch.setattr(Path, "is_file", flaky_is_file)
        srv.ACTIVE_REVIEWS.add("org__repo__1")
        summary = srv.data_summary()
        state_bytes = srv.STATE_FILE.stat().st_size
        assert (summary["reports"], summary["prs"], summary["running"]) == (1, 1, 1)
        assert summary["bytes"] == 100 + 10 + 5 + state_bytes


class TestClearLocalData:
    """Deleting local data stops the queue and running reviews first."""

    @pytest.mark.parametrize("confirm", ["", None, "yes", "clea"])
    def test_needs_typed_confirmation(self, srv, confirm):
        _populate(srv)
        with pytest.raises(ValueError, match='type "clear"'):
            srv.clear_local_data(confirm)
        assert srv.STATE_FILE.exists()

    def test_wipes_everything_and_drains_the_queue(self, srv, tmp_path):
        _populate(srv)
        outside = tmp_path / "kb"
        outside.mkdir()
        (outside / "keep.md").write_text("kb")
        (srv.DATA / "kb-link").symlink_to(outside)
        srv.JOBS.put(("org__repo__1", "new"))
        srv.QUEUED.add("org__repo__1")
        srv.STORE.meta.update(last_poll="2026-01-01", viewer="me")
        assert srv.clear_local_data("  Clear ") == {"ok": True, "cancelled": 0}
        assert sorted(p.name for p in srv.DATA.iterdir()) == ["reports", "state.json"]
        assert sorted(p.name for p in srv.WORKSPACE.iterdir()) == ["prs", "repos"]
        assert list(srv.REPORTS.iterdir()) == [] and list(srv.REPOS_DIR.iterdir()) == []
        assert (outside / "keep.md").exists()
        assert srv.JOBS.empty() and not srv.QUEUED and not srv.CLEARING.is_set()
        assert json.loads(srv.STATE_FILE.read_text()) == {
            "prs": {}, "meta": {"last_poll": None, "last_poll_error": None, "next_poll": None, "viewer": "me"}}

    @pytest.mark.parametrize("kill_error", [None, ProcessLookupError, PermissionError])
    def test_stops_running_reviews_first(self, srv, monkeypatch, kill_error):
        _populate(srv)
        srv.ACTIVE_REVIEWS.add("org__repo__1")
        srv.RUNNING_PROCS["org__repo__1"] = SimpleNamespace(pid=4242)
        killed = []

        def fake_killpg(pid, sig):
            killed.append((pid, sig))
            if kill_error:
                raise kill_error

        monkeypatch.setattr(os, "killpg", fake_killpg)
        monkeypatch.setattr(srv.time, "sleep", lambda s: srv.ACTIVE_REVIEWS.clear())
        assert srv.clear_local_data("clear") == {"ok": True, "cancelled": 1}
        assert killed == [(4242, signal.SIGTERM)]
        assert srv.STORE.prs == {}

    def test_gives_up_when_reviews_do_not_stop(self, srv, monkeypatch):
        _populate(srv)
        srv.ACTIVE_REVIEWS.add("org__repo__1")
        clock = iter(range(0, 10_000, 60))
        monkeypatch.setattr(srv.time, "time", lambda: next(clock))
        monkeypatch.setattr(srv.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="still finishing"):
            srv.clear_local_data("clear")
        assert "org__repo__1" in srv.STORE.prs
        assert not srv.CLEARING.is_set()


class TestSearchQueries:
    """GitHub search strings built from config."""

    @pytest.mark.parametrize("orgs, suffix", [([], ""), (None, ""), (["acme", "beta"], " org:acme org:beta")])
    def test_search_and_authored(self, srv, orgs, suffix):
        srv.CONFIG["github"]["orgs"] = orgs
        assert srv.search_query() == srv.CONFIG["github"]["search_query"] + suffix
        assert srv.authored_query() == "is:pr is:open author:@me" + suffix

    @pytest.mark.parametrize(
        "my_prs, authored, auto",
        [
            (None, "is:pr is:open author:@me", False),
            ({}, "is:pr is:open author:@me", False),
            ({"enabled": False, "auto_review": True}, None, True),
            ({"enabled": True, "auto_review": True}, "is:pr is:open author:@me", True),
        ],
    )
    def test_my_prs_settings(self, srv, my_prs, authored, auto):
        srv.CONFIG["my_prs"] = my_prs
        assert (srv.authored_query(), srv.my_prs_auto()) == (authored, auto)

    def test_my_activity_disabled(self, srv):
        srv.CONFIG["github"]["include_my_reviews"] = False
        assert srv.my_activity_queries() == []

    @pytest.mark.parametrize("orgs, org_part", [([], ""), (["acme"], " org:acme")])
    def test_my_activity_window(self, srv, monkeypatch, orgs, org_part):
        now = 1_800_000_000.0
        monkeypatch.setattr(srv.time, "time", lambda: now)
        srv.CONFIG["github"].update(orgs=orgs, my_reviews_window_days=7)
        since = datetime.fromtimestamp(now - 7 * 86400).strftime("%Y-%m-%d")
        base = f"is:pr is:open draft:false -author:@me updated:>={since}{org_part}"
        assert srv.my_activity_queries() == [f"{base} reviewed-by:@me", f"{base} commenter:@me"]

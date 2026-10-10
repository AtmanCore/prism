"""Background-work coordination and config safety: the things that keep PRism running unattended."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

KEY = "org__repo__7"


def _pr(srv, **fields):
    pr = {"key": KEY, "repo": "org/repo", "number": 7, "head_sha": "a" * 40, "author": "alice", "active": True}
    pr.update(fields)
    srv.STORE.prs[KEY] = pr
    return pr


class TestConfig:
    def test_save_without_config_file_keeps_the_defaults(self, srv):
        srv.CONFIG.clear()
        srv.CONFIG.update(srv.load_config())
        srv.save_config_key("poll_interval_sec", 600)
        assert (srv.CONFIG["poll_interval_sec"], srv.CONFIG["github"]["orgs"]) == (600, [])
        assert srv.DEFAULTS["poll_interval_sec"] == 300

    def test_local_override_is_reported(self, srv):
        (srv.ROOT / "config.local.json").write_text(json.dumps({"poll_interval_sec": 120}))
        with pytest.raises(ValueError, match="config.local.json overrides poll_interval_sec"):
            srv.save_config_key("poll_interval_sec", 600)

    @pytest.mark.parametrize("minutes", [True, 5.0, "5", None])
    def test_sync_interval_needs_an_exact_whole_number(self, srv, minutes):
        with pytest.raises(ValueError, match="choose one of"):
            srv.set_sync_interval(minutes)


class TestStore:
    def test_update_of_an_unknown_pr_is_ignored(self, srv):
        srv.STORE.update_pr("gone__pr__1", responses_checking=False)
        assert "gone__pr__1" not in srv.STORE.prs

    def test_update_of_a_known_pr(self, srv):
        _pr(srv)
        srv.STORE.update_pr(KEY, progress="x")
        assert srv.STORE.prs[KEY]["progress"] == "x"


class TestPoller:
    def test_a_crashing_poll_is_recorded_and_the_loop_goes_on(self, srv, monkeypatch):
        polls = []

        def boom():
            polls.append(1)
            if len(polls) == 2:
                raise SystemExit
            raise KeyError("title")

        monkeypatch.setattr(srv, "poll_once", boom)
        monkeypatch.setattr(srv.POLL_NOW, "wait", lambda timeout: True)
        with pytest.raises(SystemExit):
            srv.poller_loop()
        assert len(polls) == 2 and "internal error" in srv.STORE.meta["last_poll_error"]


class TestBusyAndClear:
    def test_busy_refuses_while_clearing(self, srv):
        srv.CLEARING.set()
        with pytest.raises(RuntimeError, match="being cleared"):
            with srv.busy("chat:x"):
                pass

    def test_busy_registers_and_releases(self, srv):
        with srv.busy("reply:x"):
            assert srv.BUSY == {"reply:x"}
        assert srv.BUSY == set()

    def test_clear_drains_reply_checks_and_waits_for_other_jobs(self, srv, monkeypatch):
        srv.RESP_JOBS.put((KEY, False))
        srv.RESP_QUEUED.add(KEY)
        srv.BUSY.add("chat:x")
        monkeypatch.setattr(srv.time, "sleep", lambda s: srv.BUSY.clear())
        assert srv.clear_local_data("clear") == {"ok": True, "cancelled": 0}
        assert srv.RESP_JOBS.empty() and not srv.RESP_QUEUED

    def test_clear_gives_up_on_a_stuck_job(self, srv, monkeypatch):
        srv.BUSY.add("kb:big")
        clock = iter(range(0, 1000, 50))
        monkeypatch.setattr(srv.time, "time", lambda: next(clock))
        monkeypatch.setattr(srv.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="still finishing"):
            srv.clear_local_data("clear")
        assert not srv.CLEARING.is_set()


class TestApprovePinsTheShownCommit:
    @pytest.fixture
    def posted(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(json.loads(kwargs["input"]))
            return SimpleNamespace(returncode=0, stdout='{"html_url": "u"}', stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    def test_refuses_when_the_pr_moved_on(self, srv, posted):
        _pr(srv, head_sha="b" * 40)
        with pytest.raises(srv.StaleApproval, match="new commits"):
            srv.approve_pr(KEY, "approve", expected_sha="a" * 40)
        assert posted == []

    def test_approves_the_shown_commit(self, srv, posted):
        _pr(srv)
        srv.approve_pr(KEY, "approve", expected_sha="a" * 40)
        assert posted == [{"commit_id": "a" * 40, "event": "APPROVE"}]


class TestApproveCheckCaching:
    @pytest.mark.parametrize(
        "user_reply, why",
        [
            (SimpleNamespace(returncode=1, stdout="", stderr="network"), "couldn't reach GitHub"),
            (subprocess.TimeoutExpired("gh", 30), "timed out"),
        ],
    )
    def test_transient_failures_are_not_cached(self, srv, monkeypatch, user_reply, why):
        def fake_run(cmd, **kwargs):
            if isinstance(user_reply, Exception):
                raise user_reply
            return user_reply

        monkeypatch.setattr(subprocess, "run", fake_run)
        can, reason = srv.token_can_approve("org/repo", 7)
        assert (can, why in reason, srv.APPROVE_CACHE) == (False, True, {})

    def test_unclear_probe_status_is_not_cached(self, srv, monkeypatch):
        def fake_run(cmd, **kwargs):
            out = "HTTP/2.0 200 OK\n" if "user" in cmd else "HTTP/2.0 502 Bad Gateway\n"
            return SimpleNamespace(returncode=0, stdout=out, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert srv.token_can_approve("org/repo", 7) == (False, "couldn't check the token's permission (HTTP 502)")
        assert srv.APPROVE_CACHE == {}


class TestDecisionIds:
    def test_ids_are_not_reused_after_an_undo(self, srv):
        srv.save_decisions(KEY, [{"id": "D1"}, {"id": "D2"}])
        srv.save_decisions(KEY, [{"id": "D1"}])
        assert srv.decision_seq(KEY) == 2


class TestKbRepoNames:
    @pytest.mark.parametrize("repo", ["..", ".", "org/..", "../x", "org", "org/a/b", "", None])
    def test_rejects_anything_but_owner_slash_name(self, srv, tmp_path, repo):
        with pytest.raises(ValueError, match="not a GitHub repo name"):
            srv.sync_kb_repo(tmp_path, repo, None)


class TestKbRunLock:
    def test_pre_review_refresh_skips_a_kb_that_is_refreshing(self, srv, tmp_path, monkeypatch):
        srv.CONFIG["knowledge_bases"] = [{"id": "kb1", "name": "KB", "path": str(tmp_path),
                                          "refresh_command": ["tool", "{service}"], "refresh_max_age_hours": 24}]
        monkeypatch.setattr(srv, "run", lambda *a, **k: pytest.fail("ran while another refresh held the lock"))
        srv.kb_run_lock("kb1").acquire()
        notes = srv.refresh_kbs([{"id": "kb1", "name": "KB", "services": ["svc"]}], lambda m: None)
        assert notes == ["KB: a refresh is already running; used existing content"]


class TestOriginGuard:
    @pytest.mark.parametrize("origin, ok", [("http://[::1]:6161", True), ("http://[::1]:9999", False)])
    def test_ipv6_loopback_origin(self, srv, origin, ok):
        handler = SimpleNamespace(headers={"Origin": origin, "Content-Type": "application/json"},
                                  server=SimpleNamespace(server_address=("127.0.0.1", 6161)))
        assert srv.Handler._same_origin(handler) is ok


class TestMyPrsOverride:
    def test_dict_setting_overridden_locally_is_reported(self, srv):
        (srv.ROOT / "config.local.json").write_text(json.dumps({"my_prs": {"auto_review": False}}))
        with pytest.raises(ValueError, match="config.local.json overrides my_prs"):
            srv.save_config_key("my_prs", {"enabled": True, "auto_review": True})

    def test_dict_setting_saved(self, srv):
        srv.save_config_key("my_prs", {"enabled": True, "auto_review": True})
        assert srv.CONFIG["my_prs"]["auto_review"] is True


class TestKbRefreshLockRelease:
    @pytest.fixture
    def kb(self, srv, tmp_path):
        kb = {"id": "kb1", "name": "KB", "path": str(tmp_path), "refresh_command": ["tool"]}
        srv.CONFIG["knowledge_bases"] = [kb]
        return kb

    @pytest.mark.parametrize("outcome, state", [("ok", "done"), ("raise", "failed"), ("clearing", "failed")])
    def test_lock_is_released_whatever_happens(self, srv, kb, monkeypatch, outcome, state):
        def refresh(kb, note):
            if outcome == "raise":
                raise RuntimeError("tool crashed")

        monkeypatch.setattr(srv, "_kb_refresh_command", refresh)
        if outcome == "clearing":
            srv.CLEARING.set()
        lock = srv.kb_run_lock("kb1")
        lock.acquire()
        job = {"state": "running", "log": [], "error": None, "_lock": lock}
        srv._kb_refresh_job(kb, "command", job)
        assert (job["state"], lock.locked(), "_lock" in job) == (state, False, False)

    def test_thread_that_cannot_start_releases_the_lock(self, srv, kb, monkeypatch):
        monkeypatch.setattr(srv.shutil, "which", lambda exe: "/usr/bin/tool")

        class NoThread:
            def __init__(self, **kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        monkeypatch.setattr(srv.threading, "Thread", NoThread)
        with pytest.raises(RuntimeError, match="can't start"):
            srv.start_kb_refresh("kb1")
        assert (srv.kb_run_lock("kb1").locked(), "kb1" in srv.KB_REFRESH) == (False, False)


class TestKbSkillRefreshHasNoShell:
    def test_claude_gets_read_and_kb_scoped_writes_only(self, srv, tmp_path, monkeypatch):
        d = tmp_path / "kb" / "svc"
        d.mkdir(parents=True)
        (d / "sources.json").write_text(json.dumps({"generated_by": "prism-kb-builder", "repo": "org/svc"}))
        monkeypatch.setattr(srv, "sync_kb_repo", lambda root, repo, ref: tmp_path / ".cache" / "repos" / "org__svc")
        monkeypatch.setattr(srv, "run", lambda cmd, cwd=None, **k: "c0ffee\n")
        seen = {}

        def fake_claude(cmd, **kwargs):
            seen["cmd"], seen["prompt"] = cmd, kwargs["input"]
            return SimpleNamespace(returncode=0, stdout=json.dumps({"result": "Updated svc."}), stderr="")

        monkeypatch.setattr(subprocess, "run", fake_claude)
        srv._kb_refresh_skill({"id": "kb1", "path": str(tmp_path)}, lambda m: None)
        cmd = seen["cmd"]
        start = cmd.index("--allowedTools") + 1
        allowed = cmd[start:next(i for i in range(start, len(cmd)) if cmd[i].startswith("--"))]
        assert "--strict-mcp-config" in cmd and not [t for t in allowed if t.startswith("Bash")]
        assert "Bash" in seen["cmd"][seen["cmd"].index("--disallowedTools") + 1:]
        assert all(t in ("Read", "Grep", "Glob") or f"/{tmp_path.resolve()}/" in t for t in allowed)
        assert "commit c0ffee" in seen["prompt"]


class TestChatLockRelease:
    @pytest.fixture
    def chat_pr(self, srv):
        _pr(srv)
        run_dir = srv.REPORTS / KEY / "20260101-120000"
        run_dir.mkdir(parents=True)
        (run_dir / "findings.json").write_text(json.dumps({"findings": []}))
        return run_dir

    @pytest.mark.parametrize("clearing", [False, True])
    def test_lock_is_released_when_the_turn_fails(self, srv, chat_pr, monkeypatch, clearing):
        monkeypatch.setattr(srv, "_chat_turn", lambda *a: (_ for _ in ()).throw(RuntimeError("claude failed")))
        if clearing:
            srv.CLEARING.set()
        with pytest.raises(RuntimeError):
            srv.chat_turn(KEY, "20260101-120000", "why F1?")
        assert not srv.chat_lock(KEY).locked() and srv.BUSY == set()


class TestApproveNeedsTheShownCommit:
    def test_missing_commit_is_refused(self, srv):
        _pr(srv)
        with pytest.raises(ValueError, match="commit you're approving is missing"):
            srv.approve_pr(KEY, "approve")


class TestKbRefNames:
    @pytest.mark.parametrize("ref", ["--upload-pack=x", "-x", "a..b", "", "../main"])
    def test_rejects_option_like_or_odd_refs(self, srv, tmp_path, ref):
        with pytest.raises(ValueError, match="not a branch or tag name"):
            srv.sync_kb_repo(tmp_path, "org/repo", ref)

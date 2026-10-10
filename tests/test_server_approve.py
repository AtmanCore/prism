"""Approving a PR: token permission check, the Approve button state and the approval itself."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

KEY = "org__repo__7"
RUN_ID = "20260101-120000"


@pytest.fixture
def gh(monkeypatch):
    """Fake `gh api`: answers by the request kind and records every call."""
    replies = {"user": "", "delete": "", "post": SimpleNamespace(returncode=0, stdout="{}", stderr="")}
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:4] == ["gh", "api", "-i", "user"]:
            return SimpleNamespace(returncode=0, stdout=replies["user"], stderr="")
        if "DELETE" in cmd:
            return SimpleNamespace(returncode=0, stdout=replies["delete"], stderr="")
        return replies["post"]

    monkeypatch.setattr(subprocess, "run", fake_run)
    return SimpleNamespace(replies=replies, calls=calls)


def _pr(srv, **fields):
    pr = {"key": KEY, "repo": "org/repo", "number": 7, "head_sha": "a" * 40, "author": "alice", "active": True}
    pr.update(fields)
    srv.STORE.prs[KEY] = pr
    return pr


class TestTokenCanApprove:
    """Classic tokens are judged by their scopes header, fine-grained ones by a harmless DELETE probe."""

    @pytest.mark.parametrize(
        "user_head, probe, can, why",
        [
            ("HTTP/2.0 200 OK\nX-OAuth-Scopes: read:org, repo\n", "", True, ""),
            ("HTTP/2.0 200 OK\nx-oauth-scopes: public_repo\n", "", False, "doesn't have the 'repo' scope"),
            ("HTTP/2.0 200 OK\nX-OAuth-Scopes: read:org\n", "", False, "doesn't have the 'repo' scope"),
            ("HTTP/2.0 200 OK\n", "HTTP/2.0 404 Not Found\n", True, ""),
            ("HTTP/2.0 200 OK\n", "HTTP/2.0 422 Unprocessable\n", True, ""),
            ("HTTP/2.0 200 OK\n", "HTTP/2.0 403 Forbidden\n", False, "can't write pull requests"),
            ("HTTP/2.0 200 OK\n", "HTTP/2.0 500 Oops\n", False, "(HTTP 500)"),
            ("", "", False, "(HTTP ?)"),
        ],
    )
    def test_decides_from_github_answers(self, srv, gh, user_head, probe, can, why):
        gh.replies.update(user=user_head, delete=probe)
        got_can, got_why = srv.token_can_approve("org/repo", 7)
        assert got_can is can and why in got_why and (why or got_why == "")
        probes = [c for c, _ in gh.calls if "DELETE" in c]
        assert probes == ([] if "scopes" in user_head.lower() else
                          [["gh", "api", "-i", "-X", "DELETE", "repos/org/repo/pulls/7/reviews/1"]])

    def test_caches_per_token_and_repo(self, srv, gh, monkeypatch):
        gh.replies["user"] = "X-OAuth-Scopes: repo\n"
        clock = [1000.0]
        monkeypatch.setattr(srv.time, "time", lambda: clock[0])
        srv.DOTENV["GITHUB_TOKEN"] = "ghp_one"
        assert srv.token_can_approve("org/repo", 7) == (True, "")
        assert srv.token_can_approve("org/repo", 8) == (True, "")
        assert len(gh.calls) == 1
        srv.token_can_approve("org/other", 1)
        srv.DOTENV["GITHUB_TOKEN"] = "ghp_two"
        srv.token_can_approve("org/repo", 7)
        clock[0] += srv.APPROVE_CACHE_SEC
        srv.token_can_approve("org/repo", 7)
        assert len(gh.calls) == 4
        assert gh.calls[0][1]["env"]["GH_TOKEN"] == "ghp_one"


class TestApproveCheck:
    """Whether the Approve button shows, and what it warns about."""

    @pytest.mark.parametrize(
        "fields, viewer, reason",
        [
            ({"active": False}, "bob", "the PR isn't open"),
            ({"closed_state": "merged"}, "bob", "the PR isn't open"),
            ({"author": "bob"}, "bob", "you can't approve your own PR"),
        ],
    )
    def test_refuses_without_asking_github(self, srv, gh, fields, viewer, reason):
        _pr(srv, **fields)
        srv.STORE.meta["viewer"] = viewer
        assert srv.approve_check(KEY) == {"can": False, "reason": reason}
        assert gh.calls == []

    @pytest.mark.parametrize(
        "last_run, findings, reviewed_sha, new_commits",
        [
            (None, None, None, False),
            ("../escape", None, None, False),
            (RUN_ID, None, None, False),
            (RUN_ID, "{broken", None, False),
            (RUN_ID, json.dumps({"meta": {"head_sha": "a" * 40}}), "a" * 40, False),
            (RUN_ID, json.dumps({"meta": {"head_sha": "b" * 40}}), "b" * 40, True),
        ],
    )
    def test_reports_review_freshness(self, srv, gh, last_run, findings, reviewed_sha, new_commits):
        gh.replies["user"] = "X-OAuth-Scopes: repo\n"
        _pr(srv, last_run=last_run, my_verdict="APPROVED", author="bob")
        if findings is not None or last_run == RUN_ID:
            run_dir = srv.REPORTS / KEY / RUN_ID
            run_dir.mkdir(parents=True)
            if findings is not None:
                (run_dir / "findings.json").write_text(findings)
        assert srv.approve_check(KEY) == {
            "can": True, "reason": "", "already_approved": True, "head_sha": "a" * 40,
            "reviewed_sha": reviewed_sha, "new_commits_since_review": new_commits}


class TestApprovePr:
    """The approval itself, only from the typed-confirm dialog."""

    @pytest.mark.parametrize("confirm", ["", None, "yes", "approved"])
    def test_needs_typed_confirmation(self, srv, gh, confirm):
        _pr(srv)
        with pytest.raises(ValueError, match='type "approve"'):
            srv.approve_pr(KEY, confirm)
        assert gh.calls == []

    @pytest.mark.parametrize(
        "body, payload_body",
        [("", None), ("   ", None), ("  LGTM  ", "LGTM"), ("x" * 2500, "x" * 2000)],
    )
    def test_approves_at_the_seen_head(self, srv, gh, body, payload_body):
        _pr(srv)
        gh.replies["post"] = SimpleNamespace(returncode=0, stdout='{"html_url": "https://gh/r/1"}', stderr="")
        assert srv.approve_pr(KEY, " Approve ", body, expected_sha="a" * 40) == {"ok": True, "url": "https://gh/r/1"}
        cmd, kwargs = gh.calls[0]
        assert cmd == ["gh", "api", "-X", "POST", "repos/org/repo/pulls/7/reviews", "--input", "-"]
        expected = {"commit_id": "a" * 40, "event": "APPROVE"}
        if payload_body:
            expected["body"] = payload_body
        assert json.loads(kwargs["input"]) == expected
        pr = srv.STORE.prs[KEY]
        assert (pr["my_verdict"], pr["my_state"]) == ("APPROVED", "reviewed")
        assert json.loads(srv.STATE_FILE.read_text())["prs"][KEY]["my_verdict"] == "APPROVED"

    @pytest.mark.parametrize(
        "stderr, stdout, message",
        [("HTTP 422: Can not approve your own pull request\n", "", "Can not approve"),
         ("", '{"message": "Not Found"}', "Not Found")],
    )
    def test_github_refusal_is_raised(self, srv, gh, stderr, stdout, message):
        _pr(srv)
        gh.replies["post"] = SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)
        with pytest.raises(RuntimeError, match=message):
            srv.approve_pr(KEY, "approve", expected_sha="a" * 40)
        assert "my_verdict" not in srv.STORE.prs[KEY]

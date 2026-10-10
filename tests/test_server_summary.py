"""Posting all open findings of a run as one GitHub summary comment, and its two HTTP routes."""

from __future__ import annotations

import io
import json
import subprocess
from http import HTTPStatus
from types import SimpleNamespace

import pytest

KEY = "org__repo__7"
RUN_ID = "20260101-120000"
PR_SHA = "a" * 40


def _finding(fid, severity, **fields):
    return {"id": fid, "severity": severity, "title": f"Title {fid}", **fields}


def _setup_run(srv, data, posted=None):
    srv.STORE.prs[KEY] = {"key": KEY, "repo": "org/repo", "number": 7, "head_sha": PR_SHA}
    run_dir = srv.REPORTS / KEY / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "findings.json").write_text(json.dumps(data))
    if posted is not None:
        (run_dir / "posted.json").write_text(json.dumps(posted))
    return run_dir


@pytest.fixture
def gh(monkeypatch):
    """Fake `gh api` POST: returns the configured reply and records every call."""
    state = SimpleNamespace(reply=SimpleNamespace(returncode=0, stdout='{"html_url": "https://gh/r/1"}', stderr=""),
                            calls=[])

    def fake_run(cmd, **kwargs):
        state.calls.append((cmd, kwargs))
        return state.reply

    monkeypatch.setattr(subprocess, "run", fake_run)
    return state


class TestBuildSummaryComment:
    """The markdown comment: severity groups, counts, per-finding fields and size limit."""

    def test_no_open_findings(self, srv):
        data = {"summary": "ignored", "findings": [_finding("f1", "high", dismissed=True)]}
        assert srv.build_summary_comment(data) == (
            "## Review summary\n\nI went through this PR and didn't find anything else that needs changing.\n", [])

    def test_groups_by_severity_and_skips_dismissed(self, srv):
        data = {
            "summary": "  Looks\nmostly fine. ",
            "findings": [
                _finding("l1", "low"),
                _finding("c1", "critical"),
                _finding("d1", "critical", dismissed=True),
                _finding("l2", "low"),
                _finding("h1", "high"),
            ],
        }
        body, ids = srv.build_summary_comment(data)
        assert ids == ["c1", "h1", "l1", "l2"]
        assert "**4 things** worth a look (1 critical, 1 high, 2 low)." in body
        assert "\nLooks mostly fine.\n" in body
        assert "### Medium" not in body and "Title d1" not in body
        order = [body.index(s) for s in ("### Critical", "**1. Title c1**", "### High", "**2. Title h1**",
                                         "### Low", "**3. Title l1**", "**4. Title l2**")]
        assert order == sorted(order)
        assert body.endswith("\n") and not body.endswith("\n\n")

    @pytest.mark.parametrize(
        "fields, heading, absent",
        [
            ({"file": "a.py", "line": 12, "ref": "x"}, "**1. Title f1** · `a.py:12`", []),
            ({"file": "a.py", "line": None}, "**1. Title f1** · `a.py`", []),
            ({"ref": " PR\ndescription "}, "**1. Title f1** · PR description", []),
            ({"title": "  "}, "**1. Finding**\n", ["·"]),
        ],
    )
    def test_where_variants(self, srv, fields, heading, absent):
        body, _ = srv.build_summary_comment({"findings": [_finding("f1", "medium", **fields)]})
        assert "**1 thing** worth a look (1 medium)." in body
        assert heading in body
        assert not [a for a in absent if a in body]

    @pytest.mark.parametrize(
        "fields, present, absent",
        [
            (
                {"why": "a\nb", "impact": "c", "fix": "  use:\n```py\nx = 1\n```  "},
                ["- **What's wrong:** a b", "- **Why it matters:** c",
                 "- **How to fix:** use:\n  ```py\n  x = 1\n  ```"],
                [],
            ),
            ({"why": " ", "impact": None, "fix": "\n"}, [], ["What's wrong", "Why it matters", "How to fix"]),
        ],
    )
    def test_optional_fields(self, srv, fields, present, absent):
        body, _ = srv.build_summary_comment({"findings": [_finding("f1", "low", **fields)]})
        assert all(p in body for p in present)
        assert not [a for a in absent if a in body]

    def test_stops_at_whole_findings_over_the_github_limit(self, srv, monkeypatch):
        data = {"findings": [_finding(f"f{i}", "high", why="w" * 40) for i in range(20)]}
        full, _ = srv.build_summary_comment(data)
        monkeypatch.setattr(srv, "GITHUB_BODY_LIMIT", 600)
        body, ids = srv.build_summary_comment(data)
        kept, note = body.split("\n\n_(", 1)
        assert 0 < len(ids) < 20 and len(kept) <= 600 and full.startswith(kept)
        assert note == f"{20 - len(ids)} more findings didn't fit in one comment. Post them separately.)_\n"
        assert [f"**{i + 1}." in kept for i in range(len(ids))] == [True] * len(ids)

    def test_leaves_out_posted_and_unknown_severity_findings(self, srv):
        data = {"findings": [_finding("f1", "high"), _finding("f2", "high"), _finding("f3", "nit")]}
        body, ids = srv.build_summary_comment(data, {"f1": {"url": "u"}})
        assert ids == ["f2"] and "found **1 thing** worth a look (1 high)" in body

    def test_fix_that_opens_with_a_code_fence_starts_on_its_own_line(self, srv):
        data = {"findings": [_finding("f1", "high", fix="```py\nx = 1\n```")]}
        body, _ = srv.build_summary_comment(data)
        assert "- **How to fix:**\n  ```py\n  x = 1\n  ```" in body


class TestSummaryDraft:
    """The draft is read from the run's findings.json."""

    def test_reads_run_findings(self, srv):
        _setup_run(srv, {"findings": [_finding("f1", "high")]})
        draft = srv.summary_draft(KEY, RUN_ID)
        assert draft["finding_ids"] == ["f1"] and "**1. Title f1**" in draft["body"]

    def test_unknown_run(self, srv):
        with pytest.raises(ValueError, match="unknown run"):
            srv.summary_draft(KEY, RUN_ID)


class TestPostSummary:
    """Posting the approved body as one COMMENT review and recording what it covered."""

    @pytest.mark.parametrize(
        "body, message",
        [(None, "empty"), ("  \n ", "empty"), ("x" * 65001, "longer than GitHub allows")],
    )
    def test_rejects_bad_body(self, srv, gh, body, message):
        with pytest.raises(ValueError, match=message):
            srv.post_summary(KEY, RUN_ID, body, [])
        assert gh.calls == []

    @pytest.mark.parametrize(
        "stderr, stdout, message",
        [(" boom \n", "", "boom"), ("", " out ", "out")],
    )
    def test_gh_failure(self, srv, gh, stderr, stdout, message):
        run_dir = _setup_run(srv, {"findings": [_finding("f1", "high")]})
        gh.reply = SimpleNamespace(returncode=1, stdout=stdout, stderr=stderr)
        with pytest.raises(RuntimeError, match=f"^{message}$"):
            srv.post_summary(KEY, RUN_ID, "hello", ["f1"])
        assert not (run_dir / "posted.json").exists()

    @pytest.mark.parametrize(
        "meta, sha",
        [({"head_sha": "b" * 40}, "b" * 40), ({}, PR_SHA), (None, PR_SHA)],
    )
    def test_success_records_known_findings(self, srv, gh, meta, sha):
        data = {"findings": [_finding("f1", "high"), _finding("f2", "low")]}
        if meta is not None:
            data["meta"] = meta
        existing = {"f0": {"url": "old", "inline": True}}
        run_dir = _setup_run(srv, data, posted=existing)
        result = srv.post_summary(KEY, RUN_ID, "  hello  ", ["f1", "ghost", "f1"])
        assert result == {"url": "https://gh/r/1", "findings": 1}
        (cmd, kwargs), = gh.calls
        assert cmd == ["gh", "api", "-X", "POST", "repos/org/repo/pulls/7/reviews", "--input", "-"]
        assert json.loads(kwargs["input"]) == {"commit_id": sha, "event": "COMMENT", "body": "hello"}
        posted = json.loads((run_dir / "posted.json").read_text())
        assert set(posted) == {"f0", "f1"} and posted["f0"] == existing["f0"]
        assert posted["f1"]["url"] == "https://gh/r/1"
        assert {k: posted["f1"][k] for k in ("inline", "summary", "body", "summary_url")} == {
            "inline": False, "summary": True, "body": None, "summary_url": "https://gh/r/1"}

    def test_keeps_an_earlier_inline_record(self, srv, gh):
        inline = {"url": "inline-url", "inline": True, "body": "exact text", "posted_at": "t0"}
        run_dir = _setup_run(srv, {"findings": [_finding("f1", "high")]}, posted={"f1": inline})
        srv.post_summary(KEY, RUN_ID, "hello", ["f1"])
        rec = json.loads((run_dir / "posted.json").read_text())["f1"]
        assert {k: rec[k] for k in inline} == inline and rec["summary_url"] == "https://gh/r/1"

    def test_success_without_prior_posted_file(self, srv, gh):
        run_dir = _setup_run(srv, {"findings": [_finding("f1", "high")]})
        assert srv.post_summary(KEY, RUN_ID, "hello", None) == {"url": "https://gh/r/1", "findings": 0}
        assert json.loads((run_dir / "posted.json").read_text()) == {}


def _post(srv, action, payload):
    h = srv.Handler.__new__(srv.Handler)
    raw = json.dumps(payload).encode()
    h.path = f"/api/prs/{KEY}/{action}"
    h.headers = {"Host": "localhost:6161", "Content-Type": "application/json", "Content-Length": str(len(raw))}
    h.server = SimpleNamespace(server_address=("127.0.0.1", 6161))
    h.rfile = io.BytesIO(raw)
    sent = []
    h._json = lambda body, status=HTTPStatus.OK: sent.append((status, body))
    h.do_POST()
    (reply,) = sent
    return reply


class TestSummaryRoutes:
    """POST /api/prs/<key>/summary-draft and /post-summary."""

    def test_draft_route(self, srv):
        _setup_run(srv, {"findings": [_finding("f1", "high")]})
        status, body = _post(srv, "summary-draft", {"run_id": RUN_ID})
        assert status == HTTPStatus.OK and body["finding_ids"] == ["f1"]
        assert _post(srv, "summary-draft", {}) == (HTTPStatus.BAD_REQUEST, {"error": "unknown run"})

    @pytest.mark.parametrize("confirmed", [None, False, "true", 1])
    def test_post_requires_confirmation(self, srv, gh, confirmed):
        _setup_run(srv, {"findings": [_finding("f1", "high")]})
        payload = {"run_id": RUN_ID, "body": "hi", "finding_ids": ["f1"]}
        if confirmed is not None:
            payload["confirmed_by_user"] = confirmed
        assert _post(srv, "post-summary", payload) == (
            HTTPStatus.BAD_REQUEST, {"error": "posting needs explicit confirmation"})
        assert gh.calls == []

    def test_post_success(self, srv, gh):
        _setup_run(srv, {"findings": [_finding("f1", "high")]})
        payload = {"confirmed_by_user": True, "run_id": RUN_ID, "body": "hi", "finding_ids": ["f1"]}
        assert _post(srv, "post-summary", payload) == (
            HTTPStatus.OK, {"ok": True, "url": "https://gh/r/1", "findings": 1})
        assert srv.BUSY == set()

    @pytest.mark.parametrize(
        "setup, status, error",
        [
            ("empty", HTTPStatus.BAD_REQUEST, "the comment is empty"),
            ("clearing", HTTPStatus.CONFLICT, "local data is being cleared; try again in a moment"),
            ("gh_fails", HTTPStatus.BAD_GATEWAY, "rate limited"),
        ],
    )
    def test_post_errors(self, srv, gh, setup, status, error):
        _setup_run(srv, {"findings": [_finding("f1", "high")]})
        payload = {"confirmed_by_user": True, "run_id": RUN_ID, "body": "" if setup == "empty" else "hi"}
        if setup == "clearing":
            srv.CLEARING.set()
        if setup == "gh_fails":
            gh.reply = SimpleNamespace(returncode=1, stdout="", stderr="rate limited")
        assert _post(srv, "post-summary", payload) == (status, {"error": error})
        assert len(gh.calls) == (setup == "gh_fails") and srv.BUSY == set()

"""Review output: the report is rendered from the findings JSON, agents carry their model, and runs
record per-model token usage."""

from __future__ import annotations

import json
import shutil
import string

import pytest

from conftest import PROJECT

KEY = "org__repo__7"

FINDINGS = {
    "verdict": "fix-before-merge",
    "summary": "Adds retries.",
    "lint": "ruff: clean",
    "ticket": {"key": "ABC-1", "summary": "Retry uploads"},
    "must_fix": [{"severity": "high", "title": "Unbounded retry", "ref": "a.py:3", "cause": "loop",
                  "why": "hangs", "fix": "cap it", "importance": "fix before merge"}],
    "flow_context": {"summary": "api -> uploader -> s3", "source": "kb/uploader/overview.md"},
    "previously_raised": [{"status": "expected", "ref": "a.py:9", "point": "naming", "reason": "house style"}],
    "ac_coverage": [{"status": "covered", "criterion": "retries 3x", "ref": "a.py:3", "note": "ok"},
                    {"status": "missing", "criterion": "metrics", "ref": None}],
    "findings": [
        {"id": "F1", "severity": "HIGH", "title": "Unbounded retry", "source": "code", "scope": "changed lines",
         "category": "Bug", "ref": "a.py:3", "why": "no cap", "impact": "hangs", "fix": "cap it"},
        {"id": "F2", "severity": "low", "title": "Typo", "ref": "a.py:5"},
    ],
    "kb_regression": {"ran": True, "summary": "contract unchanged", "checked": ["POST /upload"]},
    "counts": {"critical": 9},
    "scope_filter": {"dropped_out_of_scope": 2, "downgraded_medium_to_low": 1},
    "top_priorities": ["cap retries"],
    "residual_risk": "s3 errors untested",
    "not_validated": ["metrics", "load"],
}


class TestRenderReport:
    def test_full_report_has_every_section(self, srv):
        report = srv.render_report(json.loads(json.dumps(FINDINGS)))
        for expected in (
            "Verdict: fix-before-merge", "Ticket: ABC-1 — Retry uploads", "Lint: ruff: clean", "Adds retries.",
            "MUST FIX", "1. [HIGH] Unbounded retry (a.py:3)", "   Fix: cap it",
            "FLOW CONTEXT", "Source: kb/uploader/overview.md",
            "- [expected] a.py:9: naming. house style",
            "- [covered] retries 3x → a.py:3 (ok)", "- [missing] metrics → n/a",
            "[HIGH] F1 Unbounded retry", "Source: code   Scope: changed lines   Category: Bug", "Why: no cap",
            "[LOW] F2 Typo", "KNOWLEDGE-BASE REGRESSION", "- checked: POST /upload",
            "Critical 0  High 1  Medium 0  Low 1", "Dropped as out-of-scope: 2   Downgraded: 1",
            "1. cap retries", "RESIDUAL RISK: s3 errors untested", "AREAS NOT FULLY VALIDATED: metrics; load",
        ):
            assert expected in report

    def test_minimal_findings(self, srv):
        report = srv.render_report({})
        assert "Verdict: n/a" in report and "Ticket: none linked" in report
        assert "None. Nothing blocks this PR." in report
        for absent in ("FLOW CONTEXT", "PREVIOUSLY RAISED", "AC / SPEC", "KNOWLEDGE-BASE", "RESIDUAL RISK"):
            assert absent not in report


class TestParseOutput:
    BLOCK = "<<<FINDINGS_JSON\n{body}\nFINDINGS_JSON>>>"

    def test_json_only_renders_the_report(self, srv):
        report, findings = srv.parse_output(self.BLOCK.format(body=json.dumps(FINDINGS)))
        assert findings["verdict"] == "fix-before-merge" and "MUST FIX" in report

    def test_fenced_json_is_accepted(self, srv):
        _, findings = srv.parse_output(self.BLOCK.format(body="```json\n" + json.dumps({"verdict": "clean"}) + "\n```"))
        assert findings == {"verdict": "clean"}

    def test_report_block_still_wins(self, srv):
        text = "<<<REPORT\nmy report\nREPORT>>>\n" + self.BLOCK.format(body=json.dumps(FINDINGS))
        assert srv.parse_output(text)[0] == "my report"

    @pytest.mark.parametrize("bad, kept", [
        ({"ticket": "ABC-1"}, "Ticket: none linked"),
        ({"flow_context": ["x"]}, "MUST FIX"),
        ({"findings": ["F1", {"id": "F2", "severity": "low", "title": "ok"}]}, "[LOW] F2 ok"),
        ({"previously_raised": "none"}, "MUST FIX"),
        ({"kb_regression": "skipped", "must_fix": [7, {"severity": "high", "title": "t"}]}, "1. [HIGH] t"),
        ({"top_priorities": "cap", "not_validated": 3, "kb_regression": {"ran": True, "checked": "x"}},
         "KNOWLEDGE-BASE REGRESSION"),
    ])
    def test_odd_fields_are_skipped_not_the_report(self, srv, bad, kept):
        report, findings = srv.parse_output(self.BLOCK.format(body=json.dumps(bad)))
        assert findings == bad and kept in report and "FINDINGS_JSON" not in report

    def test_render_failure_keeps_the_raw_text(self, srv, monkeypatch):
        monkeypatch.setattr(srv, "render_report", lambda f: 1 / 0)
        text = self.BLOCK.format(body="{}")
        assert srv.parse_output(text)[0] == text.strip()

    @pytest.mark.parametrize("text", ["just prose", BLOCK.format(body="{not json"), BLOCK.format(body="[1, 2]")])
    def test_unparseable_output_keeps_the_raw_text(self, srv, text):
        report, _ = srv.parse_output(text)
        assert report == text.strip()


class TestTokenUsage:
    def test_per_model_counts(self, srv):
        result = {"modelUsage": {"claude-opus": {"inputTokens": 5, "outputTokens": 900, "cacheReadInputTokens": 40,
                                                 "cacheCreationInputTokens": 7, "costUSD": 0.5},
                                 "claude-sonnet": {}}}
        assert srv.token_usage(result) == {
            "claude-opus": {"input": 5, "output": 900, "cache_read": 40, "cache_write": 7, "cost_usd": 0.5},
            "claude-sonnet": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": None},
        }

    def test_no_usage(self, srv):
        assert srv.token_usage({}) == {}

    def test_review_records_it(self, srv, tmp_path, monkeypatch):
        srv.STORE.prs[KEY] = {"key": KEY, "repo": "org/repo", "number": 7, "head_sha": "a" * 40, "active": True}
        monkeypatch.setattr(srv, "prepare_worktree", lambda pr: tmp_path)
        monkeypatch.setattr(srv, "match_kbs", lambda pr: [])
        monkeypatch.setattr(srv, "build_review_prompt", lambda *a: "review")
        monkeypatch.setattr(srv, "start_compaction", lambda *a: None)
        monkeypatch.setattr(srv, "run_claude", lambda *a: {
            "result": "<<<FINDINGS_JSON\n{}\nFINDINGS_JSON>>>",
            "modelUsage": {"claude-opus": {"inputTokens": 1, "outputTokens": 2}}})
        srv._review(KEY, "test")
        run_dir = next((srv.REPORTS / KEY).iterdir())
        meta = json.loads((run_dir / "findings.json").read_text())["meta"]
        assert meta["model_usage"]["claude-opus"]["output"] == 2
        assert (run_dir / "report.md").read_text().startswith("Verdict: n/a")


class TestBundledAgents:
    @pytest.fixture
    def reviewers(self, srv):
        shutil.copytree(PROJECT / "reviewers", srv.ROOT / "reviewers")
        srv.DATA.mkdir(parents=True, exist_ok=True)
        return srv.ROOT / "reviewers"

    def test_shipped_agents_models(self, srv, reviewers):
        agents = json.loads(srv.agents_file().read_text())
        assert agents["prr-consistency-reviewer"]["model"] == "sonnet"
        assert "model" not in agents["prr-code-reviewer"]

    def test_model_line_is_optional(self, srv, reviewers):
        (reviewers / "extra-reviewer.md").write_text("---\nname: x\ndescription: d\ntools: Read\n---\nbody")
        assert "model" not in json.loads(srv.agents_file().read_text())["x"]


class TestShippedPrompts:
    FIELDS = {"repo", "number", "title", "url", "author", "head_ref", "head_sha", "base_ref", "skill_path",
              "ticket_section", "kb_section", "previous_section", "decisions_section"}

    @pytest.mark.parametrize("body", ["review_bundled.md", "review_user.md"])
    def test_review_template_formats(self, body):
        parts = ("_header.md", body, "_shared.md", "_output.md")
        template = "".join((PROJECT / "prompts" / n).read_text() for n in parts)
        used = {f for _, f, _, _ in string.Formatter().parse(template) if f}
        assert used <= self.FIELDS
        text = template.format(**{f: f"<{f}>" for f in self.FIELDS})
        assert "<<<FINDINGS_JSON" in text and "<<<REPORT" not in text
        assert '"previously_raised"' in text

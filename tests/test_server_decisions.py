"""Chat decisions: applying, clearing and carrying forward your calls on findings."""

from __future__ import annotations

import json

import pytest

KEY = "org__repo__7"


def _finding(fid, severity, title="t", file="a.py", **extra):
    return {"id": fid, "severity": severity, "title": title, "file": file, **extra}


def _save_decisions(srv, decisions):
    srv.save_decisions(KEY, decisions)


class TestNormTitle:
    """Titles are compared case- and punctuation-insensitively."""

    @pytest.mark.parametrize(
        "raw, expected",
        [(None, ""), ("", ""), ("  SQL-Injection in /login!! ", "sql injection in login"), (42, "42")],
    )
    def test_normalizes(self, srv, raw, expected):
        assert srv.norm_title(raw) == expected


class TestRecount:
    """Counts and must-fix follow the effective severities."""

    def test_counts_only_when_must_fix_is_not_a_list(self, srv):
        data = {
            "findings": [_finding("F1", "high"), _finding("F2", "low"), _finding("F3", "high", dismissed=True)],
            "must_fix": None,
        }
        srv.recount(data)
        assert data["counts"] == {"critical": 0, "high": 1, "medium": 0, "low": 1}
        assert data["must_fix"] is None

    def test_rebuilds_must_fix_from_live_findings(self, srv):
        data = {
            "findings": [
                _finding("F1", "medium", why="cause", impact="impact", fix="fix", ref="a.py:3"),
                _finding("F2", "critical"),
                _finding("F3", "low"),
                _finding("F4", "high", dismissed=True),
            ],
            "must_fix": [
                {"finding_id": "F2", "severity": "high", "title": "kept", "extra": "x"},
                {"finding_id": "F3", "severity": "high"},
                {"finding_id": "F4", "severity": "high"},
            ],
        }
        srv.recount(data)
        assert data["must_fix"] == [
            {"finding_id": "F2", "severity": "critical", "title": "kept", "extra": "x",
             "importance": "blocks merge"},
            {"finding_id": "F1", "severity": "medium", "title": "t", "ref": "a.py:3", "cause": "cause",
             "why": "impact", "fix": "fix", "importance": "fix before merge"},
        ]
        assert data["counts"] == {"critical": 1, "high": 0, "medium": 1, "low": 1}


class TestApplyAndClearDecision:
    """A decision overrides the reviewer's severity and can be undone."""

    @pytest.mark.parametrize(
        "decision, severity, dismissed",
        [
            ({"id": "D1", "action": "severity", "to": "low"}, "low", False),
            ({"id": "D1", "action": "dismiss"}, "high", True),
            ({"id": "D1", "action": "keep"}, "high", False),
        ],
    )
    def test_apply_over_a_previous_decision_then_clear(self, srv, decision, severity, dismissed):
        x = _finding("F1", "high")
        srv.apply_decision(x, {"id": "D0", "action": "severity", "to": "critical"})
        srv.apply_decision(x, {"id": "D0b", "action": "dismiss"})
        srv.apply_decision(x, decision)
        assert (x["severity"], x.get("dismissed", False), x["decision_id"]) == (severity, dismissed, "D1")
        assert x["original_severity"] == "high"
        srv.clear_decision(x)
        assert x == _finding("F1", "high")

    def test_clear_without_decision_is_a_no_op(self, srv):
        x = _finding("F1", "high")
        srv.clear_decision(x)
        assert x == _finding("F1", "high")


class TestCarryForwardDecisions:
    """After a re-review, decisions re-apply to findings the reviewer raised again."""

    def test_reapplies_by_id_or_signature_and_honors_reopen(self, srv):
        _save_decisions(srv, [
            {"id": "D1", "action": "dismiss", "finding_id": "OLD1", "file": "a.py", "title": "Old title"},
            {"id": "D2", "action": "severity", "to": "low", "finding_id": "OLD2", "file": "b.py",
             "title": "Missing NULL check!"},
            {"id": "D3", "action": "keep", "finding_id": "OLD3", "file": "c.py", "title": "Race"},
            {"id": "D4", "action": "context", "reason": "Feature-flagged"},
        ])
        data = {"findings": [
            _finding("F1", "high", title="Renamed", decision_id="D1"),
            _finding("F2", "high", title="missing null check", file="b.py"),
            _finding("F3", "medium", title="Race", file="c.py", reopen="lock removed " * 40),
            _finding("F4", "medium", title="Unrelated", decision_id="D404"),
        ]}
        srv.carry_forward_decisions(KEY, data)
        f1, f2, f3, f4 = data["findings"]
        assert f1["dismissed"] is True and f1["decision_id"] == "D1"
        assert (f2["severity"], f2["original_severity"], f2["decision_id"]) == ("low", "high", "D2")
        assert f3["reopened"]["decision_id"] == "D3" and len(f3["reopened"]["reason"]) == 300
        assert "reopen" not in f3 and "decision_id" not in f3 and f3["severity"] == "medium"
        assert "decision_id" not in f4
        assert data["counts"] == {"critical": 0, "high": 0, "medium": 2, "low": 1}

    def test_without_saved_decisions_only_recounts(self, srv):
        data = {"findings": [_finding("F1", "high")]}
        srv.carry_forward_decisions(KEY, data)
        assert data == {"findings": [_finding("F1", "high")],
                        "counts": {"critical": 0, "high": 1, "medium": 0, "low": 0}}


class TestDecisionsSection:
    """The prompt section that tells the next review what you decided."""

    def test_none_yet(self, srv):
        assert srv.decisions_section(KEY) == "None yet."

    def test_lists_the_latest_decisions(self, srv, monkeypatch):
        monkeypatch.setattr(srv, "DECISIONS_IN_PROMPT", 4)
        _save_decisions(srv, [
            {"id": "D0", "action": "context", "reason": "dropped: older than the window"},
            {"id": "D1", "action": "context", "reason": "Behind a flag."},
            {"id": "D2", "action": "severity", "file": "a.py", "line": 3, "title": "T2", "from": "high",
             "to": "low", "reason": "Internal only."},
            {"id": "D3", "action": "dismiss", "file": None, "title": "T3", "from": "low", "to": None,
             "reason": "Handled upstream."},
            {"id": "D4", "action": "keep", "file": "b.py", "line": None, "title": "T4", "from": "medium",
             "to": None, "reason": "Real."},
        ])
        lines = srv.decisions_section(KEY).splitlines()
        assert lines[-4:] == [
            "- D1 (PR context): Behind a flag.",
            '- D2 `a.py:3` "T2": severity high → low. Internal only.',
            '- D3 (no file) "T3": dismissed, not a real issue. Handled upstream.',
            '- D4 `b.py:None` "T4": confirmed valid as medium. Real.',
        ]
        assert not any("D0" in line for line in lines)


class TestSplitProposals:
    """Proposals the chat agent appends are parsed, validated and de-duplicated."""

    DATA = {"findings": [
        _finding("F1", "high", title="One"),
        _finding("F2", "medium", title="Two", decision_id="D1"),
        _finding("F3", "low", title="Three", decision_id="D2"),
    ]}
    APPLIED = [{"id": "D1", "action": "severity", "to": "low"}, {"id": "D2", "action": "dismiss"}]

    @staticmethod
    def _text(body: str) -> str:
        return f"Here is my take.\n<<<PROPOSALS\n{body}\nPROPOSALS>>>\nThanks."

    @pytest.mark.parametrize(
        "text, expected_reply",
        [
            ("  just a reply  ", "just a reply"),
            ("Here is my take.\n<<<PROPOSALS\nnot json\nPROPOSALS>>>\nThanks.", "Here is my take.\n\nThanks."),
            ("Here is my take.\n<<<PROPOSALS\n{\"action\": \"dismiss\"}\nPROPOSALS>>>", "Here is my take."),
        ],
    )
    def test_no_usable_proposals(self, srv, text, expected_reply):
        assert srv.split_proposals(text, self.DATA) == (expected_reply, [])

    def test_validates_and_skips_already_applied(self, srv):
        raw = [
            {"action": "context", "reason": "  Uses   the\nnew API  "},
            {"action": "rereview"},
            {"action": "dismiss", "finding_id": "F404"},
            {"action": "explode", "finding_id": "F1"},
            {"action": "severity", "finding_id": "F1", "to": "urgent"},
            {"action": "severity", "finding_id": "F1", "to": "low", "reason": "x" * 400},
            {"action": "severity", "finding_id": "F2", "to": "low"},
            {"action": "severity", "finding_id": "F2", "to": "critical"},
            {"action": "dismiss", "finding_id": "F3"},
            {"action": "keep", "finding_id": "F3"},
        ]
        text = self._text("```json\n" + json.dumps(raw) + "\n```")
        reply, out = srv.split_proposals(text, self.DATA, self.APPLIED)
        assert reply == "Here is my take.\n\nThanks."
        assert out == [
            {"action": "context", "reason": "Uses the new API"},
            {"action": "rereview", "reason": ""},
            {"action": "severity", "finding_id": "F1", "title": "One", "from": "high", "to": "low",
             "reason": "x" * 300},
            {"action": "severity", "finding_id": "F2", "title": "Two", "from": "medium", "to": "critical",
             "reason": ""},
            {"action": "keep", "finding_id": "F3", "title": "Three", "from": "low", "to": None, "reason": ""},
        ]

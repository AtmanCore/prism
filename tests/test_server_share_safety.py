"""Safety for strangers' PRs: fork PRs aren't reviewed automatically, the deny list covers token and
push escapes, and a busy port stops the service before any worker starts."""

from __future__ import annotations

import pytest


def node(number: int, fork: bool) -> dict:
    return {"number": number, "title": "t", "url": f"https://github.com/org/repo/pull/{number}",
            "isDraft": False, "isCrossRepository": fork, "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z", "additions": 1, "deletions": 0, "changedFiles": 1,
            "headRefName": "b", "baseRefName": "main", "headRefOid": "a" * 40,
            "author": {"login": "someone"}, "repository": {"nameWithOwner": "org/repo", "name": "repo"}}


@pytest.fixture
def poll(srv, monkeypatch):
    queued = []
    srv.STORE.meta["viewer"] = "me"
    monkeypatch.setattr(srv, "my_activity_queries", lambda: [])
    monkeypatch.setattr(srv, "authored_query", lambda: "authored")
    monkeypatch.setattr(srv, "match_kbs", lambda pr: [])
    monkeypatch.setattr(srv, "my_review_state", lambda n, viewer, requested: {"my_state": "new"})
    monkeypatch.setattr(srv, "enqueue", lambda key, reason: queued.append(key))

    def run(nodes_by_label):
        monkeypatch.setattr(srv, "search_query", lambda: "requested")
        monkeypatch.setattr(srv, "run_search", lambda q: nodes_by_label.get(q, []))
        srv.poll_once()
        return queued
    return run


class TestForkPullRequests:
    @pytest.mark.parametrize("fork, allow, expected", [
        (False, False, ["org__repo__1"]),
        (True, False, []),
        (True, True, ["org__repo__1"]),
    ])
    def test_requested_review(self, srv, poll, fork, allow, expected):
        srv.CONFIG["github"]["auto_review_forks"] = allow
        assert poll({"requested": [node(1, fork)]}) == expected
        assert srv.STORE.prs["org__repo__1"]["fork"] is fork

    def test_new_commits_on_a_fork_do_not_rereview(self, srv, poll):
        srv.STORE.prs["org__repo__1"] = {"review_status": "done", "reviewed_sha": "b" * 40}
        assert poll({"requested": [node(1, True)]}) == []
        assert srv.STORE.prs["org__repo__1"]["outdated"] is True

    def test_your_own_fork_pr_follows_my_prs_setting(self, srv, poll):
        srv.CONFIG["my_prs"] = {"enabled": True, "auto_review": True}
        assert poll({"authored": [node(1, True)]}) == ["org__repo__1"]

    def test_missing_field_counts_as_same_repo(self, srv, poll):
        n = node(1, False)
        del n["isCrossRepository"]
        assert poll({"requested": [n]}) == ["org__repo__1"]

    def test_query_asks_for_it(self, srv):
        assert "isCrossRepository" in srv.SEARCH_GQL


class TestDenyList:
    @pytest.mark.parametrize("rule", [
        "Bash(gh auth:*)", "Bash(gh project:*)", "Bash(gh run cancel:*)", "Bash(gh run delete:*)",
        "Bash(gh cache:*)", "Bash(gh issue develop:*)", "Bash(gh issue pin:*)", "Bash(gh issue unpin:*)",
        "Bash(gh ssh-key:*)", "Bash(gh gpg-key:*)", "Bash(gh codespace:*)", "Bash(git -C:*)", "Bash(git -c:*)",
        "Bash(git push:*)", "Bash(gh api:*)",
    ])
    def test_covers(self, srv, rule):
        assert rule in srv.denied_tools()


class TestBusyPort:
    def test_exits_before_starting_workers(self, srv, monkeypatch):
        started = []
        monkeypatch.setattr(srv.threading.Thread, "start", lambda self: started.append(self))

        def busy(*a, **kw):
            raise OSError(48, "Address already in use")
        monkeypatch.setattr(srv, "ThreadingHTTPServer", busy)
        with pytest.raises(SystemExit) as exc:
            srv.main()
        assert exc.value.code == 1 and started == []

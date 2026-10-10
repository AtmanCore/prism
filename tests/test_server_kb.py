"""Knowledge bases: on-demand refresh, source repo sync, folder inspection and the folder picker."""

from __future__ import annotations

import json
import os
import shutil

import pytest

SKILL_SOURCES = {"generated_by": "prism-kb-builder", "repo": "org/svc-a", "ref": "main"}


def _kb(root, **extra):
    return {"id": "kb1", "name": "KB", "path": str(root), **extra}


def _make_skill_kb(root, sources=(SKILL_SOURCES,)):
    for i, src in enumerate(sources):
        d = root / "kb" / f"s{i}"
        d.mkdir(parents=True)
        (d / "sources.json").write_text(src if isinstance(src, str) else json.dumps(src))


def _make_registry_kb(root, services=("svc-a", "svc-b"), with_dirs=("svc-a", "svc-b")):
    root.mkdir(parents=True, exist_ok=True)
    (root / "registry.yaml").write_text("".join(f"- name: {s}\n  repo: org/{s}\n" for s in services))
    for s in with_dirs:
        (root / "kb" / s).mkdir(parents=True)
    return {"file": "registry.yaml"}


@pytest.fixture
def fake_run(srv, monkeypatch):
    """Records `run` calls; a command containing a name listed in ``fail`` raises like a failed process."""
    calls, fail = [], set()

    def run(cmd, cwd=None, timeout=600, check=True):
        calls.append((cmd, cwd))
        if fail & set(cmd):
            raise RuntimeError(f"{cmd[0]} failed (1): boom")
        return ""

    monkeypatch.setattr(srv, "run", run)
    run.calls, run.fail = calls, fail
    return run


class TestKbSkillSources:
    """sources.json files the prism-kb-builder skill wrote."""

    def test_keeps_only_skill_generated_dicts(self, srv, tmp_path):
        root = tmp_path / "kb"
        other = {"generated_by": "someone-else"}
        _make_skill_kb(root, [SKILL_SOURCES, "{broken", json.dumps(other), "[1, 2]"])
        assert srv.kb_skill_sources(root) == [SKILL_SOURCES]
        assert srv.kb_skill_sources(tmp_path / "missing") == []


class TestKbRefreshMethod:
    """How (and whether) a KB can be refreshed from the UI."""

    @pytest.mark.parametrize(
        "in_docker, tail", [(False, "isn't installed here."), (True, "Refresh it on your computer.")]
    )
    def test_missing_command(self, srv, tmp_path, monkeypatch, in_docker, tail):
        monkeypatch.setattr(srv, "IN_DOCKER", in_docker)
        monkeypatch.setattr(shutil, "which", lambda exe: None)
        method, why = srv.kb_refresh_method(_kb(tmp_path, refresh_command=["kbtool", "refresh"]))
        assert method is None and "(kbtool)" in why and why.endswith(tail)

    def test_command_found_on_path_or_in_the_folder(self, srv, tmp_path, monkeypatch):
        monkeypatch.setattr(shutil, "which", lambda exe: "/usr/bin/kbtool" if exe == "kbtool" else None)
        script = tmp_path / "bin" / "refresh.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        assert srv.kb_refresh_method(_kb(tmp_path, refresh_command=["kbtool"]))[0] == "command"
        assert srv.kb_refresh_method(_kb(tmp_path, refresh_command=["bin/refresh.sh"]))[0] == "command"
        assert srv.kb_refresh_method(_kb(tmp_path, refresh_command=[str(script)]))[0] == "command"
        script.chmod(0o644)
        assert srv.kb_refresh_method(_kb(tmp_path, refresh_command=["bin/refresh.sh"]))[0] is None

    @pytest.mark.parametrize(
        "writable, method, why",
        [(True, "skill", "re-runs the prism-kb-builder skill"), (False, None, "read-only here")],
    )
    def test_skill_built_kb(self, srv, tmp_path, monkeypatch, writable, method, why):
        _make_skill_kb(tmp_path)
        real_access = os.access
        monkeypatch.setattr(os, "access", lambda p, mode: writable if mode == os.W_OK else real_access(p, mode))
        got_method, got_why = srv.kb_refresh_method(_kb(tmp_path))
        assert got_method == method and why in got_why

    @pytest.mark.parametrize("make_dir, why", [(False, "doesn't exist"), (True, "No refresh command")])
    def test_not_refreshable(self, srv, tmp_path, make_dir, why):
        root = tmp_path / "kb"
        if make_dir:
            root.mkdir()
        method, got_why = srv.kb_refresh_method(_kb(root))
        assert method is None and why in got_why


class TestKbRefreshState:
    """What the UI shows about a KB's refresh: method, last refresh and the running job."""

    def test_idle_without_stamps(self, srv, tmp_path):
        assert srv.kb_refresh_state(_kb(tmp_path)) == {
            "method": None, "explain": srv.kb_refresh_method(_kb(tmp_path))[1], "last_refreshed": None,
            "state": None, "started_at": None, "finished_at": None, "error": None, "log": []}

    def test_corrupt_stamps_are_ignored(self, srv, tmp_path):
        srv.DATA.mkdir(parents=True)
        srv.KB_STAMP_FILE.write_text("{broken")
        assert srv.kb_refresh_state(_kb(tmp_path))["last_refreshed"] is None

    def test_latest_stamp_and_job_log(self, srv, tmp_path):
        srv.DATA.mkdir(parents=True)
        srv.KB_STAMP_FILE.write_text(json.dumps(
            {"kb1:svc-a": 1_700_000_000, "kb1:*": 1_800_000_000, "kb10:*": 1_900_000_000}))
        srv.KB_REFRESH["kb1"] = {"state": "running", "started_at": "s", "log": [str(i) for i in range(10)],
                                 "error": None}
        state = srv.kb_refresh_state(_kb(tmp_path))
        assert state["last_refreshed"] == "2027-01-15T08:00:00+00:00"
        assert (state["state"], state["started_at"], state["log"]) == ("running", "s", ["4", "5", "6", "7", "8", "9"])


class TestStartKbRefresh:
    """Starting a refresh from the UI."""

    @pytest.fixture
    def threads(self, monkeypatch, srv):
        started = []

        class FakeThread:
            def __init__(self, target, args, daemon):
                self.target, self.args = target, args

            def start(self):
                started.append((self.target, self.args))

        monkeypatch.setattr(srv.threading, "Thread", FakeThread)
        return started

    def test_rejections(self, srv, tmp_path, threads):
        srv.CONFIG["knowledge_bases"] = [_kb(tmp_path / "missing"), _kb(tmp_path, id="kb2")]
        _make_skill_kb(tmp_path)
        srv.kb_run_lock("kb2").acquire()
        for kb_id, message in (("nope", "unknown knowledge base"), ("kb1", "doesn't exist"),
                               ("kb2", "already refreshing")):
            with pytest.raises(ValueError, match=message):
                srv.start_kb_refresh(kb_id)
        assert threads == []

    def test_starts_a_background_job(self, srv, tmp_path, threads):
        _make_skill_kb(tmp_path)
        srv.CONFIG["knowledge_bases"] = [_kb(tmp_path)]
        srv.KB_REFRESH["kb1"] = {"state": "failed", "error": "old"}
        assert srv.start_kb_refresh("kb1") == {"ok": True, "method": "skill"}
        job = srv.KB_REFRESH["kb1"]
        assert (job["state"], job["method"], job["log"], job["error"]) == ("running", "skill", [], None)
        assert threads == [(srv._kb_refresh_job, (srv.CONFIG["knowledge_bases"][0], "skill", job))]


class TestKbRefreshCommand:
    """Running a KB's refresh_command, per service or once."""

    def test_once_for_the_whole_kb(self, srv, tmp_path, fake_run, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        notes = []
        srv._kb_refresh_command(_kb(tmp_path, refresh_command=["~/bin/kbtool", "refresh", ""]), notes.append)
        assert fake_run.calls == [([f"{tmp_path}/bin/kbtool", "refresh"], tmp_path)]
        assert list(json.loads(srv.KB_STAMP_FILE.read_text())) == ["kb1:*"]
        assert notes == ["Refreshing everything…"]

    def test_per_service_reports_failures(self, srv, tmp_path, fake_run):
        reg = _make_registry_kb(tmp_path, services=("svc-a", "svc-b", "svc-c"), with_dirs=("svc-a", "svc-b"))
        kb = _kb(tmp_path, registry=reg, service_dir="kb/{service}", refresh_command=["kbtool", "--svc", "{service}"])
        fake_run.fail.add("svc-b")
        notes = []
        with pytest.raises(RuntimeError, match="1 of 2 failed: svc-b"):
            srv._kb_refresh_command(kb, notes.append)
        assert [c for c, _ in fake_run.calls] == [["kbtool", "--svc", "svc-a"], ["kbtool", "--svc", "svc-b"]]
        assert list(json.loads(srv.KB_STAMP_FILE.read_text())) == ["kb1:svc-a"]
        assert notes[-1] == "svc-b: kbtool failed (1): boom"

    def test_per_service_without_services(self, srv, tmp_path, fake_run):
        with pytest.raises(RuntimeError, match="registry lists no services"):
            srv._kb_refresh_command(_kb(tmp_path, refresh_command=["kbtool", "{service}"]), print)
        assert fake_run.calls == []


class TestSyncKbRepo:
    """Source repos are shallow-cloned into the KB's own .cache, never your checkouts."""

    def test_fresh_clone_at_a_ref(self, srv, tmp_path, fake_run):
        stale = tmp_path / ".cache" / "repos" / "org__svc-a"
        stale.mkdir(parents=True)
        (stale / "leftover").write_text("x")
        dest = srv.sync_kb_repo(tmp_path, "org/svc-a", "release")
        assert dest == stale and not (stale / "leftover").exists()
        assert fake_run.calls == [(["git", "clone", "--quiet", "--depth", "1", "--branch", "release",
                                    "git@github.com:org/svc-a.git", str(dest)], None)]
        assert (tmp_path / ".gitignore").read_text() == ".cache/\n"

    @pytest.mark.parametrize(
        "gitignore, expected",
        [("node_modules/", "node_modules/\n.cache/\n"), ("a\n", "a\n.cache/\n"), (".cache/\nb\n", ".cache/\nb\n")],
    )
    def test_update_existing_checkout(self, srv, tmp_path, fake_run, gitignore, expected):
        srv.CONFIG["github"]["clone_url"] = "https://github.com/{repo}.git"
        dest = tmp_path / ".cache" / "repos" / "org__svc-a"
        (dest / ".git").mkdir(parents=True)
        (tmp_path / ".gitignore").write_text(gitignore)
        assert srv.sync_kb_repo(tmp_path, "org/svc-a", None) == dest
        assert fake_run.calls == [
            (["git", "fetch", "--quiet", "--depth", "1", "--", "origin", "HEAD"], dest),
            (["git", "checkout", "--quiet", "--force", "--detach", "FETCH_HEAD"], dest),
        ]
        assert (tmp_path / ".gitignore").read_text() == expected


class TestInspectKbPath:
    """Suggested settings for a folder you want to add as a KB."""

    @pytest.mark.parametrize("where", ["outside", "missing", "file"])
    def test_rejects_non_folders_and_paths_outside_home(self, srv, home, tmp_path, where):
        path = {"outside": tmp_path, "missing": home / "nope", "file": home / "notes.md"}[where]
        if where == "file":
            path.write_text("x")
        assert srv.inspect_kb_path(str(path)) == {"exists": False, "resolved_path": str(path)}

    def test_symlink_out_of_home_is_rejected(self, srv, home, tmp_path):
        (home / "escape").symlink_to(tmp_path)
        assert srv.inspect_kb_path("~/escape")["exists"] is False

    def test_suggests_settings_for_a_full_kb(self, srv, home):
        root = home / "my_team-kb"
        _make_registry_kb(root, services=("svc-a", "svc-b", "svc-gone"))
        for rel in ("README.md", "architecture.md", "kb/overview.md", "kb/svc-a/api.md", "docs/flow.md",
                    ".cache/repos/x/README.md", "node_modules/pkg/README.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("# doc")
        assert srv.inspect_kb_path("~/my_team-kb") == {
            "exists": True, "resolved_path": str(root), "markdown_files": 5, "services": ["svc-a", "svc-b"],
            "suggested_name": "My Team Kb",
            "suggested": {"registry": {"file": "registry.yaml", "name_field": "name", "repo_field": "repo"},
                          "service_dir": "kb/{service}",
                          "context_files": ["README.md", "architecture.md", "kb/*.md", "docs/**/*.md"]}}

    @pytest.mark.parametrize(
        "subdirs, service_dir, context_files",
        [
            (["services/only-one", "kb", "docs/services/a", "docs/services/b"], "docs/services/{service}",
             ["docs/**/*.md"]),
            ([], None, []),
        ],
    )
    def test_folder_without_registry(self, srv, home, subdirs, service_dir, context_files):
        root = home / "notes"
        root.mkdir()
        for rel in subdirs:
            (root / rel).mkdir(parents=True)
        result = srv.inspect_kb_path(str(root))
        assert (result["services"], result["markdown_files"]) == ([], 0)
        assert result["suggested"] == {"registry": None, "service_dir": service_dir, "context_files": context_files}


class TestBrowseDir:
    """The folder picker lists folders inside your home only."""

    @pytest.fixture
    def tree(self, home):
        (home / "projects" / "kb-one" / "kb").mkdir(parents=True)
        (home / "projects" / "kb-one" / "registry.yaml").write_text("")
        (home / "projects" / "kb-one" / "README.MD").write_text("")
        (home / "projects" / "kb-one" / "notes.markdown").write_text("")
        (home / "projects" / "kb-one" / "conf.yml").write_text("")
        (home / "projects" / "kb-one" / "data.json").write_text("")
        (home / "projects" / "kb-one" / "image.png").write_text("")
        (home / ".hidden").mkdir()
        (home / "Zeta").mkdir()
        return home

    def test_home_listing(self, srv, tree):
        result = srv.browse_dir("")
        assert result == {"path": "~", "parent": None, "dirs": [{"name": "projects", "path": "~/projects"},
                                                                {"name": "Zeta", "path": "~/Zeta"}],
                          "files": [], "other_files": 0, "truncated": False, "looks_like_kb": False,
                          "docker": False}
        assert [d["name"] for d in srv.browse_dir("~", show_hidden=True)["dirs"]] == [".hidden", "projects", "Zeta"]

    def test_kb_folder_listing(self, srv, tree):
        result = srv.browse_dir("~/projects/kb-one")
        assert (result["path"], result["parent"], result["looks_like_kb"]) == ("~/projects/kb-one", "~/projects", True)
        assert result["dirs"] == [{"name": "kb", "path": "~/projects/kb-one/kb"}]
        assert result["files"] == [{"name": "conf.yml", "type": "yaml"}, {"name": "data.json", "type": "json"},
                                   {"name": "notes.markdown", "type": "markdown"},
                                   {"name": "README.MD", "type": "markdown"},
                                   {"name": "registry.yaml", "type": "yaml"}]
        assert result["other_files"] == 1

    @pytest.mark.parametrize("path", ["~/../..", "~/projects/../../..", "/", "/etc", "~/missing",
                                      "~/projects/kb-one/registry.yaml", "~/out", "relative/path"])
    def test_falls_back_to_home(self, srv, tree, tmp_path, path):
        (tree / "out").symlink_to(tmp_path)
        assert srv.browse_dir(path)["path"] == "~"

    def test_truncates_long_listings(self, srv, home, monkeypatch):
        monkeypatch.setattr(srv, "BROWSE_LIMIT", 2)
        for i in range(3):
            (home / f"d{i}").mkdir()
        result = srv.browse_dir("~")
        assert (len(result["dirs"]), result["truncated"]) == (2, True)

    def test_unreadable_entries_are_skipped(self, srv, home, monkeypatch):
        (home / "locked").mkdir()
        (home / "flaky").mkdir()
        (home / "fine").mkdir()
        path_cls = type(home)
        real_iterdir, real_is_dir = path_cls.iterdir, path_cls.is_dir

        def iterdir(self):
            if self.name == "locked":
                raise PermissionError("denied")
            return real_iterdir(self)

        def is_dir(self, *a, **k):
            if self.name == "flaky":
                raise OSError("stale mount")
            return real_is_dir(self, *a, **k)

        monkeypatch.setattr(path_cls, "iterdir", iterdir)
        monkeypatch.setattr(path_cls, "is_dir", is_dir)
        locked = srv.browse_dir("~/locked")
        assert (locked["path"], locked["dirs"], locked["files"]) == ("~/locked", [], [])
        assert [d["name"] for d in srv.browse_dir("~")["dirs"]] == ["fine", "locked"]

    @pytest.mark.parametrize("path, expected", [("", "~/kb"), ("~", "~/kb"), ("~/other", "~/other")])
    def test_docker_opens_the_mounted_kbs(self, srv, home, monkeypatch, path, expected):
        monkeypatch.setattr(srv, "IN_DOCKER", True)
        monkeypatch.setattr(srv, "DOCKER_KB_ROOT", home / "kb")
        (home / "kb").mkdir()
        (home / "other").mkdir()
        result = srv.browse_dir(path)
        assert (result["path"], result["docker"]) == (expected, True)

"""tools/setup.py: token parsing, .env handling, prompts and the Docker knowledge-base mounts."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

TOKEN = "sk-ant-oat01-" + "A" * 40


class TestParseSetupToken:
    """Picks the token out of `claude setup-token`'s terminal output."""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("", ""),
            ("Login failed\r\n", ""),
            (f"\x1b[32m✓ Your token:\x1b[0m {TOKEN}\r\n\r\nStore this token securely.\n", TOKEN),
            ("Token:\n  sk-ant-oat01-abc_\r\n  DEF-123\x1b[0m\n  ghi\nStore this token securely",
             "sk-ant-oat01-abc_DEF-123ghi"),
            (f"{TOKEN}", TOKEN),
            ("sk-ant-oat1-short sk-ant-api03-nope", ""),
        ],
    )
    def test_extracts_wrapped_token(self, setup_mod, text, expected):
        assert setup_mod.parse_setup_token(text) == expected


class TestEnvFile:
    """.env is edited in place: comments and order survive, permissions are owner-only."""

    def test_missing_file(self, setup_mod):
        assert setup_mod.read_env() == {}
        setup_mod.write_env({"GITHUB_TOKEN": "ghp_x"})
        assert setup_mod.ENV_FILE.read_text() == "GITHUB_TOKEN=ghp_x\n"
        assert stat.S_IMODE(setup_mod.ENV_FILE.stat().st_mode) == 0o600

    def test_updates_in_place(self, setup_mod):
        setup_mod.ENV_FILE.write_text(
            "# GitHub\n# GITHUB_TOKEN=commented-out\nGITHUB_TOKEN='old'\n\nJIRA_URL = \"https://x\"\nnot a pair\n")
        setup_mod.ENV_FILE.chmod(0o644)
        setup_mod.write_env({"GITHUB_TOKEN": "ghp_new", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN})
        assert setup_mod.ENV_FILE.read_text() == (
            "# GitHub\n# GITHUB_TOKEN=commented-out\nGITHUB_TOKEN=ghp_new\n\nJIRA_URL = \"https://x\"\nnot a pair\n"
            f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
        assert stat.S_IMODE(setup_mod.ENV_FILE.stat().st_mode) == 0o600
        assert setup_mod.read_env() == {"GITHUB_TOKEN": "ghp_new", "JIRA_URL": "https://x",
                                        "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
        assert [p.name for p in setup_mod.ROOT.iterdir()] == [".env"]


class TestGithubTokenPage:
    """The pre-filled fine-grained token page."""

    @pytest.mark.parametrize(
        "owner, write, name, pulls",
        [("acme", True, "PRism", "write"), ("", False, "PRism (read-only)", "read")],
    )
    def test_prefills_permissions(self, setup_mod, owner, write, name, pulls):
        url = urlparse(setup_mod.github_token_page(owner, write))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert f"{url.scheme}://{url.netloc}{url.path}" == "https://github.com/settings/personal-access-tokens/new"
        assert (params["name"], params["pull_requests"], params.get("target_name")) == (name, pulls, owner or None)
        assert (params["contents"], params["metadata"], params["issues"], params["expires_in"]) == (
            "read", "read", "read", "90")


class TestYamlStr:
    """Paths are written so compose reads them literally."""

    @pytest.mark.parametrize(
        "value, expected",
        [("/home/me/kb", '"/home/me/kb"'), ('/a $HOME/"q": b', '"/a $$HOME/\\"q\\": b"'), (Path("/x$y"), '"/x$$y"')],
    )
    def test_quotes_and_escapes_dollars(self, setup_mod, value, expected):
        assert setup_mod.yaml_str(value) == expected


class TestPrompts:
    """Numbered pickers; Enter keeps the default and Ctrl-D counts as Enter."""

    @pytest.mark.parametrize(
        "replies, chosen",
        [((), 1), (("",), 1), (("9", "zero", "0", "3"), 2), (("1",), 0)],
    )
    def test_pick_one(self, setup_mod, answers, replies, chosen):
        answers(*replies)
        assert setup_mod.pick_one("Pick", ["a", "b", "c"], default=1) == chosen

    @pytest.mark.parametrize(
        "selected, replies, chosen",
        [
            ({1}, (), [1]),
            (set(), ("1 3", ""), [0, 2]),
            ({0}, ("1,2 9 x",), [1]),
            (set(), ("a", "2", ""), [0, 2]),
            ({0, 1, 2}, ("N", ""), []),
        ],
    )
    def test_pick_many(self, setup_mod, answers, selected, replies, chosen):
        answers(*replies)
        assert setup_mod.pick_many("Pick", ["a", "b", "c"], selected) == chosen


class TestChooseOwner:
    """The token owner: your orgs from an existing gh login, or one you type."""

    @pytest.fixture
    def gh_orgs(self, monkeypatch):
        state = {"installed": True, "result": SimpleNamespace(returncode=0, stdout="acme\ngamma\n")}
        monkeypatch.setattr(shutil, "which", lambda exe: "/usr/bin/gh" if exe == "gh" and state["installed"] else None)
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: state["result"])
        return state

    @pytest.mark.parametrize("installed, returncode", [(False, 0), (True, 1)])
    def test_types_one_without_known_orgs(self, setup_mod, answers, gh_orgs, installed, returncode):
        gh_orgs.update(installed=installed, result=SimpleNamespace(returncode=returncode, stdout="acme\n"))
        answers("my-org")
        assert setup_mod.choose_owner([]) == "my-org"

    @pytest.mark.parametrize(
        "replies, owner",
        [((), "beta"), (("2",), "acme"), (("3",), "gamma"), (("4",), ""), (("5", "delta"), "delta")],
    )
    def test_picks_from_configured_and_gh_orgs(self, setup_mod, answers, gh_orgs, replies, owner):
        answers(*replies)
        assert setup_mod.choose_owner(["beta", "acme"]) == owner


class TestFindKbFolders:
    """Folders under HOME with a registry file plus kb/ or services/."""

    def _kb(self, home, rel, marker="registry.yaml", sub="kb"):
        d = home / rel
        (d / sub).mkdir(parents=True)
        (d / marker).write_text("")
        return d

    def test_finds_kbs_and_skips_noise(self, setup_mod, home):
        top = self._kb(home, "team-kb")
        nested = self._kb(home, "work/platform", marker="services.json", sub="services")
        self._kb(home, "team-kb/kb/inner")
        self._kb(home, ".config/hidden")
        self._kb(home, "Library/app")
        self._kb(home, "work/node_modules/pkg")
        self._kb(home, "a/b/c/too-deep")
        (home / "work" / "no-marker" / "kb").mkdir(parents=True)
        (home / "work" / "no-dir").mkdir()
        (home / "work" / "no-dir" / "registry.yml").write_text("")
        assert setup_mod.find_kb_folders() == [top, nested]

    def test_stops_when_out_of_time(self, setup_mod, home):
        self._kb(home, "team-kb")
        assert setup_mod.find_kb_folders(budget_sec=-1) == []


class TestKbMounts:
    """setup_kbs writes docker-compose.override.yml; current_kb_mounts reads it back."""

    @pytest.fixture
    def picks(self, setup_mod, monkeypatch, answers):
        answers()
        state = {"found": [], "pick": None, "seen": []}

        def pick_many(title, options, selected):
            state["seen"].append((options, selected))
            return state["pick"](options) if callable(state["pick"]) else state["pick"]

        monkeypatch.setattr(setup_mod, "find_kb_folders", lambda: list(state["found"]))
        monkeypatch.setattr(setup_mod, "pick_many", pick_many)
        return state

    def test_native_mode_writes_nothing(self, setup_mod, picks):
        setup_mod.setup_kbs("native")
        assert not setup_mod.OVERRIDE_FILE.exists() and picks["seen"] == []

    def test_round_trip_and_unmount(self, setup_mod, picks, home, tmp_path):
        odd = home / 'my "kb": $HOME'
        dup_a, dup_b = home / "a" / "kb", tmp_path / "elsewhere" / "kb"
        for d in (odd, dup_a, dup_b):
            d.mkdir(parents=True)
        picks.update(found=[odd, dup_a, dup_b], pick=[0, 1, 2])
        setup_mod.setup_kbs("docker")
        assert picks["seen"][0] == ([f"~/{odd.name}", "~/a/kb", str(dup_b)], set())
        mounts = setup_mod.current_kb_mounts()
        assert mounts == {str(odd): "/home/prism/kb/my--kb----HOME", str(dup_a): "/home/prism/kb/kb",
                          str(dup_b): "/home/prism/kb/kb-2"}
        assert "read_only: true" in setup_mod.OVERRIDE_FILE.read_text()

        picks.update(found=[dup_a], pick=[1])
        setup_mod.setup_kbs("docker")
        assert picks["seen"][1][1] == {0, 1, 2}
        assert setup_mod.current_kb_mounts() == {str(dup_a): "/home/prism/kb/kb"}

        picks.update(found=[], pick=[])
        setup_mod.setup_kbs("docker")
        assert not setup_mod.OVERRIDE_FILE.exists()

    def test_add_folders_by_path(self, setup_mod, picks, answers, home, monkeypatch):
        monkeypatch.setattr(setup_mod.sys, "platform", "linux")
        target = home / "typed-kb"
        target.mkdir()
        answers("y", str(home / "nope"), "Yes", "~/typed-kb", "")
        setup_mod.setup_kbs("docker")
        assert picks["seen"] == []
        assert setup_mod.current_kb_mounts() == {str(target): "/home/prism/kb/typed-kb"}

    def test_nothing_found_or_chosen(self, setup_mod, picks):
        setup_mod.setup_kbs("docker")
        assert not setup_mod.OVERRIDE_FILE.exists()

    def test_reader_ignores_unrelated_lines(self, setup_mod):
        assert setup_mod.current_kb_mounts() == {}
        setup_mod.OVERRIDE_FILE.write_text("\n".join([
            "services:",
            '        target: "/orphan"',
            "        source: /unquoted",
            '        source: "broken',
            '        source: "/a$$b"',
            "        read_only: true",
            '        target: "/home/prism/kb/a"',
        ]) + "\n")
        assert setup_mod.current_kb_mounts() == {"/a$b": "/home/prism/kb/a"}


class TestTokenCandidates:
    def test_one_word_line_after_the_token_is_offered_as_a_shorter_candidate(self, setup_mod):
        text = "Your token:\nsk-ant-oat01-ABCDEF\nGHI4\nSuccess\n\nStore this token securely"
        assert setup_mod.setup_token_candidates(text) == [
            "sk-ant-oat01-ABCDEFGHI4Success", "sk-ant-oat01-ABCDEFGHI4", "sk-ant-oat01-ABCDEF"]

    def test_no_token(self, setup_mod):
        assert setup_mod.setup_token_candidates("Login cancelled") == []


class TestKbFoldersAtMaxDepth:
    def test_found_exactly_at_the_depth_limit(self, setup_mod, home):
        d = home / "a" / "b" / "c"
        (d / "kb").mkdir(parents=True)
        (d / "registry.yaml").write_text("")
        assert setup_mod.find_kb_folders() == [d]


class TestConfigSafety:
    def test_strict_read_refuses_broken_json(self, setup_mod):
        setup_mod.CONFIG_FILE.write_text('{"github": {"orgs": [],}}')
        assert setup_mod.read_config() == {}
        with pytest.raises(setup_mod.ConfigUnreadable, match="isn't valid JSON"):
            setup_mod.read_config(strict=True)

    def test_save_config_is_atomic_and_complete(self, setup_mod):
        setup_mod.save_config({"a": 1, "github": {"orgs": ["x"]}})
        assert json.loads(setup_mod.CONFIG_FILE.read_text()) == {"a": 1, "github": {"orgs": ["x"]}}
        assert [f.name for f in setup_mod.ROOT.iterdir() if f.name.startswith(".config.")] == []


class TestForeignOverride:
    def test_leaves_a_user_written_override_alone(self, setup_mod, monkeypatch):
        setup_mod.OVERRIDE_FILE.write_text("services:\n  prism:\n    environment:\n      X: 1\n")
        monkeypatch.setattr(setup_mod, "find_kb_folders", lambda *a, **k: pytest.fail("searched anyway"))
        setup_mod.setup_kbs("docker")
        assert "X: 1" in setup_mod.OVERRIDE_FILE.read_text()


class TestSetupClaudeCandidates:
    def test_saves_the_first_candidate_claude_accepts(self, setup_mod, monkeypatch):
        tried = []
        monkeypatch.setattr(setup_mod, "read_env", lambda: {})
        monkeypatch.setattr(setup_mod.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(setup_mod, "yes", lambda *a, **k: True)
        monkeypatch.setattr(setup_mod, "capture_setup_token", lambda: ["tok-long", "tok-right", "tok-short"])
        monkeypatch.setattr(setup_mod, "claude_token_works",
                            lambda t: (tried.append(t) or t == "tok-right", ""))
        saved = {}
        monkeypatch.setattr(setup_mod, "write_env", saved.update)
        setup_mod.setup_claude("docker")
        assert (tried, saved) == (["tok-long", "tok-right"], {"CLAUDE_CODE_OAUTH_TOKEN": "tok-right"})


class TestSaveConfigInPlace:
    def test_keeps_the_same_file_and_permissions(self, setup_mod):
        setup_mod.CONFIG_FILE.write_text("{}")
        setup_mod.CONFIG_FILE.chmod(0o644)
        before = setup_mod.CONFIG_FILE.stat()
        setup_mod.save_config({"a": 1})
        after = setup_mod.CONFIG_FILE.stat()
        assert (after.st_ino, stat.S_IMODE(after.st_mode)) == (before.st_ino, 0o644)

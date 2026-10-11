#!/usr/bin/env python3
"""Local PRism service.

Every ``poll_interval_sec`` it pulls the open, non-draft PRs that request your review
(``github.search_query`` + ``github.orgs``), checks each one out into a throwaway git
worktree, reviews it headlessly with ``claude -p`` (bundled reviewer agents, or your own
``/pr-review`` skill), adds a regression pass against any knowledge base attached to the
repo, and serves the reports at http://localhost:<port>.

Background reviews never write to GitHub or your ticket tracker: they're denied every write
command. The only write path is the UI's post dialog, which needs your typed confirmation.

Stdlib only; run with any Python 3.9+.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPORTS = DATA / "reports"
WORKSPACE = ROOT / "workspace"
REPOS_DIR = WORKSPACE / "repos"
PRS_DIR = WORKSPACE / "prs"
STATIC = ROOT / "static"
STATE_FILE = DATA / "state.json"
KB_STAMP_FILE = DATA / "kb_refresh.json"
DRAIN_FILE = ROOT / "service.drain"  # present while ./restart.sh waits for running reviews to finish

log = logging.getLogger("pr-review")

READ_ONLY_DENY = [
    "Write",
    "Edit",
    "NotebookEdit",
    "AskUserQuestion",
    "Bash(gh pr comment:*)",
    "Bash(gh pr review:*)",
    "Bash(gh pr merge:*)",
    "Bash(gh pr edit:*)",
    "Bash(gh pr close:*)",
    "Bash(gh pr ready:*)",
    "Bash(gh pr reopen:*)",
    "Bash(gh pr create:*)",
    "Bash(gh pr lock:*)",
    "Bash(gh pr unlock:*)",
    "Bash(gh pr update-branch:*)",
    "Bash(gh pr revert:*)",
    "Bash(gh issue comment:*)",
    "Bash(gh issue create:*)",
    "Bash(gh issue edit:*)",
    "Bash(gh issue close:*)",
    "Bash(gh issue reopen:*)",
    "Bash(gh issue delete:*)",
    "Bash(gh issue lock:*)",
    "Bash(gh issue transfer:*)",
    "Bash(gh api:*)",
    "Bash(gh repo:*)",
    "Bash(gh release:*)",
    "Bash(gh workflow:*)",
    "Bash(gh alias:*)",
    "Bash(gh extension:*)",
    "Bash(gh label:*)",
    "Bash(gh gist:*)",
    "Bash(gh secret:*)",
    "Bash(gh variable:*)",
    "Bash(bash -c:*)",
    "Bash(sh -c:*)",
    "Bash(zsh -c:*)",
    "WebFetch",
    "Bash(gh run rerun:*)",
    "Bash(git push:*)",
    "Bash(kubectl:*)",
    "Bash(docker:*)",
    "Bash(gcloud:*)",
    "Bash(aws:*)",
    "Bash(gh auth token:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
]

def ticket_read_tools() -> list[str]:
    """Read-only ticket/doc tools: yours from config, plus any the Docker image registered
    (PRISM_EXTRA_READ_TOOLS, set by docker/entrypoint.sh when Jira/Confluence are configured)."""
    extra = [t.strip() for t in os.environ.get("PRISM_EXTRA_READ_TOOLS", "").split(",") if t.strip()]
    return list(dict.fromkeys(list(CONFIG["tickets"].get("read_tools") or []) + extra))


def ticket_url_template() -> str | None:
    t = CONFIG["tickets"].get("url_template")
    jira = (os.environ.get("JIRA_URL") or "").rstrip("/")
    return t or (f"{jira}/browse/{{key}}" if jira else None)


def allowed_tools() -> list[str]:
    return READ_ALLOW + ticket_read_tools()


def denied_tools() -> list[str]:
    return READ_ONLY_DENY + list(CONFIG["tickets"].get("deny_tools") or []) + unlisted_user_mcp_tools()


_WARNED_SERVER_RULES: set[str] = set()


def unlisted_user_mcp_tools() -> list[str]:
    """MCP tools your own ~/.claude/settings.json pre-approves that PRism isn't configured to use.

    Reviews load user settings (--setting-sources user), so an `allow` entry there would otherwise let
    a review call, say, Jira even when PRism has no ticket tools configured. PRism's `read_tools` is
    the only list that counts; everything else MCP that you pre-approved is denied here.
    """
    try:
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        allow = (settings.get("permissions") or {}).get("allow") or []
    except (OSError, ValueError, AttributeError):
        return []
    keep = set(ticket_read_tools())
    out = []
    for rule in allow:
        if not isinstance(rule, str) or not rule.startswith("mcp__") or rule in keep:
            continue
        if any(k.startswith(rule.rstrip("*").rstrip("_") + "__") for k in keep):
            # A whole-server rule covering tools you did list: denying it would block those too, so the
            # server's other tools (writes included) stay allowed unless tickets.deny_tools names them.
            if rule not in _WARNED_SERVER_RULES:
                _WARNED_SERVER_RULES.add(rule)
                log.warning("~/.claude/settings.json allows the whole MCP server %r, so reviews can call all "
                            "of its tools. List its write tools in tickets.deny_tools (see README).", rule)
            continue
        out.append(rule)
    return out


READ_ALLOW = [
    "Bash",
    "Read",
    "Grep",
    "Glob",
    "Agent",
    "Task",
]

SEARCH_GQL = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 100) {
    issueCount
    nodes {
      ... on PullRequest {
        number title url isDraft createdAt updatedAt
        additions deletions changedFiles
        headRefName baseRefName headRefOid reviewDecision
        author { login avatarUrl }
        repository { nameWithOwner name }
        labels(first: 10) { nodes { name color } }
        files(first: 100) { nodes { path } }
        reviews(last: 40) { nodes { author { login __typename } submittedAt state commit { oid } } }
        comments(last: 40) { nodes { author { login __typename } createdAt } }
        commits(last: 1) { nodes { commit { committedDate } } }
        reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login } } } }
        timelineItems(last: 30, itemTypes: [REVIEW_REQUESTED_EVENT, READY_FOR_REVIEW_EVENT]) {
          nodes {
            __typename
            ... on ReviewRequestedEvent { createdAt requestedReviewer { ... on User { login } ... on Team { slug } } }
            ... on ReadyForReviewEvent { createdAt }
          }
        }
      }
    }
  }
}
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULTS: dict = {
    "port": 6161,
    "github": {"search_query": "is:pr is:open draft:false review-requested:@me", "orgs": [],
               "include_my_reviews": True, "my_reviews_window_days": 30,
               "clone_url": "git@github.com:{repo}.git"},
    "poll_interval_sec": 300,
    "auto_review": True,
    "auto_rereview_on_new_commits": True,
    "max_concurrent_reviews": 3,
    "review_timeout_sec": 2700,
    "response_check_timeout_sec": 900,
    "claude": {"bin": "claude", "model": None, "draft_model": "sonnet", "chat_model": None},
    "chat_timeout_sec": 600,
    # After a review, /compact a copy of its session (cheap while its cache is warm) so "Ask the agent"
    # starts from the reviewer's own memory of the code. false = chats start from the report alone.
    "compact_after_review": True,
    "compact_timeout_sec": 180,
    "reviewer": {
        "mode": "bundled",  # "bundled" = reviewers/*.md shipped here; "user" = your ~/.claude skill + agents
        "skill_path": "~/.claude/commands/pr-review.md",
        "lessons_files": {"code": "data/lessons/code-reviewer.lessons.md",
                          "consistency": "data/lessons/consistency-reviewer.lessons.md"},
    },
    "tickets": {"key_pattern": "[A-Z][A-Z0-9]+-[0-9]+", "url_template": None, "read_tools": [], "deny_tools": []},
    "knowledge_bases": [],
    # "My PRs": open PRs you wrote (drafts too). Listed, but only reviewed on request unless auto_review.
    "my_prs": {"enabled": True, "auto_review": False},
}
CONFIG_FILE = ROOT / "config.json"


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config() -> dict:
    """Defaults ← config.json (yours, gitignored) ← config.local.json (optional overrides)."""
    cfg = copy.deepcopy(DEFAULTS)  # never hand out DEFAULTS itself: CONFIG.clear() would empty it
    for f in (CONFIG_FILE, ROOT / "config.local.json"):
        if f.is_file():
            cfg = deep_merge(cfg, json.loads(f.read_text()))
    return cfg


def save_config_key(key: str, value) -> None:
    """Write one top-level key back to config.json, keeping everything else as the user wrote it.
    Raises ValueError if config.local.json overrides the key, so the UI doesn't claim a change that
    didn't take effect."""
    raw = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    raw[key] = value
    text = json.dumps(raw, indent=2) + "\n"
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(text)
    try:
        tmp.replace(CONFIG_FILE)
    except OSError:
        # Docker bind-mounts config.json as a single file, and Linux won't rename over a mount point
        # (EBUSY). Write it in place instead.
        tmp.unlink(missing_ok=True)
        CONFIG_FILE.write_text(text)
    new = load_config()  # build first, then update in place: CONFIG is never empty, even for a moment
    CONFIG.update(new)
    for stale in set(CONFIG) - set(new):
        del CONFIG[stale]
    took = CONFIG.get(key)
    overridden = (any((took or {}).get(k) != v for k, v in value.items()) if isinstance(value, dict)
                  else took != value)
    if overridden:
        raise ValueError(f"saved, but config.local.json overrides {key}; change it there")


def xpath(p: str | Path) -> Path:
    """Expand ~ and make project-relative paths absolute."""
    path = Path(os.path.expanduser(str(p)))
    return path if path.is_absolute() else ROOT / path


def load_dotenv() -> dict:
    """Read KEY=VALUE pairs from .env (no shell expansion)."""
    env_file = ROOT / ".env"
    values: dict[str, str] = {}
    if env_file.exists():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip("'\"")
    return values


CONFIG = load_config()
DOTENV = load_dotenv()


def child_env() -> dict:
    """Environment for gh / git / claude subprocesses; injects the token if provided."""
    env = dict(os.environ)
    # Every non-empty key in .env reaches gh / git / claude.
    env.update({k: v for k, v in DOTENV.items() if v})
    if not DOTENV.get("ANTHROPIC_API_KEY"):
        env.pop("ANTHROPIC_API_KEY", None)  # no key set: claude uses your logged-in subscription, never API billing
    token = DOTENV.get("GITHUB_TOKEN") or DOTENV.get("GH_TOKEN")
    if token:
        env["GH_TOKEN"] = token
        env["GITHUB_TOKEN"] = token
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 600, check: bool = True) -> str:
    log.debug("run %s (cwd=%s)", " ".join(cmd), cwd)
    proc = subprocess.run(
        cmd, cwd=cwd, env=child_env(), capture_output=True, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} failed ({proc.returncode}): {proc.stderr.strip()[:800]}")
    return proc.stdout


# --------------------------------------------------------------------------- state


class Store:
    """JSON-file backed state, guarded by a single lock."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.prs: dict[str, dict] = {}
        self.meta: dict = {"last_poll": None, "last_poll_error": None, "next_poll": None, "viewer": None}
        if STATE_FILE.exists():
            try:
                raw = json.loads(STATE_FILE.read_text())
                self.prs = raw.get("prs", {})
                self.meta.update(raw.get("meta", {}))
            except json.JSONDecodeError:
                log.warning("state.json is corrupt; starting fresh")
        for pr in self.prs.values():
            # A restart kills any in-flight run; requeue it rather than show it stuck.
            if pr.get("review_status") in ("queued", "preparing", "kb-refresh", "reviewing"):
                pr["review_status"] = "interrupted"

    def save(self) -> None:
        with self.lock:
            DATA.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"prs": self.prs, "meta": self.meta}, indent=1))
            tmp.replace(STATE_FILE)

    def update_pr(self, key: str, **fields) -> None:
        """Update a known PR. Unknown keys are ignored (e.g. a late write after "Clear local data"),
        so a half-empty record can't appear; poll_once is the only place that adds PRs."""
        with self.lock:
            if key not in self.prs:
                return
            self.prs[key].update(fields)
            self.save()


STORE = Store()


def pr_key(repo_full: str, number: int) -> str:
    return f"{repo_full.replace('/', '__')}__{number}"


# ------------------------------------------------------------------ knowledge bases


def kb_registry(kb: dict) -> dict[str, str]:
    """Optional service registry inside the KB: {repo (owner/name or name): service}.

    JSON (a list, or {"services": [...]}) is parsed properly. Anything else is read line by line
    as a simple YAML list of mappings (`- <name_field>: x` followed by `<repo_field>: owner/repo`),
    which keeps the service stdlib-only.
    """
    reg = kb.get("registry") or {}
    if not reg.get("file"):
        return {}
    path = xpath(kb["path"]) / reg["file"]
    if not path.is_file():
        return {}
    name_f, repo_f = reg.get("name_field", "name"), reg.get("repo_field", "repo")
    out: dict[str, str] = {}
    if path.suffix == ".json":
        data = json.loads(path.read_text())
        items = data.get("services", []) if isinstance(data, dict) else data
        for item in items:
            if isinstance(item, dict) and item.get(name_f) and item.get(repo_f):
                out[str(item[repo_f])] = str(item[name_f])
        return out
    current = None
    for line in path.read_text().splitlines():
        m_name = re.match(rf"^\s*-\s+{re.escape(name_f)}:\s*['\"]?([^'\"\s#]+)", line)
        if m_name:
            current = m_name.group(1)
            continue
        m_repo = re.match(rf"^\s+{re.escape(repo_f)}:\s*['\"]?([^'\"\s#]+)", line)
        if m_repo and current:
            out[m_repo.group(1)] = current
            current = None
    return out


def kb_service_dir(kb: dict, service: str) -> Path | None:
    tpl = kb.get("service_dir")
    return xpath(kb["path"]) / tpl.format(service=service) if tpl else None


def kb_services_available(kb: dict) -> dict[str, str]:
    """Registry entries whose service folder actually exists in the KB."""
    out = {}
    for repo, svc in kb_registry(kb).items():
        d = kb_service_dir(kb, svc)
        if d is None or d.is_dir():
            out[repo] = svc
    return out


def match_kbs(pr: dict) -> list[dict]:
    """Which knowledge bases (and which of their services) a PR touches.

    1. Explicit attachments: `attach: [{"repo": "org/repo-or-glob", "service": "optional"}]`.
    2. Registry: the KB's registry lists this repo.
    3. Monorepos: for repos matching `monorepos`, a registered service named in the changed paths/title.
    """
    repo = pr["repo"].lower()
    bare = repo.split("/")[-1]
    haystack = " ".join([pr.get("title", "")] + pr.get("files", [])).lower()
    matches = []
    for kb in CONFIG.get("knowledge_bases", []):
        if not xpath(kb["path"]).is_dir():
            continue
        hits, attached = set(), False
        for a in kb.get("attach") or []:
            if fnmatch.fnmatch(repo, str(a.get("repo", "")).lower()):
                attached = True
                if a.get("service"):
                    hits.add(a["service"])
        is_mono = any(fnmatch.fnmatch(repo, m.lower()) for m in kb.get("monorepos") or [])
        for reg_repo, svc in kb_services_available(kb).items():
            reg = reg_repo.lower()
            if reg in (repo, bare):
                hits.add(svc)
            elif is_mono and (reg.split("/")[-1] in haystack or svc.lower() in haystack):
                hits.add(svc)
        if hits or attached:
            matches.append({"id": kb["id"], "name": kb.get("name", kb["id"]), "path": str(xpath(kb["path"])),
                            "services": sorted(hits)})
    return matches


KB_LOCK = threading.Lock()


def kb_by_id(kb_id: str) -> dict | None:
    return next((k for k in CONFIG.get("knowledge_bases", []) if k["id"] == kb_id), None)


KB_RUN_LOCKS: dict[str, threading.Lock] = {}


def kb_run_lock(kb_id: str) -> threading.Lock:
    """One refresh at a time per KB, whether it's the pre-review refresh or "Refresh now"."""
    with KB_LOCK:
        return KB_RUN_LOCKS.setdefault(kb_id, threading.Lock())


def kb_stamps() -> dict:
    with KB_LOCK:
        try:
            return json.loads(KB_STAMP_FILE.read_text()) if KB_STAMP_FILE.exists() else {}
        except ValueError:
            return {}


def refresh_kbs(matches: list[dict], progress) -> list[str]:
    """Run each KB's optional `refresh_command` per service, at most once per `refresh_max_age_hours`."""
    notes = []
    for m in matches:
        kb = kb_by_id(m["id"]) or {}
        cmd = kb.get("refresh_command")
        max_age = float(kb.get("refresh_max_age_hours", 24)) * 3600
        if not cmd or max_age <= 0:
            continue
        lock = kb_run_lock(m["id"])
        if not lock.acquire(blocking=False):
            notes.append(f"{m['name']}: a refresh is already running; used existing content")
            continue
        try:
            stamps = kb_stamps()
            for svc in m["services"] or [None]:
                if time.time() - stamps.get(f"{m['id']}:{svc or '*'}", 0) < max_age:
                    continue
                argv = [os.path.expanduser(str(a)).replace("{service}", svc or "") for a in cmd]
                argv = [a for a in argv if a != ""]
                progress(f"Refreshing {m['name']}{f' for {svc}' if svc else ''}")
                try:
                    run(argv, cwd=xpath(kb["path"]), timeout=900)
                    _stamp_kb(m["id"], svc)
                    notes.append(f"{m['name']}: refreshed {svc or 'all'}")
                except Exception as exc:  # best-effort; a stale KB still helps
                    notes.append(f"{m['name']}: refresh of {svc or 'all'} failed ({str(exc)[:200]}), "
                                 "used existing content")
        finally:
            lock.release()
    return notes


# ------------------------------------------------- refreshing a knowledge base on demand
#
# "Refresh now" in the UI. Two ways, picked per KB:
#   command: the KB's own refresh_command (e.g. a CLI that regenerates it), per service or once;
#   skill:   KBs built by the bundled prism-kb-builder skill are refreshed by `claude -p` running that
#            skill, allowed to write ONLY inside the KB folder and to read the source repos.

KB_REFRESH: dict[str, dict] = {}  # kb id -> {"state", "method", "started_at", "finished_at", "log", "error"}
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")  # never starts with "-", so git can't take it as an option
KB_SKILL_FILE = ROOT / "skills" / "prism-kb-builder" / "SKILL.md"


def kb_skill_sources(root: Path) -> list[dict]:
    """sources.json files written by the prism-kb-builder skill (empty if the KB wasn't built by it)."""
    out = []
    for f in sorted(root.glob("kb/*/sources.json"))[:200]:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("generated_by") == "prism-kb-builder":
            out.append(data)
    return out


def kb_refresh_method(kb: dict) -> tuple[str | None, str]:
    root = xpath(kb.get("path", ""))
    if not root.is_dir():
        return None, "The folder doesn't exist."
    if kb.get("refresh_command"):
        exe = os.path.expanduser(str(kb["refresh_command"][0]))
        found = shutil.which(exe) or (("/" in exe)
                                      and os.access(root / exe if not os.path.isabs(exe) else exe, os.X_OK))
        if not found:
            return None, (f"Its refresh command ({exe}) isn't installed here"
                          + (" (inside the Docker container). Refresh it on your computer." if IN_DOCKER else "."))
        return "command", "Runs its refresh command."
    if kb_skill_sources(root):
        if not os.access(root, os.W_OK):
            return None, ("The folder is read-only here (in Docker, knowledge bases are mounted read-only). "
                          "Refresh it on your computer: see its README.")
        return "skill", ("Updates the source repos in this folder's .cache/repos (never your own checkouts), then "
                         "re-runs the prism-kb-builder skill, which can write only inside this folder.")
    return None, "No refresh command, and it wasn't built by the prism-kb-builder skill. Add a command under Edit."


def kb_refresh_state(kb: dict) -> dict:
    method, why = kb_refresh_method(kb)
    stamps = kb_stamps()
    last = max((v for k, v in stamps.items() if k.split(":", 1)[0] == kb["id"]), default=None)
    job = KB_REFRESH.get(kb["id"]) or {}
    return {"method": method, "explain": why,
            "last_refreshed": datetime.fromtimestamp(last, timezone.utc).isoformat(
                timespec="seconds") if last else None,
            **{k: job.get(k) for k in ("state", "started_at", "finished_at", "error")},
            "log": (job.get("log") or [])[-6:]}


def start_kb_refresh(kb_id: str) -> dict:
    kb = kb_by_id(kb_id)
    if not kb:
        raise ValueError("unknown knowledge base")
    method, why = kb_refresh_method(kb)
    if not method:
        raise ValueError(why)
    lock = kb_run_lock(kb_id)
    if not lock.acquire(blocking=False):
        raise ValueError("already refreshing (manually or before a review); try again when it finishes")
    job = {"state": "running", "method": method, "started_at": now_iso(), "log": [], "error": None, "_lock": lock}
    KB_REFRESH[kb_id] = job
    try:
        threading.Thread(target=_kb_refresh_job, args=(kb, method, job), daemon=True).start()
    except Exception:
        KB_REFRESH.pop(kb_id, None)
        lock.release()
        raise
    return {"ok": True, "method": method}


def _kb_refresh_job(kb: dict, method: str, job: dict) -> None:

    def note(msg: str) -> None:
        job["log"].append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")

    try:
        with busy(f"kb:{kb['id']}"):
            if method == "command":
                _kb_refresh_command(kb, note)
            else:
                _kb_refresh_skill(kb, note)
        job["state"] = "done"
        note("Done.")
    except Exception as exc:
        log.exception("refresh of KB %s failed", kb["id"])
        job["state"], job["error"] = "failed", str(exc)[:600]
        note(f"Failed: {str(exc)[:200]}")
    finally:
        job["finished_at"] = now_iso()
        lock = job.pop("_lock", None)
        if lock:
            lock.release()


def _stamp_kb(kb_id: str, svc: str | None) -> None:
    with KB_LOCK:
        try:
            stamps = json.loads(KB_STAMP_FILE.read_text()) if KB_STAMP_FILE.exists() else {}
        except ValueError:
            stamps = {}
        stamps[f"{kb_id}:{svc or '*'}"] = time.time()
        DATA.mkdir(parents=True, exist_ok=True)
        KB_STAMP_FILE.write_text(json.dumps(stamps, indent=1))


def _kb_refresh_command(kb: dict, note) -> None:
    cmd = kb["refresh_command"]
    per_service = any("{service}" in str(a) for a in cmd)
    services = sorted(set(kb_services_available(kb).values())) if per_service else [None]
    if per_service and not services:
        raise RuntimeError("the command needs {service}, but the registry lists no services")
    failed = []
    for svc in services:
        argv = [a for a in (os.path.expanduser(str(a)).replace("{service}", svc or "") for a in cmd) if a != ""]
        note(f"Refreshing {svc or 'everything'}…")
        try:
            run(argv, cwd=xpath(kb["path"]), timeout=900)
            _stamp_kb(kb["id"], svc)
        except Exception as exc:
            failed.append(svc or "all")
            note(f"{svc or 'all'}: {str(exc)[:160]}")
    if failed:
        raise RuntimeError(f"{len(failed)} of {len(services)} failed: {', '.join(failed[:8])}")


def sync_kb_repo(root: Path, repo: str, ref: str | None) -> Path:
    """Shallow clone (or update) a source repo into the KB's own .cache/repos/, at `ref` or the default
    branch. Your working checkouts are never used: they can be on any branch, with uncommitted changes."""
    if not REPO_NAME_RE.match(repo or "") or ".." in repo:
        raise ValueError(f"not a GitHub repo name: {repo!r}")
    if ref is not None and (not REF_RE.match(ref) or ".." in ref):
        raise ValueError(f"not a branch or tag name: {ref!r}")
    dest = root / ".cache" / "repos" / repo.replace("/", "__")
    url = CONFIG["github"]["clone_url"].format(repo=repo)
    if not (dest / ".git").exists():
        shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--quiet", "--depth", "1", *(["--branch", ref] if ref else []), url, str(dest)],
            timeout=900)
    else:
        run(["git", "fetch", "--quiet", "--depth", "1", "--", "origin", ref or "HEAD"], cwd=dest, timeout=900)
        run(["git", "checkout", "--quiet", "--force", "--detach", "FETCH_HEAD"], cwd=dest)
    gi = root / ".gitignore"  # keep the checkouts out of the KB's own git history
    existing = gi.read_text() if gi.exists() else ""
    if ".cache/" not in existing.split():
        gi.write_text(existing + ("\n" if existing and not existing.endswith("\n") else "") + ".cache/\n")
    return dest


def _kb_refresh_skill(kb: dict, note) -> None:
    root = xpath(kb["path"]).resolve()
    sources = kb_skill_sources(root)
    repos = {}
    for src in sources:
        if src.get("repo"):
            ref = src.get("ref")
            repos[src["repo"]] = None if not ref or ref == "HEAD" or re.fullmatch(r"[0-9a-f]{7,40}", ref) else ref
    if not repos:
        raise RuntimeError("no source repos listed in kb/*/sources.json")
    checkouts, failed = {}, []
    for repo, ref in sorted(repos.items()):
        note(f"Updating {repo}{f' ({ref})' if ref else ''} in .cache/repos…")
        try:
            checkouts[repo] = sync_kb_repo(root, repo, ref)
        except Exception as exc:
            failed.append(repo)
            note(f"{repo}: {str(exc)[:160]}")
    if not checkouts:
        raise RuntimeError("couldn't fetch any source repo (check GitHub access for these repos)")
    note(f"Running the prism-kb-builder skill on {len(checkouts)} repo(s)…")
    heads = {repo: run(["git", "rev-parse", "HEAD"], cwd=path).strip() for repo, path in checkouts.items()}
    listing = "\n".join(f"- {repo}: {path} (commit {heads[repo]})" for repo, path in checkouts.items())
    prompt = (
        "Refresh the PRism knowledge base in the current folder, following the skill instructions below "
        "exactly, in **refreshing** mode. This is an unattended run: don't ask any questions and skip the "
        "neighbor check (it's for new KBs). The output folder is the current folder. PRism already "
        "checked out each source repo, up to date, here, with its commit (read the files with your "
        "Read/Grep/Glob tools; you have no shell, git or network access):\n"
        f"{listing}\n"
        + (f"These couldn't be fetched; leave their services as they are: {', '.join(failed)}.\n" if failed else "")
        + "Only regenerate services whose commit changed. Finish with the short summary the skill describes.\n\n"
        "--- prism-kb-builder skill ---\n" + KB_SKILL_FILE.read_text()
    )
    # Read/Grep/Glob, plus writes inside this KB only. No Bash at all: even "read" git commands can write
    # files (git log --output=...), and PRism already supplied the checkouts and commits.
    cmd = [CONFIG["claude"]["bin"], "-p", "--setting-sources", "user", "--output-format", "json",
           "--permission-mode", "default",
           "--allowedTools", "Read", "Grep", "Glob", f"Write(/{root}/kb/**)", f"Edit(/{root}/kb/**)",
           f"Write(/{root}/registry.yaml)", f"Edit(/{root}/registry.yaml)", f"Write(/{root}/README.md)",
           f"Edit(/{root}/README.md)",
           "--strict-mcp-config",  # no MCP servers at all: your pre-approved Jira/Notion/... tools can't run here
           "--disallowedTools", "Bash", "WebFetch", "WebSearch", "NotebookEdit", "AskUserQuestion",
           *(CONFIG["tickets"].get("deny_tools") or []), *unlisted_user_mcp_tools(), *ticket_read_tools()]
    if CONFIG["claude"].get("model"):
        cmd += ["--model", CONFIG["claude"]["model"]]
    proc = subprocess.run(cmd, cwd=root, input=prompt, capture_output=True, text=True, env=child_env(),
                          timeout=int(CONFIG.get("review_timeout_sec", 2700)))
    try:
        out = json.loads(proc.stdout or "{}")
    except ValueError:
        out = {}
    if proc.returncode != 0 or out.get("is_error"):
        raise RuntimeError((out.get("result") or proc.stderr or "claude failed").strip()[:400])
    for line in (out.get("result") or "").strip().splitlines()[-4:]:
        if line.strip():
            note(line.strip()[:200])
    _stamp_kb(kb["id"], None)
    if failed:
        raise RuntimeError(f"refreshed, but {len(failed)} repo(s) couldn't be fetched: {', '.join(failed[:6])}")


def kb_context_files(kb: dict, limit: int = 40) -> list[str]:
    root = xpath(kb["path"])
    files: list[str] = []
    for pattern in kb.get("context_files") or []:
        for f in sorted(root.glob(pattern)):
            if f.is_file() and str(f) not in files:
                files.append(str(f))
    return files[:limit]


def kb_prompt_section(matches: list[dict]) -> str:
    if not matches:
        return ("No knowledge base is attached to this repo. SKIP the KB reads, set `kb_regression.ran` "
                "to false, and derive `flow_context` from the code (routes, clients, producers/consumers).")
    lines = [
        "This repo is attached to local knowledge bases (markdown docs about the system). **Only read "
        "them.** Don't run the services, any KB CLI, or anything that resolves secrets. Use them to "
        "find regressions the diff alone can't show:",
        "",
    ]
    for m in matches:
        kb = kb_by_id(m["id"]) or {}
        lines.append(f"- **{m['name']}** at `{m['path']}`"
                     + (f", services: {', '.join(m['services'])}" if m["services"] else ""))
        for svc in m["services"]:
            d = kb_service_dir(kb, svc)
            if d:
                lines.append(f"  - service docs: `{d}/` (read every file)")
        ctx = kb_context_files(kb)
        if ctx:
            lines.append("  - system context: " + ", ".join(f"`{c}`" for c in ctx))
        if not m["services"]:
            lines.append("  - no service folder: `Grep` the KB for the repo name and identifiers from the diff")
    lines += [
        "",
        "Check at least:",
        "1. **API contracts.** For any changed route, request/response field, status code or enum, find who "
        "calls or consumes it according to the KB. Would they break?",
        "2. **Data contracts.** Changed tables, columns, models, topics, bucket paths or file formats. Who reads them?",
        "3. **Config/env.** Env vars renamed, added without a default, or removed while a documented deployment "
        "still sets them. Changed secret references.",
        "4. **Behavioral drift.** Documented behavior that the diff silently changes.",
        "5. **Flow.** Work out the flow around the changed code (callers → this component → downstream) "
        "for `flow_context`.",
        "",
        "Every real regression becomes a finding with `source: kb-regression` and the KB file as evidence in "
        "`ref`. If nothing regresses, say so in `kb_regression.summary` and list what you checked.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------ polling


def search_query() -> str:
    gh = CONFIG["github"]
    return " ".join([gh["search_query"]] + [f"org:{o}" for o in gh.get("orgs") or []])


def my_activity_queries() -> list[str]:
    """Open PRs you already reviewed or commented on (GitHub drops them from review-requested)."""
    gh = CONFIG["github"]
    if not gh.get("include_my_reviews", True):
        return []
    since = datetime.fromtimestamp(time.time() - 86400 * int(gh.get("my_reviews_window_days", 30))).strftime("%Y-%m-%d")
    orgs = " ".join(f"org:{o}" for o in gh.get("orgs") or [])
    base = f"is:pr is:open draft:false -author:@me updated:>={since} {orgs}".strip()
    return [f"{base} reviewed-by:@me", f"{base} commenter:@me"]


def authored_query() -> str | None:
    """Your own open PRs, drafts included (a self-review before you ask others helps most)."""
    if not (CONFIG.get("my_prs") or {}).get("enabled", True):
        return None
    orgs = " ".join(f"org:{o}" for o in CONFIG["github"].get("orgs") or [])
    return f"is:pr is:open author:@me {orgs}".strip()


def my_prs_auto() -> bool:
    return bool((CONFIG.get("my_prs") or {}).get("auto_review", False))


def run_search(q: str) -> list[dict]:
    out = run(["gh", "api", "graphql", "-f", f"query={SEARCH_GQL}", "-f", f"q={q}"], timeout=120)
    return [n for n in json.loads(out)["data"]["search"]["nodes"] if n]


def is_bot(actor: dict | None) -> bool:
    return not actor or actor.get("__typename") == "Bot" or str(actor.get("login", "")).endswith("[bot]")


def author_login(node: dict) -> str | None:
    return (node.get("author") or {}).get("login")


def my_review_state(n: dict, viewer: str, requested: bool) -> dict:
    """Your status on a PR, from YOUR latest review/comment and what the author did after it.

    to-review: you haven't reviewed it yet.
    reviewed:  you reviewed it, and the author hasn't handed it back. Commits/replies since are
               listed in `my_activity` as a hint, but don't change the status.
    updated:   "Ready again": after your review the author re-requested your review (you or a team
               you're in) or marked the PR ready for review.
    """
    reviews = [r for r in (n.get("reviews") or {}).get("nodes") or [] if r and r.get("submittedAt")]
    comments = [c for c in (n.get("comments") or {}).get("nodes") or [] if c]
    events = [e for e in (n.get("timelineItems") or {}).get("nodes") or [] if e and e.get("createdAt")]
    login = author_login
    my_reviews = sorted((r for r in reviews if login(r) == viewer), key=lambda r: r["submittedAt"])
    my_comments = sorted(c["createdAt"] for c in comments if login(c) == viewer)
    last_review = my_reviews[-1] if my_reviews else None
    stamps = ([last_review["submittedAt"]] if last_review else []) + my_comments[-1:]
    if not stamps or (requested and not last_review):
        return {"my_state": "to-review", "my_last_at": max(stamps) if stamps else None,
                "my_changes": [], "my_activity": [], "my_verdict": None}
    last_at = max(stamps)

    # Explicit hand-back signals from the author (these alone make it "Ready again").
    signals = []
    for e in events:
        if e["createdAt"] <= last_at:
            continue
        if e["__typename"] == "ReadyForReviewEvent":
            signals.append("marked ready for review")
        elif e["__typename"] == "ReviewRequestedEvent":
            rr = e.get("requestedReviewer") or {}
            if rr.get("login") == viewer or (rr.get("slug") and requested):
                signals.append("re-requested your review")
    if requested and last_review and "re-requested your review" not in signals:
        signals.append("re-requested your review")  # e.g. via a team, without a visible event

    # Informational only: what happened since, shown as a hint on the row.
    activity = []
    reviewed_oid = (last_review.get("commit") or {}).get("oid") if last_review else None
    if last_review and last_review["submittedAt"] == last_at and reviewed_oid:
        if reviewed_oid != n.get("headRefOid"):
            activity.append("new commits")
    else:
        commits = (n.get("commits") or {}).get("nodes") or []
        if commits and commits[-1]["commit"]["committedDate"] > last_at:
            activity.append("new commits")
    others = [r for r in reviews if login(r) != viewer and not is_bot(r.get("author")) and r["submittedAt"] > last_at]
    others += [c for c in comments if login(c) != viewer and not is_bot(c.get("author")) and c["createdAt"] > last_at]
    if others:
        activity.append(f"{len(others)} new repl{'y' if len(others) == 1 else 'ies'}")
    signals = list(dict.fromkeys(signals))
    return {"my_state": "updated" if signals else "reviewed", "my_last_at": last_at,
            "my_changes": signals, "my_activity": activity,
            "my_verdict": last_review.get("state") if last_review else "COMMENTED"}


def closed_state(pr: dict) -> str:
    owner, name = pr["repo"].split("/")
    try:
        out = run(["gh", "api", "graphql", "-f",
                   "query=query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r)"
                   "{pullRequest(number:$n){state isDraft}}}",
                   "-f", f"o={owner}", "-f", f"r={name}", "-F", f"n={pr['number']}"], timeout=60)
        node = json.loads(out)["data"]["repository"]["pullRequest"]
        return "DRAFT" if node["state"] == "OPEN" and node.get("isDraft") else node["state"]
    except Exception:
        return "UNKNOWN"


def poll_once() -> None:
    log.info("polling GitHub")
    try:
        viewer = STORE.meta.get("viewer") or run(["gh", "api", "user", "--jq", ".login"], timeout=60).strip()
        found: dict[str, dict] = {}
        sources: dict[str, set] = {}
        queries = [("requested", search_query())] + [("mine", q) for q in my_activity_queries()]
        if authored_query():
            queries.append(("authored", authored_query()))
        for label, q in queries:
            for n in run_search(q):
                if "repository" not in n or (n.get("isDraft") and label != "authored"):
                    continue
                key = pr_key(n["repository"]["nameWithOwner"], n["number"])
                found[key] = n
                sources.setdefault(key, set()).add(label)
    except Exception as exc:
        log.exception("poll failed")
        with STORE.lock:
            STORE.meta["last_poll_error"] = str(exc)[:500]
            STORE.meta["last_poll"] = now_iso()
            STORE.save()
        return

    seen = set(found)
    to_queue = []
    with STORE.lock:
        STORE.meta["viewer"] = viewer
        for key, n in found.items():
            repo = n["repository"]["nameWithOwner"]
            prev = STORE.prs.get(key, {})
            requested = "requested" in sources[key]
            authored = "authored" in sources[key]
            fields = {
                "key": key,
                "repo": repo,
                "number": n["number"],
                "title": n["title"],
                "url": n["url"],
                "author": (n.get("author") or {}).get("login", "ghost"),
                "author_avatar": (n.get("author") or {}).get("avatarUrl"),
                "created_at": n["createdAt"],
                "updated_at": n["updatedAt"],
                "additions": n["additions"],
                "deletions": n["deletions"],
                "changed_files": n["changedFiles"],
                "head_ref": n["headRefName"],
                "base_ref": n["baseRefName"],
                "head_sha": n["headRefOid"],
                "review_decision": n.get("reviewDecision"),
                "labels": [lab["name"] for lab in (n.get("labels") or {}).get("nodes", [])],
                "files": [f["path"] for f in (n.get("files") or {}).get("nodes", [])],
                "active": True,
                "requested": requested,
                "authored": authored,
                "draft": bool(n.get("isDraft")),
                "closed_state": None,
                **my_review_state(n, viewer, requested),
            }
            if authored:
                fields["my_state"] = "authored"
            fields["review_status"] = prev.get("review_status", "new")
            merged = {**prev, **fields}
            try:
                merged["kb_matches"] = [{"id": m["id"], "name": m["name"], "services": m["services"]}
                                        for m in match_kbs(merged)]
            except Exception:  # one broken KB registry mustn't stop the whole sync
                log.exception("matching knowledge bases for %s failed", key)
                merged["kb_matches"] = prev.get("kb_matches") or []
            STORE.prs[key] = merged

            status = merged.get("review_status")
            reviewed_sha = merged.get("reviewed_sha")
            # Auto-review what needs your attention; PRs you already reviewed only re-run on changes.
            # Your own PRs (My PRs) only when you ask, unless "Auto-review my PRs" is on.
            auto = my_prs_auto() if authored else CONFIG.get("auto_review", True)
            wants_review = authored or requested or merged["my_state"] == "updated"
            if status in ("queued", "preparing", "kb-refresh", "reviewing"):
                continue
            if status == "interrupted" or (status == "new" and wants_review and auto):
                to_queue.append((key, ("your PR" if authored else "new PR") if status == "new"
                                 else "resumed after restart"))
            elif (reviewed_sha and reviewed_sha != merged["head_sha"]
                  and status in ("done", "failed")):
                merged["outdated"] = True
                if CONFIG.get("auto_rereview_on_new_commits", True) and auto:
                    to_queue.append((key, "new commits pushed"))

        newly_gone = [pr for key, pr in STORE.prs.items() if key not in seen and pr.get("active")]
        for pr in newly_gone:
            pr["active"] = False
            pr["inactive_since"] = now_iso()
        STORE.meta["last_poll"] = now_iso()
        STORE.meta["last_poll_error"] = None
        STORE.meta["next_poll"] = datetime.fromtimestamp(
            (_last_poll_started or time.time()) + CONFIG["poll_interval_sec"],
            timezone.utc).isoformat(timespec="seconds")
        STORE.save()

    for pr in newly_gone:  # outside the lock: one small GraphQL read each
        STORE.update_pr(pr["key"], closed_state=closed_state(pr))
    for key, reason in to_queue:
        enqueue(key, reason)
    for key in seen:
        if not STORE.prs[key].get("authored"):  # reply checks are for threads you started on others' PRs
            enqueue_response_check(key)
    counts = Counter(STORE.prs[k].get("my_state") for k in seen)
    log.info("poll done: %d open PRs (%s), %d queued", len(seen), dict(counts), len(to_queue))


POLL_NOW = threading.Event()
SYNC_MINUTES = (1, 2, 5, 10, 15, 30, 60)
_last_poll_started = 0.0


def poller_loop() -> None:
    global _last_poll_started
    while True:
        _last_poll_started = time.time()
        try:
            poll_once()
        except Exception as exc:  # never let one bad poll stop syncing for good
            log.exception("poll crashed")
            with STORE.lock:
                STORE.meta["last_poll_error"] = f"internal error: {str(exc)[:300]}"
                STORE.meta["last_poll"] = now_iso()
                try:
                    STORE.save()
                except OSError:
                    pass
        # Short waits, so a new interval chosen in the UI applies without waiting out the old one.
        while not POLL_NOW.wait(timeout=5):
            if time.time() >= _last_poll_started + CONFIG["poll_interval_sec"]:
                break
        POLL_NOW.clear()


def data_summary() -> dict:
    """What "Clear local data" would delete, for the confirmation dialog."""
    def size(p: Path) -> int:
        total = 0
        for f in p.rglob("*"):
            try:
                if f.is_file() and not f.is_symlink():
                    total += f.stat().st_size
            except OSError:
                pass
        return total
    reports = sum(1 for d in REPORTS.glob("*/*") if d.is_dir() and RUN_ID_RE.match(d.name)) if REPORTS.exists() else 0
    return {"data_dir": str(DATA), "workspace_dir": str(WORKSPACE), "reports": reports,
            "prs": len(STORE.prs), "bytes": size(DATA) + size(WORKSPACE), "running": len(ACTIVE_REVIEWS),
            "busy": len(BUSY)}


def clear_local_data(confirm: str) -> dict:
    """Delete everything PRism stored locally: reports, chats, decisions, lessons, state, clones.
    Your config.json, .env and knowledge-base folders are not touched."""
    if (confirm or "").strip().lower() != "clear":
        raise ValueError('type "clear" to confirm')
    CLEARING.set()  # no new job starts; running reviews are cancelled below, other jobs are waited for
    try:
        with QUEUED_LOCK:
            for q in (JOBS, RESP_JOBS):
                while True:
                    try:
                        q.get_nowait()
                        q.task_done()
                    except queue.Empty:
                        break
            QUEUED.clear()
            RESP_QUEUED.clear()
        cancelled = len(ACTIVE_REVIEWS)
        deadline = time.time() + 90
        while (ACTIVE_REVIEWS or BUSY) and time.time() < deadline:
            for proc in all_procs():
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(0.5)
        if ACTIVE_REVIEWS or BUSY:
            raise RuntimeError("Some background work is still finishing (a review, reply check, chat or "
                               "knowledge-base refresh). Try again in a minute.")
        _wipe_local_data()
    finally:
        CLEARING.clear()
    log.info("user cleared all local data (%s, %s); %d running review(s) cancelled", DATA, WORKSPACE, cancelled)
    return {"ok": True, "cancelled": cancelled}


def _wipe_local_data() -> None:
    # Clones first, outside the state lock (it can take a while and /api/state must stay responsive).
    if WORKSPACE.exists():
        for child in WORKSPACE.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    with STORE.lock:
        for base in (DATA,):
            if base.exists():
                for child in base.iterdir():
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
        for d in (DATA, REPORTS, REPOS_DIR, PRS_DIR):
            d.mkdir(parents=True, exist_ok=True)
        STORE.prs.clear()
        STORE.meta.update({"last_poll": None, "last_poll_error": None, "next_poll": None})
        STORE.save()


def set_sync_interval(minutes) -> dict:
    if type(minutes) is not int or minutes not in SYNC_MINUTES:
        raise ValueError(f"choose one of {', '.join(map(str, SYNC_MINUTES))} minutes")
    save_config_key("poll_interval_sec", minutes * 60)
    if _last_poll_started:
        with STORE.lock:
            STORE.meta["next_poll"] = datetime.fromtimestamp(
                _last_poll_started + minutes * 60, timezone.utc).isoformat(timespec="seconds")
    log.info("sync interval set to %d min", minutes)
    return {"ok": True, "poll_interval_sec": CONFIG["poll_interval_sec"], "next_poll": STORE.meta.get("next_poll")}



# --------------------------------------------------------------- review prompt


def ticket_section() -> str:
    t = CONFIG["tickets"]
    tools = ticket_read_tools()
    pattern = t.get("key_pattern") or "[A-Z][A-Z0-9]+-[0-9]+"
    if tools:
        return (f"Start from the PR. Only follow what **the PR itself** links to: a ticket key matching "
                f"`{pattern}` in its title, branch or description, and doc links in its description. "
                f"Fetch them with these **read-only** tools: {', '.join(f'`{x}`' for x in tools)}. "
                "Don't search the tracker for other work. If the PR links nothing, the PR title and "
                "description are the spec. If a tool fails, note it under AREAS NOT FULLY VALIDATED.")
    return ("No ticket or doc tools are configured. The PR title and description are the spec. GitHub "
            "issues the PR links to can be read with `gh issue view <n> --repo <owner/repo>` (read-only). "
            "If the PR mentions a ticket you can't read, note it under AREAS NOT FULLY VALIDATED.")


def agents_file() -> Path:
    """Bundled mode: turn reviewers/*.md (frontmatter + body) into the JSON `claude --agents` takes."""
    agents = {}
    lessons = lesson_files()
    for f in sorted((ROOT / "reviewers").glob("*.md")):
        text = f.read_text()
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            continue
        meta = dict(re.findall(r"^(\w+):\s*(.+)$", m.group(1), re.M))
        kind = "code" if "code" in f.stem else "consistency"
        body = m.group(2).replace("{lessons_file}", str(lessons.get(kind, "")))
        agents[meta["name"]] = {"description": meta.get("description", ""), "prompt": body,
                                "tools": [x.strip() for x in meta.get("tools", "").split(",") if x.strip()]}
        if meta.get("model"):  # e.g. the consistency pass runs on a cheaper model than the code pass
            agents[meta["name"]]["model"] = meta["model"]
    out = DATA / "agents.json"
    out.write_text(json.dumps(agents, indent=1))
    return out


def build_review_prompt(pr: dict, key: str, matches: list[dict]) -> str:
    mode = CONFIG["reviewer"]["mode"]
    def part(n):
        return (ROOT / "prompts" / n).read_text()
    body = part("review_bundled.md" if mode == "bundled" else "review_user.md")
    template = part("_header.md") + body + part("_shared.md") + part("_output.md")
    return template.format(
        repo=pr["repo"], number=pr["number"], title=pr["title"], url=pr["url"],
        author=pr["author"], head_ref=pr["head_ref"], head_sha=pr["head_sha"], base_ref=pr["base_ref"],
        skill_path=os.path.expanduser(CONFIG["reviewer"].get("skill_path", "")),
        ticket_section=ticket_section(), kb_section=kb_prompt_section(matches),
        previous_section=previous_responses_section(key), decisions_section=decisions_section(key),
    )


# ------------------------------------------------------------------------ reviews

JOBS: "queue.Queue[tuple[str, str]]" = queue.Queue()
QUEUED: set[str] = set()
QUEUED_LOCK = threading.Lock()
REPO_LOCKS: dict[str, threading.Lock] = {}
RUNNING_PROCS: dict[str, subprocess.Popen] = {}  # running reviews, by PR key
COMPACT_PROCS: dict[tuple[str, str], subprocess.Popen] = {}  # running compactions, by (key, run_id)
COMPACT_STOPPED: set[tuple[str, str]] = set()  # compactions told to stop: they must not start a process
COMPACT_LOCK = threading.Lock()  # guards COMPACT_PROCS and COMPACT_STOPPED together


def all_procs() -> list[subprocess.Popen]:
    """Every claude process PRism started, so clear and shutdown can stop them all."""
    with COMPACT_LOCK:
        compactions = list(COMPACT_PROCS.values())
    return list(RUNNING_PROCS.values()) + compactions  # snapshots: other threads mutate them


def enqueue(key: str, reason: str) -> bool:
    with QUEUED_LOCK:
        if key in QUEUED:
            return False
        QUEUED.add(key)
    STORE.update_pr(key, review_status="queued", queued_reason=reason, queued_at=now_iso(),
                    progress=f"Queued ({reason})")
    JOBS.put((key, reason))
    return True


def repo_lock(repo: str) -> threading.Lock:
    with QUEUED_LOCK:
        return REPO_LOCKS.setdefault(repo, threading.Lock())


def prepare_worktree(pr: dict) -> Path:
    """Bare partial clone per repo + a detached-branch worktree per PR at its head."""
    repo = pr["repo"]
    bare = REPOS_DIR / f"{repo.replace('/', '__')}.git"
    wt = PRS_DIR / pr["key"]
    with repo_lock(repo):
        if not bare.exists():
            REPOS_DIR.mkdir(parents=True, exist_ok=True)
            run(["git", "clone", "--bare", "--filter=blob:none",
                 CONFIG["github"]["clone_url"].format(repo=repo), str(bare)], timeout=1800)
            run(["git", "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"], cwd=bare)
        run(["git", "fetch", "--no-tags", "origin",
             f"+refs/heads/{pr['base_ref']}:refs/remotes/origin/{pr['base_ref']}",
             f"+refs/pull/{pr['number']}/head:refs/remotes/origin/pr/{pr['number']}"], cwd=bare, timeout=1800)
        if wt.exists():
            stop_compactions(pr["key"])  # they run in this worktree, outside the PR lock
            run(["git", "worktree", "remove", "--force", str(wt)], cwd=bare, check=False)
            shutil.rmtree(wt, ignore_errors=True)
        run(["git", "worktree", "prune"], cwd=bare, check=False)
        PRS_DIR.mkdir(parents=True, exist_ok=True)
        # Local branch keeps the head name (reviewers read ticket keys off it) but is unique per PR,
        # so two PRs from same-named fork branches (e.g. `patch-1`) can't collide.
        run(["git", "worktree", "add", "-f", "-B", f"pr-{pr['number']}/{pr['head_ref']}", str(wt),
             f"refs/remotes/origin/pr/{pr['number']}"], cwd=bare, timeout=1800)
        run(["git", "branch", f"--set-upstream-to=origin/{pr['base_ref']}"], cwd=wt, check=False)
    return wt


def describe_tool(block: dict) -> str:
    name = block.get("name", "tool")
    inp = block.get("input") or {}
    if name == "Bash":
        return f"$ {str(inp.get('command', ''))[:140]}"
    if name in ("Agent", "Task"):
        return f"Agent → {inp.get('subagent_type', '?')}: {inp.get('description', '')}"
    if name in ("Read", "Grep", "Glob"):
        return f"{name} {inp.get('file_path') or inp.get('pattern') or ''}"[:160]
    if name.startswith("mcp__"):
        return f"{name.split('__')[-1]} {json.dumps(inp)[:100]}"
    return name


def run_claude(pr: dict, wt: Path, prompt: str, run_dir: Path, progress) -> dict:
    kb_dirs = [str(xpath(kb["path"])) for kb in CONFIG.get("knowledge_bases", []) if xpath(kb["path"]).is_dir()]
    cmd = [
        CONFIG["claude"]["bin"], "-p", "--setting-sources", "user",
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", "default",
        "--allowedTools", *allowed_tools(),
        "--disallowedTools", *denied_tools(),
        "--add-dir", str(DATA), *kb_dirs,
    ]
    if CONFIG["reviewer"]["mode"] == "user":  # your own skill + agents live there
        cmd += ["--add-dir", str(Path.home() / ".claude")]
    if CONFIG["reviewer"]["mode"] == "bundled":
        cmd += ["--agents", str(agents_file())]
    if CONFIG["claude"].get("model"):
        cmd += ["--model", CONFIG["claude"]["model"]]
    log_path = run_dir / "run.log"
    events_path = run_dir / "events.jsonl"
    result: dict = {}
    if CLEARING.is_set():
        raise RuntimeError("cancelled: local data is being cleared")
    with open(log_path, "w") as lf, open(events_path, "w") as ef:
        proc = subprocess.Popen(cmd, cwd=wt, env=child_env(), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=lf, text=True, start_new_session=True)
        RUNNING_PROCS[pr["key"]] = proc
        timer = threading.Timer(CONFIG.get("review_timeout_sec", 2700),
                                lambda: os.killpg(proc.pid, signal.SIGTERM))
        timer.start()
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                ef.write(line)
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "assistant":
                    for block in (ev.get("message") or {}).get("content", []):
                        if block.get("type") == "tool_use":
                            step = describe_tool(block)
                            lf.write(f"[{now_iso()}] {step}\n")
                            lf.flush()
                            progress(step)
                elif ev.get("type") == "result":
                    result = ev
            proc.wait()
        finally:
            timer.cancel()
            RUNNING_PROCS.pop(pr["key"], None)
    if proc.returncode != 0 and not result:
        tail = log_path.read_text()[-1500:]
        raise RuntimeError(f"claude exited {proc.returncode}: {tail}")
    return result


def parse_output(text: str) -> tuple[str, dict | None]:
    """The review's report and findings. Reviews emit only FINDINGS_JSON and the report is rendered
    from it (writing both made the model generate every finding twice); a <<<REPORT block still wins."""
    report_m = re.search(r"<<<REPORT\s*\n(.*?)\n\s*REPORT>>>", text, re.S)
    json_m = re.search(r"<<<FINDINGS_JSON\s*\n(.*?)\n\s*FINDINGS_JSON>>>", text, re.S)
    findings = None
    if json_m:
        raw = json_m.group(1).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        try:
            findings = json.loads(raw)
        except json.JSONDecodeError:
            findings = None
        if not isinstance(findings, dict):
            findings = None  # e.g. a bare list: the review records it as unparsed instead of failing
    report = text.strip()
    if report_m:
        report = report_m.group(1).strip()
    elif isinstance(findings, dict):
        try:
            report = render_report(findings)
        except Exception:  # odd field types from the model: keep the raw text rather than fail the review
            log.warning("could not render the report from the findings JSON", exc_info=True)
    return report, findings


def _obj(v) -> dict:
    return v if isinstance(v, dict) else {}


def _objs(v) -> list[dict]:
    """The dict items of a list from the model's JSON; anything else is skipped."""
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _strs(v) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else []


def render_report(f: dict) -> str:
    """The plain-text "Full report" for a review, built from its findings JSON. Odd field types from the
    model drop only that field or item, not the report."""
    def lines(*parts):
        return [x for x in parts if x]
    ticket = _obj(f.get("ticket"))
    ticket_line = "Ticket: none linked"
    if ticket.get("key"):
        ticket_line = f"Ticket: {ticket['key']}" + (f" — {ticket['summary']}" if ticket.get("summary") else "")
    out = lines(f"Verdict: {f.get('verdict') or 'n/a'}", ticket_line,
                f"Lint: {f['lint']}" if f.get("lint") else "", "", str(f.get("summary") or "").strip(), "")
    must = _objs(f.get("must_fix"))
    out.append("MUST FIX")
    for i, m in enumerate(must, 1):
        out += [f"{i}. [{str(m.get('severity', '')).upper()}] {m.get('title', '')} ({m.get('ref') or 'n/a'})",
                f"   Cause: {m.get('cause', '')}", f"   Why it matters: {m.get('why', '')}",
                f"   Fix: {m.get('fix', '')}", f"   Importance: {m.get('importance', '')}"]
    if not must:
        out.append("None. Nothing blocks this PR.")
    flow = _obj(f.get("flow_context"))
    if flow.get("summary"):
        out += ["", "FLOW CONTEXT", str(flow["summary"]).strip(), f"Source: {flow.get('source') or 'n/a'}"]
    prev = _objs(f.get("previously_raised"))
    if prev:
        out += ["", "PREVIOUSLY RAISED"]
        out += [f"- [{p.get('status')}] {p.get('ref')}: {p.get('point')}. {p.get('reason') or ''}".rstrip()
                for p in prev]
    ac = _objs(f.get("ac_coverage"))
    if ac:
        out += ["", "AC / SPEC CONSISTENCY"]
        out += [f"- [{a.get('status')}] {a.get('criterion')} → {a.get('ref') or 'n/a'}"
                + (f" ({a['note']})" if a.get("note") else "") for a in ac]
    items = _objs(f.get("findings"))
    for x in items:
        out += ["", f"[{str(x.get('severity', '')).upper()}] {x.get('id', '')} {x.get('title', '')}".rstrip(),
                f"Source: {x.get('source') or 'n/a'}   Scope: {x.get('scope') or 'n/a'}   "
                f"Category: {x.get('category') or 'n/a'}",
                f"Ref: {x.get('ref') or 'n/a'}"]
        out += [f"{label}: {x[k]}" for k, label in (("why", "Why"), ("impact", "Impact"), ("fix", "Fix"))
                if x.get(k)]
        out.append("---")
    kb = _obj(f.get("kb_regression"))
    if kb.get("ran"):
        out += ["", "KNOWLEDGE-BASE REGRESSION", str(kb.get("summary") or "").strip()]
        out += [f"- checked: {c}" for c in _strs(kb.get("checked"))]
    counts = {s: sum(1 for x in items if str(x.get("severity", "")).lower() == s) for s in SEVERITIES}
    scope = _obj(f.get("scope_filter"))
    out += ["", "SUMMARY",
            "- " + "  ".join(f"{s.capitalize()} {counts.get(s, 0)}" for s in SEVERITIES),
            f"- Dropped as out-of-scope: {scope.get('dropped_out_of_scope', 0)}   "
            f"Downgraded: {scope.get('downgraded_medium_to_low', 0)}"]
    if _strs(f.get("top_priorities")):
        out += ["", "TOP PRIORITIES"] + [f"{i}. {t}" for i, t in enumerate(_strs(f["top_priorities"]), 1)]
    if f.get("residual_risk"):
        out += ["", f"RESIDUAL RISK: {f['residual_risk']}"]
    if _strs(f.get("not_validated")):
        out += [f"AREAS NOT FULLY VALIDATED: {'; '.join(_strs(f['not_validated']))}"]
    return "\n".join(out).strip() + "\n"


def token_usage(result: dict) -> dict:
    """Per-model token counts and API-price cost of one claude run, for the run's meta."""
    out = {}
    for model, u in _obj(result.get("modelUsage")).items():
        u = _obj(u)
        out[model] = {"input": u.get("inputTokens", 0), "output": u.get("outputTokens", 0),
                      "cache_read": u.get("cacheReadInputTokens", 0),
                      "cache_write": u.get("cacheCreationInputTokens", 0), "cost_usd": u.get("costUSD")}
    return out


SEVERITIES = ("critical", "high", "medium", "low")


def normalize_findings(f: dict) -> dict:
    items = _objs(f.get("findings"))
    for i, item in enumerate(items, 1):
        item.setdefault("id", f"F{i}")
        item["severity"] = str(item.get("severity", "low")).lower()
    counts = {s: sum(1 for x in items if x["severity"] == s) for s in SEVERITIES}
    f["counts"] = counts
    f["findings"] = items
    return f


def review(key: str, reason: str) -> None:
    with pr_lock(key):
        _review(key, reason)


def _review(key: str, reason: str) -> None:
    pr = dict(STORE.prs[key])
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = REPORTS / key / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()

    def progress(msg: str) -> None:
        STORE.update_pr(key, progress=msg[:200], progress_at=now_iso())

    STORE.update_pr(key, review_status="preparing", current_run=run_id, run_started_at=now_iso(),
                    progress="Checking out PR")
    try:
        wt = prepare_worktree(pr)
        matches = match_kbs(pr)
        kb_notes = []
        if matches:
            STORE.update_pr(key, review_status="kb-refresh")
            kb_notes = refresh_kbs(matches, progress)
        STORE.update_pr(key, review_status="reviewing", progress="Starting review")
        prompt = build_review_prompt(pr, key, matches)
        (run_dir / "prompt.md").write_text(prompt)
        result = run_claude(pr, wt, prompt, run_dir, progress)
        text = result.get("result") or ""
        report, findings = parse_output(text)
        (run_dir / "report.md").write_text(report)
        if findings is None:
            findings = {"verdict": None, "summary": "The review finished but its structured findings "
                        "could not be parsed. See the Full report tab.", "findings": [], "parse_error": True}
        findings = normalize_findings(findings)
        carry_forward_decisions(key, findings)
        findings.setdefault("kb_regression", {})["notes"] = kb_notes
        meta = {
            "run_id": run_id, "reason": reason, "head_sha": pr["head_sha"],
            "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds"),
            "finished_at": now_iso(), "duration_sec": round(time.time() - started),
            "cost_usd": result.get("total_cost_usd"), "num_turns": result.get("num_turns"),
            "is_error": result.get("is_error", False), "session_id": result.get("session_id"),
            "model_usage": token_usage(result),
        }
        (run_dir / "findings.json").write_text(json.dumps({**findings, "meta": meta}, indent=1))
        failed = bool(result.get("is_error")) or not text
        if not failed and result.get("session_id") and CONFIG.get("compact_after_review", True):
            start_compaction(key, run_id, wt, result["session_id"], run_dir)  # before "done": a chat then waits
        ticket = findings.get("ticket")
        STORE.update_pr(
            key, review_status="failed" if failed else "done", reviewed_sha=pr["head_sha"],
            last_run=run_id, last_reviewed_at=now_iso(), outdated=False, seen_run=None,
            counts=findings["counts"], verdict=findings.get("verdict"),
            ticket=ticket.get("key") if isinstance(ticket, dict) else None,
            error=(text[:500] if failed else None), progress=None,
        )
    except Exception as exc:
        log.exception("review of %s failed", key)
        (run_dir / "error.txt").write_text(str(exc))
        STORE.update_pr(key, review_status="failed", error=str(exc)[:800], last_run=run_id,
                        reviewed_sha=pr.get("head_sha"), progress=None)


CLEARING = threading.Event()  # set while "Clear local data" stops background work and deletes files
ACTIVE_REVIEWS: set[str] = set()
BUSY: set[str] = set()  # other jobs that write local data: "reply:<key>", "chat:<key>", "kb:<id>"


class ClearingNow(RuntimeError):
    """A job was refused because "Clear local data" is running."""


_busy_seq = 0


def busy_tag(prefix: str) -> str:
    """A unique tag for jobs that can overlap (e.g. two quick decision clicks)."""
    global _busy_seq
    with QUEUED_LOCK:
        _busy_seq += 1
        return f"{prefix}:{_busy_seq}"


@contextmanager
def busy(tag: str):
    """Register a job that writes local data, so "Clear local data" waits for it (and refuses to
    start it while clearing)."""
    with QUEUED_LOCK:
        if CLEARING.is_set():
            raise ClearingNow("local data is being cleared; try again in a moment")
        BUSY.add(tag)
    try:
        yield
    finally:
        with QUEUED_LOCK:
            BUSY.discard(tag)


def worker_loop() -> None:
    while True:
        key, reason = JOBS.get()
        while True:  # restart or clear pending: hold the job; claim it atomically with the check
            while DRAIN_FILE.exists() or CLEARING.is_set():
                time.sleep(3)
            with QUEUED_LOCK:
                if not CLEARING.is_set():
                    ACTIVE_REVIEWS.add(key)
                    break
        try:
            if key in STORE.prs:  # gone if local data was cleared while it waited
                review(key, reason)
        except Exception:
            log.exception("review worker: %s", key)  # never let one job kill the worker thread
        finally:
            with QUEUED_LOCK:
                QUEUED.discard(key)
                ACTIVE_REVIEWS.discard(key)
            JOBS.task_done()


def cleanup_inactive() -> None:
    """Drop worktrees of PRs that no longer request review (reports are kept)."""
    while True:
        time.sleep(3600)
        with STORE.lock:
            stale = [p for p in STORE.prs.values() if not p.get("active") and p.get("key") and p.get("repo")]
        for pr in stale:
            try:
                wt = PRS_DIR / pr["key"]
                if wt.exists():
                    bare = REPOS_DIR / f"{pr['repo'].replace('/', '__')}.git"
                    stop_compactions(pr["key"])
                    run(["git", "worktree", "remove", "--force", str(wt)], cwd=bare, check=False)
                    shutil.rmtree(wt, ignore_errors=True)
            except Exception:
                log.exception("cleaning up the worktree of %s failed", pr.get("key"))




# ------------------------------------------------------------ replies to your comments
#
# Read-only. Looks at review threads YOU started on a PR, checks the author's replies against
# the current code, and labels each one resolved / expected / still-open / awaiting-reply.
# It never replies, resolves threads, or posts anything.

THREADS_GQL = """
query($owner: String!, $name: String!, $n: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $n) {
      reviewThreads(first: 100) {
        nodes {
          id isResolved isOutdated path line originalLine
          comments(first: 30) { nodes { author { login } body createdAt url } }
        }
      }
    }
  }
}
"""

PR_LOCKS: dict[str, threading.Lock] = {}
RESP_JOBS: "queue.Queue[tuple[str, bool]]" = queue.Queue()
RESP_QUEUED: set[str] = set()
LESSON_LOCK = threading.Lock()
CANDIDATES_FILE = DATA / "lesson_candidates.json"
LESSON_ALIASES = {"dev-reviewer": "code", "pr-reviewer": "consistency"}  # agent names used by older user-mode setups


LESSON_CAP = 30
PROMOTE_AFTER_PRS = 2


def lesson_files() -> dict[str, Path]:
    return {k: xpath(v) for k, v in CONFIG["reviewer"]["lessons_files"].items()}


def pr_lock(key: str) -> threading.Lock:
    with QUEUED_LOCK:
        return PR_LOCKS.setdefault(key, threading.Lock())


def enqueue_response_check(key: str, force: bool = False) -> bool:
    with QUEUED_LOCK:
        if key in RESP_QUEUED:
            return False
        RESP_QUEUED.add(key)
    RESP_JOBS.put((key, force))
    return True


def fetch_threads(pr: dict) -> list[dict]:
    owner, name = pr["repo"].split("/")
    out = run(["gh", "api", "graphql", "-f", f"query={THREADS_GQL}", "-f", f"owner={owner}",
               "-f", f"name={name}", "-F", f"n={pr['number']}"], timeout=120)
    nodes = json.loads(out)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    threads = []
    for t in nodes:
        comments = t["comments"]["nodes"]
        if not comments:
            continue
        threads.append({
            "thread_id": t["id"], "path": t["path"], "line": t.get("line") or t.get("originalLine"),
            "is_resolved": t["isResolved"], "is_outdated": t["isOutdated"], "url": comments[0]["url"],
            "starter": author_login(comments[0]), "original": comments[0]["body"],
            "replies": [{"author": author_login(c), "body": c["body"], "at": c["createdAt"]} for c in comments[1:]],
        })
    return threads


def load_responses(key: str) -> dict:
    f = REPORTS / key / "responses.json"
    return json.loads(f.read_text()) if f.exists() else {}


def map_threads_to_findings(key: str, threads: list[dict]) -> dict[str, dict]:
    """Thread → finding: exact match on a comment posted from this UI, else same file within ±3 lines."""
    runs = [r for r in list_runs(key) if "finished_at" in r]
    by_body: dict[str, dict] = {}
    for r in runs:
        pf = REPORTS / key / r["run_id"] / "posted.json"
        if pf.exists():
            for fid, info in json.loads(pf.read_text()).items():
                if (info.get("body") or "").strip():  # summary posts have no per-finding body to match
                    by_body.setdefault(info["body"].strip(), {"run_id": r["run_id"], "finding_id": fid})
    latest = []
    if runs:
        fj = REPORTS / key / runs[0]["run_id"] / "findings.json"
        latest = json.loads(fj.read_text()).get("findings", []) if fj.exists() else []
    out = {}
    for t in threads:
        hit = by_body.get(t["original"].strip())
        if not hit and runs and t.get("line"):
            for f in latest:
                try:
                    if f.get("file") == t["path"] and abs(int(str(f.get("line")).split("-")[0]) - int(t["line"])) <= 3:
                        hit = {"run_id": runs[0]["run_id"], "finding_id": f["id"]}
                        break
                except (TypeError, ValueError):
                    continue
        if hit:
            fj = REPORTS / key / hit["run_id"] / "findings.json"
            for f in json.loads(fj.read_text()).get("findings", []) if fj.exists() else []:
                if f.get("id") == hit["finding_id"]:
                    hit.update(title=f.get("title"), severity=f.get("severity"), why=f.get("why"), fix=f.get("fix"))
            out[t["thread_id"]] = hit
    return out


def run_response_eval(pr: dict, wt: Path, threads: list[dict], key: str) -> dict[str, dict]:
    cands = load_candidates()
    template = (ROOT / "prompts" / "check_responses.md").read_text()
    prompt = template.format(
        repo=pr["repo"], number=pr["number"], title=pr["title"], head_sha=pr["head_sha"],
        base_ref=pr["base_ref"], viewer=STORE.meta.get("viewer") or "the reviewer",
        threads=json.dumps([{k: t.get(k) for k in ("thread_id", "path", "line", "is_resolved", "is_outdated",
                                                     "original", "replies", "finding")} for t in threads], indent=1),
        candidates=json.dumps([{"candidate_id": c["id"], "target": c["target"], "rule": c["rule"]}
                               for c in cands.values() if c["status"] in ("candidate", "promoted")], indent=1),
    )
    out_dir = REPORTS / key / "responses"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"{stamp}-prompt.md").write_text(prompt)
    cmd = [CONFIG["claude"]["bin"], "-p", "--setting-sources", "user", "--output-format", "json",
           "--permission-mode", "default",
           "--allowedTools", "Bash", "Read", "Grep", "Glob",
           "--disallowedTools", *denied_tools()]
    if CONFIG["claude"].get("model"):
        cmd += ["--model", CONFIG["claude"]["model"]]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=wt, env=child_env(),
                          timeout=CONFIG.get("response_check_timeout_sec", 900))
    (out_dir / f"{stamp}-output.json").write_text(proc.stdout)
    text = json.loads(proc.stdout).get("result", "") if proc.stdout.strip().startswith("{") else proc.stdout
    m = (re.search(r"<<<RESPONSES_JSON\s*\n(.*?)\n\s*RESPONSES_JSON>>>", text, re.S)
         or re.search(r"(\[.*\])", text, re.S))
    if not m:
        raise RuntimeError(f"could not parse reply check output: {text[:300]}")
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", m.group(1).strip())
    return {v["thread_id"]: v for v in json.loads(raw)}


def check_responses(key: str, force: bool = False) -> None:
    pr = dict(STORE.prs.get(key) or {})
    if not pr or not pr.get("active"):
        return
    viewer = STORE.meta.get("viewer")
    threads = [t for t in fetch_threads(pr) if t["starter"] == viewer]
    prev = load_responses(key)
    sig = hashlib.sha1(json.dumps([pr["head_sha"]] + [
        (t["thread_id"], t["is_resolved"], t["is_outdated"], len(t["replies"])) for t in threads]).encode()).hexdigest()
    if not force and prev.get("sig") == sig:
        return
    lock = pr_lock(key)
    if not lock.acquire(blocking=False):
        return  # a review is running; the next poll retries because sig wasn't saved
    try:
        prev_by_id = {t["thread_id"]: t for t in prev.get("threads", [])}
        mapping = map_threads_to_findings(key, threads)
        results, to_eval = [], []
        for t in threads:
            t["finding"] = mapping.get(t["thread_id"])
            others = [r for r in t["replies"] if r["author"] != viewer]
            old = prev_by_id.get(t["thread_id"])
            unchanged = old and old.get("n_replies") == len(t["replies"]) \
                and old.get("is_resolved") == t["is_resolved"] \
                and old.get("is_outdated") == t["is_outdated"] and old.get("head_sha") == pr["head_sha"]
            if unchanged and not force:
                results.append({**old, "finding": t["finding"] or old.get("finding")})
            elif not others and not t["is_resolved"] and not t["is_outdated"]:
                results.append({**t, "status": "awaiting-reply", "note": "No reply yet."})
            else:
                to_eval.append(t)
        if to_eval:
            STORE.update_pr(key, responses_checking=True)
            wt = prepare_worktree(pr)
            verdicts = run_response_eval(pr, wt, to_eval, key)
            for t in to_eval:
                v = verdicts.get(t["thread_id"], {})
                status = v.get("status") if v.get("status") in (
                    "resolved", "expected", "still-open", "awaiting-reply") else "still-open"
                entry = {**t, "status": status, "note": v.get("note") or "", "lesson": v.get("lesson")}
                if status == "expected" and v.get("lesson") and v["lesson"].get("rule"):
                    entry["lesson_id"] = record_lesson(key, v["lesson"], t["url"])
                results.append(entry)
        for r in results:
            r.update(n_replies=len(r.get("replies", [])), head_sha=pr["head_sha"],
                     checked_at=r.get("checked_at") or now_iso())
        (REPORTS / key).mkdir(parents=True, exist_ok=True)
        (REPORTS / key / "responses.json").write_text(json.dumps(
            {"sig": sig, "checked_at": now_iso(), "head_sha": pr["head_sha"], "threads": results}, indent=1))
        STORE.update_pr(key, responses=dict(Counter(r["status"] for r in results)), responses_checking=False)
    finally:
        lock.release()
        STORE.update_pr(key, responses_checking=False)


def previous_responses_section(key: str) -> str:
    threads = [t for t in load_responses(key).get("threads", [])
               if t.get("status") in ("resolved", "expected", "still-open")]
    if not threads:
        return "None yet."
    lines = ["These points were already raised on this PR and the author replied. Respect them:"]
    for t in threads:
        what = (t.get("finding") or {}).get("title") or t["original"][:160].replace("\n", " ")
        rule = {"expected": "author's explanation was valid. DO NOT flag this again unless the new code changes "
                            "that reasoning",
                "resolved": "fixed in code. Don't re-flag; only flag a regression of the fix",
                "still-open": "still open after the reply. Keep it if the code still has the problem"}[t["status"]]
        lines.append(f"- `{t['path']}:{t.get('line')}`: {what}. **{t['status']}**: {rule}. ({t.get('note', '')})")
    return "\n".join(lines)


def response_worker() -> None:
    while True:
        key, force = RESP_JOBS.get()
        try:
            with busy(f"reply:{key}"):
                check_responses(key, force)
        except ClearingNow:
            log.info("reply check for %s skipped: local data is being cleared", key)
        except Exception:
            log.exception("reply check failed for %s", key)
            STORE.update_pr(key, responses_checking=False)
        finally:
            with QUEUED_LOCK:
                RESP_QUEUED.discard(key)


# ---------------------------------------------------------------- learned lessons


def load_candidates() -> dict:
    return json.loads(CANDIDATES_FILE.read_text()) if CANDIDATES_FILE.exists() else {}


def save_candidates(c: dict) -> None:
    CANDIDATES_FILE.write_text(json.dumps(c, indent=1))


def record_lesson(pr_key: str, lesson: dict, thread_url: str) -> str | None:
    target = LESSON_ALIASES.get(lesson.get("target"), lesson.get("target"))
    target = target if target in lesson_files() else "consistency"
    with LESSON_LOCK:
        cands = load_candidates()
        cid = lesson.get("candidate_id")
        if cid not in cands:
            cid = f"L{len(cands) + 1}"
            cands[cid] = {"id": cid, "target": target, "rule": lesson["rule"].strip(), "occurrences": [],
                          "status": "candidate", "created_at": now_iso()}
        c = cands[cid]
        if pr_key not in {o["pr"] for o in c["occurrences"]}:
            c["occurrences"].append({"pr": pr_key, "thread": thread_url, "at": now_iso()})
        if c["status"] == "candidate" and len({o["pr"] for o in c["occurrences"]}) >= PROMOTE_AFTER_PRS:
            promote_lesson(c)
        save_candidates(cands)
        return cid


def promote_lesson(c: dict) -> None:
    """Insert the rule at the top of the lessons file (under the comment header). Never overwrites."""
    path = lesson_files()[LESSON_ALIASES.get(c["target"], c["target"])]
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else [
        f"# {c['target']} reviewer lessons learned",
        "# One rule per line, newest first: - YYYY-MM-DD: <rule>. Lines starting with # are comments."]
    active = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    if len(active) >= LESSON_CAP:
        c["status"] = "needs-prune"
        return
    entry = f"- {datetime.now().strftime('%Y-%m-%d')}: {c['rule']} (auto-learned from PR replies)"
    i = 0
    while i < len(lines) and (lines[i].lstrip().startswith("#") or not lines[i].strip()):
        i += 1
    lines.insert(i, entry)
    path.write_text("\n".join(lines) + "\n")
    c.update(status="promoted", entry=entry, file=str(path), promoted_at=now_iso())
    log.info("added lesson %s to %s: %s", c["id"], path.name, c["rule"])


def remove_lesson(cid: str) -> dict:
    with LESSON_LOCK:
        cands = load_candidates()
        c = cands[cid]
        if c.get("status") == "promoted" and c.get("file"):
            path = Path(c["file"])
            lines = path.read_text().splitlines()
            path.write_text("\n".join(ln for ln in lines if ln != c["entry"]) + "\n")
        c["status"] = "removed"
        save_candidates(cands)
        return c


def manual_promote(cid: str) -> dict:
    with LESSON_LOCK:
        cands = load_candidates()
        c = cands[cid]
        if c["status"] in ("candidate", "needs-prune", "removed"):
            promote_lesson(c)
        save_candidates(cands)
        return c


# ------------------------------------------------------- user-approved PR comments
#
# The ONLY write path to GitHub. It runs solely when the user clicks "Post" in the UI
# on drafts they have already seen. Background reviews never reach this code.



def run_record(key: str, run_id: str) -> Path:
    if not RUN_ID_RE.match(str(run_id)):
        raise ValueError("unknown run")
    run_dir = (REPORTS / key / run_id).resolve()
    if not run_dir.is_relative_to(REPORTS.resolve()) or not run_dir.is_dir():
        raise ValueError("unknown run")
    return run_dir


def draft_comments(key: str, run_id: str, finding_ids: list[str]) -> list[dict]:
    pr = STORE.prs[key]
    run_dir = run_record(key, run_id)
    data = json.loads((run_dir / "findings.json").read_text())
    chosen = [f for f in data.get("findings", []) if f.get("id") in set(finding_ids)]
    if not chosen:
        return []
    slim = [{k: f.get(k) for k in ("id", "severity", "title", "file", "line", "why", "impact", "fix")}
            for f in chosen]
    template = (ROOT / "prompts" / "draft_comment.md").read_text()
    prompt = template.format(repo=pr["repo"], number=pr["number"], title=pr["title"],
                                 findings=json.dumps(slim, indent=1))
    cmd = [CONFIG["claude"]["bin"], "-p", "--setting-sources", "user", "--output-format", "json", "--tools", ""]
    if CONFIG["claude"].get("draft_model"):
        cmd += ["--model", CONFIG["claude"]["draft_model"]]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300,
                          env=child_env(), cwd=DATA)
    text = json.loads(proc.stdout).get("result", "") if proc.stdout.strip().startswith("{") else proc.stdout
    m = re.search(r"\[.*\]", text, re.S)
    bodies = {d["finding_id"]: d["body"] for d in json.loads(m.group(0))} if m else {}
    commentable = commentable_lines(pr, data.get("meta", {}).get("head_sha") or pr["head_sha"])
    drafts = []
    for f in chosen:
        path, line = f.get("file"), f.get("line")
        try:
            line = int(str(line).split("-")[0]) if line not in (None, "") else None
        except ValueError:
            line = None
        inline = bool(path and line and line in commentable.get(path, set()))
        drafts.append({
            "finding_id": f["id"], "severity": f.get("severity"), "title": f.get("title"),
            "path": path, "line": line, "inline": inline,
            "body": bodies.get(f["id"]) or f"{f.get('title')}. {f.get('fix') or ''}".strip(),
        })
    return drafts


def commentable_lines(pr: dict, sha: str) -> dict[str, set[int]]:
    """RIGHT-side lines GitHub accepts inline comments on (hunk lines incl. 3 lines of context)."""
    bare = REPOS_DIR / f"{pr['repo'].replace('/', '__')}.git"
    try:
        diff = run(["git", "diff", "--no-color", "-U3", f"refs/remotes/origin/{pr['base_ref']}...{sha}"],
                   cwd=bare, timeout=300)
    except Exception:
        log.exception("could not compute diff lines for %s", pr["key"])
        return {}
    lines: dict[str, set[int]] = {}
    current = None
    for row in diff.splitlines():
        if row.startswith("+++ "):
            current = row[6:] if row.startswith("+++ b/") else None
        elif row.startswith("@@") and current:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", row)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                lines.setdefault(current, set()).update(range(start, start + count))
    return lines


# ------------------------------------------------------------- approving a PR
#
# The Approve button shows only when the GitHub token PRism uses is allowed to approve. Classic and
# `gh` login tokens list their scopes in a header. Fine-grained tokens don't, so PRism asks GitHub to
# delete a review that doesn't exist: that needs the same "Pull requests: write" permission, answers
# 403 without it and 404 with it, and can never change anything.

APPROVE_CACHE: dict[tuple[str, str], tuple[float, bool, str]] = {}
APPROVE_CACHE_SEC = 3600


def token_can_approve(repo: str, number: int) -> tuple[bool, str]:
    token = DOTENV.get("GITHUB_TOKEN") or DOTENV.get("GH_TOKEN") or "gh-login"
    ck = (hashlib.sha256(token.encode()).hexdigest()[:16], repo)
    hit = APPROVE_CACHE.get(ck)
    if hit and time.time() - hit[0] < APPROVE_CACHE_SEC:
        return hit[1], hit[2]
    env = child_env()
    try:
        who = subprocess.run(["gh", "api", "-i", "user"], capture_output=True, text=True, env=env, timeout=30)
    except subprocess.TimeoutExpired:
        return False, "couldn't reach GitHub to check the token (timed out)"
    if who.returncode != 0:
        return False, "couldn't reach GitHub to check the token"  # transient: not cached
    scopes = next((h.split(":", 1)[1] for h in who.stdout.splitlines() if h.lower().startswith("x-oauth-scopes:")),
                  None)
    if scopes is not None:
        can = "repo" in {x.strip() for x in scopes.split(",")}  # public_repo can't approve private repos
        why = "" if can else "your GitHub token doesn't have the 'repo' scope"
    else:
        try:
            probe = subprocess.run(["gh", "api", "-i", "-X", "DELETE", f"repos/{repo}/pulls/{number}/reviews/1"],
                                   capture_output=True, text=True, env=env, timeout=30).stdout
        except subprocess.TimeoutExpired:
            return False, "couldn't reach GitHub to check the token (timed out)"
        status = int(m.group(1)) if (m := re.match(r"HTTP/\S+ (\d{3})", probe)) else 0
        if status not in (403, 404, 422):
            return False, f"couldn't check the token's permission (HTTP {status or '?'})"  # not cached
        can = status != 403
        why = "" if can else "your GitHub token can't write pull requests (needs 'Pull requests: Read and write')"
    APPROVE_CACHE[ck] = (time.time(), can, why)
    return can, why


def approve_check(key: str) -> dict:
    pr = STORE.prs[key]
    viewer = STORE.meta.get("viewer")
    if not pr.get("active", True) or pr.get("closed_state"):
        return {"can": False, "reason": "the PR isn't open"}
    if viewer and pr.get("author") == viewer:
        return {"can": False, "reason": "you can't approve your own PR"}
    can, why = token_can_approve(pr["repo"], pr["number"])
    run_sha = None
    if pr.get("last_run"):
        try:
            run_sha = (json.loads((run_record(key, pr["last_run"]) / "findings.json").read_text())
                       .get("meta", {}).get("head_sha"))
        except (OSError, ValueError):
            pass
    return {"can": can, "reason": why, "already_approved": pr.get("my_verdict") == "APPROVED",
            "head_sha": pr["head_sha"], "reviewed_sha": run_sha,
            "new_commits_since_review": bool(run_sha and run_sha != pr["head_sha"])}


class StaleApproval(Exception):
    """The PR moved on since the confirmation dialog was opened."""


def approve_pr(key: str, confirm: str, body: str = "", expected_sha: str | None = None) -> dict:
    """Approve exactly the commit the confirmation dialog showed. Only called from that dialog."""
    if (confirm or "").strip().lower() != "approve":
        raise ValueError('type "approve" to confirm')
    pr = STORE.prs[key]
    if not expected_sha:
        raise ValueError("the commit you're approving is missing; reload the page and try again")
    sha = expected_sha
    if sha != pr["head_sha"]:
        raise StaleApproval(f"new commits were pushed since you opened this dialog (now {pr['head_sha'][:8]}). "
                            "Close it, check the changes, and approve again.")
    payload = {"commit_id": sha, "event": "APPROVE"}
    if body.strip():
        payload["body"] = body.strip()[:2000]
    proc = subprocess.run(["gh", "api", "-X", "POST", f"repos/{pr['repo']}/pulls/{pr['number']}/reviews",
                           "--input", "-"],
                          input=json.dumps(payload), capture_output=True, text=True, env=child_env(), timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:600] or proc.stdout.strip()[:600])
    review = json.loads(proc.stdout)
    STORE.update_pr(key, my_verdict="APPROVED", my_state="reviewed", my_last_at=now_iso())
    log.info("user approved %s#%s at %s", pr["repo"], pr["number"], sha[:12])
    return {"ok": True, "url": review.get("html_url")}


GITHUB_BODY_LIMIT = 60000  # GitHub allows 65,536 characters; leave room for the "didn't fit" note
POSTED_LOCKS: dict[str, threading.Lock] = {}


def posted_lock(key: str) -> threading.Lock:
    """posted.json is read, changed and written by both post flows; one at a time per PR."""
    with QUEUED_LOCK:
        return POSTED_LOCKS.setdefault(key, threading.Lock())


def _one_line(text) -> str:
    return " ".join(str(text or "").split())


def _summary_block(n: int, f: dict) -> str:
    where = (f"`{f['file']}:{f['line']}`" if f.get("file") and f.get("line")
             else f"`{f['file']}`" if f.get("file") else _one_line(f.get("ref")))
    lines = [f"**{n}. {_one_line(f.get('title')) or 'Finding'}**" + (f" · {where}" if where else "")]
    if _one_line(f.get("why")):
        lines.append(f"- **What's wrong:** {_one_line(f.get('why'))}")
    if _one_line(f.get("impact")):
        lines.append(f"- **Why it matters:** {_one_line(f.get('impact'))}")
    fix = str(f.get("fix") or "").strip()
    if fix:
        indented = fix.replace("\n", "\n  ")  # keep a snippet's lines inside the list item
        # a fix that opens with a code fence must start on its own line, or the fence won't render
        lines.append("- **How to fix:**\n  " + indented if fix.startswith("```") else "- **How to fix:** " + indented)
    return "\n".join(lines) + "\n"


def build_summary_comment(data: dict, posted: dict | None = None) -> tuple[str, list[str]]:
    """Open findings of a run as ONE readable comment, grouped by severity. Like "Post all", findings that
    were dismissed or already posted are left out. Findings are added whole until GitHub's size limit, so
    the returned ids are exactly the ones in the comment. Built from the report, so no model call."""
    posted = posted or {}
    open_findings = [f for f in data.get("findings") or []
                     if not f.get("dismissed") and f.get("severity") in SEVERITIES and f.get("id") not in posted]
    counts = {sev: sum(1 for f in open_findings if f["severity"] == sev) for sev in SEVERITIES}
    lines = ["## Review summary", ""]
    if not open_findings:
        lines.append("I went through this PR and didn't find anything else that needs changing.")
        return "\n".join(lines) + "\n", []
    tally = ", ".join(f"{n} {sev}" for sev, n in counts.items() if n)
    plural = "s" if len(open_findings) != 1 else ""
    lines += [f"I went through this PR and found **{len(open_findings)} thing{plural}** worth a look ({tally}).", ""]
    if _one_line(data.get("summary")):
        lines += [_one_line(data.get("summary")), ""]
    body, ids, n = "\n".join(lines), [], 0
    for sev in SEVERITIES:
        group = [f for f in open_findings if f["severity"] == sev]
        if not group:
            continue
        heading = f"### {sev.capitalize()}\n\n"
        for i, f in enumerate(group):
            block = (heading if i == 0 else "") + _summary_block(n + 1, f) + "\n"
            if len(body) + len(block) > GITHUB_BODY_LIMIT:
                left = len(open_findings) - n
                return (body.rstrip() + f"\n\n_({left} more finding{'s' if left != 1 else ''} didn't fit in one "
                        "comment. Post them separately.)_\n", ids)
            body += block
            n += 1
            if f.get("id"):
                ids.append(f["id"])
    return body.rstrip() + "\n", ids


def summary_draft(key: str, run_id: str) -> dict:
    run_dir = run_record(key, run_id)
    data = json.loads((run_dir / "findings.json").read_text())
    posted_file = run_dir / "posted.json"
    posted = json.loads(posted_file.read_text()) if posted_file.exists() else {}
    body, ids = build_summary_comment(data, posted)
    return {"body": body, "finding_ids": ids}


def post_summary(key: str, run_id: str, body: str, finding_ids: list[str]) -> dict:
    """Post the user-approved summary as ONE review comment (event COMMENT, no inline comments)."""
    body = str(body or "").strip()
    if not body:
        raise ValueError("the comment is empty")
    if len(body) > 65000:
        raise ValueError("the comment is longer than GitHub allows; shorten it")
    pr = STORE.prs[key]
    run_dir = run_record(key, run_id)
    data = json.loads((run_dir / "findings.json").read_text())
    sha = data.get("meta", {}).get("head_sha") or pr["head_sha"]
    proc = subprocess.run(["gh", "api", "-X", "POST", f"repos/{pr['repo']}/pulls/{pr['number']}/reviews",
                           "--input", "-"],
                          input=json.dumps({"commit_id": sha, "event": "COMMENT", "body": body}),
                          capture_output=True, text=True, env=child_env(), timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:600] or proc.stdout.strip()[:600])
    review = json.loads(proc.stdout)
    known = {f.get("id") for f in data.get("findings") or []} - {None, ""}
    recorded = [fid for fid in dict.fromkeys(finding_ids or []) if fid in known]
    with posted_lock(key):
        posted_file = run_dir / "posted.json"
        posted = json.loads(posted_file.read_text()) if posted_file.exists() else {}
        for fid in recorded:
            note = {"summary_url": review.get("html_url"), "summary_posted_at": now_iso()}
            # keep an earlier inline comment's record (its exact body links replies back to the finding)
            posted[fid] = ({**posted[fid], **note} if fid in posted else
                           {"url": review.get("html_url"), "posted_at": now_iso(), "inline": False,
                            "summary": True, "body": None, **note})
        posted_file.write_text(json.dumps(posted, indent=1))
    log.info("user posted a summary comment (%d findings) to %s#%s", len(recorded), pr["repo"], pr["number"])
    return {"url": review.get("html_url"), "findings": len(recorded)}


def post_comments(key: str, run_id: str, comments: list[dict]) -> dict:
    """Post exactly the user-approved drafts as ONE review with event=COMMENT."""
    pr = STORE.prs[key]
    run_dir = run_record(key, run_id)
    data = json.loads((run_dir / "findings.json").read_text())
    sha = data.get("meta", {}).get("head_sha") or pr["head_sha"]
    inline, general = [], []
    for c in comments:
        body = str(c.get("body", "")).strip()
        if not body:
            continue
        if c.get("inline") and c.get("path") and c.get("line"):
            inline.append({"path": c["path"], "line": int(c["line"]), "side": "RIGHT", "body": body})
        else:
            where = f"`{c['path']}:{c['line']}`: " if c.get("path") and c.get("line") else (
                f"`{c['path']}`: " if c.get("path") else "")
            general.append(f"- {where}{body}")
    if not inline and not general:
        raise ValueError("nothing to post")
    payload = {"commit_id": sha, "event": "COMMENT", "comments": inline}
    if general:
        payload["body"] = "\n".join(general)
    env = child_env()
    owner, repo = pr["repo"].split("/")
    proc = subprocess.run(["gh", "api", "-X", "POST", f"repos/{owner}/{repo}/pulls/{pr['number']}/reviews",
                           "--input", "-"], input=json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:600] or proc.stdout.strip()[:600])
    review = json.loads(proc.stdout)
    with posted_lock(key):
        posted_file = run_dir / "posted.json"
        posted = json.loads(posted_file.read_text()) if posted_file.exists() else {}
        for c in comments:
            posted[c["finding_id"]] = {"url": review.get("html_url"), "posted_at": now_iso(),
                                       "inline": bool(c.get("inline")), "body": c.get("body")}
        posted_file.write_text(json.dumps(posted, indent=1))
    log.info("user posted %d comment(s) to %s#%s", len(comments), pr["repo"], pr["number"])
    return {"url": review.get("html_url"), "inline": len(inline), "general": len(general)}



# ------------------------------------------------ discussing findings with the agent
#
# A private chat between YOU (the reviewer) and the review agent about a report. It is never
# shown to the PR author and never touches GitHub. The agent can only PROPOSE a change (new
# severity, dismiss, keep, a PR-level context note, or a re-review); nothing changes until you
# click Apply. Applied decisions live in decisions.json as one-liners and are fed to the next
# review, so they survive re-reviews without replaying the chat.

CHAT_LOCKS: dict[str, threading.Lock] = {}
DECISION_LOCK = threading.Lock()
FINDING_ACTIONS = ("severity", "dismiss", "keep")
PR_ACTIONS = ("context", "rereview")
DECISIONS_IN_PROMPT = 25


def chat_lock(key: str) -> threading.Lock:
    with QUEUED_LOCK:
        return CHAT_LOCKS.setdefault(key, threading.Lock())


def load_chat(key: str) -> dict:
    f = REPORTS / key / "chat.json"
    return json.loads(f.read_text()) if f.exists() else {"sessions": {}, "messages": []}


def save_chat(key: str, chat: dict) -> None:
    (REPORTS / key).mkdir(parents=True, exist_ok=True)
    (REPORTS / key / "chat.json").write_text(json.dumps(chat, indent=1))


def load_decisions(key: str) -> list[dict]:
    f = REPORTS / key / "decisions.json"
    return json.loads(f.read_text()).get("decisions", []) if f.exists() else []


def decision_seq(key: str, decisions: list[dict] | None = None) -> int:
    """Highest decision number ever used on this PR (kept after an undo, so ids are never reused)."""
    f = REPORTS / key / "decisions.json"
    stored = json.loads(f.read_text()).get("seq", 0) if f.exists() else 0
    ids = [int(d["id"][1:]) for d in (decisions if decisions is not None else load_decisions(key))
           if str(d.get("id", ""))[1:].isdigit()]
    return max([stored, *ids])


def save_decisions(key: str, decisions: list[dict]) -> None:
    (REPORTS / key).mkdir(parents=True, exist_ok=True)
    seq = decision_seq(key, decisions)
    (REPORTS / key / "decisions.json").write_text(json.dumps({"decisions": decisions, "seq": seq}, indent=1))


def chat_payload(key: str) -> dict:
    return {"messages": load_chat(key)["messages"], "decisions": load_decisions(key),
            "busy": chat_lock(key).locked()}


def norm_title(t) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(t or "").lower()).strip()


def recount(data: dict) -> None:
    """Counts and must_fix follow the effective severities; dismissed findings drop out of both."""
    live = [x for x in data.get("findings", []) if not x.get("dismissed")]
    data["counts"] = {s: sum(1 for x in live if x["severity"] == s) for s in SEVERITIES}
    if not isinstance(data.get("must_fix"), list):
        return
    pending = {x["id"]: x for x in live if x["severity"] in ("critical", "high", "medium")}
    items = []
    for m in data["must_fix"]:
        x = pending.pop(m.get("finding_id"), None)
        if x:
            items.append({**m, "severity": x["severity"]})
    items += [{"finding_id": x["id"], "severity": x["severity"], "title": x.get("title"), "ref": x.get("ref"),
               "cause": x.get("why"), "why": x.get("impact"), "fix": x.get("fix")} for x in pending.values()]
    for m in items:
        m["importance"] = "fix before merge" if m["severity"] == "medium" else "blocks merge"
    data["must_fix"] = sorted(items, key=lambda m: SEVERITIES.index(m["severity"]))


def apply_decision(x: dict, d: dict) -> None:
    x.setdefault("original_severity", x["severity"])
    x["severity"] = x["original_severity"]
    x.pop("dismissed", None)
    x["decision_id"] = d["id"]
    if d["action"] == "severity":
        x["severity"] = d["to"]
    elif d["action"] == "dismiss":
        x["dismissed"] = True


def clear_decision(x: dict) -> None:
    if "original_severity" in x:
        x["severity"] = x.pop("original_severity")
    x.pop("dismissed", None)
    x.pop("decision_id", None)


def carry_forward_decisions(key: str, data: dict) -> None:
    """After a re-review, re-apply your decisions to findings the reviewer raised again."""
    ds = {d["id"]: d for d in load_decisions(key) if d.get("finding_id")}
    by_sig = {(d.get("file"), norm_title(d.get("title"))): d for d in ds.values()}
    for x in data.get("findings", []):
        reopen = x.pop("reopen", None)
        d = ds.get(x.get("decision_id")) or by_sig.get((x.get("file"), norm_title(x.get("title"))))
        x.pop("decision_id", None)
        if not d:
            continue
        if reopen:
            x["reopened"] = {"decision_id": d["id"], "reason": str(reopen)[:300]}
        else:
            apply_decision(x, d)
    recount(data)


def decision_line(d: dict) -> str:
    if d["action"] == "context":
        return f"- {d['id']} (PR context): {d['reason']}"
    where = f"`{d['file']}:{d.get('line')}`" if d.get("file") else "(no file)"
    what = {"severity": f"severity {d['from']} → {d['to']}", "dismiss": "dismissed, not a real issue",
            "keep": f"confirmed valid as {d['from']}"}[d["action"]]
    return f"- {d['id']} {where} \"{d.get('title')}\": {what}. {d['reason']}"


def decisions_section(key: str) -> str:
    ds = load_decisions(key)[-DECISIONS_IN_PROMPT:]
    if not ds:
        return "None yet."
    return "\n".join([
        "The reviewer discussed earlier findings on this PR with you and decided the following. If you",
        'raise one of these issues again, put its id in that finding\'s `"decision_id"` and leave the',
        "severity as you see it: the service applies the reviewer's decision. Only if the code changed so",
        'the reason no longer holds, also set `"reopen"` to one line saying what changed. PR context notes',
        "are facts from the reviewer: take them into account everywhere.",
        *map(decision_line, ds)])


def sync_counts(key: str, run_id: str, data: dict) -> None:
    if (STORE.prs.get(key) or {}).get("last_run") == run_id:
        STORE.update_pr(key, counts=data["counts"])


def add_decision(key: str, run_id: str, body: dict) -> dict:
    action = body.get("action")
    reason = " ".join(str(body.get("reason") or "").split())[:300]
    if not reason:
        raise ValueError("a one-line reason is required")
    run_dir = run_record(key, run_id)
    with DECISION_LOCK:
        ds = load_decisions(key)
        next_id = f"D{decision_seq(key, ds) + 1}"
        if action == "context":
            d = {"id": next_id, "action": "context", "reason": reason, "run_id": run_id, "at": now_iso()}
            ds.append(d)
            save_decisions(key, ds)
            data = None
        elif action in FINDING_ACTIONS:
            fj = run_dir / "findings.json"
            data = json.loads(fj.read_text())
            x = next((f for f in data.get("findings", []) if f.get("id") == body.get("finding_id")), None)
            if not x:
                raise ValueError("unknown finding")
            frm = x.get("original_severity", x["severity"])
            to = body.get("to")
            if action == "severity" and to not in SEVERITIES:
                raise ValueError("severity must be critical, high, medium or low")
            if action == "severity" and to == frm:
                action, to = "keep", None
            ds = [o for o in ds if o["id"] != x.get("decision_id")]  # a new decision replaces the old one
            d = {"id": next_id, "finding_id": x["id"], "run_id": run_id, "title": x.get("title"),
                 "file": x.get("file"), "line": x.get("line"), "action": action, "from": frm,
                 "to": to if action == "severity" else None, "reason": reason, "at": now_iso()}
            ds.append(d)
            save_decisions(key, ds)
            x.pop("reopened", None)
            apply_decision(x, d)
            recount(data)
            fj.write_text(json.dumps(data, indent=1))
        else:
            raise ValueError("unknown action")
        mark_proposal(key, body, d["id"])
    if data:
        sync_counts(key, run_id, data)
    return d


def mark_proposal(key: str, body: dict, value: str) -> None:
    """Record what happened to a proposal (decision id, "queued" or "declined"). Caller holds DECISION_LOCK."""
    mi, pi = body.get("message_index"), body.get("proposal_index")
    if not isinstance(mi, int) or not isinstance(pi, int):
        return
    chat = load_chat(key)
    try:
        chat["messages"][mi]["proposals"][pi]["applied"] = value
    except (IndexError, KeyError, TypeError):
        return
    save_chat(key, chat)


def resolve_proposal(key: str, body: dict) -> None:
    """Re-review or decline a proposal from the chat. Re-review only queues a normal review."""
    with DECISION_LOCK:
        chat = load_chat(key)
        try:
            p = chat["messages"][body["message_index"]]["proposals"][body["proposal_index"]]
        except (IndexError, KeyError, TypeError):
            raise ValueError("unknown proposal") from None
        if body.get("decision") == "rereview" and p["action"] == "rereview":
            enqueue(key, "re-review after discussion")
            mark_proposal(key, body, "queued")
        elif body.get("decision") == "decline":
            mark_proposal(key, body, "declined")
        else:
            raise ValueError("unknown decision")


def undo_decision(key: str, did: str) -> None:
    with DECISION_LOCK:
        ds = load_decisions(key)
        if not any(d["id"] == did for d in ds):
            raise ValueError("unknown decision")
        save_decisions(key, [d for d in ds if d["id"] != did])
        for r in list_runs(key):
            fj = REPORTS / key / r["run_id"] / "findings.json"
            if not fj.exists():
                continue
            data = json.loads(fj.read_text())
            hit = [x for x in data.get("findings", []) if x.get("decision_id") == did]
            if hit:
                for x in hit:
                    clear_decision(x)
                recount(data)
                fj.write_text(json.dumps(data, indent=1))
                sync_counts(key, r["run_id"], data)


def build_chat_prompt(pr: dict, key: str, run_id: str, data: dict) -> str:
    slim = []
    for x in data.get("findings", []):
        item = {k: x.get(k) for k in ("id", "severity", "title", "ref", "scope", "category") if x.get(k)}
        item.update({k: str(x[k])[:400] for k in ("why", "fix") if x.get(k)})
        if x.get("dismissed"):
            item["dismissed_by_reviewer"] = True
        if x.get("original_severity") and x["original_severity"] != x["severity"]:
            item["original_severity"] = x["original_severity"]
        slim.append(item)
    ds = load_decisions(key)
    template = (ROOT / "prompts" / "discuss.md").read_text()
    return template.format(
        repo=pr["repo"], number=pr["number"], title=pr["title"], url=pr["url"], author=pr["author"],
        head_ref=pr["head_ref"], base_ref=pr["base_ref"], run_id=run_id,
        verdict=data.get("verdict") or "n/a", summary=data.get("summary") or "",
        findings=json.dumps(slim, indent=1),
        decisions="\n".join(map(decision_line, ds)) if ds else "None yet.")


COMPACT_PROMPT = ("/compact This session will be used to discuss the review with the reviewer. Keep every "
                  "finding with its ID, the code you read and what it showed, the callers and paths you checked, "
                  "and what you ruled out and why.")


COMPACTING: dict[tuple[str, str], threading.Event] = {}  # (key, run_id) -> set when compaction ends


def start_compaction(key: str, run_id: str, wt: Path, session: str, run_dir: Path) -> None:
    """Compact the review's session in its own thread, outside the PR lock and the review worker slot.
    A chat that starts meanwhile waits for it (see wait_for_compaction)."""
    job_busy = busy(f"compact:{key}:{run_id}")
    try:
        job_busy.__enter__()  # registered before the thread starts, so "Clear local data" always waits for it
    except ClearingNow:
        return
    done = threading.Event()
    COMPACTING[(key, run_id)] = done

    def finish() -> None:
        done.set()
        COMPACTING.pop((key, run_id), None)
        with COMPACT_LOCK:
            COMPACT_STOPPED.discard((key, run_id))
        job_busy.__exit__(None, None, None)

    def job() -> None:
        try:
            compact_review_session(key, run_id, wt, session, run_dir)
        finally:
            finish()
    try:
        threading.Thread(target=job, name=f"compact-{key}", daemon=True).start()
    except Exception:  # e.g. no threads left: skip compaction, chats start from the report
        log.warning("could not start compaction for %s", key, exc_info=True)
        finish()


def stop_compactions(key: str) -> None:
    """Stop a PR's running compactions and wait for their threads, e.g. before its worktree is deleted.
    The chats then start from the report."""
    def signal_all(sig) -> None:
        with COMPACT_LOCK:
            procs = [proc for (k, _), proc in COMPACT_PROCS.items() if k == key]
        for proc in procs:
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
    pending = [done for (k, _), done in list(COMPACTING.items()) if k == key]
    with COMPACT_LOCK:  # one that hasn't launched its process yet now never will
        COMPACT_STOPPED.update(tag for tag in list(COMPACTING) if tag[0] == key)
    signal_all(signal.SIGTERM)  # signal only: the compaction's own thread reaps its process
    if not all(done.wait(10) for done in pending):
        signal_all(signal.SIGKILL)
        for done in pending:
            done.wait(10)


COMPACTION_WAIT_SEC = 45  # a chat waits at most this long for a running compaction, then starts from the report


def wait_for_compaction(key: str, run_id: str) -> None:
    done = COMPACTING.get((key, run_id))
    if done:
        done.wait(COMPACTION_WAIT_SEC)


def stop_process(proc: subprocess.Popen) -> None:
    """SIGTERM the process group, SIGKILL it if it lingers, and reap it."""
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=wait)
            return
        except (subprocess.TimeoutExpired, ValueError):
            continue


def compact_review_session(key: str, run_id: str, wt: Path, session: str, run_dir: Path) -> None:
    """Compact a copy of the finished review's session for chats. Best effort: never raises."""
    cmd = [CONFIG["claude"]["bin"], "-p", "--setting-sources", "user", "--output-format", "json", "--tools", "",
           "--resume", session, "--fork-session"]
    if CONFIG["claude"].get("model"):
        cmd += ["--model", CONFIG["claude"]["model"]]
    tag = (key, run_id)  # in COMPACT_PROCS so cancel, clear and shutdown can kill it
    try:
        with COMPACT_LOCK:  # launch and register together, so a stop never misses the process
            if CLEARING.is_set() or tag in COMPACT_STOPPED:
                return
            proc = subprocess.Popen(cmd, cwd=wt, env=child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
            COMPACT_PROCS[tag] = proc
        try:
            if CLEARING.is_set():  # clearing started while the process was launching
                stop_process(proc)
                return
            stdout, stderr = proc.communicate(COMPACT_PROMPT, timeout=int(CONFIG["compact_timeout_sec"]))
        except subprocess.TimeoutExpired:
            stop_process(proc)
            raise
        finally:
            with COMPACT_LOCK:
                COMPACT_PROCS.pop(tag, None)
        out = json.loads(stdout)
        if out.get("is_error") or not out.get("session_id"):
            raise RuntimeError((stderr or stdout).strip()[-300:])
        (run_dir / "session.json").write_text(json.dumps(
            {"compacted": out["session_id"], "cost_usd": out.get("total_cost_usd"),
             "model_usage": token_usage(out), "at": now_iso()}))
    except Exception as exc:  # chats fall back to starting from the report
        log.warning("compacting review session %s failed: %s", session, exc)


def compacted_review_session(pr: dict, run_dir: Path, data: dict) -> str | None:
    """The run's compacted review session, if a chat can start from it."""
    if not CONFIG.get("compact_after_review", True):
        return None
    if (data.get("meta") or {}).get("head_sha") != pr.get("head_sha"):
        return None  # the PR moved on: the review's memory of the code is stale
    try:
        return json.loads((run_dir / "session.json").read_text()).get("compacted")
    except (OSError, json.JSONDecodeError):
        return None


# Errors that mean the resumed session itself is unusable. Anything else (rate limits, API errors)
# would fail a fresh session too, so it isn't retried.
SESSION_UNUSABLE_RE = re.compile(r"No conversation found|prompt is too long|context.{0,20}(limit|window)", re.I)


def run_chat_claude(wt: Path, prompt: str, session: str | None, fork: bool = False) -> dict | None:
    cmd = [CONFIG["claude"]["bin"], "-p", "--setting-sources", "user", "--output-format", "json",
           "--permission-mode", "default",
           "--allowedTools", "Bash", "Read", "Grep", "Glob",
           "--disallowedTools", *denied_tools()]
    if session:
        cmd += ["--resume", session]
    if session and fork:
        cmd += ["--fork-session"]  # leave the review's own session untouched
    model = CONFIG["claude"].get("chat_model") or CONFIG["claude"].get("model")
    if model:  # unset: your own Claude Code default model
        cmd += ["--model", model]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=wt, env=child_env(),
                              timeout=CONFIG["chat_timeout_sec"])
    except subprocess.TimeoutExpired:
        if session and fork:
            return None  # starting from the compacted review is optional: caller starts from the report
        raise
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out = None
    if not out or out.get("is_error") or not out.get("result"):
        detail = f"{proc.stderr}\n{proc.stdout}\n{(out or {}).get('result') or ''}"
        if session and (fork or SESSION_UNUSABLE_RE.search(detail)):
            return None  # fork failed, or the session is gone or too big: caller starts a fresh one
        raise RuntimeError((proc.stderr or proc.stdout or "no output").strip()[-600:])
    return out


def split_proposals(text: str, data: dict, decisions: list[dict] | None = None) -> tuple[str, list[dict]]:
    m = re.search(r"<<<PROPOSALS\s*\n(.*?)\n\s*PROPOSALS>>>", text, re.S)
    if not m:
        return text.strip(), []
    reply = (text[:m.start()] + text[m.end():]).strip()
    try:
        raw = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", m.group(1).strip()))
    except json.JSONDecodeError:
        return reply, []
    by_id = {x["id"]: x for x in data.get("findings", [])}
    applied = {d["id"]: d for d in decisions or []}
    out = []
    for p in raw if isinstance(raw, list) else []:
        action = p.get("action")
        reason = " ".join(str(p.get("reason") or "").split())[:300]
        if action in PR_ACTIONS:
            out.append({"action": action, "reason": reason})
        elif action in FINDING_ACTIONS and p.get("finding_id") in by_id:
            if action == "severity" and p.get("to") not in SEVERITIES:
                continue
            x = by_id[p["finding_id"]]
            d = applied.get(x.get("decision_id"))
            if d and d["action"] == action and (action != "severity" or d["to"] == p.get("to")):
                continue  # already applied; don't offer it again
            out.append({"action": action, "finding_id": x["id"], "title": x.get("title"), "from": x["severity"],
                        "to": p.get("to") if action == "severity" else None, "reason": reason})
    return reply, out


def chat_turn(key: str, run_id: str, message: str) -> dict:
    message = message.strip()[:8000]
    if not message:
        raise ValueError("empty message")
    pr = dict(STORE.prs[key])
    run_dir = run_record(key, run_id)
    data = json.loads((run_dir / "findings.json").read_text())
    lock = chat_lock(key)
    if not lock.acquire(blocking=False):
        raise RuntimeError("the agent is still answering your previous message")
    try:
        with busy(f"chat:{key}"):
            _chat_turn(key, run_id, message, pr, run_dir, data)
    finally:
        lock.release()
    return chat_payload(key)


def _chat_turn(key: str, run_id: str, message: str, pr: dict, run_dir: Path, data: dict) -> None:
    chat = load_chat(key)
    wt = PRS_DIR / key
    if not wt.exists():
        wt = prepare_worktree(pr)
    mine = [m for m in chat["messages"] if m.get("run_id") == run_id]
    last_agent = next((m["at"] for m in reversed(mine) if m["role"] == "agent"), "")
    fresh = [d for d in load_decisions(key) if d["at"] > last_agent] if last_agent else []
    note = ("[Since your last reply the reviewer applied:\n" + "\n".join(map(decision_line, fresh)) + "]\n\n"
            if fresh else "")
    session = chat["sessions"].get(run_id)
    try:
        out = run_chat_claude(wt, note + message, session) if session else None
    except Exception:
        with DECISION_LOCK:  # drop the session: the next message rebuilds it from the review and a recap
            chat = load_chat(key)
            chat["sessions"].pop(run_id, None)
            save_chat(key, chat)
        raise
    if out is None:
        recap = "\n".join(f"{'Reviewer' if m['role'] == 'you' else 'You'}: {m['text'][:400]}" for m in mine[-8:])
        prompt = build_chat_prompt(pr, key, run_id, data)
        if recap:
            prompt += f"\n\n# Earlier in this discussion\n{recap}"
        prompt += f"\n\n# The reviewer's message\n{message}"
        wait_for_compaction(key, run_id)
        review_session = compacted_review_session(pr, run_dir, data)
        out = run_chat_claude(wt, prompt, review_session, fork=True) if review_session else None
        if out is None:
            out = run_chat_claude(wt, prompt, None)
    with DECISION_LOCK:  # re-read: decisions may have been applied while the agent was thinking
        data = json.loads((run_dir / "findings.json").read_text())
        reply, proposals = split_proposals(out["result"], data, load_decisions(key))
        chat = load_chat(key)
        chat["sessions"][run_id] = out.get("session_id")
        chat["messages"] += [
            {"role": "you", "text": message, "at": now_iso(), "run_id": run_id},
            {"role": "agent", "text": reply, "at": now_iso(), "run_id": run_id, "proposals": proposals,
             "cost_usd": out.get("total_cost_usd")},
        ]
        save_chat(key, chat)


# -------------------------------------------------------- KB management (UI)

KB_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def kb_status(kb: dict) -> dict:
    root = xpath(kb.get("path", ""))
    services = sorted(set(kb_services_available(kb).values())) if root.is_dir() else []
    with STORE.lock:
        prs = [p for p in STORE.prs.values() if p.get("active")]
    attached = [p["key"] for p in prs if any(m["id"] == kb["id"] for m in match_kbs(p))]
    return {"exists": root.is_dir(), "resolved_path": str(root), "services": services,
            "context_files": len(kb_context_files(kb)) if root.is_dir() else 0, "active_prs": attached,
            "refresh": kb_refresh_state(kb)}


def inspect_kb_path(path: str) -> dict:
    """Suggest settings for a folder the user wants to add as a KB (inside your home folder only)."""
    root = xpath(path)
    if not root.resolve().is_relative_to(Path.home().resolve()) or not root.is_dir():
        return {"exists": False, "resolved_path": str(root)}
    md = [f for f in root.rglob("*.md") if ".cache" not in f.parts and "node_modules" not in f.parts]
    registries = [f.name for f in root.iterdir() if f.is_file() and (
        f.name in ("services.yaml", "services.yml", "registry.yaml", "registry.yml", "services.json", "registry.json"))]
    service_dir = None
    for cand in ("kb", "services", "docs/services"):
        d = root / cand
        if d.is_dir() and sum(1 for x in d.iterdir() if x.is_dir()) >= 2:
            service_dir = f"{cand}/{{service}}"
            break
    ctx = [n for n in ("README.md", "architecture.md", "_index.md") if (root / n).is_file()]
    if (root / "kb").is_dir() and any((root / "kb").glob("*.md")):
        ctx.append("kb/*.md")
    if (root / "docs").is_dir():
        ctx.append("docs/**/*.md")
    reg = {"file": registries[0], "name_field": "name", "repo_field": "repo"} if registries else None
    services = kb_services_available({"path": str(root), "registry": reg, "service_dir": service_dir}) if reg else {}
    return {"exists": True, "resolved_path": str(root), "markdown_files": len(md),
            "services": sorted(set(services.values())),
            "suggested_name": root.name.replace("_", " ").replace("-", " ").strip().title(),
            "suggested": {"registry": {"file": registries[0], "name_field": "name", "repo_field": "repo"}
                          if registries else None,
                          "service_dir": service_dir, "context_files": ctx}}


# Folder picker for "Add knowledge base". Browsers can't hand a local page a folder's real path,
# so the (localhost-only) server lists folders itself. Read-only, and limited to your home folder.
BROWSE_FILE_TYPES = {".md": "markdown", ".markdown": "markdown", ".yaml": "yaml", ".yml": "yaml", ".json": "json"}
BROWSE_LIMIT = 400


def tilde(path: Path) -> str:
    home = Path.home()
    return "~" if path == home else f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)


IN_DOCKER = Path("/.dockerenv").exists()
DOCKER_KB_ROOT = Path.home() / "kb"  # where docker-compose.override.yml (./setup.sh kb) mounts KB folders


def browse_dir(path: str, show_hidden: bool = False) -> dict:
    home = Path.home().resolve()
    if IN_DOCKER and (path or "~") == "~" and DOCKER_KB_ROOT.is_dir():
        path = str(DOCKER_KB_ROOT)  # in Docker, the mounted knowledge bases are the only useful place
    target = xpath(path or "~").resolve()
    if not target.is_relative_to(home) or not target.is_dir():
        target = home
    dirs, files, other = [], [], 0
    try:
        entries = sorted(target.iterdir(), key=lambda e: e.name.lower())
    except PermissionError:
        entries = []
    for e in entries:
        if e.name.startswith(".") and not show_hidden:
            continue
        try:
            if e.is_dir():
                dirs.append({"name": e.name, "path": tilde(e)})
            elif e.suffix.lower() in BROWSE_FILE_TYPES:
                files.append({"name": e.name, "type": BROWSE_FILE_TYPES[e.suffix.lower()]})
            else:
                other += 1
        except OSError:
            continue
    names = {e.name for e in entries}
    looks_like_kb = bool({"registry.yaml", "registry.yml", "services.yaml", "services.yml", "registry.json",
                          "services.json"} & names) or any(d["name"] in ("services", "kb") for d in dirs)
    return {"path": tilde(target), "parent": tilde(target.parent) if target != home else None,
            "dirs": dirs[:BROWSE_LIMIT], "files": files[:BROWSE_LIMIT], "other_files": other,
            "truncated": len(dirs) > BROWSE_LIMIT or len(files) > BROWSE_LIMIT, "looks_like_kb": looks_like_kb,
            "docker": IN_DOCKER}


KB_SKILL = ROOT / "skills" / "prism-kb-builder" / "SKILL.md"


def kb_guide() -> dict:
    """Read-only material for the "How to build a knowledge base" page."""
    skill_dir = Path.home() / ".claude" / "skills" / "prism-kb-builder"
    return {
        "guide": (ROOT / "docs" / "knowledge-bases.md").read_text(),
        "skill": KB_SKILL.read_text() if KB_SKILL.exists() else "",
        "skill_installed": (skill_dir / "SKILL.md").exists(),
        # Built from wherever PRism is installed, shown with ~ for your home folder.
        "install_command": f"mkdir -p {tilde(skill_dir)} && cp {tilde(KB_SKILL)} {tilde(skill_dir)}/",
        "example_path": tilde(ROOT / "examples" / "knowledge-base"),
    }


def validate_kbs(kbs: list) -> list[dict]:
    if not isinstance(kbs, list):
        raise ValueError("knowledge_bases must be a list")
    seen, clean = set(), []
    for kb in kbs:
        kid = str(kb.get("id", "")).strip().lower()
        if not KB_ID_RE.match(kid) or kid in seen:
            raise ValueError(f"bad or duplicate id: {kid!r} (lowercase letters, digits, - or _)")
        if not str(kb.get("path", "")).strip():
            raise ValueError(f"{kid}: path is required")
        seen.add(kid)
        attach = [{"repo": a["repo"].strip(), **({"service": a["service"].strip()} if a.get("service") else {})}
                  for a in kb.get("attach") or [] if str(a.get("repo", "")).strip()]
        reg = kb.get("registry")
        clean.append({
            "id": kid, "name": str(kb.get("name") or kid).strip(), "path": str(kb["path"]).strip(),
            "service_dir": (kb.get("service_dir") or None),
            "context_files": [c.strip() for c in kb.get("context_files") or [] if c.strip()],
            "registry": reg if reg and reg.get("file") else None,
            "attach": attach,
            "monorepos": [m.strip() for m in kb.get("monorepos") or [] if m.strip()],
            "refresh_command": kb.get("refresh_command") or None,
            "refresh_max_age_hours": float(kb.get("refresh_max_age_hours", 24)),
        })
    return clean


def rematch_all() -> None:
    with STORE.lock:
        for pr in STORE.prs.values():
            pr["kb_matches"] = [{"id": m["id"], "name": m["name"], "services": m["services"]} for m in match_kbs(pr)]
        STORE.save()


# ---------------------------------------------------------------------------- HTTP


RUN_ID_RE = re.compile(r"^\d{8}-\d{6}$")


def list_runs(key: str) -> list[dict]:
    base = REPORTS / key
    if not base.exists():
        return []
    runs = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir() or not RUN_ID_RE.match(d.name):
            continue
        fj = d / "findings.json"
        entry = {"run_id": d.name}
        if fj.exists():
            try:
                data = json.loads(fj.read_text())
                entry.update(data.get("meta", {}))
                entry["counts"] = data.get("counts")
                entry["verdict"] = data.get("verdict")
            except json.JSONDecodeError:
                pass
        elif (d / "error.txt").exists():
            entry["error"] = (d / "error.txt").read_text()[:300]
        runs.append(entry)
    return runs


class Handler(BaseHTTPRequestHandler):
    server_version = "PRism/1.0"

    def log_message(self, fmt, *args):  # route to our logger, quietly
        log.debug("http " + fmt, *args)

    def _json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, rel: str):
        path = (STATIC / rel).resolve()
        if not path.is_relative_to(STATIC.resolve()) or not path.is_file():
            path = STATIC / "index.html"
        ctype = {".html": "text/html", ".js": "application/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml"}.get(path.suffix, "application/octet-stream")
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local_host(self) -> bool:
        """Refuse requests addressed to any other host name (blocks DNS-rebinding reads)."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("localhost", "127.0.0.1", "::1")

    def do_GET(self):
        if not self._local_host():
            return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        path = unquote(urlparse(self.path).path)
        parts = [p for p in path.split("/") if p]
        if parts == ["downloads", "prism-kb-builder", "SKILL.md"] and KB_SKILL.exists():
            body = KB_SKILL.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="SKILL.md"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parts[:1] != ["api"]:
            return self._static("/".join(parts) or "index.html")
        if parts == ["api", "data", "summary"]:
            return self._json(data_summary())
        if parts == ["api", "state"]:
            with STORE.lock:
                prs = [{k: v for k, v in p.items() if k != "files"} for p in STORE.prs.values()]
                meta = dict(STORE.meta)
            meta["queue_size"] = JOBS.qsize()
            meta["running"] = list(RUNNING_PROCS)
            meta["busy_jobs"] = len(BUSY)  # reply checks, chats, KB refreshes, saves: ./restart.sh waits for them
            meta["poll_interval_sec"] = CONFIG["poll_interval_sec"]
            meta["ticket_url_template"] = ticket_url_template()
            meta["org"] = ", ".join(CONFIG["github"].get("orgs") or []) or None
            meta["auto_review"] = bool(CONFIG.get("auto_review", True))
            meta["my_prs"] = {"enabled": bool((CONFIG.get("my_prs") or {}).get("enabled", True)),
                              "auto_review": my_prs_auto()}
            meta["kbs"] = [{"id": k["id"], "name": k.get("name", k["id"])} for k in CONFIG.get("knowledge_bases", [])]
            meta["reviewer_mode"] = CONFIG["reviewer"]["mode"]
            meta["ui_version"] = int(max(f.stat().st_mtime for f in STATIC.iterdir() if f.is_file()))
            return self._json({"prs": prs, "meta": meta})
        if parts == ["api", "knowledge-bases"]:
            with STORE.lock:
                repos = sorted({p["repo"] for p in STORE.prs.values()})
            kbs = [{**kb, "status": kb_status(kb)} for kb in CONFIG.get("knowledge_bases", [])]
            return self._json({"knowledge_bases": kbs, "known_repos": repos})
        if parts == ["api", "knowledge-bases", "guide"]:
            return self._json(kb_guide())
        if parts == ["api", "learning"]:
            cands = sorted(load_candidates().values(), key=lambda c: c.get("created_at", ""), reverse=True)
            counts = {}
            for name, path in lesson_files().items():
                lines = path.read_text().splitlines() if path.exists() else []
                counts[name] = sum(1 for ln in lines if ln.strip() and not ln.lstrip().startswith("#"))
            return self._json({"candidates": cands, "active_counts": counts, "cap": LESSON_CAP,
                               "promote_after_prs": PROMOTE_AFTER_PRS})
        if len(parts) >= 3 and parts[1] == "prs":
            key = parts[2]
            pr = STORE.prs.get(key)
            if not pr:
                return self._json({"error": "unknown PR"}, HTTPStatus.NOT_FOUND)
            if len(parts) == 3:
                return self._json({"pr": pr, "runs": list_runs(key)})
            if parts[3:] == ["chat"]:
                return self._json(chat_payload(key))
            if len(parts) == 5 and parts[3] == "runs":
                run_dir = REPORTS / key / parts[4]
                if parts[4] == "latest":
                    # Latest *finished* run, so an in-flight run never blanks the report.
                    done = [r for r in list_runs(key) if "finished_at" in r or "error" in r]
                    if not done:
                        return self._json({"error": "no runs"}, HTTPStatus.NOT_FOUND)
                    run_dir = REPORTS / key / done[0]["run_id"]
                if not run_dir.resolve().is_relative_to(REPORTS.resolve()) or not run_dir.exists():
                    return self._json({"error": "unknown run"}, HTTPStatus.NOT_FOUND)
                def read(n):
                    return (run_dir / n).read_text() if (run_dir / n).exists() else None
                findings = read("findings.json")
                return self._json({
                    "run_id": run_dir.name,
                    "findings": json.loads(findings) if findings else None,
                    "report": read("report.md"),
                    "log": (read("run.log") or "")[-60000:],
                    "error": read("error.txt"),
                    "posted": json.loads(read("posted.json") or "{}"),
                    "responses": load_responses(key),
                })
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _same_origin(self) -> bool:
        """Block cross-site requests: only this page may trigger actions (especially posting)."""
        origin = self.headers.get("Origin")
        if origin:
            o = urlparse(origin)
            if o.hostname not in ("localhost", "127.0.0.1", "::1") or o.port != self.server.server_address[1]:
                return False
        return (self.headers.get("Content-Type") or "").startswith("application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_POST(self):
        parts = [p for p in unquote(urlparse(self.path).path).split("/") if p]
        if not self._local_host() or not self._same_origin():
            return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
        if parts == ["api", "poll"]:
            POLL_NOW.set()
            return self._json({"ok": True})
        if parts == ["api", "data", "clear"]:
            body = self._body()
            if body.get("confirmed_by_user") is not True:
                return self._json({"error": "confirmation required"}, HTTPStatus.BAD_REQUEST)
            try:
                return self._json(clear_local_data(str(body.get("confirm") or "")))
            except ValueError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except RuntimeError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
        if parts == ["api", "settings", "auto-review"]:
            on = self._body().get("auto_review")
            if not isinstance(on, bool):
                return self._json({"error": "auto_review must be true or false"}, HTTPStatus.BAD_REQUEST)
            try:
                save_config_key("auto_review", on)
            except ValueError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            POLL_NOW.set()  # queue waiting PRs right away when turned on
            log.info("auto-review of PRs you're asked to review: %s", on)
            return self._json({"ok": True, "auto_review": on})
        if parts == ["api", "settings", "my-prs"]:
            body = self._body()
            cur = dict(CONFIG.get("my_prs") or {"enabled": True, "auto_review": False})
            for k in ("enabled", "auto_review"):
                if isinstance(body.get(k), bool):
                    cur[k] = body[k]
            try:
                save_config_key("my_prs", cur)
            except ValueError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            POLL_NOW.set()  # list (or auto-review) your PRs right away
            log.info("My PRs settings: %s", cur)
            return self._json({"ok": True, "my_prs": cur})
        if parts == ["api", "settings", "sync"]:
            try:
                return self._json(set_sync_interval(self._body().get("minutes")))
            except ValueError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parts == ["api", "fs", "browse"]:
            body = self._body()
            return self._json(browse_dir(str(body.get("path") or "~"), bool(body.get("show_hidden"))))
        if len(parts) == 4 and parts[:2] == ["api", "knowledge-bases"] and parts[3] == "refresh":
            try:
                return self._json(start_kb_refresh(parts[2]))
            except ValueError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        if parts == ["api", "knowledge-bases", "inspect"]:
            return self._json(inspect_kb_path(self._body().get("path", "")))
        if parts == ["api", "knowledge-bases", "save"]:
            try:
                kbs = validate_kbs(self._body().get("knowledge_bases"))
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            try:
                save_config_key("knowledge_bases", kbs)
            except ValueError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            rematch_all()
            return self._json({"ok": True, "knowledge_bases": kbs})
        if len(parts) == 4 and parts[:2] == ["api", "learning"]:
            cid, action = parts[2], parts[3]
            if cid not in load_candidates():
                return self._json({"error": "unknown lesson"}, HTTPStatus.NOT_FOUND)
            if action == "remove":
                return self._json(remove_lesson(cid))
            if action == "promote":
                return self._json(manual_promote(cid))
        if len(parts) == 4 and parts[:2] == ["api", "prs"]:
            key, action = parts[2], parts[3]
            if key not in STORE.prs:
                return self._json({"error": "unknown PR"}, HTTPStatus.NOT_FOUND)
            if action == "review":
                queued = enqueue(key, "manual re-review")
                return self._json({"ok": True, "queued": queued})
            if action == "check-responses":
                return self._json({"ok": True, "queued": enqueue_response_check(key, force=True)})
            if action == "seen":
                STORE.update_pr(key, seen_run=STORE.prs[key].get("last_run"))
                return self._json({"ok": True})
            if action == "draft-comments":
                body = self._body()
                try:
                    drafts = draft_comments(key, body["run_id"], body.get("finding_ids") or [])
                except Exception as exc:
                    log.exception("drafting failed")
                    return self._json({"error": str(exc)[:500]}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return self._json({"drafts": drafts})
            if action == "approve-check":  # POST: it calls GitHub, so only this page may trigger it
                try:
                    return self._json(approve_check(key))
                except Exception as exc:
                    log.exception("approve check failed")
                    return self._json({"can": False, "reason": f"couldn't check: {str(exc)[:200]}"})
            if action == "approve":
                body = self._body()
                if body.get("confirmed_by_user") is not True:
                    return self._json({"error": "approving needs explicit confirmation"}, HTTPStatus.BAD_REQUEST)
                try:
                    return self._json(approve_pr(key, str(body.get("confirm") or ""), str(body.get("body") or ""),
                                                 body.get("expected_sha")))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except StaleApproval as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
                except Exception as exc:
                    log.exception("approve failed")
                    return self._json({"error": str(exc)[:600]}, HTTPStatus.BAD_GATEWAY)
            if action == "summary-draft":
                try:
                    return self._json(summary_draft(key, str(self._body().get("run_id") or "")))
                except (ValueError, OSError) as exc:
                    return self._json({"error": str(exc)[:300]}, HTTPStatus.BAD_REQUEST)
            if action == "post-summary":
                body = self._body()
                if body.get("confirmed_by_user") is not True:
                    return self._json({"error": "posting needs explicit confirmation"}, HTTPStatus.BAD_REQUEST)
                try:
                    with busy(busy_tag(f"post:{key}")):
                        result = post_summary(key, str(body.get("run_id") or ""), body.get("body"),
                                              list(body.get("finding_ids") or []))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except ClearingNow as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
                except Exception as exc:
                    log.exception("posting the summary failed")
                    return self._json({"error": str(exc)[:600]}, HTTPStatus.BAD_GATEWAY)
                return self._json({"ok": True, **result})
            if action == "post-comments":
                body = self._body()
                if body.get("confirmed_by_user") is not True:
                    return self._json({"error": "posting needs explicit confirmation"}, HTTPStatus.BAD_REQUEST)
                try:
                    with busy(busy_tag(f"post:{key}")):
                        result = post_comments(key, body["run_id"], body.get("comments") or [])
                except ClearingNow as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
                except Exception as exc:
                    log.exception("posting failed")
                    return self._json({"error": str(exc)[:600]}, HTTPStatus.BAD_GATEWAY)
                return self._json({"ok": True, **result})
            if action == "chat":
                body = self._body()
                try:
                    return self._json(chat_turn(key, body["run_id"], str(body.get("message") or "")))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except ClearingNow as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
                except Exception as exc:
                    log.exception("chat turn failed for %s", key)
                    return self._json({"error": str(exc)[:600]}, HTTPStatus.INTERNAL_SERVER_ERROR)
            if action == "decisions":
                body = self._body()
                try:
                    with busy(busy_tag(f"decision:{key}")):
                        add_decision(key, body["run_id"], body)
                except (ValueError, KeyError) as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except ClearingNow as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
                return self._json(chat_payload(key))
            if action == "proposal":
                try:
                    with busy(busy_tag(f"proposal:{key}")):
                        resolve_proposal(key, self._body())
                except ValueError as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                except ClearingNow as exc:
                    return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
                return self._json(chat_payload(key))
            if action == "cancel":
                proc = RUNNING_PROCS.get(key)
                compacting = any(k == key for k, _ in list(COMPACTING))
                if proc:
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                if compacting:
                    threading.Thread(target=stop_compactions, args=(key,), daemon=True).start()
                return self._json({"ok": True, "killed": bool(proc) or compacting})
        if len(parts) == 6 and parts[:2] == ["api", "prs"] and parts[3] == "decisions" and parts[5] == "undo":
            key = parts[2]
            if key not in STORE.prs:
                return self._json({"error": "unknown PR"}, HTTPStatus.NOT_FOUND)
            try:
                with busy(busy_tag(f"undo:{key}")):
                    undo_decision(key, parts[4])
            except ValueError as exc:
                return self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except ClearingNow as exc:
                return self._json({"error": str(exc)}, HTTPStatus.CONFLICT)
            return self._json(chat_payload(key))
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    for d in (DATA, REPORTS, REPOS_DIR, PRS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    DRAIN_FILE.unlink(missing_ok=True)
    for tool, what in ((CONFIG["claude"]["bin"],
                        "Claude Code (https://claude.com/claude-code), then run `claude` once to log in"),
                       ("gh", "the GitHub CLI (https://cli.github.com), then run `gh auth login`"),
                       ("git", "git")):
        if not shutil.which(tool):
            log.error("%s not found on PATH. Install %s. Under launchd, re-run ./install-autostart.sh afterwards.",
                      tool, what)
    tools = ticket_read_tools()
    if tools:
        log.info("Ticket/doc tools reviews may use (read-only): %s", ", ".join(tools))
    else:
        log.info("No Jira/Confluence tools configured: reviews use the PR title and description as the spec.")
    if unlisted_user_mcp_tools():
        log.info("Blocked in reviews (pre-approved in ~/.claude/settings.json but not in tickets.read_tools): %s",
                 ", ".join(unlisted_user_mcp_tools()))
    for _ in range(max(1, int(CONFIG.get("max_concurrent_reviews", 2)))):
        threading.Thread(target=worker_loop, daemon=True).start()
    threading.Thread(target=poller_loop, daemon=True).start()
    threading.Thread(target=cleanup_inactive, daemon=True).start()
    threading.Thread(target=response_worker, daemon=True).start()
    port = int(DOTENV.get("PR_REVIEW_PORT") or os.environ.get("PR_REVIEW_PORT") or CONFIG.get("port", 6161))
    # 127.0.0.1 natively. The Docker image sets PRISM_BIND=0.0.0.0 so the published port works; compose
    # publishes it on the host's 127.0.0.1 only, and the Host check still refuses other host names.
    bind = os.environ.get("PRISM_BIND") or "127.0.0.1"
    httpd = ThreadingHTTPServer((bind, port), Handler)
    log.info("PRism service on http://localhost:%d", port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for proc in all_procs():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    main()

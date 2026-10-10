#!/usr/bin/env python3
"""PRism setup helper: get the GitHub, Claude, Jira and Confluence credentials into .env.

For each service it opens the right page in your browser (pre-filled where the service allows),
you approve or generate the token there, paste it once into a hidden prompt, and this script checks
it works before saving it to .env (gitignored, owner-only permissions). Tokens are never printed.

    ./setup.sh               walk through everything
    ./setup.sh github        one part: github | claude | atlassian | kb
    ./setup.sh --check       test what's already in .env, change nothing

Standard library only, like the service.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
CONFIG_FILE = ROOT / "config.json"
GITHUB_API = "https://api.github.com"
ATLASSIAN_TOKENS_PAGE = "https://id.atlassian.com/manage-profile/security/api-tokens"

BOLD, DIM, GREEN, YELLOW, RED, RESET = ("\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m") \
    if sys.stdout.isatty() else ("",) * 6


# ------------------------------------------------------------------ small helpers

def say(msg: str = "") -> None:
    print(msg)


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}!{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


def heading(n: str, title: str) -> None:
    print(f"\n{BOLD}{n}. {title}{RESET}")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or default


def yes(prompt: str, default: bool = True) -> bool:
    val = ask(f"{prompt} ({'Y/n' if default else 'y/N'})").lower()
    return default if not val else val.startswith("y")


def pick_one(title: str, options: list[str], default: int = 0) -> int:
    """Numbered list; Enter keeps the default. Returns the chosen index."""
    say(f"  {title}")
    for i, o in enumerate(options, 1):
        say(f"    {i}) {o}{'  (default)' if i - 1 == default else ''}")
    while True:
        val = ask("Choose a number", str(default + 1))
        if val.isdigit() and 1 <= int(val) <= len(options):
            return int(val) - 1
        warn(f"Type a number from 1 to {len(options)}.")


def pick_many(title: str, options: list[str], selected: set[int]) -> list[int]:
    """Checklist: type numbers to select or deselect them, Enter when done."""
    chosen = set(selected)
    while True:
        say(f"  {title}")
        for i, o in enumerate(options, 1):
            say(f"    [{'x' if i - 1 in chosen else ' '}] {i}) {o}")
        val = ask("Numbers to select/deselect (e.g. 1 3), a = all, n = none, Enter = done").lower()
        if not val:
            return sorted(chosen)
        if val == "a":
            chosen = set(range(len(options)))
        elif val == "n":
            chosen = set()
        else:
            for part in val.replace(",", " ").split():
                if part.isdigit() and 1 <= int(part) <= len(options):
                    chosen ^= {int(part) - 1}


def secret(prompt: str) -> str:
    try:
        return getpass.getpass(f"  {prompt} (hidden, paste and press Enter): ").strip()
    except EOFError:
        return ""


def tail(token: str) -> str:
    return f"…{token[-4:]}" if len(token) > 8 else "(set)"


def open_url(url: str) -> None:
    say(f"  {DIM}Opening: {url}{RESET}")
    try:
        if not webbrowser.open(url):
            raise webbrowser.Error
    except webbrowser.Error:
        say("  Couldn't open a browser here. Open the link above yourself.")


def tls_context() -> ssl.SSLContext:
    """Python from python.org or pyenv on macOS often ships without CA certificates; fall back to the system's."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca"):
        return ctx
    for cafile in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                   "/opt/homebrew/etc/ca-certificates/cert.pem", "/usr/local/etc/ca-certificates/cert.pem"):
        if os.path.exists(cafile):
            ctx.load_verify_locations(cafile)
            return ctx
    try:
        import certifi  # type: ignore
        ctx.load_verify_locations(certifi.where())
    except ImportError:
        pass
    return ctx


TLS = tls_context()


def http_json(url: str, headers: dict, timeout: int = 15) -> tuple[int, dict | list | None]:
    req = urllib.request.Request(url, headers={"User-Agent": "prism-setup", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=TLS) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return 0, {"message": str(e)}


# ------------------------------------------------------------------ .env and config.json

def read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip("'\"")
    return out


def write_env(updates: dict[str, str]) -> None:
    """Set keys in .env in place, keeping comments and order; owner-only permissions."""
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    done = set()
    for i, raw in enumerate(lines):
        key = raw.split("=", 1)[0].strip()
        if "=" in raw and not raw.lstrip().startswith("#") and key in updates:
            lines[i] = f"{key}={updates[key]}"
            done.add(key)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in done]
    fd, tmp = tempfile.mkstemp(dir=ROOT, prefix=".env.")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ENV_FILE)


class ConfigUnreadable(Exception):
    pass


def read_config(strict: bool = False) -> dict:
    """config.json, or {} if it's missing. With strict, a file that exists but isn't valid JSON raises
    instead of reading as {} (so nothing writes a near-empty config over it)."""
    try:
        return json.loads(CONFIG_FILE.read_text())
    except OSError:
        return {}
    except ValueError as exc:
        if strict:
            raise ConfigUnreadable(f"config.json isn't valid JSON ({exc}); fix it, then run setup again") from exc
        return {}


def save_config(cfg: dict) -> None:
    """Serialize first (so a bad value can't leave a half-written file), then write in place. In place, not
    rename: Docker bind-mounts config.json as a single file, and a new file would be invisible to a running
    container. It also keeps the file's permissions."""
    text = json.dumps(cfg, indent=2) + "\n"
    CONFIG_FILE.write_text(text)


def ensure_files() -> None:
    for name, example in ((CONFIG_FILE, ROOT / "config.example.json"), (ENV_FILE, ROOT / ".env.example")):
        if name.is_dir():
            sys.exit(f"{name.name} is a folder (Docker created it). Remove it and run setup again: rmdir {name.name}")
        if not name.exists() and example.exists():
            shutil.copy(example, name)
            ok(f"Created {name.name} from {example.name}")
    os.chmod(ENV_FILE, 0o600)
    (ROOT / "data").mkdir(exist_ok=True)  # so Docker doesn't create it as root on Linux
    ignored = ENV_FILE.name in (ROOT / ".gitignore").read_text().split() if (ROOT / ".gitignore").exists() else False
    if (ROOT / ".git").exists() and shutil.which("git"):
        ignored = subprocess.run(["git", "check-ignore", "-q", str(ENV_FILE)], cwd=ROOT).returncode == 0
    if ignored:
        ok(".env is gitignored, so your tokens are never committed")
    else:
        warn(".env is NOT gitignored. Add '.env' to .gitignore before committing anything.")


# ------------------------------------------------------------------ GitHub

def github_token_page(owner: str, write: bool) -> str:
    params = {
        "name": "PRism" if write else "PRism (read-only)",
        "description": ("PRism local PR review: reads PRs, diffs and linked issues, and posts the review "
                        "comments you approve in its UI. Can't push code or merge." if write else
                        "PRism local PR review: reads PRs, diffs, review threads and linked issues. Read-only."),
        "expires_in": "90",
        "contents": "read", "metadata": "read", "issues": "read",
        "pull_requests": "write" if write else "read",
    }
    if owner:
        params["target_name"] = owner
    return "https://github.com/settings/personal-access-tokens/new?" + urllib.parse.urlencode(params)


def check_github(token: str, label: str = "GITHUB_TOKEN") -> bool:
    status, me = http_json(f"{GITHUB_API}/user", {"Authorization": f"Bearer {token}",
                                                 "Accept": "application/vnd.github+json"})
    if status != 200 or not isinstance(me, dict):
        fail(f"{label} {tail(token)} was rejected by GitHub ({status}): {(me or {}).get('message', 'no response')}")
        return False
    ok(f"{label} {tail(token)} works: signed in as {me.get('login')}")
    if not token.startswith("github_pat_"):
        warn("This isn't a fine-grained token (github_pat_…). Classic and OAuth tokens can usually push "
             "and merge too. A fine-grained one limits PRism to what it needs.")
    q = urllib.parse.quote("is:pr is:open review-requested:@me")
    status, found = http_json(f"{GITHUB_API}/search/issues?q={q}&per_page=1",
                              {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    if status == 200 and isinstance(found, dict):
        n = found.get("total_count", 0)
        (ok if n else warn)(f"It can see {n} open PR(s) requesting your review"
                            + ("" if n else " (fine if none are waiting; otherwise check the token's "
                                            "repository access)"))
        if not n:
            explain_repo_access(token, me.get("login", ""))
    return True


def explain_repo_access(token: str, login: str) -> None:
    """0 PRs is usually a token that can't see the org's repos. Say which owners it does reach."""
    status, repos = http_json(f"{GITHUB_API}/user/repos?per_page=100", {"Authorization": f"Bearer {token}",
                                                                        "Accept": "application/vnd.github+json"})
    if status != 200 or not isinstance(repos, list):
        return
    owners = sorted({r["full_name"].split("/")[0] for r in repos})
    say(f"    It can reach {len(repos)} repo(s), owned by: {', '.join(owners) or 'nobody'}.")
    if not owners or owners == [login]:
        warn("This token only reaches your personal repos. Create it with Resource owner = your organization "
             "(./setup.sh github, and pick your organization). If your org approves tokens, it stays pending until an "
             "admin approves it.")


def choose_owner(configured: list[str]) -> str:
    """Pick the token's owner from your organizations (found through an existing gh login), or type one."""
    found = []
    if shutil.which("gh"):
        out = subprocess.run(["gh", "api", "user/orgs", "--paginate", "--jq", ".[].login"],
                             capture_output=True, text=True)
        found = out.stdout.split() if out.returncode == 0 else []
    names = list(dict.fromkeys(configured + found))
    if not names:
        return ask("GitHub organization that owns the repos you review (blank = only your personal repos)")
    options = names + ["None: only my personal repos", "Another organization (type it)"]
    i = pick_one("Which organization's PRs will you review?", options, 0)
    if i < len(names):
        return names[i]
    return "" if i == len(names) else ask("Organization name")


def setup_github() -> None:
    heading("1", "GitHub (required)")
    env = read_env()
    current = env.get("GITHUB_TOKEN", "")
    if current and check_github(current) and not yes("Replace it?", default=False):
        return
    cfg = read_config()
    orgs = ((cfg.get("github") or {}).get("orgs") or [])
    if shutil.which("gh") and subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0:
        say("  1 = create a token for PRism in the browser (recommended: limited to what PRism needs)")
        say("  2 = reuse your existing `gh` login token (quick, but it can usually do anything you can, e.g. push)")
        if ask("Choose", "1") == "2":
            token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()
            if token and check_github(token):
                write_env({"GITHUB_TOKEN": token})
                ok("Saved your gh login token as GITHUB_TOKEN")
                return
            warn("Couldn't read a token from `gh auth token`. Using the browser instead.")
    write = yes("Will you post comments or approve PRs from PRism's UI? "
                "(the token then also gets 'Pull requests: write')")
    say(f"  {BOLD}A fine-grained token sees the repos of ONE owner{RESET}: pick the organization whose PRs you review.")
    owner = choose_owner(orgs)
    say("\n  In the page that opens: name, owner, expiry and permissions are filled in"
        + (" (read code, read + write pull requests; it can't push or merge)." if write else " (read-only)."))
    say(f"  {BOLD}You choose Repository access{RESET} (All repositories, or the ones you review),")
    say(f"  then click {BOLD}Generate token{RESET} and copy it.")
    say(f"  {DIM}If your org requires approval for tokens, an admin approves it before it can read org repos.{RESET}")
    open_url(github_token_page(owner, write=write))
    while True:
        token = secret("GitHub token")
        if not token:
            warn("Skipped. Natively PRism falls back to your `gh auth login`; Docker needs this token.")
            return
        if check_github(token):
            write_env({"GITHUB_TOKEN": token})
            ok("Saved GITHUB_TOKEN to .env")
            if owner and owner not in orgs and yes(f"Limit PR search to the {owner} organization?"):
                try:
                    cfg = read_config(strict=True)
                    cfg.setdefault("github", {})["orgs"] = list(dict.fromkeys(
                        ((cfg.get("github") or {}).get("orgs") or []) + [owner]))
                    save_config(cfg)
                except ConfigUnreadable as exc:
                    warn(str(exc))
                    return
                ok(f"Added {owner} to github.orgs in config.json")
            return
        if not yes("Try again?"):
            return


# ------------------------------------------------------------------ Claude

def capture_setup_token() -> list[str]:
    """Run `claude setup-token` in this terminal (it opens the browser) and return the possible tokens
    from its output, most likely first (see setup_token_candidates)."""
    if not shutil.which("claude"):
        return []
    d = tempfile.mkdtemp(prefix="prism-setup-")
    os.chmod(d, 0o700)
    log = Path(d) / "out"
    script = shutil.which("script")
    if script and sys.platform == "darwin":
        cmd = [script, "-q", str(log), "claude", "setup-token"]
    elif script:
        cmd = [script, "-q", "-c", "claude setup-token", str(log)]
    else:
        cmd = ["claude", "setup-token"]
    try:
        subprocess.run(cmd)
        text = log.read_text(errors="ignore") if log.exists() else ""
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return setup_token_candidates(text)


def setup_token_candidates(text: str) -> list[str]:
    """Possible tokens in `claude setup-token` output, longest first. A long token wraps onto the next
    lines, so lines made only of token characters are joined on; but a one-word line after it (say
    "Success") looks the same, so every shorter join is offered too and setup tests them in order."""
    lines = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\r", "", text).split("\n")
    for i, line in enumerate(lines):
        m = re.search(r"sk-ant-oat[0-9]{2}-[A-Za-z0-9_\-]+", line)
        if not m:
            continue
        joins = [m.group(0)]
        for nxt in lines[i + 1:]:
            if not re.fullmatch(r"[A-Za-z0-9_\-]+", nxt.strip()):
                break  # e.g. "Store this token securely…"
            joins.append(joins[-1] + nxt.strip())
        return joins[::-1]
    return []


def parse_setup_token(text: str) -> str:
    """The most likely token (see setup_token_candidates)."""
    found = setup_token_candidates(text)
    return found[0] if found else ""


def claude_token_works(token: str) -> tuple[bool, str]:
    """Log in with the token alone (empty home folder, no other credentials) and ask for one word."""
    if not shutil.which("claude"):
        return True, "not checked (Claude Code isn't on this machine)"
    home = tempfile.mkdtemp(prefix="prism-claude-check-")
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_"))}
        env.update(HOME=home, CLAUDE_CODE_OAUTH_TOKEN=token)
        r = subprocess.run(["claude", "-p", "Reply with just: ok", "--model", "haiku"], env=env, cwd=home,
                           capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return r.returncode == 0, (r.stdout or r.stderr).strip()[:160]


def setup_claude(mode: str) -> None:
    heading("2", "Claude (required)")
    if mode == "native":
        if not shutil.which("claude"):
            fail("Claude Code isn't installed. Install it from https://claude.com/claude-code, "
                 "run `claude` once to log in.")
            return
        say("  Natively PRism uses your Claude Code login (your subscription). Checking it…")
        r = subprocess.run(["claude", "-p", "--model", "haiku", "Reply with just: ok"], capture_output=True, text=True,
                           timeout=120, env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"})
        if r.returncode == 0 and r.stdout.strip():
            ok("Claude Code is logged in and answering")
        else:
            fail("`claude -p` didn't answer. Run `claude` once and log in, then run setup again.")
        return
    env = read_env()
    if env.get("CLAUDE_CODE_OAUTH_TOKEN") and not yes(
            f"A Claude token {tail(env['CLAUDE_CODE_OAUTH_TOKEN'])} is set. Replace it?", False):
        return
    say("  Docker needs a long-lived token tied to your Claude subscription (`claude setup-token`).")
    token = ""
    if shutil.which("claude") and yes("Run `claude setup-token` now? It opens your browser to approve"):
        candidates = capture_setup_token()
        say("  Testing the token (one tiny request)…" if candidates else "  Couldn't read the token from the output.")
        for cand in candidates[:4]:
            if claude_token_works(cand)[0]:
                write_env({"CLAUDE_CODE_OAUTH_TOKEN": cand})
                ok(f"Got the token from `claude setup-token` {tail(cand)}; it works. "
                   "Saved CLAUDE_CODE_OAUTH_TOKEN to .env")
                return
        if candidates:
            warn("The token read from the output didn't work. Copy it from the output above and paste it here.")
    elif not shutil.which("claude"):
        say("  Claude Code isn't on this machine. Run `claude setup-token` wherever it is installed, then paste here.")
    if not token:
        token = secret("Claude token (starts with sk-ant-oat)")
    if not token:
        warn("Skipped. Reviews in Docker fail until CLAUDE_CODE_OAUTH_TOKEN is set "
             "(or ANTHROPIC_API_KEY, billed per token).")
        return
    if not token.startswith("sk-ant-oat"):
        warn("That doesn't look like a `claude setup-token` token (sk-ant-oat…).")
    say("  Testing the token (one tiny request)…")
    works, detail = claude_token_works(token)
    if not works:
        fail(f"Claude rejected token {tail(token)}: {detail}")
        if not yes("Save it anyway?", default=False):
            return
    else:
        ok(f"Claude token {tail(token)} works")
    write_env({"CLAUDE_CODE_OAUTH_TOKEN": token})
    ok("Saved CLAUDE_CODE_OAUTH_TOKEN to .env")


# ------------------------------------------------------------------ Jira / Confluence

def basic(email: str, token: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode(),
            "Accept": "application/json"}


def check_jira(url: str, email: str, token: str) -> bool:
    status, me = http_json(f"{url}/rest/api/3/myself", basic(email, token))
    if status == 200 and isinstance(me, dict):
        ok(f"Jira works: {me.get('displayName') or email} on {url}")
        return True
    fail(f"Couldn't reach Jira: {(me or {}).get('message')}" if status == 0 else
         f"Jira rejected it ({status}). Check the site, the email and the token.")
    return False


def check_confluence(url: str, email: str, token: str) -> bool:
    status, me = http_json(f"{url}/rest/api/user/current", basic(email, token))
    if status == 200 and isinstance(me, dict) and me.get("type") != "anonymous":
        ok(f"Confluence works: {me.get('displayName') or email} on {url}")
        return True
    fail(f"Couldn't reach Confluence: {(me or {}).get('message')}" if status == 0 else
         f"Confluence rejected it ({status}). Check the site, the email and the token.")
    return False


def setup_atlassian(mode: str) -> None:
    heading("3", "Jira and Confluence (optional)")
    if mode == "native":
        say("  Natively, reviews use your own Claude Code connection to Jira/Confluence (for example the")
        say("  claude.ai Atlassian connector), listed in config.json → tickets.read_tools. See docs/setup.md.")
        return
    say("  Reviews work without these; they then use the PR description as the spec.")
    use_jira = yes("Read Jira tickets that PRs link to?", default=bool(read_env().get("JIRA_URL")))
    use_conf = yes("Read Confluence pages that PRs link to?", default=bool(read_env().get("CONFLUENCE_URL")))
    if not (use_jira or use_conf):
        write_env({k: "" for k in ("JIRA_URL", "JIRA_USERNAME", "JIRA_API_TOKEN",
                                   "CONFLUENCE_URL", "CONFLUENCE_USERNAME", "CONFLUENCE_API_TOKEN")})
        ok("Jira and Confluence left off")
        return
    env = read_env()
    site = ask("Your Atlassian site (e.g. your-company or your-company.atlassian.net)",
               re.sub(r"^https://|/wiki$|/$", "", env.get("JIRA_URL") or env.get("CONFLUENCE_URL") or ""))
    site = site.removeprefix("https://").removeprefix("http://").rstrip("/").removesuffix("/wiki")
    if "." not in site:
        site += ".atlassian.net"
    base = f"https://{site}"
    email = ask("Your Atlassian account email", env.get("JIRA_USERNAME") or env.get("CONFLUENCE_USERNAME") or "")
    say(f"\n  In the page that opens, click {BOLD}Create API token{RESET} (the plain one, not 'with scopes'),")
    say("  name it PRism, pick an expiry, and copy it. It reads what your account can read.")
    open_url(ATLASSIAN_TOKENS_PAGE)
    while True:
        token = secret("Atlassian API token")
        if not token:
            warn("Skipped Jira/Confluence.")
            return
        good_j = check_jira(base, email, token) if use_jira else True
        good_c = check_confluence(f"{base}/wiki", email, token) if use_conf else True
        if good_j and good_c:
            break
        if not yes("Try again?"):
            return
    updates = {"JIRA_URL": "", "JIRA_USERNAME": "", "JIRA_API_TOKEN": "",
               "CONFLUENCE_URL": "", "CONFLUENCE_USERNAME": "", "CONFLUENCE_API_TOKEN": ""}
    if use_jira:
        updates.update(JIRA_URL=base, JIRA_USERNAME=email, JIRA_API_TOKEN=token)
    if use_conf:
        updates.update(CONFLUENCE_URL=f"{base}/wiki", CONFLUENCE_USERNAME=email, CONFLUENCE_API_TOKEN=token)
    write_env(updates)
    ok("Saved " + " and ".join(x for x, on in (("Jira", use_jira), ("Confluence", use_conf)) if on)
       + " settings to .env")


# ------------------------------------------------------------------ check only

def check_all() -> int:
    env = read_env()
    say(f"{BOLD}Checking .env{RESET} (nothing is changed)")
    bad = 0
    heading("1", "GitHub")
    if env.get("GITHUB_TOKEN"):
        bad += not check_github(env["GITHUB_TOKEN"])
    else:
        warn("GITHUB_TOKEN not set (fine natively with `gh auth login`; required in Docker)")
    heading("2", "Claude")
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        works, detail = claude_token_works(env["CLAUDE_CODE_OAUTH_TOKEN"])
        if works:
            ok(f"CLAUDE_CODE_OAUTH_TOKEN {tail(env['CLAUDE_CODE_OAUTH_TOKEN'])} works ({detail or 'ok'})")
        else:
            fail(f"CLAUDE_CODE_OAUTH_TOKEN {tail(env['CLAUDE_CODE_OAUTH_TOKEN'])} was rejected: {detail}. "
                 "Run ./setup.sh claude")
            bad += 1
    elif env.get("ANTHROPIC_API_KEY"):
        ok("ANTHROPIC_API_KEY is set (billed per token)")
    else:
        warn("No Claude token in .env (fine natively with your Claude Code login; required in Docker)")
    heading("3", "Jira and Confluence")
    j = [env.get(k, "") for k in ("JIRA_URL", "JIRA_USERNAME", "JIRA_API_TOKEN")]
    c = [env.get(k, "") for k in ("CONFLUENCE_URL", "CONFLUENCE_USERNAME", "CONFLUENCE_API_TOKEN")]
    if all(j):
        bad += not check_jira(*j)
    elif any(j):
        fail("Jira is half configured: JIRA_URL, JIRA_USERNAME and JIRA_API_TOKEN are all needed")
        bad += 1
    else:
        say("  Jira: off")
    if all(c):
        bad += not check_confluence(*c)
    elif any(c):
        fail("Confluence is half configured: CONFLUENCE_URL, CONFLUENCE_USERNAME and CONFLUENCE_API_TOKEN "
             "are all needed")
        bad += 1
    else:
        say("  Confluence: off")
    say("")
    return 1 if bad else 0


# ------------------------------------------------------------------ main

# ------------------------------------------------------------------ Knowledge bases (Docker)

OVERRIDE_FILE = ROOT / "docker-compose.override.yml"
KB_MARKERS = ("registry.yaml", "registry.yml", "services.yaml", "services.yml", "registry.json", "services.json")
SKIP_DIRS = {"Library", "Applications", "Movies", "Music", "Pictures", "node_modules", "venv", ".venv", "site-packages",
             "dist", "build", "target", "Downloads"}


def find_kb_folders(max_depth: int = 3, budget_sec: float = 6.0) -> list[Path]:
    """Folders under your home that look like a knowledge base (a registry file plus kb/ or services/)."""
    home, start, found = Path.home(), time.time(), []
    for dirpath, dirnames, filenames in os.walk(home):
        depth = len(Path(dirpath).relative_to(home).parts)
        if set(filenames) & set(KB_MARKERS) and set(dirnames) & {"kb", "services"}:  # check before pruning
            found.append(Path(dirpath))
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS] if depth < max_depth else []
        if time.time() - start > budget_sec:
            break
    return sorted(found)


def current_kb_mounts() -> dict[str, str]:
    """host path -> container path, from the override file this script writes."""
    if not OVERRIDE_FILE.exists():
        return {}
    mounts, source = {}, None
    for line in OVERRIDE_FILE.read_text().splitlines():
        key, _, val = line.strip().partition(": ")
        if key in ("source", "target") and val.startswith('"'):
            try:
                val = json.loads(val).replace("$$", "$")
            except ValueError:
                continue
            if key == "source":
                source = val
            elif source:
                mounts[source], source = val, None
    return mounts


def yaml_str(value: str) -> str:
    """A path as a YAML string compose reads literally: JSON quoting (valid YAML), with $ doubled
    so compose doesn't expand it as a variable."""
    return json.dumps(str(value).replace("$", "$$"))


OVERRIDE_MARKER = "# Written by ./setup.sh kb"


def override_is_ours() -> bool:
    return not OVERRIDE_FILE.exists() or OVERRIDE_FILE.read_text().startswith(OVERRIDE_MARKER)


def setup_kbs(mode: str) -> None:
    heading("4", "Knowledge bases (optional)")
    if mode != "docker":
        say("  Natively PRism reads folders straight from your disk: add them in the UI under")
        say("  Knowledge bases → Add → Browse….")
        return
    if not override_is_ours():
        warn("docker-compose.override.yml exists and wasn't written by this script, so it's left alone. Add your")
        warn("knowledge-base folders to it by hand under services: prism: volumes: (see docker-compose.yml).")
        return
    say("  The container only sees folders you mount into it. Pick your knowledge-base folders;")
    say("  they're mounted read-only under /home/prism/kb/ and appear in the UI's Browse….")
    mounted = current_kb_mounts()
    say(f"  {DIM}Looking for knowledge-base folders in your home folder…{RESET}")
    if sys.platform == "darwin":
        say(f"  {DIM}(macOS may ask to let Terminal access folders like Documents; \"Don't Allow\" is fine.){RESET}")
    cands = list(dict.fromkeys([Path(p) for p in mounted] + find_kb_folders()))
    def tilde(p):
        return "~/" + str(p.relative_to(Path.home())) if p.is_relative_to(Path.home()) else str(p)
    chosen = []
    if cands:
        idx = pick_many("Knowledge-base folders to mount:", [tilde(p) for p in cands],
                        {i for i, p in enumerate(cands) if str(p) in mounted})
        chosen = [cands[i] for i in idx]
    else:
        say("  None found automatically (a KB folder has registry.yaml or services.yaml "
            "plus a kb/ or services/ folder).")
    while yes("Add another folder by path?", default=False):
        p = Path(os.path.expanduser(ask("Folder path"))).resolve()
        if p.is_dir():
            chosen.append(p)
        else:
            warn(f"{p} isn't a folder.")
    if not chosen:
        if OVERRIDE_FILE.exists() and mounted:
            OVERRIDE_FILE.unlink()
            ok("Removed the knowledge-base mounts")
        else:
            say("  No knowledge bases mounted. Run ./setup.sh kb any time.")
        return
    used, lines, shown = set(), [], []
    for p in chosen:
        base = re.sub(r"[^A-Za-z0-9._-]", "-", p.name) or "kb"
        name, n = base, 2
        while name in used:
            name, n = f"{base}-{n}", n + 1
        used.add(name)
        # Long volume syntax: no "src:dst" splitting, so colons, spaces and quotes in paths are safe.
        lines += ["      - type: bind", f"        source: {yaml_str(p)}",
                  f"        target: {yaml_str('/home/prism/kb/' + name)}", "        read_only: true"]
        shown.append(f"{p}  →  /home/prism/kb/{name}")
    OVERRIDE_FILE.write_text(f"{OVERRIDE_MARKER}: your knowledge-base folders, mounted read-only. "
                             "Personal (gitignored).\n"
                             "services:\n  prism:\n    volumes:\n" + "\n".join(lines) + "\n")
    ok(f"Mounted {len(chosen)} folder(s) in docker-compose.override.yml")
    for line in shown:
        say(f"    {DIM}{line}{RESET}")
    say("  Apply with: docker compose up -d   then in the UI: Knowledge bases → Add → Browse… → kb/")


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    if "--check" in args:
        return check_all()
    say(f"{BOLD}PRism setup{RESET}: GitHub, Claude, Jira and Confluence credentials → .env")
    ensure_files()
    mode = "docker" if os.environ.get("PRISM_MODE", "").lower() == "docker" else ""
    if not mode:
        choice = ask("How will you run PRism? 1 = Docker (recommended), 2 = natively on this machine", "1")
        mode = "native" if choice.strip() == "2" else "docker"
    parts = ([a for a in args if a in ("github", "claude", "atlassian", "jira", "confluence", "kb")]
             or ["github", "claude", "atlassian", "kb"])
    if "github" in parts:
        setup_github()
    if "claude" in parts:
        setup_claude(mode)
    if any(p in parts for p in ("atlassian", "jira", "confluence")):
        setup_atlassian(mode)
    if "kb" in parts:
        setup_kbs(mode)
    say(f"\n{BOLD}Done.{RESET} Re-test any time with ./setup.sh --check")
    say("Next: " + ("docker compose up -d --build   (then open http://localhost:6161)" if mode == "docker"
                    else "./start.sh   (or ./restart.sh if PRism is already running)"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Nothing after the last ✓ was saved.")
        sys.exit(130)

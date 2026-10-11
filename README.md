# PRism

A local service that keeps your GitHub review queue pre-reviewed. It runs on your machine at **http://localhost:6161**. Every few minutes it:

1. finds the open, non-draft PRs that request **your** review;
2. checks each one out into a throwaway git worktree;
3. reviews it with [Claude Code](https://docs.claude.com/en/docs/claude-code) in headless mode:
   - a **code pass**: lint, a secret scan, and bugs, reliability, security and tests;
   - a **consistency pass** against the linked ticket and docs, if any;
4. adds a **knowledge-base pass** for repos you've attached docs to: flow context, and regressions against documented contracts;
5. shows the reports in a Jira-style UI, with findings grouped Critical, High, Medium and Low.

Medium and Low findings only count when they're on lines the PR changes, and Medium means "should fix before merge". Each report opens with **What needs fixing** (cause, why it matters and fix, one line each) and **Flow context**.

**Nothing is posted to GitHub unless you explicitly confirm it.** See [Safety](#safety).

---

## Contents

- [Setup guide: GitHub, Claude, Jira, Confluence](docs/setup.md) (Docker or native)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Access and tokens](#access-and-tokens)
- [Configuration](#configuration-configjson)
- [Knowledge bases](#knowledge-bases) (full guide: [docs/knowledge-bases.md](docs/knowledge-bases.md))
- [Tickets and docs through MCP](#tickets-and-docs-through-mcp) (Jira, Confluence, Google Drive, Linear, Notion…)
- [Reviewer modes](#reviewer-modes)
- [Using the UI](#using-the-ui)
- [My PRs](#my-prs)
- [Posting comments (opt-in)](#posting-comments-opt-in)
- [Replies to your comments and learned lessons](#replies-to-your-comments-and-learned-lessons)
- [Asking the review agent about findings](#asking-the-review-agent-about-findings)
- [Running it: start, stop, auto-start](#running-it)
- [Safety](#safety)
- [Troubleshooting](#troubleshooting)
- [Layout](#layout)
- [License](#license)

---

## Requirements

| What | Why | Check |
| --- | --- | --- |
| macOS or Linux (native), or any OS with Docker | Auto-start uses launchd (macOS); with Docker, compose restarts it. On Linux, run `./start.sh` or write a systemd unit that runs `run.sh`. Windows isn't supported directly; use WSL. | |
| **Python 3.9+** (macOS's built-in `python3` works) | The service. It uses only the standard library; there's nothing to `pip install`. | `python3 --version` |
| **git** | Partial clones and worktrees. | `git --version` |
| **GitHub CLI (`gh`)**, logged in | Finds PRs, reads diffs and threads, and posts only when you confirm. | `gh auth status` |
| **SSH access to GitHub** (default) or HTTPS | Cloning. See `github.clone_url`. | `ssh -T git@github.com` |
| **Claude Code CLI (`claude`)**, logged in | Runs the reviews headlessly. | `claude --version`, then `claude -p "say ok"` |
| *Optional:* MCP servers | Reading tickets and docs the PR links to. | [below](#tickets-and-docs-through-mcp) |
| *Optional:* knowledge-base folders | Flow context and regression checks. | [below](#knowledge-bases) |
| *Optional:* your repos' lint tools | The code pass runs whatever the repo uses (`make lint`, `ruff`, `eslint`, …) if it's installed. | |

---

## Quick start

PRism needs four things: **GitHub** and **Claude** (required), **Jira** and **Confluence** (optional;
reviews work without them and use the PR description as the spec). Git host: **GitHub (github.com)
only** for now.
**[docs/setup.md](docs/setup.md)** walks through each one, with exactly the access it needs.

**Docker (recommended).** Everything is installed in the image, and reviews can't see the rest of your machine.

```sh
git clone <this repo> prism && cd prism
./setup.sh                           # opens each token page in your browser, checks and saves to .env
docker compose up -d --build         # then open http://localhost:6161
```

`./setup.sh` creates `config.json` and `.env` (gitignored, owner-only), opens GitHub's token page with
permissions pre-filled, runs `claude setup-token`, checks your Jira/Confluence token, and (Docker) mounts your knowledge-base
folders read-only. It tests each token before saving. `./setup.sh --check` re-tests them later.

**Native (macOS / Linux).** Uses the `gh` and Claude Code logins you already have.

```sh
git clone <this repo> prism && cd prism
cp config.example.json config.json   # your settings (gitignored)
cp .env.example .env                 # your keys (gitignored), may stay empty
$EDITOR config.json                  # optional: github.orgs to limit the search to your org(s)
./start.sh                           # starts in the background and opens http://localhost:6161
./install-autostart.sh               # optional (macOS): start at login, restart on crash
```

The first sync runs straight away. Reviews start automatically, `max_concurrent_reviews` at a time. Each review takes a few minutes and costs roughly $1–2 of Claude usage, depending on the PR's size and your model.

---

## Access and tokens

All keys live in **one file, `.env`**. Every value in it is passed to the `gh`, `git` and `claude` processes the service runs.

| Key | Needed? | What it's for | Minimum permissions |
| --- | --- | --- | --- |
| `GITHUB_TOKEN` | No. Empty means your `gh auth login` session is used. | Searching PRs, reading PR metadata, diffs and review threads, and posting comments or approving PRs when you confirm it in the UI. | Fine-grained token for your org(s): **Contents: Read**, **Metadata: Read**, **Issues: Read**, **Pull requests: Read and write** (just *Read* if you won't post or approve from PRism; the **Approve…** button then stays hidden). It can't push or merge. `./setup.sh` opens this page pre-filled. Classic token (not recommended: it can push and merge): `repo`, `read:org`. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Docker only | Your Claude subscription inside the container (`claude setup-token` on the host; `./setup.sh` captures it). | |
| `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`, `CONFLUENCE_URL`, `CONFLUENCE_USERNAME`, `CONFLUENCE_API_TOKEN` | Docker only, optional | The bundled read-only Atlassian server. Natively, use your own MCP setup instead. See [docs/setup.md](docs/setup.md#4-jira-and-confluence-optional-an-atlassian-api-token). | An Atlassian API token (reads what you can read). |
| `ANTHROPIC_API_KEY` | No. Empty means your logged-in Claude Code account (subscription) is used. Set it only if you want per-token API billing. | The `claude -p` reviews and chats. | |
| `PR_REVIEW_PORT` | No | Port override (default `6161`). | |

Cloning uses `github.clone_url` (default `git@github.com:{repo}.git`, i.e. SSH). For HTTPS with a token, set it to `https://github.com/{repo}.git` and make sure `gh auth setup-git` has run.

Claude Code needs to be logged in as the same OS user the service runs as, because launchd runs it as you. Check with `claude -p "say ok"`.

---

## Configuration (`config.json`)

`config.json` is yours and gitignored. Start from `config.example.json`. Anything you leave out falls back to the defaults in `server.py`. `config.local.json`, if present, is merged on top.

```jsonc
{
  "port": 6161,
  "github": {
    "search_query": "is:pr is:open draft:false review-requested:@me",  // any GitHub search
    "orgs": ["my-org"],                        // adds org:my-org (several are OR'd); [] = all
    "include_my_reviews": true,                // also keep open PRs you reviewed/commented on
    "my_reviews_window_days": 30,              // ...if updated within this many days
    "clone_url": "git@github.com:{repo}.git"
  },
  "poll_interval_sec": 300,                    // or pick "Every N min" next to Sync now
  "auto_review": true,                         // review PRs you're asked to review automatically (switch in "To review")
  "my_prs": {"enabled": true, "auto_review": false}, // "My PRs": list your own PRs; review on request (or auto)
  "auto_rereview_on_new_commits": true,        // re-review only when the head SHA changes
  "max_concurrent_reviews": 3,
  "review_timeout_sec": 2700,
  "response_check_timeout_sec": 900,           // one check of replies to your comments
  "claude": { "bin": "claude", "model": null, "draft_model": "sonnet", "chat_model": null },
  "chat_timeout_sec": 600,                     // one "Ask the agent" reply
  "compact_after_review": true,                // keep a compacted copy of each review's session for "Ask the agent"
  "compact_timeout_sec": 180,                  // one compaction; runs in the background after the review
  "reviewer": {
    "mode": "bundled",                         // or "user" (see Reviewer modes)
    "skill_path": "~/.claude/commands/pr-review.md",
    "lessons_files": {
      "code": "data/lessons/code-reviewer.lessons.md",
      "consistency": "data/lessons/consistency-reviewer.lessons.md"
    }
  },
  "tickets": {
    "key_pattern": "[A-Z][A-Z0-9]+-[0-9]+",    // how ticket keys look in PR titles/branches
    "url_template": "https://my-org.atlassian.net/browse/{key}",   // or null
    "read_tools": [],                          // MCP tools reviews MAY use (read-only)
    "deny_tools": []                           // MCP tools reviews must NEVER use (writes)
  },
  "knowledge_bases": []                        // see Knowledge bases; easiest to manage in the UI
}
```

Restart after editing by hand (`./restart.sh`). Changes made in the UI apply immediately.

---

## Knowledge bases

A **knowledge base (KB)** is any local folder of markdown describing your systems: service overviews, APIs, data models, deployments, architecture notes. Generated docs, hand-written docs and a docs repo checkout all work. When a PR's repo is attached to a KB, the review only **reads** it to:

- write the **flow context**: who calls this code, what it calls or writes downstream, and which part the PR changes;
- catch **regressions the diff alone can't show**: broken API/data contracts with documented consumers, env vars a deployment still sets, and documented behavior the change silently alters.

If you set a KB's `refresh_command`, PRism **runs that command** in the KB folder before reviews and on **Refresh now**, so only set it for KBs you trust. A **Refresh now** on a KB built by the skill writes inside that KB (its `kb/`, `registry.yaml`, `README.md` and `.cache/repos/`).

> **Building a KB from scratch?** Read **[docs/knowledge-bases.md](docs/knowledge-bases.md)**. It covers the folder layout, the registry file that maps services to repos automatically, what goes in each service's files, monorepos, and how to check it's picked up. A working example is in [`examples/knowledge-base/`](examples/knowledge-base/).

### Adding one (UI)

1. Go to **Manage knowledge bases** in the sidebar and click **Add knowledge base**.
2. Click **Browse…** and pick the folder. PRism reads it and fills in the name, service folder, registry and context files.
3. Attach repos, either on the card or from any PR page with **Attach a knowledge base…**.

No knowledge base yet? **How to build one** (next to **Add knowledge base**) has copy-paste steps and the `prism-kb-builder` Claude Code skill, which generates one from your repos.

### How a repo gets matched

Rules are checked in this order, and any hit attaches the KB:

1. **Explicit attachment:** `attach: [{"repo": "my-org/payments-api", "service": "payments-api"}]`. `repo` accepts globs (`my-org/*`, `my-org/payments-*`). `service` is optional and names the service folder to read. Without it, the reviewer gets the context files and greps the KB.
2. **Registry:** if the KB has a file listing services and their repos, every listed repo is attached automatically, but only for services whose folder exists.
3. **Monorepos:** for repos listed in `monorepos` (for example a deploy-manifests repo), a registered service is attached when the PR's changed paths or title name it.

### KB fields

| Field | Example | Meaning |
| --- | --- | --- |
| `id`, `name` | `payments`, `Payments platform` | `id` is lowercase and used in URLs. |
| `path` | `~/docs/payments-kb` | Absolute, `~/…`, or relative to this project. |
| `service_dir` | `kb/{service}` | Optional per-service folder. Every file in it is read for that service. |
| `context_files` | `["README.md", "kb/*.md", "docs/**/*.md"]` | Globs for system-level docs read for every attached PR (up to 40 files). |
| `registry` | `{"file": "services.yaml", "name_field": "name", "repo_field": "repo"}` | Optional. JSON (a list, or `{"services": [...]}`), or a simple YAML list (`- name: x` then `repo: owner/x`). |
| `attach` | `[{"repo": "my-org/api-*"}]` | Explicit attachments. |
| `monorepos` | `["my-org/deploy"]` | Repos matched by changed paths. |
| `refresh_command` | `["./bin/kb", "update", "{service}"]` | Optional. Runs in the KB folder before a review (at most every `refresh_max_age_hours`) and on **Refresh now**. `null` means the KB is read as-is. Only for KBs you trust. |
| `refresh_max_age_hours` | `24` | How often, at most, `refresh_command` runs before reviews (per service). `0` turns the pre-review refresh off. |

A minimal KB can be one folder with a `README.md`, attached to `my-org/*`.

---

## Tickets and docs through MCP

The review always **starts from the GitHub PR**. It only follows what the PR itself links to: a ticket key in the title, branch or description, and doc links in the description. It reads those with the MCP tools you allow, **read-only**. Without MCP, the PR title and description are the spec, and GitHub issues the PR links to are read with `gh issue view`.

Reviews run `claude -p` as you, so they see the **same MCP servers your `claude` sees**: servers you added with `claude mcp add` (user scope), and claude.ai connectors if you're logged in with a claude.ai account. MCP servers defined in the *reviewed repo* (`.mcp.json`, `.claude/settings.json`) are **not** loaded.

### 1. Connect the MCP server to Claude Code

Use one of these:
- **A claude.ai connector:** in claude.ai go to **Settings → Connectors**, add it (for example Atlassian, Google Drive, Notion or Linear) and **authorize** it. It then appears in `claude mcp list` as `claude.ai <Name>`.
- **A local or remote MCP server:**
  - remote: `claude mcp add --scope user --transport http <name> <url>`
  - local: `claude mcp add --scope user <name> -- <command> <args…>`

Check it with `claude mcp list`. It must show **✔ Connected**, not "Needs authentication". If it needs auth, run `claude` interactively, then `/mcp`, and authenticate.

### 2. Find the exact tool names

Tool names are `mcp__<server>__<tool>`. claude.ai connectors are named `mcp__claude_ai_<Name>__<tool>`. To list a server's tools:
- **Interactively:** run `claude`, then `/mcp`, pick the server, and view its tools.
- **Headlessly:** `echo "Use ToolSearch to find tools matching '+<Name>' and print their exact names" | claude -p --allowedTools ToolSearch`

### 3. Allow the reads and deny the writes in `config.json`

- Put read tools in `tickets.read_tools`: those are the only MCP tools reviews may call.
- Put every write tool you can find in `tickets.deny_tools`.

Tools in neither list can't be used anyway, because headless runs deny anything not allowed. `deny_tools` is a second lock.

**Jira + Confluence** (claude.ai Atlassian connector):

```json
"tickets": {
  "key_pattern": "[A-Z][A-Z0-9]+-[0-9]+",
  "url_template": "https://YOUR-SITE.atlassian.net/browse/{key}",
  "read_tools": [
    "mcp__claude_ai_Atlassian__getJiraIssue",
    "mcp__claude_ai_Atlassian__getJiraIssueRemoteIssueLinks",
    "mcp__claude_ai_Atlassian__getConfluencePage",
    "mcp__claude_ai_Atlassian__getAccessibleAtlassianResources"
  ],
  "deny_tools": [
    "mcp__claude_ai_Atlassian__addCommentToJiraIssue",
    "mcp__claude_ai_Atlassian__editJiraIssue",
    "mcp__claude_ai_Atlassian__transitionJiraIssue",
    "mcp__claude_ai_Atlassian__createJiraIssue",
    "mcp__claude_ai_Atlassian__createIssueLink",
    "mcp__claude_ai_Atlassian__addWorklogToJiraIssue",
    "mcp__claude_ai_Atlassian__createConfluencePage",
    "mcp__claude_ai_Atlassian__updateConfluencePage",
    "mcp__claude_ai_Atlassian__createConfluenceFooterComment",
    "mcp__claude_ai_Atlassian__createConfluenceInlineComment"
  ]
}
```

**Google Drive / Docs** (claude.ai Google Drive connector): authorize it first; until then only `…__authenticate` tools exist. Then list its tools (step 2), add the ones that **search, read or fetch** a file to `read_tools`, and add anything that creates, updates, shares or deletes to `deny_tools`. Links like `docs.google.com/document/d/…` in a PR description are then read as part of the spec.

**Other trackers** (Linear, Notion, GitHub Projects, a self-hosted MCP): follow the same three steps. Set `key_pattern` to the tracker's key format (e.g. `[A-Z]{2,5}-[0-9]+` for Linear) and `url_template` to its issue URL.

**No tracker:** leave `read_tools` empty. Reviews use the PR description and linked GitHub issues.

---

## Reviewer modes

| `reviewer.mode` | Uses | Lessons files | Good for |
| --- | --- | --- | --- |
| `bundled` (default) | `prompts/review_bundled.md` plus the two agents in `reviewers/`: `prr-code-reviewer` and `prr-consistency-reviewer` | `data/lessons/*.lessons.md` | Anyone cloning this repo; nothing else to install. |
| `user` | Your own Claude Code skill at `reviewer.skill_path`, and whatever agents it calls from `~/.claude/agents/` | Set `reviewer.lessons_files` to your agents' lessons files | People with an existing `/pr-review` skill. `prompts/review_user.md` tells the skill which of its phases to skip (fetch, ticket, threads, checkout, posting) and assumes a code-review agent and a consistency-review agent; adapt it to your skill's steps. |

Both modes share the same output format (`prompts/_output.md`), so the UI works with either. To change how reviews behave, edit `reviewers/*.md` (bundled) or your skill (user).

**Keeping reviews cheap.** Each finding is written once: the agents report only their own findings, the review's final message is just the findings JSON, and PRism builds the **Full report** tab from it instead of having the model write the report a second time. In bundled mode the consistency pass runs on Sonnet (`model: sonnet` in `reviewers/consistency-reviewer.md`); the code pass, which needs the strongest reasoning, uses `claude.model`. Add, change or remove a `model:` line in a reviewer's frontmatter to choose its model. The **Tokens** row under "This review" shows each model's input, output and cache tokens for the run, so you can see where the cost goes.

---

## Using the UI

### Your review status

GitHub stops listing a PR under `review-requested:@me` as soon as you review it, so the service also tracks open PRs **you reviewed or commented on** (updated within `my_reviews_window_days`, excluding your own PRs). Each PR gets a capsule:

| Capsule | Meaning | Row |
| --- | --- | --- |
| **To review** | You haven't reviewed it yet. | normal |
| **✓ Reviewed** | You reviewed it, and the author hasn't handed it back. A small hint shows activity since then (e.g. *since: new commits, 1 new reply*), but that alone doesn't change the status. | **green** |
| **● Ready again** | After your review, the author **re-requested your review** or **marked the PR ready for review**. Only these explicit signals count. | yellow edge |
| **Merged / Closed** | The PR left the open list. | faded |

**To review** in the sidebar means *To review + Ready again*. **Reviewed by you**, **Ready again** and **All open** filter by status, and the board groups PRs by it. The bot's own background review still re-runs whenever new commits land; that's independent of your status. PRs you'd already reviewed aren't auto-reviewed when they first appear, only when they change.

- **Sidebar views:**
  - To review, Needs attention (Critical/High), In review, Failed, No longer requested;
  - one view per knowledge base and per repo;
  - Learned lessons, and Manage knowledge bases.
- **List or Board**, sortable by severity, updated time, age or repo. A blue dot marks a report you haven't opened.
- **PR page:**
  - the verdict and summary;
  - **What needs fixing** and **Flow context**;
  - tabs: **Findings** (filter by severity or source; `file:line` links jump to the diff line), **Acceptance criteria**, **KB regression**, **Replies**, **Full report**, **Run log** (live), **History**.
- **Buttons:** **Review again** re-runs the review, **Check replies** re-checks threads, and **Sync now** polls GitHub.

---

## My PRs

**Auto-review switches.** At the top of **To review**, **Auto-review PRs I'm asked to review** is on by default. Turn it off and PRs are still listed, each with a **Review** button, and new commits don't trigger re-reviews. **My PRs** has its own switch, off by default.

**My PRs** in the sidebar lists your own open PRs, drafts included. They're **not** reviewed automatically: click **Review** on a row (or **Review this PR** on its page). Turn on **Auto-review my PRs** at the top of that view to review each one automatically and again after new commits. Reports, the agent chat and draft comments work as for any PR, so you can fix things before reviewers see them. (Approving your own PR isn't possible on GitHub, so there's no Approve button there.)

---

## Posting comments (opt-in)

Background reviews **never** post or approve. Only three actions write to GitHub, each after you confirm it: **Post to PR…** (type `comment`), **Post as one comment** (type `comment`) and **Approve…** (type `approve`). All three endpoints accept only same-origin JSON with `confirmed_by_user: true`. The per-finding flow:

1. Pick findings: **Post to PR…** on one, or tick several and use the bar at the bottom (or **Post all**).
2. The service drafts **short, human-sounding comments** (rules in `prompts/draft_comment.md`) and shows them in one dialog. Edit or untick any of them. Nothing is posted yet.
3. Click **Post N comments…**, type **`comment`**, then click **OK, post**.
4. They go up as **one GitHub review with event `COMMENT`** (never approve or request-changes). Comments are inline where the line is in the diff; otherwise they go in the review body.

**Post as one comment.** Instead of separate comments, this posts every open finding as **one** comment: a short overview, then the findings grouped Critical, High, Medium and Low, each with where it is, what's wrong, why it matters and how to fix it. It's built from the report (no extra model call), shown in an editable box, and posted only after you type **`comment`**, as one GitHub review with event `COMMENT` and no inline comments (it never approves or requests changes). Like **Post all**, findings you dismissed or already posted are left out; the ones it covers are then marked as posted. If they don't all fit GitHub's size limit, it says how many are left to post separately.

**Approving.** When your GitHub token is allowed to approve (and you didn't write the PR), the PR page shows **Approve…**. It warns if there are new commits since PRism's review or critical/high findings still open, takes an optional comment, and approves only after you type **`approve`**. PRism checks the permission without writing anything: classic and `gh` tokens list their scopes, and for fine-grained tokens it asks GitHub to delete a review that doesn't exist (403 = no permission, 404 = allowed).

**Sync interval and local data.** Next to **Sync now**, pick how often PRism checks GitHub (1–60 minutes; saved to `config.json`). **Clear local data** cancels any running reviews, then deletes PRism's reports, chats, decisions, state, clones and any lessons stored in `data/` after you type **`clear`**. Your `config.json`, `.env` and knowledge-base folders are kept, nothing on GitHub changes, and PRs still waiting for you are reviewed again on the next sync.

---

## Replies to your comments and learned lessons

Every sync reads the review threads **you** started, with a read-only GraphQL query. When a thread changes (a new reply, a resolve, or new commits), the reply is checked against the current code:

| Label | Meaning |
| --- | --- |
| **Resolved** | The code at HEAD fixes it. This is verified in the code, not taken from the reply. |
| **Expected · skip** | The author's reason holds up. |
| **Still open** | The reply doesn't address it, or it was only deferred. |
| **Awaiting reply** | No answer yet. |

Labels show on the findings, in "What needs fixing", in the **Replies** tab and as list chips. The next re-review gets them as input, so settled points aren't raised again.

When an *expected* reason is a **general rule**, it becomes a lesson candidate. On the **second PR** where it appears, it's inserted at the top of the matching lessons file, dated and tagged `(auto-learned from PR replies)`. Nothing is added once a file has 30 entries. **Learned lessons** lists them all, with **Remove** and **Add now**. Nothing here ever replies, resolves or posts.

---

## Asking the review agent about findings

The **Ask the agent** tab on a PR (or **Ask the agent about F3** on a finding) opens a **private chat between you and the review agent**. It's not a conversation with the PR author: they never see it, and nothing is posted to GitHub. Mention findings by ID (`F3`). The agent reads the checked-out code before it answers (read-only, same deny list as reviews).

- **It only proposes.** When you push back or ask for a change, the agent proposes a new severity, a dismissal, a confirmation ("keep"), or a PR-level context note. Each one has an **Apply** button. Nothing changes until you click it. Dismissed findings move to "Dismissed by you" and drop out of the counts, "What needs fixing" and "Post all". **Undo** puts the finding back.
- **Decisions survive re-reviews.** Applied decisions go to `data/reports/<pr>/decisions.json` as one-liners (about 30 tokens each), and the next review gets them in its prompt. When the reviewer raises the same issue again, the service re-applies your decision itself. If the code changed so your reason no longer holds, the finding comes back marked **Reopened**.
- **Chats stay out of future reviews.** The full transcript is kept in `data/reports/<pr>/chat.json` for you to reread, but it's never sent to a review. Within one report, replies resume the same `claude` session, so each message only sends what you typed. When a review finishes, PRism compacts a copy of its session (cheap then, while Claude's prompt cache is still warm) and the chat starts from that, so the agent remembers the code it read and why it ruled things out, whenever you open the chat. Compaction runs in the background after the review shows as done; if you open the chat before it finishes, the first reply waits for it (up to `compact_timeout_sec`). If the PR has new commits, or `compact_after_review` is `false`, the chat starts from the report alone. Compaction costs one short summary per review, logged as `cost_usd` in the run's `session.json`. Each chat reply's cost is in `chat.json`.
- **Re-review only when it helps.** Once the discussion wraps up, the agent says once whether running the review again is worth it. Usually it isn't, because decisions already apply. If it is, you get a **Re-run review** button.

It runs as your logged-in Claude Code account and default model, the same as reviews. Set `claude.chat_model` to override the model.

---

## Running it

```sh
./start.sh          # start (no-op if running) and open the UI; add --no-open to skip the browser
./status.sh         # running? plus the last log lines
./restart.sh        # safe restart: new reviews pause, running ones finish, then it restarts
./stop.sh           # stop; interrupted reviews are re-queued on the next start
tail -f service.log
```

### Auto-start at login (macOS, launchd)

```sh
./install-autostart.sh     # install or refresh ~/Library/LaunchAgents/local.pr-review.plist
./uninstall-autostart.sh   # remove it; ./start.sh still works by hand afterwards
```

- It starts at login and restarts on crash (`KeepAlive`).
- Once it's installed, the scripts above go through launchd. `./stop.sh` unloads the service until the next `./start.sh` or login. `kill` alone won't stop it, because launchd restarts it.
- launchd starts with a bare `PATH`, so the plist has one fixed `PATH` built from where `gh`, `claude`, `node` and your Python lived at install time. **Re-run `./install-autostart.sh`** after moving this folder or changing those tools (e.g. a new nvm Node version).
- Check it with `launchctl print gui/$(id -u)/local.pr-review | grep -E "state|pid"`.

---

## Safety

- **Background reviews are blocked from writing.** `claude -p` runs with an allow-list (`Bash`, `Read`, `Grep`, `Glob`, `Agent`, plus your `tickets.read_tools`) and a deny-list: `gh pr comment/review/edit/merge/close/create`, `gh api`, `gh issue` writes, `gh alias`, `bash -c`, `gh auth token`, `git push`, `curl`, `wget`, `kubectl`, `docker`, `gcloud`, `aws`, file edits, and your `tickets.deny_tools`. The deny-list matches command prefixes, so it's a strong guard rail rather than a sandbox. **What the token itself allows still matters.** The default fine-grained token has no *Contents: write*, so it can't push code or merge, whatever command runs. It does have *Pull requests: write* (so the Post button works), and reviews hold it too: a review that slips past the deny-list (for example with a `python3 -c` script), or the PR's own lint code (see Trust boundary), could comment on, approve, close or edit PRs. **In Docker**, a *Pull requests: Read* token (if you never post or approve from PRism) means reviews can't write to GitHub at all. **Natively**, reviews also run as you, with your `gh` keyring login and SSH keys, so the deny-list is the only guard; use Docker if that matters.
- **Posting and approving need your typed confirmation** (`comment` / `approve`) in the UI and an explicit flag on the server. Nothing else writes to GitHub (the Approve permission check is a no-op request, see Approving).
- **Reviewed repos can't configure Claude.** Reviews use `--setting-sources user`, so a PR's own `.claude/settings.json` (hooks, permissions), `.mcp.json` and `CLAUDE.md` are ignored.
- **Trust boundary:** the code pass runs the repo's own lint tooling (`make lint`, `npm run lint`, …) in the worktree, and that executes code from the PR. Use this on repos whose contributors you trust (your org's PRs), not on arbitrary forks.
- **Local only:** the server binds to `127.0.0.1`, answers only requests addressed to `localhost`/`127.0.0.1` (which blocks DNS-rebinding pages), and POST endpoints reject cross-site requests. The knowledge-base folder picker can only see inside your home folder.
- **Reviews only read knowledge bases.** Secrets are never resolved and no service is started. The exceptions are ones you set up: a KB's `refresh_command` runs before reviews, and **Refresh now** on a skill-built KB lets Claude write inside that KB only (no shell, no MCP servers, no network tools).

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Sync failed" in the top bar | Run `gh auth status`. Try the search in the GitHub UI with your `search_query`. |
| No PRs show up | Run `gh search prs --review-requested=@me --state=open`. If that lists PRs, check `github.orgs` and `github.search_query` in `config.json`. |
| Port 6161 already in use | Set `PR_REVIEW_PORT=<other port>` in `.env`, or find the other process with `lsof -i :6161`. |
| `claude` / `gh` / `git` not found | Install it and log in (`claude`, `gh auth login`); the startup log names what's missing. Under launchd, re-run `./install-autostart.sh` afterwards. |
| Reviews fail at "Checking out PR" | Test `ssh -T git@github.com`, or switch `github.clone_url` to HTTPS. |
| Reviews fail immediately | Run `claude -p "say ok"` as the same user. Under launchd, re-run `./install-autostart.sh` to refresh `PATH`. |
| Ticket not fetched | Check that `claude mcp list` shows the server as Connected, and that the tool names in `read_tools` match exactly. |
| KB not used for a repo | Check **Manage knowledge bases**: the folder must be found, and the repo must be attached, in the registry, or a listed monorepo. |
| **Approve…** button missing | Your token lacks *Pull requests: write* (or the `repo` scope for a classic token), or it's your own PR, or the PR isn't open. |
| **Refresh now** disabled or failing | Hover it for the reason: the KB is mounted read-only in Docker, its `refresh_command` tool isn't installed, or the source repos can't be cloned with `github.clone_url`. |
| A finding line doesn't link to the diff | The line isn't part of the PR's diff hunks. Posting puts it in the review body instead. |

---

## Development

PRism itself needs only Python's standard library. The tests and linter are dev-only:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest          # no network, no real gh/claude calls
.venv/bin/ruff check .
```

---

## Layout

Internal names still say `pr-review` in a few places (the default folder name, the launchd label `local.pr-review`, the `PR_REVIEW_PORT` and `PR_REVIEW_PYTHON` variables). They're harmless identifiers, kept so existing installs keep working.

```
server.py                  poller, review queue, reply checks, lessons, HTTP API, static server
config.example.json        template → copy to config.json (gitignored)
.env.example               template → copy to .env (gitignored)
prompts/
  _header.md               headless rules + PR block (shared)
  review_bundled.md        workflow for bundled mode
  review_user.md           workflow for user mode (drives your /pr-review skill)
  _shared.md               previously-raised + KB sections (shared)
  _output.md               report + findings JSON contract the UI reads (shared)
  check_responses.md       how replies are judged (resolved / expected / …)
  draft_comment.md         how findings become short PR comments (only when you post)
  discuss.md               the private "Ask the agent" chat about a report
reviewers/                 bundled agents: code-reviewer.md, consistency-reviewer.md
docs/knowledge-bases.md    how to build a knowledge base that repos map to automatically
examples/knowledge-base/   a small working KB (registry, service folders, architecture) to copy
skills/prism-kb-builder/   Claude Code skill that generates a KB in this layout from your repos
static/                    UI (index.html, app.js, styles.css, logo and icon SVGs)
.claude/agents/            share-readiness-reviewer.md: audits the repo before you publish it
run.sh                     foreground entry point (what launchd runs)
setup.sh, tools/setup.py   credential helper: browser-assisted tokens for GitHub, Claude, Jira, Confluence → .env
SECURITY.md                how to report a security issue privately
Dockerfile                 image: Python, git, gh, Claude Code, read-only Atlassian MCP server
docker-compose.yml         `docker compose up -d --build`; volumes for config, data, clones
docker/entrypoint.sh       container start: checks credentials, sets up git and Jira/Confluence
docs/setup.md              setup guide for the four pillars, Docker and native
start.sh stop.sh restart.sh status.sh           day-to-day control (launchd-aware)
install-autostart.sh uninstall-autostart.sh     login item on/off
data/                      state.json, reports/<pr>/<run>/, reports/<pr>/{chat,decisions}.json,
                           lessons/, lesson_candidates.json
workspace/                 repos/ (bare partial clones), prs/ (one worktree per PR)
tests/                     pytest tests; run .venv/bin/pytest (after pip install -r requirements-dev.txt)
```

---

## License

MIT. See [LICENSE](LICENSE).

This applies to the repository. A Docker image you build from it also contains third-party software under its own licenses (Debian, Node.js, GitHub CLI, Claude Code, mcp-atlassian), so don't publish built images.

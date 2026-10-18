# Using PRism

- [How a review works](#how-a-review-works)
- [Using the UI](#using-the-ui)
- [My PRs](#my-prs)
- [Posting comments (opt-in)](#posting-comments-opt-in)
- [Replies to your comments and learned lessons](#replies-to-your-comments-and-learned-lessons)
- [Asking the review agent about findings](#asking-the-review-agent-about-findings)
- [Running it: start, stop, auto-start](#running-it)
- [Development](#development)
- [Layout](#layout)

---

## How a review works

Every few minutes PRism:

1. finds the open, non-draft PRs that request **your** review;
2. checks each one out into a throwaway git worktree;
3. reviews it with [Claude Code](https://docs.claude.com/en/docs/claude-code) in headless mode:
   - a **code pass**: lint, a secret scan, and bugs, reliability, security and tests;
   - a **consistency pass** against the linked ticket and docs, if any;
4. adds a **knowledge-base pass** for repos you've attached docs to: flow context, and regressions against documented contracts (see [knowledge-bases.md](knowledge-bases.md));
5. shows the reports in a Jira-style UI, with findings grouped Critical, High, Medium and Low.

Medium and Low findings only count when they're on lines the PR changes, and Medium means "should fix before merge". Each report opens with **What needs fixing** (cause, why it matters and fix, one line each) and **Flow context**.

The first sync runs straight away. Reviews start automatically, `max_concurrent_reviews` at a time. Each review takes a few minutes and costs roughly $1–2 of Claude usage, depending on the PR's size and your model.

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
- **Chats stay out of future reviews.** The full transcript is kept in `data/reports/<pr>/chat.json` for you to reread, but it's never sent to a review. Within one report, replies resume the same `claude` session, so each message only sends what you typed. When a review finishes, PRism compacts a copy of its session (cheap then, while Claude's prompt cache is still warm) and the chat starts from that, so the agent remembers the code it read and why it ruled things out, whenever you open the chat. Compaction runs in the background after the review shows as done; if you open the chat before it finishes, the first reply waits up to 45 seconds for it, then starts from the report. If the PR has new commits, or `compact_after_review` is `false`, the chat starts from the report alone. Compaction costs one short summary per review, logged as `cost_usd` in the run's `session.json`. Each chat reply's cost is in `chat.json`.
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

For Docker, see [Day to day](setup.md#day-to-day) in the setup guide.

### Auto-start at login (macOS, launchd)

```sh
./install-autostart.sh     # install or refresh ~/Library/LaunchAgents/local.pr-review.plist
./uninstall-autostart.sh   # remove it; ./start.sh still works by hand afterwards
```

- It starts at login and restarts on crash (`KeepAlive`).
- Once it's installed, the scripts above go through launchd. `./stop.sh` unloads the service until the next `./start.sh` or login. `kill` alone won't stop it, because launchd restarts it.
- launchd starts with a bare `PATH`, so the plist has one fixed `PATH` built from where `claude`, `gh`, `git`, `node` and your Python lived at install time. **Re-run `./install-autostart.sh`** after moving this folder or changing those tools (e.g. a new nvm Node version).
- Check it with `launchctl print gui/$(id -u)/local.pr-review | grep -E "state|pid"`.

### Auto-start on Linux (systemd)

Save this as `~/.config/systemd/user/prism.service`, with `WorkingDirectory` and `ExecStart` pointing at your checkout:

```ini
[Unit]
Description=PRism PR review service

[Service]
WorkingDirectory=%h/prism
ExecStart=%h/prism/run.sh
Restart=on-failure

[Install]
WantedBy=default.target
```

Then `systemctl --user daemon-reload && systemctl --user enable --now prism`, and `loginctl enable-linger $USER` if it should run while you're logged out. The service sees systemd's `PATH`, so add `Environment=PATH=...` if `claude` or `gh` live somewhere else (e.g. `~/.local/bin`).

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

Internal names still say `pr-review` in a few places (the default folder name, the launchd label `local.pr-review`, the `PR_REVIEW_PORT` and `PR_REVIEW_PYTHON` variables). They're internal identifiers and harmless.

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
docs/                      setup, configuration, usage, knowledge bases, safety, troubleshooting
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
start.sh stop.sh restart.sh status.sh           day-to-day control (launchd-aware)
install-autostart.sh uninstall-autostart.sh     login item on/off
data/                      state.json, reports/<pr>/<run>/, reports/<pr>/{chat,decisions}.json,
                           lessons/, lesson_candidates.json
workspace/                 repos/ (bare partial clones), prs/ (one worktree per PR)
tests/                     pytest tests; run .venv/bin/pytest (after pip install -r requirements-dev.txt)
```

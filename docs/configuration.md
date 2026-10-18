# Configuration

- [Access and tokens (`.env`)](#access-and-tokens)
- [`config.json`](#configjson)
- [Tickets and docs through MCP](#tickets-and-docs-through-mcp) (Jira, Confluence, Google Drive, Linear, Notion…)
- [Reviewer modes](#reviewer-modes)

For step-by-step setup of each service, see [setup.md](setup.md).

---

## Access and tokens

All keys live in **one file, `.env`**. Every value in it is passed to the `gh`, `git` and `claude` processes the service runs.

| Key | Needed? | What it's for | Minimum permissions |
| --- | --- | --- | --- |
| `GITHUB_TOKEN` | Docker: yes. Natively: no; empty means your `gh auth login` session is used. | Searching PRs, reading PR metadata, diffs and review threads, and posting comments or approving PRs when you confirm it in the UI. | Classic token: `repo`, `read:org`, authorized for your org's SSO if it uses SAML SSO. See [setup.md](setup.md#2-github-one-token). A fine-grained token is a stricter alternative: **Contents: Read**, **Metadata: Read**, **Issues: Read**, **Pull requests: Read and write** (just *Read* if you won't post or approve from PRism; the **Approve…** button then stays hidden). |
| `CLAUDE_CODE_OAUTH_TOKEN` | Docker only | Your Claude subscription inside the container (`claude setup-token` on the host; `./setup.sh` captures it). | |
| `JIRA_URL`, `JIRA_USERNAME`, `JIRA_API_TOKEN`, `CONFLUENCE_URL`, `CONFLUENCE_USERNAME`, `CONFLUENCE_API_TOKEN` | Docker only, optional | The bundled read-only Atlassian server. Natively, use your own MCP setup instead. See [setup.md](setup.md#4-jira-and-confluence-optional-an-atlassian-api-token). | An Atlassian API token (reads what you can read). |
| `ANTHROPIC_API_KEY` | No. Empty means your logged-in Claude Code account (subscription) is used. Set it only if you want per-token API billing. | The `claude -p` reviews and chats. | |
| `PR_REVIEW_PORT` | No | Port override (default `6161`). | |
| `PRISM_BIND`, `PRISM_EXTRA_READ_TOOLS`, `PR_REVIEW_PYTHON` | No, set for you | Internal: the listen address (the Docker image sets `0.0.0.0`), ticket tools the Docker entrypoint registers, and the Python that `run.sh` and launchd use. Leave them unset. | |

Cloning uses `github.clone_url` (default `git@github.com:{repo}.git`, i.e. SSH). For HTTPS with a token, set it to `https://github.com/{repo}.git` and make sure `gh auth setup-git` has run.

Claude Code needs to be logged in as the same OS user the service runs as, because launchd runs it as you. Check with `claude -p "say ok"`.

---

## `config.json`

`config.json` is yours and gitignored. Start from `config.example.json`. Anything you leave out falls back to the defaults in `server.py`. `config.local.json`, if present, is merged on top.

```jsonc
{
  "port": 6161,
  "github": {
    "search_query": "is:pr is:open draft:false review-requested:@me",  // any GitHub search
    "orgs": ["my-org"],                        // adds org:my-org (several are OR'd); [] = all
    "include_my_reviews": true,                // also keep open PRs you reviewed/commented on
    "my_reviews_window_days": 30,              // ...if updated within this many days
    "clone_url": "git@github.com:{repo}.git",
    "auto_review_forks": false                 // fork PRs run a stranger's code: review them only on request
  },
  "poll_interval_sec": 300,                    // or pick "Every N min" next to Sync now
  "auto_review": true,                         // review PRs you're asked to review automatically (switch in "To review")
  "my_prs": {"enabled": true, "auto_review": false}, // "My PRs": list your own PRs; review on request (or auto)
  "auto_rereview_on_new_commits": true,        // re-review only when the head SHA changes
  "max_concurrent_reviews": 3,
  "review_timeout_sec": 2700,
  "response_check_timeout_sec": 900,           // one check of replies to your comments
  "claude": { "bin": "claude", "model": null, "draft_model": "sonnet", "chat_model": null }, // null = your Claude Code default model
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
  "knowledge_bases": []                        // see knowledge-bases.md; easiest to manage in the UI
}
```

Restart after editing by hand (`./restart.sh`). Changes made in the UI apply immediately.

The knowledge-base fields are described in [knowledge-bases.md](knowledge-bases.md#kb-fields).

---

## Tickets and docs through MCP

The review always **starts from the GitHub PR**. It only follows what the PR itself links to: a ticket key in the title, branch or description, and doc links in the description. It reads those with the MCP tools you allow, **read-only**. Without MCP, the PR title and description are the spec, and GitHub issues the PR links to are read with `gh issue view`.

Reviews run `claude -p` as you, so they see the **same MCP servers your `claude` sees**: servers you added with `claude mcp add` (user scope), and claude.ai connectors if you're logged in with a claude.ai account. MCP servers defined in the *reviewed repo* (`.mcp.json`, `.claude/settings.json`) are **not** loaded.

In Docker, Jira and Confluence come from the bundled read-only server instead; see [setup.md](setup.md#4-jira-and-confluence-optional-an-atlassian-api-token).

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
| `user` | Your own Claude Code skill at `reviewer.skill_path`, and whatever agents it calls from `~/.claude/agents/` | Set `reviewer.lessons_files` to your agents' lessons files | People with an existing `/pr-review` skill. `prompts/review_user.md` tells the skill which steps to skip (threads, checkout, posting) and assumes a code-review agent and a consistency-review agent; adapt it to your skill's steps. |

Both modes share the same output format (`prompts/_output.md`), so the UI works with either. To change how reviews behave, edit `reviewers/*.md` (bundled) or your skill (user).

**Keeping reviews cheap.** Each finding is written once: the agents report only their own findings, the review's final message is just the findings JSON, and PRism builds the **Full report** tab from it instead of having the model write the report a second time. In bundled mode the consistency pass runs on Sonnet (`model: sonnet` in `reviewers/consistency-reviewer.md`); the code pass, which needs the strongest reasoning, uses `claude.model`. Add, change or remove a `model:` line in a reviewer's frontmatter to choose its model. The **Tokens** row under "This review" shows each model's input, output and cache tokens for the run, so you can see where the cost goes.

# Setting up PRism

PRism needs access to four things. This page says exactly what each one needs, for both ways of
running it.

| Pillar | Used for | Required? | Access it needs |
| --- | --- | --- | --- |
| **GitHub** | Finding PRs that request your review, reading diffs and review threads, cloning | Yes | Read code; read and write pull requests (to post comments and approve PRs when you confirm) |
| **Claude** | Running the reviews and the "Ask the agent" chat | Yes | Your Claude subscription (or an API key) |
| **Jira** | Reading the ticket a PR links to, so the review checks the acceptance criteria | Optional | Read |
| **Confluence** | Reading design docs a PR links to | Optional | Read |

**Jira and Confluence are optional, and reviews never fail without them.** When they aren't set up
(or a lookup fails), the review uses the PR title and description as the spec and lists the ticket
under "not validated". PRism also *can't* reach them unless you set them up here: any Atlassian or
other MCP tool your personal Claude settings pre-approve is blocked in reviews unless it's listed in
PRism's own config.

**Git host: GitHub only (github.com) for now.** PRism finds PRs, reads review threads and posts
comments through the GitHub API, and fetches PR heads from `refs/pull/<n>/head`. GitLab, Bitbucket
and Azure DevOps aren't supported. GitHub Enterprise Server might work by setting `GH_HOST` and
`github.clone_url`, but it hasn't been tested.

Pick one way to run it:

- **[Docker](#docker-recommended-for-most-people)** (recommended): one container with everything
  installed. Reviews run inside it, so they can't see the rest of your machine: your files, SSH
  keys and other logins stay out of reach. You give it tokens in `.env`.
- **[Native](#native-macos-or-linux)**: runs on your machine with the logins you already have
  (`gh auth login`, your Claude Code login, your Claude Code MCP servers). Simplest if those already
  work, but reviews run with your user's access.

PRism has no Python packages to install (standard library only), so there's no virtualenv step in
either setup.

---

## Docker (recommended for most people)

You need [Docker](https://docs.docker.com/get-docker/) (Docker Desktop on macOS or Windows, or Docker
Engine with the compose plugin on Linux), plus `python3` on the host for the setup helper.

### The quick way: `./setup.sh`

```sh
git clone <this repo> prism && cd prism
./setup.sh                    # choose "1 = Docker"
docker compose up -d --build
```

The helper does steps 1–4 below with you. For each service it opens the right page in your browser
(GitHub's is pre-filled with the name, your org, 90-day expiry and only the permissions PRism needs), you click
**Generate** or **Approve**, paste once into a hidden prompt, and it tests the token before saving it
to `.env`. For Claude it runs `claude setup-token` and picks the token up by itself. Tokens are never
printed. Run `./setup.sh --check` any time to re-test what's saved, or `./setup.sh github`,
`./setup.sh claude`, `./setup.sh atlassian` or `./setup.sh kb` (mount knowledge-base folders) to redo one part.

**Why tokens, not your existing logins?** On macOS, `gh` and Claude Code keep their logins in the
Keychain, so there's nothing a container could mount. And your `gh` login can usually do anything you can, including push
and merge, while PRism's token can't. `./setup.sh` can reuse your `gh` login if you choose to, with
a warning.

The manual steps, if you'd rather do it by hand:

### 1. Get the files

```sh
git clone <this repo> prism && cd prism
cp config.example.json config.json
cp .env.example .env
```

Create both files **before** the first start. If `config.json` doesn't exist, Docker creates a
folder with that name instead, and PRism will tell you to fix it.

### 2. GitHub: one token

The container can't use your host's `gh` login, so it uses a token.

1. Go to GitHub → **Settings → Developer settings → Fine-grained personal access tokens → Generate
   new token**.
2. **Resource owner:** your organization. **Repository access:** all repositories, or the ones you
   review.
3. **Permissions → Repository:** *Contents: Read*, *Metadata: Read*, *Issues: Read*, and *Pull requests:
   Read and write* (just *Read* if you won't post comments or approve PRs from PRism).
4. Put it in `.env`:
   ```
   GITHUB_TOKEN=github_pat_...
   ```

Without *Contents: write*, this token can't push code or merge, whatever command runs. Its *Pull
requests: write* lets the **Post** button work. Reviews hold the same token, and PRism's command
deny-list blocks the `gh` commands that comment, approve or edit. That list is a guard rail, not a
sandbox: a review that slips past it, or a PR's own lint code, could comment on, approve, close or
edit PRs.

If you never post from the UI, give it *Pull requests: Read* only (answer "no" in `./setup.sh`):
then reviews can't write to GitHub at all.

Your organization may need to approve fine-grained tokens. If PRs don't show up, check the token's
status on that settings page.

### 3. Claude: your subscription, via a token

On your **host** (where Claude Code is installed and logged in):

```sh
claude setup-token
```

It prints a long-lived token tied to your Claude subscription. Put it in `.env`:

```
CLAUDE_CODE_OAUTH_TOKEN=...
```

Usage counts against your plan, the same as using Claude Code yourself. Don't have Claude Code on the
host? Use an API key instead (`ANTHROPIC_API_KEY=`), which is billed per token.

### 4. Jira and Confluence (optional): an Atlassian API token

1. Create a token at <https://id.atlassian.com/manage-profile/security/api-tokens>. It acts as you, so
   reviews can read what you can read.
2. Put it in `.env` (on Atlassian Cloud the same token works for both):
   ```
   JIRA_URL=https://your-site.atlassian.net
   JIRA_USERNAME=you@your-company.com
   JIRA_API_TOKEN=...
   CONFLUENCE_URL=https://your-site.atlassian.net/wiki
   CONFLUENCE_USERNAME=you@your-company.com
   CONFLUENCE_API_TOKEN=...
   ```

When these are set, the container starts the bundled Atlassian MCP server
([mcp-atlassian](https://github.com/sooperset/mcp-atlassian), a community project) with two locks:
**read-only mode** (no tool that creates, edits, comments or transitions exists) and an allow-list of
just five tools: `jira_get_issue`, `jira_search`, `confluence_get_page`, `confluence_search` and
`confluence_get_page_children`. Ticket links in the UI point at your `JIRA_URL` automatically.

Each service is enabled only when its URL, username and token are **all** set. Set only Jira, only
Confluence, or neither. A half-filled service is skipped, with a `WARNING` in the startup log. Leave
them empty to skip both: reviews then use the PR description as the spec, and nothing errors.

### 5. Start it

```sh
docker compose up -d --build
docker compose logs -f        # optional: watch the first sync
```

Open <http://localhost:6161>. The startup log prints a clear `WARNING` for any credential that's
missing. It restarts automatically after a crash or reboot (`restart: unless-stopped`).

### 6. Knowledge bases (optional)

The container sees only what you mount. Run:

```sh
./setup.sh kb          # finds knowledge-base folders in your home folder; tick the ones to mount
docker compose up -d
```

It writes `docker-compose.override.yml` (gitignored; compose loads it automatically), mounting each
folder read-only under `/home/prism/kb/<folder>`. In the UI, go to **Knowledge bases → Add →
Browse…**: it opens at `~/kb`, where your folders are. A `refresh_command` runs inside the container, so it can only use tools in the
image.

### Day to day

| Task | Command |
| --- | --- |
| Stop / start | `docker compose stop` / `docker compose start` |
| Update after `git pull` | `docker compose up -d --build` |
| Logs | `docker compose logs -f` |
| Shell inside | `docker compose exec prism bash` |
| Check the tools inside | `docker compose exec prism sh -c 'gh auth status; claude mcp list'` |

Reports, chats and decisions live in `./data` on your host. Clones and Claude Code's state live in
Docker volumes (`prism-workspace`, `prism-claude`); `docker compose down -v` deletes them. They're
re-created on the next start.

### Notes and limits

- **Lint tools:** reviews can only run linters installed in the image (`make` is). Add the ones your
  repos use with `--build-arg EXTRA_APT_PACKAGES="shellcheck ..."`, or extend the `Dockerfile`.
- **Reviewer mode:** use `bundled` (the default). `user` mode drives a skill from your own
  `~/.claude`, which isn't in the container.
- **Claude Code version:** `latest` at build time. Pin it with
  `--build-arg CLAUDE_CODE_VERSION=<version>`.
- **Linux: `./data` must be writable by uid 1000** (the container's user). `./setup.sh` creates it.
  If your uid isn't 1000, run `sudo chown 1000:1000 data`, or add `user: "${UID}:${GID}"` under
  `prism:` in `docker-compose.yml`. Docker Desktop on macOS handles this for you.
- **Don't run Docker and the native service from the same folder at the same time.** They share
  `./data` and the port.

---

## Native (macOS or Linux)

You need Python 3.9+ (macOS's built-in `python3` works), git, the
[GitHub CLI](https://cli.github.com) and [Claude Code](https://claude.com/claude-code).

### 1. Get the files

```sh
git clone <this repo> prism && cd prism
cp config.example.json config.json
cp .env.example .env              # can stay empty natively
```

### 2. GitHub

```sh
gh auth login
gh auth status
```

PRism uses this login. Your `gh` login usually has the `repo` scope, which can push and merge, and
only PRism's command deny-list stands in the way. To narrow that, put a fine-grained `GITHUB_TOKEN`
in `.env` (step 2 of the Docker setup, or `./setup.sh github`). Natively, reviews still run as you (your `gh`
keyring login and SSH keys stay reachable), so the deny-list is the real guard; for reviews that
truly can't write, run PRism in Docker with a read-only token (then **Post** and **Approve** are unavailable).

### 3. Claude

```sh
claude          # log in once, then exit
claude -p "say ok"
```

PRism runs `claude -p` as you, on your subscription. Leave `ANTHROPIC_API_KEY` empty.

### 4. Jira and Confluence (optional)

Natively, reviews use **your own Claude Code MCP setup**. Either:

- **The claude.ai Atlassian connector:** connect it in claude.ai (Settings → Connectors). The tool
  names look like `mcp__claude_ai_Atlassian__getJiraIssue`.
- **A local server**, e.g. the same one the Docker image uses, read-only:
  ```sh
  pip install mcp-atlassian   # or: uvx mcp-atlassian
  claude mcp add --scope user atlassian \
    -e READ_ONLY_MODE=true -e JIRA_URL=... -e JIRA_USERNAME=... -e JIRA_API_TOKEN=... \
    -e CONFLUENCE_URL=... -e CONFLUENCE_USERNAME=... -e CONFLUENCE_API_TOKEN=... \
    -- mcp-atlassian
  ```

Then list the exact tool names (`claude mcp list`, or ask Claude Code "list the tools of the atlassian
MCP server"), and put **only the read tools** in `config.json`:

```json
"tickets": {
  "url_template": "https://your-site.atlassian.net/browse/{key}",
  "read_tools": ["mcp__atlassian__jira_get_issue", "mcp__atlassian__confluence_get_page"],
  "deny_tools": []
}
```

Reviews can use only the tools listed in `read_tools`. If your own `~/.claude/settings.json`
pre-approves other MCP tools, PRism blocks them in reviews; the startup log lists what it blocked.
One exception: if your settings allow a **whole server** (e.g. `"mcp__atlassian"`) and you list some
of its tools in `read_tools`, PRism can't block the rest without blocking those too. List that
server's write tools in `deny_tools` (the startup log warns when this applies).
With `read_tools` empty, reviews don't touch Jira or Confluence at all. The README section
[Tickets and docs through MCP](../README.md#tickets-and-docs-through-mcp) has more examples.

### 5. Start it

```sh
./start.sh                  # runs in the background and opens http://localhost:6161
./install-autostart.sh      # optional, macOS: start at login, restart on crash
```

`./stop.sh`, `./restart.sh` and `./status.sh` do what they say.

---

## Check that every pillar works

In the UI, open any PR and click **Review again**, then look at:

| Pillar | Working when |
| --- | --- |
| GitHub | PRs appear in **To review**; the top bar says "Synced …" |
| Claude | The review finishes and shows findings |
| Jira | The review's **Acceptance criteria** tab lists the ticket's criteria; the Details panel shows the ticket |
| Confluence | Linked docs appear in the review's sources; anything unreadable is listed under "not validated" |

If something fails, see [Troubleshooting](../README.md#troubleshooting).

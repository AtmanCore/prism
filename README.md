# PRism

PRism pre-reviews the GitHub PRs that request your review. It runs locally, checks out each PR, reviews
it with [Claude Code](https://docs.claude.com/en/docs/claude-code) (bugs, security, tests, the linked
ticket and your docs), and shows the findings in a web UI at <http://localhost:6161>.
**Nothing is posted to GitHub unless you confirm it.**

## Requirements

- A GitHub account (github.com) and a Claude subscription or API key
- **Docker** (recommended), plus `python3` for the setup helper, or
- **Native** (macOS, Linux or WSL): Python 3.9+, git, the [GitHub CLI](https://cli.github.com),
  [Claude Code](https://claude.com/claude-code) logged in, and SSH access to GitHub (`ssh -T git@github.com`),
  or set `github.clone_url` to HTTPS ([configuration](docs/configuration.md))
- Optional: Jira and Confluence, for checking a PR against its ticket

## GitHub token

1. Create a **classic** token at <https://github.com/settings/tokens/new> with scopes **`repo`** and
   **`read:org`**. PRism uses it to read PRs, and to post comments or approve when you confirm it.
2. If your org uses SAML SSO: on the [tokens list](https://github.com/settings/tokens), click
   **Configure SSO** next to the token and **Authorize** your org. Otherwise org PRs won't show up.
3. Put it in `.env` as `GITHUB_TOKEN=ghp_...`, or let `./setup.sh` open the page and paste it there.

Natively, `gh auth login` works instead (authorize your org's SSO there too). A `repo` token can
also push and merge; PRism's deny-list blocks that, but it's a guard rail. See [Safety](docs/safety.md).

## Quick start

**Docker (recommended):**

```sh
git clone https://github.com/AtmanCore/prism.git && cd prism
./setup.sh                      # choose Docker; asks for the GitHub and Claude tokens, saves .env
docker compose up -d --build    # then open http://localhost:6161
```

**Native:**

```sh
git clone https://github.com/AtmanCore/prism.git && cd prism
cp config.example.json config.json
cp .env.example .env            # add GITHUB_TOKEN, or leave empty to use gh auth login
./start.sh                      # opens http://localhost:6161
```

Step-by-step setup, including Jira and Confluence: [docs/setup.md](docs/setup.md).

## Useful commands

```sh
# Native
./start.sh                      # start and open the UI
./stop.sh
./restart.sh                    # safe restart: running reviews finish first
./status.sh                     # running? plus the last log lines
./install-autostart.sh          # macOS: start at login

# Docker
docker compose up -d --build    # start, or update after git pull
docker compose stop
docker compose logs -f

# Setup
./setup.sh --check              # re-test saved tokens
./setup.sh github               # redo one part: github, claude, atlassian or kb

# Tests
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

## Docs

- [Setup](docs/setup.md): GitHub, Claude, Jira and Confluence, Docker or native
- [Configuration](docs/configuration.md): `.env` keys, `config.json`, ticket tools (MCP), reviewer modes
- [Using PRism](docs/usage.md): the UI, posting comments, replies, the agent chat, auto-start
- [Knowledge bases](docs/knowledge-bases.md): give reviews context about your services
- [Safety](docs/safety.md): what stops PRism from writing to GitHub
- [Troubleshooting](docs/troubleshooting.md)

## License

MIT. See [LICENSE](LICENSE). Docker images you build also contain third-party software under its
own licenses, so don't publish built images.

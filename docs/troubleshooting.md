# Troubleshooting

| Symptom | Fix |
| --- | --- |
| "Sync failed" in the top bar | Run `gh auth status` (or `./setup.sh --check` in Docker). Try the search in the GitHub UI with your `search_query`. |
| No PRs show up | Run `gh search prs --review-requested=@me --state=open`. If that lists PRs, check `github.orgs` and `github.search_query` in `config.json`. If your org uses SAML SSO, the token must be authorized for it: on <https://github.com/settings/tokens>, click **Configure SSO** next to the token and **Authorize** your org. |
| Port 6161 already in use | Set `PR_REVIEW_PORT=<other port>` in `.env`, or find the other process with `lsof -i :6161`. |
| `claude` / `gh` / `git` not found | Install it and log in (`claude`, `gh auth login`); the startup log names what's missing. Under launchd, re-run `./install-autostart.sh` afterwards. |
| Reviews fail at "Checking out PR" | Test `ssh -T git@github.com`, or switch `github.clone_url` to HTTPS. |
| Reviews fail immediately | Run `claude -p "say ok"` as the same user. Under launchd, re-run `./install-autostart.sh` to refresh `PATH`. |
| Ticket not fetched | Check that `claude mcp list` shows the server as Connected, and that the tool names in `read_tools` match exactly. See [configuration.md](configuration.md#tickets-and-docs-through-mcp). |
| KB not used for a repo | Check **Manage knowledge bases**: the folder must be found, and the repo must be attached, in the registry, or a listed monorepo. See [knowledge-bases.md](knowledge-bases.md#8-check-that-its-picked-up). |
| **Approve…** button missing | Your token lacks the `repo` scope (or *Pull requests: write* for a fine-grained token), or it's your own PR, or the PR isn't open. |
| **Refresh now** disabled or failing | Hover it for the reason: the KB is mounted read-only in Docker, its `refresh_command` tool isn't installed, or the source repos can't be cloned with `github.clone_url`. |
| A finding line doesn't link to the diff | The line isn't part of the PR's diff hunks. Posting puts it in the review body instead. |

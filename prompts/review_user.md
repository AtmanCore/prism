# What to do
Follow the `/pr-review` skill at `{skill_path}` with these changes:

- **Phase 1 (fetch):** run as written, e.g. `gh pr view {url} --json ...` and `gh pr diff {url}`.
- **Phase 2 (ticket + docs):** follow "Tickets and linked docs" above: start from the PR, and
  only fetch what the PR itself links to.
- **Phase 1.5 (existing threads):** SKIP. The service already read them, and they're in
  "Previously raised on this PR" below. Never reply to or resolve any thread.
- **Phase 3 (checkout):** SKIP. The branch is already checked out here.
- **Phase 4 (dev-reviewer):** run as written (including its scope instruction), but tell the agent the base is `origin/{base_ref}`.
- **Phase 4.5 (flow context):** replaced by **Phase KB** below, which does the same and more.
- **Phase 5 (pr-reviewer):** run as written, with the PR description plus whatever ticket or docs
  Phase 2 found. Pass the knowledge-base findings from Phase KB below in as extra input too.
- **Phases 6 and 7:** SKIP ENTIRELY. There is nothing to restore and NOTHING is ever posted.


# What to do
Follow your PR review skill at `{skill_path}`, with these changes:

- **Fetching the PR:** run it as written, e.g. `gh pr view {url} --json ...` and `gh pr diff {url}`.
- **Ticket and docs:** follow "Tickets and linked docs" above: start from the PR, and only fetch
  what the PR itself links to.
- **Existing review threads:** SKIP. The service already read them, and they're in "Previously
  raised on this PR" below. Never reply to or resolve any thread.
- **Checking out the branch:** SKIP. It is already checked out here.
- **Code review:** run your code-review step or agent as written (including its scope
  instruction), but tell it the base is `origin/{base_ref}`.
- **Flow context:** if your skill has a step for it, replace it with **Phase KB** below, which does
  the same and more.
- **Ticket / consistency review:** run your consistency step or agent as written, with the PR
  description plus whatever ticket or docs you found. Pass it the knowledge-base findings from
  Phase KB below too.
- **Restoring state, posting comments, approving:** SKIP ENTIRELY. There is nothing to restore and
  NOTHING is ever posted.

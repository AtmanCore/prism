# What to do
Run this workflow. Two reviewer agents are available: `prr-code-reviewer` and
`prr-consistency-reviewer`.

1. **Fetch the PR:** `gh pr view {url} --json number,title,body,author,baseRefName,headRefName,url,files`
   and `git diff origin/{base_ref}...HEAD`. Pull any ticket key or doc links out of the title,
   branch and description.
2. **Ticket and docs:** follow "Tickets and linked docs" above.
3. **Code pass:** call the Agent tool with `subagent_type: prr-code-reviewer`. Give it the base
   (`origin/{base_ref}`), the PR title and description, and the "Previously raised" list below.
   Keep its full report.
4. **Knowledge-base pass:** run "Phase KB" below yourself (read-only).
5. **Consistency pass + merge:** call the Agent tool with `subagent_type: prr-consistency-reviewer`.
   Give it:
   - the PR metadata and the diff range;
   - the ticket and doc content, or "none linked";
   - the full code-review report;
   - your KB notes (flow and regressions);
   - the "Previously raised" list.

   It returns the final merged report.


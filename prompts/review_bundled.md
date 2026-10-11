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
5. **Consistency pass:** call the Agent tool with `subagent_type: prr-consistency-reviewer`.
   Give it:
   - the PR metadata and the diff range;
   - the ticket and doc content, or "none linked";
   - a one-line-per-finding list of the code review's findings (severity, title, ref) so it
     doesn't repeat them. Not the full report;
   - your KB notes (flow and regressions);
   - the "Previously raised" list.

   It returns only its own findings, AC coverage, flow context and the previously-raised
   classification. It does NOT merge or restate the code findings.
6. **Merge (you):** combine both reports straight into the final JSON below. Write each finding
   once, there. Don't write a prose report: PRism renders the full report from the JSON.
   - Keep the code findings' severity and substance; tighten wording, don't add to it.
   - Apply the scope filter: Medium/Low only for changed code, Medium only if genuinely
     should-fix, Critical/High may be pre-existing. Drop what restates an *expected* or
     *resolved* "Previously raised" point. Count what you dropped or downgraded.
   - Order: Critical → Low, code before consistency.

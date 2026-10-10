# Final output (required, exact format)
Your final message must contain exactly these two blocks and nothing else:

<<<REPORT
(the merged PR REVIEW REPORT from the consistency reviewer, verbatim, plus a trailing
"KNOWLEDGE-BASE REGRESSION" section if Phase KB ran)
REPORT>>>
<<<FINDINGS_JSON
(one JSON object, valid JSON, no comments, no trailing commas, matching the schema below)
FINDINGS_JSON>>>

JSON schema:
{{
  "verdict": "block" | "fix-before-merge" | "approve-with-nits" | "clean",
  "summary": "2-4 sentence plain-English overview of what the PR does and its main risks",
  "must_fix": [
    {{"finding_id": "F1", "severity": "critical|high|medium", "title": "short title",
      "ref": "file:line", "cause": "one line: what in this diff causes it",
      "why": "one line: what breaks / who is affected", "fix": "one line: the change to make",
      "importance": "blocks merge|fix before merge"}}
  ],
  "flow_context": {{
    "steps": [{{"name": "caller / component / sink", "kind": "caller|service|endpoint|job|db|topic|bucket|external", "changed": false, "note": "short"}}],
    "summary": "3-6 lines: caller -> this component -> downstream, and which part this PR changes",
    "source": "KB file(s) and/or code refs, or 'Not available'"
  }},
  "scope_filter": {{"dropped_out_of_scope": 0, "downgraded_medium_to_low": 0}},
  "ticket": {{"key": "ticket key linked from the PR, or null", "summary": "ticket summary or null"}},
  "counts": {{"critical": 0, "high": 0, "medium": 0, "low": 0}},
  "ac_coverage": [
    {{"status": "covered|partial|missing|contradicts", "criterion": "ticket AC, or a change the PR description claims when no ticket is linked", "ref": "file:line or null", "note": "..."}}
  ],
  "findings": [
    {{
      "id": "F1",
      "severity": "critical|high|medium|low",
      "title": "short title",
      "source": "code|consistency|kb-regression",
      "scope": "changed lines|caused by change|pre-existing",
      "category": "Bug|Reliability|Security|Secrets|Performance|Maintainability|Style|Lint|Tests|Missing AC|Contradicts Spec|Undocumented Change|Regression|Contract Break|...",
      "file": "path/relative/to/repo or null",
      "line": 123,
      "ref": "file:line or AC#/doc/KB statement",
      "why": "why this is a problem",
      "impact": "what breaks",
      "fix": "specific recommended fix (code snippets allowed, markdown ok)",
      "decision_id": "D1 if this re-raises an issue from \"Reviewer's own decisions\", else null",
      "reopen": "only with decision_id: one line on what changed in the code so the decision no longer holds, else null"
    }}
  ],
  "kb_regression": {{
    "ran": true,
    "knowledge_bases": ["..."],
    "services": ["..."],
    "summary": "what you checked against the KB and the outcome",
    "checked": ["contract / consumer / env / schema item you verified"]
  }},
  "top_priorities": ["...", "...", "..."],
  "residual_risk": "...",
  "not_validated": ["..."],
  "lint": "one-line lint result summary"
}}

`counts` must match `findings`. `findings` are the ones that survive the consistency reviewer's scope
filter: Medium/Low only for changed code, and Critical/High may be pre-existing. `must_fix` has one
entry per surviving Critical/High/Medium finding, in severity order, each field one plain-English
line. `flow_context.steps` lists the flow in order (3-7 steps), with `changed: true` on the steps
this PR modifies. KB findings use `"source": "kb-regression"`.

# Building a knowledge base

A knowledge base (KB) gives reviews what the diff can't: who calls this code, what it writes to, and which contracts other services depend on. With it, a review can say *"this renames `status` in `order.created`, which billing-worker and analytics consume"* instead of only commenting on the lines in front of it.

A KB is **just a folder of markdown** on your machine. PRism only **reads** it: it never runs anything from it, and never resolves secrets.

This page covers:
1. [How a PR finds its KB](#1-how-a-pr-finds-its-kb) (the matching rules)
2. [The recommended layout](#2-recommended-layout)
3. [The registry file](#3-the-registry-file-automatic-repo-mapping), which gives automatic repo → service mapping
4. [What to put in each service folder](#4-what-to-put-in-a-service-folder)
5. [System-level context files](#5-system-level-context-files)
6. [Monorepos](#6-monorepos-deploy-repos-infra-repos)
7. [Registering the KB in PRism](#7-register-the-kb-in-prism)
8. [Checking that it's picked up](#8-check-that-its-picked-up)
9. [Keeping it fresh](#9-keeping-it-fresh)
10. [A checklist](#10-checklist)

A complete, working example lives in [`examples/knowledge-base/`](../examples/knowledge-base/).

**Fastest way to build one:** the `prism-kb-builder` Claude Code skill in [`skills/prism-kb-builder/`](../skills/prism-kb-builder/SKILL.md) reads your repos and writes this whole layout for you. Install it with `mkdir -p ~/.claude/skills/prism-kb-builder && cp skills/prism-kb-builder/SKILL.md ~/.claude/skills/prism-kb-builder/`, then ask Claude Code to build a PRism knowledge base from your repos. Before writing, it lists the **upstream and downstream services** it found (who calls yours, and what yours feeds) and asks which to include, so the KB covers the services a change can break. The **How to build one** page under Knowledge bases in the UI has the same steps with copy buttons. **Running PRism in Docker?** The container only sees folders you mount: run `./setup.sh kb` to pick your KB folders, then `docker compose up -d`.

---

## 1. How a PR finds its KB

For every PR, each KB is checked with three rules. **Any** hit attaches that KB to the review:

| # | Rule | You provide | Result |
| --- | --- | --- | --- |
| 1 | **Registry** | a registry file listing `service → owner/repo` | PRs in a listed repo get that **service's folder** plus the KB's context files. Automatic, with no per-repo setup. |
| 2 | **Monorepo paths** | the repo in `monorepos` (e.g. `my-org/deploy`) | A PR there gets every registered service whose **repo name or service name appears in the changed paths or title**. |
| 3 | **Explicit attachment** | `attach: [{"repo": "my-org/x", "service": "x"}]` (or the UI) | That repo (globs like `my-org/*` work) gets the KB. With `service`, it also gets that folder. |

A registered service counts **only if its service folder exists**. That way a registry entry for a service you haven't documented yet doesn't attach an empty KB. (If you set no `service_dir` at all, every registry entry counts.)

**The simplest way to get automatic mapping:** keep a registry file and one folder per service. Every repo you register is then picked up on its next PR, with nothing to configure in PRism.

---

## 2. Recommended layout

```
my-knowledge-base/
├── README.md                  # what the KB covers and how it's generated
├── registry.yaml              # service → repo map                     (rule 1: automatic)
├── docs/                      # HAND-WRITTEN: runbooks, ADRs, design notes (context files)
│   └── *.md
└── kb/                        # GENERATED: regenerated on refresh
    ├── _index.md              # start page: services table, shared stores  (context file)
    ├── architecture.md        # system picture, cross-service contracts    (context file)
    ├── postgres.md            # optional: one file per shared data store   (context file)
    ├── orders-api/            # one folder per service                     (service_dir)
    │   ├── overview.md        # role, upstream, downstream, key behavior
    │   ├── api.md             # endpoints, request/response, callers
    │   ├── data.md            # tables, events/topics, schemas, consumers
    │   ├── env.md             # config/env var NAMES and where they come from (never values)
    │   ├── deploy.md          # how and where it runs (optional)
    │   └── sources.json       # repo, ref and commit it was generated from
    └── billing-worker/
        └── overview.md
```

Any names work. You tell PRism where things are with `service_dir` (e.g. `kb/{service}`), `registry.file` and `context_files`. When you pick the folder with **Browse…** in the UI, PRism detects the common layouts (`kb/`, `services/` and `docs/services/`) and fills these in for you.

---

## 3. The registry file (automatic repo mapping)

It maps each **service name** (which must equal its folder name under `service_dir`) to its **GitHub repo** (`owner/name`). Two formats are supported.

**YAML.** Each entry must start with `- name:` and have a `repo:` line. Other keys are fine; the matcher ignores them, and they're useful to people reading the KB:

```yaml
services:
  - name: orders-api          # must equal the folder name: kb/orders-api/
    repo: my-org/orders-api   # owner/name, exactly as on GitHub
    role: "HTTP API for orders"
    downstream: [billing-worker]

  - name: billing-worker
    repo: my-org/billing-worker
```

PRism uses no YAML library; it reads the file line by line. So keep `name` and `repo` as plain `key: value` lines, one entry per `- name:`, and don't use anchors, flow maps (`{…}`) or multi-line strings for these two keys.

**JSON** (a list, or `{"services": [...]}`):

```json
{ "services": [
  { "name": "orders-api", "repo": "my-org/orders-api" },
  { "name": "billing-worker", "repo": "my-org/billing-worker" }
] }
```

**Different field names?** If your registry already exists with other keys, e.g. `slug` and `github`, point PRism at them: `"registry": {"file": "catalog.yaml", "name_field": "slug", "repo_field": "github"}`.

**Repo names:** use `owner/name`. A bare `name` also works, but it can match same-named repos in other orgs.

---

## 4. What to put in a service folder

The reviewer reads **every file** in the service's folder, so keep it focused on what a reviewer needs in order to spot a break. Short and factual beats long and narrative.

| File | Put in it | The regressions it lets the review catch |
| --- | --- | --- |
| `overview.md` | One-line role; **upstream** (who calls it, and which endpoints/events); **downstream** (DBs, topics, buckets, other services); behavior other teams rely on (idempotency, units, ordering, schedules). | Changed behavior that callers depend on. Flow context. |
| `api.md` | Endpoints with request/response fields, status codes, enums, **and who calls each one**. | Removed or renamed fields, changed status codes, enum changes that break a named caller. |
| `data.md` | Tables and columns, events/topics and their payloads, file formats and paths, **and who consumes each one**. | Schema or event changes that break a consumer. |
| `env.md` | Env var **names**, where each comes from (a literal, or a secret/config reference), and required vs. optional. **Never values or secrets.** | Renamed or removed vars that a deployment still sets; new required vars without a default. |
| `deploy.md` *(optional)* | Where and how it runs, replicas, schedules and cron, resources. | Changes that assume a different runtime. |

What matters most is **naming the consumers**. "`status` enum: renaming breaks billing-worker" gives a review something concrete to check against the diff. Generated content (extracted from code or manifests) is great; add a line saying where it came from and when.

---

## 5. System-level context files

`context_files` are globs, relative to the KB folder, that are read for **every** PR attached to the KB, whichever service it is. Use them for:

- `kb/*.md`: `_index.md`, `architecture.md` (the overall picture and the **cross-service contracts**, the most valuable single file) and any shared-store docs such as `postgres.md`;
- `docs/**/*.md`: runbooks, ADRs and design notes;
- `README.md`: if it explains the system rather than the KB tooling.

Up to 40 files are passed per review, so point at the useful ones rather than `**/*.md` in a huge tree.

---

## 6. Monorepos (deploy repos, infra repos)

Some repos hold config for many services, like a Kubernetes manifests repo or Terraform. List them in the KB's `monorepos`, e.g. `["my-org/deploy", "my-org/terraform"]`.

A PR there picks up a registered service when its **repo name or service name appears in a changed path or the PR title**. For example, a change to `k8s/billing-worker/deployment.yaml` attaches `billing-worker`. So name your monorepo folders after the services.

---

## 7. Register the KB in PRism

**In the UI (recommended):**
1. Click **Manage knowledge bases**, then **Add knowledge base**.
2. Click **Browse…** and pick the folder. PRism reads it, shows the markdown files, registry and services it found, and fills in the name and settings.
3. Open **Settings** only if something needs changing (add monorepos there), then click **Save**.
4. Repos outside the registry: on any PR page click **Attach a knowledge base…**, or use **Attach** on the KB card. Globs like `my-org/*` work.

**In `config.json`** (then run `./restart.sh`):

```json
"knowledge_bases": [
  {
    "id": "shop",
    "name": "Shop platform",
    "path": "~/docs/shop-kb",
    "service_dir": "kb/{service}",
    "context_files": ["kb/*.md", "docs/**/*.md"],
    "registry": {"file": "registry.yaml", "name_field": "name", "repo_field": "repo"},
    "monorepos": ["my-org/deploy"],
    "attach": [{"repo": "my-org/shared-*"}],
    "refresh_command": null,
    "refresh_max_age_hours": 24
  }
]
```

You can have as many KBs as you like, and a PR can match several. For example, a shared service documented in two KBs gets both.

---

## 8. Check that it's picked up

- **Manage knowledge bases:** each card should show **Folder found**, the number of **services from the registry**, the **context files matched**, and **Open PRs using it**.
- **PR list:** PRs that match show a yellow `KB <id>` chip, and each KB gets its own sidebar view.
- **PR page:** the Details panel lists the matched KB and services, and the **KB regression** tab shows what was checked.
- **Quick command-line check** (no service restart needed):

  ```sh
  python3 -c "import server as S, json; kb=S.CONFIG['knowledge_bases'][0]; print(json.dumps(S.kb_services_available(kb), indent=1))"
  ```

A repo isn't matching? Check three things: the registry `repo` is exactly `owner/name`, the service's folder exists and is named exactly like `name`, and the KB `path` resolves (the card says **Folder found**).

---

## 9. Keeping it fresh

A stale KB still helps, but a fresh one helps more. If your KB is generated, set `refresh_command`. It runs in the KB folder before a review, at most every `refresh_max_age_hours` per service, and `{service}` is replaced with the service name:

```json
"refresh_command": ["./bin/generate-kb", "--service", "{service}"]
```

Failures are logged in the review's KB notes and never block it. Generate from **released tags** rather than working trees, so the KB describes what's deployed, and **never write secret values** into the KB.

### Refresh now, and how to refresh by hand

Every KB card in **Knowledge bases** has a **Refresh now** button:

- **KBs with their own tool** (a `refresh_command`, e.g. `./bin/kb update {service}`): PRism runs that
  command in the KB folder, once per registry service when it contains `{service}`. The same command
  also runs before reviews, at most every `refresh_max_age_hours`.
- **KBs built by the prism-kb-builder skill**: PRism shallow-clones or updates each source repo in the
  KB's own `.cache/repos/` (never your working checkouts, which may be on a feature branch), then runs
  the skill with Claude in refresh mode. Claude can write only inside the KB's `kb/`, `registry.yaml`
  and `README.md`, and has no shell, no MCP servers and no network tools, so it can't fetch, push or
  touch anything else.

Either way, say how to refresh in the KB's README (the skill writes this section for you), so a
person can do it from a terminal too, e.g. `cd ~/kb/my-platform && claude "Use the prism-kb-builder
skill to refresh this knowledge base"`, or your own tool's command.

In Docker, KB folders are mounted read-only, so skill refreshes run on your computer instead; a
`refresh_command` only works if its tool is installed in the image.

---

## 10. Checklist

- [ ] One folder per service, named exactly like the service.
- [ ] `registry.yaml` or `.json` mapping each service to `owner/repo`.
- [ ] `overview.md` per service, naming **upstream and downstream**.
- [ ] `api.md` and `data.md` naming **who consumes** each endpoint, field, table and event.
- [ ] `env.md` with var **names** and sources, and no values.
- [ ] `kb/architecture.md` with the cross-service contracts, and `kb/_index.md` listing the services.
- [ ] Added in **Manage knowledge bases**, with **Folder found** and the services counted.
- [ ] Monorepos listed; repos outside the registry attached.

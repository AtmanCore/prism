---
name: prism-kb-builder
description: Build or refresh a PRism knowledge base (a folder of markdown describing your services, their callers, data and contracts) from local repos. Use when the user wants to create a knowledge base for PRism, document services for code review, add a service to an existing KB, or refresh a KB after code changes. Reads source repos only; writes only inside the KB folder.
---

# Build a PRism knowledge base

A PRism knowledge base (KB) is a plain folder of markdown that tells a reviewer what the diff can't:
who calls a service, what it reads and writes, and which contracts other services depend on.
You read the user's repos and write that folder in the exact layout PRism matches automatically.

## Inputs (ask once if missing; the only other question is the neighbor check below)

1. **Sources**: GitHub repos (`owner/name`), or local repo paths / a parent folder of repos (used
   only to find each repo's `owner/name` with `git remote get-url origin`). A repo may be a monorepo
   whose subfolders are services.
2. **Output folder**: where the KB lives, e.g. `~/kb/my-platform`. Create it if it doesn't exist.
   If it already exists and has a `registry.yaml`, you are **refreshing**: update only the services
   whose code changed, and never touch `docs/` (see "Refreshing").
3. Optional: services to include or skip.

## Neighbor check: ask before you write (required)

A KB is only as useful as its edges. A change to one service breaks the services that call it
(**upstream**) or the ones it calls and feeds (**downstream**). So after discovering the services
you were given, and before writing anything:

1. For each one, find its direct neighbors in the code: HTTP/gRPC clients and the URLs or hostnames
   they call, message producers/consumers (topics, queues, events), shared tables and buckets,
   scheduled jobs that read its output, and callers that reference its hostname, client class or
   topic. Search all the sources you were given, plus sibling repos in the same parent folder.
2. Show **one** short message listing the neighbors that aren't already in scope, grouped as
   upstream and downstream, each with the evidence and where its repo is (or "repo not found
   locally"). For example:

   ```
   orders-api has neighbors that aren't in this KB yet:
     Upstream (call orders-api; a contract change breaks them):
       1. web-frontend   calls POST /orders           (~/code/web-frontend)
       2. support-tool   calls GET /orders/{id}       (repo not found locally)
     Downstream (orders-api feeds them; they break if what it sends changes):
       3. billing-worker consumes topic order.created  (~/code/billing-worker)
   Include which? (numbers, "all", or "none")
   ```
3. Add the chosen neighbors that have a local repo as full services. For the others (not chosen, or
   no repo), still name them as callers/consumers in `overview.md`, `api.md`, `data.md` and
   `architecture.md`, marked `(not in this KB)`, so a reviewer knows the edge exists.
4. Go one level out only: don't repeat the check for the newly added neighbors unless the user asks.

## Hard rules

- **Read from fresh checkouts, never from the user's working copies.** A working copy can be on a
  feature branch with uncommitted changes, so the KB would describe code that isn't deployed. Shallow
  clone each source repo into `<output>/.cache/repos/<owner>__<name>` at its default branch (or the
  ref the user names), or update an existing one:
  `git -C <dir> fetch --depth 1 origin <ref or HEAD> && git -C <dir> checkout --force --detach FETCH_HEAD`.
  Read only from there. Make sure `<output>/.gitignore` contains `.cache/`. (When PRism refreshes a
  KB, it has already done this and tells you the paths: don't clone or fetch yourself then.)
- **Read-only on sources.** Never modify, commit, build, install, or run anything in the checkouts or
  in the user's own repos. Reading files and `git` read commands (`git log`, `git ls-files`) only.
- **Write only inside the output folder, and never inside its `docs/`.** `docs/` is for people.
- **No secret values, ever.** Env vars, keys and connection strings are recorded by **name and
  source** (e.g. "from secret `orders-db-password`"), never their values. If you see a literal
  secret in code, write "literal value in code (redacted)" and mention it in your final summary.
- **Facts over prose.** Short, factual, scannable. Every claim should be checkable in the code.
  When unsure, write `Unverified:` in front of the line rather than guessing.
- **Name the consumers.** The most useful sentence in a KB is "renaming field X breaks service Y".
  Always record who calls each endpoint and who consumes each table, topic, file or event.

## Output layout (PRism reads exactly this)

```
<output>/
├── README.md              # what this KB covers, sources, and HOW TO REFRESH IT (see below)
├── .gitignore             # contains .cache/
├── .cache/repos/          # fresh shallow checkouts of the source repos (not committed)
├── registry.yaml          # service -> GitHub repo map (automatic PR matching)
├── docs/                  # HAND-WRITTEN notes. Create it empty if missing; never write inside it.
└── kb/                    # GENERATED. Everything you write goes here.
    ├── _index.md          # start page: links, services table, shared stores
    ├── architecture.md    # system picture and cross-service contracts
    ├── <store>.md         # optional, one per shared data store (e.g. postgres.md, snowflake.md)
    └── <service-name>/    # folder name MUST equal the registry `name`
        ├── overview.md
        ├── api.md         # only if it exposes an API
        ├── data.md        # only if it owns or emits data
        ├── env.md
        ├── deploy.md      # optional
        └── sources.json   # where this folder was generated from
```

### `registry.yaml` (strict format: PRism parses it line by line, without a YAML library)

```yaml
services:
  - name: orders-api
    repo: my-org/orders-api
    role: "HTTP API for orders"

  - name: billing-worker
    repo: my-org/billing-worker
```

- Every entry starts with `- name:` and has a `repo:` line. Plain `key: value` lines only; no
  anchors, flow maps or multi-line strings for `name` and `repo`.
- `repo` is `owner/name` exactly as on GitHub. Get it from `git remote get-url origin`.
- For a monorepo, give every service in it the monorepo's `repo`, and use the subfolder name as
  the service `name`.
- `name` is lowercase kebab-case (`^[a-z0-9][a-z0-9._-]*$`) and matches the folder under `kb/`.

### `kb/<name>/sources.json`

```json
{
  "repo": "my-org/orders-api",
  "ref": "main",
  "commit_sha": "<full sha of HEAD you read>",
  "generated_at": "<ISO 8601 UTC timestamp>",
  "generated_by": "prism-kb-builder"
}
```

### `kb/<name>/overview.md`

```markdown
# <Display name>

- **Repo:** `<owner/name>`
- **Ref:** `<branch or tag>` (`<short sha>`)
- **Generated:** <YYYY-MM-DD>

## Role
One or two lines.

## Upstream (who calls this)
- <caller>: <endpoint/event/schedule> (<file:line where you found it, if in these sources>)

## Downstream (what this calls or writes)
- <db/table | topic | bucket | service>: <what and why>

## Behavior others rely on
- idempotency, units, ordering, retries, schedules, rate limits, timeouts
```

### `kb/<name>/api.md`
One section per endpoint or RPC: method and path, request fields, response fields, status codes,
enums, auth, and **callers** (which services or clients use it, when you can find them in the
other sources; otherwise `Callers: unknown`).

### `kb/<name>/data.md`
Tables and columns, events/topics and payload fields, files and paths and formats. For each, the
**owner** and the **consumers**.

### `kb/<name>/env.md`
A table: `NAME | required? | source (literal / config / secret name) | used for`. Names and
sources only, never values.

### `kb/_index.md`

```markdown
# <System name>: knowledge base index

_Generated <YYYY-MM-DD> by the prism-kb-builder skill. Regenerated on refresh; put hand-written notes in `docs/`._

## Start here
- [architecture.md](architecture.md): how the pieces fit together
- one line per shared-store doc

## Services
| service | repo | role | ref |
|---|---|---|---|
| [`<name>`](<name>/overview.md) | `<owner/name>` | <role> | `<ref>` @ `<short sha>` |

## Shared data stores
- `<store>`: owner, and which services read or write it
```

### `kb/<store>.md` (optional)
Write one when a database, warehouse, bucket or topic family is shared by several services: its
objects (tables, schemas, prefixes), the owner of each, and every service that reads or writes it.

### `kb/architecture.md`
- A one-paragraph system description.
- A flow list: `caller -> service -> sink` for the main paths.
- **Cross-service contracts**: every endpoint, event, table or file used by more than one service,
  with its owner and its consumers. This is the single most valuable file. Build it last, from the
  service docs.

## How to work

1. **Discover services.** For each source path: a repo with one deployable is one service. A
   monorepo is one service per deployable subfolder (look for Dockerfiles, `main`/`app` modules,
   manifests, `pyproject.toml`/`package.json` per folder). Then run the **neighbor check** above and
   wait for the answer; it's the one question this skill always asks.
2. **Per service, read the code that defines contracts.** Entry points, route/handler
   definitions, schema and model files, migrations, message producers and consumers, HTTP clients
   to other services, config and env loading, deploy manifests. Skim tests for expected behavior.
   Use subagents in parallel when there are many services.
3. **Cross-reference callers.** Search all sources for each service's URLs, hostnames, topic
   names, table names and client classes to fill in Upstream and consumers.
4. **Write the files** in the layout above. Keep each file focused; omit `api.md` or `data.md`
   when they'd be empty.
5. **Write `registry.yaml`, then `kb/architecture.md`, any `kb/<store>.md`, then `kb/_index.md`,
   then `README.md`** using the README template below. Create an empty `docs/` if it doesn't exist.
6. **Self-check**: every `kb/<x>/` folder has a registry entry with the same name and a
   `sources.json`; every registry
   entry has a folder; `repo` values are `owner/name`; `grep -riE "password|secret|token|key"` the
   output and confirm that only names appear, never values.

### `README.md` (template: keep the "How to refresh" section exactly, with the real folder path)

```markdown
# <System name> knowledge base

<One paragraph: what this covers.> Generated by the prism-kb-builder skill on <YYYY-MM-DD>
from: <owner/repo list>.

- `kb/`: generated docs (regenerated on refresh). Start at `kb/_index.md`.
- `docs/`: hand-written notes. Never touched by a refresh.
- `.cache/repos/`: fresh checkouts of the source repos (not committed).

## How to refresh

From PRism: **Knowledge bases → <this KB> → Refresh now**. PRism updates the checkouts in
`.cache/repos/` and re-runs the skill; only services whose code changed are regenerated.

From a terminal:

    cd <absolute path of this folder>
    claude "Use the prism-kb-builder skill to refresh this knowledge base"

(The skill must be installed: `mkdir -p ~/.claude/skills/prism-kb-builder` and copy `SKILL.md`
from PRism's `skills/prism-kb-builder/` there.)
```

## Refreshing an existing KB

- Update each checkout in `.cache/repos/` first (see Hard rules), then compare its HEAD with
  `commit_sha` in the service's `kb/<name>/sources.json`. Regenerate
  only changed services, plus `kb/architecture.md`, store docs and `kb/_index.md` if contracts moved.
- `docs/` is never touched. Inside `kb/`, lines ending with `<!-- keep -->` are carried over verbatim.
- Never delete a service folder without saying so in the summary.

## Finish with

1. A short summary: services written or updated, contracts found, anything `Unverified`, any
   redacted literal secrets.
2. The settings to paste into PRism (**Manage knowledge bases -> Add knowledge base**, or click
   **Browse...** and pick the folder; PRism detects these automatically):
   - Folder: `<output>`
   - Service folder: `kb/{service}`
   - Registry file: `registry.yaml` (fields `name` / `repo`)
   - Context files: `kb/*.md`, `docs/**/*.md`
   - Monorepos: list any monorepo `owner/name` so PRs there match by changed paths.

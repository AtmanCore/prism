# Shop platform knowledge base (example)

A small example of the layout PRism and the `prism-kb-builder` skill use.

- `registry.yaml`: which GitHub repo each service lives in (PRism matches PRs with it).
- `kb/`: generated docs. `kb/_index.md` and `kb/architecture.md` describe the system;
  `kb/<service>/` has one folder per service.
- `docs/`: hand-written notes (runbooks, design docs). The skill never changes this folder.
- `.cache/repos/`: fresh checkouts of the source repos, made by a refresh (gitignored).

Add it in PRism: **Knowledge bases → Add → Browse…**, pick this folder. The settings fill in.

## How to refresh

From PRism: **Knowledge bases → Shop platform → Refresh now**. PRism updates the checkouts in
`.cache/repos/` and re-runs the prism-kb-builder skill; only services whose code changed are
regenerated. **Refresh now won't work on this example** until you point each `kb/*/sources.json` at
real repos (`my-org/...` doesn't exist).

From a terminal:

    cd path/to/this/folder
    claude "Use the prism-kb-builder skill to refresh this knowledge base"

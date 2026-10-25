# AGENTS.md

This repository is the `acu` CLI (`acumatica-cli`) plus the CLI test seed
(`acu-config/` SEED_DIRS + `acu-config/views/` + `acu-scenario/` at repo root).

Product GitOps (QMS) stays in the sibling data repo. Never print `.env` secrets.

## Live test target

| | |
| --- | --- |
| Tenant login | `ACUCLI` (`ACU_TENANT=ACUCLI`) |
| CLI test seed | this checkout `acu-config/` + `acu-scenario/` |
| Instance | this repo's `.env` (`ACU_BASE_URL`) |

The sibling GitOps `.env` names tenant `CNBN`. That is the QMS product tenant.
Do not apply, delete, or rebuild `CNBN` while testing this CLI.
Do not change `~/github/acu-gitops-qms/.env`.
Do not run `gmake rebuild` there (it uses `.env` `ACU_TENANT`).
Do not `cd` the sibling for CLI soak.

Pass `--tenant ACUCLI` on every live command (flag wins over `.env` `ACU_TENANT`).

## Local CLI

Global `acu` is the PyPI release. Do not `uv tool install --editable .`
(that replaces it).

This checkout is `.venv/bin/acu` (editable; `--version` shows `+dev`).
`gmake install` only syncs `.venv`.

```sh
gmake install              # uv sync; does not touch the PyPI tool
uv run acu --version       # 0.x.y+dev (<this checkout>)
```

Offline unit tests stay here: `gmake check`.
`gmake e2e` is a separate path: scratch tenants from packaged `config init` templates.
It is not the CLI test-seed soak.

## CLI test-seed soak (this checkout)

Run from this tree so cwd walk-up finds `acu-config/` and `.env`.
Use this checkout's `.venv/bin/acu`.

```sh
gmake install
uv run acu --tenant ACUCLI config check
uv run acu --tenant ACUCLI tenant list

# cold rebuild of the CLI test tenant only
uv run acu --tenant ACUCLI tenant delete --login ACUCLI --yes
uv run acu --tenant ACUCLI tenant create --login ACUCLI
uv run acu --tenant ACUCLI apply acu-config
uv run acu --tenant ACUCLI run acu-scenario
uv run acu --tenant ACUCLI diff acu-config
uv run acu --tenant ACUCLI state acu-config/views
```

This seed has no customization YAML under `acu-config/`. QMS apply stays in the sibling GitOps repo.

## Related repos

- GitOps seed: `~/github/acu-gitops-qms` (`kborovik/acu-gitops-qms`)
- QMS customization: `kborovik/acu-custom-qms`

## Work tracking: Plane

Work items live in Plane project **Acumatica CLI** (key `ACLI`), synced two-way
with this repo's issues (kborovik/acumatica-cli). Plane is where work is created,
assigned and moved; GitHub holds the code and a synced copy of each item.

- PR title starts with the work item key in brackets: `[ACLI-12] Short title`.
  This links the PR and moves the item (open: In Progress, merged: Done). A plain
  `ACLI-12` only links it; `#N` alone does not link at all.
- PR body: `Closes #N` for the item's synced GitHub issue, so the merge closes it.
- Commits mention `ACLI-N`.
- Create new work in Plane with the `github` label, which creates the GitHub
  issue; wait for its link (up to a minute) before editing the item. Do not open
  issues on GitHub; if one appears, add the `plane` label to import it.
- Change state in Plane. A GitHub reopen does not sync back.
- Through the Plane MCP, set labels with `workitem update` and the full `labels`
  list; `manage_label` does not trigger the sync.
- Related Plane projects: Acumatica Setup (`CORR`, kborovik/acumatica-corriganoil),
  Windmill Hub (`HUB`, kborovik/acumatica-integration-hub), Acumatica CLI
  (`ACLI`, kborovik/acumatica-cli), Kronos Infra (`INFRA`, kborovik/kronos-infra).
  Cross-project dependencies are Plane `blocked_by` relations.

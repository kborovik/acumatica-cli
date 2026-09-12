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

# AGENTS.md

This repository is the `acu` CLI (`acumatica-cli`). It has no GitOps seed.
Do not `acu apply` / `diff` / `run` from here.

Live CLI tests use tenant login `ACUCLI` and sibling GitOps repo
`~/github/acu-gitops-qms` as the YAML source.

Never print `.env` secrets.

## Live test target

| | |
| --- | --- |
| Tenant login | `ACUCLI` (`ACU_TENANT=ACUCLI`) |
| GitOps source | `~/github/acu-gitops-qms` |
| Instance | this repo's `.env` (`ACU_BASE_URL`) |

The GitOps repo `.env` names tenant `CNBN`. That is the QMS product tenant.
Do not apply, delete, or rebuild `CNBN` while testing this CLI.
Do not change `~/github/acu-gitops-qms/.env`.
Do not run `gmake rebuild` there (it uses `.env` `ACU_TENANT`).

Pass `--tenant ACUCLI` on every live command (flag wins over the GitOps `.env`).

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
It is not the GitOps soak.

## GitOps soak (from the data repo)

Run from the GitOps tree so cwd walk-up finds `config/` and `.env`.
Resolve this checkout with git, then put its `.venv/bin` first on `PATH`
so `acu` is local, not PyPI.

```sh
PATH="$(git rev-parse --show-toplevel)/.venv/bin:$PATH"
cd ~/github/acu-gitops-qms

acu --tenant ACUCLI config check
acu --tenant ACUCLI tenant list

# cold rebuild of the CLI test tenant only
acu --tenant ACUCLI tenant delete --login ACUCLI --yes
acu --tenant ACUCLI tenant create --login ACUCLI
acu --tenant ACUCLI apply
acu --tenant ACUCLI run
acu --tenant ACUCLI diff
acu --tenant ACUCLI state
```

`config/qms/` needs Lab5.QMS published. Skip it unless the change under test is QMS apply.

## Related repos

- GitOps seed: `~/github/acu-gitops-qms` (`kborovik/acu-gitops-qms`)
- QMS customization: `kborovik/acu-custom-qms`

# Overlays (Default API half)

Surgical rewrites for hosts whose **Default contract half** differs from
trunk. Keyed by resolved `api_version` (`--api-version`, else
`ACU_API_VERSION`, else code default), not ERP marketing year.

```
overlays/default-<api_version>/
  acu-config/…     # optional: same SEED_DIRS layout as trunk acu-config/
  acu-scenario/…   # optional: same basenames as trunk acu-scenario/*.yaml
```

Pass overlay trees as extra explicit paths. apply / diff / run / state
do not auto-compose overlays when a path is omitted (omitted path prints help).

```sh
# host pin ACU_API_VERSION=24.200.001 (scenario-only overlay today)
acu apply acu-config
acu diff acu-config
acu run acu-scenario overlays/default-24.200.001/acu-scenario
```

Pass an overlay `acu-config/` dir too when that half ships seed rewrites.
`run` later same-basename wins; apply/diff extra dirs append.

## Current lab halves (ERP line → half → overlay)

| ERP line (typical) | `api_version` | Overlay dir | Notes |
|--------------------|---------------|-------------|--------|
| 25r1 | `24.200.001` | `default-24.200.001/` | KitAssembly Type `Assembly` |
| 25r2 | `25.200.001` | *(none — trunk)* | KitAssembly Type `Production` |
| 26r1 | `25.200.001` | *(none — trunk)* | same half as 25r2 |

Trunk seed targets the newest supported half (`25.200.001` today). Older
halves only need an overlay when the contract rejects trunk fields.

## Future halves

1. Set host-true `ACU_API_VERSION` in `.env` (or `--api-version`) from `acu config check`.
2. If apply/run fails on a contract field, add
   `overlays/default-<that-half>/…` with the minimal rewrite.
3. Re-run with explicit overlay paths.

Do not add long-running git branches per ERP version. Do not commit multi-version OpenAPI trees.

# Overlays (Default API half)

Surgical rewrites for hosts whose **Default contract half** differs from
trunk. Keyed by `matrix.yaml` cell `default_api`, not ERP marketing year.

```
overlays/default-<default_api>/
  config/…      # optional: same SEED_DIRS layout as trunk config/
  scenario/…    # optional: same basenames as trunk scenario/*.yaml
```

## Bare compose (pin auto)

When you omit path args, `acu` appends the pin overlay when present:

| Command | Behavior |
|---------|----------|
| `acu apply` / `acu diff` | trunk `config/` then `overlays/default-<api>/config/` (or root SEED_DIRS under the overlay) |
| `acu run` | trunk `scenario/*.yaml`, same basename under `overlays/default-<api>/scenario/` **replaces** trunk |

Explicit path args disable pin auto (you compose paths yourself).

```sh
# host pinned default_api: 24.200.001 → uses overlays/default-24.200.001/
acu apply
acu run
acu diff

# explicit (no auto)
acu apply config/ overlays/default-24.200.001/
```

## Current lab matrix (ERP line → half → overlay)

| ERP line (typical) | `default_api` | Overlay dir | Notes |
|--------------------|---------------|-------------|--------|
| 25r1 | `24.200.001` | `default-24.200.001/` | KitAssembly Type `Assembly` |
| 25r2 | `25.200.001` | *(none — trunk)* | KitAssembly Type `Production` |
| 26r1 | `25.200.001` | *(none — trunk)* | same half as 25r2 |

Trunk seed targets the newest supported half (`25.200.001` today). Older
halves only need an overlay when the contract rejects trunk fields.

## Future halves

1. Set host-true `matrix.yaml` cell (`erp` + `default_api` + `base_url`) from `acu config check`.
2. If bare apply/run fails on a contract field, add
   `overlays/default-<that-half>/…` with the minimal rewrite.
3. Re-run bare `acu apply` / `acu run` / `acu diff` (pin auto picks it up).

Do not add long-running git branches per ERP version. Do not commit multi-version OpenAPI trees.

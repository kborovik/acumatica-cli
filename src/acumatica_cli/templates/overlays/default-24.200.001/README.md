# Overlay: `default-24.200.001`

For hosts whose highest published Default half is **24.200.001**
(lab 25r1 ERP pin).

| Path | Rewrite |
|------|---------|
| `scenario/30-build.yaml` | KitAssembly `Type: Assembly` (trunk uses `Production`) |

No config-entity rewrites yet. Bare `acu run` with `default_api: "24.200.001"`
replaces trunk `30-build.yaml` automatically.

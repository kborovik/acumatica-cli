# Overlay: `default-24.200.001`

For hosts whose highest published Default half is **24.200.001**
(lab 25r1 ERP pin). Set `ACU_API_VERSION=24.200.001`.

| Path | Rewrite |
|------|---------|
| `acu-scenario/30-build.yaml` | KitAssembly `Type: Assembly` (trunk uses `Production`) |

No config-entity rewrites yet.

Pass the overlay dir as a second `run` path. Later same-basename wins
(V44/V59).

```sh
acu run acu-scenario overlays/default-24.200.001/acu-scenario
```

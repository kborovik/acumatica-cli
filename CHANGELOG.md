# Changelog

## Unreleased

### Changed

- **Default API pin from `target.yaml` (T125–T126):** `Instance.api_version`
  resolves as `--api-version` flag, else `target.yaml` `default_api`, else
  code default `25.200.001`. `ACU_API_VERSION` is no longer a config key
  (ignored if present); scaffolded `.env` omits it; `acu config show` never
  emits it. Dual-source match gate retired (source-merge). `config check`
  reports `api_version from default_api=…` instead of a mismatch fail.
- **`acu extract` layout hard-cut (T115–T120):** extract always writes under
  `config/{bootstrap,baseline,setup,master}/`. Root `bootstrap/`…`master/`
  emit is gone; there is no `--layout`. Re-extract into a modern data repo or
  move root trees under `config/` before the next apply.
- Packaged registry rename: `extract_manifest.yaml` becomes `seed_catalog.yaml`
  (package data; operators do not author it). Catalog completeness equals the
  packaged `templates/config/**` seed set (V34), including master and
  filter-split multi-file entities.
- Docs: README + [docs/demo-seed.md](docs/demo-seed.md) treat extract as the
  inverse of apply under `config/`; entity map mirrors the catalog; CLI help
  documents the hard-cut. Sole data-repo Default pin = `target.yaml`
  `default_api` (no operator `ACU_API_VERSION` pin).

### Fixed

- Seed packaging no longer claims `WeightUOM` / `VolumeUOM` fields the endpoint
  GET never returns (B26 class permanent red diff).

# Changelog

## Unreleased

### Changed

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
  documents the hard-cut.

### Fixed

- Seed packaging no longer claims `WeightUOM` / `VolumeUOM` fields the endpoint
  GET never returns (B26 class permanent red diff).

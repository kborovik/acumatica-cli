# Changelog

## Unreleased

### Added

- `acu config init --flavor distribution` scaffolds a full virgin-tenant distribution demo seed: Bootstrap `project.xml`, extended features and COA, `master/` (inventory through customers), golden `scenario/buy-build-sell.yaml`, and a short rebuild README.
- Default `acu apply` / `acu diff` directory order includes `master/` when that directory exists (`bootstrap/`, `baseline/`, `setup/`, `master/`).
- Docs: [docs/distribution.md](docs/distribution.md) entity map and apply-order notes.

### Unchanged

- Default `acu config init` (no `--flavor`) remains finance-minimal; offline e2e that uses packaged templates stays on that path.

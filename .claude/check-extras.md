# check-extras — repo-local audit recipes

Consumed by /sdd:check (cascade scan) and sweep tasks. Each recipe:
run cmd, apply exemption filter, any surviving match = violation
→ bail w/ recipe msg; no commit until match-free.

## §V.9 — ASCII-only output audit

- cmd: `grep -rnP '[^\x00-\x7F]' src/`
- exempt: docstring bodies + `#` comment lines + XML comments (non-output-reaching)
- surviving match → bail: `non-ASCII in output-reaching string per §V.9 — swap ASCII glyph or relocate to docstring/comment`

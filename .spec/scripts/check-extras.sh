#!/usr/bin/env bash
# check-extras.sh - mechanical V1/V10/V18 drift greps (SPEC ST.19).
#
# /sdd:check extras-hook contract: emit bare `id|verdict|evidence` rows
# (no header, no prose) on stdout; the check run appends them verbatim.
# Exit 1 when any row is VIOLATE, 0 when all HOLD. Paths resolve from
# the repo root so the hook runs from any cwd. ASCII-only output (V9).
set -u
cd "$(dirname "$0")/../.." || exit 1

SRC=src/acumatica_cli
fail=0

row() { printf '%s|%s|%s\n' "$1" "$2" "$3"; }

# V1 plane-split: control plane (tenant.py, SSH) never touches httpx;
# data plane (client.py, REST) never touches subprocess.
v1=$(
    {
        grep -Hn 'httpx' "$SRC/tenant.py"
        grep -Hn 'subprocess' "$SRC/client.py"
    } | cut -d: -f1,2 | head -1
)
if [ -n "$v1" ]; then
    row V1 VIOLATE "$v1 crosses the SSH/REST plane split"
    fail=1
else
    row V1 HOLD "tenant.py free of httpx; client.py free of subprocess"
fi

# V10 inheritance: outside models.py no class subclasses BaseModel
# directly - models route through models.Model (frozen, extra=forbid).
v10=$(
    grep -RHn --include='*.py' '^class .*BaseModel' "$SRC" \
        | grep -v "^$SRC/models\.py:" | cut -d: -f1,2 | head -1
)
if [ -n "$v10" ]; then
    row V10 VIOLATE "$v10 subclasses BaseModel outside models.py"
    fail=1
else
    row V10 HOLD "no BaseModel subclass outside models.py"
fi

# V18 choke point: `exit $LASTEXITCODE` appears exactly once in src/,
# inside _ssh - call sites never hand-append the suffix.
v18_hits=$(grep -RHn --include='*.py' 'exit \$LASTEXITCODE' "$SRC" | cut -d: -f1,2)
if [ -z "$v18_hits" ]; then
    row V18 VIOLATE "no exit \$LASTEXITCODE in $SRC - _ssh choke point gone"
    fail=1
else
    v18_count=$(printf '%s\n' "$v18_hits" | wc -l | tr -d ' ')
    stray=$(printf '%s\n' "$v18_hits" | grep -v "^$SRC/tenant\.py:" | head -1)
    site=$(awk '
        /def [A-Za-z_][A-Za-z0-9_]*\(/ {
            fn = $0; sub(/^.*def /, "", fn); sub(/\(.*/, "", fn)
        }
        /exit \$LASTEXITCODE/ { print fn }
    ' "$SRC/tenant.py" | head -1)
    if [ "$v18_count" != "1" ] || [ -n "$stray" ]; then
        first=${stray:-$(printf '%s\n' "$v18_hits" | head -1)}
        row V18 VIOLATE "$v18_count sites; $first outside the _ssh choke point"
        fail=1
    elif [ "$site" != "_ssh" ]; then
        row V18 VIOLATE "$v18_hits sits in ${site:-module scope}, not _ssh"
        fail=1
    else
        row V18 HOLD "sole site $v18_hits inside _ssh"
    fi
fi

exit "$fail"

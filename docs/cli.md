# CLI output standards

How every `acu` command talks to the terminal. All output goes through
`src/acumatica_cli/output.py` — the only module that creates a rich `Console`.

## Two audiences, one code path

- **Humans** (TTY): colors, symbols, rounded tables, spinners.
- **LLM agents and scripts** (piped/non-TTY): plain, deterministic,
  greppable text — no ANSI codes, no box-drawing, no spinners.

[rich](https://rich.readthedocs.io) auto-detects the TTY and degrades to
plain text when piped, so both audiences share one code path. Never branch
output logic on TTY manually — the two sanctioned exceptions live inside
`output.py` (table borders, spinner-vs-line). `NO_COLOR` and `FORCE_COLOR`
are respected (rich built-in). Markup, emoji shortcodes, and syntax
highlighting are disabled on both consoles so payload text (e.g.
`PUT Currency [CAD]`) is never reinterpreted.

## Streams

- **stdout** — data and results: tenant table, drift lines, apply record
  lines. Anything an agent or script would consume or pipe. One record per
  line, never hard-wrapped at console width (`data()` prints with
  `soft_wrap` so long paths and drift lines stay greppable).
- **stderr** — everything about the process: progress steps, warnings,
  errors, success confirmations.

## Symbols and colors

Deterministic prefixes that survive piping (agents can grep them):

| Helper      | Prefix | Style    | Stream |
|-------------|--------|----------|--------|
| `data()`    | —      | plain    | stdout |
| `info()`    | —      | dim cyan | stderr |
| `success()` | `✓`    | green    | stderr |
| `warn()`    | `!`    | yellow   | stderr |
| `error()`   | `✗`    | red      | stderr |

## Tables

`output.table()`: rich `Table` with a rounded box on a TTY; `box=None`
(header + aligned plain columns) when piped.

## Long operations

Anything that takes seconds or more (tenant create, app-pool recycle,
REST login poll) runs inside `output.step("message")`: a spinner on a TTY,
a plain stderr line when piped — agents still see every step.

## Errors and exit codes

- Expected failures surface as one line — `✗ <message>` on stderr — and
  exit 1. No tracebacks: `cli.main()` catches `RuntimeError` (SSH/ac.exe,
  REST, first-login failures) and `httpx.HTTPError`.
- `ACU_DEBUG=1` re-raises for the full traceback.
- Validation errors raise `SystemExit("message")` directly — the sanctioned
  pattern for bad input (missing YAML fields, unset `ACU_PASSWORD`).
- Exit codes: **0** success, **1** error, **1 on drift** (`acu diff` — the
  load-bearing contract behind the drift proof).

## Dry run

Mutating lines are phrased `would PUT …`; the per-file summary is suffixed
`(dry run)`.

## Enforcement

No bare `print()` anywhere in `src/` — ruff rule `T20` fails the build.
Use `acumatica_cli.output`.

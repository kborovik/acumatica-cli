"""CLI output helpers — the only place `acu` writes to the terminal.

Two audiences, one code path (docs/cli.md): rich renders color, tables,
and spinners on a TTY and degrades to plain deterministic text when piped
(how LLM agents and scripts see it). stdout carries data and results;
stderr carries status, warnings, and errors.
"""

from collections.abc import Generator, Iterable
from contextlib import contextmanager

from rich import box
from rich.console import Console
from rich.table import Table

out = Console(markup=False, emoji=False, highlight=False)
err = Console(stderr=True, markup=False, emoji=False, highlight=False)


def data(msg: str) -> None:
    """Result line on stdout — what a script or agent consumes."""
    # soft_wrap: a result line must stay one greppable line, never
    # hard-wrapped at console width (long paths, drift lines)
    out.print(msg, soft_wrap=True)


def info(msg: str) -> None:
    """Progress line on stderr."""
    err.print(msg, style="dim cyan")


def success(msg: str) -> None:
    """Success line on stderr."""
    err.print(f"+ {msg}", style="green")


def warn(msg: str) -> None:
    """Warning line on stderr."""
    err.print(f"! {msg}", style="yellow")


def error(msg: str) -> None:
    """Error line on stderr."""
    err.print(f"x {msg}", style="red")


def table(title: str, columns: Iterable[str], rows: Iterable[Iterable[str]]) -> None:
    """Table on stdout: ASCII box on a TTY, plain aligned columns piped."""
    t = Table(
        title=title,
        title_justify="left",
        box=box.ASCII if out.is_terminal else None,
    )
    for column in columns:
        t.add_column(column)
    for row in rows:
        t.add_row(*row)
    out.print(t)


@contextmanager
def step(msg: str) -> Generator[None]:
    """Long operation: spinner on a TTY, plain stderr line when piped."""
    if err.is_terminal:
        # "line" is the ASCII spinner (-\|/); the default "dots" is braille
        with err.status(msg, spinner="line"):
            yield
    else:
        info(msg)
        yield

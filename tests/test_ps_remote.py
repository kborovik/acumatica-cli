"""scripts/ps-remote: the V12 discovery instrument, fully offline.

ssh and acu are stub executables prepended to PATH — these tests pin the
encoding round-trip (utf-16le/base64 -EncodedCommand sidesteps the
PowerShell-remote-shell quoting trap), the $LASTEXITCODE propagation idiom,
and host resolution through `acu config show` (never a parallel parse).
"""

import base64
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "ps-remote"


@pytest.fixture
def bindir(tmp_path: Path) -> Path:
    """Stub bin dir: ssh records its argv and exits 0; acu emits config yaml."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    capture = tmp_path / "ssh-argv"
    ssh = stubs / "ssh"
    ssh.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{capture}"\n')
    ssh.chmod(0o755)
    acu = stubs / "acu"
    acu.write_text(
        '#!/bin/sh\necho "host: stub.host"\necho "ssh: Administrator@stub.host"\n'
    )
    acu.chmod(0o755)
    return stubs


def run_script(bindir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    return subprocess.run(
        [str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False
    )


def ssh_argv(bindir: Path) -> list[str]:
    return (bindir.parent / "ssh-argv").read_text().splitlines()


def test_encoded_command_round_trips_utf16le(bindir: Path, tmp_path: Path) -> None:
    probe = tmp_path / "probe.ps1"
    probe.write_text('[PX.Data.PXVersionInfo]::Version | Write-Output "$_"\n')
    r = run_script(bindir, str(probe), "user@box")
    assert r.returncode == 0, r.stderr
    argv = ssh_argv(bindir)
    assert argv[:3] == ["-o", "BatchMode=yes", "user@box"]
    command = argv[3]
    prefix = "powershell -NoProfile -EncodedCommand "
    suffix = "; exit $LASTEXITCODE"
    assert command.startswith(prefix)
    assert command.endswith(suffix)
    encoded = command[len(prefix) : -len(suffix)]
    decoded = base64.b64decode(encoded).decode("utf-16le")
    assert decoded == probe.read_text()  # byte-exact: no quoting layer touched it


def test_host_defaults_to_acu_config_show_ssh_line(
    bindir: Path, tmp_path: Path
) -> None:
    probe = tmp_path / "probe.ps1"
    probe.write_text("Get-Date\n")
    r = run_script(bindir, str(probe))
    assert r.returncode == 0, r.stderr
    assert ssh_argv(bindir)[2] == "Administrator@stub.host"


def test_explicit_host_wins_without_invoking_acu(bindir: Path, tmp_path: Path) -> None:
    (bindir / "acu").write_text("#!/bin/sh\nexit 1\n")  # would fail if consulted
    probe = tmp_path / "probe.ps1"
    probe.write_text("Get-Date\n")
    r = run_script(bindir, str(probe), "other@box")
    assert r.returncode == 0, r.stderr
    assert ssh_argv(bindir)[2] == "other@box"


def test_missing_file_is_one_x_line(bindir: Path) -> None:
    r = run_script(bindir, "nope.ps1")
    assert r.returncode == 1
    assert r.stderr.startswith("x nope.ps1: no such file")


def test_ssh_exit_code_propagates(bindir: Path, tmp_path: Path) -> None:
    (bindir / "ssh").write_text("#!/bin/sh\nexit 7\n")
    probe = tmp_path / "probe.ps1"
    probe.write_text("Get-Date\n")
    assert run_script(bindir, str(probe), "user@box").returncode == 7

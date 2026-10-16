"""The trial launcher keeps its own home and pins Desktop at this checkout.

Sourcing the script must leave the caller alone. A real launch is not run:
setup downloads a Python toolchain, so these tests skip that hop and record
the command the script would execute.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


pytestmark = pytest.mark.platforms("posix")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "try-checkout.sh"
ENV_KEYS = (
    "HERMES_HOME",
    "HERMES_RUNTIME_DIR",
    "HERMES_DESKTOP_USER_DATA_DIR",
    "HERMES_DESKTOP_APP_NAME",
    "HERMES_DESKTOP_IGNORE_EXISTING",
    "HERMES_DESKTOP_ISOLATED_BACKEND",
    "HERMES_DESKTOP_HERMES_ROOT",
    "HERMES_DESKTOP_PYTHON",
    "XDG_DATA_HOME",
    "UV_CACHE_DIR",
    "NPM_CONFIG_CACHE",
    "NPM_CONFIG_USERCONFIG",
    "VIRTUAL_ENV",
    "HERMES_DESKTOP_HERMES",
    "HERMES_TUI_DIR",
    "HERMES_DESKTOP_DEV_SERVER",
    "HERMES_DESKTOP_IS_PACKAGED",
    "__HERMES_ACTIVATED",
    "__HERMES_TEST_PYTHON",
)


def _base_env(sandbox: Path, **extra: str) -> dict[str, str]:
    blocked = {
        "HERMES_TRY_DIR",
        "TRY_CHECKOUT_INNER",
        "TRY_CHECKOUT_SKIP_SETUP",
        "HERMES_TRY_LAUNCHER",
        "HERMES_TRY_PROBE_OUT",
        *ENV_KEYS,
    }
    env = {key: value for key, value in os.environ.items() if key not in blocked}
    env["HERMES_TRY_DIR"] = str(sandbox)
    env["TRY_CHECKOUT_SKIP_SETUP"] = "1"
    env.update(extra)
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_exec_keeps_the_installed_hermes_untouched(tmp_path: Path) -> None:
    sandbox = tmp_path / "trial dir"
    poison = tmp_path / "installed-home"
    output = tmp_path / "observed.json"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "keys = sys.argv[2].split(',')\n"
        "Path(sys.argv[1]).write_text(json.dumps({'argv': sys.argv[3:], 'env': {k: os.environ.get(k) for k in keys}}), encoding='utf-8')\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    checkout_existed = (ROOT / ".try-checkout").exists()
    env = _base_env(
        sandbox,
        HERMES_HOME=str(poison),
        VIRTUAL_ENV="/other/venv",
        HERMES_DESKTOP_HERMES="/usr/bin/hermes",
        HERMES_TUI_DIR="/other/tui",
        HERMES_DESKTOP_DEV_SERVER="http://127.0.0.1:5174",
        HERMES_DESKTOP_IS_PACKAGED="1",
        __HERMES_ACTIVATED="/real/hermes/facts.json",
        __HERMES_TEST_PYTHON="/real/hermes/test-python",
    )
    result = _run(
        ["exec", "--", sys.executable, str(probe), str(output), ",".join(ENV_KEYS), "argument with spaces"],
        env,
    )
    assert result.returncode == 7, result.stderr
    observed = json.loads(output.read_text(encoding="utf-8"))
    home = Path(observed["env"]["HERMES_HOME"])
    assert observed["argv"] == ["argument with spaces"]
    assert home == sandbox / "home"
    assert Path(observed["env"]["HERMES_RUNTIME_DIR"]) == sandbox / "tools"
    assert Path(observed["env"]["HERMES_DESKTOP_USER_DATA_DIR"]) == sandbox / "desktop"
    assert Path(observed["env"]["XDG_DATA_HOME"]) == sandbox / "xdg"
    assert Path(observed["env"]["UV_CACHE_DIR"]) == sandbox / "uv-cache"
    assert Path(observed["env"]["NPM_CONFIG_CACHE"]) == sandbox / "npm-cache"
    assert Path(observed["env"]["NPM_CONFIG_USERCONFIG"]) == sandbox / "npmrc"
    assert observed["env"]["HERMES_DESKTOP_APP_NAME"] == "Hermes Try"
    assert observed["env"]["HERMES_DESKTOP_IGNORE_EXISTING"] == "1"
    assert observed["env"]["HERMES_DESKTOP_ISOLATED_BACKEND"] == "1"
    assert Path(observed["env"]["HERMES_DESKTOP_HERMES_ROOT"]).resolve() == ROOT
    assert observed["env"]["HERMES_DESKTOP_PYTHON"] is None
    assert observed["env"]["VIRTUAL_ENV"] is None
    assert observed["env"]["HERMES_DESKTOP_HERMES"] is None
    assert observed["env"]["HERMES_TUI_DIR"] is None
    assert observed["env"]["HERMES_DESKTOP_DEV_SERVER"] is None
    assert observed["env"]["HERMES_DESKTOP_IS_PACKAGED"] is None
    assert observed["env"]["__HERMES_ACTIVATED"] is None
    assert observed["env"]["__HERMES_TEST_PYTHON"] is None
    assert not poison.exists()
    assert (sandbox / "npmrc").is_file()
    assert (ROOT / ".try-checkout").exists() == checkout_existed


def test_sourcing_does_not_redirect_the_caller(tmp_path: Path) -> None:
    poison = tmp_path / "installed-home"
    sandbox = tmp_path / "trial dir"
    env = _base_env(sandbox, HERMES_HOME=str(poison))
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; printf %s "${HERMES_HOME-}"', "bash", str(SCRIPT)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(poison)
    assert "do not source" in result.stderr
    assert not sandbox.exists()
    assert not poison.exists()


def test_help_and_unknown_commands_do_not_create_a_trial(tmp_path: Path) -> None:
    sandbox = tmp_path / "trial dir"
    env = _base_env(sandbox)
    help_result = _run(["--help"], env)
    assert help_result.returncode == 0, help_result.stderr
    assert "private Hermes home" in help_result.stdout
    assert not sandbox.exists()

    missing = _run([], env)
    assert missing.returncode == 1
    assert not sandbox.exists()

    unknown = _run(["nope"], env)
    assert unknown.returncode == 1
    assert "unknown command" in unknown.stderr
    assert not sandbox.exists()


@pytest.mark.parametrize(
    ("args", "expect_force", "expect_build_only", "expect_python"),
    [
        (["desktop"], False, False, True),
        (["desktop", "--rebuild"], True, False, True),
        (["build"], False, True, True),
        (["build", "--rebuild"], True, True, True),
        (["tui"], False, False, False),
        (["cli"], False, False, False),
    ],
)
def test_surfaces_pin_this_checkout(
    tmp_path: Path, args: list[str], expect_force: bool, expect_build_only: bool, expect_python: bool
) -> None:
    sandbox = tmp_path / "trial dir"
    output = tmp_path / "launch.json"
    launcher = tmp_path / "launcher.py"
    python_path = tmp_path / "python"
    python_path.write_text("", encoding="utf-8")
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path(os.environ['HERMES_TRY_PROBE_OUT']).write_text(json.dumps({'argv': sys.argv[1:], 'python': os.environ.get('HERMES_DESKTOP_PYTHON'), 'home': os.environ.get('HERMES_HOME')}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    extra = {"HERMES_TRY_LAUNCHER": str(launcher), "HERMES_TRY_PROBE_OUT": str(output)}
    if expect_python:
        extra["HERMES_DESKTOP_PYTHON"] = str(python_path)
    env = _base_env(sandbox, **extra)
    forwarded = ["--resume", "session with spaces"] if args[0] == "tui" else ["chat", "hello"] if args[0] == "cli" else []
    result = _run([*args, *forwarded], env)
    assert result.returncode == 0, result.stderr
    observed = json.loads(output.read_text(encoding="utf-8"))
    argv = observed["argv"]
    if args[0] in {"desktop", "build"}:
        assert argv[0] == "desktop"
        assert "--source" in argv
        assert "--ignore-existing" in argv
        assert "--build-only" in argv if expect_build_only else "--build-only" not in argv
        assert "--force-build" in argv if expect_force else "--force-build" not in argv
        root = argv[argv.index("--hermes-root") + 1]
        assert Path(root).resolve() == ROOT
        assert observed["python"] == str(python_path)
    elif args[0] == "tui":
        assert argv == ["--tui", "--resume", "session with spaces"]
        assert observed["python"] is None
    else:
        assert argv == ["chat", "hello"]
        assert observed["python"] is None
    assert Path(observed["home"]) == sandbox / "home"


def test_desktop_keeps_the_venv_interpreter_symlink(tmp_path: Path) -> None:
    sandbox = tmp_path / "trial dir"
    output = tmp_path / "launch.json"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "store-python"
    store.write_text("", encoding="utf-8")
    link = tmp_path / "venv-python"
    link.symlink_to(store)
    resolver = bindir / "python"
    resolver.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if sys.argv[1:2] != ['-c']:\n"
        "    raise SystemExit('resolver expected python -c')\n"
        "print(os.environ['EXPECTED_TRIAL_PYTHON'])\n",
        encoding="utf-8",
    )
    resolver.chmod(resolver.stat().st_mode | stat.S_IEXEC)
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os\nfrom pathlib import Path\n"
        "Path(os.environ['HERMES_TRY_PROBE_OUT']).write_text(json.dumps({'python': os.environ.get('HERMES_DESKTOP_PYTHON')}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    env = _base_env(
        sandbox,
        HERMES_TRY_LAUNCHER=str(launcher),
        HERMES_TRY_PROBE_OUT=str(output),
        EXPECTED_TRIAL_PYTHON=str(link),
    )
    env["PATH"] = os.pathsep.join((str(bindir), env.get("PATH", "")))
    result = _run(["desktop"], env)
    assert result.returncode == 0, result.stderr
    observed = json.loads(output.read_text(encoding="utf-8"))
    assert observed["python"] == str(link)
    assert Path(observed["python"]).resolve() == store.resolve()


def test_desktop_rejects_extra_arguments(tmp_path: Path) -> None:
    sandbox = tmp_path / "trial dir"
    result = _run(["desktop", "--local"], _base_env(sandbox))
    assert result.returncode == 1
    assert "only accepts --rebuild" in result.stderr

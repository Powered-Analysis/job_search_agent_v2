import os
import subprocess
import tomllib
from pathlib import Path

import pytest
from conftest import (
    REPO_ROOT,
    TABLES,
    drop_all_tables,
    existing_tables,
    venv_script,
)


def run_jsa(*args, env_overrides=None, unset=(), cwd=None):
    env = {**os.environ, **(env_overrides or {})}
    for name in unset:
        env.pop(name, None)
    return subprocess.run(
        [venv_script("jsa"), *args],
        env=env,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_help_lists_init_db():
    result = run_jsa("--help")
    assert result.returncode == 0
    assert "init-db" in result.stdout


def test_init_db_creates_all_tables_and_is_repeatable(db_url, tmp_path):
    drop_all_tables(db_url)
    for _ in range(2):
        result = run_jsa(
            "init-db", env_overrides={"TURSO_DATABASE_URL": db_url}, cwd=tmp_path
        )
        assert result.returncode == 0, result.stderr
        assert set(TABLES) <= existing_tables(db_url)


def test_init_db_second_run_changes_nothing(db_url, tmp_path):
    from conftest import raw_connect

    drop_all_tables(db_url)
    env = {"TURSO_DATABASE_URL": db_url}
    assert run_jsa("init-db", env_overrides=env, cwd=tmp_path).returncode == 0
    conn = raw_connect(db_url)
    before = conn.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    conn.close()
    assert run_jsa("init-db", env_overrides=env, cwd=tmp_path).returncode == 0
    conn = raw_connect(db_url)
    after = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    conn.close()
    assert before == after


@pytest.mark.parametrize("value", [None, ""])
def test_init_db_without_database_url_fails_naming_variable_and_env_example(
    tmp_path, value
):
    overrides = {} if value is None else {"TURSO_DATABASE_URL": value}
    result = run_jsa(
        "init-db",
        env_overrides=overrides,
        unset=("TURSO_DATABASE_URL",) if value is None else (),
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "TURSO_DATABASE_URL" in result.stderr
    assert ".env.example" in result.stderr


def test_init_db_reads_database_url_from_dotenv(tmp_path):
    (tmp_path / ".env").write_text(
        f"TURSO_DATABASE_URL=file:{tmp_path / 'from_env.db'}\n"
    )
    result = run_jsa("init-db", unset=("TURSO_DATABASE_URL",), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert set(TABLES) <= existing_tables(f"file:{tmp_path / 'from_env.db'}")


def test_real_environment_beats_dotenv(tmp_path):
    (tmp_path / ".env").write_text(
        f"TURSO_DATABASE_URL=file:{tmp_path / 'dotenv.db'}\n"
    )
    real = f"file:{tmp_path / 'real.db'}"
    result = run_jsa(
        "init-db", env_overrides={"TURSO_DATABASE_URL": real}, cwd=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "real.db").exists()
    assert not (tmp_path / "dotenv.db").exists()


# --- project setup -----------------------------------------------------------


def _pyproject():
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())


def test_package_and_console_command():
    project = _pyproject()
    assert project["project"]["scripts"]["jsa"].startswith("jsa.")
    assert (REPO_ROOT / "src" / "jsa" / "__init__.py").exists()


def test_python_314():
    assert "3.14" in _pyproject()["project"]["requires-python"]
    assert _pyproject()["tool"]["ruff"]["target-version"] == "py314"


def test_pytest_and_ruff_are_dev_only():
    pyproject = _pyproject()
    runtime = " ".join(pyproject["project"]["dependencies"]).lower()
    assert "pytest" not in runtime and "ruff" not in runtime
    dev = " ".join(pyproject["dependency-groups"]["dev"]).lower()
    assert "pytest" in dev and "ruff" in dev


def test_ruff_format_and_check_pass():
    for command in (["format", "--check"], ["check"]):
        result = subprocess.run(
            [venv_script("ruff"), *command],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def _ruff_codes(tmp_path: Path, source: str) -> str:
    sample = tmp_path / "sample.py"
    sample.write_text(source)
    result = subprocess.run(
        [
            venv_script("ruff"),
            "check",
            "--config",
            str(REPO_ROOT / "pyproject.toml"),
            "--output-format",
            "concise",
            str(sample),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result.stdout


def test_ruff_dtz_rules_enabled(tmp_path):
    out = _ruff_codes(tmp_path, "import datetime\n\nnow = datetime.datetime.now()\n")
    assert "DTZ005" in out


def test_ruff_up_rules_enabled_for_py314(tmp_path):
    out = _ruff_codes(tmp_path, "from typing import List\n\nx: List[int] = []\n")
    assert "UP006" in out or "UP035" in out


# --- .env.example ------------------------------------------------------------


@pytest.mark.parametrize("variable", ["TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"])
def test_env_example_lists_variable_with_a_comment(variable):
    lines = (REPO_ROOT / ".env.example").read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(f"{variable}="))
    assert index > 0 and lines[index - 1].startswith("#"), (
        f"{variable} has no comment directly above it"
    )


def test_env_example_has_no_secret_values():
    for line in (REPO_ROOT / ".env.example").read_text().splitlines():
        if line.startswith("TURSO_AUTH_TOKEN="):
            assert line == "TURSO_AUTH_TOKEN="

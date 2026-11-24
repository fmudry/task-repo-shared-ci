"""Tests for hack/validate-migration.sh.

The script is sourced in a temporary repository, then single check functions are called on
prepared pipelines, without network access or a cluster.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from textwrap import dedent

import pytest

SCRIPT = Path(__file__).parent.parent / "hack" / "validate-migration.sh"

PIPELINE = dedent("""\
    apiVersion: tekton.dev/v1
    kind: Pipeline
    metadata:
      name: {name}
    spec:
      tasks:
        - name: init
    """)

MIGRATION_APPEND = 'echo "# migrated" >> "$1"\n'

# Sources the script, prepares the pipelines and runs the given check. Arguments: the script, the
# directory of the prepared pipelines, the directory where the results are copied, then the check.
RUNNER = dedent("""\
    source "$1"
    pipelines_dir=$2
    results_dir=$3
    shift 3
    trap 'cp -r "$WORK_DIR/pipelines/." "$results_dir"; rm -rf "$WORK_DIR"' EXIT
    mkdir -p "$WORK_DIR/pipelines/pushed"
    find "$pipelines_dir" -name "*.yaml" -exec cp -t "$WORK_DIR/pipelines/pushed/" {} +
    eval "$@"
    """)


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    pipelines: Path

    def pipeline(self, name: str) -> str:
        return (self.pipelines / "pushed" / name).read_text()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A task repository with the script in hack/."""
    path = tmp_path / "repo"
    (path / "hack").mkdir(parents=True)
    shutil.copy(SCRIPT, path / "hack")
    return path


@pytest.fixture
def run_check(repo: Path, tmp_path: Path):
    def _run(check: str, pipelines: list[str], env: dict[str, str] | None = None) -> Result:
        pipelines_dir = tmp_path / "pipelines"
        pipelines_dir.mkdir(exist_ok=True)
        for name in pipelines:
            (pipelines_dir / f"{name}.yaml").write_text(PIPELINE.format(name=name))
        results_dir = tmp_path / "results"
        results_dir.mkdir(exist_ok=True)
        proc = subprocess.run(
            ["bash", "-c", RUNNER, "runner", repo / "hack" / "validate-migration.sh"]
            + [str(pipelines_dir), str(results_dir), check],
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr, results_dir)

    return _run


@pytest.fixture
def migration(repo: Path):
    def _write(content: str) -> Path:
        path = repo / "task" / "hello" / "0.2" / "migrations" / "0.2.sh"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    return _write


class TestCheckApplyOnPipelines:

    def test_modified_pipelines(self, run_check, migration):
        result = run_check(
            f"check_apply_on_pipelines {migration(MIGRATION_APPEND)}", ["docker-build", "fbc"]
        )
        assert result.returncode == 0, result.stderr
        for name in ("docker-build", "fbc"):
            modified = result.pipeline(f"{name}.yaml.modified")
            assert modified == PIPELINE.format(name=name) + "# migrated\n"

    def test_unmodified_pipelines(self, run_check, migration):
        result = run_check(f"check_apply_on_pipelines {migration('true')}", ["docker-build"])
        assert result.returncode == 0, result.stderr
        assert "does not modify any of pipelines docker-build" in result.stderr
        assert not (result.pipelines / "pushed" / "docker-build.yaml.modified").exists()

    def test_failing_migration(self, run_check, migration):
        result = run_check(f"check_apply_on_pipelines {migration('exit 3')}", ["docker-build"])
        assert result.returncode == 1
        assert "failed to run migration file" in result.stderr
        assert "does not modify any of pipelines" not in result.stderr

    def test_later_failures_still_stop_the_script(self, run_check, migration):
        """Showing the diff of a modified pipeline must not turn off errexit."""
        result = run_check(
            f"check_apply_on_pipelines {migration(MIGRATION_APPEND)}; false; echo still running",
            ["docker-build"],
        )
        assert result.returncode == 1
        assert "still running" not in result.stdout


class TestCheckOciTaMigration:

    @pytest.fixture
    def tasks(self, repo: Path) -> None:
        for task in ("hello", "hello-oci-ta"):
            (repo / "task" / task / "0.2" / "migrations").mkdir(parents=True)

    def test_migration_of_the_oci_ta_variant(self, run_check, repo, tasks):
        migration = "task/hello-oci-ta/0.2/migrations/0.3.sh"
        (repo / migration).write_text("true\n")
        result = run_check(f"check_oci_ta_migration {migration}", [])
        assert result.returncode == 0, result.stderr
        assert "oci-ta-oci-ta" not in result.stderr

    def test_missing_migration_in_the_oci_ta_variant(self, run_check, repo, tasks):
        migration = "task/hello/0.2/migrations/0.3.sh"
        (repo / migration).write_text("true\n")
        result = run_check(f"check_oci_ta_migration {migration}", [])
        assert result.returncode == 1
        assert "task/hello-oci-ta/0.2/migrations/0.3.sh" in result.stderr

    def test_oci_ta_variant_has_the_migration(self, run_check, repo, tasks):
        for task in ("hello", "hello-oci-ta"):
            (repo / "task" / task / "0.2" / "migrations" / "0.3.sh").write_text("true\n")
        result = run_check("check_oci_ta_migration task/hello/0.2/migrations/0.3.sh", [])
        assert result.returncode == 0, result.stderr


# Stands for `pmt run-migration`: runs the migration in a scratch directory with only PATH and
# HOME, like the sandbox, and updates the pipeline file only on success. FAKE_SANDBOX_EXIT makes
# it fail as if the sandbox could not be used.
FAKE_PMT = dedent("""\
    #!/usr/bin/env bash
    set -uo pipefail
    [ "$1" = run-migration ] || exit 2
    [ "$2" = --help ] && exit 0
    if [ -n "${FAKE_SANDBOX_EXIT:-}" ]; then
        echo "error: crun: unshare: Operation not permitted" >&2
        exit "$FAKE_SANDBOX_EXIT"
    fi
    work=$(mktemp -d)
    name=$(basename "$3")
    cp "$2" "$work/script.sh"
    cp "$3" "$work/$name"
    status=0
    (cd "$work" && env -i PATH="$PATH" HOME="$HOME" bash script.sh "$name") || status=$?
    if [ "$status" -eq 0 ]; then
        cp "$work/$name" "$3"
    fi
    rm -rf "$work"
    exit "$status"
    """)

# Works only with a variable of the CI environment, which the sandbox does not pass on
MIGRATION_NEEDS_ENV = 'echo "# ${CI_TOKEN:?}" >> "$1"\n'


class TestSandbox:

    @pytest.fixture
    def env(self, tmp_path: Path) -> dict[str, str]:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "pmt").write_text(FAKE_PMT)
        (bin_dir / "pmt").chmod(0o755)
        return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CI_TOKEN": "secret"}

    @pytest.mark.parametrize("mode", ["warn", "enforce"])
    def test_compatible_migration(self, run_check, migration, env, mode):
        result = run_check(
            f"check_apply_on_pipelines {migration(MIGRATION_APPEND)}",
            ["docker-build"],
            {**env, "MIGRATION_SANDBOX": mode},
        )
        assert result.returncode == 0, result.stderr
        modified = result.pipeline("docker-build.yaml.modified")
        assert modified == PIPELINE.format(name="docker-build") + "# migrated\n"
        assert "::" not in result.stdout
        assert "warning" not in result.stderr

    def test_enforce_fails_on_sandbox_only_failure(self, run_check, migration, env, repo):
        # Relative to the repository root, as main passes it
        migration_file = migration(MIGRATION_NEEDS_ENV).relative_to(repo)
        result = run_check(
            f"check_apply_on_pipelines {migration_file}",
            ["docker-build", "fbc"],
            {**env, "MIGRATION_SANDBOX": "enforce"},
        )
        assert result.returncode == 1
        assert "CI_TOKEN: parameter null or not set" in result.stderr
        assert "sees only PATH and HOME" in result.stderr
        # One annotation per migration file, not per pipeline
        assert result.stdout.count(f"::error file={migration_file},line=1::") == 1
        assert not (result.pipelines / "pushed" / "docker-build.yaml.modified").exists()

    def test_warn_uses_the_result_outside_the_sandbox(self, run_check, migration, env):
        migration_file = migration(MIGRATION_NEEDS_ENV)
        result = run_check(
            f"check_apply_on_pipelines {migration_file}",
            ["docker-build"],
            {**env, "MIGRATION_SANDBOX": "warn"},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.count(f"::warning file={migration_file},line=1::") == 1
        assert "sees only PATH and HOME" in result.stderr
        modified = result.pipeline("docker-build.yaml.modified")
        assert modified == PIPELINE.format(name="docker-build") + "# secret\n"

    @pytest.mark.parametrize("mode", ["warn", "enforce"])
    def test_failing_migration(self, run_check, migration, env, mode):
        result = run_check(
            f"check_apply_on_pipelines {migration('echo broken >&2; exit 3')}",
            ["docker-build"],
            {**env, "MIGRATION_SANDBOX": mode},
        )
        assert result.returncode == 1
        assert "in the sandbox:\nbroken" in result.stderr
        assert "outside the sandbox:\n" in result.stderr
        assert "::" not in result.stdout

    def test_different_result_outside_the_sandbox(self, run_check, migration, env):
        result = run_check(
            f"check_apply_on_pipelines {migration('echo "# ${CI_TOKEN:-none}" >> "$1"')}",
            ["docker-build"],
            {**env, "MIGRATION_SANDBOX": "enforce"},
        )
        assert result.returncode == 0, result.stderr
        assert "different result outside the sandbox" in result.stderr
        modified = result.pipeline("docker-build.yaml.modified")
        assert modified == PIPELINE.format(name="docker-build") + "# none\n"

    def test_enforce_stops_when_the_sandbox_is_unusable(self, run_check, migration, env):
        result = run_check(
            f"check_apply_on_pipelines {migration(MIGRATION_APPEND)}",
            ["docker-build"],
            {**env, "MIGRATION_SANDBOX": "enforce", "FAKE_SANDBOX_EXIT": "125"},
        )
        assert result.returncode == 1
        assert "sandbox cannot be used on this machine" in result.stderr
        assert "unshare: Operation not permitted" in result.stderr

    def test_warn_falls_back_when_the_sandbox_is_unusable(self, run_check, migration, env):
        migration_file = migration(MIGRATION_NEEDS_ENV)
        result = run_check(
            f"check_apply_on_pipelines {migration_file}",
            ["docker-build", "fbc"],
            {**env, "MIGRATION_SANDBOX": "warn", "FAKE_SANDBOX_EXIT": "125"},
        )
        assert result.returncode == 0, result.stderr
        # Reported once, then the sandbox is not tried again
        assert result.stdout.count("::warning") == 1
        assert result.stderr.count("unshare: Operation not permitted") == 1
        for name in ("docker-build", "fbc"):
            modified = result.pipeline(f"{name}.yaml.modified")
            assert modified == PIPELINE.format(name=name) + "# secret\n"

    def test_invalid_mode(self, run_check, migration, env):
        result = run_check(
            f"check_apply_on_pipelines {migration(MIGRATION_APPEND)}",
            ["docker-build"],
            {**env, "MIGRATION_SANDBOX": "yes"},
        )
        assert result.returncode == 1
        assert "MIGRATION_SANDBOX must be off, warn or enforce" in result.stderr

    @pytest.mark.parametrize("mode", ["warn", "enforce"])
    def test_pmt_without_run_migration(self, run_check, migration, env, tmp_path, mode):
        """An old pmt is a problem of the machine, not of the migration."""
        (tmp_path / "bin" / "pmt").write_text(
            "#!/bin/sh\necho \"pmt: error: invalid choice: '$1'\" >&2\nexit 2\n"
        )
        result = run_check(
            f"check_apply_on_pipelines {migration(MIGRATION_APPEND)}",
            ["docker-build", "fbc"],
            {**env, "MIGRATION_SANDBOX": mode},
        )
        assert "invalid choice: 'run-migration'" in result.stderr
        assert "the run-migration subcommand is required" in result.stderr
        assert "fails in the sandbox" not in result.stderr
        if mode == "enforce":
            assert result.returncode == 1
            assert "sandbox cannot be used on this machine" in result.stderr
        else:
            assert result.returncode == 0, result.stderr
            assert result.stdout.count("::warning") == 1
            assert result.pipeline("fbc.yaml.modified").endswith("# migrated\n")

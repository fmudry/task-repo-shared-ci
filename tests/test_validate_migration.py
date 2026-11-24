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
    cp "$pipelines_dir"/*.yaml "$WORK_DIR/pipelines/pushed/"
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

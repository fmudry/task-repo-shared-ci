#!/usr/bin/env python3

# <TEMPLATED FILE!>
# This file comes from the templates at https://github.com/konflux-ci/task-repo-shared-ci.
# Please consider sending a PR upstream instead of editing the file directly.
# See the SHARED-CI.md document in this repo for more details.

"""Run a migration script on a pipeline file inside the pipeline-migration-tool sandbox.

The script runs exactly as MintMaker runs it: in a rootless crun container built from the PMT
runner image, with no /proc, a read-only root file system, only PATH and HOME set and only the
pipeline file's directory writable. The pipeline file is updated only when the script succeeds.

PROTOTYPE: a stand-in for a future `pmt` subcommand. It uses the internal sandbox module of the
installed pipeline-migration-tool and requires crun and crane in PATH.

Usage: run-migration-in-sandbox.py MIGRATION_FILE PIPELINE_FILE

Exit status: the migration script's exit status, 1 when it left no usable pipeline file,
124 on timeout, 125 when the sandbox itself cannot be used (not the script's fault).
"""

import logging
import os
import sys
import tempfile

EXIT_TIMEOUT = 124
EXIT_SANDBOX_ERROR = 125


def self_test(sandbox) -> None:
    """Run a no-op script, so a container that cannot start is not blamed on the migration.

    crun reports its own failures (e.g. user namespaces blocked) as a non-zero exit status with
    its message in the output, like a failing script would.
    """
    with tempfile.TemporaryDirectory(prefix="sandbox-self-test-") as tmp:
        script = os.path.join(tmp, "noop.sh")
        pipeline = os.path.join(tmp, "pipeline.yaml")
        with open(script, "w") as f:
            f.write("exit 0\n")
        with open(pipeline, "w") as f:
            f.write("kind: Pipeline\n")
        proc = sandbox.run_migration(script, pipeline, timeout=60)
        if proc.returncode != 0:
            output = proc.stdout.decode(errors="replace").strip()
            raise sandbox.SandboxError(
                f"a no-op container exited with code {proc.returncode}: {output}"
            )


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} MIGRATION_FILE PIPELINE_FILE", file=sys.stderr)
        return 2
    migration_file, pipeline_file = sys.argv[1:]

    logging.basicConfig(level=logging.INFO, format="sandbox: %(message)s", stream=sys.stderr)
    # Never fall back to running the script directly on the host
    os.environ["PMT_SANDBOX"] = "on"

    try:
        from pipeline_migration.actions.migrate import sandbox
        from pipeline_migration.actions.migrate.constants import MIGRATION_TIMEOUT_SECONDS
    except ImportError as e:
        print(f"error: pipeline-migration-tool with sandbox support not found: {e}", file=sys.stderr)
        return EXIT_SANDBOX_ERROR

    try:
        sandbox.enabled()
        self_test(sandbox)
    except sandbox.SandboxError as e:
        print(f"error: sandbox unavailable: {e}", file=sys.stderr)
        return EXIT_SANDBOX_ERROR

    try:
        proc = sandbox.run_migration(
            migration_file, pipeline_file, timeout=MIGRATION_TIMEOUT_SECONDS
        )
    except TimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_TIMEOUT
    except RuntimeError as e:
        # The script succeeded but left no usable pipeline file (removed, symlink, too big)
        print(f"error: {e}", file=sys.stderr)
        return 1

    sys.stdout.write(proc.stdout.decode(errors="replace"))
    if proc.returncode == EXIT_SANDBOX_ERROR:
        # Do not let a script exit status be mistaken for a sandbox failure
        return 1
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())

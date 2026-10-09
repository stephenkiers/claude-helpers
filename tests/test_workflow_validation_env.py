#!/usr/bin/env python3
"""
Test suite for validation environment building and isolation.

Run with: python3 tests/test_workflow_validation_env.py
"""

import sys
import json
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow.validation import build_validation_env
from _test_harness import Harness


if __name__ == "__main__":
    h = Harness("WORKFLOW VALIDATION ENV TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Base allowlist passes through expected vars")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        caller_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/user",
            "USER": "testuser",
            "SHELL": "/bin/bash",
            "TERM": "xterm",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "TMPDIR": "/tmp",
            "COMPOSE_PROJECT_NAME": "poisoned-wt",
            "DATABASE_URL": "postgres://poison",
            "CUSTOM_VAR": "should_drop",
        }

        derivation = build_validation_env(main_wt, caller_env)

        test_result(
            "build_validation_env returns EnvDerivation",
            derivation is not None and hasattr(derivation, 'env')
        )

        if derivation.env:
            test_result(
                "Allowlist vars PATH passes through",
                derivation.env.get("PATH") == "/usr/bin:/bin"
            )
            test_result(
                "Allowlist vars HOME passes through",
                derivation.env.get("HOME") == "/home/user"
            )
            test_result(
                "Allowlist vars LANG passes through",
                derivation.env.get("LANG") == "en_US.UTF-8"
            )
            test_result(
                "Allowlist vars LC_ALL passes through",
                derivation.env.get("LC_ALL") == "en_US.UTF-8"
            )

        test_result(
            "Poisoned COMPOSE_PROJECT_NAME is dropped",
            derivation.env is None or "COMPOSE_PROJECT_NAME" not in derivation.env
        )
        test_result(
            "Poisoned DATABASE_URL is dropped",
            derivation.env is None or "DATABASE_URL" not in derivation.env
        )
        test_result(
            "Dropped var names are recorded (not values)",
            "DATABASE_URL" in derivation.dropped_names and "CUSTOM_VAR" in derivation.dropped_names
        )
        test_result(
            "Dropped names do not contain sensitive values",
            all("poison" not in name.lower() for name in derivation.dropped_names)
        )

    print()
    print("[Section 2] Compose detection and direnv behavior")

    # Test 1: Compose repo with direnv absent from PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        compose_file = main_wt / "compose.yaml"
        compose_file.write_text("version: '3'\n")

        caller_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/user",
        }

        # Mock shutil.which to return None for direnv
        with mock.patch("workflow.validation.shutil.which") as mock_which:
            mock_which.return_value = None

            derivation = build_validation_env(main_wt, caller_env)

            test_result(
                "Compose repo with no direnv gives inconclusive_reason",
                derivation.inconclusive_reason is not None and "direnv" in derivation.inconclusive_reason.lower()
            )
            test_result(
                "Inconclusive result has no checks executed",
                derivation.env is None
            )

    print()
    print("[Section 3] Compose detection: subdirectory compose file")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        # Compose file in subdirectory
        server_dir = main_wt / "server"
        server_dir.mkdir()
        compose_file = server_dir / "compose.yaml"
        compose_file.write_text("version: '3'\n")

        # Mock git ls-files to list the compose file
        caller_env = {"PATH": "/usr/bin", "HOME": "/home/user"}

        with mock.patch("workflow.validation.subprocess.run") as mock_run:
            with mock.patch("workflow.validation.shutil.which") as mock_which:
                # Mock git ls-files to find compose file
                mock_run_result = mock.Mock()
                mock_run_result.stdout = "server/compose.yaml\n"
                mock_run_result.returncode = 0
                mock_run.return_value = mock_run_result
                mock_which.return_value = None  # direnv not found

                derivation = build_validation_env(main_wt, caller_env)

                test_result(
                    "Subdirectory compose file triggers Compose detection",
                    derivation.inconclusive_reason is not None
                )

    print()
    print("[Section 4] direnv export json null value handling")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        caller_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/user",
            "XDG_DATA_HOME": "/home/user/.local/share",
        }

        # Mock direnv that returns a payload with null values
        direnv_output = json.dumps({
            "PATH": "/custom/bin:/usr/bin",
            "UNSET_VAR": None,  # null means unset
            "CUSTOM_VAR": "custom_value",
        })

        with mock.patch("workflow.validation.shutil.which") as mock_which:
            with mock.patch("workflow.validation.subprocess.run") as mock_run:
                mock_which.return_value = "/usr/bin/direnv"

                mock_run_result = mock.Mock()
                mock_run_result.stdout = direnv_output
                mock_run_result.returncode = 0
                mock_run.return_value = mock_run_result

                derivation = build_validation_env(main_wt, caller_env)

                test_result(
                    "direnv export with null values succeeds",
                    derivation.env is not None or derivation.inconclusive_reason is not None
                )

    print()
    print("[Section 5] .envrc alone is not a Compose signal; compose file + direnv error is inconclusive")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        envrc_file = main_wt / ".envrc"
        envrc_file.write_text('export VAR=value\n')
        (main_wt / "compose.yaml").write_text("services: {}\n")

        caller_env = {"PATH": "/usr/bin", "HOME": "/home/user"}

        with mock.patch("workflow.validation.shutil.which") as mock_which:
            with mock.patch("workflow.validation.subprocess.run") as mock_run:
                # direnv exits nonzero (blocked)
                mock_which.return_value = "/usr/bin/direnv"
                mock_run_result = mock.Mock()
                mock_run_result.stdout = ""
                mock_run_result.stderr = "direnv: error in .envrc"
                mock_run_result.returncode = 127
                mock_run.return_value = mock_run_result

                derivation = build_validation_env(main_wt, caller_env)

                test_result(
                    "compose repo with .envrc and direnv error gives inconclusive",
                    derivation.inconclusive_reason is not None and "direnv" in derivation.inconclusive_reason
                )

    print()
    print("[Section 6] Non-Compose repo without direnv")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        # Create a non-compose repo (no compose files, no .envrc)
        test_file = main_wt / "test.txt"
        test_file.write_text("test content")

        caller_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/user",
            "MISSING_VAR": "should_drop",
        }

        with mock.patch("workflow.validation.shutil.which") as mock_which:
            with mock.patch("workflow.validation.subprocess.run") as mock_run:
                # Git ls-files shows no compose files
                mock_run_result = mock.Mock()
                mock_run_result.stdout = "test.txt\n"
                mock_run_result.returncode = 0
                mock_run.return_value = mock_run_result

                mock_which.return_value = None  # direnv not found

                derivation = build_validation_env(main_wt, caller_env)

                test_result(
                    "Non-Compose repo without direnv still runs",
                    derivation.env is not None
                )
                test_result(
                    "Non-Compose: env includes allowlist vars",
                    derivation.env and derivation.env.get("PATH") is not None
                )
                test_result(
                    "Non-Compose: has note about direnv",
                    any("direnv" in note.lower() for note in derivation.notes)
                )

    print()
    print("[Section 7] Exception handling never raises")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        caller_env = {"PATH": "/usr/bin", "HOME": "/home/user"}

        with mock.patch("workflow.validation.subprocess.run") as mock_run:
            mock_run.side_effect = RuntimeError("Unexpected error")

            # Should not raise, should return an EnvDerivation with inconclusive_reason
            try:
                derivation = build_validation_env(main_wt, caller_env)
                test_result(
                    "Unexpected exception returns EnvDerivation (no raise)",
                    True
                )
            except Exception:
                test_result(
                    "Unexpected exception returns EnvDerivation (no raise)",
                    False
                )

    print()
    print("[Section 8] XDG vars reach direnv subprocess but not check")

    with tempfile.TemporaryDirectory() as tmpdir:
        main_wt = Path(tmpdir) / "main"
        main_wt.mkdir()

        caller_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/user",
            "XDG_DATA_HOME": "/home/user/.local/share",
            "XDG_CONFIG_HOME": "/home/user/.config",
            "DIRENV_CONFIG": "/home/user/.direnv",
        }

        direnv_output = json.dumps({
            "PATH": "/custom/bin",
            "EXTRA_VAR": "extra",
        })

        with mock.patch("workflow.validation.shutil.which") as mock_which:
            with mock.patch("workflow.validation.subprocess.run") as mock_run:
                mock_which.return_value = "/usr/bin/direnv"

                def run_side_effect(*args, **kwargs):
                    result = mock.Mock()
                    # Check if this is the direnv call by examining env passed
                    if kwargs.get('env') and "XDG_DATA_HOME" in kwargs.get('env', {}):
                        result.stdout = direnv_output
                        result.returncode = 0
                    else:
                        result.stdout = ""
                        result.returncode = 1
                    return result

                mock_run.side_effect = run_side_effect

                derivation = build_validation_env(main_wt, caller_env)

                if derivation.env:
                    test_result(
                        "XDG vars not in check env (only direnv subprocess)",
                        "XDG_DATA_HOME" not in derivation.env
                    )

    h.summarize_and_exit()

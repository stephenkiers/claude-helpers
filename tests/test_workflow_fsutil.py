#!/usr/bin/env python3
"""
Test suite for scripts/workflow/fsutil.py helpers (#442).

Tests the JSON reading and private file creation helpers that are now shared
between heal.py, deps.py, and other modules.

Run with: python3 tests/test_workflow_fsutil.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from workflow import fsutil
from _test_harness import Harness


def main():
    h = Harness("WORKFLOW FSUTIL TEST SUITE")

    print("[Section 1] read_json_object() — JSON object reading")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Happy path: valid JSON object
        obj_file = tmp_path / "valid.json"
        test_obj = {"key": "value", "number": 42, "nested": {"inner": True}}
        obj_file.write_text(json.dumps(test_obj))
        result = fsutil.read_json_object(obj_file)
        h.test_result(
            "read_json_object reads a valid JSON object",
            result == test_obj,
            f"got {result}",
        )

        # Happy path: empty object
        empty_file = tmp_path / "empty.json"
        empty_file.write_text(json.dumps({}))
        result = fsutil.read_json_object(empty_file)
        h.test_result(
            "read_json_object reads an empty JSON object",
            result == {},
            f"got {result}",
        )

        # Happy path: object with various types
        types_file = tmp_path / "types.json"
        test_types = {
            "string": "hello",
            "number": 123,
            "float": 3.14,
            "bool": True,
            "null": None,
            "array": [1, 2, 3],
            "object": {"a": "b"},
        }
        types_file.write_text(json.dumps(test_types))
        result = fsutil.read_json_object(types_file)
        h.test_result(
            "read_json_object preserves various JSON types",
            result == test_types,
            f"got {result}",
        )

        # Edge case: malformed JSON
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not valid json}")
        result = fsutil.read_json_object(bad_file)
        h.test_result(
            "read_json_object returns None for malformed JSON",
            result is None,
            f"got {result}",
        )

        # Edge case: JSON array (not an object)
        array_file = tmp_path / "array.json"
        array_file.write_text(json.dumps([1, 2, 3]))
        result = fsutil.read_json_object(array_file)
        h.test_result(
            "read_json_object returns None for JSON array",
            result is None,
            f"got {result}",
        )

        # Edge case: JSON string (not an object)
        string_file = tmp_path / "string.json"
        string_file.write_text(json.dumps("just a string"))
        result = fsutil.read_json_object(string_file)
        h.test_result(
            "read_json_object returns None for JSON string",
            result is None,
            f"got {result}",
        )

        # Edge case: missing file
        missing_file = tmp_path / "missing.json"
        result = fsutil.read_json_object(missing_file)
        h.test_result(
            "read_json_object returns None for missing file",
            result is None,
            f"got {result}",
        )

        # Edge case: empty file
        empty_txt = tmp_path / "empty.txt"
        empty_txt.write_text("")
        result = fsutil.read_json_object(empty_txt)
        h.test_result(
            "read_json_object returns None for empty file",
            result is None,
            f"got {result}",
        )

        # Edge case: file with only whitespace
        whitespace_file = tmp_path / "whitespace.json"
        whitespace_file.write_text("   \n  \t  \n")
        result = fsutil.read_json_object(whitespace_file)
        h.test_result(
            "read_json_object returns None for whitespace-only file",
            result is None,
            f"got {result}",
        )

        # Edge case: JSON null literal
        null_file = tmp_path / "null.json"
        null_file.write_text("null")
        result = fsutil.read_json_object(null_file)
        h.test_result(
            "read_json_object returns None for JSON null",
            result is None,
            f"got {result}",
        )

    print()
    print("[Section 2] open_private_exclusive() — private file creation")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Happy path: create a new private file
        private_file = tmp_path / "private.txt"
        fd = fsutil.open_private_exclusive(private_file)
        h.test_result(
            "open_private_exclusive creates a file and returns a file descriptor",
            fd is not None and isinstance(fd, int) and fd >= 0,
            f"got {fd}",
        )

        # Check file permissions are 0600
        if fd is not None and fd >= 0:
            os.close(fd)
            mode = private_file.stat().st_mode & 0o777
            h.test_result(
                "open_private_exclusive creates file with mode 0600",
                mode == 0o600,
                f"got {oct(mode)}",
            )

        # Edge case: parent directories don't exist
        nested_file = tmp_path / "parent" / "subdir" / "private.txt"
        try:
            fd = fsutil.open_private_exclusive(nested_file)
            h.test_result(
                "open_private_exclusive raises FileNotFoundError if parent directories don't exist",
                False,
                f"expected exception but got fd={fd}",
            )
        except FileNotFoundError:
            h.test_result(
                "open_private_exclusive raises FileNotFoundError if parent directories don't exist",
                True,
            )

        # Happy path: create in an existing directory with proper mode
        created_dir = tmp_path / "mydir"
        created_dir.mkdir(mode=0o700)
        nested_file2 = created_dir / "private.txt"
        fd = fsutil.open_private_exclusive(nested_file2)
        h.test_result(
            "open_private_exclusive creates file in existing directory",
            fd is not None and isinstance(fd, int) and fd >= 0,
            f"got {fd}",
        )

        if fd is not None and fd >= 0:
            os.close(fd)
            # Check directory mode is preserved
            dir_mode = created_dir.stat().st_mode & 0o777
            h.test_result(
                f"existing directory mode is preserved (0700)",
                dir_mode == 0o700,
                f"got {oct(dir_mode)}",
            )

        # Edge case: file already exists (exclusive create should fail)
        existing_file = tmp_path / "existing.txt"
        existing_file.write_text("already here")
        try:
            fd = fsutil.open_private_exclusive(existing_file)
            h.test_result(
                "open_private_exclusive raises FileExistsError if file exists",
                False,
                f"expected exception but got fd={fd}",
            )
        except FileExistsError:
            h.test_result(
                "open_private_exclusive raises FileExistsError if file exists",
                True,
            )

        # Verify the existing file wasn't modified
        h.test_result(
            "existing file content is not modified by exclusive create attempt",
            existing_file.read_text() == "already here",
        )

        # Edge case: symlink at the target path
        symlink_target = tmp_path / "target.txt"
        symlink_target.write_text("target")
        symlink_file = tmp_path / "symlink.txt"
        os.symlink(str(symlink_target), str(symlink_file))
        try:
            fd = fsutil.open_private_exclusive(symlink_file)
            h.test_result(
                "open_private_exclusive refuses to create over a symlink",
                False,
                f"expected exception but got fd={fd}",
            )
        except (OSError, FileExistsError):
            h.test_result(
                "open_private_exclusive refuses to create over a symlink (raises exception)",
                True,
            )

        # Verify the symlink target wasn't modified
        h.test_result(
            "symlink target is not modified by attempt",
            symlink_target.read_text() == "target",
        )

        # Happy path: writing to the opened file descriptor
        writable_file = tmp_path / "written.txt"
        fd = fsutil.open_private_exclusive(writable_file)
        if fd is not None and fd >= 0:
            os.write(fd, b"test content\n")
            os.close(fd)
            h.test_result(
                "data written to the file descriptor persists",
                writable_file.read_text() == "test content\n",
            )

    print()
    print("[Section 3] Shared helper semantics")

    # These tests verify that the helpers follow the expected semantics
    # for JSON reading and private file creation as described in the plan.

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Verify read_json_object returns a dict or None (no exceptions)
        bad_json = tmp_path / "bad.json"
        bad_json.write_text("{broken")
        try:
            result = fsutil.read_json_object(bad_json)
            h.test_result(
                "read_json_object never raises, returns None on error",
                result is None,
            )
        except Exception as e:
            h.test_result(
                "read_json_object never raises, returns None on error",
                False,
                f"raised {type(e).__name__}: {e}",
            )

        # Verify open_private_exclusive never overwrites
        exclusive_file = tmp_path / "exclusive.txt"
        exclusive_file.write_text("KEEP THIS")
        try:
            result = fsutil.open_private_exclusive(exclusive_file)
            h.test_result(
                "open_private_exclusive is exclusive (raises exception, never overwrites)",
                False,
                f"expected exception but got fd={result}",
            )
        except (FileExistsError, OSError):
            h.test_result(
                "open_private_exclusive is exclusive (raises exception, never overwrites)",
                exclusive_file.read_text() == "KEEP THIS",
                f"file content was {exclusive_file.read_text()!r}",
            )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()

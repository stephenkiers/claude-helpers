"""Run repo CLI scripts in-process and build git fixture repos once per test file.

`run_python(argv, env=, cwd=, input=)` is a drop-in for
`subprocess.run([sys.executable, *argv], capture_output=True, text=True, ...)`: it executes the
script (or `-m module`) in this interpreter with fresh module globals each call, captured
stdin/stdout/stderr, and env/cwd/argv/sys.path restored afterwards. The compiled code object is
cached per (path, mtime), so a 5,000-line script is compiled once per test file, not once per call.

Not equivalent to a subprocess for: signals, real file descriptors (os.write(1, ...)), threads that
outlive the call, or modules the script imports (imported once and shared across calls). Keep at
least one real-subprocess (`_e2e`) call per CLI so the end-to-end path stays covered.

`RepoTemplate(build).copy(dest)` runs `build(root)` once into a temp dir, then copytree's it to each
`dest` (commit SHAs are preserved). `fix_paths` lists files whose embedded absolute template path
must be rewritten to `dest` (e.g. a worktree's `.git` pointer file).
"""

import atexit
import io
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path

_CODE = {}


def _code_for(path):
    key = (path, os.stat(path).st_mtime_ns)
    code = _CODE.get(key)
    if code is None:
        with open(path, "rb") as f:
            code = compile(f.read(), path, "exec")
        _CODE[key] = code
    return code


def run_python(argv, *, env=None, cwd=None, input=None, text=True):
    argv = [str(a) for a in argv]
    if input is None:
        data = b""
    elif isinstance(input, str):
        data = input.encode("utf-8")
    else:
        data = input
    saved_env, saved_cwd = dict(os.environ), os.getcwd()
    saved_argv, saved_path = sys.argv, list(sys.path)
    saved_streams = (sys.stdin, sys.stdout, sys.stderr)
    saved_main = sys.modules.get("__main__")
    out_b, err_b = io.BytesIO(), io.BytesIO()
    sys.stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
    sys.stdout = io.TextIOWrapper(out_b, encoding="utf-8", write_through=True)
    sys.stderr = io.TextIOWrapper(err_b, encoding="utf-8", errors="backslashreplace", write_through=True)
    rc = 0
    try:
        if env is not None:
            os.environ.clear()
            os.environ.update({str(k): str(v) for k, v in env.items()})
        time.tzset()
        if cwd is not None:
            os.chdir(str(cwd))
        if argv[0] == "-m":
            sys.argv = [argv[1], *argv[2:]]
            sys.path.insert(0, os.getcwd())
            runpy.run_module(argv[1], run_name="__main__", alter_sys=True)
        else:
            path = os.path.abspath(argv[0])
            sys.argv = list(argv)
            sys.path.insert(0, os.path.dirname(path))
            mod = types.ModuleType("__main__")
            mod.__file__ = path
            sys.modules["__main__"] = mod
            exec(_code_for(path), mod.__dict__)
    except SystemExit as e:
        if e.code is None:
            rc = 0
        elif isinstance(e.code, int):
            rc = e.code
        else:
            print(e.code, file=sys.stderr)
            rc = 1
    except Exception:
        traceback.print_exc()
        rc = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        out, err = out_b.getvalue(), err_b.getvalue()
        sys.stdin, sys.stdout, sys.stderr = saved_streams
        sys.argv = saved_argv
        sys.path[:] = saved_path
        if saved_main is not None:
            sys.modules["__main__"] = saved_main
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_env)
        time.tzset()
    if text:
        out, err = out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
    return subprocess.CompletedProcess([sys.executable, *argv], rc, out, err)


class RepoTemplate:
    def __init__(self, build, fix_paths=()):
        self._build, self._fix = build, list(fix_paths)
        self._root, self.meta = None, None

    def copy(self, dest):
        if self._root is None:
            root = tempfile.mkdtemp(prefix="repo-template-")
            atexit.register(shutil.rmtree, root, True)
            self.meta = self._build(Path(root))
            self._root = root
        dest = Path(dest)
        shutil.copytree(self._root, dest, symlinks=True, dirs_exist_ok=dest.exists())
        for rel in self._fix:
            p = dest / rel
            s = p.read_text()
            s = s.replace(os.path.realpath(self._root), os.path.realpath(dest))
            s = s.replace(self._root, str(dest))
            p.write_text(s)
        return dest

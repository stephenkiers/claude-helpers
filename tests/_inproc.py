"""Run repo CLI scripts in-process and build git fixture repos once per test file.

`run_python(argv, env=, cwd=, input=, replace_env=)` is a drop-in for
`subprocess.run([sys.executable, *argv], capture_output=True, text=True, ...)`: it executes the
script (or `-m module`) in this interpreter with fresh module globals each call, captured
stdin/stdout/stderr, and the process state restored afterwards (see `state_snapshot`). The compiled
code object is cached per (path, mtime), so a 5,000-line script is compiled once per test file, not
once per call.

`env=` overlays `os.environ` by default: keys given in `env` are set and every other inherited key
stays. Pass `replace_env=True` to make `env` the whole environment. Use it when a caller removes an
inherited key (e.g. CLAUDE_CODE_SESSION_ID), because an overlay would put that key back.
`replace_env` is ignored when `env` is None (the inherited environment is used as is).

Single-threaded only: os.environ, the cwd, sys.argv/sys.path, the std streams and sys.modules are
process-global, so run one `run_python` at a time per process. A nested call raises RuntimeError.

Not equivalent to a subprocess for: signals, real file descriptors (os.write(1, ...)), threads that
outlive the call, or exit-code parity beyond the basics (a SystemExit code, or 1 for an uncaught
exception, becomes rc; the exit-code contract itself is covered by the `_e2e` tests). Startup-only
settings are ignored in-process: PYTHONHASHSEED, PYTHONPATH, -X options and the like are read once
when an interpreter starts, so putting them in `env` has no effect here. Code that reads os.environ
at call time does see the overlay. Modules a script imports are dropped from sys.modules afterwards
and re-import on the next call. Keep at least one real-subprocess (`_e2e`) call per CLI so the
end-to-end path stays covered.

`RepoTemplate(build).copy(dest)` runs `build(root)` once into a temp dir, then copytree's it to each
`dest` (commit SHAs are preserved). `fix_paths` lists files whose embedded absolute template path
must be rewritten to `dest` (e.g. a worktree's `.git` pointer file). Only the listed files are
rewritten: an absolute template path embedded anywhere else is copied verbatim and still points at
the template, so list every file that embeds one.
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
from contextlib import contextmanager
from pathlib import Path

_CODE = {}
_ACTIVE = False  # True while a run_python call is in progress (single-threaded guard)


def _code_for(path):
    key = (path, os.stat(path).st_mtime_ns)
    code = _CODE.get(key)
    if code is None:
        with open(path, "rb") as f:
            code = compile(f.read(), path, "exec")
        _CODE[key] = code
    return code


@contextmanager
def state_snapshot():
    """Restore process state on exit, including when the block raises: os.environ, the cwd,
    sys.argv, sys.path, sys.stdin/stdout/stderr, sys.modules['__main__'], and sys.modules itself
    (modules added inside the block are dropped, so the next import runs them fresh; modules
    removed or replaced inside it are put back)."""
    saved_env, saved_cwd = dict(os.environ), os.getcwd()
    saved_argv, saved_path = sys.argv, list(sys.path)
    saved_streams = (sys.stdin, sys.stdout, sys.stderr)
    saved_modules = dict(sys.modules)
    try:
        yield
    finally:
        sys.stdin, sys.stdout, sys.stderr = saved_streams
        sys.argv = saved_argv
        sys.path[:] = saved_path
        for name in list(sys.modules):
            if name not in saved_modules:
                del sys.modules[name]
        sys.modules.update(saved_modules)  # undo removals and replacements too
        os.environ.clear()
        os.environ.update(saved_env)
        os.chdir(saved_cwd)
        time.tzset()


def run_python(argv, *, env=None, cwd=None, input=None, text=True, replace_env=False):
    global _ACTIVE
    if _ACTIVE:
        raise RuntimeError("run_python is single-threaded and not reentrant: "
                           "a call is already in progress in this process")
    _ACTIVE = True
    try:
        return _run_python(argv, env=env, cwd=cwd, input=input, text=text, replace_env=replace_env)
    finally:
        _ACTIVE = False


def _run_python(argv, *, env, cwd, input, text, replace_env):
    argv = [str(a) for a in argv]
    if input is None:
        data = b""
    elif isinstance(input, str):
        data = input.encode("utf-8")
    else:
        data = input
    out_b, err_b = io.BytesIO(), io.BytesIO()
    rc = 0
    out, err = b"", b""
    with state_snapshot():
        sys.stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")
        sys.stdout = io.TextIOWrapper(out_b, encoding="utf-8", write_through=True)
        sys.stderr = io.TextIOWrapper(err_b, encoding="utf-8", errors="backslashreplace", write_through=True)
        try:
            if env is not None:
                if replace_env:
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

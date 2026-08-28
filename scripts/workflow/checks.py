"""
Toolchain and check detection, and check execution for /shipit.

Phase 1: detection only (read-only). Phase 2: execution for /shipit.
"""

import json
import os
import signal
import subprocess
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Dict, Optional


class ToolchainDetector:
    """Detect present toolchains from config files in repo root."""

    @staticmethod
    def detect_typescript(repo_root: Path) -> bool:
        """Detect TypeScript from tsconfig.json."""
        return (repo_root / "tsconfig.json").exists()

    @staticmethod
    def detect_eslint(repo_root: Path) -> bool:
        """Detect ESLint from eslint config or eslint.config.js."""
        configs = [
            ".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.yml",
            ".eslintrc.yaml", "eslint.config.js"
        ]
        return any((repo_root / cfg).exists() for cfg in configs)

    @staticmethod
    def detect_prettier(repo_root: Path) -> bool:
        """Detect Prettier from config files."""
        configs = [
            ".prettierrc", ".prettierrc.json", ".prettierrc.js",
            ".prettierrc.cjs", ".prettierrc.yml", ".prettierrc.yaml",
            "prettier.config.js", "prettier.config.cjs"
        ]
        return any((repo_root / cfg).exists() for cfg in configs)

    @staticmethod
    def detect_python(repo_root: Path) -> bool:
        """Detect Python from pyproject.toml."""
        return (repo_root / "pyproject.toml").exists()

    @staticmethod
    def detect_rust(repo_root: Path) -> bool:
        """Detect Rust from Cargo.toml."""
        return (repo_root / "Cargo.toml").exists()

    @staticmethod
    def detect_node(repo_root: Path) -> bool:
        """Detect Node.js from package.json."""
        return (repo_root / "package.json").exists()

    @staticmethod
    def detect_editorconfig(repo_root: Path) -> bool:
        """Detect EditorConfig."""
        return (repo_root / ".editorconfig").exists()

    @staticmethod
    def detect_biome(repo_root: Path) -> bool:
        """Detect Biome from biome.json."""
        return (repo_root / "biome.json").exists()

    @staticmethod
    def get_npm_scripts(repo_root: Path) -> List[str]:
        """Extract test/check scripts from package.json if present."""
        scripts = []
        pkg_json = repo_root / "package.json"
        if pkg_json.exists():
            try:
                data = json.loads(pkg_json.read_text())
                package_scripts = data.get("scripts", {})
                if "test" in package_scripts:
                    scripts.append("test")
                if "lint" in package_scripts:
                    scripts.append("lint")
                if "type-check" in package_scripts:
                    scripts.append("type-check")
            except (json.JSONDecodeError, OSError):
                pass
        return scripts


def detect_toolchains(repo_root: Path) -> Dict[str, bool]:
    """
    Detect all present toolchains.

    Returns dict mapping toolchain names to presence (True/False).
    """
    detector = ToolchainDetector()
    return {
        "typescript": detector.detect_typescript(repo_root),
        "eslint": detector.detect_eslint(repo_root),
        "prettier": detector.detect_prettier(repo_root),
        "python": detector.detect_python(repo_root),
        "rust": detector.detect_rust(repo_root),
        "node": detector.detect_node(repo_root),
        "editorconfig": detector.detect_editorconfig(repo_root),
        "biome": detector.detect_biome(repo_root),
    }


def detect_checks(repo_root: Path) -> Dict[str, str]:
    """
    Detect checks that would run, ordered by precedence.

    Returns dict mapping check names to their commands (not executed).
    Order matters — tests first, then lints, then type-checks.
    """
    detector = ToolchainDetector()
    checks = {}

    npm_scripts = detector.get_npm_scripts(repo_root)
    if "test" in npm_scripts:
        checks["npm_test"] = "npm test"
    if "lint" in npm_scripts:
        checks["npm_lint"] = "npm run lint"
    if "type-check" in npm_scripts:
        checks["npm_typecheck"] = "npm run type-check"

    if detector.detect_rust(repo_root):
        checks["cargo_test"] = "cargo test"

    if detector.detect_python(repo_root):
        checks["pytest"] = "pytest"

    return checks


@dataclass
class CheckResult:
    """Result of executing a check command."""
    success: bool
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


@dataclass
class CheckStepResult:
    """Result of a single check step in the execution sequence."""
    command_type: str
    command: str
    success: bool
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None


@dataclass
class CheckResults:
    """Results of executing all checks for a /shipit run."""
    results: List[CheckStepResult] = field(default_factory=list)
    all_passed: bool = True
    failed_at: Optional[str] = None

    def to_dict(self):
        """Convert to dict for JSON serialization."""
        return {
            "results": [asdict(r) for r in self.results],
            "all_passed": self.all_passed,
            "failed_at": self.failed_at
        }


def execute_check(cmd: str, cwd: Optional[Path], timeout: int = 300) -> CheckResult:
    """
    Execute a shell check command, capturing output. Never raises.

    Runs in its own process group so a timeout can kill the whole
    process tree (a check command that spawns children would otherwise
    orphan them on TimeoutExpired).
    """
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=cwd, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            return CheckResult(success=False, error=f"timed out after {timeout}s")
        return CheckResult(success=proc.returncode == 0, returncode=proc.returncode, stdout=stdout, stderr=stderr)
    except Exception as e:
        return CheckResult(success=False, error=str(e))


def run_checks(
    commands: Dict[str, Optional[str]],
    repo_root: Path,
    parallelizable: Optional[List[str]] = None,
    timeout: int = 300
) -> CheckResults:
    """
    Execute checks in order (format → check → parallelizable → build), stopping at first failure.

    Cached check commands execute via shell=True (intentional for shell syntax like && and pipes).
    Trust boundary: .claude/repo-cache.json content is repo-committer-controlled, not PR/attacker input.

    Args:
        commands: Dict of command type → command string (from cache).
        repo_root: Path to repo root for cwd of executed commands.
        parallelizable: List of command types to run in the "parallelizable" group (typically lint, vet, typecheck, test).
        timeout: Timeout per command in seconds (default 300).

    Returns:
        CheckResults with ordered list of per-command results, all_passed, and failed_at fields.
    """
    if parallelizable is None:
        parallelizable = []

    results = CheckResults()
    order = ["format", "check"]
    effective_parallelizable = list(parallelizable) if parallelizable else []
    if commands.get("check"):
        effective_parallelizable = [c for c in effective_parallelizable if c not in ("lint", "typecheck")]
    order.extend(effective_parallelizable)
    order.append("build")

    for cmd_type in order:
        if cmd_type not in commands or commands[cmd_type] is None:
            continue

        cmd = commands[cmd_type]
        check_result = execute_check(cmd, cwd=repo_root, timeout=timeout)

        step_result = CheckStepResult(
            command_type=cmd_type,
            command=cmd,
            success=check_result.success,
            returncode=check_result.returncode,
            stdout=check_result.stdout,
            stderr=check_result.stderr,
            error=check_result.error
        )
        results.results.append(step_result)

        if not check_result.success:
            results.all_passed = False
            results.failed_at = cmd_type
            break

    return results

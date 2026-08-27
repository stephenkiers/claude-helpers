"""
Toolchain and check detection.

Phase 1: detection only (read-only). Execution comes in Phase 2+.
"""

from pathlib import Path
from typing import List, Dict, Optional
import json


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

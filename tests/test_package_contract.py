from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_toolchain import TOOL_NAME, __version__, git_io, json_io
from agent_toolchain import tool_skill_sync as sync
from agent_toolchain import tool_temp_dir as temp_tool
from agent_toolchain.errors import ErrorCode, FinalizerError, ToolchainError


class PackageAndCliContractTests(unittest.TestCase):
    def test_pyproject_metadata_and_scripts_contract(self) -> None:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["build-system"]["build-backend"], "uv_build")
        project = data["project"]
        self.assertEqual(project["name"], "coding-agent-toolchain")
        self.assertEqual(project["version"], "1.0.1")
        self.assertEqual(project["version"], __version__)
        self.assertEqual(TOOL_NAME, "agent-toolchain")
        self.assertEqual(project["requires-python"], ">=3.12,<3.15")
        self.assertEqual(project["license"], "Apache-2.0")
        self.assertEqual(project["dependencies"], [])
        self.assertEqual(
            project["scripts"],
            {
                "tool-skill-sync": "agent_toolchain.tool_skill_sync:main",
                "tool-temp-dir": "agent_toolchain.tool_temp_dir:main",
            },
        )
        self.assertEqual(
            project["urls"],
            {
                "Repository": "https://github.com/xuanheng-tech/agent-toolchain",
                "Issues": "https://github.com/xuanheng-tech/agent-toolchain/issues",
            },
        )

    def test_package_is_decoupled_from_git_finalizer_delivery(self) -> None:
        pkg_dir = SRC / "agent_toolchain"
        py_files = sorted(pkg_dir.glob("*.py"))
        self.assertEqual(
            [p.name for p in py_files],
            [
                "__init__.py",
                "errors.py",
                "git_io.py",
                "json_io.py",
                "tool_skill_sync.py",
                "tool_temp_dir.py",
                "workflow_health.py",
            ],
        )
        for path in py_files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertFalse(
                            alias.name.startswith("git_finalizer"),
                            f"{path.name} unexpectedly imports {alias.name}",
                        )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertFalse(
                        node.module.startswith("git_finalizer"),
                        f"{path.name} unexpectedly imports from {node.module}",
                    )

    def test_root_wrappers_are_executable_and_report_version(self) -> None:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(ROOT),
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        for name in ("tool-skill-sync", "tool-temp-dir"):
            wrapper = ROOT / name
            self.assertTrue(wrapper.is_file(), f"missing wrapper {name}")
            self.assertTrue(os.access(wrapper, os.X_OK), f"{name} is not executable")
            res = subprocess.run(
                [str(wrapper), "--version"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(res.stdout.strip(), f"{name} {__version__}")

    def test_compatibility_and_manifests_align_with_v6_schema(self) -> None:
        compat_path = ROOT / "toolchain_compatibility.json"
        compat = sync.read_json(compat_path)
        self.assertEqual(compat["schema_version"], 1)
        self.assertEqual(compat["toolchain_contract_version"], 6)
        self.assertEqual(compat["context_loader_contract_version"], 4)
        self.assertEqual(compat["snapshot_runner_contract_version"], 5)
        self.assertEqual(compat["git_finalizer_contract_version"], 10)
        self.assertEqual(compat["worktree_controller_adapter_version"], 3)
        self.assertEqual(compat["worktree_controller_status"], "available_external")
        sync.validate_controller_requirements(
            compat["runtime_requirements"]["worktree-controller"]
        )

        expected_versions = {
            "context-loader": "1.4.0",
            "snapshot-runner": "2.7.0",
            "git-finalizer": "2.0.1",
        }
        skill_hashes: set[str] = set()
        compat_skill_hashes: set[str] = set()
        for tool, expected_version in expected_versions.items():
            manifest_path = ROOT / "manifests" / tool / "tool_skill_manifest.json"
            manifest = sync.read_json(manifest_path)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["tool_name"], tool)
            self.assertEqual(manifest["tool_version"], expected_version)
            self.assertEqual(manifest["tool_commit"], "@release")
            self.assertEqual(manifest["compatible_toolchain_contract_version"], 6)
            self.assertEqual(manifest["skill_contract_version"], 2)
            sync.validate_manifest_static_fields(manifest)
            sync.validate_install_targets(manifest)
            skill_hashes.add(manifest["canonical_skill_sha256"])
            compat_skill_hashes.add(manifest["compatibility_skill"]["sha256"])

        self.assertEqual(len(skill_hashes), 1)
        self.assertEqual(len(compat_skill_hashes), 1)
        self.assertIn(next(iter(skill_hashes)), sync.RELEASED_CANONICAL_SKILL_SHA256)

    def test_errors_git_io_and_json_io_primitives(self) -> None:
        err = ToolchainError(ErrorCode.TEMP_DIR_FAILED, "bad temp dir")
        self.assertIsInstance(err, FinalizerError)
        self.assertEqual(err.code, "TEMP_DIR_FAILED")
        self.assertEqual(err.status, "blocked")
        with self.assertRaises(ValueError):
            ToolchainError("not-valid", "bad code")

        self.assertEqual(
            json_io.loads_unique('{"a": 1, "b": 2}'),
            {"a": 1, "b": 2},
        )
        with self.assertRaises(json_io.DuplicateKeyError):
            json_io.loads_unique('{"a": 1, "a": 2}')
        with self.assertRaises(json_io.NonFiniteConstantError):
            json_io.loads_unique('{"x": NaN}')

        encoded = json_io.canonical_json({"b": 1, "a": 2})
        self.assertEqual(encoded, b'{\n  "a": 2,\n  "b": 1\n}\n')
        self.assertEqual(len(json_io.sha256_bytes(encoded)), 64)

        self.assertEqual(git_io.validate_oid("a" * 40), "a" * 40)
        with self.assertRaises(ToolchainError):
            git_io.validate_oid("not-an-oid")
        self.assertEqual(git_io.validate_remote_name("origin"), "origin")
        with self.assertRaises(ToolchainError):
            git_io.validate_remote_name("../bad")
        self.assertEqual(git_io.validate_branch_name("feat/test"), "feat/test")
        with self.assertRaises(ToolchainError):
            git_io.validate_branch_name("refs/heads/main")
        self.assertEqual(git_io.parse_nul_records(b"a\x00b\x00"), ["a", "b"])
        self.assertTrue(git_io.is_binary_bytes(b"abc\x00def"))
        self.assertFalse(git_io.is_binary_bytes(b"plain text\n"))

        with tempfile.TemporaryDirectory() as td:
            sample = Path(td) / "sample.bin"
            sample.write_bytes(b"hello\x00world")
            self.assertEqual(git_io.inspect_file_stream(sample), (True, 11))
            self.assertEqual(json_io.sha256_file(sample), json_io.sha256_bytes(b"hello\x00world"))

    def test_governance_files_and_workflows_and_no_private_markers(self) -> None:
        required = (
            "README.md",
            "CHANGELOG.md",
            "LICENSE",
            "SECURITY.md",
            "CONTRIBUTING.md",
            ".gitignore",
            ".python-version",
            "justfile",
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml",
            ".github/workflows/publish-pypi.yml",
        )
        for rel in required:
            self.assertTrue((ROOT / rel).is_file(), f"missing governance file: {rel}")

        self.assertEqual((ROOT / ".python-version").read_text(encoding="utf-8").strip(), "3.12")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## 1.0.1 - 2026-11-22", changelog)
        self.assertIn("## 1.0.0 - 2026-11-20", changelog)

        publish_yml = (ROOT / ".github/workflows/publish-pypi.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", publish_yml)
        self.assertIn("environment: pypi", publish_yml)
        self.assertIn("id-token: write", publish_yml)
        self.assertIn(
            "pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
            publish_yml,
        )
        self.assertIn("https://pypi.org/pypi/coding-agent-toolchain/", publish_yml)

        # Ensure repository files remain free of local/private workspace identifiers.
        banned_patterns = (
            re.compile(r"/home/" + "hsd"),
            re.compile(r"quant-" + "platform"),
            re.compile(r"QUANT_" + "JQ"),
            re.compile(r"\b" + "Q" + r"P\b"),
        )
        for file_path in sorted(ROOT.rglob("*")):
            if not file_path.is_file():
                continue
            rel = file_path.relative_to(ROOT)
            if rel.parts[0] in {".git", ".venv", ".ruff_cache", ".pytest_cache", "__pycache__"}:
                continue
            if "__pycache__" in rel.parts:
                continue
            content = file_path.read_text(encoding="utf-8")
            for pattern in banned_patterns:
                self.assertIsNone(
                    pattern.search(content),
                    f"unexpected private marker {pattern.pattern!r} in {rel}",
                )

    def test_tool_temp_dir_module_version_matches_package_version(self) -> None:
        self.assertEqual(temp_tool.TOOL_VERSION, "1.0.1")
        self.assertEqual(sync.VERSION, "1.0.1")
        self.assertEqual(json.loads(json.dumps({"v": __version__}))["v"], "1.0.1")


if __name__ == "__main__":
    unittest.main()

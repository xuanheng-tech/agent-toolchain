from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from agent_toolchain import tool_skill_sync as sync

TOOLS = {
    "context-loader": ("0.1.5", ("project-context",)),
    "snapshot-runner": ("1.4.0", ("snapshot-runner",)),
    "git-finalizer": ("0.9.3", ("git-finalize",)),
}


def write_json(path: Path, value: object) -> None:
    path.write_bytes(sync.canonical_json(value))


def run_git(repo: Path, *arguments: str) -> None:
    env = dict(
        os.environ,
        LC_ALL="C",
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
    )
    subprocess.run(
        ("git", "-C", str(repo), *arguments),
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


class SkillSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sources_root = self.root / "sources"
        self.install_root = self.root / "install"
        self.agents_root = self.root / "agents"
        self.bin_dir = self.root / "bin"
        self.hooks_dir = self.root / "hooks"
        self.sources_root.mkdir()
        self.bin_dir.mkdir()
        self.hooks_dir.mkdir()
        self._build_sources()
        self._build_installed_pair()
        self._uv_tool_layout(TOOLS["snapshot-runner"][0])
        self.sources = sync.Sources(self.sources_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _init_repo(self, repo: Path) -> None:
        run_git(repo, "init", "-q")
        run_git(repo, "config", "user.name", "Skill Sync Test")
        run_git(repo, "config", "user.email", "skill-sync@example.invalid")

    def _commit(self, repo: Path) -> None:
        run_git(repo, "add", ".")
        run_git(repo, "commit", "-qm", "fixture")

    def _build_sources(self) -> None:
        owner = self.sources_root / "git-finalizer"
        skill = owner / "skills" / sync.SKILL_NAME
        for relative in sync.SKILL_PAYLOAD:
            path = skill / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture {relative}\n", encoding="utf-8")
        skill_sha = sync.tree_sha256(skill, sync.SKILL_PAYLOAD)
        compatibility_skill = (
            owner / "skills" / sync.COMPATIBILITY_SKILL_NAME / "SKILL.md"
        )
        compatibility_skill.parent.mkdir(parents=True)
        compatibility_skill.write_text(
            "---\n"
            f"name: {sync.COMPATIBILITY_SKILL_NAME}\n"
            "description: Deprecated compatibility entry.\n"
            "---\n\n"
            "Use $git-change-delivery.\n",
            encoding="utf-8",
        )
        compatibility_sha = sync.sha256_file(compatibility_skill)

        compatibility = {
            "schema_version": 1,
            "toolchain_contract_version": 3,
            "workflow_skill": {
                "name": sync.SKILL_NAME,
                "contract_version": 1,
                "canonical_owner": "git-finalizer",
                "compatibility_shims": [sync.COMPATIBILITY_SKILL_NAME],
            },
            "context_loader_contract_version": 1,
            "snapshot_runner_contract_version": 1,
            "git_finalizer_contract_version": 1,
            "worktree_controller_adapter_version": 1,
            "worktree_controller_status": "available_external",
            "tools": {
                name: {
                    "source_directory": name,
                    "public_cli_contract_version": 1,
                    "skill_contract_version": 1,
                }
                for name in TOOLS
            },
        }
        write_json(owner / "toolchain_compatibility.json", compatibility)

        for name, (version, entrypoints) in TOOLS.items():
            repo = self.sources_root / name
            repo.mkdir(exist_ok=True)
            contract = {
                "schema_version": 1,
                "contract_version": 1,
                "tool_name": name,
                "tool_version": version,
                "commands": [
                    {
                        "name": entrypoints[0],
                        "flags": ["--version"],
                        "operation_class": "read_only",
                        "preconditions": [],
                        "result_statuses": ["complete"],
                    }
                ],
                "flags": {},
                "operation_classes": ["read_only"],
                "protected_operations": [],
            }
            write_json(repo / "tool_cli_contract.json", contract)
            if name == "git-finalizer":
                executable_kind = "source_files"
                executable = repo / "git-finalize"
                executable.write_text(
                    "#!/usr/bin/env bash\n"
                    f'readonly VERSION="{version}"\n'
                    "printf 'git-finalize %s\\n' \"$VERSION\"\n",
                    encoding="utf-8",
                )
                executable.chmod(0o755)
                companion = repo / "git-finalize-snapshot-verify.py"
                companion.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
                companion.chmod(0o755)
                bridge = repo / "hooks" / "test_support.py"
                bridge.parent.mkdir()
                bridge.write_text(
                    "BRIDGE_OPTION_SPECS: dict[str, tuple[int, bool]] = "
                    '{"--summary": (0, False)}\n',
                    encoding="utf-8",
                )
                sync_entry = repo / "tool-skill-sync"
                sync_entry.write_text(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    'script_dir=$(CDPATH=\'\' cd -- "$(dirname -- "$0")" && pwd)\n'
                    'implementation="$script_dir/tooling/tool_skill_sync.py"\n'
                    'if [[ ! -f "$implementation" ]]; then\n'
                    '    implementation="$script_dir/tool-skill-sync-support/'
                    'tool_skill_sync.py"\n'
                    "fi\n"
                    'exec python3 -B "$implementation" "$@"\n',
                    encoding="utf-8",
                )
                sync_entry.chmod(0o755)
                sync_implementation = repo / "tooling" / "tool_skill_sync.py"
                sync_implementation.parent.mkdir()
                sync_implementation.write_text(
                    "#!/usr/bin/env python3\n"
                    f"VERSION = {sync.VERSION!r}\n"
                    "import argparse\n"
                    "parser = argparse.ArgumentParser(prog='tool-skill-sync')\n"
                    "parser.add_argument('--version', action='version', "
                    f"version='tool-skill-sync {sync.VERSION}')\n"
                    "parser.parse_args()\n",
                    encoding="utf-8",
                )
                sync_implementation.chmod(0o755)
                executable_definition = {
                    "kind": executable_kind,
                    "paths": [
                        "git-finalize",
                        "git-finalize-snapshot-verify.py",
                        "hooks/test_support.py",
                        "tool-skill-sync",
                        "tooling/tool_skill_sync.py",
                    ],
                    "entrypoints": list(entrypoints),
                    "production_targets": {
                        "git-finalize": "bin/git-finalize",
                        "git-finalize-snapshot-verify.py": (
                            "bin/git-finalize-snapshot-verify.py"
                        ),
                        "hooks/test_support.py": (
                            "hooks/test_support.py"
                        ),
                        "tool-skill-sync": "bin/tool-skill-sync",
                        "tooling/tool_skill_sync.py": (
                            "bin/tool-skill-sync-support/tool_skill_sync.py"
                        ),
                    },
                }
            else:
                executable_kind = "python_console_scripts"
                scripts = "\n".join(
                    f'{entry} = "package.cli:main"' for entry in entrypoints
                )
                (repo / "pyproject.toml").write_text(
                    "[project]\n"
                    f'name = "{name}"\n'
                    f'version = "{version}"\n'
                    "[project.scripts]\n"
                    f"{scripts}\n",
                    encoding="utf-8",
                )
                executable_definition = {
                    "kind": executable_kind,
                    "entrypoints": list(entrypoints),
                }
            manifest = {
                "schema_version": sync.TOOL_SKILL_MANIFEST_SCHEMA_VERSION,
                "tool_name": name,
                "tool_version": version,
                "tool_commit": "@release",
                "skill_contract_version": 1,
                "public_cli_contract_sha256": sync.sha256_file(
                    repo / "tool_cli_contract.json"
                ),
                "canonical_skill_sha256": skill_sha,
                "compatible_toolchain_contract_version": 3,
                "canonical_skill": {
                    "owner": "git-finalizer",
                    "path": f"skills/{sync.SKILL_NAME}/SKILL.md",
                },
                "compatibility_skill": {
                    "name": sync.COMPATIBILITY_SKILL_NAME,
                    "path": (f"skills/{sync.COMPATIBILITY_SKILL_NAME}/SKILL.md"),
                    "sha256": compatibility_sha,
                },
                "executable": executable_definition,
                "install_targets": {
                    "stable_entries": list(entrypoints),
                    "skill_directory": sync.SKILL_NAME,
                },
            }
            write_json(repo / "tool_skill_manifest.json", manifest)
            self._init_repo(repo)
            self._commit(repo)

    def _build_installed_pair(self) -> None:
        installed_skill = self.agents_root / "skills" / sync.SKILL_NAME
        source_skill = self.sources_root / "git-finalizer" / "skills" / sync.SKILL_NAME
        for relative in sync.SKILL_PAYLOAD:
            target = installed_skill / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_skill / relative, target)
        compatibility_source = (
            self.sources_root
            / "git-finalizer"
            / "skills"
            / sync.COMPATIBILITY_SKILL_NAME
            / "SKILL.md"
        )
        compatibility_target = (
            self.agents_root / "skills" / sync.COMPATIBILITY_SKILL_NAME / "SKILL.md"
        )
        compatibility_target.parent.mkdir(parents=True)
        shutil.copyfile(compatibility_source, compatibility_target)
        for _, (version, entrypoints) in TOOLS.items():
            for entrypoint in entrypoints:
                path = self.bin_dir / entrypoint
                path.write_text(
                    f"#!/usr/bin/env sh\nprintf '%s {version}\\n' '{entrypoint}'\n",
                    encoding="utf-8",
                )
                path.chmod(0o755)
        (self.bin_dir / "git-finalize-snapshot-verify.py").write_text(
            "old companion\n", encoding="utf-8"
        )
        (self.hooks_dir / "test_support.py").write_text(
            "BRIDGE_OPTION_SPECS: dict[str, tuple[int, bool]] = "
            "{'--old': (0, False)}\n",
            encoding="utf-8",
        )

    def _current(self, tool: str) -> Path:
        return (self.install_root / "tools" / tool / "CURRENT").resolve(strict=True)

    def _update_skill_manifest_hashes(self) -> None:
        skill = self.sources_root / "git-finalizer" / "skills" / sync.SKILL_NAME
        digest = sync.tree_sha256(skill, sync.SKILL_PAYLOAD)
        compatibility = (
            self.sources_root
            / "git-finalizer"
            / "skills"
            / sync.COMPATIBILITY_SKILL_NAME
            / "SKILL.md"
        )
        compatibility_digest = sync.sha256_file(compatibility)
        for tool in TOOLS:
            path = self.sources_root / tool / "tool_skill_manifest.json"
            manifest = sync.read_json(path)
            manifest["canonical_skill_sha256"] = digest
            manifest["compatibility_skill"]["sha256"] = compatibility_digest
            write_json(path, manifest)

    def _install_production(
        self,
        tool: str,
        *,
        allow_dirty_source: bool = False,
    ) -> dict[str, object]:
        return sync.install(
            self.sources,
            tool,
            self.install_root,
            allow_dirty_source=allow_dirty_source,
            activate_production=True,
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )

    def _deployment(self, tool: str = "snapshot-runner") -> dict[str, object]:
        return sync.check_tool_deployment(
            self.sources, tool, self.agents_root, self.bin_dir, self.hooks_dir
        )

    def _rewrite_entry(self, entrypoint: str, version: str) -> None:
        path = self.bin_dir / entrypoint
        path.write_text(
            f"#!/usr/bin/env sh\nprintf '%s {version}\\n' '{entrypoint}'\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def test_check_deployment_passes_when_installation_matches(self) -> None:
        report = self._deployment()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(report["source_contract_status"], "PASS")
        self.assertEqual(report["installed_drift"], "none")
        self.assertEqual(report["installed_binary_version"], "1.4.0")
        self.assertEqual(report["errors"], [])
        # 2.0.0 ships a single provider-neutral console script.
        self.assertEqual(sorted(report["installed_binaries"]), ["snapshot-runner"])

    def test_check_deployment_fails_on_binary_only_drift(self) -> None:
        for entrypoint in TOOLS["snapshot-runner"][1]:
            self._rewrite_entry(entrypoint, "9.9.9")
        report = self._deployment()
        self.assertEqual(report["status"], "FAIL")
        # The source contract is still internally valid; only production drifted.
        self.assertEqual(report["source_contract_status"], "PASS")
        self.assertEqual(report["installed_drift"], "binary_only")
        self.assertEqual(report["installed_binary_version"], "9.9.9")
        self.assertTrue(any("binary_only" in error for error in report["errors"]))

    def test_check_detects_same_version_runtime_drift(self) -> None:
        module = self.root / "uvtools/snapshot-runner/lib/python3.12/site-packages/snapshot_runner/__init__.py"
        module.write_text(module.read_text() + "unreviewed_runtime_change = True\n")
        report = self._deployment()
        self.assertEqual(report["installed_binary_version"], TOOLS["snapshot-runner"][0])
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["installed_drift"], "binary_only")

    def test_check_detects_same_version_source_and_sidecar_drift(self) -> None:
        self._install_production("git-finalizer")
        for name in ("git-finalize", "git-finalize-snapshot-verify.py"):
            target = self.bin_dir / name
            before = target.read_bytes()
            target.write_bytes(before + b"# unreviewed same-version change\n")
            report = self._deployment("git-finalizer")
            self.assertEqual(report["installed_binary_version"], TOOLS["git-finalizer"][0])
            self.assertEqual(report["status"], "FAIL")
            self.assertIn(str(target), report["binary_content_drift"])
            target.write_bytes(before)

    def test_check_deployment_detects_missing_primary_entrypoint(self) -> None:
        (self.bin_dir / "snapshot-runner").unlink()
        report = self._deployment()
        self.assertEqual(report["status"], "FAIL")
        # An unresolvable installation reports no version at all.
        self.assertIsNone(report["installed_binary_version"])
        self.assertEqual(report["installed_drift"], "binary_only")
        self.assertIsNone(report["installed_binaries"]["snapshot-runner"]["path"])

    def test_check_deployment_detects_wrong_primary_entrypoint_version(self) -> None:
        self._rewrite_entry("snapshot-runner", "9.9.9")
        report = self._deployment()
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["installed_binary_version"], "9.9.9")
        self.assertEqual(report["installed_drift"], "binary_only")
        self.assertEqual(
            report["installed_binaries"]["snapshot-runner"]["version"], "9.9.9"
        )

    def test_check_deployment_fails_when_manifest_omits_primary_entrypoint(
        self,
    ) -> None:
        path = self.sources_root / "snapshot-runner" / "tool_skill_manifest.json"
        manifest = sync.read_json(path)
        entrypoints = [
            name
            for name in manifest["executable"]["entrypoints"]
            if name != "snapshot-runner"
        ]
        manifest["executable"]["entrypoints"] = entrypoints
        manifest["install_targets"]["stable_entries"] = entrypoints
        write_json(path, manifest)
        report = self._deployment()
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["source_contract_status"], "FAIL")

    def test_source_only_check_ignores_installed_drift(self) -> None:
        for entrypoint in TOOLS["snapshot-runner"][1]:
            self._rewrite_entry(entrypoint, "9.9.9")
        self.assertEqual(
            sync.check_tool(self.sources, "snapshot-runner")["status"], "PASS"
        )
        self.assertEqual(self._deployment()["status"], "FAIL")

    def test_explicit_checkouts_do_not_require_a_shared_directory_layout(self) -> None:
        owner = self.sources_root / "git-finalizer"
        checkout = self.root / "independent-feature"
        owner.rename(checkout)
        sources = sync.Sources(self.sources_root, {"git-finalizer": checkout})
        self.assertEqual(sync.check_tool(sources, "git-finalizer")["status"], "PASS")
        with self.assertRaisesRegex(sync.SyncError, "unknown tool"):
            sync.Sources(self.sources_root, {"git-finalizer": checkout, "unknown": checkout})

    def _rename_primary(self, old: str, new: str) -> None:
        repo = self.sources_root / "git-finalizer"
        (repo / old).rename(repo / new)
        manifest_path = repo / "tool_skill_manifest.json"
        manifest = sync.read_json(manifest_path)
        executable = manifest["executable"]
        executable["entrypoints"] = [new]
        executable["paths"] = [new if p == old else p for p in executable["paths"]]
        executable["production_targets"].pop(old)
        executable["production_targets"][new] = f"bin/{new}"
        manifest["install_targets"]["stable_entries"] = [new]
        contract_path = repo / "tool_cli_contract.json"
        contract = sync.read_json(contract_path)
        contract["commands"][0]["name"] = new
        write_json(contract_path, contract)
        manifest["public_cli_contract_sha256"] = sync.sha256_file(contract_path)
        write_json(manifest_path, manifest)

    def test_renamed_entrypoint_activation_and_rollback_restore_exact_sets(self) -> None:
        self._rename_primary("git-finalize", "legacy-finalize")
        self._install_production("git-finalizer", allow_dirty_source=True)
        (self.bin_dir / "git-finalize").unlink()  # Absent in this old installation.
        previous = self._current("git-finalizer")
        old_content = (self.bin_dir / "legacy-finalize").read_bytes()
        self._rename_primary("legacy-finalize", "git-finalize")
        self._install_production("git-finalizer", allow_dirty_source=True)
        current = self._current("git-finalizer")
        self.assertFalse((self.bin_dir / "legacy-finalize").exists())
        self.assertTrue((self.bin_dir / "git-finalize").is_file())
        result = sync.rollback(
            "git-finalizer", self.install_root, activate_production=True,
            agents_root=self.agents_root, bin_dir=self.bin_dir, hooks_dir=self.hooks_dir,
        )
        self.assertEqual(result["status"], "ROLLED_BACK")
        self.assertEqual(self._current("git-finalizer"), previous)
        self.assertFalse((self.bin_dir / "git-finalize").exists())
        self.assertEqual((self.bin_dir / "legacy-finalize").read_bytes(), old_content)
        sync.rollback(
            "git-finalizer", self.install_root, activate_production=True,
            agents_root=self.agents_root, bin_dir=self.bin_dir, hooks_dir=self.hooks_dir,
        )
        self.assertEqual(self._current("git-finalizer"), current)
        self.assertFalse((self.bin_dir / "legacy-finalize").exists())

    def _bump_finalizer_source_release(self, version: str) -> None:
        repo = self.sources_root / "git-finalizer"
        executable = repo / "git-finalize"
        executable.write_text(
            re.sub(
                r'readonly VERSION="[0-9.]+"',
                f'readonly VERSION="{version}"',
                executable.read_text(encoding="utf-8"),
            ),
            encoding="utf-8",
        )
        contract_path = repo / "tool_cli_contract.json"
        contract = sync.read_json(contract_path)
        contract["tool_version"] = version
        write_json(contract_path, contract)
        manifest_path = repo / "tool_skill_manifest.json"
        manifest = sync.read_json(manifest_path)
        manifest["tool_version"] = version
        manifest["public_cli_contract_sha256"] = sync.sha256_file(contract_path)
        write_json(manifest_path, manifest)

    def _stage_synthetic_released_tree(self, marker_text: str) -> str:
        installed = self.agents_root / "skills" / sync.SKILL_NAME
        target = installed / "SKILL.md"
        target.write_text(f"synthetic released {marker_text}\n", encoding="utf-8")
        return sync.tree_sha256(installed, sync.SKILL_PAYLOAD)

    def test_activation_upgrades_a_registered_released_production_tree(self) -> None:
        self._install_production("git-finalizer")
        self.assertIn(
            "9c05e5b279731a37b3ce15e2fbda7ae9962f81355cbafb86fa410f28ec19f527",
            sync.RELEASED_CANONICAL_SKILL_SHA256,
        )
        live_tree = self._stage_synthetic_released_tree("v1.3.0")
        previous = self._current("git-finalizer")
        self._bump_finalizer_source_release("2.0.0")

        with mock.patch.object(
            sync,
            "RELEASED_CANONICAL_SKILL_SHA256",
            sync.RELEASED_CANONICAL_SKILL_SHA256 | {live_tree},
        ):
            result = sync.install(
                self.sources,
                "git-finalizer",
                self.install_root,
                allow_dirty_source=True,
                activate_production=True,
                agents_root=self.agents_root,
                bin_dir=self.bin_dir,
                hooks_dir=self.hooks_dir,
            )

        installed = self.agents_root / "skills" / sync.SKILL_NAME
        self.assertEqual(result["status"], "INSTALLED")
        self.assertNotEqual(self._current("git-finalizer"), previous)
        self.assertEqual(
            sync.tree_sha256(
                self.sources_root / "git-finalizer" / "skills" / sync.SKILL_NAME,
                sync.SKILL_PAYLOAD,
            ),
            sync.tree_sha256(installed, sync.SKILL_PAYLOAD),
        )
        sync.verify_production_bundle(
            self._current("git-finalizer"),
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )

    def test_activation_upgrades_from_the_newest_released_production_tree(self) -> None:
        """The tree the newest public release shipped must count as released lineage."""
        for digest in (
            "0c82bceb12edde749aa6deddab25acb4ec083aeea92a37970ec64179ef92726b",
            "010cb09faf50921149b5d22b26093d9445d15461e8d3f10b7faff35259cd8e45",
            "d1525f9aeb9dcdda18fc9a5e8914ba3ba7c7ed26d9ec5463ca1359b428940bf1",
        ):
            self.assertIn(digest, sync.RELEASED_CANONICAL_SKILL_SHA256)
        self._install_production("git-finalizer")
        live_tree = self._stage_synthetic_released_tree("newest-release")
        previous = self._current("git-finalizer")
        self._bump_finalizer_source_release("2.0.0")

        with mock.patch.object(
            sync,
            "RELEASED_CANONICAL_SKILL_SHA256",
            sync.RELEASED_CANONICAL_SKILL_SHA256 | {live_tree},
        ):
            result = sync.install(
                self.sources,
                "git-finalizer",
                self.install_root,
                allow_dirty_source=True,
                activate_production=True,
                agents_root=self.agents_root,
                bin_dir=self.bin_dir,
                hooks_dir=self.hooks_dir,
            )

        self.assertEqual(result["status"], "INSTALLED")
        self.assertNotEqual(self._current("git-finalizer"), previous)
        sync.verify_production_bundle(
            self._current("git-finalizer"),
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )

    def test_activation_refuses_hand_drift_on_a_released_production_tree(self) -> None:
        self._install_production("git-finalizer")
        released_tree = self._stage_synthetic_released_tree("released-base")
        installed = self.agents_root / "skills" / sync.SKILL_NAME
        hand_edit = installed / "SKILL.md"
        hand_edit.write_bytes(hand_edit.read_bytes() + b"\nlocal policy\n")
        hand_digest = sync.sha256_file(hand_edit)
        self.assertNotIn(
            sync.tree_sha256(installed, sync.SKILL_PAYLOAD),
            sync.RELEASED_CANONICAL_SKILL_SHA256 | {released_tree},
        )
        previous = self._current("git-finalizer")
        self._bump_finalizer_source_release("2.0.0")

        with (
            mock.patch.object(
                sync,
                "RELEASED_CANONICAL_SKILL_SHA256",
                sync.RELEASED_CANONICAL_SKILL_SHA256 | {released_tree},
            ),
            self.assertRaisesRegex(sync.SyncError, "unknown content drift"),
        ):
            sync.install(
                self.sources,
                "git-finalizer",
                self.install_root,
                allow_dirty_source=True,
                activate_production=True,
                agents_root=self.agents_root,
                bin_dir=self.bin_dir,
                hooks_dir=self.hooks_dir,
            )

        self.assertEqual(self._current("git-finalizer"), previous)
        self.assertFalse(
            (self.install_root / "tools" / "git-finalizer" / "PREVIOUS").exists()
        )
        self.assertEqual(sync.sha256_file(hand_edit), hand_digest)


    def test_renamed_entrypoint_failure_keeps_previous_command_and_pointer(self) -> None:
        self._install_production("git-finalizer")
        previous = self._current("git-finalizer")
        original = (self.bin_dir / "git-finalize").read_bytes()
        self._rename_primary("git-finalize", "new-finalize")
        with (
            mock.patch.object(sync, "verify_production_bundle", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._install_production("git-finalizer", allow_dirty_source=True)
        self.assertEqual(self._current("git-finalizer"), previous)
        self.assertEqual((self.bin_dir / "git-finalize").read_bytes(), original)
        self.assertFalse((self.bin_dir / "new-finalize").exists())

    def test_retiring_entrypoint_drift_prevents_deletion(self) -> None:
        self._install_production("git-finalizer")
        self._rename_primary("git-finalize", "new-finalize")
        entry = self.bin_dir / "git-finalize"
        entry.write_text("local work must survive\n")
        with self.assertRaisesRegex(sync.SyncError, "retiring production target drifted"):
            self._install_production("git-finalizer", allow_dirty_source=True)
        self.assertEqual(entry.read_text(), "local work must survive\n")
        self.assertFalse((self.bin_dir / "new-finalize").exists())

    # --- safe production upgrade path -------------------------------------------------

    def _uv_tool_layout(
        self,
        version: str,
        entrypoints: tuple[str, ...] | None = None,
        bin_dir: Path | None = None,
    ) -> Path:
        """Build a uv-tool-shaped installation for snapshot-runner in the fixture."""
        root = self.root / "uvtools" / "snapshot-runner"
        site = root / "lib" / "python3.12" / "site-packages"
        package = site / "snapshot_runner"
        package.mkdir(parents=True, exist_ok=True)
        module = package / "__init__.py"
        module.write_text(f'__version__ = "{version}"\n', encoding="utf-8")
        dist_info = site / f"snapshot_runner-{version}.dist-info"
        if dist_info.exists():
            shutil.rmtree(dist_info)
        dist_info.mkdir(parents=True)
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(module.read_bytes()).digest())
            .rstrip(b"=")
            .decode()
        )
        (dist_info / "RECORD").write_text(
            f"snapshot_runner/__init__.py,sha256={digest},{module.stat().st_size}\n"
            f"../../../bin/snapshot-runner,sha256=ignored,1\n"
            f"snapshot_runner-{version}.dist-info/RECORD,,\n",
            encoding="utf-8",
        )
        for stale in site.glob("snapshot_runner-*.dist-info"):
            if stale != dist_info:
                shutil.rmtree(stale)
        venv_bin = root / "bin"
        venv_bin.mkdir(exist_ok=True)
        entrypoints = entrypoints or TOOLS["snapshot-runner"][1]
        install_dir = bin_dir or self.bin_dir
        entries = ",\n".join(
            f'    {{ name = "{name}", install-path = "{install_dir / name}", '
            f'from = "snapshot-runner" }}'
            for name in entrypoints
        )
        (root / "uv-receipt.toml").write_text(
            "[tool]\n"
            f'requirements = [{{ name = "snapshot-runner", specifier = "=={version}" }}]\n'
            f"entrypoints = [\n{entries},\n]\n"
            "\n[tool.options]\n"
            'index = [{ url = "https://pypi.org/simple" }]\n'
            "no-build = true\n",
            encoding="utf-8",
        )
        for name in entrypoints:
            target = venv_bin / name
            target.write_text(
                f"#!/usr/bin/env sh\nprintf '%s {version}\\n' '{name}'\n",
                encoding="utf-8",
            )
            target.chmod(0o755)
            link = install_dir / name
            if link.exists() or link.is_symlink():
                link.unlink()
            link.symlink_to(target)
        return root

    def _upgrade(self, **kwargs):
        return sync.upgrade(
            self.sources, "snapshot-runner", bin_dir=self.bin_dir, **kwargs
        )

    def test_upgrade_is_unchanged_and_verified_when_already_current(self) -> None:
        self._uv_tool_layout("1.4.0")
        report = self._upgrade()
        self.assertEqual(report["status"], "UNCHANGED")
        self.assertEqual(report["target_version"], "1.4.0")
        self.assertEqual(report["no_build"], True)
        self.assertEqual(report["bin_dir"], str(self.bin_dir))
        self.assertEqual(report["verification"]["record"]["verified_files"], 1)
        self.assertEqual(
            sorted(report["verification"]["entries"]),
            sorted(TOOLS["snapshot-runner"][1]),
        )

    def test_upgrade_preserves_bin_dir_index_and_no_build(self) -> None:
        self._uv_tool_layout("1.3.0")
        captured: dict[str, object] = {}

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            captured.update(
                tool=tool,
                version=version,
                bin_dir=bin_dir,
                indexes=indexes,
                no_build=no_build,
            )
            self._uv_tool_layout(version)

        with mock.patch.object(sync, "install_python_tool", fake_install):
            report = self._upgrade()
        self.assertEqual(report["status"], "UPGRADED")
        self.assertEqual(captured["version"], "1.4.0")
        self.assertEqual(captured["bin_dir"], str(self.bin_dir))
        self.assertEqual(captured["no_build"], True)
        self.assertEqual(captured["indexes"], ["https://pypi.org/simple"])
        self.assertEqual(report["verification"]["version"], "1.4.0")

    def test_upgrade_verifies_every_runtime_distribution_record(self) -> None:
        root = self._uv_tool_layout("1.4.0")
        site = root / "lib/python3.12/site-packages"
        module = site / "synthetic_dependency.py"
        content = b"value = 1\n"
        dist = site / "synthetic_dependency-1.0.0.dist-info"
        dist.mkdir()
        record = dist / "RECORD"
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        record_bytes = (
            f"synthetic_dependency.py,sha256={digest},{len(content)}\n"
            "synthetic_dependency-1.0.0.dist-info/RECORD,,\n"
        ).encode()
        module.write_bytes(content)
        record.write_bytes(record_bytes)
        report = self._upgrade()
        self.assertEqual(report["status"], "UNCHANGED")
        self.assertEqual(report["verification"]["record"], {
            "dist_infos": ["snapshot_runner-1.4.0.dist-info", dist.name],
            "verified_files": 2,
        })
        self.assertEqual(record.read_bytes(), record_bytes)
        self.assertEqual(module.read_bytes(), content)
        for drift, message in (
            ("changed", "does not match the published artifact"),
            ("missing", "installed file is missing"),
            ("empty-record", "installed RECORD verified no files"),
            ("missing-record", "installed RECORD"),
        ):
            with self.subTest(drift=drift):
                module.write_bytes(content)
                record.write_bytes(record_bytes)
                if drift == "changed":
                    module.write_bytes(b"local modification\n")
                elif drift == "missing":
                    module.unlink()
                elif drift == "empty-record":
                    record.write_text("synthetic_dependency-1.0.0.dist-info/RECORD,,\n")
                else:
                    record.unlink()
                with self.assertRaisesRegex(sync.SyncError, message):
                    self._upgrade()

    def test_failed_upgrade_restores_the_previous_working_installation(self) -> None:
        """An alias conflict or any install failure must not strand the CLIs."""
        self._uv_tool_layout("1.3.0")
        attempts: list[str] = []

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            attempts.append(version)
            if version == "1.4.0":
                raise sync.SyncError(
                    "uv tool install failed: Executables already exist: legacy-command"
                )
            self._uv_tool_layout(version)

        with (
            mock.patch.object(sync, "install_python_tool", fake_install),
            self.assertRaises(sync.SyncError) as error,
        ):
            self._upgrade()
        self.assertIn("restored", str(error.exception))
        self.assertEqual(attempts, ["1.4.0", "1.3.0"])
        # The previous release is installed and every entrypoint still works.
        state = sync.installed_binary_state(TOOLS["snapshot-runner"][1], self.bin_dir)
        self.assertEqual(state["version"], "1.3.0")
        self.assertEqual(len(state["entries"]), 1)

    def test_upgrade_fails_closed_when_verification_rejects_the_result(self) -> None:
        self._uv_tool_layout("1.3.0")

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            # Simulate an install that silently produced the wrong version.
            self._uv_tool_layout("9.9.9" if version == "1.4.0" else version)

        with (
            mock.patch.object(sync, "install_python_tool", fake_install),
            self.assertRaises(sync.SyncError),
        ):
            self._upgrade()
        state = sync.installed_binary_state(TOOLS["snapshot-runner"][1], self.bin_dir)
        self.assertEqual(state["version"], "1.3.0")

    def test_upgrade_detects_runtime_files_that_do_not_match_the_artifact(self) -> None:
        root = self._uv_tool_layout("1.4.0")
        module = root / "lib" / "python3.12" / "site-packages" / "snapshot_runner"
        (module / "__init__.py").write_text(
            "__version__ = 'tampered'\n", encoding="utf-8"
        )
        with self.assertRaises(sync.SyncError) as error:
            self._upgrade()
        self.assertIn("does not match the published artifact", str(error.exception))

    def test_upgrade_accepts_a_release_that_removes_entrypoints(self) -> None:
        """A major release may drop console scripts; retained ones must keep their paths."""
        legacy = ("snapshot-runner", "legacy-alias")
        self._uv_tool_layout("1.3.0", entrypoints=legacy)
        self.assertTrue((self.bin_dir / "legacy-alias").exists())

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            # The new release ships only the neutral primary command.
            self._uv_tool_layout(version, entrypoints=("snapshot-runner",))
            (self.bin_dir / "legacy-alias").unlink()

        with mock.patch.object(sync, "install_python_tool", fake_install):
            report = self._upgrade()
        self.assertEqual(report["status"], "UPGRADED")
        self.assertEqual(report["verification"]["version"], "1.4.0")
        self.assertEqual(
            report["verification"]["retired_entrypoints"], ["legacy-alias"]
        )
        self.assertEqual(sorted(report["verification"]["entries"]), ["snapshot-runner"])

    def _previous_python_bundle(self) -> Path:
        repo = self.sources_root / "snapshot-runner"
        originals = {p: (repo / p).read_bytes() for p in (
            "pyproject.toml", "tool_cli_contract.json", "tool_skill_manifest.json"
        )}
        project = repo / "pyproject.toml"
        project.write_text(project.read_text().replace('version = "1.4.0"', 'version = "1.3.0"')
                           .replace('snapshot-runner =', 'legacy-runner ='))
        contract_path = repo / "tool_cli_contract.json"
        contract = sync.read_json(contract_path)
        contract["tool_version"] = "1.3.0"
        contract["commands"][0]["name"] = "legacy-runner"
        write_json(contract_path, contract)
        manifest_path = repo / "tool_skill_manifest.json"
        manifest = sync.read_json(manifest_path)
        manifest["tool_version"] = "1.3.0"
        manifest["executable"]["entrypoints"] = ["legacy-runner"]
        manifest["install_targets"]["stable_entries"] = ["legacy-runner"]
        manifest["public_cli_contract_sha256"] = sync.sha256_file(contract_path)
        write_json(manifest_path, manifest)
        try:
            sync.install(self.sources, "snapshot-runner", self.install_root, allow_dirty_source=True)
            bundle = self._current("snapshot-runner")
        finally:
            for p, content in originals.items():
                (repo / p).write_bytes(content)
        self._uv_tool_layout("1.3.0", entrypoints=("legacy-runner",))
        (self.bin_dir / "snapshot-runner").unlink()
        return bundle

    def test_python_entrypoint_rename_uses_explicit_previous_bundle(self) -> None:
        previous = self._previous_python_bundle()
        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            self._uv_tool_layout(version)
            (self.bin_dir / "legacy-runner").unlink()
        with mock.patch.object(sync, "install_python_tool", fake_install):
            report = sync.upgrade(self.sources, "snapshot-runner", bin_dir=self.bin_dir,
                                  previous_bundle=previous)
        self.assertEqual(report["status"], "UPGRADED")
        self.assertEqual(report["verification"]["retired_entrypoints"], ["legacy-runner"])
        self.assertFalse((self.bin_dir / "legacy-runner").exists())

    def test_interrupted_python_rename_restores_the_old_contract(self) -> None:
        previous = self._previous_python_bundle()
        attempts = []
        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            attempts.append(version)
            if version == "1.4.0":
                self._uv_tool_layout(version)
                (self.bin_dir / "legacy-runner").unlink()
                raise KeyboardInterrupt
            self._uv_tool_layout(version, entrypoints=("legacy-runner",))
            (self.bin_dir / "snapshot-runner").unlink()
        with (
            mock.patch.object(sync, "install_python_tool", fake_install),
            self.assertRaises(KeyboardInterrupt),
        ):
            sync.upgrade(self.sources, "snapshot-runner", bin_dir=self.bin_dir,
                         previous_bundle=previous)
        self.assertEqual(attempts, ["1.4.0", "1.3.0"])
        self.assertEqual(sync.installed_binary_state(["legacy-runner"], self.bin_dir)["version"], "1.3.0")
        self.assertFalse((self.bin_dir / "snapshot-runner").exists())

    def test_upgrade_still_rejects_a_moved_retained_entrypoint(self) -> None:
        self._uv_tool_layout("1.3.0")
        moved = self.root / "elsewhere"
        moved.mkdir()

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            self._uv_tool_layout(version)
            link = self.bin_dir / "snapshot-runner"
            link.unlink()
            target = moved / "snapshot-runner"
            target.write_text(
                "#!/usr/bin/env sh\nprintf '%s 1.4.0\\n' 'snapshot-runner'\n",
                encoding="utf-8",
            )
            target.chmod(0o755)
            link.symlink_to(target)

        # Relocating a retained entrypoint out of its uv tool root must fail closed.
        with (
            mock.patch.object(sync, "install_python_tool", fake_install),
            self.assertRaises(sync.SyncError),
        ):
            self._upgrade()

    def _declare_uv_policy(self, bin_dir: Path, *, no_build: bool = True) -> None:
        path = self.sources_root / "git-finalizer" / "toolchain_compatibility.json"
        compatibility = sync.read_json(path)
        compatibility["tools"]["snapshot-runner"]["uv_install_policy"] = {
            "bin_dir": str(bin_dir),
            "index": ["https://pypi.org/simple"],
            "no_build": no_build,
        }
        write_json(path, compatibility)
        self.sources = sync.Sources(self.sources_root)

    def test_declared_policy_normalizes_a_drifted_receipt(self) -> None:
        """A drifted bin dir/index/no-build is normalized, not preserved."""
        self._uv_tool_layout("1.4.0")
        drifted = self.root / "drifted-bin"
        drifted.mkdir()
        self._declare_uv_policy(drifted)
        captured: dict[str, object] = {}

        def fake_install(tool, version, *, bin_dir, indexes, no_build):
            captured.update(bin_dir=bin_dir, indexes=indexes, no_build=no_build)
            self._uv_tool_layout(version, bin_dir=drifted)

        with mock.patch.object(sync, "install_python_tool", fake_install):
            report = self._upgrade()
        # Already at the target version, so this is a deployment-only normalization.
        self.assertEqual(report["status"], "NORMALIZED")
        self.assertTrue(report["receipt_drifted"])
        self.assertEqual(report["bin_dir"], str(drifted))
        self.assertEqual(captured["bin_dir"], str(drifted))
        self.assertEqual(captured["indexes"], ["https://pypi.org/simple"])
        self.assertEqual(captured["no_build"], True)

    def test_declared_policy_reports_unchanged_when_already_canonical(self) -> None:
        self._uv_tool_layout("1.4.0")
        self._declare_uv_policy(self.bin_dir)
        with mock.patch.object(
            sync, "install_python_tool", side_effect=AssertionError("must not install")
        ):
            report = self._upgrade()
        self.assertEqual(report["status"], "UNCHANGED")
        self.assertFalse(report["receipt_drifted"])

    def test_without_a_declared_policy_the_receipt_is_preserved(self) -> None:
        self._uv_tool_layout("1.4.0")
        report = self._upgrade()
        self.assertEqual(report["status"], "UNCHANGED")
        self.assertFalse(report["receipt_drifted"])
        self.assertEqual(report["bin_dir"], str(self.bin_dir))

    def test_upgrade_dry_run_does_not_install(self) -> None:
        self._uv_tool_layout("1.3.0")
        with mock.patch.object(
            sync, "install_python_tool", side_effect=AssertionError("must not install")
        ):
            report = self._upgrade(dry_run=True)
        self.assertEqual(report["status"], "DRY_RUN")
        self.assertEqual(report["previous_version"], "1.3.0")
        self.assertEqual(report["target_version"], "1.4.0")

    def test_check_and_status_report_matching_pair(self) -> None:
        for tool in TOOLS:
            self.assertEqual(sync.check_tool(self.sources, tool)["status"], "PASS")
        report = sync.status(self.sources, self.agents_root, self.bin_dir)
        self.assertEqual(
            {item["tool_name"]: item["drift"] for item in report["tools"]},
            {tool: "none" for tool in TOOLS},
        )

    def test_status_classifies_binary_skill_and_incompatible_drift(self) -> None:
        context_entry = self.bin_dir / "project-context"
        context_entry.write_text(
            "#!/usr/bin/env sh\nprintf 'tool 9.9.9\\n'\n", encoding="utf-8"
        )
        context_entry.chmod(0o755)
        report = sync.status(self.sources, self.agents_root, self.bin_dir)
        by_tool = {item["tool_name"]: item for item in report["tools"]}
        self.assertEqual(by_tool["context-loader"]["drift"], "binary_only")

        installed_skill = self.agents_root / "skills" / sync.SKILL_NAME / "SKILL.md"
        installed_skill.write_text("drift\n", encoding="utf-8")
        report = sync.status(self.sources, self.agents_root, self.bin_dir)
        by_tool = {item["tool_name"]: item for item in report["tools"]}
        self.assertEqual(by_tool["context-loader"]["drift"], "incompatible")
        self.assertEqual(by_tool["snapshot-runner"]["drift"], "skill_only")

    def test_check_fails_on_contract_hash_mismatch(self) -> None:
        contract = self.sources_root / "context-loader" / "tool_cli_contract.json"
        payload = sync.read_json(contract)
        payload["protected_operations"].append("tampered")
        write_json(contract, payload)
        checked = sync.check_tool(self.sources, "context-loader")
        self.assertEqual(checked["status"], "FAIL")
        self.assertIn("public CLI contract SHA mismatch", checked["errors"][0])

    def test_check_rejects_nondeterministic_json_serialization(self) -> None:
        contract = self.sources_root / "context-loader" / "tool_cli_contract.json"
        contract.write_text(
            contract.read_text(encoding="utf-8") + "\n", encoding="utf-8"
        )
        checked = sync.check_tool(self.sources, "context-loader")
        self.assertEqual(checked["status"], "FAIL")
        self.assertIn("not deterministically serialized", checked["errors"][0])

    def test_incompatible_public_contract_version_fails(self) -> None:
        compatibility_path = (
            self.sources_root / "git-finalizer" / "toolchain_compatibility.json"
        )
        compatibility = sync.read_json(compatibility_path)
        compatibility["tools"]["snapshot-runner"]["public_cli_contract_version"] = 2
        write_json(compatibility_path, compatibility)
        sources = sync.Sources(self.sources_root)
        checked = sync.check_tool(sources, "snapshot-runner")
        self.assertEqual(checked["status"], "FAIL")
        self.assertIn("incompatible", checked["errors"][0])

    def test_install_all_tools_is_atomic_and_deterministic(self) -> None:
        for tool in TOOLS:
            installed = sync.install(
                self.sources,
                tool,
                self.install_root,
                allow_dirty_source=False,
            )
            self.assertEqual(installed["status"], "INSTALLED")
            current = self._current(tool)
            release_before = sync.sha256_file(current / "release_manifest.json")
            repeated = sync.install(
                self.sources,
                tool,
                self.install_root,
                allow_dirty_source=False,
            )
            self.assertEqual(repeated["status"], "UNCHANGED")
            self.assertEqual(
                release_before,
                sync.sha256_file(self._current(tool) / "release_manifest.json"),
            )

    def test_install_failure_keeps_previous_pair_active(self) -> None:
        sync.install(
            self.sources,
            "git-finalizer",
            self.install_root,
            allow_dirty_source=False,
        )
        before = self._current("git-finalizer")
        with (
            mock.patch.object(
                sync, "smoke_bundle", side_effect=sync.SyncError("smoke failed")
            ),
            self.assertRaisesRegex(sync.SyncError, "smoke failed"),
        ):
            sync.install(
                self.sources,
                "git-finalizer",
                self.install_root,
                allow_dirty_source=False,
            )
        self.assertEqual(self._current("git-finalizer"), before)

    def test_pre_rename_bundle_remains_verifiable_for_migration(self) -> None:
        sync.install(
            self.sources,
            "git-finalizer",
            self.install_root,
            allow_dirty_source=False,
        )
        legacy = self.root / "legacy-bundle"
        shutil.copytree(self._current("git-finalizer"), legacy)
        manifest_path = legacy / "tool_skill_manifest.json"
        manifest = sync.read_json(manifest_path)
        manifest["schema_version"] = 1
        manifest.pop("compatibility_skill")
        manifest["canonical_skill"]["path"] = (
            f"skills/{sync.COMPATIBILITY_SKILL_NAME}/SKILL.md"
        )
        manifest["install_targets"]["skill_directory"] = sync.COMPATIBILITY_SKILL_NAME
        write_json(manifest_path, manifest)
        shutil.rmtree(legacy / "compatibility")
        release_path = legacy / "release_manifest.json"
        release = sync.read_json(release_path)
        release["manifest_sha"] = sync.sha256_file(manifest_path)
        write_json(release_path, release)

        verified = sync.verify_bundle(legacy, "git-finalizer")

        self.assertEqual(verified["tool_name"], "git-finalizer")

    def _bump_console_source_release(self, version: str) -> None:
        repo = self.sources_root / "snapshot-runner"
        project = repo / "pyproject.toml"
        project.write_text(
            project.read_text(encoding="utf-8").replace(
                'version = "1.4.0"', f'version = "{version}"'
            ),
            encoding="utf-8",
        )
        contract_path = repo / "tool_cli_contract.json"
        contract = sync.read_json(contract_path)
        contract["tool_version"] = version
        write_json(contract_path, contract)
        manifest_path = repo / "tool_skill_manifest.json"
        manifest = sync.read_json(manifest_path)
        manifest["tool_version"] = version
        manifest["public_cli_contract_sha256"] = sync.sha256_file(contract_path)
        write_json(manifest_path, manifest)

    def test_single_call_activation_follows_console_upgrade(self) -> None:
        self._install_production("snapshot-runner")
        self._rewrite_entry("snapshot-runner", "1.5.0")
        self._bump_console_source_release("1.5.0")

        result = sync.install(
            self.sources,
            "snapshot-runner",
            self.install_root,
            allow_dirty_source=True,
            activate_production=True,
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )

        self.assertEqual(result["status"], "INSTALLED")
        sync.verify_production_bundle(
            self._current("snapshot-runner"),
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )
        second = sync.install(
            self.sources,
            "snapshot-runner",
            self.install_root,
            allow_dirty_source=True,
            activate_production=True,
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )
        self.assertEqual(second["status"], "ACTIVATED")

    def test_activation_still_refuses_binary_that_matches_neither_release(self) -> None:
        self._install_production("snapshot-runner")
        self._rewrite_entry("snapshot-runner", "1.5.0")
        self._bump_console_source_release("1.5.0")
        self._rewrite_entry("snapshot-runner", "9.9.9")
        current_before = self._current("snapshot-runner")
        skill_marker = (
            self.agents_root / "skills" / sync.SKILL_NAME / "SKILL.md"
        )
        skill_before = skill_marker.read_bytes()

        with self.assertRaises(sync.SyncError) as caught:
            sync.install(
                self.sources,
                "snapshot-runner",
                self.install_root,
                allow_dirty_source=True,
                activate_production=True,
                agents_root=self.agents_root,
                bin_dir=self.bin_dir,
                hooks_dir=self.hooks_dir,
            )

        self.assertIn("do not match", str(caught.exception))
        self.assertEqual(self._current("snapshot-runner"), current_before)
        self.assertEqual(skill_marker.read_bytes(), skill_before)

    def test_production_activation_installs_verified_binary_hook_and_skill(
        self,
    ) -> None:
        installed = self._install_production("git-finalizer")

        self.assertTrue(installed["production_activation"])
        current = self._current("git-finalizer")
        self.assertEqual(
            (self.bin_dir / "git-finalize").read_bytes(),
            (current / "executable" / "git-finalize").read_bytes(),
        )
        self.assertEqual(
            (self.hooks_dir / "test_support.py").read_bytes(),
            (
                current / "executable" / "hooks" / "test_support.py"
            ).read_bytes(),
        )
        self.assertEqual(
            sync.tree_sha256(
                self.agents_root / "skills" / sync.SKILL_NAME,
                sync.SKILL_PAYLOAD,
            ),
            sync.read_json(current / "release_manifest.json")["skill_sha"],
        )
        self.assertEqual(
            (
                self.agents_root / "skills" / sync.COMPATIBILITY_SKILL_NAME / "SKILL.md"
            ).read_bytes(),
            (current / sync.COMPATIBILITY_BUNDLE_PATH).read_bytes(),
        )
        smoke = subprocess.run(
            (str(self.bin_dir / "tool-skill-sync"), "--version"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        self.assertIn(sync.VERSION, smoke.stdout)

    def test_activation_accepts_verified_previous_sync_version(self) -> None:
        implementation = (
            self.sources_root / "git-finalizer" / "tooling" / "tool_skill_sync.py"
        )
        current_source = implementation.read_text(encoding="utf-8")
        implementation.write_text(
            current_source.replace(sync.VERSION, "0.9.0"),
            encoding="utf-8",
        )
        sync.install(
            self.sources,
            "git-finalizer",
            self.install_root,
            allow_dirty_source=True,
        )
        legacy = self._current("git-finalizer")
        legacy_manifest_path = legacy / "tool_skill_manifest.json"
        legacy_manifest = sync.read_json(legacy_manifest_path)
        legacy_manifest["schema_version"] = 1
        legacy_manifest.pop("compatibility_skill")
        legacy_manifest["canonical_skill"]["path"] = (
            f"skills/{sync.COMPATIBILITY_SKILL_NAME}/SKILL.md"
        )
        legacy_manifest["install_targets"]["skill_directory"] = (
            sync.COMPATIBILITY_SKILL_NAME
        )
        write_json(legacy_manifest_path, legacy_manifest)
        shutil.rmtree(legacy / "compatibility")
        legacy_release_path = legacy / "release_manifest.json"
        legacy_release = sync.read_json(legacy_release_path)
        legacy_release["manifest_sha"] = sync.sha256_file(legacy_manifest_path)
        write_json(legacy_release_path, legacy_release)

        implementation.write_text(current_source, encoding="utf-8")
        installed = self._install_production("git-finalizer")

        self.assertTrue(installed["production_activation"])
        smoke = subprocess.run(
            (str(self.bin_dir / "tool-skill-sync"), "--version"),
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        self.assertIn(sync.VERSION, smoke.stdout)

    def test_shipped_sync_implementation_keeps_unambiguous_version(self) -> None:
        for rel in (
            Path("executable") / "tooling",
            Path("executable") / "src" / "git_finalizer" / "toolchain",
            Path("executable") / "src" / "agent_toolchain",
        ):
            with self.subTest(rel=str(rel)):
                staging = Path(self.temporary.name) / f"sync-bundle-{rel.name}"
                target = staging / rel
                target.mkdir(parents=True)
                shutil.copyfile(Path(sync.__file__), target / "tool_skill_sync.py")
                self.assertEqual(sync.bundled_sync_version(staging), sync.VERSION)

    def test_production_activation_refuses_hand_edited_skill_without_changes(
        self,
    ) -> None:
        self._install_production("git-finalizer")
        skill = self.agents_root / "skills" / sync.SKILL_NAME
        edited = skill / "SKILL.md"
        edited.write_bytes(b"# locally hand-enriched policy\n")
        state = {
            path.relative_to(skill).as_posix(): path.read_bytes()
            for path in skill.rglob("*")
            if path.is_file()
        }
        production_root = (
            self.install_root / "tools" / "git-finalizer" / "production"
        )
        backups_before = (
            len(list((production_root / "backups").iterdir()))
            if (production_root / "backups").exists()
            else 0
        )

        with self.assertRaises(sync.SyncError) as caught:
            self._install_production("git-finalizer")

        self.assertIn("unknown content drift", str(caught.exception))
        self.assertIn("live=", str(caught.exception))
        self.assertEqual(
            {
                path.relative_to(skill).as_posix(): path.read_bytes()
                for path in skill.rglob("*")
                if path.is_file()
            },
            state,
        )
        self.assertEqual(
            len(list((production_root / "backups").iterdir()))
            if (production_root / "backups").exists()
            else 0,
            backups_before,
        )

    def test_production_activation_refuses_incomplete_skill(self) -> None:
        self._install_production("git-finalizer")
        skill = self.agents_root / "skills" / sync.SKILL_NAME
        before = {
            path.relative_to(skill).as_posix(): path.read_bytes()
            for path in skill.rglob("*")
            if path.is_file() and path.name != "context-loader.md"
        }
        (skill / "references" / "context-loader.md").unlink()

        with self.assertRaises(sync.SyncError) as caught:
            self._install_production("git-finalizer")

        self.assertIn("incomplete", str(caught.exception))
        self.assertFalse((skill / "references" / "context-loader.md").exists())
        self.assertEqual(
            {
                path.relative_to(skill).as_posix(): path.read_bytes()
                for path in skill.rglob("*")
                if path.is_file()
            },
            before,
        )

    def test_production_activation_upgrades_accepted_canonical_lineage(self) -> None:
        self._install_production("git-finalizer")
        accepted_tree = sync.verify_bundle(self._current("git-finalizer"))["skill_sha"]
        skill_source = (
            self.sources_root / "git-finalizer" / "skills" / sync.SKILL_NAME
        )
        (skill_source / "references" / "unpublished-queue.md").write_text(
            "# extended\n", encoding="utf-8"
        )
        self._update_skill_manifest_hashes()
        self._commit(self.sources_root / "git-finalizer")
        sync.install(
            self.sources,
            "git-finalizer",
            self.install_root,
            allow_dirty_source=True,
        )
        bundle = self._current("git-finalizer")
        original = sync.RELEASED_CANONICAL_SKILL_SHA256
        sync.RELEASED_CANONICAL_SKILL_SHA256 = frozenset({accepted_tree})
        try:
            backup, verified = sync.activate_production_bundle(
                bundle,
                "git-finalizer",
                self.install_root / "tools" / "git-finalizer" / "production",
                agents_root=self.agents_root,
                bin_dir=self.bin_dir,
                hooks_dir=self.hooks_dir,
                previous_bundle=None,
            )
        finally:
            sync.RELEASED_CANONICAL_SKILL_SHA256 = original

        self.assertIsNotNone(backup)
        self.assertEqual(
            sync.tree_sha256(
                self.agents_root / "skills" / sync.SKILL_NAME, sync.SKILL_PAYLOAD
            ),
            verified["skill_sha256"],
        )

    def test_production_rollback_refuses_hand_edited_skill(self) -> None:
        self._install_production("git-finalizer")
        skill_source = (
            self.sources_root / "git-finalizer" / "skills" / sync.SKILL_NAME
        )
        (skill_source / "references" / "unpublished-queue.md").write_text(
            "# second release\n", encoding="utf-8"
        )
        self._update_skill_manifest_hashes()
        self._commit(self.sources_root / "git-finalizer")
        self._install_production("git-finalizer")
        skill = self.agents_root / "skills" / sync.SKILL_NAME
        (skill / "SKILL.md").write_bytes(b"# hand drift\n")
        before = {
            path.relative_to(skill).as_posix(): path.read_bytes()
            for path in skill.rglob("*")
            if path.is_file()
        }
        current_before = self._current("git-finalizer")

        with self.assertRaises(sync.SyncError) as caught:
            sync.rollback(
                "git-finalizer",
                self.install_root,
                activate_production=True,
                agents_root=self.agents_root,
                bin_dir=self.bin_dir,
                hooks_dir=self.hooks_dir,
            )

        self.assertIn("SHA mismatch", str(caught.exception))
        self.assertEqual(
            {
                path.relative_to(skill).as_posix(): path.read_bytes()
                for path in skill.rglob("*")
                if path.is_file()
            },
            before,
        )
        self.assertEqual(self._current("git-finalizer"), current_before)

    def test_production_verification_failure_restores_active_pair(self) -> None:
        binary = self.bin_dir / "git-finalize"
        skill = self.agents_root / "skills" / sync.SKILL_NAME / "SKILL.md"
        bridge = self.hooks_dir / "test_support.py"
        before = (binary.read_bytes(), skill.read_bytes(), bridge.read_bytes())
        real_verify = sync.verify_production_bundle

        with (
            mock.patch.object(
                sync,
                "verify_production_bundle",
                side_effect=[sync.SyncError("post-install verify failed")],
            ),
            self.assertRaisesRegex(sync.SyncError, "post-install verify failed"),
        ):
            self._install_production("git-finalizer")

        self.assertEqual(
            (binary.read_bytes(), skill.read_bytes(), bridge.read_bytes()), before
        )
        self.assertFalse(
            (self.install_root / "tools" / "git-finalizer" / "CURRENT").exists()
        )
        self.assertTrue(callable(real_verify))

    def test_production_keyboard_interrupt_restores_active_pair(self) -> None:
        binary = self.bin_dir / "git-finalize"
        skill = self.agents_root / "skills" / sync.SKILL_NAME / "SKILL.md"
        before = (binary.read_bytes(), skill.read_bytes())

        with (
            mock.patch.object(
                sync, "verify_production_bundle", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._install_production("git-finalizer")

        self.assertEqual((binary.read_bytes(), skill.read_bytes()), before)
        self.assertFalse(
            (self.install_root / "tools" / "git-finalizer" / "CURRENT").exists()
        )

    def test_interrupted_install_cleans_stage_and_keeps_current(self) -> None:
        sync.install(
            self.sources,
            "git-finalizer",
            self.install_root,
            allow_dirty_source=False,
        )
        before = self._current("git-finalizer")
        with (
            mock.patch.object(sync, "smoke_bundle", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            sync.install(
                self.sources,
                "git-finalizer",
                self.install_root,
                allow_dirty_source=False,
            )
        self.assertEqual(self._current("git-finalizer"), before)
        bundle_root = self.install_root / "bundles" / "git-finalizer"
        self.assertFalse(
            any(path.name.startswith(".staging-") for path in bundle_root.iterdir())
        )

    def test_rollback_switches_binary_and_skill_as_one_bundle(self) -> None:
        first = self._install_production("git-finalizer")
        first_bundle = first["current_bundle"]
        skill = (
            self.sources_root
            / "git-finalizer"
            / "skills"
            / sync.SKILL_NAME
            / "SKILL.md"
        )
        skill.write_text("fixture changed Skill\n", encoding="utf-8")
        self._update_skill_manifest_hashes()
        executable = self.sources_root / "git-finalizer" / "git-finalize"
        executable.write_text(
            executable.read_text(encoding="utf-8") + "# second bundle\n",
            encoding="utf-8",
        )
        second = self._install_production("git-finalizer", allow_dirty_source=True)
        self.assertNotEqual(second["current_bundle"], first_bundle)
        second_binary = (self.bin_dir / "git-finalize").read_bytes()
        rolled_back = sync.rollback(
            "git-finalizer",
            self.install_root,
            activate_production=True,
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )
        self.assertEqual(rolled_back["current_bundle"], first_bundle)
        release = sync.verify_bundle(self._current("git-finalizer"), "git-finalizer")
        self.assertEqual(release["source_state"], "committed_release")
        self.assertNotEqual(
            (self.bin_dir / "git-finalize").read_bytes(), second_binary
        )
        sync.verify_production_bundle(
            self._current("git-finalizer"),
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )

    def test_production_rollback_accepts_identical_pair_in_distinct_bundle(
        self,
    ) -> None:
        first = self._install_production("git-finalizer")
        (self.sources_root / "git-finalizer" / "unbundled-note.txt").write_text(
            "changes release identity only\n", encoding="utf-8"
        )
        second = self._install_production("git-finalizer", allow_dirty_source=True)
        self.assertNotEqual(second["current_bundle"], first["current_bundle"])

        rolled_back = sync.rollback(
            "git-finalizer",
            self.install_root,
            activate_production=True,
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )

        self.assertEqual(rolled_back["current_bundle"], first["current_bundle"])
        sync.verify_production_bundle(
            self._current("git-finalizer"),
            agents_root=self.agents_root,
            bin_dir=self.bin_dir,
            hooks_dir=self.hooks_dir,
        )

    def test_bundle_hash_tampering_fails_without_switch(self) -> None:
        sync.install(
            self.sources,
            "snapshot-runner",
            self.install_root,
            allow_dirty_source=False,
        )
        current = self._current("snapshot-runner")
        contract = current / "tool_cli_contract.json"
        payload = sync.read_json(contract)
        payload["protected_operations"].append("tampered")
        write_json(contract, payload)
        before = self._current("snapshot-runner")
        with self.assertRaisesRegex(sync.SyncError, "cli_contract_sha mismatch"):
            sync.install(
                self.sources,
                "snapshot-runner",
                self.install_root,
                allow_dirty_source=False,
            )
        self.assertEqual(self._current("snapshot-runner"), before)


class MultiClientSkillSyncTests(unittest.TestCase):
    """Isolated tests for B1/B2/B3 multi-client skill-check and skill-install behavior."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo_root = Path(__file__).resolve().parents[1]
        self.cli = self.repo_root / "tool-skill-sync"
        self.sources_root = self.root / "sources"
        self.agents_root = self.root / ".agents"
        self.bin_dir = self.root / "bin"
        self.hooks_dir = self.root / "hooks"
        self.sources_root.mkdir()
        self.bin_dir.mkdir()
        self.hooks_dir.mkdir()
        self._build_valid_skill_sources()
        self.sources = sync.Sources(self.sources_root)

        self.client_homes = {
            "codex": self.root / ".codex",
            "claude": self.root / ".claude",
            "gemini": self.root / ".gemini",
            "cursor": self.root / ".cursor",
            "qoder": self.root / ".qoder",
            "qoder-cn": self.root / ".qoder-cn",
        }

    def _build_valid_skill_sources(self) -> None:
        owner = self.sources_root / "git-finalizer"
        skill = owner / "skills" / sync.SKILL_NAME
        ref_links = "\n".join(
            f"- [{rel}]({rel})"
            for rel in sync.SKILL_PAYLOAD
            if rel.startswith("references/")
        )
        for relative in sync.SKILL_PAYLOAD:
            path = skill / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if relative == "SKILL.md":
                path.write_text(
                    "---\n"
                    f"name: {sync.SKILL_NAME}\n"
                    "description: Synthetic test canonical skill for multi-client sync.\n"
                    "---\n\n"
                    "# Synthetic Skill\n\n"
                    f"{ref_links}\n",
                    encoding="utf-8",
                )
            else:
                path.write_text(f"# fixture {relative}\n", encoding="utf-8")
            path.chmod(0o600)

        compat = owner / "skills" / sync.COMPATIBILITY_SKILL_NAME / "SKILL.md"
        compat.parent.mkdir(parents=True)
        compat.write_text(
            "---\n"
            f"name: {sync.COMPATIBILITY_SKILL_NAME}\n"
            "description: Deprecated compatibility entry.\n"
            "---\n\n"
            f"Use ${sync.SKILL_NAME}.\n",
            encoding="utf-8",
        )
        compat.chmod(0o600)

        compatibility = {
            "schema_version": 1,
            "toolchain_contract_version": 3,
            "workflow_skill": {
                "name": sync.SKILL_NAME,
                "contract_version": 1,
                "canonical_owner": "git-finalizer",
                "compatibility_shims": [sync.COMPATIBILITY_SKILL_NAME],
            },
            "context_loader_contract_version": 1,
            "snapshot_runner_contract_version": 1,
            "git_finalizer_contract_version": 1,
            "worktree_controller_adapter_version": 1,
            "worktree_controller_status": "available_external",
            "tools": {
                "git-finalizer": {
                    "source_directory": "git-finalizer",
                    "public_cli_contract_version": 1,
                    "skill_contract_version": 1,
                }
            },
        }
        write_json(owner / "toolchain_compatibility.json", compatibility)

    def _isolated_env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.root),
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "CODEX_HOME": str(self.client_homes["codex"]),
            "CLAUDE_CONFIG_DIR": str(self.client_homes["claude"]),
            "GEMINI_HOME": str(self.client_homes["gemini"]),
            "CURSOR_HOME": str(self.client_homes["cursor"]),
            "QODER_HOME": str(self.client_homes["qoder"]),
            "QODER_CN_HOME": str(self.client_homes["qoder-cn"]),
        }

    def _run_cli(
        self,
        *subcommand_args: str,
        include_client_flags: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [
            str(self.cli),
            "--source-root",
            str(self.sources_root),
            "--agents-root",
            str(self.agents_root),
            "--bin-dir",
            str(self.bin_dir),
            "--hooks-dir",
            str(self.hooks_dir),
            *subcommand_args,
        ]
        if include_client_flags:
            cmd.extend(
                [
                    "--codex-home",
                    str(self.client_homes["codex"]),
                    "--claude-home",
                    str(self.client_homes["claude"]),
                    "--gemini-home",
                    str(self.client_homes["gemini"]),
                    "--cursor-home",
                    str(self.client_homes["cursor"]),
                    "--qoder-home",
                    str(self.client_homes["qoder"]),
                    "--qoder-cn-home",
                    str(self.client_homes["qoder-cn"]),
                ]
            )
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=self._isolated_env(),
            check=False,
        )

    def _create_all_client_homes(self) -> None:
        for path in self.client_homes.values():
            path.mkdir(parents=True, exist_ok=True)

    def test_multi_client_skill_install_and_check_isolated_with_qoder_cn(self) -> None:
        self._create_all_client_homes()

        res_inst = self._run_cli("skill-install")
        self.assertEqual(res_inst.returncode, 0, msg=res_inst.stderr or res_inst.stdout)
        install_doc = json.loads(res_inst.stdout)
        self.assertEqual(install_doc["status"], "INSTALLED")
        self.assertFalse((self.agents_root / "skills" / "synced").exists())

        # Per-skill link clients (claude + qoder-cn) link inside the fixture.
        for cname in ("claude", "qoder-cn"):
            self.assertEqual(install_doc["clients"][cname]["status"], "SYNCED")
            for sname in sync.MANAGED_SKILL_NAMES:
                link = self.client_homes[cname] / "skills" / sname
                self.assertTrue(link.is_symlink(), msg=f"missing symlink {link}")
                resolved = link.resolve(strict=True)
                self.assertEqual(resolved, (self.agents_root / "skills" / sname).resolve())
                self.assertTrue(resolved.is_relative_to(self.root))

        # Native shared-root clients (codex, cursor, qoder) do not create redundant symlinks.
        for cname in ("codex", "cursor", "qoder"):
            self.assertEqual(install_doc["clients"][cname]["status"], "NATIVE")
            self.assertFalse((self.client_homes[cname] / "skills").exists())

        # Gemini / AGY uses entries: [{"path": "..."}] with no spurious top-level paths key.
        gemini_cfg = json.loads(
            (self.client_homes["gemini"] / "config" / "skills.json").read_text(encoding="utf-8")
        )
        self.assertEqual(gemini_cfg, {"entries": [{"path": str(self.agents_root / "skills")}]})
        self.assertNotIn("paths", gemini_cfg)

        res_chk = self._run_cli("skill-check", "--strict-clients")
        self.assertEqual(res_chk.returncode, 0, msg=res_chk.stderr or res_chk.stdout)
        chk_doc = json.loads(res_chk.stdout)
        self.assertEqual(chk_doc["status"], "PASS")
        self.assertEqual(chk_doc["client_status"], "PASS")
        self.assertEqual(
            chk_doc["clients_summary"],
            {
                "status": "PASS",
                "counts": {"PASS": 6, "DRIFT": 0, "FAIL": 0, "SKIP": 0},
                "passed": ["claude", "codex", "cursor", "gemini", "qoder", "qoder-cn"],
                "drifted": [],
                "failing": [],
                "skipped": [],
            },
        )
        for cname in ("codex", "claude", "gemini", "cursor", "qoder", "qoder-cn"):
            self.assertEqual(chk_doc["clients"][cname]["status"], "PASS")

        # Verify env-only invocation (HOME + QODER_CN_HOME without CLI home flags) stays isolated.
        env_chk = self._run_cli("skill-check", include_client_flags=False)
        self.assertEqual(env_chk.returncode, 0, msg=env_chk.stderr or env_chk.stdout)
        env_doc = json.loads(env_chk.stdout)
        self.assertEqual(env_doc["client_status"], "PASS")
        self.assertEqual(
            env_doc["clients"]["gemini"]["config"],
            str(self.client_homes["gemini"] / "config" / "skills.json"),
        )

    def test_gemini_agy_entries_lossless_read_write_and_legacy_read(self) -> None:
        self._create_all_client_homes()
        sync.skill_install(
            self.sources,
            agents_root=self.agents_root,
            client_homes=self.client_homes,
            sync_clients=False,
        )
        cfg_dir = self.client_homes["gemini"] / "config"
        cfg_dir.mkdir(parents=True)
        cfg_path = cfg_dir / "skills.json"

        # Existing rich AGY config with inherits, filters, and custom top-level field.
        initial_cfg = {
            "inherits": [
                {
                    "path": "/shared/team-skills.json",
                    "include_only": ["lint-skill"],
                    "exclude": ["legacy-.*"],
                }
            ],
            "entries": [
                {
                    "path": "/custom/project/skills",
                    "exclude": ["experimental-.*"],
                    "include_only": ["custom-skill"],
                }
            ],
            "custom_flag": True,
        }
        cfg_path.write_text(json.dumps(initial_cfg, indent=2) + "\n", encoding="utf-8")

        before = sync.skill_check(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        self.assertEqual(before["status"], "PASS")
        self.assertEqual(before["clients"]["gemini"]["status"], "DRIFT")
        self.assertEqual(before["clients"]["gemini"]["registered_paths"], [])

        # Sync preserves inherits, existing entry fields, custom_flag, and avoids writing paths.
        sync.skill_install(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        updated = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(updated["inherits"], initial_cfg["inherits"])
        self.assertEqual(updated["custom_flag"], True)
        self.assertNotIn("paths", updated)
        self.assertEqual(
            updated["entries"],
            [
                initial_cfg["entries"][0],
                {"path": str(self.agents_root / "skills")},
            ],
        )

        # Idempotent repeated sync adds no duplicates and leaves file untouched.
        raw_before_repeat = cfg_path.read_bytes()
        sync.skill_install(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        self.assertEqual(cfg_path.read_bytes(), raw_before_repeat)

        # Home-relative (~/) and relative (.agents/skills) entries resolve and avoid duplication.
        for raw_entry_path in ("~/.agents/skills", ".agents/skills"):
            with (
                self.subTest(raw_entry_path=raw_entry_path),
                mock.patch.dict(os.environ, {"HOME": str(self.root)}, clear=False),
            ):
                preserved_cfg = {
                    "inherits": [{"path": "/shared/base.json"}],
                    "entries": [{"path": raw_entry_path, "exclude": ["wip-.*"]}],
                }
                encoded = json.dumps(preserved_cfg, indent=2) + "\n"
                cfg_path.write_text(encoded, encoding="utf-8")

                chk = sync.skill_check(
                    self.sources, agents_root=self.agents_root, client_homes=self.client_homes
                )
                self.assertEqual(chk["clients"]["gemini"]["status"], "PASS")
                self.assertEqual(
                    chk["clients"]["gemini"]["registered_paths"],
                    [str(self.agents_root / "skills")],
                )

                sync.skill_install(
                    self.sources, agents_root=self.agents_root, client_homes=self.client_homes
                )
                self.assertEqual(cfg_path.read_text(encoding="utf-8"), encoded)

        # Graceful read tolerance for legacy paths/skillPaths/skills lists.
        cfg_path.write_text(
            json.dumps({"paths": [str(self.agents_root / "skills")]}), encoding="utf-8"
        )
        legacy_chk = sync.skill_check(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        self.assertEqual(legacy_chk["clients"]["gemini"]["status"], "PASS")

        # Malformed configs fail closed on both skill-check and skill-install.
        for bad_text in (
            "{not-json",
            "[]",
            json.dumps({"entries": "not-a-list"}),
            json.dumps({"entries": ["not-an-object"]}),
            json.dumps({"entries": [{}]}),
            json.dumps({"entries": [{"path": "   "}]}),
        ):
            with self.subTest(bad_text=bad_text):
                cfg_path.write_text(bad_text, encoding="utf-8")
                chk = sync.skill_check(
                    self.sources, agents_root=self.agents_root, client_homes=self.client_homes
                )
                self.assertEqual(chk["clients"]["gemini"]["status"], "FAIL")
                self.assertIn("error", chk["clients"]["gemini"])
                with self.assertRaises(sync.SyncError):
                    sync.skill_install(
                        self.sources, agents_root=self.agents_root, client_homes=self.client_homes
                    )
                self.assertEqual(cfg_path.read_text(encoding="utf-8"), bad_text)

        # Symlinked and non-regular-file skills.json fail closed.
        foreign = self.root / "foreign-skills.json"
        foreign.write_text("{}", encoding="utf-8")
        cfg_path.unlink()
        cfg_path.symlink_to(foreign)
        sym_chk = sync.skill_check(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        self.assertEqual(sym_chk["clients"]["gemini"]["status"], "FAIL")
        with self.assertRaises(sync.SyncError):
            sync.skill_install(
                self.sources, agents_root=self.agents_root, client_homes=self.client_homes
            )

        cfg_path.unlink()
        cfg_path.mkdir()
        dir_chk = sync.skill_check(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        self.assertEqual(dir_chk["clients"]["gemini"]["status"], "FAIL")
        self.assertIn("not a regular file", dir_chk["clients"]["gemini"]["error"])
        with self.assertRaisesRegex(sync.SyncError, "not a regular file"):
            sync.skill_install(
                self.sources, agents_root=self.agents_root, client_homes=self.client_homes
            )

    def test_native_shared_root_clients_and_strict_broken_link_detection(self) -> None:
        self._create_all_client_homes()

        # Before payload installation, native clients report DRIFT (shared skills absent).
        pre_chk = sync.skill_check(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        self.assertEqual(pre_chk["status"], "DRIFT")
        for cname in ("codex", "cursor", "qoder"):
            self.assertEqual(pre_chk["clients"][cname]["status"], "DRIFT")

        # Fail-closed check: broken native client entry stops skill-install BEFORE mutating claude/qoder-cn.
        stale_target = self.root / "deleted-tmp" / "agents" / "skills" / sync.SKILL_NAME
        (self.client_homes["cursor"] / "skills").mkdir()
        (self.client_homes["cursor"] / "skills" / sync.SKILL_NAME).symlink_to(stale_target)
        with self.assertRaisesRegex(sync.SyncError, "broken or conflicting"):
            sync.skill_install(
                self.sources, agents_root=self.agents_root, client_homes=self.client_homes
            )
        self.assertFalse((self.client_homes["claude"] / "skills").exists())
        self.assertFalse((self.client_homes["qoder-cn"] / "skills").exists())
        (self.client_homes["cursor"] / "skills" / sync.SKILL_NAME).unlink()
        (self.client_homes["cursor"] / "skills").rmdir()

        # Install payload + sync clients -> all PASS.
        sync.skill_install(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )

        # Valid optional per-skill symlink under cursor/qoder stays PASS.
        (self.client_homes["cursor"] / "skills").mkdir()
        (self.client_homes["cursor"] / "skills" / sync.SKILL_NAME).symlink_to(
            self.agents_root / "skills" / sync.SKILL_NAME
        )
        valid_opt_chk = sync.skill_check(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        self.assertEqual(valid_opt_chk["clients"]["cursor"]["status"], "PASS")
        self.assertEqual(valid_opt_chk["clients"]["cursor"]["invalid_links"], [])

        # Broken symlink (like stale /tmp/... target) must FAIL on cursor, qoder, qoder-cn, claude.
        for cname in ("cursor", "qoder", "qoder-cn", "claude"):
            with self.subTest(broken_symlink_client=cname):
                cskills = self.client_homes[cname] / "skills"
                cskills.mkdir(exist_ok=True)
                link = cskills / sync.SKILL_NAME
                if link.is_symlink() or link.exists():
                    link.unlink()
                link.symlink_to(stale_target)

                chk = sync.skill_check(
                    self.sources, agents_root=self.agents_root, client_homes=self.client_homes
                )
                self.assertEqual(chk["status"], "PASS")
                self.assertEqual(chk["clients"][cname]["status"], "FAIL")
                self.assertEqual(chk["clients"][cname]["invalid_links"], [sync.SKILL_NAME])
                self.assertEqual(chk["client_status"], "FAIL")
                self.assertIn(cname, chk["clients_summary"]["failing"])

                if cname in sync.NATIVE_SHARED_ROOT_CLIENTS:
                    with self.assertRaisesRegex(sync.SyncError, "broken or conflicting"):
                        sync.skill_install(
                            self.sources,
                            agents_root=self.agents_root,
                            client_homes=self.client_homes,
                        )
                    link.unlink()
                else:
                    # Link clients repair broken symlinks during skill-install.
                    sync.skill_install(
                        self.sources,
                        agents_root=self.agents_root,
                        client_homes=self.client_homes,
                    )
                    self.assertEqual(
                        link.resolve(strict=True),
                        (self.agents_root / "skills" / sync.SKILL_NAME).resolve(),
                    )

        # Wrong-target symlink and non-symlink directory shadowing entry also FAIL.
        other_dir = self.root / "other-skill"
        other_dir.mkdir()
        qoder_skills = self.client_homes["qoder"] / "skills"
        qoder_skills.mkdir(exist_ok=True)
        wrong_link = qoder_skills / sync.SKILL_NAME
        wrong_link.symlink_to(other_dir)
        wrong_chk = sync.skill_check(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        self.assertEqual(wrong_chk["clients"]["qoder"]["status"], "FAIL")
        self.assertEqual(wrong_chk["clients"]["qoder"]["invalid_links"], [sync.SKILL_NAME])
        wrong_link.unlink()

        shadow_dir = qoder_skills / sync.COMPATIBILITY_SKILL_NAME
        shadow_dir.mkdir()
        shadow_chk = sync.skill_check(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        self.assertEqual(shadow_chk["clients"]["qoder"]["status"], "FAIL")
        self.assertEqual(
            shadow_chk["clients"]["qoder"]["invalid_links"], [sync.COMPATIBILITY_SKILL_NAME]
        )
        shadow_dir.rmdir()
        qoder_skills.rmdir()

        # Whole-root skills symlink and non-directory skills path both FAIL.
        (self.client_homes["qoder"] / "skills").symlink_to(self.agents_root / "skills")
        root_sym_chk = sync.skill_check(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        self.assertEqual(root_sym_chk["clients"]["qoder"]["status"], "FAIL")
        self.assertIn("whole-root symlink", root_sym_chk["clients"]["qoder"]["error"])
        (self.client_homes["qoder"] / "skills").unlink()

        (self.client_homes["qoder"] / "skills").write_text("not a dir\n", encoding="utf-8")
        not_dir_chk = sync.skill_check(
            self.sources, agents_root=self.agents_root, client_homes=self.client_homes
        )
        self.assertEqual(not_dir_chk["clients"]["qoder"]["status"], "FAIL")
        self.assertIn("not a directory", not_dir_chk["clients"]["qoder"]["error"])

    def test_skill_check_client_summary_and_strict_clients_compat(self) -> None:
        # 1. All clients absent -> payload PASS + client_status SKIP.
        sync.skill_install(
            self.sources,
            agents_root=self.agents_root,
            client_homes=self.client_homes,
            sync_clients=False,
        )
        skip_default = self._run_cli("skill-check")
        self.assertEqual(skip_default.returncode, 0, msg=skip_default.stderr)
        skip_doc = json.loads(skip_default.stdout)
        self.assertEqual(skip_doc["status"], "PASS")
        self.assertEqual(skip_doc["client_status"], "SKIP")
        self.assertEqual(
            skip_doc["clients_summary"]["counts"],
            {"PASS": 0, "DRIFT": 0, "FAIL": 0, "SKIP": 6},
        )
        skip_strict = self._run_cli("skill-check", "--strict-clients")
        self.assertEqual(skip_strict.returncode, 0, msg=skip_strict.stderr)

        # 2. Payload PASS + client DRIFT -> default exits 0 (compat), --strict-clients exits 1.
        self.client_homes["claude"].mkdir()
        self.client_homes["cursor"].mkdir()
        drift_default = self._run_cli("skill-check")
        self.assertEqual(drift_default.returncode, 0, msg=drift_default.stderr)
        drift_doc = json.loads(drift_default.stdout)
        self.assertEqual(drift_doc["status"], "PASS")
        self.assertEqual(drift_doc["client_status"], "DRIFT")
        self.assertEqual(drift_doc["clients_summary"]["drifted"], ["claude"])
        self.assertEqual(drift_doc["clients_summary"]["passed"], ["cursor"])
        self.assertEqual(drift_doc["clients_summary"]["counts"]["SKIP"], 4)

        drift_strict = self._run_cli("skill-check", "--strict-clients")
        self.assertEqual(drift_strict.returncode, 1)
        drift_strict_doc = json.loads(drift_strict.stdout)
        self.assertEqual(drift_strict_doc["status"], "PASS")
        self.assertEqual(drift_strict_doc["client_status"], "DRIFT")

        # 3. Payload PASS + client FAIL (precedence FAIL > DRIFT > PASS > SKIP).
        self.client_homes["qoder-cn"].mkdir()
        (self.client_homes["qoder-cn"] / "skills").mkdir()
        (self.client_homes["qoder-cn"] / "skills" / sync.SKILL_NAME).symlink_to(
            self.root / "nonexistent-stale-target"
        )
        fail_default = self._run_cli("skill-check")
        self.assertEqual(fail_default.returncode, 0, msg=fail_default.stderr)
        fail_doc = json.loads(fail_default.stdout)
        self.assertEqual(fail_doc["status"], "PASS")
        self.assertEqual(fail_doc["client_status"], "FAIL")
        self.assertEqual(fail_doc["clients_summary"]["failing"], ["qoder-cn"])
        self.assertEqual(fail_doc["clients_summary"]["drifted"], ["claude"])
        self.assertEqual(fail_doc["clients_summary"]["passed"], ["cursor"])

        fail_strict = self._run_cli("skill-check", "--strict-clients")
        self.assertEqual(fail_strict.returncode, 1)
        self.assertEqual(json.loads(fail_strict.stdout)["status"], "PASS")

        # 4. Payload FAIL (untrusted live drift) exits 1 even without --strict-clients.
        (self.agents_root / "skills" / sync.SKILL_NAME / "SKILL.md").write_text(
            "hand-edited untrusted drift\n", encoding="utf-8"
        )
        payload_fail = self._run_cli("skill-check")
        self.assertEqual(payload_fail.returncode, 1)
        payload_fail_doc = json.loads(payload_fail.stdout)
        self.assertEqual(payload_fail_doc["status"], "FAIL")
        self.assertFalse(payload_fail_doc["trusted_lineage"])


if __name__ == "__main__":
    unittest.main()

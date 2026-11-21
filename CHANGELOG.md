# Changelog

All notable changes to `agent-toolchain` are documented in this file.

## Unreleased

## 1.0.1 - 2026-11-22

- Changed: synced `toolchain_compatibility.json` (`snapshot_runner_contract_version` 5) and `manifests/{context-loader,snapshot-runner,git-finalizer}/tool_skill_manifest.json` with `snapshot-runner` `2.7.0` (contract 5), `git-finalizer` `2.0.1` (contract 10), and the updated `git-change-delivery` Skill payload digest (`9e4790e5...`).
- Added: recorded the `git-finalizer` `v2.0.1` canonical Skill payload digest in `RELEASED_CANONICAL_SKILL_SHA256` alongside `v2.0.0` lineage for seamless upgrades.

## 1.0.0 - 2026-11-20

- Added: standalone zero-dependency pure-Python `agent-toolchain` package (PyPI distribution `coding-agent-toolchain`, module `agent_toolchain`, `pyproject.toml` with `uv_build`, `requires-python = ">=3.12,<3.15"`, Apache-2.0) exposing `tool-skill-sync` and `tool-temp-dir` console scripts and thin executable root wrappers.
- Changed: extracted `errors`, `git_io`, `json_io`, `tool_skill_sync`, `tool_temp_dir`, and `workflow_health` under `src/agent_toolchain/`, cleanly decoupled from `git_finalizer.delivery` while preserving full CLI, bundle, and `toolchain_compatibility.json` v6 schema compatibility.
- Added: root `toolchain_compatibility.json` (`toolchain_contract_version` 6) and `manifests/{context-loader,snapshot-runner,git-finalizer}/tool_skill_manifest.json` aligned with `context-loader` `1.4.0` (contract 4), `snapshot-runner` `2.6.0` (contract 4), `git-finalizer` `2.0.0` (contract 10), and `worktree-controller` CLI `37..62` / storage `4..5`.
- Added: migrated unit/integration test suites (`tests/test_tool_skill_sync.py`, `tests/test_tool_temp_dir.py`, `tests/test_workflow_health.py`, `tests/test_package_contract.py`) and GitHub Actions CI, release, and OIDC PyPI publishing workflows.

# Agent Toolchain

Standalone, zero-dependency pure-Python CLI suite for coding-agent toolchain compatibility verification, Skill synchronization, deployment diagnostics, and safe `/tmp` task directory management.

Current source version: **1.0.1**.

## Overview

`agent-toolchain` extracts the shared toolchain governance and workspace utilities into an independent package (`agent_toolchain`, published on PyPI as `coding-agent-toolchain`) decoupled from Git Finalizer's delivery engine while preserving full CLI and schema compatibility:

- **`tool-skill-sync`**: Validates `toolchain_compatibility.json` (contract v6) and `ToolSkillManifest` v2 pins across `context-loader`, `snapshot-runner`, and `git-finalizer`; manages atomic `ToolReleaseBundle` staging, production activation/rollback, multi-client Skill exposure (`codex`, `claude`, `gemini`, `cursor`, `qoder`, `qoder-cn`), and read-only `doctor` deployment/instruction diagnostics (including external `worktree-controller` runtime checks).
- **`tool-temp-dir`**: Creates and safely cleans up user-owned, registered `/tmp/tool-task-<prefix>-<hex>` task directories with strict symlink, device/inode, mount-point, active Unix socket, and explicit pytest basetemp guards.

## Requirements

- **Python**: `>=3.12,<3.15` (pure Python standard library only; zero runtime dependencies).
- **OS & Tools**: POSIX/Linux environment with `git` on `PATH` (for repository checks). Verified on Ubuntu 24.04 LTS with Python 3.12.

## Installation

Install from a built wheel or source checkout:

```bash
uv tool install 'coding-agent-toolchain==1.0.1'
```

Or using `pip`:

```bash
pip install 'coding-agent-toolchain==1.0.1'
```

From a local source checkout, thin executable wrappers are available directly at the repository root:

```bash
./tool-skill-sync --version
./tool-temp-dir --version
```

## Quick Start

### Toolchain status, contract verification, and read-only diagnostics

```bash
# Inspect source contracts vs. active installed tools
tool-skill-sync status

# Read-only deployment & instruction freshness check
tool-skill-sync doctor --repo /path/to/checkout --summary

# Source-only contract validation for a tool
tool-skill-sync check --source-only git-finalizer

# Check or sync multi-client shared Skill links
# Top-level `status` reflects managed Skill payload/lineage verification (exit 0 on PASS);
# `client_status` / `clients_summary` report independent multi-client exposure (add --strict-clients to gate exit code on client exposure)
tool-skill-sync skill-check
tool-skill-sync skill-check --strict-clients
tool-skill-sync skill-install
```

### Safe registered temporary task directories

```bash
# Create a registered /tmp/tool-task-<prefix>-<16hex> directory (0700)
tool-temp-dir create my-task

# Dry-run and cleanup an owned registered directory
tool-temp-dir dry-run-cleanup /tmp/tool-task-my-task-0123456789abcdef
tool-temp-dir cleanup /tmp/tool-task-my-task-0123456789abcdef
```

## Pinned Compatibility Baseline (`toolchain_compatibility.json` v6)

The repository carries `toolchain_compatibility.json` (`toolchain_contract_version: 6`) and manifest pins under `manifests/`:

| Tool | Pinned Version | Contract Version |
| --- | --- | --- |
| `context-loader` | `1.4.0` | `4` |
| `snapshot-runner` | `2.7.0` | `5` |
| `git-finalizer` | `2.0.1` | `10` |
| `worktree-controller` | external runtime | CLI `37..62`, storage `4..5`, adapter `3` |

## Development

```bash
just lint
just test
ruff check src tests
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

## License

Licensed under the [Apache License 2.0](LICENSE).

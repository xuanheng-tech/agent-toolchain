# Security Policy

## Reporting a Vulnerability

Please report security issues through a **private security advisory** on the repository's GitHub
Security tab rather than a public issue. Include the installed versions (`tool-skill-sync --version`
and `tool-temp-dir --version`), the affected command, and a minimal reproduction using synthetic
data in a disposable directory (never include real credentials or private paths).

Only the latest stable release is supported. Fixes are shipped in a new release; released tags and
published artifacts are never rewritten.

## Security Properties and Fail-Closed Design

- **Trusted Skill lineage gate.** `tool-skill-sync` verifies canonical Skill payload SHA-256 trees,
  `ToolSkillManifest` v2 pins, and `uv` wheel `RECORD` digests before production activation or
  rollback, refusing unknown content drift without overwriting live files.
- **Read-only diagnostics.** `tool-skill-sync doctor` and `check` observe installed entrypoints,
  frozen contracts, runtime manifests, and instruction discovery budgets without mutating
  repositories or activating candidates.
- **Strict `/tmp` ownership and traversal guards.** `tool-temp-dir` cleans only paths created and
  registered by itself under `/tmp`, validating UID, device/inode identity, permissions (`0700` /
  `0600`), mount points, symlinks, inactive Unix sockets, and explicit pytest basetemp scopes via
  `dir_fd` operations before unlinking any entry.

## Trust Boundaries

- Tools execute as the invoking user with that user's permissions and do not provide isolation from
  malicious processes running under the same UID.
- Repository manifests, contracts, and instruction files are treated as untrusted inputs and parsed
  strictly without code execution.

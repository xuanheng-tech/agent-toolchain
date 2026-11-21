# Contributing

Contributions to `agent-toolchain` are welcome under the [Apache-2.0 license](LICENSE).

## Ground Rules

- **Zero runtime dependencies.** Production modules under `src/agent_toolchain/` must use only the
  Python standard library (`requires-python = ">=3.12,<3.15"`).
- **Fail closed.** Missing manifests, hash mismatches, unsafe symlinks, mount points, active
  sockets, drifted production trees, and invalid schemas must return explicit errors without partial
  mutation or silent fallbacks.
- **Preserve CLI and schema contracts.** Keep `tool-skill-sync` and `tool-temp-dir` flags, exit
  codes, JSON output schemas, and `toolchain_compatibility.json` v6 compatibility intact.
- **No private markers or host coupling.** Keep tests self-contained in disposable temporary
  directories (`tempfile.TemporaryDirectory` or `tool-temp-dir`) with isolated `GIT_*` and `HOME`
  environments.

## Local Verification

```bash
ruff check src tests
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
python3 -B -m pytest -q
just check
```

## Contract & Manifest Updates

When updating `toolchain_compatibility.json` or any `manifests/<tool>/tool_skill_manifest.json`,
ensure deterministic canonical JSON formatting (`sort_keys=True, indent=2`, trailing newline) and
verify `tests/test_package_contract.py` passes alongside the full suite.

Please follow [SECURITY.md](SECURITY.md) for reporting security issues.

default:
    just --list

lint:
    if command -v ruff >/dev/null 2>&1; then ruff check src tests; else uv tool run ruff check src tests; fi

test:
    python3 -B -m unittest discover -s tests -p 'test_*.py' -v

check:
    just lint
    just test
    ./tool-skill-sync --version
    ./tool-temp-dir --version

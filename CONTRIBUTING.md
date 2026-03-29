# Contributing to Specivo

We welcome contributions. Before your first pull request can be merged, you'll need to sign our Contributor License Agreement (CLA).

## Why a CLA?

Specivo uses a dual-licensing model: the core is AGPL v3, and enterprise features use a proprietary license. The CLA ensures we can maintain both licenses while you retain full ownership of your contributions.

## How it works

1. Open a pull request
2. The CLA bot will comment with a link to sign
3. Click the link and authenticate with GitHub (one-time)
4. Your PR status will update automatically

## Development setup

Requires: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker (for PostgreSQL + Redis).

```bash
# Clone and install dependencies
git clone https://github.com/specivo/specivo.git
cd specivo
make install

# Start test database (PostgreSQL + Redis)
make test-db-up

# Run migrations and seed default data (roles, statuses, etc.)
make migrate
make seed

# Run tests
make test

# Lint (ruff + mypy)
make lint
```

See [README.md](README.md) for full setup instructions including production deployment.

## Code style

- Python: `ruff` for linting and formatting, `mypy` for type checking
- Run `make lint` before submitting
- Sentence case for all headings and comments (see style guide)
- `from __future__ import annotations` in all new files

## Pull request guidelines

- Keep PRs focused: one feature or fix per PR
- Include tests for new code
- Update documentation if behavior changes
- All tests must pass (`make test`)
- Lint must pass (`make lint`)

## Reporting bugs

Open an issue on GitHub with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Specivo version (`/health` endpoint shows it)

## Feature requests

Open an issue with the `enhancement` label. Describe the use case, not just the solution.

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

```bash
# Clone and install
git clone https://github.com/specivo/specivo.git
cd specivo
make install

# Start test database
make test-db-up

# Run tests
make test

# Lint
make lint
```

See [README.md](README.md) for more details.

## Architecture Decision Records

Before writing code, read the ADRs in `docs/adr/`. They document conventions that aren't obvious from the code:

- **[ADR-0001](docs/adr/0001-frontend-conventions.md)** — Frontend stack (Alpine.js, HTMX, no build step), CSP compliance (no inline scripts), `sp-` CSS prefix convention, Markdown rendering
- **[ADR-0002](docs/adr/0002-backend-testing-strategy.md)** — Test isolation, fixtures, markers (`@pytest.mark.integration`, `@pytest.mark.serial`)
- **[ADR-0003](docs/adr/0003-e2e-testing-with-playwright.md)** — Playwright E2E setup, page objects, test DB lifecycle
- **[ADR-0004](docs/adr/0004-backend-conventions.md)** — API design, service layer, error handling, model conventions

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

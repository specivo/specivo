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

# Build the frontend bundles (generated, never committed; uses a Node
# container when npm is not installed). `make dev-up` rebuilds them on change.
make frontend-build

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

- **[ADR-0001](docs/adr/0001-frontend-conventions.md)** — Frontend stack (Alpine.js, HTMX, esbuild bundles built from `frontend/` and never committed), CSP compliance (no inline scripts), `sp-` CSS prefix convention, Markdown rendering
- **[ADR-0002](docs/adr/0002-backend-testing-strategy.md)** — Test isolation, fixtures, markers (`@pytest.mark.integration`, `@pytest.mark.serial`)
- **[ADR-0003](docs/adr/0003-e2e-testing-with-playwright.md)** — Playwright E2E setup, page objects, test DB lifecycle
- **[ADR-0004](docs/adr/0004-backend-conventions.md)** — API design, service layer, error handling, model conventions

## Code style

- Python: `ruff` for linting and formatting, `mypy` for type checking
- Run `make lint` before submitting
- Sentence case for all headings and comments (see style guide)
- `from __future__ import annotations` in all new files

## Quality gates

CI runs these checks on every pull request to `main` or a `release/*` branch. Run them locally first:

```bash
uv run pre-commit install   # once: ruff, formatting, layer and ESLint checks on every commit
make lint                   # ruff check, ruff format --check, import-linter, mypy
make lint-js                # ESLint on the frontend sources (frontend/js)
make complexity             # informational complexity report (radon)
make test-cov && make coverage-diff   # coverage of the lines you changed
```

- **Ruff rules.** Besides style, ruff checks complexity (`C901`, functions above 15),
  long functions and signatures (`PLR0911`/`PLR0912`/`PLR0913`/`PLR0915`), likely
  bugs (`B`), security (`S`), blocking calls in async code (`ASYNC`), blind
  `except Exception` (`BLE`) and more; see `[tool.ruff.lint]` in `pyproject.toml`.
- **Existing code is grandfathered, new code is not.** Lines that broke these rules
  when they were introduced carry a `# noqa: <code>`. Do not add new `noqa`
  comments to get past a check: split the function, narrow the `except`, or bind
  the SQL parameter instead. When a `noqa` is genuinely needed, say why after it
  (`# noqa: BLE001 - one bad source must not stop the run`). A `noqa` that no
  longer suppresses anything fails the build (`RUF100`), so remove it when you
  fix the line.
- **Types.** `mypy specivo/` must pass. The same rule applies to
  `# type: ignore[<code>]`: always name the error code, prefer fixing the type,
  give a reason when the ignore stays (`# type: ignore[attr-defined]  # added by
  the i18n extension`), and remove it once it is unused (`warn_unused_ignores`).
- **Layers.** `import-linter` keeps the dependency direction: models do not
  import services, services and schemas do not import the API, web, MCP or SCIM
  layers, and core does not import them either. Move shared code down a layer
  instead of importing up.
- **Coverage.** At least 80% of the lines a pull request adds or changes must be
  covered by tests (`diff-cover`).
- **Formatting.** `ruff format`; CI fails on unformatted files.
- **JavaScript.** ESLint checks `frontend/js` and the build script with
  `eslint:recommended`, a few correctness rules and a complexity limit of 15
  (`frontend/eslint.config.mjs`). Vendored libraries and the generated bundles
  are not linted. Violations that predate the gate are counted per file and rule
  in `frontend/eslint-suppressions.json`; new ones fail the build, and so does
  an entry that no longer matches anything. After fixing one, run
  `npm run lint:prune` in `frontend/` to update the file. Do not add entries by
  hand or with `--suppress-all`. Prefix a binding with `_` when it is unused on
  purpose (`catch (_e) {}`). The pre-commit hook needs the frontend
  dependencies installed (`cd frontend && npm ci`, or `make lint-js`).

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

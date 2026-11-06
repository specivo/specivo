# ADR-0005: Responsive Design Testing Strategy

**Date:** 2026-04-11
**Revised:** 2026-09-08 — refreshed the test counts as pages were added. 2026-11-06 — removed the
visual regression baselines (see below); structural assertions are the only automated layer.
**Status:** Accepted
**Deciders:** Boris

## Context

Specivo's E2E tests (ADR-0003) run at the browser's default viewport size — they verify functional behavior but don't test layout at different screen sizes. The CSS defines 4 breakpoints (1100px, 960px, 768px, 480px) with significant layout changes at each: sidebar collapses to off-canvas, grids go single-column, table columns hide, page headers stack vertically. These layout transitions were only tested manually (phone or Chrome DevTools) and any regression was caught reactively.

Needed: a systematic way to document responsive behavior per page type and catch layout regressions automatically.

## Decision

### Breakpoint matrix documentation

A wiki page ("Responsive Design Spec" in the SPECIVO project) documents what each UI element does at each breakpoint. Format: one table per page type, rows = elements, columns = breakpoint tiers. This is the source of truth for what "correct" looks like at each size.

### Four test viewports

Tests run at 4 viewport sizes covering all CSS breakpoints:

| Name | Size | Covers |
|------|------|--------|
| mobile | 375×812 | ≤480px — maximum column hiding, stacked headers |
| tablet | 768×1024 | ≤768px — sidebar off-canvas, some columns hidden |
| narrow | 960×800 | ≤960px — issue detail / analytics collapse |
| desktop | 1280×800 | >1100px — full multi-column layout |

Defined in `tests/e2e/conftest.py` as `VIEWPORTS` dict. The `responsive_page` fixture creates an admin-authed browser context at the parametrized viewport size.

### Seed data isolation

Responsive tests use a dedicated project (`RTEST`) with deterministic seed data: 8 issues, a wiki home page. The `responsive_project` session-scoped fixture creates this idempotently. This avoids dependency on dev server data or other test side effects.

### Test layer

**Structural assertions** (`test_mobile_responsive.py`)

CSS property checks that run at all 4 viewports via `@pytest.mark.parametrize`:
- No horizontal overflow on any page
- Buttons within viewport bounds
- Grid columns match expected layout (single vs multi-column)
- Elements visible/hidden as specified in the breakpoint matrix

Plus viewport-specific classes for mobile-only checks (sidebar hidden, hamburger visible) and desktop-only checks (sidebar visible, two-column issue detail).

### Visual regression baselines (removed)

The first version also compared screenshots of 7 page types at 4 viewports (28 baseline PNGs)
with a Pillow pixel diff. It was removed on 2026-11-06. The baselines went stale with every
intended UI change and differed between macOS and Linux CI, so most of them failed on an
unchanged branch and the suite was already skipped in CI. Structural assertions catch the layout
regressions that matter (overflow, clipping, hidden or visible elements per breakpoint) without
pixel baselines to maintain. Screenshots for the user guide are taken separately by the private
docs tooling.

## Consequences

**Positive:**
- Layout regressions caught automatically at 4 breakpoints (was: only manual testing)
- Breakpoint matrix wiki page serves as design spec and test oracle
- Same pytest/Playwright ecosystem — no new tools

**Negative:**
- The structural tests add test runs at 4 viewports per page
- Subtle visual changes (colours, spacing) are not caught automatically; they are checked by eye

## Makefile targets

```makefile
make test-e2e                    # Runs all E2E including the responsive tests
```

## File locations

| Component | Path |
|-----------|------|
| Viewport definitions + fixtures | `tests/e2e/conftest.py` |
| Structural responsive tests | `tests/e2e/test_mobile_responsive.py` |
| Breakpoint matrix (wiki) | SPECIVO project → "Responsive Design Spec" |

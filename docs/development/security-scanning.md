# Security scanning with Trivy

[Trivy](https://trivy.dev/) scans the repository and the Docker image in two
GitHub Actions workflows:

| Workflow | When | What | Blocks? |
|---|---|---|---|
| `trivy.yml`, job `repository` | push to `main`, pull requests, weekly, manual | `uv.lock` (vulnerabilities; `frontend/package-lock.json` holds only build-time dev dependencies, which Trivy skips), the Dockerfile (misconfigurations), the committed tree (secrets) | No. Results go to the Security tab (code scanning, category `trivy-fs`) |
| `trivy.yml`, job `published-image` | weekly, manual | `specivo/specivo:latest` from Docker Hub | No. Category `trivy-image-latest` |
| `release.yml`, job `scan-image` | release tag | the release image, built for linux/amd64 and not pushed | **Yes.** Nothing is pushed to Docker Hub until it passes |

The release gate fails on HIGH or CRITICAL vulnerabilities **that have a fixed
version** (`--ignore-unfixed`). A Debian package with no fix upstream doesn't
block a release; there is nothing to upgrade to.

Shared settings (skipped directories and files) are in `trivy.yaml` at the repo
root, which Trivy reads automatically when run from there. The Trivy version
is pinned as `TRIVY_VERSION` in both workflows; use the same version locally.

## Running it locally

From the repo root, with the Trivy version from the workflows:

```bash
# Repository: what trivy.yml reports
trivy fs --scanners vuln,misconfig,secret .

# Release gate: what release.yml enforces
docker buildx build --platform linux/amd64 --load -t specivo-release-scan:local .
trivy image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \
  --exit-code 1 specivo-release-scan:local
```

The image build copies the build context, so run it from a clean checkout (a
local `.env` would be copied in and show up in a secret scan).

## When the release gate fails

The `scan-image` job's log prints a table of the findings, and its
`trivy-image-sarif` artifact holds the same results as SARIF. For each row:

1. **A Python package** (Type `python-pkg`): upgrade it in `uv.lock`
   (`uv lock --upgrade-package NAME`), run the tests, and cut the release
   again. Fixes in transitive dependencies are handled the same way.
2. **A Debian package** (Type `debian`): the Dockerfile already runs
   `apt-get upgrade`, so a fix that Debian has published is picked up by
   rebuilding. If the fix is newer than the cached layer, rebuild without the
   cache or bump the base image.
3. **No fix can ship now** (the fixed version breaks Specivo, or the
   vulnerable code is unreachable): accept it in `.trivyignore`, with a reason
   and an expiry date, and get that change reviewed like any other:

   ```text
   # somepkg: affects the XML exporter only, which Specivo doesn't use.
   # The fixed release needs Python 3.13; revisit with the base image bump.
   CVE-2026-00000 exp:2027-01-31
   ```

   Trivy ignores an entry after its `exp:` date, so the gate fails again and
   the decision is revisited instead of forgotten. Don't add entries without
   an expiry, and don't widen the gate (`--severity`, `--exit-code`) to get a
   release out.

A failing gate stops `build-and-push` and `create-release`; nothing was
published. Once the fix is on the release branch, tag the release again from
the fixed commit.

## Reading the weekly report

Findings from `trivy.yml` appear under Security → Code scanning. They don't
fail any check. A HIGH or CRITICAL finding with a fixed version there means
the next release gate will fail on it too, so fix it on the release branch
before tagging.

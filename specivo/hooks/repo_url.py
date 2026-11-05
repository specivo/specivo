"""Read a forge repository address: provider, repository full name and web URL.

One implementation for the server; the repository form mirrors it in
``frontend/js/lib/repo-url.js``. Both are tested against the same table,
``tests/fixtures/repo_urls.json``: change them together.

Accepted: web URLs with or without ``www.``, a trailing slash, a ``.git``
suffix or a sub-page (``/tree/main``, ``/pulls``, GitLab ``/-/…``), and the
SSH forms ``git@host:owner/repo.git`` and ``ssh://git@host[:port]/owner/repo.git``.

- **Known hosts** (``KNOWN_HOSTS``) name the provider and give a web URL cut
  back to the repository root, always ``https``.
- **Other hosts** are self-hosted forges: the provider is whatever was chosen,
  and the repository comes from the path. For GitLab that is every segment
  before ``/-/`` (groups nest); for the others the first two segments.

Anything else (no host, no repository path, odd characters) gives ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Hosts whose provider is known. Codeberg runs Forgejo.
KNOWN_HOSTS: dict[str, str] = {"github.com": "github", "gitlab.com": "gitlab", "codeberg.org": "forgejo"}

#: Providers a known host may be connected as. Codeberg also answers Gitea webhooks.
COMPATIBLE_PROVIDERS: dict[str, frozenset[str]] = {
    "github": frozenset({"github"}),
    "gitlab": frozenset({"gitlab"}),
    "forgejo": frozenset({"forgejo", "gitea"}),
}

_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")
_SCP = re.compile(r"^[A-Za-z0-9_.-]+@(?P<host>[A-Za-z0-9.-]+):(?P<path>[^/].*)$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


@dataclass(frozen=True)
class RepoAddress:
    """What a repository address says.

    ``provider`` is the known host's provider, else the provider passed in
    (``None`` when none was). ``known_host`` tells which. ``url`` is the web
    URL of the repository root: ``https`` for known hosts and SSH input, the
    scheme as given otherwise. ``path`` is every path segment, for the
    self-hosted check in :func:`repo_name_in_path`.
    """

    provider: str | None
    known_host: bool
    host: str
    repo_full_name: str
    url: str
    path: tuple[str, ...]


def _strip_git(segment: str) -> str:
    return segment[:-4] if segment.lower().endswith(".git") else segment


def _repo_segments(segments: list[str], provider: str | None) -> list[str] | None:
    """The segments that name the repository, or None when the path names none."""
    if provider == "gitlab":
        if "-" in segments:
            segments = segments[: segments.index("-")]
        repo = segments
    else:
        repo = segments[:2]
    if len(repo) < 2:
        return None
    repo = [*repo[:-1], _strip_git(repo[-1])]
    if any(not _SEGMENT.fullmatch(s) or s in (".", "..") for s in repo):
        return None
    return repo


def parse_repo_address(raw: str, provider: str | None = None) -> RepoAddress | None:
    """Read *raw* as a repository address; *provider* is the one chosen, used for other hosts.

    Returns None for anything that is not a repository address. Never raises.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > 2048 or any(c.isspace() for c in text):
        return None

    scheme = "https"
    port = ""
    scp = _SCP.match(text)
    if scp is not None and "://" not in text:
        host, path = scp.group("host"), scp.group("path")
        from_ssh = True
    else:
        try:
            parts = urlsplit(text)
            parsed_port = parts.port
        except ValueError:
            return None
        if parts.scheme.lower() not in ("http", "https", "ssh", "git", "git+ssh") or not parts.hostname:
            return None
        from_ssh = parts.scheme.lower() not in ("http", "https")
        host, path = parts.hostname, parts.path
        if not from_ssh:
            scheme = parts.scheme.lower()
            # The web port stays; an SSH port says nothing about the web address.
            port = f":{parsed_port}" if parsed_port else ""
    host = host.lower().rstrip(".")
    if not _HOST.fullmatch(host):
        return None
    bare = host[4:] if host.startswith("www.") else host
    known = KNOWN_HOSTS.get(bare)
    if known is not None:
        host, scheme, port = bare, "https", ""
    effective = known or provider

    segments = [s for s in path.split("/") if s]
    repo = _repo_segments(segments, effective)
    if repo is None:
        return None
    full_name = "/".join(repo)
    return RepoAddress(
        provider=effective,
        known_host=known is not None,
        host=host,
        repo_full_name=full_name,
        url=f"{scheme}://{host}{port}/{full_name}",
        path=tuple(_strip_git(s) for s in segments),
    )


def repo_name_in_path(repo_full_name: str, path: tuple[str, ...]) -> bool:
    """Whether the repository's segments appear, in a row, in the URL path (case aside).

    The check for self-hosted forges: it allows a forge served under a
    sub-path (``https://example.org/git/owner/repo``) and links to sub-pages.
    """
    name = [s.lower() for s in repo_full_name.strip("/").split("/")]
    lowered = [s.lower() for s in path]
    width = len(name)
    return any(lowered[i : i + width] == name for i in range(len(lowered) - width + 1))


def is_ssh_address(raw: str) -> bool:
    """Whether *raw* is written in one of the SSH forms (which the form converts to a web URL)."""
    text = raw.strip()
    if "://" in text:
        return text.split("://", 1)[0].lower() in ("ssh", "git", "git+ssh")
    return _SCP.match(text) is not None

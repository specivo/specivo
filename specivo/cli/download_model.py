"""Download the multilingual-e5-small embedding model into the data directory.

Fetches the two files the local embedder reads, from a pinned Hugging Face
revision, and checks each against its published SHA-256. Run it inside the
``api`` container, where the data directory is mounted at ``/app/data``:

    docker compose exec api python -m specivo.cli.download_model
    # or, from the directory that holds docker-compose.yml:
    make download-model

Files that are already present and match their checksum are skipped, so the
command is safe to re-run; ``--force`` downloads them again. A download goes to
a ``.part`` file that only replaces the target once its checksum matches.

Exit status: 0 when the model is ready, 1 when a download failed, the host is
offline or a checksum did not match.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from specivo.services.local_embedder import _DEFAULT_MODEL_DIR

#: Hugging Face repository and the commit the checksums below belong to.
MODEL_REPO = "intfloat/multilingual-e5-small"
MODEL_REVISION = "fd1525a9fd15316a2d503bf26ab031a61d056e98"
DEFAULT_BASE_URL = "https://huggingface.co"

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ModelFile:
    """One file of the model: where it comes from, where it goes and its checksum."""

    remote_path: str
    filename: str
    sha256: str
    size: int


MODEL_FILES: tuple[ModelFile, ...] = (
    ModelFile(
        remote_path="onnx/model.onnx",
        filename="multilingual-e5-small.onnx",
        sha256="ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665",
        size=470_268_510,
    ),
    ModelFile(
        remote_path="tokenizer.json",
        filename="tokenizer.json",
        sha256="0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
        size=17_082_730,
    ),
)


class DownloadError(Exception):
    """A file could not be downloaded or did not match its checksum."""


class OfflineError(DownloadError):
    """The download host could not be reached at all."""


def _make_client() -> httpx.Client:
    # trust_env (the default) honours HTTPS_PROXY and friends.
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, connect=10.0))


def file_url(base_url: str, model_file: ModelFile) -> str:
    return f"{base_url.rstrip('/')}/{MODEL_REPO}/resolve/{MODEL_REVISION}/{model_file.remote_path}"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _mb(size: int) -> str:
    return f"{size / 1_000_000:.0f} MB" if size >= 1_000_000 else f"{size / 1000:.0f} kB"


def download_file(
    client: httpx.Client,
    url: str,
    model_file: ModelFile,
    dest: Path,
    log: Callable[[str], None],
) -> None:
    """Stream *url* to *dest*, replacing it only when the SHA-256 matches."""
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    received = 0
    next_report = 10
    try:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise DownloadError(f"{model_file.filename}: the server answered HTTP {response.status_code} for {url}")
            total = int(response.headers.get("content-length") or model_file.size)
            with part.open("wb") as fh:
                for block in response.iter_bytes(_CHUNK):
                    fh.write(block)
                    digest.update(block)
                    received += len(block)
                    percent = received * 100 // total if total else 100
                    if percent >= next_report and total >= 10 * _CHUNK:
                        log(f"    {min(percent, 100)}% of {_mb(total)}")
                        next_report = (percent // 10 + 1) * 10
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        part.unlink(missing_ok=True)
        raise OfflineError(f"cannot reach {httpx.URL(url).host}: {exc}") from exc
    except httpx.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise DownloadError(f"{model_file.filename}: download failed: {exc}") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise

    actual = digest.hexdigest()
    if actual != model_file.sha256:
        part.unlink(missing_ok=True)
        raise DownloadError(
            f"{model_file.filename}: checksum mismatch (expected {model_file.sha256}, got {actual}); "
            "the file was discarded"
        )
    os.replace(part, dest)


def download_model(
    model_dir: Path,
    *,
    force: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    client: httpx.Client | None = None,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Make sure every model file is in *model_dir* with the right checksum.

    Returns the names of the files that were downloaded. Raises
    :class:`OfflineError` or :class:`DownloadError` on failure.
    """
    model_dir.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    if client is None:
        client = _make_client()
    downloaded: list[str] = []
    try:
        for model_file in MODEL_FILES:
            dest = model_dir / model_file.filename
            if dest.exists() and not force:
                if sha256_of(dest) == model_file.sha256:
                    log(f"  ok    {model_file.filename} (already present, checksum matches)")
                    continue
                log(f"  !     {model_file.filename} is present but its checksum does not match; downloading again")
            log(f"  get   {model_file.filename} ({_mb(model_file.size)})")
            download_file(client, file_url(base_url, model_file), model_file, dest, log)
            log(f"  ok    {model_file.filename} (checksum verified)")
            downloaded.append(model_file.filename)
    finally:
        if own_client:
            client.close()
    return downloaded


def offline_help(model_dir: Path, base_url: str) -> str:
    lines = [
        "",
        "This host cannot download the model. Fetch the files on a machine that can,",
        f"for example with scripts/download-model.sh, and copy them into {model_dir}/:",
        "",
    ]
    for model_file in MODEL_FILES:
        lines.append(f"  {model_file.filename}")
        lines.append(f"    from    {file_url(base_url, model_file)}")
        lines.append(f"    sha256  {model_file.sha256}")
    lines += [
        "",
        "Behind a proxy, set HTTPS_PROXY for the api container and run this command again.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m specivo.cli.download_model",
        description=f"Download the {MODEL_REPO} embedding model (revision {MODEL_REVISION[:12]}) for semantic search.",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=_DEFAULT_MODEL_DIR,
        help=f"Target directory (default: {_DEFAULT_MODEL_DIR}, i.e. /app/data/models in the container).",
    )
    parser.add_argument("--force", action="store_true", help="Download the files even when they are already present.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SPECIVO_MODEL_BASE_URL", DEFAULT_BASE_URL),
        help="Hugging Face compatible mirror (default: $SPECIVO_MODEL_BASE_URL or %(default)s).",
    )
    args = parser.parse_args(argv)

    total = sum(f.size for f in MODEL_FILES)
    print(f"Embedding model {MODEL_REPO} @ {MODEL_REVISION[:12]} (about {_mb(total)}) into {args.dir}/")
    try:
        downloaded = download_model(args.dir, force=args.force, base_url=args.base_url)
    except OfflineError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        print(offline_help(args.dir, args.base_url), file=sys.stderr)
        return 1
    except (DownloadError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print("Model ready." if downloaded else "Model already up to date.")
    print("Next: index existing content with  python -m specivo.cli.backfill_embeddings  (make backfill-embeddings).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

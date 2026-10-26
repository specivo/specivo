"""The container-side model download command, against a mocked HTTP server."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import httpx
import pytest

from specivo.cli import download_model as dl

pytestmark = pytest.mark.unit

_ONNX = b"onnx weights " * 4096
_TOKENIZER = b'{"model": "tokenizer"}'


def _file(remote: str, name: str, body: bytes) -> dl.ModelFile:
    return dl.ModelFile(remote_path=remote, filename=name, sha256=hashlib.sha256(body).hexdigest(), size=len(body))


@pytest.fixture
def small_model(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """Swap the real ~487 MB file list for two small files; returns path -> body."""
    files = (
        _file("onnx/model.onnx", "multilingual-e5-small.onnx", _ONNX),
        _file("tokenizer.json", "tokenizer.json", _TOKENIZER),
    )
    monkeypatch.setattr(dl, "MODEL_FILES", files)
    return {
        f"/{dl.MODEL_REPO}/resolve/{dl.MODEL_REVISION}/onnx/model.onnx": _ONNX,
        f"/{dl.MODEL_REPO}/resolve/{dl.MODEL_REVISION}/tokenizer.json": _TOKENIZER,
    }


def _client(bodies: dict[str, bytes], requests: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request.url.path)
        body = bodies.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_downloads_pinned_files_and_verifies_them(tmp_path: Path, small_model):
    requests: list[str] = []
    downloaded = dl.download_model(tmp_path, client=_client(small_model, requests), log=lambda _m: None)

    assert downloaded == ["multilingual-e5-small.onnx", "tokenizer.json"]
    assert (tmp_path / "multilingual-e5-small.onnx").read_bytes() == _ONNX
    assert (tmp_path / "tokenizer.json").read_bytes() == _TOKENIZER
    # Every URL names the pinned revision, never a moving branch.
    assert all(f"/resolve/{dl.MODEL_REVISION}/" in path for path in requests)
    assert not list(tmp_path.glob("*.part"))


def test_skips_files_that_match_and_force_downloads_again(tmp_path: Path, small_model):
    dl.download_model(tmp_path, client=_client(small_model), log=lambda _m: None)

    requests: list[str] = []
    assert dl.download_model(tmp_path, client=_client(small_model, requests), log=lambda _m: None) == []
    assert requests == []

    forced = dl.download_model(tmp_path, force=True, client=_client(small_model, requests), log=lambda _m: None)
    assert forced == ["multilingual-e5-small.onnx", "tokenizer.json"]
    assert len(requests) == 2


def test_replaces_a_present_file_with_a_wrong_checksum(tmp_path: Path, small_model):
    (tmp_path / "tokenizer.json").write_bytes(b"truncated")
    dl.download_model(tmp_path, client=_client(small_model), log=lambda _m: None)
    assert (tmp_path / "tokenizer.json").read_bytes() == _TOKENIZER


def test_checksum_mismatch_fails_and_keeps_nothing(tmp_path: Path, small_model):
    tampered = dict(small_model)
    tampered[next(k for k in tampered if k.endswith("tokenizer.json"))] = b'{"model": "tampered"}'

    with pytest.raises(dl.DownloadError, match="tokenizer.json: checksum mismatch"):
        dl.download_model(tmp_path, client=_client(tampered), log=lambda _m: None)

    assert not (tmp_path / "tokenizer.json").exists()
    assert not list(tmp_path.glob("*.part"))


def test_http_error_status_fails(tmp_path: Path, small_model):
    with pytest.raises(dl.DownloadError, match="HTTP 404"):
        dl.download_model(tmp_path, client=_client({}), log=lambda _m: None)


def test_offline_raises_offline_error(tmp_path: Path, small_model):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    client = httpx.Client(transport=httpx.MockTransport(unreachable))
    with pytest.raises(dl.OfflineError, match="cannot reach huggingface.co"):
        dl.download_model(tmp_path, client=client, log=lambda _m: None)
    assert not list(tmp_path.iterdir())


def test_main_offline_exits_1_with_air_gap_instructions(tmp_path: Path, small_model, monkeypatch, capsys):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known", request=request)

    monkeypatch.setattr(dl, "_make_client", lambda: httpx.Client(transport=httpx.MockTransport(unreachable)))

    assert dl.main(["--dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "cannot reach huggingface.co" in err
    assert "copy them into" in err
    for model_file in dl.MODEL_FILES:
        assert model_file.sha256 in err


def test_main_succeeds_and_points_to_the_backfill(tmp_path: Path, small_model, monkeypatch, capsys):
    monkeypatch.setattr(dl, "_make_client", lambda: _client(small_model))

    assert dl.main(["--dir", str(tmp_path / "models")]) == 0
    assert "Model ready." in capsys.readouterr().out
    assert (tmp_path / "models" / "tokenizer.json").exists()


def test_main_checksum_mismatch_exits_1(tmp_path: Path, small_model, monkeypatch, capsys):
    tampered = {k: v + b"x" for k, v in small_model.items()}
    monkeypatch.setattr(dl, "_make_client", lambda: _client(tampered))

    assert dl.main(["--dir", str(tmp_path)]) == 1
    assert "checksum mismatch" in capsys.readouterr().err


def test_files_match_what_the_local_embedder_loads():
    names = {f.filename for f in dl.MODEL_FILES}
    assert names == {"multilingual-e5-small.onnx", "tokenizer.json"}
    assert all(re.fullmatch(r"[0-9a-f]{64}", f.sha256) for f in dl.MODEL_FILES)
    assert re.fullmatch(r"[0-9a-f]{40}", dl.MODEL_REVISION)


def test_host_script_pins_the_same_revision_and_checksums():
    script = (Path(__file__).resolve().parents[2] / "scripts" / "download-model.sh").read_text()
    assert f'REVISION="{dl.MODEL_REVISION}"' in script
    for model_file in dl.MODEL_FILES:
        assert f'"{model_file.remote_path}|{model_file.filename}|{model_file.sha256}"' in script
    # No reference to a release asset that is not published.
    assert "specivo-models.tar.gz" not in script

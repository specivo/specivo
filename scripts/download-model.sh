#!/usr/bin/env bash
# Download the multilingual-e5-small embedding model on the host.
#
# Usage:
#   bash scripts/download-model.sh                  # into ${SPECIVO_DATA_DIR:-./specivo-data}/models
#   bash scripts/download-model.sh /custom/path     # into another directory
#
# On a running install the simpler route is the container command, which needs
# nothing on the host:
#   docker compose exec api python -m specivo.cli.download_model   (or: make download-model)
#
# This script is for preparing the files on a connected machine for an
# air-gapped host: run it there, then copy the directory to
# <SPECIVO_DATA_DIR>/models/ on the target.
#
# Model: intfloat/multilingual-e5-small (MIT License), pinned to the revision
# below. Each file is checked against its SHA-256; keep these values in step
# with specivo/cli/download_model.py.
# Size: about 487 MB (ONNX fp32 model + tokenizer). Needs curl and sha256sum or shasum.

set -euo pipefail

MODEL_DIR="${1:-${SPECIVO_DATA_DIR:-./specivo-data}/models}"
BASE_URL="${SPECIVO_MODEL_BASE_URL:-https://huggingface.co}"
REVISION="fd1525a9fd15316a2d503bf26ab031a61d056e98"
REPO_URL="${BASE_URL%/}/intfloat/multilingual-e5-small/resolve/${REVISION}"

# remote path | local file name | sha256
FILES=(
    "onnx/model.onnx|multilingual-e5-small.onnx|ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665"
    "tokenizer.json|tokenizer.json|0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39"
)

if command -v sha256sum >/dev/null 2>&1; then
    sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
    sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
    echo "Error: neither sha256sum nor shasum is installed." >&2
    exit 1
fi

echo "Embedding model multilingual-e5-small @ ${REVISION:0:12} (about 487 MB) into ${MODEL_DIR}/"

if ! curl -sI --connect-timeout 10 "$BASE_URL" >/dev/null 2>&1; then
    echo "Error: cannot reach ${BASE_URL}." >&2
    echo "Run this script on a machine with internet access and copy ${MODEL_DIR}/ across," >&2
    echo "or fetch these files by hand and check their SHA-256:" >&2
    for entry in "${FILES[@]}"; do
        IFS='|' read -r remote name sum <<<"$entry"
        echo "  ${name}  from ${REPO_URL}/${remote}" >&2
        echo "    sha256 ${sum}" >&2
    done
    exit 1
fi

mkdir -p "$MODEL_DIR"

for entry in "${FILES[@]}"; do
    IFS='|' read -r remote name sum <<<"$entry"
    dest="${MODEL_DIR}/${name}"
    if [ -f "$dest" ] && [ "$(sha256 "$dest")" = "$sum" ]; then
        echo "  ok    ${name} (already present, checksum matches)"
        continue
    fi
    echo "  get   ${name}"
    if ! curl -fL --progress-bar "${REPO_URL}/${remote}" -o "${dest}.part"; then
        rm -f "${dest}.part"
        echo "Error: failed to download ${name}." >&2
        exit 1
    fi
    actual="$(sha256 "${dest}.part")"
    if [ "$actual" != "$sum" ]; then
        rm -f "${dest}.part"
        echo "Error: checksum mismatch for ${name} (expected ${sum}, got ${actual}); the file was discarded." >&2
        exit 1
    fi
    mv "${dest}.part" "$dest"
    echo "  ok    ${name} (checksum verified)"
done

echo "Model ready. Next, index existing content:"
echo "  docker compose exec api python -m specivo.cli.backfill_embeddings   (or: make backfill-embeddings)"

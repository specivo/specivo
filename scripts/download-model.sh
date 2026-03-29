#!/usr/bin/env bash
# Download the bundled multilingual-e5-small embedding model.
# Called during Docker build and on first-run setup.
#
# Model: intfloat/multilingual-e5-small (MIT License)
# Size: ~393 MB total (ONNX fp32 + tokenizer)
# Languages: 100 | Dimensions: 384 | Parameters: 118M

set -euo pipefail

MODEL_DIR="${1:-specivo/static/models}"
HF_BASE="https://huggingface.co/intfloat/multilingual-e5-small/resolve/main"

mkdir -p "$MODEL_DIR"

download() {
    local url="$1"
    local dest="$2"
    if [ -f "$dest" ]; then
        echo "  [skip] $(basename "$dest") already exists"
        return
    fi
    echo "  [download] $(basename "$dest")..."
    curl -sL "$url" -o "$dest"
}

echo "Downloading multilingual-e5-small embedding model..."
download "$HF_BASE/onnx/model.onnx"         "$MODEL_DIR/multilingual-e5-small.onnx"
download "$HF_BASE/tokenizer.json"           "$MODEL_DIR/tokenizer.json"
download "$HF_BASE/tokenizer_config.json"    "$MODEL_DIR/tokenizer_config.json"
download "$HF_BASE/special_tokens_map.json"  "$MODEL_DIR/special_tokens_map.json"
download "$HF_BASE/sentencepiece.bpe.model"  "$MODEL_DIR/sentencepiece.bpe.model"

echo "Done. Model files in $MODEL_DIR:"
ls -lh "$MODEL_DIR/"

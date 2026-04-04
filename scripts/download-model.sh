#!/usr/bin/env bash
# Download the bundled multilingual-e5-small embedding model.
#
# Usage:
#   make download-model              # interactive, shows progress
#   bash scripts/download-model.sh   # same, default to data/models/
#   bash scripts/download-model.sh /custom/path
#
# Model: intfloat/multilingual-e5-small (MIT License)
# Size: ~393 MB total (ONNX fp32 + tokenizer)
# Languages: 100 | Dimensions: 384 | Parameters: 118M
#
# Air-gap: If you cannot download, get the model files from the GitHub
# releases page (specivo-models.tar.gz) and extract to the model dir.

set -euo pipefail

MODEL_DIR="${1:-${SPECIVO_DATA_DIR:-specivo-data}/models}"
HF_BASE="https://huggingface.co/intfloat/multilingual-e5-small/resolve/main"

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo -e "${BOLD}Specivo Embedding Model Setup${NC}"
echo -e "Model: multilingual-e5-small (MIT License)"
echo -e "Size:  ~393 MB total"
echo ""

# Check connectivity
if ! curl -sI --connect-timeout 5 "https://huggingface.co" >/dev/null 2>&1; then
    echo -e "${RED}Error: Cannot reach huggingface.co${NC}"
    echo ""
    echo "For air-gapped environments:"
    echo "  1. Download from GitHub releases: specivo-models.tar.gz"
    echo "  2. Extract to: ${MODEL_DIR}/"
    echo ""
    echo "Required files:"
    echo "  - multilingual-e5-small.onnx  (~370 MB)"
    echo "  - tokenizer.json              (~1 MB)"
    echo "  - tokenizer_config.json"
    echo "  - special_tokens_map.json"
    echo "  - sentencepiece.bpe.model"
    exit 1
fi

mkdir -p "$MODEL_DIR"

download() {
    local url="$1"
    local dest="$2"
    local name
    name="$(basename "$dest")"

    if [ -f "$dest" ]; then
        echo -e "  ${GREEN}✓${NC} ${name} (already downloaded)"
        return
    fi
    echo -e "  ${YELLOW}↓${NC} Downloading ${name}..."
    if curl -L --progress-bar "$url" -o "${dest}.tmp"; then
        mv "${dest}.tmp" "$dest"
        echo -e "  ${GREEN}✓${NC} ${name}"
    else
        rm -f "${dest}.tmp"
        echo -e "  ${RED}✗${NC} Failed to download ${name}"
        return 1
    fi
}

echo "Downloading to ${MODEL_DIR}/..."
echo ""
download "$HF_BASE/onnx/model.onnx"         "$MODEL_DIR/multilingual-e5-small.onnx"
download "$HF_BASE/tokenizer.json"           "$MODEL_DIR/tokenizer.json"
download "$HF_BASE/tokenizer_config.json"    "$MODEL_DIR/tokenizer_config.json"
download "$HF_BASE/special_tokens_map.json"  "$MODEL_DIR/special_tokens_map.json"
download "$HF_BASE/sentencepiece.bpe.model"  "$MODEL_DIR/sentencepiece.bpe.model"

echo ""
echo -e "${GREEN}${BOLD}Done!${NC} Embedding model ready."
echo ""
echo "Model files:"
ls -lh "$MODEL_DIR/" 2>/dev/null | grep -v "^total"
echo ""
echo "Next steps:"
echo "  1. Restart the application:  make dev-up"
echo "  2. Backfill existing data:   make backfill-embeddings"
echo ""

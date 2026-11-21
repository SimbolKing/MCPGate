#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

REPOS=(
  "upstream/dify|https://github.com/langgenius/dify.git|main"
  "upstream/higress|https://github.com/higress-group/higress.git|main"
  "upstream/openapi-to-mcpserver|https://github.com/higress-group/openapi-to-mcpserver.git|main"
)

for item in "${REPOS[@]}"; do
  IFS='|' read -r dir url branch <<< "$item"
  if [ -e "$dir" ]; then
    echo "skip: $dir already exists"
  else
    mkdir -p "$(dirname "$dir")"
    git clone --filter=blob:none --branch "$branch" "$url" "$dir"
  fi
done

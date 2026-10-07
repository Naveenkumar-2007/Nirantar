#!/usr/bin/env bash
# Assemble exactly what the Hugging Face Space needs (no secrets, no tests, no data) into $1.
set -euo pipefail
OUT=${1:?usage: space_bundle.sh <out-dir>}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
rm -rf "$OUT" && mkdir -p "$OUT/apps" "$OUT/infra/compose" "$OUT/deploy"
cd "$ROOT"
cp Dockerfile pyproject.toml uv.lock alembic.ini "$OUT/"
cp Dockerfile.dockerignore "$OUT/.dockerignore"
cp deploy/space/README.md "$OUT/README.md"
cp -r src ml "$OUT/"
mkdir -p "$OUT/docs" "$OUT/evals" && cp -r docs/compliance "$OUT/docs/" && cp -r evals/results "$OUT/evals/"
cp -r deploy/space "$OUT/deploy/space"
cp -r infra/compose/postgres-init "$OUT/infra/compose/postgres-init"
# the web app without build output, dependencies or local env files
tar -C apps --exclude='web/node_modules' --exclude='web/.next' --exclude='web/.env*' -cf - web | tar -C "$OUT/apps" -xf -
find "$OUT" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -f "$OUT/ml/mlflow.db"
# refuse to ship anything that looks like a secret file
if find "$OUT" \( -name '.env' -o -name '.env.*' -o -name '*.pem' -o -name '*.key' \) | grep -q .; then
  echo "refusing: secret-looking file in bundle" >&2; exit 1
fi
echo "bundle ready: $OUT ($(du -sh "$OUT" | cut -f1))"

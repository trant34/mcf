#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python -m grpc_tools.protoc \
  -I api/proto \
  --python_out=services/ai_engine/src/pb \
  --grpc_python_out=services/ai_engine/src/pb \
  api/proto/ai_service.proto

protoc \
  -I api/proto \
  --go_out=services/logic/internal/pb/api/proto \
  --go_opt=paths=source_relative \
  --go-grpc_out=services/logic/internal/pb/api/proto \
  --go-grpc_opt=paths=source_relative \
  api/proto/ai_service.proto
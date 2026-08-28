#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ---------------------------------------------------------------------------
# ai_service.proto  (RTPGW <-> AI Engine)
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# video_ingest.proto  (MF <-> RTPGW)  -- NEW in v3.1
#   - Go stubs: RTPGW is the gRPC server (services/logic).
#   - C++ stubs: MF is the gRPC client (mf_cpp), needs grpc_cpp_plugin.
#     Requires: apt-get install -y protobuf-compiler-grpc libgrpc++-dev
#     (or build grpc from source: https://github.com/grpc/grpc/tree/master/src/cpp)
# ---------------------------------------------------------------------------
protoc \
  -I api/proto \
  --go_out=services/logic/internal/pb/api/proto \
  --go_opt=paths=source_relative \
  --go-grpc_out=services/logic/internal/pb/api/proto \
  --go-grpc_opt=paths=source_relative \
  api/proto/video_ingest.proto

GRPC_CPP_PLUGIN="$(command -v grpc_cpp_plugin || true)"
if [ -z "$GRPC_CPP_PLUGIN" ]; then
  echo "WARN: grpc_cpp_plugin not found on PATH, skipping mf_cpp stub generation." >&2
  echo "      Install it (apt-get install -y protobuf-compiler-grpc libgrpc++-dev)" >&2
  echo "      and re-run this script before building mf_cpp." >&2
else
  mkdir -p mf_cpp/generated
  protoc \
    -I api/proto \
    --cpp_out=mf_cpp/generated \
    --grpc_out=mf_cpp/generated \
    --plugin=protoc-gen-grpc="$GRPC_CPP_PLUGIN" \
    api/proto/video_ingest.proto
fi

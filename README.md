# Python grpc tools
```
python -m pip install --user grpcio grpcio-tools
```
# Go protoc plugins (yêu cầu GOPATH/bin trong PATH)
```
go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@latest
export PATH="$PATH:$(go env GOPATH)/bin"
```

# từ repo root
```
python -m grpc_tools.protoc -I=api/proto \
  --python_out=services/ai_engine/src/pb \
  --grpc_python_out=services/ai_engine/src/pb \
  api/proto/ai_service.proto
```

# từ repo root
```
protoc -I=api/proto \
  --go_out=services/logic/internal/pb/api/proto --go_opt=paths=source_relative \
  --go-grpc_out=services/logic/internal/pb/api/proto --go-grpc_opt=paths=source_relative \
  api/proto/ai_service.proto
```

# port

8080  → HTTPGW của LOGIC

50052 → gRPC của AI Engine

5004  → RTPGW của LOGIC

5006  → RTP receiver riêng cho MF realtime test


# chạy AI Engine
```
cd mcf/services/ai_engine/src
python ai_server.py
```
# chạy MCF server
```
cd mcf/services/logic
go run cmd/mcf_server/main.go
```
# chạy MF
```
python -u mf_video/mf_v1.py \
  --listen-host 127.0.0.1 \
  --listen-port 5006 \
  --payload-type 114 \
  --ssrc 0x5cccb090 \
  --width 240 \
  --height 320 \
  --fps 15 \
  --infer-fps 5 \
  --infer-width 256 \
  --infer-height 144 \
  --mcf-url http://127.0.0.1:8080/v1/video/infer \
  --effect bg_replace \
  --background services/ai_engine/model_checkpoints/bg_image.jpg \
  --output output_ver2_grpc.mp4

# replay PCAP
```
cd mcf
python -u mf_video/pcap_replay.py \
  --pcap mf_video/input_video.txt \
  --src-port 10444 \
  --dst-port 10448 \
  --target-host 127.0.0.1 \
  --target-port 5006 \
  --speed 1.0
```

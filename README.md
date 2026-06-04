```markdown
mcf/
├── api/                        # Chứa các file định nghĩa giao thức giao tiếp chung
│   └── proto/
│       ├── control.proto       # Định nghĩa API giữa HTTPGW và LOGIC
│       ├── media.proto         # Định nghĩa luồng dữ liệu giữa RTPGW và LOGIC
│       └── ai_service.proto    # Định nghĩa luồng dữ liệu gRPC giữa LOGIC và AI Engine
├── deployments/                # Chứa cấu hình triển khai hạ tầng
│   ├── docker/                 # (Tùy chọn) Các docker-compose cho môi trường local dev
│   └── k8s/                    # Các file K8s YAML hoặc Helm charts
│       ├── rtpgw.yaml
│       ├── logic.yaml
│       ├── httpgw.yaml
│       └── ai-engine.yaml
├── services/                   # Mã nguồn của các microservices
│   ├── rtpgw/                  # [C++] Xử lý I/O mạng thời gian thực
│   │   ├── src/
│   │   ├── include/
│   │   ├── CMakeLists.txt      # Cấu hình biên dịch C++
│   │   └── Dockerfile          # Sử dụng base image Alpine/Ubuntu có cài sẵn thư viện DPDK
│   ├── logic/                  # [Golang] Điều phối session và stream dữ liệu
│   │   ├── cmd/
│   │   ├── internal/
│   │   ├── go.mod
│   │   └── Dockerfile          # Multi-stage build, base image distroless
│   ├── httpgw/                 # [Golang] Xử lý I/O HTTP/2 với mạng ngoài
│   │   ├── cmd/
│   │   ├── internal/
│   │   ├── go.mod
│   │   └── Dockerfile          # Multi-stage build
│   └── ai_engine/              # [Python] Xử lý mô hình ASR/MT/TTS
│       ├── src/
│       ├── requirements.txt    # Chứa PyTorch, Transformers, grpcio...
│       └── Dockerfile          # Sử dụng base image NVIDIA CUDA (ví dụ: nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04)
├── scripts/                    # Các shell script phục vụ CI/CD (build, push image, generate protobuf)
└── README.md
```
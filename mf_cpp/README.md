# mf_cpp — C++ port of `mf_v1.py` (Mode-B MF prototype)

Bản chuyển đổi C++ của phân hệ **MF (Media Function)**, giữ nguyên kiến trúc
và luồng xử lý của `mf_v1.py`:

```
RTP/H264 --(UDP)--> depacketize --> ffmpeg decode --> sampled JPEG
        --(HTTP)--> MCF HTTPGW --> mask (RLE) --> composite --> MP4 output
```

## Cấu trúc thư mục

```
mf_cpp/
├── CMakeLists.txt
├── include/
│   ├── args.hpp                 # struct Args (tương ứng argparse trong Python)
│   ├── rtp.hpp                  # RTPPacket + parse_rtp()
│   ├── latest_queue.hpp         # LatestQueue<T> (mirror queue.Queue drop-oldest)
│   ├── access_unit_assembler.hpp
│   ├── h264_depacketizer.hpp    # STAP-A / FU-A / single NALU -> Annex-B
│   ├── ffmpeg_decoder.hpp       # spawn `ffmpeg` qua pipe, đọc rawvideo bgr24
│   ├── mask_store.hpp           # thread-safe mask cache (mirror MaskStore)
│   ├── rle.hpp                  # decode_rle() -> cv::Mat
│   ├── mcf_client.hpp           # POST JPEG tới MCF, parse JSON response
│   └── mf_mode_b.hpp            # class MFModeB (orchestrator chính)
├── src/
    ├── args.cpp
    ├── ffmpeg_decoder.cpp
    ├── mcf_client.cpp
    ├── mf_mode_b.cpp
    └── main.cpp
```

## Mapping Python → C++

| Python (`mf_v1.py`)           | C++                                   |
|-------------------------------|---------------------------------------|
| `parse_rtp()`                 | `rtp.hpp::parse_rtp()`                |
| `LatestQueue`                 | `latest_queue.hpp::LatestQueue<T>`    |
| `AccessUnitAssembler`         | `access_unit_assembler.hpp`           |
| `H264Depacketizer`            | `h264_depacketizer.hpp`               |
| `FFmpegDecoder`               | `ffmpeg_decoder.{hpp,cpp}` (fork/exec + pipe thay cho `subprocess.Popen`) |
| `MaskStore`                   | `mask_store.hpp` (mutex thay cho `threading.Lock`) |
| `decode_rle()`                | `rle.hpp`                              |
| `MCFInferenceClient`          | `mcf_client.{hpp,cpp}` (libcurl thay cho `requests`) |
| `MF`                     | `mf.{hpp,cpp}` (POSIX UDP socket thay cho `socket` module) |
| `argparse`                    | `args.{hpp,cpp}` (parser thủ công, cùng tên flag) |

Toàn bộ tham số dòng lệnh (`--listen-host`, `--width`, `--effect`,
`--mcf-url`, ...) giữ nguyên tên và giá trị mặc định như bản Python.

## Phụ thuộc (dependencies)

- CMake >= 3.16, C++17
- OpenCV (core, imgproc, imgcodecs, videoio) — dùng cho `cv::Mat`, resize,
  GaussianBlur, JPEG encode, `cv::VideoWriter` (mp4v) — tương đương `cv2`
  trong bản Python.
- libcurl — HTTP client tới MCF HTTPGW (thay cho `requests`).
- nlohmann/json — parse JSON response từ MCF (dùng bản hệ thống nếu có,
  nếu không sẽ fallback sang header đơn trong `third_party/`).
- `ffmpeg` binary phải có trong PATH (hoặc chỉ định qua `--ffmpeg`), vì
  decoder gọi `ffmpeg` như một subprocess giống hệt bản Python.

Trên Ubuntu/Debian:
```bash
sudo apt-get install -y cmake libopencv-dev libcurl4-openssl-dev \
    nlohmann-json3-dev pkg-config ffmpeg
```

## Build

```bash
cd mf_cpp
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j$(nproc)
```

Sinh ra binary `build/mf_cpp`.

## Chạy

Các flag giống hệt `mf_v1.py`:

```bash
./mf_cpp \
  --listen-host 127.0.0.1 --listen-port 5006 \
  --payload-type 114 --ssrc 0x5cccb090 \
  --width 240 --height 320 --fps 15 \
  --infer-fps 5 --infer-width 256 --infer-height 144 \
  --mcf-url http://127.0.0.1:8080/v1/video/infer \
  --session-id CALL-VIDEO-TEST --stream-id video-0 \
  --effect bg_replace \
  --background ../services/ai_engine/model_checkpoints/bg_image.jpg \
  --output output_test_cpp.mp4
```

## Đã kiểm thử (smoke test)

Đã build và chạy thử end-to-end trong môi trường sandbox: gửi một luồng
RTP/H264 (payload STAP-A/single-NALU/FU-A tự tạo) tới `mf_cpp`, dùng một
MCF HTTPGW giả lập (Python `http.server`) trả về mask RLE giả — pipeline
chạy đúng: nhận gói RTP → tách access unit → decode qua `ffmpeg` → gửi
JPEG lên MCF giả lập → nhận mask → composite → ghi ra MP4 hợp lệ, và
tiến trình thoát sạch (không treo) khi hết luồng input (`end-idle-seconds`).

Trong lúc test, đã phát hiện và sửa một race condition thực sự giữa luồng
đọc stdout của `ffmpeg` và luồng chính khi tắt decoder (`FFmpegDecoder::close()`):
luồng đọc phải tiếp tục "drain" stdout cho đến khi tiến trình `ffmpeg` con
thực sự thoát, nếu không `ffmpeg` sẽ bị block ở `write()` (pipe đầy) và
không bao giờ thoát, khiến `mf_cpp` treo ở bước dọn dẹp. Đã sửa để
`close()` chờ tiến trình con thoát (có timeout + SIGTERM dự phòng) rồi
mới join luồng đọc.

## Những điểm khác biệt / lưu ý khi tích hợp

- Xử lý tín hiệu, log ra `stdout` bằng `std::cout` giống `print(..., flush=True)`.
- `MaskStore`, `LatestQueue` dùng `std::mutex`/`std::condition_variable`
  thay cho `threading.Lock`/`queue.Queue`.
- `FFmpegDecoder` dùng `fork()`/`execlp()`/pipe POSIX (chỉ chạy trên Linux),
  tương đương `subprocess.Popen` trong bản Python.
- `MCFInferenceClient` dùng libcurl đồng bộ (blocking), giữ đúng ngữ nghĩa
  `requests.Session` (một connection pool nhỏ, timeout theo `--http-timeout`).
- Toàn bộ logic nghiệp vụ (depacketize H.264, ghép access unit, decode
  RLE mask, composite alpha blend) được port 1:1 theo đúng thuật toán của
  `mf_v1.py`, không thay đổi hành vi.

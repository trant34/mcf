#pragma once
#include <atomic>
#include <string>
#include <thread>

#include "mask_store.hpp"

namespace mf {

// MaskCallbackServer: new in v3.1 (rtpgw_design_v3.md section 35).
//
// Runs a small HTTP server MF listens on so the AI Engine can push mask RLE
// results the moment a MediaPipe LIVE_STREAM `segment_async` callback
// fires, instead of MF blocking on a synchronous request/response per
// frame. On POST it decodes the RLE JSON body and writes straight into the
// shared MaskStore that MF::compose() already reads from -- so the
// compositor loop (mf.cpp) does not need to change at all to pick up
// results delivered this way.
//
// Expected JSON body (matches ai_engine/src/video/mf_callback_client.py):
//   {
//     "session_id": "...", "stream_id": "...",
//     "frame_id": 123, "rtp_timestamp": 456789,
//     "mask_width": 144, "mask_height": 256,
//     "rle_counts": [0, 120, 30, ...],
//     "latency_ms": 12.3, "status": "ok" | "error",
//     "error_message": "..."            // only when status == "error"
//   }
class MaskCallbackServer {
public:
    // listen_addr like "0.0.0.0", listen_port like 8090, path like "/v1/video/mask".
    MaskCallbackServer(std::string listen_addr, int listen_port, std::string path,
                        MaskStore& mask_store, std::string expected_session_id);
    ~MaskCallbackServer();

    void start();
    void stop();

    long received_count() const { return received_count_.load(); }
    long error_count() const { return error_count_.load(); }

private:
    std::string listen_addr_;
    int listen_port_;
    std::string path_;
    MaskStore& mask_store_;
    std::string expected_session_id_;

    std::atomic<long> received_count_{0};
    std::atomic<long> error_count_{0};

    std::thread server_thread_;
    // Opaque pointer so this header does not need to pull in httplib.h
    // (22k+ lines) into every translation unit that includes mf.hpp.
    void* server_impl_ = nullptr;
};

} // namespace mf

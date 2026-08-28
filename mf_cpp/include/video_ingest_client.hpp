#pragma once
#include <atomic>
#include <functional>
#include <string>
#include <thread>

#include "rtp.hpp"

namespace mf {

// VideoIngestClient: v3.2 (rtpgw_design_v3.md section 37).
//
// Streams every RTP packet MF receives to RTPGW over a persistent
// bidirectional gRPC stream (api/proto/video_ingest.proto,
// VideoIngestService.Transport). As of v3.2, this is now genuinely
// bidirectional in the sense MF actually needs: RTPGW decodes ONCE and
// streams every decoded frame straight back over the SAME connection
// (IngestResponse.frame) -- MF no longer decodes anything itself, it
// just forwards RTP out and consumes decoded frames in on this same
// client via on_frame().
//
// Requires generated stubs in mf_cpp/generated/video_ingest.{pb,grpc.pb}.h,
// produced by scripts/generate_proto.sh (needs protoc + grpc_cpp_plugin).
class VideoIngestClient {
public:
    // on_frame is called (on this client's own reader thread) for every
    // DecodedFrame RTPGW streams back. bgr points into an internal buffer
    // valid only for the duration of the callback -- copy it if you need
    // to keep it. width/height should match decode_width/decode_height
    // passed to the constructor below; MF::on_decoded_frame() should still
    // defensively check this rather than assume it.
    using FrameCallback = std::function<void(const uint8_t* bgr, int width, int height, uint32_t rtp_ts)>;

    VideoIngestClient(std::string rtpgw_grpc_addr, std::string session_id, std::string stream_id,
                       std::string leg, std::string codec, uint32_t clock_rate,
                       std::string effect_type, int decode_width, int decode_height,
                       int target_width, int target_height, int target_fps,
                       std::string mf_callback_url, FrameCallback on_frame);
    ~VideoIngestClient();

    // Starts the background reader thread (drains both IngestEvent acks
    // and DecodedFrame pushes, dispatching the latter to on_frame). Sends
    // the RtpOpen message immediately.
    void start();

    // Non-blocking: enqueues the packet onto the gRPC stream. Safe to call
    // from MF's main receive loop; never waits on RTPGW.
    void send_packet(const RTPPacket& packet);

    void close();

    long packets_sent() const { return packets_sent_.load(); }
    long send_errors() const { return send_errors_.load(); }
    long frames_received() const { return frames_received_.load(); }

private:
    std::string rtpgw_grpc_addr_;
    std::string session_id_, stream_id_, leg_, codec_, effect_type_, mf_callback_url_;
    uint32_t clock_rate_;
    int decode_width_, decode_height_;
    int target_width_, target_height_, target_fps_;
    FrameCallback on_frame_;

    std::atomic<long> packets_sent_{0};
    std::atomic<long> send_errors_{0};
    std::atomic<long> frames_received_{0};
    std::atomic<bool> running_{false};
    std::thread reader_thread_;

    // Opaque pointers to keep grpc++/generated headers out of mf.hpp's
    // include graph.
    void* channel_ = nullptr;
    void* stub_ = nullptr;
    void* context_ = nullptr;
    void* stream_ = nullptr;
};

} // namespace mf

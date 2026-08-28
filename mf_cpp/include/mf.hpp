#pragma once
#include <atomic>
#include <memory>
#include <mutex>
#include <thread>

#include <opencv2/opencv.hpp>

#include "args.hpp"
#include "mask_store.hpp"
#include "mask_callback_server.hpp"
#include "rtp.hpp"
#ifdef MF_HAVE_GRPC_INGEST
#include "video_ingest_client.hpp"
#endif

namespace mf {

// MF: v3.2 (rtpgw_design_v3.md section 37).
//
// MF's role changed from "decode RTP locally + composite" to "forward RTP
// + composite". Decode moved entirely into RTPGW (embedded C++ decoder via
// cgo -- the exact code that used to live in this directory as
// ffmpeg_decoder.cpp/access_unit_assembler.hpp/h264_depacketizer.hpp now
// lives in services/logic/internal/video/cdecoder/ instead, copied
// verbatim). MF:
//   1. still receives RTP over UDP (unchanged) and still forwards every
//      packet to RTPGW over the gRPC video ingest stream (unchanged from
//      v3.1);
//   2. no longer decodes those packets itself -- instead, it receives
//      already-decoded raw frames FROM RTPGW on the SAME gRPC stream
//      (VideoIngestClient's on_frame callback), and composites+writes
//      those;
//   3. still receives mask results from the AI Engine directly over HTTP
//      (MaskCallbackServer, unchanged from v3.1).
//
// This removes the "two independent decoders of the same stream" design
// from v3.1 (MF decoding for itself + RTPGW decoding separately for AI) in
// favour of "decode once in RTPGW, use twice" -- see rtpgw_design_v3.md
// section 37 for the full rationale and trade-offs.
class MF {
public:
    explicit MF(const Args& args);
    void run();

private:
    cv::Mat load_background();
    void forward_packet_to_rtpgw(const RTPPacket& packet);
    // Invoked (on VideoIngestClient's reader thread) whenever RTPGW streams
    // back a decoded frame. Composites it against the latest mask and
    // writes it to the output file. bgr must be exactly
    // args_.width * args_.height * 3 bytes.
    void on_decoded_frame(const uint8_t* bgr, int width, int height, uint32_t rtp_ts);
    cv::Mat compose(const cv::Mat& frame);

    Args args_;
    std::atomic<bool> running_{true};

    MaskStore mask_store_;
    MaskCallbackServer mask_callback_server_;

#ifdef MF_HAVE_GRPC_INGEST
    std::unique_ptr<VideoIngestClient> video_ingest_client_;
#endif

    std::mutex writer_mutex_; // on_decoded_frame runs on a different thread than run()'s socket loop
    long packet_count_ = 0;
    long frame_count_ = 0;

    cv::Mat background_;
    cv::VideoWriter writer_;
};

} // namespace mf

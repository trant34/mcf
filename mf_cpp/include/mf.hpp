#pragma once
#include <atomic>
#include <thread>

#include <opencv2/opencv.hpp>

#include "args.hpp"
#include "access_unit_assembler.hpp"
#include "h264_depacketizer.hpp"
#include "ffmpeg_decoder.hpp"
#include "latest_queue.hpp"
#include "mask_store.hpp"
#include "mcf_client.hpp"

namespace mf {

struct PendingFrame {
    int frame_id;
    uint32_t rtp_ts;
    cv::Mat frame;
};

// MF: owns the full MF pipeline
// (RTP ingest -> depacketize -> decode -> AI inference -> composite -> MP4).
class MF {
public:
    explicit MF(const Args& args);
    void run();

private:
    cv::Mat load_background();
    void inference_worker();
    cv::Mat compose(const cv::Mat& frame);

    Args args_;
    std::atomic<bool> running_{true};

    AccessUnitAssembler assembler_;
    H264Depacketizer depacketizer_;
    FFmpegDecoder decoder_;
    MaskStore mask_store_;
    LatestQueue<PendingFrame> inference_queue_{1};
    MCFInferenceClient client_;

    long packet_count_ = 0;
    long au_count_ = 0;
    long frame_count_ = 0;
    long inference_count_ = 0;
    long inference_errors_ = 0;
    std::chrono::steady_clock::time_point last_inference_time_{};

    cv::Mat background_;
    cv::VideoWriter writer_;
};

} // namespace mf

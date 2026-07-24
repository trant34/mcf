#pragma once
#include <cstdint>
#include <string>
#include <vector>

#include <opencv2/opencv.hpp>

namespace mf {

struct InferenceResult {
    std::string status;
    std::string error_message;
    int frame_id = 0;
    uint32_t rtp_timestamp = 0;
    int mask_width = 0;
    int mask_height = 0;
    std::vector<int64_t> rle_counts;
    double latency_ms = 0.0;
    int rle_runs = 0;
    double round_trip_ms = 0.0;
};

// MCFInferenceClient: JPEG-encodes a frame and POSTs it to
// the MCF HTTP gateway, returning the parsed inference JSON response.
class MCFInferenceClient {
public:
    MCFInferenceClient(std::string endpoint, std::string session_id,
                        std::string stream_id, double timeout_seconds);
    ~MCFInferenceClient();

    InferenceResult infer(int frame_id, uint32_t rtp_timestamp, const cv::Mat& frame,
                           const std::string& effect, int jpeg_quality);

private:
    std::string endpoint_;
    std::string session_id_;
    std::string stream_id_;
    double timeout_seconds_;
};

} // namespace mf

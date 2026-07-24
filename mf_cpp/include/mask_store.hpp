#pragma once
#include <chrono>
#include <mutex>
#include <optional>

#include <opencv2/opencv.hpp>

namespace mf {

// MaskStore: thread-safe holder for the most recent
// inference mask, guarded by a mutex.
class MaskStore {
public:
    void update(const cv::Mat& mask, int frame_id, uint32_t rtp_timestamp) {
        std::lock_guard<std::mutex> lock(mutex_);
        mask_ = mask.clone();
        frame_id_ = frame_id;
        rtp_timestamp_ = rtp_timestamp;
        updated_at_ = std::chrono::steady_clock::now();
    }

    std::optional<cv::Mat> latest() {
        std::lock_guard<std::mutex> lock(mutex_);
        if (mask_.empty()) return std::nullopt;
        return mask_.clone();
    }

private:
    std::mutex mutex_;
    cv::Mat mask_;
    int frame_id_ = -1;
    uint32_t rtp_timestamp_ = 0;
    std::chrono::steady_clock::time_point updated_at_{};
};

} // namespace mf

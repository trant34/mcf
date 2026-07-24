#pragma once
#include <cstdint>
#include <vector>

#include <opencv2/opencv.hpp>

namespace mf {

// decode_rle(): alternating run-length counts (starting at
// value 0) decoded into a height x width CV_8U mask.
inline cv::Mat decode_rle(const std::vector<int64_t>& counts, int width, int height) {
    const size_t total = static_cast<size_t>(width) * static_cast<size_t>(height);
    cv::Mat output(1, static_cast<int>(total), CV_8U);
    uint8_t* out = output.ptr<uint8_t>(0);

    size_t pos = 0;
    uint8_t value = 0;
    for (int64_t count : counts) {
        size_t end = std::min(total, pos + static_cast<size_t>(std::max<int64_t>(0, count)));
        std::fill(out + pos, out + end, value);
        pos = end;
        value = 1 - value;
        if (pos >= total) break;
    }
    if (pos < total) {
        std::fill(out + pos, out + total, static_cast<uint8_t>(0));
    }
    return output.reshape(1, height); // height x width
}

} // namespace mf

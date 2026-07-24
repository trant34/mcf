#pragma once
#include <cstdint>
#include <optional>
#include <string>

namespace mf {

// config cho mf
struct Args {
    // RTP input
    std::string listen_host = "127.0.0.1";
    int listen_port = 5006;
    std::optional<int> payload_type = 114;
    std::optional<uint32_t> ssrc = 0x5cccb090;

    // video config
    int width = 240;
    int height = 320;
    double fps = 15.0;

    // inference config
    double infer_fps = 5.0;
    int infer_width = 256;
    int infer_height = 144;
    int jpeg_quality = 80;
    
    std::string mcf_url = "http://127.0.0.1:8080/v1/video/infer";
    double http_timeout = 1.5;
    std::string session_id = "CALL-VIDEO-TEST";
    std::string stream_id = "video-0";

    // background config
    std::string effect = "bg_replace"; // bg_replace | bg_blur | bg_remove
    std::string background = "../../services/ai_engine/model_checkpoints/bg_image.jpg";
    std::string output = "output_test_cpp.mp4";
    double mask_blur_sigma = 2.0;
    double background_blur_sigma = 12.0;

    // ffmpeg config
    double end_idle_seconds = 2.0;
    std::string ffmpeg = "ffmpeg";
    bool debug_ffmpeg = false;
};

Args parse_args(int argc, char** argv);

} // namespace mf

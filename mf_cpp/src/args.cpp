#include "args.hpp"

#include <cstdlib>
#include <cstring>
#include <iostream>
#include <stdexcept>

namespace mf {

namespace {
int64_t parse_int_auto(const std::string& value) {
    if (value.size() > 1 && (value.rfind("0x", 0) == 0 || value.rfind("0X", 0) == 0)) {
        return std::stoll(value, nullptr, 16);
    }
    return std::stoll(value, nullptr, 10);
}

std::string require_value(int argc, char** argv, int& i, const std::string& flag) {
    if (i + 1 >= argc) {
        throw std::runtime_error("missing value for " + flag);
    }
    return argv[++i];
}
} // namespace

Args parse_args(int argc, char** argv) {
    Args args;
    for (int i = 1; i < argc; ++i) {
        std::string flag = argv[i];
        if (flag == "--listen-host") {
            args.listen_host = require_value(argc, argv, i, flag);
        } else if (flag == "--listen-port") {
            args.listen_port = std::stoi(require_value(argc, argv, i, flag));
        } else if (flag == "--payload-type") {
            args.payload_type = std::stoi(require_value(argc, argv, i, flag));
        } else if (flag == "--ssrc") {
            std::string v = require_value(argc, argv, i, flag);
            args.ssrc = static_cast<uint32_t>(parse_int_auto(v));
        } else if (flag == "--width") {
            args.width = std::stoi(require_value(argc, argv, i, flag));
        } else if (flag == "--height") {
            args.height = std::stoi(require_value(argc, argv, i, flag));
        } else if (flag == "--fps") {
            args.fps = std::stod(require_value(argc, argv, i, flag));
        } else if (flag == "--infer-fps") {
            args.infer_fps = std::stod(require_value(argc, argv, i, flag));
        } else if (flag == "--infer-width") {
            args.infer_width = std::stoi(require_value(argc, argv, i, flag));
        } else if (flag == "--infer-height") {
            args.infer_height = std::stoi(require_value(argc, argv, i, flag));
        } else if (flag == "--jpeg-quality") {
            args.jpeg_quality = std::stoi(require_value(argc, argv, i, flag));
        } else if (flag == "--mcf-url") {
            args.mcf_url = require_value(argc, argv, i, flag);
        } else if (flag == "--http-timeout") {
            args.http_timeout = std::stod(require_value(argc, argv, i, flag));
        } else if (flag == "--session-id") {
            args.session_id = require_value(argc, argv, i, flag);
        } else if (flag == "--stream-id") {
            args.stream_id = require_value(argc, argv, i, flag);
        } else if (flag == "--effect") {
            args.effect = require_value(argc, argv, i, flag);
            if (args.effect != "bg_replace" && args.effect != "bg_blur" && args.effect != "bg_remove") {
                throw std::runtime_error("--effect must be one of bg_replace|bg_blur|bg_remove");
            }
        } else if (flag == "--background") {
            args.background = require_value(argc, argv, i, flag);
        } else if (flag == "--output") {
            args.output = require_value(argc, argv, i, flag);
        } else if (flag == "--mask-blur-sigma") {
            args.mask_blur_sigma = std::stod(require_value(argc, argv, i, flag));
        } else if (flag == "--background-blur-sigma") {
            args.background_blur_sigma = std::stod(require_value(argc, argv, i, flag));
        } else if (flag == "--end-idle-seconds") {
            args.end_idle_seconds = std::stod(require_value(argc, argv, i, flag));
        } else if (flag == "--ffmpeg") {
            args.ffmpeg = require_value(argc, argv, i, flag);
        } else if (flag == "--debug-ffmpeg") {
            args.debug_ffmpeg = true;
        } else if (flag == "-h" || flag == "--help") {
            std::cout << "Usage: mf_cpp [options]\n"
                      << "See mf_v1.py argparse options for the full flag list; "
                         "flags are identical (e.g. --listen-host, --listen-port, "
                         "--width, --height, --fps, --infer-fps, --mcf-url, "
                         "--effect, --background, --output, ...).\n";
            std::exit(0);
        } else {
            throw std::runtime_error("unknown argument: " + flag);
        }
    }
    return args;
}

} // namespace mf

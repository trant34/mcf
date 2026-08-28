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
        } else if (flag == "--rtpgw-grpc-addr") {
            args.rtpgw_grpc_addr = require_value(argc, argv, i, flag);
        } else if (flag == "--leg") {
            args.leg = require_value(argc, argv, i, flag);
        } else if (flag == "--codec") {
            args.codec = require_value(argc, argv, i, flag);
        } else if (flag == "--clock-rate") {
            args.clock_rate = static_cast<uint32_t>(std::stoul(require_value(argc, argv, i, flag)));
        } else if (flag == "--mask-callback-listen-addr") {
            args.mask_callback_listen_addr = require_value(argc, argv, i, flag);
        } else if (flag == "--mask-callback-listen-port") {
            args.mask_callback_listen_port = std::stoi(require_value(argc, argv, i, flag));
        } else if (flag == "--mask-callback-path") {
            args.mask_callback_path = require_value(argc, argv, i, flag);
        } else if (flag == "--mf-callback-url") {
            args.mf_callback_url = require_value(argc, argv, i, flag);
        } else if (flag == "--shutdown-grace-ms") {
            args.shutdown_grace_ms = std::stoi(require_value(argc, argv, i, flag));
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
        } else if (flag == "-h" || flag == "--help") {
            std::cout << "Usage: mf_cpp [options]\n"
                      << "\n"
                      << "v3.2 (rtpgw_design_v3.md section 37): MF forwards RTP to RTPGW and\n"
                      << "composites frames RTPGW decodes and streams back -- MF does not decode\n"
                      << "H264 itself anymore, so there is no local-decode / --legacy-http-infer\n"
                      << "fallback (removed from v3.1).\n"
                      << "\n"
                      << "RTP input:      --listen-host --listen-port --payload-type --ssrc\n"
                      << "Video/output:   --width --height --fps --effect --background --output\n"
                      << "                --mask-blur-sigma --background-blur-sigma --end-idle-seconds\n"
                      << "AI hint:        --infer-fps --infer-width --infer-height (sent to RTPGW as a\n"
                      << "                hint in RtpOpen; RTPGW's own env config wins if it disagrees)\n"
                      << "RTPGW link:     --rtpgw-grpc-addr HOST:PORT (default 127.0.0.1:50060)\n"
                      << "                --leg --codec --clock-rate --session-id --stream-id\n"
                      << "                --shutdown-grace-ms (default 3000)\n"
                      << "Mask callback:  --mask-callback-listen-addr --mask-callback-listen-port\n"
                      << "                --mask-callback-path --mf-callback-url\n";
            std::exit(0);
        } else {
            throw std::runtime_error("unknown argument: " + flag);
        }
    }
    return args;
}

} // namespace mf

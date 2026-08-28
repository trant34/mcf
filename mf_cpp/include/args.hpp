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

    // video config -- v3.2: this is now the resolution RTPGW is TOLD to
    // decode+return raw frames at (via RtpOpen.decode_width/decode_height),
    // not a resolution MF decodes to itself anymore. Still needed locally
    // to size the VideoWriter and the background image.
    int width = 240;
    int height = 320;
    double fps = 15.0;

    // AI inference hint -- v3.2: MF no longer resizes/JPEG-encodes frames
    // itself (RTPGW owns that leg entirely now). These are only sent to
    // RTPGW as a hint in RtpOpen (target_width/target_height/target_fps);
    // RTPGW's own VIDEO_INFER_WIDTH/HEIGHT/FPS config is authoritative if
    // it disagrees.
    double infer_fps = 5.0;
    int infer_width = 256;
    int infer_height = 144;

    std::string session_id = "CALL-VIDEO-TEST";
    std::string stream_id = "video-0";

    // v3.2 (rtpgw_design_v3.md section 37): MF -> RTPGW bidirectional gRPC
    // video ingest is now the ONLY transport -- MF has no local decoder to
    // fall back to anymore (removed the v3.1 --legacy-http-infer path).
    std::string rtpgw_grpc_addr = "127.0.0.1:50060";
    std::string leg = "taccess";
    std::string codec = "H264";
    uint32_t clock_rate = 90000;

    // HTTP server MF exposes so the AI Engine can push mask RLE results
    // directly (LIVE_STREAM async result_callback -> MF), instead of MF
    // waiting on a synchronous reply.
    std::string mask_callback_listen_addr = "0.0.0.0";
    int mask_callback_listen_port = 8090;
    std::string mask_callback_path = "/v1/video/mask";
    // Advertised to RTPGW (which forwards it to the AI Engine) as the
    // address the AI Engine should reach MF's callback server on -- this
    // should be MF's routable IP/hostname, not necessarily
    // mask_callback_listen_addr (which may be "0.0.0.0").
    std::string mf_callback_url = "http://127.0.0.1:8090/v1/video/mask";
    // How long to keep MF alive after input stops, to let in-flight
    // decoded frames (from RTPGW) and AI Engine LIVE_STREAM mask results
    // land before shutdown (rtpgw_design_v3.md section 35.6). Too short
    // and you'll see "Connection refused" on the AI Engine side for the
    // last few frames, plus mask_cb_received/frames_from_rtpgw
    // undercounting real activity.
    int shutdown_grace_ms = 3000;

    // background config
    std::string effect = "bg_replace"; // bg_replace | bg_blur | bg_remove
    std::string background = "../../services/ai_engine/model_checkpoints/bg_image.jpg";
    std::string output = "output_test_cpp.mp4";
    double mask_blur_sigma = 2.0;
    double background_blur_sigma = 12.0;

    double end_idle_seconds = 2.0;
};

Args parse_args(int argc, char** argv);

} // namespace mf

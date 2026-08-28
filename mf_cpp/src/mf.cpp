#include "mf.hpp"

#include <chrono>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <thread>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include "rle.hpp"

namespace mf {

namespace {
void log(const std::string& message) {
    std::cout << message << std::endl;
}
} // namespace

MF::MF(const Args& args)
    : args_(args),
      mask_callback_server_(args.mask_callback_listen_addr, args.mask_callback_listen_port,
                             args.mask_callback_path, mask_store_, args.session_id) {
    background_ = load_background();

    int fourcc = cv::VideoWriter::fourcc('m', 'p', '4', 'v');
    writer_.open(args_.output, fourcc, args_.fps, cv::Size(args_.width, args_.height));
    if (!writer_.isOpened()) {
        throw std::runtime_error("cannot open output writer: " + args_.output);
    }

#ifdef MF_HAVE_GRPC_INGEST
    video_ingest_client_ = std::make_unique<VideoIngestClient>(
        args_.rtpgw_grpc_addr, args_.session_id, args_.stream_id, args_.leg, args_.codec,
        args_.clock_rate, args_.effect,
        args_.width, args_.height,               // decode_width/height -- MUST match this MF instance
        args_.infer_width, args_.infer_height,    // AI inference resize target (hint only, RTPGW owns the resize)
        static_cast<int>(args_.infer_fps), args_.mf_callback_url,
        [this](const uint8_t* bgr, int w, int h, uint32_t rtp_ts) { on_decoded_frame(bgr, w, h, rtp_ts); });
#else
    throw std::runtime_error(
        "mf_cpp v3.2 requires the MF -> RTPGW gRPC video ingest path -- built without gRPC "
        "support (generated stubs not found at configure time). Run scripts/generate_proto.sh "
        "(install grpc_cpp_plugin first) and re-run cmake. There is no local-decode fallback "
        "anymore as of v3.2 (rtpgw_design_v3.md section 37) -- MF cannot composite without "
        "frames from RTPGW.");
#endif
}

cv::Mat MF::load_background() {
    if (!args_.background.empty()) {
        cv::Mat image = cv::imread(args_.background);
        if (image.empty()) {
            throw std::runtime_error("background image not found: " + args_.background);
        }
        cv::Mat resized;
        cv::resize(image, resized, cv::Size(args_.width, args_.height));
        return resized;
    }
    return cv::Mat::zeros(args_.height, args_.width, CV_8UC3);
}

void MF::forward_packet_to_rtpgw(const RTPPacket& packet) {
#ifdef MF_HAVE_GRPC_INGEST
    if (video_ingest_client_) {
        video_ingest_client_->send_packet(packet);
    }
#else
    (void)packet;
#endif
}

// Runs on VideoIngestClient's reader thread (see video_ingest_client.cpp),
// NOT on run()'s socket-receive thread -- writer_ access is guarded by
// writer_mutex_ accordingly (cv::VideoWriter is not documented as
// thread-safe, and this is now the ONLY place that writes to it, so the
// mutex mainly protects against a stray second caller ever being added
// later, cheap insurance for a single lock/frame).
void MF::on_decoded_frame(const uint8_t* bgr, int width, int height, uint32_t rtp_ts) {
    if (width != args_.width || height != args_.height) {
        log("[MF][WARN] decoded frame size " + std::to_string(width) + "x" + std::to_string(height) +
            " != configured " + std::to_string(args_.width) + "x" + std::to_string(args_.height) +
            " -- dropping frame. This means RTPGW's decode_width/decode_height didn't match what "
            "this MF instance advertised in RtpOpen; check RTPGW logs.");
        return;
    }

    cv::Mat frame(height, width, CV_8UC3);
    std::memcpy(frame.data, bgr, static_cast<size_t>(width) * height * 3);

    std::lock_guard<std::mutex> lock(writer_mutex_);
    ++frame_count_;
    writer_.write(compose(frame));
    if (frame_count_ <= 5 || frame_count_ % 100 == 0) {
        log("[MF] packets=" + std::to_string(packet_count_) +
            " frames=" + std::to_string(frame_count_) +
            " (rtp_ts=" + std::to_string(rtp_ts) + ")");
    }
}

cv::Mat MF::compose(const cv::Mat& frame) {
    auto mask_opt = mask_store_.latest();
    if (!mask_opt.has_value()) {
        return frame;
    }

    cv::Mat mask_f;
    mask_opt->convertTo(mask_f, CV_32F);
    cv::Mat alpha;
    cv::resize(mask_f, alpha, cv::Size(args_.width, args_.height), 0, 0, cv::INTER_LINEAR);
    if (args_.mask_blur_sigma > 0) {
        cv::GaussianBlur(alpha, alpha, cv::Size(0, 0), args_.mask_blur_sigma);
    }
    cv::threshold(alpha, alpha, 1.0, 1.0, cv::THRESH_TRUNC); // clip upper bound
    cv::max(alpha, 0.0, alpha);                              // clip lower bound

    cv::Mat background;
    if (args_.effect == "bg_blur") {
        cv::GaussianBlur(frame, background, cv::Size(0, 0), args_.background_blur_sigma);
    } else if (args_.effect == "bg_remove") {
        background = cv::Mat::zeros(frame.size(), frame.type());
    } else {
        background = background_;
    }

    cv::Mat frame_f, background_f, alpha3;
    frame.convertTo(frame_f, CV_32FC3);
    background.convertTo(background_f, CV_32FC3);
    cv::cvtColor(alpha, alpha3, cv::COLOR_GRAY2BGR);

    cv::Mat output_f = alpha3.mul(frame_f) + (cv::Scalar(1.0, 1.0, 1.0) - alpha3).mul(background_f);
    cv::Mat output;
    output_f.convertTo(output, CV_8UC3);
    return output;
}

// Run the main loop: receive RTP packets over UDP, forward every one of
// them to RTPGW. Decoding, compositing, and writing all now happen
// asynchronously in on_decoded_frame() (called from VideoIngestClient's
// reader thread) as RTPGW streams decoded frames back -- this loop's only
// job is the UDP receive + forward + idle-timeout bookkeeping.
void MF::run() {
    mask_callback_server_.start();

#ifdef MF_HAVE_GRPC_INGEST
    if (video_ingest_client_) {
        video_ingest_client_->start();
    }
#endif

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        throw std::runtime_error("failed to create UDP socket");
    }
    int rcvbuf = 4 * 1024 * 1024;
    setsockopt(sock, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));

    struct timeval tv;
    tv.tv_sec = 0;
    tv.tv_usec = 50000; // 0.05s
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    struct sockaddr_in addr {};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(args_.listen_port));
    if (inet_pton(AF_INET, args_.listen_host.c_str(), &addr.sin_addr) != 1) {
        ::close(sock);
        throw std::runtime_error("invalid --listen-host: " + args_.listen_host);
    }
    if (bind(sock, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) != 0) {
        ::close(sock);
        throw std::runtime_error("failed to bind udp://" + args_.listen_host + ":" + std::to_string(args_.listen_port));
    }

    log("[MF] listening udp://" + args_.listen_host + ":" + std::to_string(args_.listen_port));

    auto last_packet = std::chrono::steady_clock::now();
    std::vector<uint8_t> buf(65535);

    auto shutdown = [this, sock]() {
        running_.store(false);
        ::close(sock);
#ifdef MF_HAVE_GRPC_INGEST
        if (video_ingest_client_) video_ingest_client_->close();
#endif
        // Grace window so mask pushes and trailing decoded frames already
        // in flight from RTPGW/AI Engine (both operate asynchronously,
        // independent of when MF stops sending new packets) land before
        // MF tears down. See rtpgw_design_v3.md section 35.6.
        long frames_so_far = 0;
#ifdef MF_HAVE_GRPC_INGEST
        if (video_ingest_client_) frames_so_far = video_ingest_client_->frames_received();
#endif
        log("[MF] waiting " + std::to_string(args_.shutdown_grace_ms) +
            "ms for in-flight frames/mask results before shutdown "
            "(frames_received=" + std::to_string(frames_so_far) +
            " mask_cb_received=" + std::to_string(mask_callback_server_.received_count()) + " so far)");
        std::this_thread::sleep_for(std::chrono::milliseconds(args_.shutdown_grace_ms));
        mask_callback_server_.stop();
        writer_.release();
    };

    try {
        while (running_.load()) {
            ssize_t n = recvfrom(sock, buf.data(), buf.size(), 0, nullptr, nullptr);
            if (n < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    // socket timeout, fall through to idle-timeout check below
                } else if (errno == EINTR) {
                    continue;
                } else {
                    log("[MF] recvfrom error, stopping");
                    break;
                }
            } else {
                last_packet = std::chrono::steady_clock::now();
                auto packet = parse_rtp(buf.data(), static_cast<size_t>(n));
                if (packet.has_value()) {
                    bool pt_ok = !args_.payload_type.has_value() || packet->pt == *args_.payload_type;
                    bool ssrc_ok = !args_.ssrc.has_value() || packet->ssrc == *args_.ssrc;
                    if (pt_ok && ssrc_ok) {
                        ++packet_count_;
                        forward_packet_to_rtpgw(*packet);
                    }
                }
            }

            if (packet_count_ > 0) {
                double idle = std::chrono::duration<double>(std::chrono::steady_clock::now() - last_packet).count();
                if (idle > args_.end_idle_seconds) {
                    log("[MF] input idle timeout reached");
                    break;
                }
            }
        }
    } catch (...) {
        shutdown();
        throw;
    }

    shutdown();

    long frames_received = 0;
#ifdef MF_HAVE_GRPC_INGEST
    if (video_ingest_client_) frames_received = video_ingest_client_->frames_received();
#endif

    log("[MF][DONE] packets=" + std::to_string(packet_count_) +
        " frames_from_rtpgw=" + std::to_string(frames_received) +
        " frames_written=" + std::to_string(frame_count_) +
        " mask_cb_received=" + std::to_string(mask_callback_server_.received_count()) +
        " mask_cb_errors=" + std::to_string(mask_callback_server_.error_count()) +
        " output=" + args_.output);

    if (mask_callback_server_.received_count() == 0) {
        log("[MF][WARN] mask_cb_received=0 -- no mask was ever applied, output is "
            "effectively the raw composite (background NOT replaced). Check that "
            "RTPGW (video_ingest gRPC) and the AI Engine (ProcessVideoStream) logs "
            "show frames actually flowing, and that --mf-callback-url is reachable "
            "from the AI Engine's host.");
    }
    if (frames_received == 0) {
        log("[MF][WARN] frames_from_rtpgw=0 -- RTPGW never sent back a single decoded frame. "
            "Output will be empty/black. Check RTPGW logs for decode errors, and verify "
            "--rtpgw-grpc-addr is reachable and --width/--height match RTPGW's decode config.");
    }
}

} // namespace mf

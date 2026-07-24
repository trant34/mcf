#include "mf.hpp"

#include <chrono>
#include <iostream>
#include <stdexcept>
#include <thread>

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include "rle.hpp"
#include "rtp.hpp"

namespace mf {

namespace {
void log(const std::string& message) {
    std::cout << message << std::endl;
}
} // namespace

MF::MF(const Args& args)
    : args_(args),
      decoder_(args.width, args.height, args.ffmpeg, args.debug_ffmpeg),
      client_(args.mcf_url, args.session_id, args.stream_id, args.http_timeout) {
    background_ = load_background();

    int fourcc = cv::VideoWriter::fourcc('m', 'p', '4', 'v');
    writer_.open(args_.output, fourcc, args_.fps, cv::Size(args_.width, args_.height));
    if (!writer_.isOpened()) {
        throw std::runtime_error("cannot open output writer: " + args_.output);
    }
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

void MF::inference_worker() {
    while (running_.load()) {
        auto item = inference_queue_.get(0.2);
        if (!item.has_value()) continue;
        int frame_id = item->frame_id;
        uint32_t rtp_ts = item->rtp_ts;
        try {
            cv::Mat small;
            cv::resize(item->frame, small, cv::Size(args_.infer_width, args_.infer_height));
            InferenceResult result = client_.infer(frame_id, rtp_ts, small, args_.effect, args_.jpeg_quality);
            if (result.status != "ok") {
                throw std::runtime_error(result.error_message.empty() ? "AI returned error" : result.error_message);
            }
            cv::Mat mask = decode_rle(result.rle_counts, result.mask_width, result.mask_height);
            mask_store_.update(mask, result.frame_id, result.rtp_timestamp);
            ++inference_count_;
            if (inference_count_ <= 5 || inference_count_ % 20 == 0) {
                log("[MF][AI] n=" + std::to_string(inference_count_) +
                    " frame=" + std::to_string(frame_id) +
                    " ai_ms=" + std::to_string(result.latency_ms) +
                    " rtt_ms=" + std::to_string(result.round_trip_ms) +
                    " runs=" + std::to_string(result.rle_runs));
            }
        } catch (const std::exception& exc) {
            ++inference_errors_;
            log("[MF][AI] error frame=" + std::to_string(frame_id) + ": " + exc.what());
        }
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

// Run the main loop: receive RTP packets, depacketize, decode, and write output.
void MF::run() {
    std::thread worker(&MF::inference_worker, this);

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        throw std::runtime_error("failed to create UDP socket");
    }
    int rcvbuf = 4 * 1024 * 1024;
    setsockopt(sock, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));

    struct timeval tv;
    tv.tv_sec = 0;
    tv.tv_usec = 50000; // 0.05s, receiver.settimeout(0.05)
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

    try {
        while (running_.load()) {
            ssize_t n = recvfrom(sock, buf.data(), buf.size(), 0, nullptr, nullptr);
            if (n < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) {
                    // socket timeout, fall through to frame draining below
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
                        for (auto& [rtp_ts, packets] : assembler_.push(*packet)) {
                            auto annexb = depacketizer_.depacketize(packets);
                            if (!annexb.empty()) {
                                ++au_count_;
                                decoder_.feed(annexb, rtp_ts);
                            }
                        }
                    }
                }
            }

            while (true) {
                auto decoded = decoder_.frames.get_nowait();
                if (!decoded.has_value()) break;
                ++frame_count_;
                auto now = std::chrono::steady_clock::now();
                double elapsed = std::chrono::duration<double>(now - last_inference_time_).count();
                if (elapsed >= 1.0 / args_.infer_fps) {
                    last_inference_time_ = now;
                    inference_queue_.put_latest(PendingFrame{static_cast<int>(frame_count_), decoded->rtp_ts, decoded->frame.clone()});
                }
                writer_.write(compose(decoded->frame));
                if (frame_count_ <= 5 || frame_count_ % 100 == 0) {
                    log("[MF] packets=" + std::to_string(packet_count_) +
                        " aus=" + std::to_string(au_count_) +
                        " frames=" + std::to_string(frame_count_));
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
        running_.store(false);
        ::close(sock);
        decoder_.close();
        if (worker.joinable()) worker.join();
        writer_.release();
        throw;
    }

    running_.store(false);
    ::close(sock);
    decoder_.close();
    if (worker.joinable()) worker.join();
    // drain any delayed decoder frames
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    writer_.release();

    log("[MF][DONE] packets=" + std::to_string(packet_count_) +
        " aus=" + std::to_string(au_count_) +
        " frames=" + std::to_string(frame_count_) +
        " inference=" + std::to_string(inference_count_) +
        " errors=" + std::to_string(inference_errors_) +
        " output=" + args_.output);
}

} // namespace mf

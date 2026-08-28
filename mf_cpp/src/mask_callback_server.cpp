#include "mask_callback_server.hpp"

#include <chrono>
#include <iostream>

#include <nlohmann/json.hpp>

// Vendored single-header HTTP server (third_party/httplib.h), same pattern
// already used for nlohmann::json (third_party/nlohmann/json.hpp) when the
// system package isn't found -- see CMakeLists.txt.
#include "httplib.h"

#include "rle.hpp"

namespace mf {

namespace {
void log(const std::string& message) {
    std::cout << message << std::endl;
}

// LATENCY_TRACE T7 in wall-clock epoch ms, same clock convention as
// Go's time.Now().UnixMilli() and Python's int(time.time()*1000) --
// see rtpgw_design_v3.md section 36. Only meaningful for cross-process
// deltas when all processes share a clock (same host / NTP-synced).
int64_t now_epoch_ms() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}
} // namespace

MaskCallbackServer::MaskCallbackServer(std::string listen_addr, int listen_port, std::string path,
                                        MaskStore& mask_store, std::string expected_session_id)
    : listen_addr_(std::move(listen_addr)),
      listen_port_(listen_port),
      path_(std::move(path)),
      mask_store_(mask_store),
      expected_session_id_(std::move(expected_session_id)) {
    server_impl_ = new httplib::Server();
}

MaskCallbackServer::~MaskCallbackServer() {
    stop();
    delete static_cast<httplib::Server*>(server_impl_);
}

void MaskCallbackServer::start() {
    auto* server = static_cast<httplib::Server*>(server_impl_);

    server->Post(path_, [this](const httplib::Request& req, httplib::Response& res) {
        int64_t t7_ms = now_epoch_ms(); // LATENCY_TRACE T7: HTTP POST received from AI Engine
        try {
            nlohmann::json body = nlohmann::json::parse(req.body);

            std::string session_id = body.value("session_id", "");
            if (!expected_session_id_.empty() && !session_id.empty() && session_id != expected_session_id_) {
                // Not fatal -- MF may be running a shared callback listener
                // for more than one session in the future; just log.
                log("[MF][mask_cb] warn: session_id mismatch got=" + session_id +
                    " expected=" + expected_session_id_);
            }

            std::string status = body.value("status", "ok");
            ++received_count_;
            if (status != "ok") {
                ++error_count_;
                log("[MF][mask_cb] error status from AI engine: " + body.value("error_message", "unknown"));
                res.status = 200; // acked, nothing more to do
                res.set_content("{\"ok\":true}", "application/json");
                return;
            }

            int frame_id = body.value("frame_id", 0);
            uint32_t rtp_timestamp = body.value("rtp_timestamp", 0u);
            int mask_width = body.value("mask_width", 0);
            int mask_height = body.value("mask_height", 0);
            std::vector<int64_t> rle_counts;
            if (body.contains("rle_counts")) {
                rle_counts = body.at("rle_counts").get<std::vector<int64_t>>();
            }

            if (mask_width <= 0 || mask_height <= 0 || rle_counts.empty()) {
                ++error_count_;
                log("[MF][mask_cb] error: empty/invalid mask payload frame_id=" + std::to_string(frame_id));
                res.status = 400;
                res.set_content("{\"ok\":false,\"error\":\"invalid mask payload\"}", "application/json");
                return;
            }

            cv::Mat mask = decode_rle(rle_counts, mask_width, mask_height);
            mask_store_.update(mask, frame_id, rtp_timestamp);

            // LATENCY_TRACE T7 + end-to-end breakdown for this frame. AI
            // Engine forwards t_ai_submit_ms/t_ai_result_ms/t_ai_push_ms as
            // wall-clock epoch ms (see mf_callback_client.py), so we can
            // print the AI-side and network legs in one line without
            // joining separate log files -- only the RTPGW-side legs
            // (rtpgw_decoded / rtpgw_dispatch, logged by RTPGW itself)
            // still need joining by frame_id if you want the true
            // end-to-end total. See rtpgw_design_v3.md section 36.
            std::string breakdown;
            if (body.contains("t_ai_submit_ms") && !body["t_ai_submit_ms"].is_null()) {
                int64_t t_submit = body.value("t_ai_submit_ms", (int64_t)0);
                int64_t t_result = body.value("t_ai_result_ms", (int64_t)0);
                double inference_ms = (t_result > 0 && t_submit > 0) ? static_cast<double>(t_result - t_submit) : -1;
                double network_ms = t_submit > 0 ? static_cast<double>(t7_ms - t_result) : -1;
                breakdown = " ai_inference_ms=" + std::to_string(inference_ms) +
                            " ai_to_mf_network_ms=" + std::to_string(network_ms);
            }
            log("[MF][mask_cb] LATENCY_TRACE stage=mf_received frame_id=" + std::to_string(frame_id) +
                " rtp_ts=" + std::to_string(rtp_timestamp) + " ts_ms=" + std::to_string(t7_ms) +
                breakdown);

            if (received_count_ <= 5 || received_count_ % 20 == 0) {
                log("[MF][mask_cb] n=" + std::to_string(received_count_.load()) +
                    " frame=" + std::to_string(frame_id) +
                    " latency_ms=" + std::to_string(body.value("latency_ms", 0.0)));
            }

            res.status = 200;
            res.set_content("{\"ok\":true}", "application/json");
        } catch (const std::exception& exc) {
            ++error_count_;
            log(std::string("[MF][mask_cb] failed to process callback: ") + exc.what());
            res.status = 400;
            res.set_content("{\"ok\":false}", "application/json");
        }
    });

    server_thread_ = std::thread([this, server]() {
        log("[MF] mask callback server listening http://" + listen_addr_ + ":" +
            std::to_string(listen_port_) + path_);
        if (!server->listen(listen_addr_.c_str(), listen_port_)) {
            log("[MF][mask_cb] failed to bind http://" + listen_addr_ + ":" + std::to_string(listen_port_));
        }
    });
}

void MaskCallbackServer::stop() {
    auto* server = static_cast<httplib::Server*>(server_impl_);
    if (server != nullptr && server->is_running()) {
        server->stop();
    }
    if (server_thread_.joinable()) {
        server_thread_.join();
    }
}

} // namespace mf

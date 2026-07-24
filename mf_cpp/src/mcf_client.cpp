#include "mcf_client.hpp"

#include <chrono>
#include <stdexcept>

#include <curl/curl.h>
#include <nlohmann/json.hpp>

namespace mf {

namespace {
size_t write_callback(char* ptr, size_t size, size_t nmemb, void* userdata) {
    auto* out = static_cast<std::string*>(userdata);
    out->append(ptr, size * nmemb);
    return size * nmemb;
}
} // namespace

MCFInferenceClient::MCFInferenceClient(std::string endpoint, std::string session_id, std::string stream_id, double timeout_seconds)
    : endpoint_(std::move(endpoint)),
      session_id_(std::move(session_id)),
      stream_id_(std::move(stream_id)),
      timeout_seconds_(timeout_seconds) {
    curl_global_init(CURL_GLOBAL_DEFAULT);
}

MCFInferenceClient::~MCFInferenceClient() {
    curl_global_cleanup();
}

InferenceResult MCFInferenceClient::infer(int frame_id, uint32_t rtp_timestamp, const cv::Mat& frame,
                                           const std::string& effect, int jpeg_quality) {
    std::vector<uint8_t> encoded;
    std::vector<int> params{cv::IMWRITE_JPEG_QUALITY, jpeg_quality};
    if (!cv::imencode(".jpg", frame, encoded, params)) {
        throw std::runtime_error("JPEG encoding failed");
    }

    CURL* curl = curl_easy_init();
    if (!curl) {
        throw std::runtime_error("failed to init curl handle");
    }

    std::string response_body;
    struct curl_slist* headers = nullptr;
    headers = curl_slist_append(headers, "Content-Type: image/jpeg");
    headers = curl_slist_append(headers, ("X-Session-ID: " + session_id_).c_str());
    headers = curl_slist_append(headers, ("X-Stream-ID: " + stream_id_).c_str());
    headers = curl_slist_append(headers, ("X-Frame-ID: " + std::to_string(frame_id)).c_str());
    headers = curl_slist_append(headers, ("X-RTP-Timestamp: " + std::to_string(rtp_timestamp)).c_str());
    headers = curl_slist_append(headers, ("X-Original-Width: " + std::to_string(frame.cols)).c_str());
    headers = curl_slist_append(headers, ("X-Original-Height: " + std::to_string(frame.rows)).c_str());
    headers = curl_slist_append(headers, ("X-Effect-Type: " + effect).c_str());

    curl_easy_setopt(curl, CURLOPT_URL, endpoint_.c_str());
    curl_easy_setopt(curl, CURLOPT_POST, 1L);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, encoded.data());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(encoded.size()));
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, write_callback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response_body);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, static_cast<long>(timeout_seconds_ * 1000));

    auto started = std::chrono::steady_clock::now();
    CURLcode res = curl_easy_perform(curl);
    double round_trip_ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - started).count();

    long http_status = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http_status);

    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);

    if (res != CURLE_OK) {
        throw std::runtime_error(std::string("HTTP request to MCF failed: ") + curl_easy_strerror(res));
    }
    if (http_status < 200 || http_status >= 300) {
        throw std::runtime_error("MCF returned HTTP status " + std::to_string(http_status));
    }

    nlohmann::json body = nlohmann::json::parse(response_body);

    InferenceResult result;
    result.status = body.value("status", "");
    result.error_message = body.value("error_message", "");
    result.frame_id = body.value("frame_id", frame_id);
    result.rtp_timestamp = body.value("rtp_timestamp", rtp_timestamp);
    result.mask_width = body.value("mask_width", 0);
    result.mask_height = body.value("mask_height", 0);
    if (body.contains("rle_counts")) {
        result.rle_counts = body.at("rle_counts").get<std::vector<int64_t>>();
    }
    result.latency_ms = body.value("latency_ms", 0.0);
    result.rle_runs = body.value("rle_runs", 0);
    result.round_trip_ms = round_trip_ms;
    return result;
}

} // namespace mf

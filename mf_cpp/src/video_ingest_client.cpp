#include "video_ingest_client.hpp"

#include <iostream>
#include <mutex>

#include <grpcpp/grpcpp.h>

#include "video_ingest.grpc.pb.h"
#include "video_ingest.pb.h"

namespace mf {

namespace {
void log(const std::string& message) {
    std::cout << message << std::endl;
}

std::mutex g_write_mutex;
} // namespace

using mcf::video::v1::DecodedFrame;
using mcf::video::v1::IngestEvent;
using mcf::video::v1::IngestResponse;
using mcf::video::v1::RtpFrame;
using mcf::video::v1::RtpOpen;
using mcf::video::v1::RtpPacket;
using mcf::video::v1::VideoIngestService;

VideoIngestClient::VideoIngestClient(std::string rtpgw_grpc_addr, std::string session_id,
                                      std::string stream_id, std::string leg, std::string codec,
                                      uint32_t clock_rate, std::string effect_type,
                                      int decode_width, int decode_height,
                                      int target_width, int target_height, int target_fps,
                                      std::string mf_callback_url, FrameCallback on_frame)
    : rtpgw_grpc_addr_(std::move(rtpgw_grpc_addr)),
      session_id_(std::move(session_id)),
      stream_id_(std::move(stream_id)),
      leg_(std::move(leg)),
      codec_(std::move(codec)),
      effect_type_(std::move(effect_type)),
      mf_callback_url_(std::move(mf_callback_url)),
      clock_rate_(clock_rate),
      decode_width_(decode_width),
      decode_height_(decode_height),
      target_width_(target_width),
      target_height_(target_height),
      target_fps_(target_fps),
      on_frame_(std::move(on_frame)) {
    auto channel = grpc::CreateChannel(rtpgw_grpc_addr_, grpc::InsecureChannelCredentials());
    auto stub = VideoIngestService::NewStub(channel);

    channel_ = new std::shared_ptr<grpc::Channel>(channel);
    stub_ = stub.release();
}

VideoIngestClient::~VideoIngestClient() {
    close();
    delete static_cast<VideoIngestService::Stub*>(stub_);
    delete static_cast<std::shared_ptr<grpc::Channel>*>(channel_);
}

void VideoIngestClient::start() {
    auto* stub = static_cast<VideoIngestService::Stub*>(stub_);
    auto* context = new grpc::ClientContext();
    context_ = context;

    auto stream = stub->Transport(context);
    auto* stream_ptr = stream.release();
    stream_ = stream_ptr;

    RtpFrame open_frame;
    open_frame.set_session_id(session_id_);
    open_frame.set_stream_id(stream_id_);
    RtpOpen* open = open_frame.mutable_open();
    open->set_session_id(session_id_);
    open->set_stream_id(stream_id_);
    open->set_leg(leg_);
    open->set_codec(codec_);
    open->set_clock_rate(clock_rate_);
    open->set_effect_type(effect_type_);
    open->set_target_width(target_width_);
    open->set_target_height(target_height_);
    open->set_target_fps(target_fps_);
    open->set_mf_callback_url(mf_callback_url_);

    open->set_decode_width(decode_width_);
    open->set_decode_height(decode_height_);

    {
        std::lock_guard<std::mutex> lock(g_write_mutex);
        stream_ptr->Write(open_frame);
    }

    running_.store(true);
    reader_thread_ = std::thread([this, stream_ptr]() {
        IngestResponse resp;
        while (running_.load() && stream_ptr->Read(&resp)) {
            if (resp.has_frame()) {
                const DecodedFrame& f = resp.frame();
                ++frames_received_;
                if (on_frame_) {
                    const auto& data = f.frame_data();
                    on_frame_(reinterpret_cast<const uint8_t*>(data.data()), f.width(), f.height(), f.rtp_timestamp());
                }
            } else if (resp.has_event()) {
                const IngestEvent& event = resp.event();
                if (event.status() != "ack") {
                    log("[MF][video_ingest] " + event.status() + " stream_id=" + event.stream_id() +
                        " packets=" + std::to_string(event.packets_received()) +
                        " frames=" + std::to_string(event.frames_decoded()) +
                        (event.message().empty() ? "" : (" msg=" + event.message())));
                }
            }
        }
    });

    log("[MF] video ingest gRPC stream opened to " + rtpgw_grpc_addr_ +
        " (decode_width=" + std::to_string(decode_width_) + " decode_height=" + std::to_string(decode_height_) + ")");
}

void VideoIngestClient::send_packet(const RTPPacket& packet) {
    auto* stream_ptr = static_cast<grpc::ClientReaderWriter<RtpFrame, IngestResponse>*>(stream_);
    if (stream_ptr == nullptr || !running_.load()) {
        return;
    }

    RtpFrame frame;
    frame.set_session_id(session_id_);
    frame.set_stream_id(stream_id_);
    RtpPacket* pkt = frame.mutable_packet();
    pkt->set_sequence_number(packet.seq);
    pkt->set_timestamp(packet.ts);
    pkt->set_ssrc(packet.ssrc);
    pkt->set_marker(packet.marker);
    pkt->set_payload_type(packet.pt);
    pkt->set_payload(reinterpret_cast<const char*>(packet.payload.data()), packet.payload.size());

    std::lock_guard<std::mutex> lock(g_write_mutex);
    if (stream_ptr->Write(frame)) {
        ++packets_sent_;
    } else {
        ++send_errors_;
    }
}

void VideoIngestClient::close() {
    if (!running_.exchange(false)) {
        return;
    }
    auto* stream_ptr = static_cast<grpc::ClientReaderWriter<RtpFrame, IngestResponse>*>(stream_);
    if (stream_ptr != nullptr) {
        RtpFrame eos;
        eos.set_session_id(session_id_);
        eos.set_stream_id(stream_id_);
        eos.set_is_eos(true);
        {
            std::lock_guard<std::mutex> lock(g_write_mutex);
            stream_ptr->Write(eos);
            stream_ptr->WritesDone();
        }
    }
    if (reader_thread_.joinable()) {
        reader_thread_.join();
    }
    if (stream_ptr != nullptr) {
        stream_ptr->Finish();
        delete stream_ptr;
        stream_ = nullptr;
    }
    if (context_ != nullptr) {
        delete static_cast<grpc::ClientContext*>(context_);
        context_ = nullptr;
    }
}

} // namespace mf

#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

import grpc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "ai_engine" / "src"))

# from services.ai_engine.src.pb import ai_service_pb2 
# from services.ai_engine.src.pb import ai_service_pb2_grpc  
from pb import ai_service_pb2 
from pb import ai_service_pb2_grpc  


def requests(session_id: str, stream_id: str, image: bytes):
    yield ai_service_pb2.VideoRequest(
        session_id=session_id,
        stream_id=stream_id,
        config=ai_service_pb2.VideoConfig(
            effect_type="bg_replace",
            inference_width=256,
            inference_height=144,
            inference_fps=5,
            threshold=0.5,
        ),
    )
    yield ai_service_pb2.VideoRequest(
        session_id=session_id,
        stream_id=stream_id,
        frame=ai_service_pb2.VideoFrame(
            frame_id=1,
            rtp_timestamp=123456,
            original_width=256,
            original_height=144,
            encoding="jpeg",
            image_data=image,
        ),
    )
    yield ai_service_pb2.VideoRequest(
        session_id=session_id,
        stream_id=stream_id,
        is_eos=True,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default="127.0.0.1:50052")
    parser.add_argument("--image", required=True)
    args = parser.parse_args()

    image = Path(args.image).read_bytes()
    channel = grpc.insecure_channel(
        args.target,
        options=[
            ("grpc.max_receive_message_length", 16 * 1024 * 1024),
            ("grpc.max_send_message_length", 16 * 1024 * 1024),
        ],
    )
    stub = ai_service_pb2_grpc.TranslationServiceStub(channel)

    for response in stub.ProcessVideoStream(
        requests("CALL-VIDEO-GRPC-TEST", "video-0", image)
    ):
        if response.is_final:
            print("final")
            continue
        mask = response.mask
        print(
            {
                "session_id": response.session_id,
                "stream_id": response.stream_id,
                "frame_id": mask.frame_id,
                "rtp_timestamp": mask.rtp_timestamp,
                "mask_width": mask.mask_width,
                "mask_height": mask.mask_height,
                "rle_runs": len(mask.rle_counts),
                "latency_ms": mask.latency_ms,
                "status": mask.status,
                "error_message": mask.error_message,
            }
        )


if __name__ == "__main__":
    main()

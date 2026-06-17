from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger("ai_engine.video")


def _rle_encode(mask: np.ndarray) -> list[int]:
    """COCO-style alternating zero/one run lengths, row-major."""
    flat = np.asarray(mask, dtype=np.uint8).reshape(-1)
    runs: list[int] = []
    current = 0
    count = 0
    for value in flat:
        bit = 1 if value else 0
        if bit == current:
            count += 1
        else:
            runs.append(count)
            count = 1
            current = bit
    runs.append(count)
    return runs


class LazySegmenter:
    def __init__(self, model_path: str, threshold: float = 0.5):
        self.model_path = Path(model_path).expanduser().resolve()
        self.threshold = threshold
        self._segmenter = None
        self._lock = threading.Lock()
        self._infer_lock = threading.Lock()

    def _load(self):
        if self._segmenter is not None:
            return self._segmenter
        with self._lock:
            if self._segmenter is not None:
                return self._segmenter
            if not self.model_path.exists():
                raise FileNotFoundError(f"MediaPipe model not found: {self.model_path}")
            from mediapipe.tasks import python
            from mediapipe.tasks.python import vision

            logger.info("Loading MediaPipe ImageSegmenter model_path=%s", self.model_path)
            options = vision.ImageSegmenterOptions(
                base_options=python.BaseOptions(model_asset_path=str(self.model_path)),
                running_mode=vision.RunningMode.IMAGE,
                output_category_mask=False,
                output_confidence_masks=True,
            )
            self._segmenter = vision.ImageSegmenter.create_from_options(options)
            logger.info("MediaPipe ImageSegmenter loaded")
        return self._segmenter

    def infer(self, jpeg: bytes, threshold: Optional[float] = None) -> tuple[np.ndarray, float]:
        from mediapipe import Image, ImageFormat

        arr = np.frombuffer(jpeg, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError("cannot decode JPEG")
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        mp_image = Image(image_format=ImageFormat.SRGB, data=rgb)
        segmenter = self._load()
        started = time.perf_counter()
        with self._infer_lock:
            result = segmenter.segment(mp_image)
        latency_ms = (time.perf_counter() - started) * 1000.0
        if not result.confidence_masks:
            raise RuntimeError("MediaPipe returned no confidence mask")
        confidence = result.confidence_masks[0].numpy_view().copy()
        binary = (confidence >= (self.threshold if threshold is None else threshold)).astype(np.uint8)
        return binary, latency_ms


class VideoInferenceHTTPServer:
    def __init__(self, address: str, model_path: str, threshold: float = 0.5, mock_mask: bool = False):
        host, port_text = address.rsplit(":", 1)
        self.address = (host, int(port_text))
        self.segmenter = LazySegmenter(model_path, threshold)
        self.mock_mask = mock_mask
        self.server: Optional[ThreadingHTTPServer] = None

    def start(self) -> ThreadingHTTPServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "MCFVideoAI/1.0"

            def log_message(self, fmt, *args):
                logger.debug("video-http " + fmt, *args)

            def _json(self, status: int, body: dict):
                payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path == "/healthz":
                    self._json(200, {"status": "ok", "model_path": str(outer.segmenter.model_path)})
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                if self.path != "/v1/video/infer":
                    self._json(404, {"error": "not found"})
                    return
                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                    if content_length <= 0 or content_length > 12 * 1024 * 1024:
                        raise ValueError("invalid JPEG body size")
                    jpeg = self.rfile.read(content_length)
                    session_id = self.headers.get("X-Session-ID", "")
                    stream_id = self.headers.get("X-Stream-ID", "video-0")
                    frame_id = int(self.headers.get("X-Frame-ID", "0"))
                    rtp_timestamp = int(self.headers.get("X-RTP-Timestamp", "0"))
                    threshold_header = self.headers.get("X-Mask-Threshold")
                    threshold = float(threshold_header) if threshold_header else None

                    if outer.mock_mask:
                        decoded = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
                        if decoded is None:
                            raise ValueError("cannot decode JPEG")
                        h, w = decoded.shape[:2]
                        mask = np.zeros((h, w), np.uint8)
                        cv2.ellipse(mask, (w // 2, h // 2), (max(1, w // 4), max(1, h // 2 - 2)), 0, 0, 360, 1, -1)
                        latency_ms = 0.0
                    else:
                        mask, latency_ms = outer.segmenter.infer(jpeg, threshold)
                    runs = _rle_encode(mask)
                    self._json(200, {
                        "session_id": session_id,
                        "stream_id": stream_id,
                        "frame_id": frame_id,
                        "rtp_timestamp": rtp_timestamp,
                        "mask_width": int(mask.shape[1]),
                        "mask_height": int(mask.shape[0]),
                        "rle_counts": runs,
                        "rle_runs": len(runs),
                        "latency_ms": round(latency_ms, 3),
                        "status": "ok",
                    })
                except Exception as exc:
                    logger.exception("Video inference failed")
                    self._json(500, {"status": "error", "error_message": str(exc)})

        self.server = ThreadingHTTPServer(self.address, Handler)
        thread = threading.Thread(target=self.server.serve_forever, name="video-http", daemon=True)
        thread.start()
        logger.info("Video AI HTTP server listening address=%s:%s model=%s", *self.address, self.segmenter.model_path)
        return self.server


def start_video_http_server() -> Optional[ThreadingHTTPServer]:
    enabled = os.getenv("VIDEO_HTTP_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    if not enabled:
        logger.info("Video HTTP inference is disabled")
        return None
    current_dir = Path(__file__).resolve().parents[1]
    default_model = current_dir.parent / "model_checkpoints" / "selfie_segmenter.tflite"
    address = os.getenv("VIDEO_HTTP_ADDRESS", "0.0.0.0:50053")
    model_path = os.getenv("VIDEO_MODEL_PATH", str(default_model))
    threshold = float(os.getenv("VIDEO_MASK_THRESHOLD", "0.5"))
    mock_mask = os.getenv("VIDEO_MOCK_MASK", "false").lower() in {"1", "true", "yes", "on"}
    return VideoInferenceHTTPServer(address, model_path, threshold, mock_mask).start()

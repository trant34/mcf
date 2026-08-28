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

from .mf_callback_client import MFCallbackClient

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


class LiveStreamSegmentSession:
    """One MediaPipe ImageSegmenter instance running in LIVE_STREAM mode,
    scoped to a single (session_id, stream_id).

    New in v3.1 (rtpgw_design_v3.md section 35), replacing the shared
    IMAGE-mode `LazySegmenter` for the RTPGW -> AI Engine gRPC path. Two
    things force one instance per stream instead of one shared global
    instance:

    1. LIVE_STREAM input timestamps must be monotonically increasing *per
       segmenter instance* -- interleaving frames from two different RTP
       streams into one shared segmenter would violate that and MediaPipe
       raises.
    2. LIVE_STREAM mode keeps temporal state internally for smoother masks
       across frames; sharing that state across unrelated calls would be a
       correctness bug, not just a performance one.

    `segment_async` returns immediately; the result arrives later on a
    MediaPipe-owned callback thread, so this class also owns pushing the
    result to MF over HTTP (mf_callback_client.MFCallbackClient) instead of
    returning it synchronously.
    """

    def __init__(
        self,
        model_path: str,
        threshold: float,
        session_id: str,
        stream_id: str,
        callback_client: MFCallbackClient,
        logger_: logging.Logger,
    ):
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        self._vision = vision
        self.threshold = threshold
        self.session_id = session_id
        self.stream_id = stream_id
        self.callback_client = callback_client
        self.logger = logger_

        self._lock = threading.Lock()
        self._last_ts_ms = -1
        self._pending: dict[int, tuple[int, int, float]] = {}  # ts_ms -> (frame_id, rtp_ts, submit_time)

        options = vision.ImageSegmenterOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(model_path)),
            running_mode=vision.RunningMode.LIVE_STREAM,
            output_category_mask=False,
            output_confidence_masks=True,
            result_callback=self._on_result,
        )
        self._segmenter = vision.ImageSegmenter.create_from_options(options)

    def submit(self, jpeg: bytes, frame_id: int, rtp_timestamp: int) -> None:
        """Decode JPEG -> RGB, submit to MediaPipe LIVE_STREAM asynchronously.

        Never blocks on inference; the result (if any) is delivered later to
        `_on_result` on MediaPipe's internal thread.
        """
        from mediapipe import Image, ImageFormat

        arr = np.frombuffer(jpeg, dtype=np.uint8)
        bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError("cannot decode JPEG")
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        mp_image = Image(image_format=ImageFormat.SRGB, data=rgb)

        # RTP clock (90kHz) -> ms, per rtpgw_design_v3.md section 17. LIVE_STREAM
        # requires strictly increasing timestamps per instance, so clamp
        # forward by 1ms instead of dropping a frame outright if two frames
        # ever map to the same ms (can happen with a coarse RTP clock delta).
        ts_ms = max(0, rtp_timestamp // 90)
        submit_epoch_ms = int(time.time() * 1000)
        with self._lock:
            if ts_ms <= self._last_ts_ms:
                ts_ms = self._last_ts_ms + 1
            self._last_ts_ms = ts_ms
            self._pending[ts_ms] = (frame_id, rtp_timestamp, time.perf_counter(), submit_epoch_ms)

        # LATENCY_TRACE T4: about to call segment_async (start of MediaPipe
        # LIVE_STREAM inference). See rtpgw_design_v3.md section 36.
        self.logger.info(
            "LATENCY_TRACE stage=ai_submit stream_id=%s frame_id=%s rtp_ts=%s ts_ms=%s",
            self.stream_id, frame_id, rtp_timestamp, submit_epoch_ms,
        )

        self._segmenter.segment_async(mp_image, ts_ms)

    def _on_result(self, result, output_image, timestamp_ms: int) -> None:
        """MediaPipe result_callback -- runs on MediaPipe's own thread."""
        with self._lock:
            pending = self._pending.pop(timestamp_ms, None)
        if pending:
            frame_id, rtp_timestamp, submitted_at, submit_epoch_ms = pending
        else:
            frame_id, rtp_timestamp, submitted_at, submit_epoch_ms = 0, 0, time.perf_counter(), 0
        latency_ms = (time.perf_counter() - submitted_at) * 1000.0
        result_epoch_ms = int(time.time() * 1000)

        # LATENCY_TRACE T5: MediaPipe result_callback fired -- this IS the
        # pure MediaPipe LIVE_STREAM inference latency (T5 - T4).
        self.logger.info(
            "LATENCY_TRACE stage=ai_result stream_id=%s frame_id=%s rtp_ts=%s "
            "ts_ms=%s inference_latency_ms=%.3f",
            self.stream_id, frame_id, rtp_timestamp, result_epoch_ms, latency_ms,
        )

        if not result.confidence_masks:
            self.logger.warning(
                "LIVE_STREAM segmentation returned no confidence mask stream_id=%s frame_id=%s",
                self.stream_id, frame_id,
            )
            self.callback_client.push_mask(
                session_id=self.session_id, stream_id=self.stream_id,
                frame_id=frame_id, rtp_timestamp=rtp_timestamp,
                mask_width=0, mask_height=0, rle_counts=[], latency_ms=latency_ms,
                status="error", error_message="no confidence mask",
                t_ai_submit_ms=submit_epoch_ms, t_ai_result_ms=result_epoch_ms,
            )
            return

        confidence = result.confidence_masks[0].numpy_view()
        binary = (confidence >= self.threshold).astype(np.uint8)
        runs = _rle_encode(binary)

        self.callback_client.push_mask(
            session_id=self.session_id, stream_id=self.stream_id,
            frame_id=frame_id, rtp_timestamp=rtp_timestamp,
            mask_width=int(binary.shape[1]), mask_height=int(binary.shape[0]),
            rle_counts=runs, latency_ms=latency_ms, status="ok",
            t_ai_submit_ms=submit_epoch_ms, t_ai_result_ms=result_epoch_ms,
        )

    def close(self) -> None:
        try:
            self._segmenter.close()
        except Exception:
            self.logger.exception("error closing LIVE_STREAM segmenter stream_id=%s", self.stream_id)


class LiveStreamSegmentPool:
    """Owns one LiveStreamSegmentSession per (session_id, stream_id),
    created on the video stream's `config` message and torn down on eos /
    gRPC stream close (see ai_server.py ProcessVideoStream)."""

    def __init__(self, model_path: str, default_threshold: float, mf_callback_url: str, mf_callback_timeout_ms: int):
        self.model_path = model_path
        self.default_threshold = default_threshold
        self.callback_client = MFCallbackClient(url=mf_callback_url, timeout_s=mf_callback_timeout_ms / 1000.0)
        self._sessions: dict[tuple[str, str], LiveStreamSegmentSession] = {}
        self._lock = threading.Lock()

    def open(self, session_id: str, stream_id: str, threshold: Optional[float] = None) -> LiveStreamSegmentSession:
        key = (session_id, stream_id)
        with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                return existing
            session = LiveStreamSegmentSession(
                self.model_path,
                threshold if threshold is not None else self.default_threshold,
                session_id, stream_id,
                self.callback_client,
                logger,
            )
            self._sessions[key] = session
            return session

    def get(self, session_id: str, stream_id: str) -> Optional[LiveStreamSegmentSession]:
        with self._lock:
            return self._sessions.get((session_id, stream_id))

    def close(self, session_id: str, stream_id: str) -> None:
        with self._lock:
            session = self._sessions.pop((session_id, stream_id), None)
        if session is not None:
            session.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()


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
    # Deprecated as of v3.1: this synchronous IMAGE-mode HTTP endpoint
    # (POST /v1/video/infer, blocking request/response) was used when MF
    # called the AI Engine directly. It is superseded by the RTPGW -> AI
    # Engine gRPC stream (ai_service.proto) + LiveStreamSegmentPool above,
    # with results pushed to MF asynchronously over HTTP instead of
    # returned synchronously. Left disabled by default but still available
    # for manual/legacy testing (rtpgw_design_v3.md section 35).
    enabled = os.getenv("VIDEO_HTTP_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
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

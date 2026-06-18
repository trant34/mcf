from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import List
import time

import numpy as np


class VideoBackgroundPipeline:
    """Incremental H264 -> background effect -> H264 pipeline.

    The heavy video dependencies are imported lazily when a video stream starts,
    so the existing audio service can still start even when video is disabled.
    """

    def __init__(
        self,
        model_path: str,
        effect_type: str,
        background_path: str,
        inference_fps: int,
        video_fps: int,
        inference_width: int,
        inference_height: int,
        threshold: float,
        smoothing_alpha: float,
        logger,
        session_id: str,
    ) -> None:
        try:
            import av
            import cv2
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
        except Exception as exc:  # pragma: no cover - runtime dependency guard
            raise RuntimeError(
                "Video dependencies are unavailable. Install mediapipe, av and opencv-python."
            ) from exc

        self.av = av
        self.cv2 = cv2
        self.mp = mp
        self.mp_python = mp_python
        self.vision = vision
        self.logger = logger
        self.session_id = session_id

        self.effect_type = effect_type
        self.inference_fps = max(1, int(inference_fps))
        self.video_fps = max(1, int(video_fps))
        self.inference_width = max(1, int(inference_width))
        self.inference_height = max(1, int(inference_height))
        self.threshold = float(np.clip(threshold, 0.0, 0.99))
        self.smoothing_alpha = float(np.clip(smoothing_alpha, 0.0, 0.99))
        self.inference_interval = max(1, round(self.video_fps / self.inference_fps))

        self.frame_index = 0
        self.encoder_pts = 0
        self.latest_mask: np.ndarray | None = None
        self.encoder = None
        self.output_width = None
        self.output_height = None
        self.cached_background: np.ndarray | None = None

        resolved_model = self._resolve_path(model_path)
        if not resolved_model.exists():
            raise FileNotFoundError(f"Segmentation model not found: {resolved_model}")

        self.segmenter = None
        if self.effect_type not in ("none", "passthrough"):
            options = vision.ImageSegmenterOptions(
                base_options=mp_python.BaseOptions(model_asset_path=str(resolved_model)),
                running_mode=vision.RunningMode.IMAGE,
                output_category_mask=False,
                output_confidence_masks=True,
            )
            self.segmenter = vision.ImageSegmenter.create_from_options(options)
        self.decoder = av.CodecContext.create("h264", "r")

        self.background_path = self._resolve_path(background_path) if background_path else None
        self.background_source = None
        if self.background_path and self.background_path.exists():
            self.background_source = cv2.imread(str(self.background_path))
            if self.background_source is None:
                raise RuntimeError(f"Cannot read background image: {self.background_path}")

        logger.info(
            "Video background pipeline initialized",
            extra={
                "session_id": session_id,
                "context": {
                    "effect_type": self.effect_type,
                    "video_fps": self.video_fps,
                    "inference_fps": self.inference_fps,
                    "inference_size": f"{self.inference_width}x{self.inference_height}",
                    "model_path": str(resolved_model),
                    "background_path": str(self.background_path) if self.background_path else "",
                },
            },
        )

    @staticmethod
    def _resolve_path(value: str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path

        current = Path(__file__).resolve().parent
        candidates = [
            Path.cwd() / path,
            current / path,
            current.parent / path,
            current.parent.parent / path,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return candidates[0].resolve()

    def process_access_unit(self, annexb: bytes) -> List[bytes]:
        if not annexb:
            return []

        outputs: List[bytes] = []
        for packet in self.decoder.parse(annexb):
            for decoded in self.decoder.decode(packet):
                frame = decoded.to_ndarray(format="bgr24")
                processed = self._process_frame(frame)
                outputs.extend(self._encode_frame(processed))
        return outputs

    def _process_frame(self, frame: np.ndarray) -> np.ndarray:
        self.frame_index += 1
        if self.effect_type in ("none", "passthrough"):
            return frame.copy()
        should_infer = self.latest_mask is None or (self.frame_index - 1) % self.inference_interval == 0

        if should_infer:
            started = time.perf_counter()
            current_mask = self._segment(frame)
            if self.latest_mask is None:
                self.latest_mask = current_mask
            else:
                self.latest_mask = (
                    self.smoothing_alpha * self.latest_mask
                    + (1.0 - self.smoothing_alpha) * current_mask
                )
            self.logger.debug(
                "Video segmentation completed",
                extra={
                    "session_id": self.session_id,
                    "context": {
                        "frame_index": self.frame_index,
                        "latency_ms": round((time.perf_counter() - started) * 1000.0, 2),
                    },
                },
            )

        if self.latest_mask is None or self.effect_type in ("none", "passthrough"):
            return frame.copy()

        alpha = self.cv2.resize(
            self.latest_mask,
            (frame.shape[1], frame.shape[0]),
            interpolation=self.cv2.INTER_LINEAR,
        )
        alpha = self.cv2.GaussianBlur(alpha, (15, 15), 0)
        alpha = np.clip(alpha, 0.0, 1.0)[..., None].astype(np.float32)

        if self.effect_type == "bg_blur":
            background = self.cv2.GaussianBlur(frame, (0, 0), sigmaX=16, sigmaY=16)
        elif self.effect_type == "bg_remove":
            background = np.zeros_like(frame)
        else:  # bg_replace
            background = self._background_for(frame.shape[1], frame.shape[0])

        composed = alpha * frame.astype(np.float32) + (1.0 - alpha) * background.astype(np.float32)
        return np.clip(composed, 0, 255).astype(np.uint8)

    def _segment(self, frame: np.ndarray) -> np.ndarray:
        resized = self.cv2.resize(
            frame,
            (self.inference_width, self.inference_height),
            interpolation=self.cv2.INTER_LINEAR,
        )
        rgb = self.cv2.cvtColor(resized, self.cv2.COLOR_BGR2RGB)
        mp_image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb)
        result = self.segmenter.segment(mp_image)
        if not result.confidence_masks:
            raise RuntimeError("MediaPipe returned no confidence mask")

        mask = np.asarray(result.confidence_masks[0].numpy_view(), dtype=np.float32).squeeze().copy()
        # Convert the confidence map to a stable soft alpha around the threshold.
        denominator = max(1e-6, 1.0 - self.threshold)
        return np.clip((mask - self.threshold) / denominator, 0.0, 1.0)

    def _background_for(self, width: int, height: int) -> np.ndarray:
        if self.cached_background is not None and self.cached_background.shape[:2] == (height, width):
            return self.cached_background

        if self.background_source is None:
            self.cached_background = np.zeros((height, width, 3), dtype=np.uint8)
            return self.cached_background

        src = self.background_source
        scale = max(width / src.shape[1], height / src.shape[0])
        new_width = max(width, int(round(src.shape[1] * scale)))
        new_height = max(height, int(round(src.shape[0] * scale)))
        resized = self.cv2.resize(src, (new_width, new_height), interpolation=self.cv2.INTER_LINEAR)
        x0 = max(0, (new_width - width) // 2)
        y0 = max(0, (new_height - height) // 2)
        self.cached_background = resized[y0:y0 + height, x0:x0 + width].copy()
        return self.cached_background

    def _ensure_encoder(self, width: int, height: int) -> None:
        if self.encoder is not None:
            return

        encoder = None
        last_error = None
        for codec_name in ("libx264", "h264"):
            try:
                encoder = self.av.CodecContext.create(codec_name, "w")
                break
            except Exception as exc:  # pragma: no cover - depends on FFmpeg build
                last_error = exc
        if encoder is None:
            raise RuntimeError("No H264 encoder is available in PyAV") from last_error

        encoder.width = width
        encoder.height = height
        encoder.pix_fmt = "yuv420p"
        encoder.time_base = Fraction(1, self.video_fps)
        encoder.framerate = Fraction(self.video_fps, 1)
        encoder.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "profile": "baseline",
            "x264-params": "keyint=30:min-keyint=30:scenecut=0:repeat-headers=1:aud=1:bframes=0",
        }
        encoder.open()
        self.encoder = encoder
        self.output_width = width
        self.output_height = height

    def _encode_frame(self, frame: np.ndarray) -> List[bytes]:
        height, width = frame.shape[:2]
        self._ensure_encoder(width, height)

        video_frame = self.av.VideoFrame.from_ndarray(frame, format="bgr24")
        video_frame = video_frame.reformat(width=width, height=height, format="yuv420p")
        video_frame.pts = self.encoder_pts
        self.encoder_pts += 1
        return [bytes(packet) for packet in self.encoder.encode(video_frame)]

    def close(self) -> List[bytes]:
        outputs: List[bytes] = []
        try:
            # Flush delayed decoder frames first.
            for decoded in self.decoder.decode(None):
                frame = decoded.to_ndarray(format="bgr24")
                outputs.extend(self._encode_frame(self._process_frame(frame)))
        except Exception:
            pass

        if self.encoder is not None:
            try:
                outputs.extend(bytes(packet) for packet in self.encoder.encode(None))
            except Exception:
                pass

        if self.segmenter is not None:
            try:
                self.segmenter.close()
            except Exception:
                pass
        return outputs

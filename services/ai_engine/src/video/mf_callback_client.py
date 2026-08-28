"""
Once a LIVE_STREAM segmentation callback fires, the AI Engine no longer routes the mask back
over the RTPGW <-> AI Engine gRPC stream (ai_service.proto ProcessVideoStream)
-- it pushes the RLE mask directly to MF over HTTP instead. MF already has an
HTTP endpoint for this (see mf_cpp mask_callback_server.hpp / .cpp), so this
is a thin, dependency-free POST client (stdlib urllib, no extra requirements).

Fire-and-forget by design: a failed push must never block or crash the
MediaPipe result_callback thread. Failures are logged and counted; MF simply
keeps compositing with its last-known-good mask (rtpgw_design_v3.md section
21, "latest frame > complete frame history" applies just as much to masks as
to frames).
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("ai_engine.video.mf_callback")


@dataclass
class MFCallbackClient:
    url: str
    timeout_s: float = 0.8

    def push_mask(
        self,
        *,
        session_id: str,
        stream_id: str,
        frame_id: int,
        rtp_timestamp: int,
        mask_width: int,
        mask_height: int,
        rle_counts: list[int],
        latency_ms: float,
        status: str = "ok",
        error_message: Optional[str] = None,
        t_ai_submit_ms: Optional[int] = None,
        t_ai_result_ms: Optional[int] = None,
    ) -> bool:
        if not self.url:
            logger.debug("MF callback URL not configured, dropping mask push")
            return False

        t_ai_push_ms = int(time.time() * 1000)
        # LATENCY_TRACE T6: about to HTTP POST the result to MF. See
        # rtpgw_design_v3.md section 36.
        logger.info(
            "LATENCY_TRACE stage=ai_push stream_id=%s frame_id=%s rtp_ts=%s ts_ms=%s",
            stream_id, frame_id, rtp_timestamp, t_ai_push_ms,
        )

        body = {
            "session_id": session_id,
            "stream_id": stream_id,
            "frame_id": frame_id,
            "rtp_timestamp": rtp_timestamp,
            "mask_width": mask_width,
            "mask_height": mask_height,
            "rle_counts": rle_counts,
            "latency_ms": round(latency_ms, 3),
            "status": status,
            # Wall-clock epoch ms checkpoints, forwarded as-is so MF can
            # print a full per-frame latency breakdown without needing to
            # separately correlate 3 processes' log files by frame_id (see
            # rtpgw_design_v3.md section 36). Only meaningful for stage
            # deltas computed on the SAME host / synced clocks.
            "t_ai_submit_ms": t_ai_submit_ms,
            "t_ai_result_ms": t_ai_result_ms,
            "t_ai_push_ms": t_ai_push_ms,
        }
        if error_message:
            body["error_message"] = error_message

        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                if resp.status >= 300:
                    logger.warning(
                        "MF callback returned non-2xx status=%s stream_id=%s frame_id=%s",
                        resp.status, stream_id, frame_id,
                    )
                    return False
                return True
        except urllib.error.URLError as exc:
            logger.warning(
                "MF callback push failed stream_id=%s frame_id=%s error=%s",
                stream_id, frame_id, exc,
            )
            return False
        except Exception:  # pragma: no cover - defensive, never raise into MediaPipe's thread
            logger.exception(
                "Unexpected error pushing mask to MF stream_id=%s frame_id=%s",
                stream_id, frame_id,
            )
            return False

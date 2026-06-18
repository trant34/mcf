#!/usr/bin/env python3
"""Mode-B MF prototype.

Media path stays in MF:
RTP/H264 -> depacketize -> decode -> sampled JPEG -> MCF HTTPGW -> mask
-> composite in MF -> MP4 output (and optionally later RTP output).
"""
from __future__ import annotations

import argparse
import collections
import json
import queue
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import requests

START_CODE = b"\x00\x00\x00\x01"
SEQ_MOD = 65536


def log(message: str) -> None:
    print(message, flush=True)


@dataclass
class RTPPacket:
    seq: int
    ts: int
    ssrc: int
    marker: bool
    pt: int
    payload: bytes


def parse_rtp(data: bytes) -> Optional[RTPPacket]:
    if len(data) < 12 or data[0] >> 6 != 2:
        return None
    cc = data[0] & 0x0F
    has_extension = bool((data[0] >> 4) & 1)
    marker = bool((data[1] >> 7) & 1)
    payload_type = data[1] & 0x7F
    seq = struct.unpack_from(">H", data, 2)[0]
    timestamp = struct.unpack_from(">I", data, 4)[0]
    ssrc = struct.unpack_from(">I", data, 8)[0]
    offset = 12 + cc * 4
    if offset > len(data):
        return None
    if has_extension:
        if offset + 4 > len(data):
            return None
        extension_words = struct.unpack_from(">H", data, offset + 2)[0]
        offset += 4 + extension_words * 4
        if offset > len(data):
            return None
    return RTPPacket(seq, timestamp, ssrc, marker, payload_type, data[offset:])


class LatestQueue:
    def __init__(self, maxsize: int = 1):
        self.queue: queue.Queue = queue.Queue(maxsize=maxsize)

    def put_latest(self, item) -> None:
        while True:
            try:
                self.queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    pass

    def get(self, timeout: Optional[float] = None):
        return self.queue.get(timeout=timeout)


class AccessUnitAssembler:
    def __init__(self):
        self.timestamp: Optional[int] = None
        self.packets: list[tuple[int, bytes]] = []

    def push(self, packet: RTPPacket) -> list[tuple[int, list[tuple[int, bytes]]]]:
        output = []
        if self.timestamp is None:
            self.timestamp = packet.ts
        if packet.ts != self.timestamp:
            if self.packets:
                output.append((self.timestamp, self._sorted(self.packets)))
            self.timestamp = packet.ts
            self.packets = []
        self.packets.append((packet.seq, packet.payload))
        if packet.marker:
            output.append((packet.ts, self._sorted(self.packets)))
            self.timestamp = None
            self.packets = []
        return output

    @staticmethod
    def _sorted(packets: list[tuple[int, bytes]]) -> list[tuple[int, bytes]]:
        base = packets[0][0]
        return sorted(packets, key=lambda item: (item[0] - base) % SEQ_MOD)


class H264Depacketizer:
    def __init__(self):
        self.sps: Optional[bytes] = None
        self.pps: Optional[bytes] = None

    def depacketize(self, packets: list[tuple[int, bytes]]) -> bytes:
        nalus: list[bytes] = []
        fu_buffer = bytearray()
        fu_active = False
        for _, payload in packets:
            if not payload:
                continue
            nal_type = payload[0] & 0x1F
            if 1 <= nal_type <= 23:
                nalus.append(payload)
            elif nal_type == 24:  # STAP-A
                pos = 1
                while pos + 2 <= len(payload):
                    size = struct.unpack_from(">H", payload, pos)[0]
                    pos += 2
                    if size <= 0 or pos + size > len(payload):
                        break
                    nalus.append(payload[pos : pos + size])
                    pos += size
            elif nal_type == 28 and len(payload) >= 2:  # FU-A
                fu_header = payload[1]
                start = bool(fu_header & 0x80)
                end = bool(fu_header & 0x40)
                reconstructed_type = fu_header & 0x1F
                nri = payload[0] & 0x60
                forbidden = payload[0] & 0x80
                if start:
                    fu_buffer = bytearray([forbidden | nri | reconstructed_type])
                    fu_buffer.extend(payload[2:])
                    fu_active = True
                elif fu_active:
                    fu_buffer.extend(payload[2:])
                if end and fu_active:
                    nalus.append(bytes(fu_buffer))
                    fu_active = False
        if not nalus:
            return b""
        for nalu in nalus:
            kind = nalu[0] & 0x1F
            if kind == 7:
                self.sps = nalu
            elif kind == 8:
                self.pps = nalu
        has_idr = any((n[0] & 0x1F) == 5 for n in nalus)
        has_sps = any((n[0] & 0x1F) == 7 for n in nalus)
        has_pps = any((n[0] & 0x1F) == 8 for n in nalus)
        final: list[bytes] = []
        if has_idr:
            if self.sps is not None and not has_sps:
                final.append(self.sps)
            if self.pps is not None and not has_pps:
                final.append(self.pps)
        final.extend(nalus)
        return b"".join(START_CODE + nalu for nalu in final)


class FFmpegDecoder:
    def __init__(self, width: int, height: int, ffmpeg: str = "ffmpeg", debug: bool = False):
        self.width = width
        self.height = height
        self.frame_size = width * height * 3
        self.frames = LatestQueue(8)
        self.timestamps: collections.deque[int] = collections.deque()
        self.running = True
        self.buffer = bytearray()
        command = [
            ffmpeg, "-loglevel", "warning" if debug else "error",
            "-f", "h264", "-probesize", "1M", "-analyzeduration", "1M",
            "-i", "pipe:0", "-an", "-vsync", "0",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
        ]
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None if debug else subprocess.DEVNULL, bufsize=0,
        )
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def feed(self, annexb: bytes, rtp_timestamp: int) -> None:
        if not annexb or self.process.stdin is None:
            return
        self.timestamps.append(rtp_timestamp)
        try:
            self.process.stdin.write(annexb)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            log(f"[MF][decoder] feed failed: {exc}")

    def _reader(self) -> None:
        assert self.process.stdout is not None
        while self.running:
            chunk = self.process.stdout.read(64 * 1024)
            if not chunk:
                if self.process.poll() is not None:
                    break
                time.sleep(0.002)
                continue
            self.buffer.extend(chunk)
            while len(self.buffer) >= self.frame_size:
                raw = bytes(self.buffer[: self.frame_size])
                del self.buffer[: self.frame_size]
                frame = np.frombuffer(raw, np.uint8).reshape(self.height, self.width, 3).copy()
                rtp_ts = self.timestamps.popleft() if self.timestamps else 0
                self.frames.put_latest((rtp_ts, frame, time.perf_counter()))

    def close(self) -> None:
        self.running = False
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()


class MaskStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.mask: Optional[np.ndarray] = None
        self.frame_id = -1
        self.rtp_timestamp = 0
        self.updated_at = 0.0

    def update(self, mask: np.ndarray, frame_id: int, rtp_timestamp: int) -> None:
        with self.lock:
            self.mask = mask
            self.frame_id = frame_id
            self.rtp_timestamp = rtp_timestamp
            self.updated_at = time.perf_counter()

    def latest(self) -> Optional[np.ndarray]:
        with self.lock:
            return None if self.mask is None else self.mask.copy()


def decode_rle(counts: list[int], width: int, height: int) -> np.ndarray:
    total = width * height
    output = np.empty(total, dtype=np.uint8)
    pos = 0
    value = 0
    for count in counts:
        end = min(total, pos + int(count))
        output[pos:end] = value
        pos = end
        value = 1 - value
        if pos >= total:
            break
    if pos < total:
        output[pos:] = 0
    return output.reshape(height, width)


class MCFInferenceClient:
    def __init__(self, endpoint: str, session_id: str, stream_id: str, timeout: float):
        self.endpoint = endpoint
        self.session_id = session_id
        self.stream_id = stream_id
        self.timeout = timeout
        self.http = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
        self.http.mount("http://", adapter)
        self.http.mount("https://", adapter)

    def infer(self, frame_id: int, rtp_timestamp: int, frame: np.ndarray, effect: str, jpeg_quality: int):
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        headers = {
            "Content-Type": "image/jpeg",
            "X-Session-ID": self.session_id,
            "X-Stream-ID": self.stream_id,
            "X-Frame-ID": str(frame_id),
            "X-RTP-Timestamp": str(rtp_timestamp),
            "X-Original-Width": str(frame.shape[1]),
            "X-Original-Height": str(frame.shape[0]),
            "X-Effect-Type": effect,
        }
        started = time.perf_counter()
        response = self.http.post(self.endpoint, data=encoded.tobytes(), headers=headers, timeout=self.timeout)
        response.raise_for_status()
        result = response.json()
        result["round_trip_ms"] = (time.perf_counter() - started) * 1000.0
        return result


class MFModeB:
    def __init__(self, args):
        self.args = args
        self.running = True
        self.assembler = AccessUnitAssembler()
        self.depacketizer = H264Depacketizer()
        self.decoder = FFmpegDecoder(args.width, args.height, args.ffmpeg, args.debug_ffmpeg)
        self.mask_store = MaskStore()
        self.inference_queue = LatestQueue(1)
        self.client = MCFInferenceClient(args.mcf_url, args.session_id, args.stream_id, args.http_timeout)
        self.packet_count = 0
        self.au_count = 0
        self.frame_count = 0
        self.inference_count = 0
        self.inference_errors = 0
        self.last_inference_time = 0.0
        self.background = self._load_background()
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        self.writer = cv2.VideoWriter(args.output, fourcc, args.fps, (args.width, args.height))
        if not self.writer.isOpened():
            raise RuntimeError(f"cannot open output writer: {args.output}")

    def _load_background(self) -> np.ndarray:
        if self.args.background:
            image = cv2.imread(self.args.background)
            if image is None:
                raise FileNotFoundError(self.args.background)
            return cv2.resize(image, (self.args.width, self.args.height))
        return np.zeros((self.args.height, self.args.width, 3), dtype=np.uint8)

    def inference_worker(self) -> None:
        while self.running:
            try:
                frame_id, rtp_ts, frame = self.inference_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                small = cv2.resize(frame, (self.args.infer_width, self.args.infer_height))
                result = self.client.infer(frame_id, rtp_ts, small, self.args.effect, self.args.jpeg_quality)
                if result.get("status") != "ok":
                    raise RuntimeError(result.get("error_message", "AI returned error"))
                mask = decode_rle(result["rle_counts"], int(result["mask_width"]), int(result["mask_height"]))
                self.mask_store.update(mask, int(result["frame_id"]), int(result["rtp_timestamp"]))
                self.inference_count += 1
                if self.inference_count <= 5 or self.inference_count % 20 == 0:
                    log(f"[MF][AI] n={self.inference_count} frame={frame_id} ai_ms={result.get('latency_ms')} rtt_ms={result['round_trip_ms']:.1f} runs={result.get('rle_runs')}")
            except Exception as exc:
                self.inference_errors += 1
                log(f"[MF][AI] error frame={frame_id}: {exc}")

    def compose(self, frame: np.ndarray) -> np.ndarray:
        mask = self.mask_store.latest()
        if mask is None:
            return frame
        alpha = cv2.resize(mask.astype(np.float32), (self.args.width, self.args.height), interpolation=cv2.INTER_LINEAR)
        alpha = cv2.GaussianBlur(alpha, (0, 0), self.args.mask_blur_sigma)
        alpha = np.clip(alpha, 0.0, 1.0)[..., None]
        if self.args.effect == "bg_blur":
            background = cv2.GaussianBlur(frame, (0, 0), self.args.background_blur_sigma)
        elif self.args.effect == "bg_remove":
            background = np.zeros_like(frame)
        else:
            background = self.background
        output = alpha * frame.astype(np.float32) + (1.0 - alpha) * background.astype(np.float32)
        return np.clip(output, 0, 255).astype(np.uint8)

    def run(self) -> None:
        worker = threading.Thread(target=self.inference_worker, daemon=True)
        worker.start()
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        receiver.bind((self.args.listen_host, self.args.listen_port))
        receiver.settimeout(0.05)
        log(f"[MF] listening udp://{self.args.listen_host}:{self.args.listen_port}")
        last_packet = time.perf_counter()
        try:
            while self.running:
                try:
                    data, _ = receiver.recvfrom(65535)
                    last_packet = time.perf_counter()
                    packet = parse_rtp(data)
                    if packet is None:
                        continue
                    if self.args.payload_type is not None and packet.pt != self.args.payload_type:
                        continue
                    if self.args.ssrc is not None and packet.ssrc != self.args.ssrc:
                        continue
                    self.packet_count += 1
                    for rtp_ts, packets in self.assembler.push(packet):
                        annexb = self.depacketizer.depacketize(packets)
                        if annexb:
                            self.au_count += 1
                            self.decoder.feed(annexb, rtp_ts)
                except socket.timeout:
                    pass

                while True:
                    try:
                        rtp_ts, frame, _ = self.decoder.frames.queue.get_nowait()
                    except queue.Empty:
                        break
                    self.frame_count += 1
                    now = time.perf_counter()
                    if now - self.last_inference_time >= 1.0 / self.args.infer_fps:
                        self.last_inference_time = now
                        self.inference_queue.put_latest((self.frame_count, rtp_ts, frame.copy()))
                    self.writer.write(self.compose(frame))
                    if self.frame_count <= 5 or self.frame_count % 100 == 0:
                        log(f"[MF] packets={self.packet_count} aus={self.au_count} frames={self.frame_count}")

                if self.packet_count > 0 and time.perf_counter() - last_packet > self.args.end_idle_seconds:
                    log("[MF] input idle timeout reached")
                    break
        finally:
            self.running = False
            receiver.close()
            self.decoder.close()
            # drain delayed decoder frames
            time.sleep(0.2)
            self.writer.release()
            log(f"[MF][DONE] packets={self.packet_count} aus={self.au_count} frames={self.frame_count} inference={self.inference_count} errors={self.inference_errors} output={self.args.output}")


def parse_int_auto(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    return int(value, 16) if value.lower().startswith("0x") else int(value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=5006)
    parser.add_argument("--payload-type", type=int, default=114)
    parser.add_argument("--ssrc", default="0x5cccb090")
    parser.add_argument("--width", type=int, default=240)
    parser.add_argument("--height", type=int, default=320)
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--infer-fps", type=float, default=5.0)
    parser.add_argument("--infer-width", type=int, default=256)
    parser.add_argument("--infer-height", type=int, default=144)
    parser.add_argument("--jpeg-quality", type=int, default=80)
    parser.add_argument("--mcf-url", default="http://127.0.0.1:8080/v1/video/infer")
    parser.add_argument("--http-timeout", type=float, default=1.5)
    parser.add_argument("--session-id", default="CALL-VIDEO-TEST")
    parser.add_argument("--stream-id", default="video-0")
    parser.add_argument("--effect", choices=["bg_replace", "bg_blur", "bg_remove"], default="bg_replace")
    parser.add_argument("--background", default="../services/ai_engine/model_checkpoints/bg_image.jpg")
    parser.add_argument("--output", default="output_mode_b.mp4")
    parser.add_argument("--mask-blur-sigma", type=float, default=2.0)
    parser.add_argument("--background-blur-sigma", type=float, default=12.0)
    parser.add_argument("--end-idle-seconds", type=float, default=2.0)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--debug-ffmpeg", action="store_true")
    args = parser.parse_args()
    args.ssrc = parse_int_auto(args.ssrc)
    MFModeB(args).run()


if __name__ == "__main__":
    main()

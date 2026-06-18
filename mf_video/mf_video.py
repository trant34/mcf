#!/usr/bin/env python3
"""
mf_video.py — Prototype test video flow with existing PCAP input.txt.

Real test flow:
PCAP/RTP/H264 input.txt
  -> parse RTP packets
  -> assemble H264 Access Units
  -> decode frames with ffmpeg
  -> send sampled frames to LOGIC HTTPGW /v1/video/segmentation
  -> LOGIC forwards to AI Engine
  -> receive RLE mask
  -> replace background locally
  -> write output_bg_replace.mp4

This script tests VIDEO ONLY. Audio is not used.

Recommended run:
python -u mf_video/mf_video.py \
  --pcap mf_video/input.txt \
  --width 240 --height 320 --fps 30 \
  --background services/ai_engine/model_checkpoints/bg_image.jpg \
  --logic-url http://127.0.0.1:8080 \
  --output output_bg_replace.mp4

Debug scan:
python -u mf_video/mf_video.py --pcap mf_video/input.txt --scan-only
"""

from __future__ import annotations

import argparse
import base64
import io
import os
import select
import struct
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import cv2
import dpkt
import numpy as np
import requests


START_CODE = b"\x00\x00\x00\x01"
SEQ_MOD = 65536


# =============================================================================
# Utils
# =============================================================================

def log(msg: str) -> None:
    print(msg, flush=True)


def parse_int_auto(x: Optional[str]) -> Optional[int]:
    if x is None or str(x).strip() == "":
        return None
    s = str(x).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def repair_pcap_headers(raw: bytes) -> bytes:
    """
    Repair only PCAP global header + packet record headers by replacing 0x20 with 0x00.
    This is safer than global replacement.
    """
    buf = bytearray(raw)
    if len(buf) < 24:
        return raw

    magic = bytes(buf[:4])
    endian = ">" if magic == b"\xa1\xb2\xc3\xd4" else "<"

    def fix(start: int, end: int) -> None:
        for i in range(start, min(end, len(buf))):
            if buf[i] == 0x20:
                buf[i] = 0x00

    fix(0, 24)
    off = 24
    while off + 16 <= len(buf):
        fix(off, off + 16)
        try:
            caplen = struct.unpack_from(endian + "I", buf, off + 8)[0]
        except Exception:
            break
        if caplen <= 0 or off + 16 + caplen > len(buf):
            break
        off += 16 + caplen

    return bytes(buf)


def repair_all_space_nulls(raw: bytes) -> bytes:
    """
    Aggressive repair used for some text-corrupted PCAPs:
    replace all byte 0x20 with 0x00.

    Use only if raw/header repair cannot detect RTP.
    """
    return bytes(0x00 if b == 0x20 else b for b in raw)


def make_pcap_reader(data: bytes):
    for cls in (dpkt.pcap.Reader, dpkt.pcapng.Reader):
        try:
            return cls(io.BytesIO(data))
        except Exception:
            pass
    raise RuntimeError("Cannot parse data as PCAP/PCAPNG")


def extract_ip_packet(buf: bytes):
    """
    Supports:
    - Ethernet capture
    - Linux cooked capture
    - raw IP packet capture
    """
    # Ethernet
    try:
        eth = dpkt.ethernet.Ethernet(buf)
        if isinstance(eth.data, (dpkt.ip.IP, dpkt.ip6.IP6)):
            return eth.data
    except Exception:
        pass

    # Linux cooked capture
    try:
        sll = dpkt.sll.SLL(buf)
        if isinstance(sll.data, (dpkt.ip.IP, dpkt.ip6.IP6)):
            return sll.data
    except Exception:
        pass

    # Raw IP
    try:
        if len(buf) >= 1:
            version = buf[0] >> 4
            if version == 4:
                return dpkt.ip.IP(buf)
            if version == 6:
                return dpkt.ip6.IP6(buf)
    except Exception:
        pass

    return None


# =============================================================================
# RTP
# =============================================================================

@dataclass
class RTPPacket:
    wall_ts: float
    seq: int
    rtp_ts: int
    ssrc: int
    marker: int
    pt: int
    payload: bytes
    udp_sport: int
    udp_dport: int


def parse_rtp(data: bytes) -> Optional[Tuple[int, int, int, int, int, bytes]]:
    if len(data) < 12:
        return None

    version = data[0] >> 6
    if version != 2:
        return None

    cc = data[0] & 0x0F
    has_ext = (data[0] >> 4) & 1
    marker = (data[1] >> 7) & 1
    pt = data[1] & 0x7F

    seq = struct.unpack_from(">H", data, 2)[0]
    ts = struct.unpack_from(">I", data, 4)[0]
    ssrc = struct.unpack_from(">I", data, 8)[0]

    off = 12 + cc * 4
    if off > len(data):
        return None

    if has_ext:
        if off + 4 > len(data):
            return None
        ext_len = struct.unpack_from(">H", data, off + 2)[0]
        off += 4 + ext_len * 4
        if off > len(data):
            return None

    return seq, ts, ssrc, marker, pt, data[off:]


def count_rtp_in_data(data: bytes) -> Tuple[int, int, Counter]:
    total_udp = 0
    total_rtp = 0
    counter = Counter()

    try:
        reader = make_pcap_reader(data)
    except Exception:
        return 0, 0, counter

    for _, buf in reader:
        try:
            ip = extract_ip_packet(buf)
            if ip is None:
                continue
            udp = ip.data
            if not isinstance(udp, dpkt.udp.UDP):
                continue

            total_udp += 1
            rtp = parse_rtp(udp.data)
            if rtp is None:
                continue

            seq, ts, ssrc, marker, pt, payload = rtp
            total_rtp += 1
            counter[(ssrc, pt, udp.sport, udp.dport)] += 1
        except Exception:
            continue

    return total_udp, total_rtp, counter


def choose_pcap_data(path: str, repair_mode: str) -> Tuple[bytes, str, int, int, Counter]:
    raw = Path(path).read_bytes()

    candidates: List[Tuple[str, bytes]] = []
    if repair_mode == "none":
        candidates = [("raw", raw)]
    elif repair_mode == "header":
        candidates = [("header", repair_pcap_headers(raw))]
    elif repair_mode == "all":
        candidates = [("all", repair_all_space_nulls(raw))]
    elif repair_mode == "auto":
        candidates = [
            ("raw", raw),
            ("header", repair_pcap_headers(raw)),
            ("all", repair_all_space_nulls(raw)),
        ]
    else:
        raise ValueError(f"Unknown repair mode: {repair_mode}")

    best = None
    for name, data in candidates:
        total_udp, total_rtp, counter = count_rtp_in_data(data)
        log(f"[PCAP] candidate={name} total_udp={total_udp} total_rtp={total_rtp}")
        score = total_rtp
        if best is None or score > best[0]:
            best = (score, name, data, total_udp, total_rtp, counter)

    if best is None:
        raise RuntimeError("No PCAP candidate could be parsed")

    _, name, data, total_udp, total_rtp, counter = best
    log(f"[PCAP] selected={name} total_udp={total_udp} total_rtp={total_rtp}")
    return data, name, total_udp, total_rtp, counter


def scan_pcap(path: str, repair_mode: str) -> None:
    data, selected, total_udp, total_rtp, counter = choose_pcap_data(path, repair_mode)
    log("[SCAN] Top RTP streams:")
    if not counter:
        log("[SCAN] No RTP stream found.")
        return

    for (ssrc, pt, sport, dport), n in counter.most_common(30):
        log(
            f"[SCAN] count={n:6d} "
            f"ssrc=0x{ssrc:08x} pt={pt:3d} "
            f"sport={sport:<5d} dport={dport:<5d}"
        )


def iter_rtp(
    pcap_data: bytes,
    ssrc_filter: Optional[int] = None,
    pt_filter: Optional[int] = None,
    udp_port_filter: Optional[int] = None,
) -> Iterator[RTPPacket]:
    reader = make_pcap_reader(pcap_data)

    for wall_ts, buf in reader:
        try:
            ip = extract_ip_packet(buf)
            if ip is None:
                continue

            udp = ip.data
            if not isinstance(udp, dpkt.udp.UDP):
                continue

            if udp_port_filter is not None:
                if udp.sport != udp_port_filter and udp.dport != udp_port_filter:
                    continue

            rtp = parse_rtp(udp.data)
            if rtp is None:
                continue

            seq, ts, ssrc, marker, pt, payload = rtp

            if ssrc_filter is not None and ssrc != ssrc_filter:
                continue
            if pt_filter is not None and pt != pt_filter:
                continue

            yield RTPPacket(
                wall_ts=wall_ts,
                seq=seq,
                rtp_ts=ts,
                ssrc=ssrc,
                marker=marker,
                pt=pt,
                payload=payload,
                udp_sport=udp.sport,
                udp_dport=udp.dport,
            )
        except Exception:
            continue


# =============================================================================
# RTP timestamp to H264 AU
# =============================================================================

def seq_sort_key(seq: int, base: int) -> int:
    return (seq - base) % SEQ_MOD


class RTPFrameAssembler:
    """
    Group RTP packets into one encoded video frame/access unit.
    Prefer RTP marker bit as end-of-frame.
    Also flush previous timestamp when timestamp changes.
    """
    def __init__(self):
        self.by_ts: Dict[int, List[Tuple[int, bytes]]] = defaultdict(list)
        self.current_ts: Optional[int] = None

    def push(self, pkt: RTPPacket) -> List[Tuple[int, List[Tuple[int, bytes]]]]:
        completed = []

        if self.current_ts is None:
            self.current_ts = pkt.rtp_ts

        # Timestamp changed before marker: flush old timestamp.
        if pkt.rtp_ts != self.current_ts:
            old = self.current_ts
            old_pkts = self._pop_sorted(old)
            if old_pkts:
                completed.append((old, old_pkts))
            self.current_ts = pkt.rtp_ts

        self.by_ts[pkt.rtp_ts].append((pkt.seq, pkt.payload))

        # Marker indicates end of access unit.
        if pkt.marker:
            done_ts = pkt.rtp_ts
            done_pkts = self._pop_sorted(done_ts)
            if done_pkts:
                completed.append((done_ts, done_pkts))
            if self.current_ts == done_ts:
                self.current_ts = None

        return completed

    def _pop_sorted(self, ts: int) -> List[Tuple[int, bytes]]:
        pkts = self.by_ts.pop(ts, [])
        if not pkts:
            return []
        base = pkts[0][0]
        return sorted(pkts, key=lambda x: seq_sort_key(x[0], base))

    def flush(self) -> List[Tuple[int, List[Tuple[int, bytes]]]]:
        completed = []
        for ts in list(self.by_ts.keys()):
            pkts = self._pop_sorted(ts)
            if pkts:
                completed.append((ts, pkts))
        self.current_ts = None
        return completed


class H264AUAssembler:
    """
    RTP/H264 packetization assembler:
    - Single NALU
    - STAP-A
    - FU-A

    This version is intentionally close to the notebook logic and not too strict
    on sequence gaps. It also caches SPS/PPS and prepends them to IDR frames.
    """
    def __init__(self):
        self.sps: Optional[bytes] = None
        self.pps: Optional[bytes] = None

    @staticmethod
    def nal_type(nal: bytes) -> int:
        return nal[0] & 0x1F if nal else -1

    def assemble(self, pkts: List[Tuple[int, bytes]]) -> Tuple[bytes, Dict[str, int]]:
        nalus: List[bytes] = []
        fua_buffer = bytearray()
        assembling = False

        stats = {
            "single": 0,
            "stap_a": 0,
            "fu_a": 0,
            "nalus": 0,
            "sps": 0,
            "pps": 0,
            "idr": 0,
            "unsupported": 0,
        }

        for seq, payload in pkts:
            if not payload:
                continue

            nt = payload[0] & 0x1F

            # Single NAL unit packet
            if 1 <= nt <= 23:
                nalus.append(payload)
                stats["single"] += 1

            # STAP-A
            elif nt == 24:
                stats["stap_a"] += 1
                pos = 1
                while pos + 2 <= len(payload):
                    size = struct.unpack_from(">H", payload, pos)[0]
                    pos += 2
                    if size <= 0 or pos + size > len(payload):
                        break
                    nalus.append(payload[pos:pos + size])
                    pos += size

            # FU-A
            elif nt == 28:
                stats["fu_a"] += 1
                if len(payload) < 2:
                    continue

                fu_header = payload[1]
                start = bool(fu_header & 0x80)
                end = bool(fu_header & 0x40)
                reconstructed_type = fu_header & 0x1F
                nri = payload[0] & 0x60
                nal_header = bytes([nri | reconstructed_type])

                if start:
                    fua_buffer = bytearray()
                    fua_buffer += nal_header
                    fua_buffer += payload[2:]
                    assembling = True
                elif assembling:
                    fua_buffer += payload[2:]

                if end and assembling:
                    nalus.append(bytes(fua_buffer))
                    assembling = False

            else:
                stats["unsupported"] += 1

        if not nalus:
            return b"", stats

        # Cache SPS/PPS
        for n in nalus:
            nt = self.nal_type(n)
            if nt == 7:
                self.sps = n
                stats["sps"] += 1
            elif nt == 8:
                self.pps = n
                stats["pps"] += 1
            elif nt == 5:
                stats["idr"] += 1

        # Prepend cached SPS/PPS to IDR if not already present.
        has_idr = any(self.nal_type(n) == 5 for n in nalus)
        has_sps = any(self.nal_type(n) == 7 for n in nalus)
        has_pps = any(self.nal_type(n) == 8 for n in nalus)

        final_nalus: List[bytes] = []
        if has_idr:
            if self.sps is not None and not has_sps:
                final_nalus.append(self.sps)
            if self.pps is not None and not has_pps:
                final_nalus.append(self.pps)

        final_nalus.extend(nalus)
        stats["nalus"] = len(final_nalus)

        return b"".join(START_CODE + n for n in final_nalus), stats


# =============================================================================
# Non-blocking ffmpeg decoder
# =============================================================================

class FFMpegH264Decoder:
    """
    Continuous H264 decoder with non-blocking stdout read.
    This prevents the script from silently hanging at stdout.read(frame_size).
    """
    def __init__(self, width: int, height: int, debug_ffmpeg: bool = False):
        self.width = width
        self.height = height
        self.frame_size = width * height * 3
        self.buf = bytearray()

        loglevel = "warning" if debug_ffmpeg else "error"

        self.proc = subprocess.Popen(
            [
                "ffmpeg",
                "-loglevel", loglevel,
                "-fflags", "nobuffer",
                "-flags", "low_delay",
                "-f", "h264",
                "-i", "-",
                "-f", "rawvideo",
                "-pix_fmt", "bgr24",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None if debug_ffmpeg else subprocess.DEVNULL,
            bufsize=0,
        )

        if self.proc.stdout is not None:
            os.set_blocking(self.proc.stdout.fileno(), False)

    def decode(self, annexb: bytes, timeout: float = 0.20) -> Optional[np.ndarray]:
        if not annexb:
            return None
        if self.proc.stdin is None or self.proc.stdout is None:
            return None

        try:
            self.proc.stdin.write(annexb)
            self.proc.stdin.flush()
        except BrokenPipeError:
            log("[DEC] ffmpeg stdin broken pipe")
            return None
        except Exception as e:
            log(f"[DEC] write error: {e}")
            return None

        deadline = time.time() + timeout

        while time.time() < deadline:
            # Already have enough bytes for a frame.
            if len(self.buf) >= self.frame_size:
                raw = bytes(self.buf[:self.frame_size])
                del self.buf[:self.frame_size]
                return np.frombuffer(raw, np.uint8).reshape((self.height, self.width, 3)).copy()

            remaining = max(0.0, deadline - time.time())
            rlist, _, _ = select.select([self.proc.stdout], [], [], min(0.02, remaining))
            if not rlist:
                continue

            try:
                chunk = self.proc.stdout.read(self.frame_size - len(self.buf))
            except BlockingIOError:
                continue
            except Exception as e:
                log(f"[DEC] read error: {e}")
                return None

            if not chunk:
                continue
            self.buf.extend(chunk)

        return None

    def close(self):
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass


# =============================================================================
# Mask + AI request + composition
# =============================================================================

def decode_rle(counts: List[int], width: int, height: int) -> np.ndarray:
    vals = []
    cur = 0
    for run in counts:
        run = int(run)
        if run > 0:
            vals.extend([cur] * run)
        cur = 1 - cur

    total = width * height
    if len(vals) < total:
        vals.extend([0] * (total - len(vals)))

    return np.asarray(vals[:total], dtype=np.float32).reshape((height, width))


def fit_cover(bg: np.ndarray, width: int, height: int) -> np.ndarray:
    scale = max(width / bg.shape[1], height / bg.shape[0])
    nw, nh = int(bg.shape[1] * scale), int(bg.shape[0] * scale)
    resized = cv2.resize(bg, (nw, nh))
    x0 = max(0, (nw - width) // 2)
    y0 = max(0, (nh - height) // 2)
    return resized[y0:y0 + height, x0:x0 + width]


def request_mask(
    url: str,
    session_id: str,
    frame_id: int,
    rtp_ts: int,
    frame: np.ndarray,
    infer_width: int,
    infer_height: int,
    effect_type: str,
    timeout: float,
) -> Optional[dict]:
    small = cv2.resize(frame, (infer_width, infer_height), interpolation=cv2.INTER_LINEAR)
    ok, enc = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if not ok:
        return None

    payload = {
        "session_id": session_id,
        "stream_id": "video_from_pcap",
        "frame_id": int(frame_id),
        "rtp_timestamp": int(rtp_ts),
        "original_width": int(frame.shape[1]),
        "original_height": int(frame.shape[0]),
        "inference_width": int(infer_width),
        "inference_height": int(infer_height),
        "effect_type": effect_type,
        "image_base64": base64.b64encode(enc.tobytes()).decode("ascii"),
    }

    endpoint = url.rstrip("/") + "/v1/video/segmentation"
    r = requests.post(endpoint, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def composite_background(frame: np.ndarray, bg: np.ndarray, mask: Optional[np.ndarray]) -> np.ndarray:
    if mask is None:
        return frame

    alpha = np.clip(mask, 0.0, 1.0)[..., None].astype(np.float32)
    out = alpha * frame.astype(np.float32) + (1.0 - alpha) * bg.astype(np.float32)
    return np.clip(out, 0, 255).astype(np.uint8)


# =============================================================================
# Main
# =============================================================================

def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--pcap", default="mf_video/input.txt")
    ap.add_argument("--width", type=int, required=False, default=240)
    ap.add_argument("--height", type=int, required=False, default=320)
    ap.add_argument("--fps", type=float, default=30.0)

    ap.add_argument("--logic-url", default="http://127.0.0.1:8080")
    ap.add_argument("--background", default="services/ai_engine/model_checkpoints/bg_image.jpg")
    ap.add_argument("--output", default="output_bg_replace.mp4")
    ap.add_argument("--session-id", default="CALL-PCAP-VIDEO-001")

    ap.add_argument("--infer-width", type=int, default=256)
    ap.add_argument("--infer-height", type=int, default=144)
    ap.add_argument("--infer-fps", type=int, default=10)
    ap.add_argument("--effect-type", default="bg_replace")
    ap.add_argument("--mask-smoothing-alpha", type=float, default=0.65)

    ap.add_argument("--ssrc", default=None)
    ap.add_argument("--pt", default=None)
    ap.add_argument("--udp-port", default=None)

    ap.add_argument(
        "--repair-mode",
        choices=["auto", "none", "header", "all"],
        default="auto",
        help="auto tries raw/header/all-space-null repairs and picks the one with most RTP packets.",
    )
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--max-frames", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--decode-timeout", type=float, default=0.25)
    ap.add_argument("--debug-ffmpeg", action="store_true")
    ap.add_argument("--verbose-every", type=int, default=30)

    args = ap.parse_args()

    log("[START] MF video PCAP prototype")
    log(f"[ARGS] pcap={args.pcap}")
    log(f"[ARGS] size={args.width}x{args.height} fps={args.fps}")
    log(f"[ARGS] logic_url={args.logic_url}")
    log(f"[ARGS] ssrc={args.ssrc} pt={args.pt} udp_port={args.udp_port}")
    log(f"[ARGS] repair_mode={args.repair_mode}")

    if args.scan_only:
        scan_pcap(args.pcap, args.repair_mode)
        return

    # Choose PCAP repair candidate
    pcap_data, selected_repair, total_udp, total_rtp, counter = choose_pcap_data(args.pcap, args.repair_mode)

    if total_rtp == 0:
        log("[ERROR] No RTP packets detected. Run with --scan-only and check PCAP format.")
        return

    log("[INFO] Top RTP streams:")
    for (ssrc, pt, sport, dport), n in counter.most_common(10):
        log(f"[INFO] count={n:6d} ssrc=0x{ssrc:08x} pt={pt:3d} sport={sport:<5d} dport={dport:<5d}")

    ssrc_filter = parse_int_auto(args.ssrc)
    pt_filter = parse_int_auto(args.pt)
    port_filter = parse_int_auto(args.udp_port)

    # Load background
    bg = cv2.imread(args.background)
    if bg is None:
        raise RuntimeError(f"Cannot read background image: {args.background}")
    bg = fit_cover(bg, args.width, args.height)
    log(f"[BG] loaded background shape={bg.shape}")

    # Prepare decoder/output
    decoder = FFMpegH264Decoder(args.width, args.height, debug_ffmpeg=args.debug_ffmpeg)
    rtp_assembler = RTPFrameAssembler()
    h264_assembler = H264AUAssembler()

    writer = cv2.VideoWriter(
        args.output,
        cv2.VideoWriter_fourcc(*"mp4v"),
        args.fps,
        (args.width, args.height),
    )
    if not writer.isOpened():
        raise RuntimeError(f"Cannot open output writer: {args.output}")

    infer_interval = max(1, int(round(args.fps / max(1, args.infer_fps))))
    latest_mask: Optional[np.ndarray] = None
    smooth_mask: Optional[np.ndarray] = None

    packets = 0
    completed_aus = 0
    decoded_frames = 0
    ai_requests = 0
    masks = 0
    empty_masks = 0
    decode_miss = 0
    annexb_empty = 0
    first_frame_sent = False
    start_time = time.time()

    try:
        for pkt in iter_rtp(pcap_data, ssrc_filter, pt_filter, port_filter):
            packets += 1

            if packets <= 10 or packets % max(1, args.verbose_every * 10) == 0:
                log(
                    f"[RTP] packets={packets} seq={pkt.seq} ts={pkt.rtp_ts} "
                    f"ssrc=0x{pkt.ssrc:08x} pt={pkt.pt} marker={pkt.marker} "
                    f"payload={len(pkt.payload)} port={pkt.udp_sport}->{pkt.udp_dport}"
                )

            completed = rtp_assembler.push(pkt)
            if not completed:
                continue

            for old_ts, pkts in completed:
                completed_aus += 1
                annexb, stats = h264_assembler.assemble(pkts)

                if not annexb:
                    annexb_empty += 1
                    if annexb_empty <= 10 or annexb_empty % 50 == 0:
                        log(
                            f"[AU] ts={old_ts} pkts={len(pkts)} annexb=0 "
                            f"stats={stats}"
                        )
                    continue

                if completed_aus <= 10 or completed_aus % args.verbose_every == 0:
                    log(
                        f"[AU] idx={completed_aus} ts={old_ts} pkts={len(pkts)} "
                        f"annexb={len(annexb)} stats={stats}"
                    )

                frame = decoder.decode(annexb, timeout=args.decode_timeout)
                if frame is None:
                    decode_miss += 1
                    if decode_miss <= 10 or decode_miss % 50 == 0:
                        log(
                            f"[DEC] no frame yet miss={decode_miss} "
                            f"au={completed_aus} ts={old_ts} "
                            f"hint='check width/height, SPS/PPS/IDR, or try --debug-ffmpeg'"
                        )
                    continue

                decoded_frames += 1

                if decoded_frames == 1:
                    log(f"[DEC] first frame decoded shape={frame.shape}")

                should_infer = (
                    not args.no_ai and
                    (decoded_frames == 1 or decoded_frames % infer_interval == 0)
                )

                if should_infer:
                    try:
                        ai_requests += 1
                        log(f"[AI] send frame={decoded_frames} ts={old_ts} to {args.logic_url}")

                        resp = request_mask(
                            args.logic_url,
                            args.session_id,
                            decoded_frames,
                            old_ts,
                            frame,
                            args.infer_width,
                            args.infer_height,
                            args.effect_type,
                            args.timeout,
                        )

                        status = resp.get("status")
                        rle = resp.get("rle_counts", [])
                        log(f"[AI] resp status={status} rle_runs={len(rle)} latency_ms={resp.get('latency_ms')}")

                        if status == "ok" and rle:
                            mask_small = decode_rle(rle, int(resp["mask_width"]), int(resp["mask_height"]))

                            if len(rle) == 1:
                                empty_masks += 1

                            mask = cv2.resize(mask_small, (args.width, args.height), interpolation=cv2.INTER_LINEAR)
                            mask = cv2.GaussianBlur(mask, (15, 15), 0)
                            mask = np.clip(mask, 0.0, 1.0)

                            if smooth_mask is None:
                                smooth_mask = mask
                            else:
                                a = float(args.mask_smoothing_alpha)
                                smooth_mask = a * smooth_mask + (1.0 - a) * mask

                            latest_mask = smooth_mask
                            masks += 1
                        else:
                            log(f"[WARN] AI result not usable: {resp}")

                    except Exception as exc:
                        log(f"[WARN] AI request failed: {exc}")

                out = composite_background(frame, bg, latest_mask)

                if not out.flags.writeable:
                    out = out.copy()
                cv2.putText(
                    out,
                    f"MCF remote | frames={decoded_frames} masks={masks}",
                    (12, 28),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    (0, 255, 0),
                    2,
                )
                writer.write(out)
                first_frame_sent = True

                if decoded_frames % max(1, args.verbose_every) == 0:
                    dt = max(1e-6, time.time() - start_time)
                    log(
                        f"[MF-TEST] packets={packets} aus={completed_aus} "
                        f"frames={decoded_frames} ai_req={ai_requests} masks={masks} "
                        f"decode_miss={decode_miss} avg_fps={decoded_frames / dt:.2f}"
                    )

                if args.max_frames > 0 and decoded_frames >= args.max_frames:
                    log(f"[STOP] reached max_frames={args.max_frames}")
                    raise StopIteration

    except StopIteration:
        pass
    finally:
        writer.release()
        decoder.close()

    dt = max(1e-6, time.time() - start_time)
    log(
        f"[DONE] packets={packets} aus={completed_aus} frames={decoded_frames} "
        f"ai_req={ai_requests} masks={masks} empty_masks={empty_masks} "
        f"annexb_empty={annexb_empty} decode_miss={decode_miss} "
        f"elapsed={dt:.2f}s output={args.output}"
    )

    if decoded_frames == 0:
        log("[HINT] No decoded frame. Try:")
        log("       1) --scan-only to find correct SSRC/PT")
        log("       2) add --pt <payload_type>")
        log("       3) swap --width/--height")
        log("       4) add --debug-ffmpeg")
        log("       5) ensure PCAP contains H264 RTP with SPS/PPS/IDR")
    elif ai_requests == 0 and not args.no_ai:
        log("[HINT] Frames decoded but no AI requests. Check --infer-fps or script logic.")
    elif masks == 0 and not args.no_ai:
        log("[HINT] AI requests sent but no usable masks. Check AI Engine logs and segmentation response.")


if __name__ == "__main__":
    main()

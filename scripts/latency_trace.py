#!/usr/bin/env python3
"""latency_trace.py -- join LATENCY_TRACE log lines across mcf-server (Go),
ai_server.py (Python), and mf_cpp (C++) by frame_id (falling back to
rtp_ts), and print a per-stage + end-to-end latency breakdown.

See rtpgw_design_v3.md section 36 for where each checkpoint is logged:

  T0 rtpgw_au_assembled   (internal to RTPGW, not logged -- see DecodeLatency)
  T1 rtpgw_decoded        mcf-server stdout/logs/logic_service.log
  T2 rtpgw_dispatch       mcf-server stdout/logs/logic_service.log
  T3 ai_received          ai_server.py stdout
  T4 ai_submit            ai_server.py stdout
  T5 ai_result            ai_server.py stdout
  T6 ai_push              ai_server.py stdout
  T7 mf_received          mf_cpp stdout

Usage:
    python3 scripts/latency_trace.py rtpgw.log ai_engine.log mf_cpp.log
    # or pipe everything through one combined log:
    cat *.log | python3 scripts/latency_trace.py -

Caveat: cross-process deltas (e.g. rtpgw_dispatch -> ai_received,
ai_push -> mf_received) are only meaningful if all three processes' clocks
are the same/NTP-synced -- true by default for a single-host pcap-replay
test, not guaranteed once these run on separate machines.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from statistics import mean, median

STAGE_ORDER = [
    "rtpgw_decoded",
    "rtpgw_dispatch",
    "ai_received",
    "ai_submit",
    "ai_result",
    "ai_push",
    "mf_received",
]

# Matches both `key=value` (Python/C++ log lines) and `"key": value`
# (Go zap console/JSON encoder) tokens on the same line.
KV_RE = re.compile(r'"?([a-zA-Z_]+)"?\s*[:=]\s*"?(-?\d+(?:\.\d+)?)"?')
STAGE_RE = re.compile(r'stage["\s:=]+([a-zA-Z_]+)')


def parse_line(line: str):
    if "LATENCY_TRACE" not in line:
        return None
    stage_match = STAGE_RE.search(line)
    if not stage_match:
        return None
    stage = stage_match.group(1)
    if stage not in STAGE_ORDER:
        return None

    fields = {}
    for key, val in KV_RE.findall(line):
        if key == "stage":
            continue
        try:
            fields[key] = int(val) if "." not in val else float(val)
        except ValueError:
            continue
    return stage, fields


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1

    # frame_id or ("rtp_ts", value) -> {stage: ts_ms}
    events: dict[object, dict[str, float]] = defaultdict(dict)

    def read_lines(fh):
        for line in fh:
            parsed = parse_line(line)
            if parsed is None:
                continue
            stage, fields = parsed
            ts_ms = fields.get("ts_ms")
            if ts_ms is None:
                continue
            frame_id = fields.get("frame_id")
            key = ("frame", frame_id) if frame_id else ("rtp_ts", fields.get("rtp_ts"))
            if key[1] is None:
                continue
            events[key][stage] = ts_ms

    for path in argv:
        if path == "-":
            read_lines(sys.stdin)
        else:
            with open(path, "r", errors="replace") as fh:
                read_lines(fh)

    if not events:
        print("No LATENCY_TRACE lines found across the given files.")
        return 0

    stage_deltas: dict[str, list[float]] = defaultdict(list)
    total_deltas: list[float] = []

    print(f"{'key':<22} " + " ".join(f"{s:>16}" for s in STAGE_ORDER) + f" {'total_ms':>10}")
    for key, stages in sorted(events.items(), key=lambda kv: min(kv[1].values())):
        row = [""] * len(STAGE_ORDER)
        prev_stage, prev_ts = None, None
        first_ts, last_ts = None, None
        for i, stage in enumerate(STAGE_ORDER):
            ts = stages.get(stage)
            if ts is None:
                continue
            if first_ts is None:
                first_ts = ts
            last_ts = ts
            if prev_ts is not None:
                delta = ts - prev_ts
                stage_deltas[f"{prev_stage}->{stage}"].append(delta)
                row[i] = f"+{delta:.0f}ms"
            else:
                row[i] = "0ms"
            prev_stage, prev_ts = stage, ts

        total = (last_ts - first_ts) if (first_ts is not None and last_ts is not None) else None
        if total is not None and total > 0:
            total_deltas.append(total)

        key_str = f"{key[0]}={key[1]}"
        total_str = f"{total:.0f}" if total is not None else "-"
        print(f"{key_str:<22} " + " ".join(f"{c:>16}" for c in row) + f" {total_str:>10}")

    print("\n--- summary (ms) ---")
    for name, values in stage_deltas.items():
        if not values:
            continue
        print(f"{name:<30} n={len(values):<5} "
              f"avg={mean(values):8.1f} p50={median(values):8.1f} "
              f"min={min(values):8.1f} max={max(values):8.1f}")
    if total_deltas:
        print(f"{'END-TO-END TOTAL':<30} n={len(total_deltas):<5} "
              f"avg={mean(total_deltas):8.1f} p50={median(total_deltas):8.1f} "
              f"min={min(total_deltas):8.1f} max={max(total_deltas):8.1f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

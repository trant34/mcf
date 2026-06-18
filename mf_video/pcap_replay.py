#!/usr/bin/env python3
from __future__ import annotations
import argparse, io, socket, time
from pathlib import Path
import dpkt

def open_pcap(path: str):
    raw = Path(path).read_bytes()
    for cls in (dpkt.pcap.Reader, dpkt.pcapng.Reader):
        try:
            return cls(io.BytesIO(raw))
        except Exception:
            pass
    raise RuntimeError(f"Cannot parse PCAP/PCAPNG: {path}")

def extract_ip_packet(buf: bytes):
    try:
        eth = dpkt.ethernet.Ethernet(buf)
        if isinstance(eth.data, (dpkt.ip.IP, dpkt.ip6.IP6)):
            return eth.data
    except Exception:
        pass
    try:
        sll = dpkt.sll.SLL(buf)
        if isinstance(sll.data, (dpkt.ip.IP, dpkt.ip6.IP6)):
            return sll.data
    except Exception:
        pass
    try:
        if buf and (buf[0] >> 4) == 4:
            return dpkt.ip.IP(buf)
        if buf and (buf[0] >> 4) == 6:
            return dpkt.ip6.IP6(buf)
    except Exception:
        pass
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pcap', required=True)
    ap.add_argument('--target-host', default='127.0.0.1')
    ap.add_argument('--target-port', type=int, default=5006)
    ap.add_argument('--speed', type=float, default=1.0)
    ap.add_argument('--src-port', type=int)
    ap.add_argument('--dst-port', type=int)
    args = ap.parse_args()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    reader = open_pcap(args.pcap)
    first_wall = None
    start = None
    sent = 0
    for wall_ts, buf in reader:
        ip = extract_ip_packet(buf)
        if ip is None or not isinstance(ip.data, dpkt.udp.UDP):
            continue
        udp = ip.data
        if args.src_port is not None and udp.sport != args.src_port:
            continue
        if args.dst_port is not None and udp.dport != args.dst_port:
            continue
        if first_wall is None:
            first_wall = wall_ts
            start = time.perf_counter()
        if args.speed > 0:
            target = start + (wall_ts - first_wall) / args.speed
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
        sock.sendto(bytes(udp.data), (args.target_host, args.target_port))
        sent += 1
        if sent <= 10 or sent % 100 == 0:
            print(f'[REPLAY] sent={sent} src={udp.sport} dst={udp.dport} bytes={len(udp.data)}', flush=True)
    print(f'[REPLAY DONE] sent={sent}', flush=True)

if __name__ == '__main__':
    main()

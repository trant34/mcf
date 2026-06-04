import socket 
import time 

try:
    from scapy.all import rdpcap, UDP
except ImportError:
    from scapy import rdpcap

def replay_pcap(pcap_path, target_ip="127.0.0.1", target_port=5004):
    print(f"[PCAP Replay] Reading file: {pcap_path}...")
    try: 
        packets = rdpcap(pcap_path)
    except Exception as e: 
        print(f"[PCAP Replay] Reading PCAP error: {e}")
        return

    sender_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[PCAP Replay] Start streaming RTP to {target_ip}:{target_port}...")

    last_pkt_time = None 
    packet_count = 0 

    for pkt in packets: 
        # only fetch UDP
        if pkt.haslayer(UDP):
            current_pkt_time = float(pkt.time)

            if last_pkt_time is not None: 
                sleep_time = current_pkt_time - last_pkt_time
                if sleep_time > 0: 
                    time.sleep(sleep_time)

            last_pkt_time = current_pkt_time
            raw_payload = bytes(pkt[UDP].payload)

            if len(raw_payload) > 0: 
                sender_sock.sendto(raw_payload, (target_ip, target_port))
                packet_count += 1

                if packet_count % 100 == 0: 
                    print(f"[PCAP Replay] Sent {packet_count} packets...", flush=True)

    print(f"[PCAP Replay] Completely sending {packet_count} packets")
    sender_sock.close()

if __name__ == "__main__": 
    PCAP_FILE = r"C:\Users\IMS-DPT\Desktop\internet_trouble.pcap"

    replay_pcap(PCAP_FILE, "127.0.0.1", 5004)





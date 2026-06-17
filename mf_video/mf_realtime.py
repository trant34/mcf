#!/usr/bin/env python3
from __future__ import annotations
import argparse, base64, queue, socket, struct, subprocess, threading, time
from dataclasses import dataclass
from typing import Optional
import cv2, numpy as np, requests

START_CODE=b'\x00\x00\x00\x01'; SEQ_MOD=65536

def log(s): print(s, flush=True)
def parse_int_auto(x):
    if x is None: return None
    s=str(x); return int(s,16) if s.lower().startswith('0x') else int(s)

@dataclass
class RTPPacket:
    seq:int; ts:int; ssrc:int; marker:int; pt:int; payload:bytes

def parse_rtp(data:bytes)->Optional[RTPPacket]:
    if len(data)<12 or (data[0]>>6)!=2: return None
    cc=data[0]&0x0f; ext=(data[0]>>4)&1; marker=(data[1]>>7)&1; pt=data[1]&0x7f
    seq=struct.unpack_from('>H',data,2)[0]; ts=struct.unpack_from('>I',data,4)[0]; ssrc=struct.unpack_from('>I',data,8)[0]
    off=12+cc*4
    if off>len(data): return None
    if ext:
        if off+4>len(data): return None
        ext_len=struct.unpack_from('>H',data,off+2)[0]; off+=4+ext_len*4
        if off>len(data): return None
    return RTPPacket(seq,ts,ssrc,marker,pt,data[off:])

class FrameAssembler:
    def __init__(self): self.ts=None; self.pkts=[]
    def push(self,p):
        out=[]
        if self.ts is None: self.ts=p.ts
        if p.ts!=self.ts:
            if self.pkts: out.append((self.ts,self._sort(self.pkts)))
            self.ts=p.ts; self.pkts=[]
        self.pkts.append((p.seq,p.payload))
        if p.marker:
            out.append((p.ts,self._sort(self.pkts))); self.ts=None; self.pkts=[]
        return out
    def _sort(self,pkts):
        base=pkts[0][0]; return sorted(pkts,key=lambda x:(x[0]-base)%SEQ_MOD)

class H264Assembler:
    def __init__(self): self.sps=None; self.pps=None
    def assemble(self,pkts):
        nalus=[]; fua=bytearray(); assembling=False
        for seq,payload in pkts:
            if not payload: continue
            nt=payload[0]&0x1f
            if 1<=nt<=23: nalus.append(payload)
            elif nt==24:
                pos=1
                while pos+2<=len(payload):
                    size=struct.unpack_from('>H',payload,pos)[0]; pos+=2
                    if size<=0 or pos+size>len(payload): break
                    nalus.append(payload[pos:pos+size]); pos+=size
            elif nt==28 and len(payload)>=2:
                fh=payload[1]; start=bool(fh&0x80); end=bool(fh&0x40); rtype=fh&0x1f; nri=payload[0]&0x60
                if start:
                    fua=bytearray([nri|rtype]); fua.extend(payload[2:]); assembling=True
                elif assembling: fua.extend(payload[2:])
                if end and assembling:
                    nalus.append(bytes(fua)); assembling=False
        if not nalus: return b''
        for n in nalus:
            t=n[0]&0x1f
            if t==7: self.sps=n
            elif t==8: self.pps=n
        has_idr=any((n[0]&0x1f)==5 for n in nalus)
        has_sps=any((n[0]&0x1f)==7 for n in nalus)
        has_pps=any((n[0]&0x1f)==8 for n in nalus)
        final=[]
        if has_idr:
            if self.sps is not None and not has_sps: final.append(self.sps)
            if self.pps is not None and not has_pps: final.append(self.pps)
        final.extend(nalus)
        return b''.join(START_CODE+n for n in final)

class LatestQueue:
    def __init__(self,n=1): self.q=queue.Queue(maxsize=n)
    def put_latest(self,x):
        while True:
            try: self.q.put_nowait(x); return
            except queue.Full:
                try: self.q.get_nowait()
                except queue.Empty: pass
    def get(self,timeout=None): return self.q.get(timeout=timeout)

# class Decoder:
#     def __init__(self,w,h,debug=False):
#         self.w=w; self.h=h; self.n=w*h*3; self.frames=LatestQueue(5); self.running=True
#         self.p=subprocess.Popen(['ffmpeg','-loglevel','warning' if debug else 'error','-fflags','nobuffer','-flags','low_delay','-probesize','32','-analyzeduration','0','-f','h264','-i','pipe:0','-an','-f','rawvideo','-pix_fmt','bgr24','pipe:1'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=None if debug else subprocess.DEVNULL,bufsize=0)
#         threading.Thread(target=self._reader,daemon=True).start()
#     def feed(self,b):
#         if not b: return
#         try: self.p.stdin.write(b); self.p.stdin.flush()
#         except Exception as e: log(f'[DEC] feed error: {e}')
#     def _reader(self):
#         while self.running:
#             raw=self.p.stdout.read(self.n)
#             if len(raw)!=self.n:
#                 if not self.running: break
#                 time.sleep(0.005); continue
#             f=np.frombuffer(raw,np.uint8).reshape((self.h,self.w,3)).copy()
#             self.frames.put_latest(f)
#     def close(self):
#         self.running=False
#         try: self.p.stdin.close()
#         except Exception: pass
#         try: self.p.terminate()
#         except Exception: pass

class Decoder:
    def __init__(self, w, h, debug=False):
        self.w = w
        self.h = h
        self.frame_size = w * h * 3

        self.frames = LatestQueue(5)
        self.running = True
        self._buffer = bytearray()

        cmd = [
            "ffmpeg",
            "-loglevel", "warning" if debug else "error",

            # H264 input from stdin
            "-f", "h264",

            # Give FFmpeg enough data to initialize decoder
            "-probesize", "1M",
            "-analyzeduration", "1M",

            "-i", "pipe:0",

            "-an",

            # Do not duplicate/drop frames based on timestamps
            "-vsync", "0",

            # Raw BGR output
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "pipe:1",
        ]

        self.p = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None if debug else subprocess.DEVNULL,
            bufsize=0,
        )

        self.reader_thread = threading.Thread(
            target=self._reader,
            daemon=True,
        )
        self.reader_thread.start()

    def feed(self, annexb):
        if not annexb or self.p.stdin is None:
            return

        try:
            self.p.stdin.write(annexb)
            self.p.stdin.flush()

        except BrokenPipeError:
            log("[DEC] ffmpeg stdin broken pipe")

        except Exception as exc:
            log(f"[DEC] feed error: {exc}")

    def _reader(self):
        """
        Accumulate partial stdout chunks until one or more complete raw frames
        are available.
        """
        read_size = 64 * 1024

        while self.running and self.p.stdout is not None:
            try:
                chunk = self.p.stdout.read(read_size)

            except Exception as exc:
                log(f"[DEC] stdout read error: {exc}")
                break

            if not chunk:
                if self.p.poll() is not None:
                    log(
                        f"[DEC] ffmpeg exited "
                        f"returncode={self.p.returncode}"
                    )
                    break

                time.sleep(0.005)
                continue

            self._buffer.extend(chunk)

            while len(self._buffer) >= self.frame_size:
                raw = bytes(self._buffer[:self.frame_size])
                del self._buffer[:self.frame_size]

                frame = np.frombuffer(
                    raw,
                    dtype=np.uint8,
                ).reshape(
                    self.h,
                    self.w,
                    3,
                ).copy()

                self.frames.put_latest(
                    (time.perf_counter(), frame)
                )

    def close(self):
        self.running = False

        # Signal EOF so FFmpeg can flush delayed frames
        try:
            if self.p.stdin is not None:
                self.p.stdin.close()
        except Exception:
            pass

        try:
            self.p.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                self.p.terminate()
            except Exception:
                pass

        try:
            self.reader_thread.join(timeout=1.0)
        except Exception:
            pass

def decode_rle(counts,w,h):
    vals=[]; cur=0
    for run in counts:
        vals.extend([cur]*int(run)); cur=1-cur
    total=w*h
    if len(vals)<total: vals.extend([0]*(total-len(vals)))
    return np.asarray(vals[:total],dtype=np.float32).reshape((h,w))

def fit_cover(bg,w,h):
    scale=max(w/bg.shape[1],h/bg.shape[0]); nw,nh=int(bg.shape[1]*scale),int(bg.shape[0]*scale)
    r=cv2.resize(bg,(nw,nh)); x=max(0,(nw-w)//2); y=max(0,(nh-h)//2); return r[y:y+h,x:x+w]

def request_mask(url,sid,fid,ts,frame,iw,ih,effect,timeout):
    small=cv2.resize(frame,(iw,ih)); ok,enc=cv2.imencode('.jpg',small,[int(cv2.IMWRITE_JPEG_QUALITY),80])
    if not ok: raise RuntimeError('jpeg encode failed')
    payload={'session_id':sid,'stream_id':'video_realtime','frame_id':fid,'rtp_timestamp':ts,'original_width':frame.shape[1],'original_height':frame.shape[0],'inference_width':iw,'inference_height':ih,'effect_type':effect,'image_base64':base64.b64encode(enc.tobytes()).decode('ascii')}
    r=requests.post(url.rstrip('/')+'/v1/video/segmentation',json=payload,timeout=timeout); r.raise_for_status(); return r.json()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--listen-host',default='0.0.0.0'); ap.add_argument('--listen-port',type=int,default=5006)
    ap.add_argument('--width',type=int,default=240); ap.add_argument('--height',type=int,default=320); ap.add_argument('--fps',type=float,default=15)
    ap.add_argument('--infer-fps',type=float,default=5); ap.add_argument('--infer-width',type=int,default=256); ap.add_argument('--infer-height',type=int,default=144)
    ap.add_argument('--logic-url',default='http://127.0.0.1:8080'); ap.add_argument('--background',required=True); ap.add_argument('--output',default='output_realtime.mp4')
    ap.add_argument('--session-id',default='CALL-PCAP-REALTIME-001'); ap.add_argument('--effect-type',default='bg_replace'); ap.add_argument('--ssrc'); ap.add_argument('--pt',type=int)
    ap.add_argument('--timeout',type=float,default=5); ap.add_argument('--debug-ffmpeg',action='store_true'); ap.add_argument('--duration',type=float,default=0)
    args=ap.parse_args(); ssrc_filter=parse_int_auto(args.ssrc)
    bg=cv2.imread(args.background)
    if bg is None: raise RuntimeError('cannot read background')
    bg=fit_cover(bg,args.width,args.height)
    writer=cv2.VideoWriter(args.output,cv2.VideoWriter_fourcc(*'mp4v'),args.fps,(args.width,args.height))
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); sock.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,4*1024*1024); sock.bind((args.listen_host,args.listen_port)); sock.settimeout(0.5)
    asm=FrameAssembler(); h264=H264Assembler(); dec=Decoder(args.width,args.height,args.debug_ffmpeg)
    infer_q=LatestQueue(1); latest={'mask':None}; lock=threading.Lock(); running=True
    metrics={'packets':0,'aus':0,'frames':0,'ai':0,'masks':0}
    def infer_worker():
        smooth=None
        while running:
            try: fid,ts,frame=infer_q.get(timeout=0.5)
            except queue.Empty: continue
            t0=time.perf_counter()
            try:
                metrics['ai']+=1; resp=request_mask(args.logic_url,args.session_id,fid,ts,frame,args.infer_width,args.infer_height,args.effect_type,args.timeout)
                if resp.get('status')!='ok' or not resp.get('rle_counts'): log(f'[AI] unusable: {resp}'); continue
                m=decode_rle(resp['rle_counts'],resp['mask_width'],resp['mask_height']); m=cv2.resize(m,(args.width,args.height)); m=cv2.GaussianBlur(m,(15,15),0); m=np.clip(m,0,1)
                smooth=m if smooth is None else 0.65*smooth+0.35*m
                with lock: latest['mask']=smooth.copy()
                metrics['masks']+=1
                log(f"[AI] frame={fid} status=ok rle_runs={len(resp['rle_counts'])} roundtrip_ms={(time.perf_counter()-t0)*1000:.1f}")
            except Exception as e: log(f'[AI] failed frame={fid}: {e}')
    threading.Thread(target=infer_worker,daemon=True).start()
    log(f'[MF] listening on {args.listen-host if False else args.listen_host}:{args.listen_port}')
    frame_id=0; last_infer=0; start=time.perf_counter(); last_log=start; last_ts=0
    try:
        while True:
            now=time.perf_counter()
            if args.duration>0 and now-start>=args.duration: break
            try: data,_=sock.recvfrom(65535)
            except socket.timeout: data=None
            if data:
                p=parse_rtp(data)
                if p is not None:
                    if ssrc_filter is not None and p.ssrc!=ssrc_filter: continue
                    if args.pt is not None and p.pt!=args.pt: continue
                    metrics['packets']+=1; last_ts=p.ts
                    for ts,pkts in asm.push(p):
                        b=h264.assemble(pkts)
                        if b: metrics['aus']+=1; dec.feed(b)
            try: frame=dec.frames.get(timeout=0.001)
            except queue.Empty: frame=None
            if frame is not None:
                frame_id+=1; metrics['frames']+=1
                if now-last_infer>=1.0/max(args.infer_fps,0.1): infer_q.put_latest((frame_id,last_ts,frame.copy())); last_infer=now
                with lock: m=None if latest['mask'] is None else latest['mask'].copy()
                if m is None: out=frame.copy()
                else:
                    a=m[...,None].astype(np.float32); out=np.clip(a*frame.astype(np.float32)+(1-a)*bg.astype(np.float32),0,255).astype(np.uint8)
                cv2.putText(out,f'Realtime PCAP | frame={frame_id} masks={metrics["masks"]}',(12,28),cv2.FONT_HERSHEY_SIMPLEX,0.55,(0,255,0),2); writer.write(out)
            if now-last_log>=1:
                elapsed=max(1e-6,now-start); log(f"[METRIC] packets={metrics['packets']} aus={metrics['aus']} frames={metrics['frames']} ai={metrics['ai']} masks={metrics['masks']} fps={metrics['frames']/elapsed:.2f}"); last_log=now
    except KeyboardInterrupt: pass
    finally:
        running=False; dec.close(); writer.release(); sock.close()
    log(f"[DONE] packets={metrics['packets']} aus={metrics['aus']} frames={metrics['frames']} ai={metrics['ai']} masks={metrics['masks']} output={args.output}")

if __name__=='__main__': main()

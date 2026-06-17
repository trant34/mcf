package video

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"fmt"
	"image"
	"image/color"
	"image/jpeg"
	"io"
	"os"
	"os/exec"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"mcf/services/logic/internal/ai_client"
	"mcf/services/logic/internal/gateway"

	"go.uber.org/zap"
)

var startCode = []byte{0, 0, 0, 1}

type Config struct {
	Width, Height           int
	FPS, InferFPS           float64
	InferWidth, InferHeight int
	JPEGQuality             int
	PayloadType             uint8
	SSRC                    uint32
	Threshold               float32
	EffectType              string
	FFmpegBinary            string
	BackgroundPath          string
	RTPOutputURL            string
}

type Processor struct {
	cfg                      Config
	logger                   *zap.Logger
	decoder                  *ffmpegDecoder
	encoder                  *ffmpegEncoder
	assembler                *auAssembler
	depacketizer             *h264Depacketizer
	frameSeq                 atomic.Int64
	latestMaskMu             sync.RWMutex
	latestMask               []float32
	latestMaskW, latestMaskH int
	background               []byte
	metrics                  Metrics
}

type Metrics struct {
	Packets          atomic.Uint64
	AUs              atomic.Uint64
	Frames           atomic.Uint64
	InferenceFrames  atomic.Uint64
	Masks            atomic.Uint64
	DroppedInference atomic.Uint64
}

func NewProcessor(cfg Config, logger *zap.Logger) (*Processor, error) {
	if cfg.FFmpegBinary == "" {
		cfg.FFmpegBinary = "ffmpeg"
	}
	if cfg.JPEGQuality <= 0 {
		cfg.JPEGQuality = 80
	}
	if cfg.FPS <= 0 {
		cfg.FPS = 15
	}
	if cfg.InferFPS <= 0 {
		cfg.InferFPS = 5
	}
	p := &Processor{cfg: cfg, logger: logger, assembler: newAUAssembler(), depacketizer: newH264Depacketizer()}
	if cfg.BackgroundPath != "" {
		bg, err := loadBackgroundBGR(cfg.BackgroundPath, cfg.Width, cfg.Height)
		if err != nil {
			return nil, err
		}
		p.background = bg
	}
	dec, err := newFFmpegDecoder(cfg.FFmpegBinary, cfg.Width, cfg.Height, logger)
	if err != nil {
		return nil, err
	}
	p.decoder = dec
	if cfg.RTPOutputURL != "" {
		enc, err := newFFmpegEncoder(cfg.FFmpegBinary, cfg.Width, cfg.Height, cfg.FPS, cfg.RTPOutputURL, logger)
		if err != nil {
			dec.Close()
			return nil, err
		}
		p.encoder = enc
	}
	return p, nil
}

func (p *Processor) Run(ctx context.Context, packetCh <-chan gateway.VideoRTPPacket, frameCh chan ai_client.VideoFrameInput, maskCh <-chan ai_client.VideoMaskResult) error {
	defer p.Close()
	go p.maskLoop(ctx, maskCh)
	go p.frameLoop(ctx, frameCh)

	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			p.logger.Info("video metrics",
				zap.Uint64("packets", p.metrics.Packets.Load()),
				zap.Uint64("aus", p.metrics.AUs.Load()),
				zap.Uint64("frames", p.metrics.Frames.Load()),
				zap.Uint64("inference_frames", p.metrics.InferenceFrames.Load()),
				zap.Uint64("masks", p.metrics.Masks.Load()),
				zap.Uint64("dropped_inference", p.metrics.DroppedInference.Load()))
		case pkt, ok := <-packetCh:
			if !ok {
				return nil
			}
			if p.cfg.PayloadType != 0 && pkt.PayloadType != p.cfg.PayloadType {
				continue
			}
			if p.cfg.SSRC != 0 && pkt.SSRC != p.cfg.SSRC {
				continue
			}
			p.metrics.Packets.Add(1)
			groups := p.assembler.Push(pkt)
			for _, group := range groups {
				annexb := p.depacketizer.Assemble(group)
				if len(annexb) == 0 {
					continue
				}
				p.metrics.AUs.Add(1)
				if err := p.decoder.Feed(annexb, group.Timestamp); err != nil {
					p.logger.Warn("decoder feed failed", zap.Error(err))
				}
			}
		}
	}
}

func (p *Processor) frameLoop(ctx context.Context, frameCh chan ai_client.VideoFrameInput) {
	interval := time.Duration(float64(time.Second) / p.cfg.InferFPS)
	lastInfer := time.Time{}
	for {
		select {
		case <-ctx.Done():
			return
		case f, ok := <-p.decoder.Frames():
			if !ok {
				return
			}
			p.metrics.Frames.Add(1)
			frameID := p.frameSeq.Add(1)
			out := p.compose(f.BGR)
			if p.encoder != nil {
				if err := p.encoder.Feed(out); err != nil {
					p.logger.Warn("encoder feed failed", zap.Error(err))
				}
			}
			now := time.Now()
			if lastInfer.IsZero() || now.Sub(lastInfer) >= interval {
				jpegBytes, err := encodeJPEGResize(f.BGR, p.cfg.Width, p.cfg.Height, p.cfg.InferWidth, p.cfg.InferHeight, p.cfg.JPEGQuality)
				if err == nil {
					item := ai_client.VideoFrameInput{SessionID: "", FrameID: frameID, RTPTimestamp: int64(f.RTPTimestamp), OriginalWidth: int32(p.cfg.Width), OriginalHeight: int32(p.cfg.Height), Encoding: "jpeg", ImageData: jpegBytes}
					select {
					case frameCh <- item:
						p.metrics.InferenceFrames.Add(1)
					default:
						select {
						case <-frameCh:
						default:
						}
						select {
						case frameCh <- item:
							p.metrics.InferenceFrames.Add(1)
						default:
							p.metrics.DroppedInference.Add(1)
						}
					}
					lastInfer = now
				}
			}
		}
	}
}

func (p *Processor) maskLoop(ctx context.Context, maskCh <-chan ai_client.VideoMaskResult) {
	for {
		select {
		case <-ctx.Done():
			return
		case m, ok := <-maskCh:
			if !ok {
				return
			}
			if m.Status != "ok" || len(m.RLECounts) == 0 {
				continue
			}
			mask := decodeRLE(m.RLECounts, int(m.MaskWidth), int(m.MaskHeight))
			p.latestMaskMu.Lock()
			p.latestMask, p.latestMaskW, p.latestMaskH = mask, int(m.MaskWidth), int(m.MaskHeight)
			p.latestMaskMu.Unlock()
			p.metrics.Masks.Add(1)
		}
	}
}

func (p *Processor) compose(frame []byte) []byte {
	p.latestMaskMu.RLock()
	mask := append([]float32(nil), p.latestMask...)
	mw, mh := p.latestMaskW, p.latestMaskH
	p.latestMaskMu.RUnlock()
	if len(mask) == 0 {
		return append([]byte(nil), frame...)
	}

	var background []byte
	switch p.cfg.EffectType {
	case "bg_blur":
		background = boxBlurBGR(frame, p.cfg.Width, p.cfg.Height, 7)
	case "bg_remove":
		background = make([]byte, len(frame))
	default:
		if len(p.background) != len(frame) {
			return append([]byte(nil), frame...)
		}
		background = p.background
	}

	out := make([]byte, len(frame))
	for y := 0; y < p.cfg.Height; y++ {
		sy := y * mh / p.cfg.Height
		for x := 0; x < p.cfg.Width; x++ {
			sx := x * mw / p.cfg.Width
			a := mask[sy*mw+sx]
			if a < 0 {
				a = 0
			}
			if a > 1 {
				a = 1
			}
			idx := (y*p.cfg.Width + x) * 3
			for c := 0; c < 3; c++ {
				out[idx+c] = byte(a*float32(frame[idx+c]) + (1-a)*float32(background[idx+c]))
			}
		}
	}
	return out
}

func boxBlurBGR(src []byte, w, h, radius int) []byte {
	if radius <= 0 {
		return append([]byte(nil), src...)
	}
	out := make([]byte, len(src))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			var sum [3]int
			count := 0
			for yy := maxInt(0, y-radius); yy <= minInt(h-1, y+radius); yy++ {
				for xx := maxInt(0, x-radius); xx <= minInt(w-1, x+radius); xx++ {
					i := (yy*w + xx) * 3
					sum[0] += int(src[i])
					sum[1] += int(src[i+1])
					sum[2] += int(src[i+2])
					count++
				}
			}
			i := (y*w + x) * 3
			out[i] = byte(sum[0] / count)
			out[i+1] = byte(sum[1] / count)
			out[i+2] = byte(sum[2] / count)
		}
	}
	return out
}
func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func (p *Processor) Close() {
	if p.decoder != nil {
		p.decoder.Close()
	}
	if p.encoder != nil {
		p.encoder.Close()
	}
}

type auGroup struct {
	Timestamp uint32
	Packets   []gateway.VideoRTPPacket
}
type auAssembler struct {
	current uint32
	has     bool
	packets []gateway.VideoRTPPacket
}

func newAUAssembler() *auAssembler { return &auAssembler{} }
func (a *auAssembler) Push(pkt gateway.VideoRTPPacket) []auGroup {
	var out []auGroup
	if !a.has {
		a.current = pkt.Timestamp
		a.has = true
	}
	if pkt.Timestamp != a.current && len(a.packets) > 0 {
		out = append(out, auGroup{Timestamp: a.current, Packets: sortPackets(a.packets)})
		a.packets = nil
		a.current = pkt.Timestamp
	}
	a.packets = append(a.packets, pkt)
	if pkt.Marker {
		out = append(out, auGroup{Timestamp: pkt.Timestamp, Packets: sortPackets(a.packets)})
		a.packets = nil
		a.has = false
	}
	return out
}
func sortPackets(in []gateway.VideoRTPPacket) []gateway.VideoRTPPacket {
	out := append([]gateway.VideoRTPPacket(nil), in...)
	sort.Slice(out, func(i, j int) bool { return out[i].SequenceNumber < out[j].SequenceNumber })
	return out
}

type h264Depacketizer struct{ sps, pps []byte }

func newH264Depacketizer() *h264Depacketizer { return &h264Depacketizer{} }
func (d *h264Depacketizer) Assemble(g auGroup) []byte {
	var nalus [][]byte
	var fu bytes.Buffer
	assembling := false
	for _, pkt := range g.Packets {
		p := pkt.Payload
		if len(p) == 0 {
			continue
		}
		nt := p[0] & 0x1f
		switch {
		case nt >= 1 && nt <= 23:
			nalus = append(nalus, append([]byte(nil), p...))
		case nt == 24:
			pos := 1
			for pos+2 <= len(p) {
				n := int(binary.BigEndian.Uint16(p[pos : pos+2]))
				pos += 2
				if n <= 0 || pos+n > len(p) {
					break
				}
				nalus = append(nalus, append([]byte(nil), p[pos:pos+n]...))
				pos += n
			}
		case nt == 28 && len(p) >= 2:
			fh := p[1]
			start := fh&0x80 != 0
			end := fh&0x40 != 0
			header := (p[0] & 0xe0) | (fh & 0x1f)
			if start {
				fu.Reset()
				fu.WriteByte(header)
				fu.Write(p[2:])
				assembling = true
			} else if assembling {
				fu.Write(p[2:])
			}
			if end && assembling {
				nalus = append(nalus, append([]byte(nil), fu.Bytes()...))
				assembling = false
			}
		}
	}
	hasIDR, hasSPS, hasPPS := false, false, false
	for _, n := range nalus {
		if len(n) == 0 {
			continue
		}
		switch n[0] & 0x1f {
		case 5:
			hasIDR = true
		case 7:
			d.sps = append([]byte(nil), n...)
			hasSPS = true
		case 8:
			d.pps = append([]byte(nil), n...)
			hasPPS = true
		}
	}
	var out bytes.Buffer
	if hasIDR {
		if !hasSPS && len(d.sps) > 0 {
			out.Write(startCode)
			out.Write(d.sps)
		}
		if !hasPPS && len(d.pps) > 0 {
			out.Write(startCode)
			out.Write(d.pps)
		}
	}
	for _, n := range nalus {
		out.Write(startCode)
		out.Write(n)
	}
	return out.Bytes()
}

type decodedFrame struct {
	BGR          []byte
	RTPTimestamp uint32
}
type ffmpegDecoder struct {
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	frames chan decodedFrame
	logger *zap.Logger
	mu     sync.Mutex
	lastTS uint32
}

func newFFmpegDecoder(bin string, w, h int, logger *zap.Logger) (*ffmpegDecoder, error) {
	cmd := exec.Command(bin, "-loglevel", "error", "-f", "h264", "-i", "pipe:0", "-an", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1")
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	d := &ffmpegDecoder{cmd: cmd, stdin: stdin, frames: make(chan decodedFrame, 5), logger: logger}
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	go d.readLoop(stdout, w*h*3)
	return d, nil
}
func (d *ffmpegDecoder) Feed(data []byte, ts uint32) error {
	d.mu.Lock()
	d.lastTS = ts
	d.mu.Unlock()
	_, err := d.stdin.Write(data)
	return err
}
func (d *ffmpegDecoder) Frames() <-chan decodedFrame { return d.frames }
func (d *ffmpegDecoder) readLoop(r io.Reader, frameSize int) {
	br := bufio.NewReaderSize(r, 64*1024)
	buf := make([]byte, 0, frameSize*2)
	tmp := make([]byte, 64*1024)
	defer close(d.frames)
	for {
		n, err := br.Read(tmp)
		if n > 0 {
			buf = append(buf, tmp[:n]...)
			for len(buf) >= frameSize {
				raw := append([]byte(nil), buf[:frameSize]...)
				buf = buf[frameSize:]
				d.mu.Lock()
				ts := d.lastTS
				d.mu.Unlock()
				select {
				case d.frames <- decodedFrame{BGR: raw, RTPTimestamp: ts}:
				default:
					select {
					case <-d.frames:
					default:
					}
					select {
					case d.frames <- decodedFrame{BGR: raw, RTPTimestamp: ts}:
					default:
					}
				}
			}
		}
		if err != nil {
			return
		}
	}
}
func (d *ffmpegDecoder) Close() {
	if d.stdin != nil {
		_ = d.stdin.Close()
	}
	if d.cmd != nil {
		_ = d.cmd.Process.Kill()
		_, _ = d.cmd.Process.Wait()
	}
}

type ffmpegEncoder struct {
	cmd   *exec.Cmd
	stdin io.WriteCloser
}

func newFFmpegEncoder(bin string, w, h int, fps float64, url string, logger *zap.Logger) (*ffmpegEncoder, error) {
	cmd := exec.Command(bin, "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", fmt.Sprintf("%dx%d", w, h), "-r", fmt.Sprintf("%.3f", fps), "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", fmt.Sprintf("%d", int(fps)), "-f", "rtp", url)
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	return &ffmpegEncoder{cmd: cmd, stdin: stdin}, nil
}
func (e *ffmpegEncoder) Feed(frame []byte) error { _, err := e.stdin.Write(frame); return err }
func (e *ffmpegEncoder) Close() {
	if e.stdin != nil {
		_ = e.stdin.Close()
	}
	if e.cmd != nil {
		_ = e.cmd.Process.Kill()
		_, _ = e.cmd.Process.Wait()
	}
}

func encodeJPEGResize(bgr []byte, sw, sh, dw, dh, quality int) ([]byte, error) {
	img := image.NewRGBA(image.Rect(0, 0, dw, dh))
	for y := 0; y < dh; y++ {
		sy := y * sh / dh
		for x := 0; x < dw; x++ {
			sx := x * sw / dw
			i := (sy*sw + sx) * 3
			img.SetRGBA(x, y, color.RGBA{R: bgr[i+2], G: bgr[i+1], B: bgr[i], A: 255})
		}
	}
	var buf bytes.Buffer
	err := jpeg.Encode(&buf, img, &jpeg.Options{Quality: quality})
	return buf.Bytes(), err
}
func decodeRLE(counts []uint32, w, h int) []float32 {
	out := make([]float32, w*h)
	idx := 0
	val := float32(0)
	for _, run := range counts {
		for i := 0; i < int(run) && idx < len(out); i++ {
			out[idx] = val
			idx++
		}
		if val == 0 {
			val = 1
		} else {
			val = 0
		}
	}
	return out
}
func loadBackgroundBGR(path string, w, h int) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	img, _, err := image.Decode(f)
	if err != nil {
		return nil, err
	}
	out := make([]byte, w*h*3)
	b := img.Bounds()
	for y := 0; y < h; y++ {
		sy := b.Min.Y + y*b.Dy()/h
		for x := 0; x < w; x++ {
			sx := b.Min.X + x*b.Dx()/w
			r, g, bb, _ := img.At(sx, sy).RGBA()
			i := (y*w + x) * 3
			out[i] = byte(bb >> 8)
			out[i+1] = byte(g >> 8)
			out[i+2] = byte(r >> 8)
		}
	}
	return out, nil
}

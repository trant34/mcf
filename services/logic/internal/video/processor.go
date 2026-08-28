// Package video: processor.go owns the per-stream pipeline state. As of
// v3.2 (rtpgw_design_v3.md section 37) the actual RTP -> raw frame decode
// happens in C++ via cgo (see cdecoder.go / cdecoder/bridge.cpp) -- this
// file is now a thin Go-side wrapper: it forwards RTP packets into the
// CDecoder, and additionally offers a JPEG-resize helper for the separate,
// FPS-sampled RTPGW -> AI Engine leg (raw frames going back to MF do NOT
// go through this helper -- they're sent as-is, bgr24, at full rate).
package video

import (
	"bytes"
	"image"
	"image/color"
	"image/jpeg"

	pb "mcf/services/logic/internal/pb/api/proto"

	"go.uber.org/zap"

	"sync/atomic"
)

// PipelineConfig configures one stream's decode + AI-sampling behaviour.
type PipelineConfig struct {
	// DecodeWidth/DecodeHeight MUST match MF's own --width/--height (see
	// RtpOpen.decode_width/decode_height in video_ingest.proto) -- this is
	// both the resolution ffmpeg decodes+scales to AND the resolution raw
	// frames are sent back to MF at.
	DecodeWidth, DecodeHeight int

	// InferWidth/InferHeight: resize target for the JPEG frames sent to
	// the AI Engine (independent of DecodeWidth/DecodeHeight).
	InferWidth, InferHeight int

	JPEGQuality  int
	FFmpegBinary string
}

type Metrics struct {
	Packets    atomic.Uint64
	FramesToMF atomic.Uint64 // every decoded frame, sent back to MF raw
	FramesToAI atomic.Uint64 // FPS-sampled subset, JPEG-encoded, sent to AI Engine
}

// StreamPipeline owns one video stream's CDecoder plus the two independent
// output counters described above.
type StreamPipeline struct {
	cfg     PipelineConfig
	logger  *zap.Logger
	decoder *CDecoder

	mfFrameSeq atomic.Int64
	aiFrameSeq atomic.Int64

	Metrics Metrics
}

func NewStreamPipeline(cfg PipelineConfig, logger *zap.Logger) (*StreamPipeline, error) {
	if cfg.JPEGQuality <= 0 {
		cfg.JPEGQuality = 80
	}
	dec, err := NewCDecoder(cfg.DecodeWidth, cfg.DecodeHeight, cfg.FFmpegBinary)
	if err != nil {
		return nil, err
	}
	return &StreamPipeline{cfg: cfg, logger: logger, decoder: dec}, nil
}

// Push feeds one RTP packet (as received over the gRPC ingest stream from
// MF) into the cgo decode chain.
func (s *StreamPipeline) Push(p *pb.RtpPacket) {
	s.Metrics.Packets.Add(1)
	s.decoder.PushPacket(
		uint16(p.GetSequenceNumber()),
		p.GetTimestamp(),
		p.GetSsrc(),
		p.GetMarker(),
		uint8(p.GetPayloadType()),
		p.GetPayload(),
	)
}

// RawFrameForMF is one decoded frame ready to stream back to MF as-is
// (bgr24, DecodeWidth x DecodeHeight, not resized/re-encoded).
type RawFrameForMF struct {
	FrameID      int64
	RTPTimestamp uint32
	BGR          []byte
}

// PollRawFrame non-blockingly checks the decoder for one newly decoded
// frame. Callers should poll this in a tight loop (short sleep when empty)
// -- see video_ingest_server.go's forwardDecodedFrames. Every frame that
// comes out here is meant for MF's compositor; FPS sampling for the AI
// Engine leg happens separately in EncodeJPEGForAI, called by the caller
// only for frames it decides to sample.
func (s *StreamPipeline) PollRawFrame() (RawFrameForMF, bool) {
	raw, rtpTS, ok := s.decoder.PollFrame()
	if !ok {
		return RawFrameForMF{}, false
	}
	s.Metrics.FramesToMF.Add(1)
	frameID := s.mfFrameSeq.Add(1)
	return RawFrameForMF{FrameID: frameID, RTPTimestamp: rtpTS, BGR: raw}, true
}

// SampledJPEGFrame is one JPEG-encoded, inference-resolution frame ready
// for the AI Engine.
type SampledJPEGFrame struct {
	FrameID      int64
	RTPTimestamp uint32
	JPEG         []byte
}

// EncodeJPEGForAI resizes+JPEG-encodes a raw bgr24 frame for the AI Engine
// leg. The caller (video_ingest_server.go) only calls this for the
// FPS-sampled subset of frames -- NOT for every frame PollRawFrame yields.
func (s *StreamPipeline) EncodeJPEGForAI(raw RawFrameForMF) (SampledJPEGFrame, error) {
	s.Metrics.FramesToAI.Add(1)
	frameID := s.aiFrameSeq.Add(1)
	buf, err := encodeJPEGResize(raw.BGR, s.cfg.DecodeWidth, s.cfg.DecodeHeight,
		s.cfg.InferWidth, s.cfg.InferHeight, s.cfg.JPEGQuality)
	if err != nil {
		return SampledJPEGFrame{}, err
	}
	return SampledJPEGFrame{FrameID: frameID, RTPTimestamp: raw.RTPTimestamp, JPEG: buf}, nil
}

func (s *StreamPipeline) Close() {
	s.decoder.Close()
}

// encodeJPEGResize: nearest-neighbor resize (cheap, adequate for a
// segmentation model's input) + JPEG encode. Unchanged from v3.1.
func encodeJPEGResize(bgr []byte, sw, sh, dw, dh, quality int) ([]byte, error) {
	if dw <= 0 || dh <= 0 {
		dw, dh = sw, sh
	}
	img := image.NewRGBA(image.Rect(0, 0, dw, dh))
	for y := 0; y < dh; y++ {
		sy := y * sh / dh
		for x := 0; x < dw; x++ {
			sx := x * sw / dw
			i := (sy*sw + sx) * 3
			if i+2 >= len(bgr) {
				continue
			}
			img.SetRGBA(x, y, color.RGBA{R: bgr[i+2], G: bgr[i+1], B: bgr[i], A: 255})
		}
	}
	var buf bytes.Buffer
	err := jpeg.Encode(&buf, img, &jpeg.Options{Quality: quality})
	return buf.Bytes(), err
}

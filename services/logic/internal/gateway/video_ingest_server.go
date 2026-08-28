// video_ingest_server.go implements the RTPGW side of the MF <-> RTPGW
// bidirectional gRPC transport (api/proto/video_ingest.proto).
//
// v3.2 (rtpgw_design_v3.md section 37): MF no longer decodes at all -- it
// only forwards RTP and composites. RTPGW decodes ONCE (via the cgo
// CDecoder, video/cdecoder.go) and fans each decoded frame out two
// independent ways:
//  1. every frame, raw bgr24, streamed straight back to MF over THIS
//     response stream (IngestResponse.frame) for its compositor;
//  2. an FPS-sampled subset, JPEG-encoded, forwarded to the AI Engine over
//     the separate RTPGW <-> AI Engine gRPC stream (unchanged from v3.1).
//
// The AI Engine still pushes mask results to MF directly over HTTP --
// RTPGW is never in that return path, same as v3.1.
package gateway

import (
	"fmt"
	"io"
	"sync"
	"time"

	"mcf/services/logic/internal/ai_client"
	pb "mcf/services/logic/internal/pb/api/proto"
	"mcf/services/logic/internal/video"

	"go.uber.org/zap"
)

// VideoSender is the subset of ai_client.VideoGRPCProxy the ingest server
// needs, for the RTPGW -> AI Engine leg.
type VideoSender interface {
	SendFrameAsync(sessionID, streamID string, in ai_client.VideoInferenceRequest) error
}

type VideoIngestConfig struct {
	// Fallback decode resolution, used only if a stream's RtpOpen doesn't
	// carry decode_width/decode_height (older MF client). New MF clients
	// always set it -- see rtpgw_design_v3.md section 37.
	DefaultDecodeWidth, DefaultDecodeHeight int

	InferWidth, InferHeight int
	InferFPS                float64
	JPEGQuality             int
	FFmpegBinary            string
}

type VideoIngestServer struct {
	pb.UnimplementedVideoIngestServiceServer

	cfg    VideoIngestConfig
	sender VideoSender
	logger *zap.Logger
}

func NewVideoIngestServer(cfg VideoIngestConfig, sender VideoSender, logger *zap.Logger) *VideoIngestServer {
	return &VideoIngestServer{cfg: cfg, sender: sender, logger: logger}
}

type ingestSession struct {
	sessionID, streamID       string
	effectType                string
	decodeWidth, decodeHeight int
	pipeline                  *video.StreamPipeline
	lastAISample              time.Time
	aiSampleInterval          time.Duration
	wg                        sync.WaitGroup
	stopPoll                  chan struct{}
}

func (s *VideoIngestServer) Transport(stream pb.VideoIngestService_TransportServer) error {
	var sess *ingestSession
	var sendMu sync.Mutex

	sendEvent := func(status, message string) {
		if sess == nil {
			return
		}
		ev := &pb.IngestEvent{
			SessionId: sess.sessionID,
			StreamId:  sess.streamID,
			Status:    status,
			Message:   message,
		}
		if sess.pipeline != nil {
			ev.PacketsReceived = sess.pipeline.Metrics.Packets.Load()
			ev.FramesDecoded = sess.pipeline.Metrics.FramesToMF.Load()
		}
		resp := &pb.IngestResponse{
			SessionId: sess.sessionID,
			StreamId:  sess.streamID,
			Payload:   &pb.IngestResponse_Event{Event: ev},
		}
		sendMu.Lock()
		err := stream.Send(resp)
		sendMu.Unlock()
		if err != nil {
			s.logger.Warn("failed to send IngestEvent", zap.Error(err))
		}
	}

	defer func() {
		if sess != nil {
			close(sess.stopPoll)
			sess.wg.Wait()
			if sess.pipeline != nil {
				sess.pipeline.Close()
			}
		}
	}()

	for {
		frame, err := stream.Recv()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return err
		}

		if sess == nil && frame.GetOpen() == nil {
			return fmt.Errorf("first message on video ingest stream must be RtpOpen")
		}

		if open := frame.GetOpen(); open != nil {
			decodeWidth := int(open.GetDecodeWidth())
			decodeHeight := int(open.GetDecodeHeight())
			if decodeWidth <= 0 || decodeHeight <= 0 {
				// Older MF client that doesn't send decode_width/height --
				// fall back to the static config default, with a loud
				// warning since a mismatch here silently corrupts frames
				// (see rtpgw_design_v3.md section 35.6 for the class of
				// bug this caused before decode_width/height existed).
				decodeWidth, decodeHeight = s.cfg.DefaultDecodeWidth, s.cfg.DefaultDecodeHeight
				s.logger.Warn("RtpOpen missing decode_width/decode_height, "+
					"falling back to static config default -- verify this matches MF's --width/--height exactly",
					zap.Int("fallback_width", decodeWidth), zap.Int("fallback_height", decodeHeight))
			}

			pipeline, perr := video.NewStreamPipeline(video.PipelineConfig{
				DecodeWidth: decodeWidth, DecodeHeight: decodeHeight,
				InferWidth: s.cfg.InferWidth, InferHeight: s.cfg.InferHeight,
				JPEGQuality: s.cfg.JPEGQuality, FFmpegBinary: s.cfg.FFmpegBinary,
			}, s.logger)
			if perr != nil {
				return fmt.Errorf("failed to start decode pipeline: %w", perr)
			}
			aiFPS := s.cfg.InferFPS
			if open.GetTargetFps() > 0 {
				aiFPS = float64(open.GetTargetFps())
			}
			if aiFPS <= 0 {
				aiFPS = 5
			}
			sess = &ingestSession{
				sessionID:        frame.GetSessionId(),
				streamID:         frame.GetStreamId(),
				effectType:       open.GetEffectType(),
				decodeWidth:      decodeWidth,
				decodeHeight:     decodeHeight,
				pipeline:         pipeline,
				aiSampleInterval: time.Duration(float64(time.Second) / aiFPS),
				stopPoll:         make(chan struct{}),
			}
			s.logger.Info("video ingest stream opened",
				zap.String("session_id", sess.sessionID),
				zap.String("stream_id", sess.streamID),
				zap.String("effect_type", sess.effectType),
				zap.Int("decode_width", decodeWidth), zap.Int("decode_height", decodeHeight))

			sess.wg.Add(1)
			go s.pollAndForward(sess, stream, &sendMu)

			sendEvent("opened", "")
			continue
		}

		if frame.GetIsEos() {
			sendEvent("closed", "eos")
			return nil
		}

		if pkt := frame.GetPacket(); pkt != nil {
			sess.pipeline.Push(pkt)
		}
	}
}

// pollAndForward drains CDecoder's poll-based output and fans each frame
// out two ways: EVERY frame goes back to MF raw (compositor needs
// continuity); an FPS-sampled subset goes to the AI Engine, JPEG-encoded.
// Polling (rather than a blocking channel read) is deliberate: PollFrame()
// crosses the cgo boundary, and a short sleep-when-empty loop avoids
// pinning an OS thread on a long blocking cgo call (a well-known cgo
// footgun) while still being effectively real-time at typical video FPS.
func (s *VideoIngestServer) pollAndForward(sess *ingestSession, stream pb.VideoIngestService_TransportServer, sendMu *sync.Mutex) {
	defer sess.wg.Done()
	const pollInterval = 5 * time.Millisecond

	for {
		select {
		case <-sess.stopPoll:
			return
		default:
		}

		raw, ok := sess.pipeline.PollRawFrame()
		if !ok {
			time.Sleep(pollInterval)
			continue
		}

		// Leg 1: every frame, raw, back to MF.
		resp := &pb.IngestResponse{
			SessionId: sess.sessionID,
			StreamId:  sess.streamID,
			Payload: &pb.IngestResponse_Frame{Frame: &pb.DecodedFrame{
				FrameId:      raw.FrameID,
				RtpTimestamp: raw.RTPTimestamp,
				Width:        int32(sess.decodeWidth),
				Height:       int32(sess.decodeHeight),
				Encoding:     "bgr24",
				FrameData:    raw.BGR,
			}},
		}
		sendMu.Lock()
		err := stream.Send(resp)
		sendMu.Unlock()
		if err != nil {
			s.logger.Warn("failed to send decoded frame to MF", zap.Error(err))
			return
		}

		// Leg 2: FPS-sampled subset, JPEG-encoded, to the AI Engine.
		now := time.Now()
		if !sess.lastAISample.IsZero() && now.Sub(sess.lastAISample) < sess.aiSampleInterval {
			continue
		}
		sess.lastAISample = now

		sampled, err := sess.pipeline.EncodeJPEGForAI(raw)
		if err != nil {
			s.logger.Warn("jpeg encode failed", zap.Error(err))
			continue
		}
		err = s.sender.SendFrameAsync(sess.sessionID, sess.streamID, ai_client.VideoInferenceRequest{
			SessionID:      sess.sessionID,
			StreamID:       sess.streamID,
			FrameID:        sampled.FrameID,
			RTPTimestamp:   int64(sampled.RTPTimestamp),
			OriginalWidth:  int32(sess.decodeWidth),
			OriginalHeight: int32(sess.decodeHeight),
			EffectType:     sess.effectType,
			JPEG:           sampled.JPEG,
		})
		if err != nil {
			s.logger.Warn("failed to forward frame to AI engine",
				zap.String("session_id", sess.sessionID),
				zap.Int64("frame_id", sampled.FrameID),
				zap.Error(err))
		}
	}
}

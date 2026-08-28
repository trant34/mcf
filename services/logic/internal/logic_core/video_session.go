package logic_core

// import (
// 	"context"

// 	"mcf/services/logic/internal/ai_client"
// 	"mcf/services/logic/internal/gateway"
// 	pb "mcf/services/logic/internal/pb/api/proto"
// 	"mcf/services/logic/internal/video"

// 	"go.uber.org/zap"
// )

// // VideoOrchestrator wires the video path end to end:
// //
// //	MP --RTP/H264 UDP--> VideoRTPGateway --packets--> video.Processor
// //	  --decode + sample--> frameCh --(gRPC bidi stream)--> AI Engine
// //	  <--masks-- maskCh <--(gRPC bidi stream)-- AI Engine
// //
// // This replaces the old flow where the MP-side test clients (mf_video.py /
// // mf_cpp) decoded RTP locally and POSTed JPEGs to a stateless HTTP endpoint.
// // RTPGW is now the one receiving and decoding RTP, and AI Engine inference
// // happens over one persistent bidirectional gRPC stream per video leg
// // (ai_client.AIClient.ProcessVideoStream), instead of one HTTP
// // request/response per frame.
// type VideoOrchestrator struct {
// 	videoRTPGW *gateway.VideoRTPGateway
// 	aiClient   *ai_client.AIClient
// 	logger     *zap.Logger

// 	processorCfg video.Config
// 	videoConfig  *pb.VideoConfig

// 	streamID string
// }

// func NewVideoOrchestrator(
// 	videoRTPGW *gateway.VideoRTPGateway,
// 	aiClient *ai_client.AIClient,
// 	processorCfg video.Config,
// 	videoConfig *pb.VideoConfig,
// 	streamID string,
// 	logger *zap.Logger,
// ) *VideoOrchestrator {
// 	if streamID == "" {
// 		streamID = "video-0"
// 	}
// 	return &VideoOrchestrator{
// 		videoRTPGW:   videoRTPGW,
// 		aiClient:     aiClient,
// 		logger:       logger,
// 		processorCfg: processorCfg,
// 		videoConfig:  videoConfig,
// 		streamID:     streamID,
// 	}
// }

// // HandleVideoSession blocks until ctx is cancelled. It should be run in its
// // own goroutine, one per call/session, alongside the audio orchestration.
// func (o *VideoOrchestrator) HandleVideoSession(ctx context.Context, sessionID string) {
// 	o.logger.Info("Starting video session orchestration",
// 		zap.String("session_id", sessionID),
// 		zap.String("stream_id", o.streamID),
// 		zap.String("video_rtp_addr", o.videoRTPGW.Address),
// 	)

// 	processor, err := video.NewProcessor(o.processorCfg, o.logger)
// 	if err != nil {
// 		o.logger.Error("Failed to start video processor (is ffmpeg installed?)",
// 			zap.String("session_id", sessionID), zap.Error(err))
// 		return
// 	}

// 	packetCh := make(chan gateway.VideoRTPPacket, 256)
// 	// Bounded, "keep latest" semantics: the processor already drops the
// 	// oldest queued frame when frameCh is full, so a small buffer is enough
// 	// to smooth bursts without adding meaningful latency.
// 	frameCh := make(chan ai_client.VideoFrameInput, 4)
// 	maskCh := make(chan ai_client.VideoMaskResult, 16)

// 	// 1. RTP listener: MP -> RTPGW.
// 	go func() {
// 		if err := o.videoRTPGW.StartListening(ctx, sessionID, packetCh); err != nil {
// 			o.logger.Error("VideoRTPGateway error", zap.String("session_id", sessionID), zap.Error(err))
// 		}
// 	}()

// 	// 2. RTPGW decode pipeline: RTP -> H264 AU -> raw frame -> sampled JPEG.
// 	go func() {
// 		if err := processor.Run(ctx, packetCh, frameCh, maskCh); err != nil {
// 			o.logger.Error("Video processor error", zap.String("session_id", sessionID), zap.Error(err))
// 		}
// 	}()

// 	// 3. Persistent bidirectional gRPC stream to the AI Engine: frames go
// 	//    up, masks come back down, continuously, on the same stream.
// 	if err := o.aiClient.ProcessVideoStream(ctx, sessionID, o.videoConfig, frameCh, maskCh); err != nil {
// 		if ctx.Err() == nil {
// 			o.logger.Error("Video AI gRPC stream error", zap.String("session_id", sessionID), zap.Error(err))
// 		}
// 	}

// 	o.logger.Info("Video session orchestration stopped", zap.String("session_id", sessionID))
// }

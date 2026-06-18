package ai_client

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"sync"
	"time"
	pb "mcf/services/logic/internal/pb/api/proto"

	"go.uber.org/zap"
)

// VideoInferenceRequest is the internal LOGIC request produced by HTTPGW.
// JPEG remains binary; it is encoded directly into protobuf bytes.
type VideoInferenceRequest struct {
	SessionID      string
	StreamID       string
	FrameID        int64
	RTPTimestamp   int64
	OriginalWidth  int32
	OriginalHeight int32
	EffectType     string
	JPEG           []byte
}

// VideoInferenceResponse preserves the JSON contract currently returned to MF.
type VideoInferenceResponse struct {
	SessionID    string   `json:"session_id"`
	StreamID     string   `json:"stream_id"`
	FrameID      int64    `json:"frame_id"`
	RTPTimestamp int64    `json:"rtp_timestamp"`
	MaskWidth    int32    `json:"mask_width"`
	MaskHeight   int32    `json:"mask_height"`
	RLECounts    []uint32 `json:"rle_counts"`
	RLERuns      int      `json:"rle_runs"`
	LatencyMS    float32  `json:"latency_ms"`
	Status       string   `json:"status"`
	ErrorMessage string   `json:"error_message,omitempty"`
}

type videoResult struct {
	resp *pb.VideoResponse
	err  error
}

type videoSessionStream struct {
	key       string
	sessionID string
	streamID  string

	stream pb.TranslationService_ProcessVideoStreamClient
	cancel context.CancelFunc

	sendMu sync.Mutex
	mu     sync.Mutex
	closed bool
	err    error

	pending map[int64]chan videoResult
	logger  *zap.Logger
}

// VideoGRPCProxy keeps one persistent gRPC stream per session/stream pair.
// HTTPGW can still expose POST /v1/video/infer to MF, while LOGIC -> AI Engine
// uses gRPC bidirectional streaming.
type VideoGRPCProxy struct {
	ai      *AIClient
	timeout time.Duration
	logger  *zap.Logger

	config *pb.VideoConfig

	mu       sync.Mutex
	sessions map[string]*videoSessionStream
}

func NewVideoGRPCProxy(
	ai *AIClient,
	timeout time.Duration,
	config *pb.VideoConfig,
	logger *zap.Logger,
) *VideoGRPCProxy {
	return &VideoGRPCProxy{
		ai:       ai,
		timeout:  timeout,
		logger:   logger,
		config:   config,
		sessions: make(map[string]*videoSessionStream),
	}
}

func sessionKey(sessionID, streamID string) string {
	return sessionID + "\x00" + streamID
}

func (p *VideoGRPCProxy) getOrCreateSession(
	ctx context.Context,
	sessionID string,
	streamID string,
) (*videoSessionStream, error) {
	key := sessionKey(sessionID, streamID)

	p.mu.Lock()
	if current, ok := p.sessions[key]; ok {
		current.mu.Lock()
		closed := current.closed
		current.mu.Unlock()
		if !closed {
			p.mu.Unlock()
			return current, nil
		}
		delete(p.sessions, key)
	}
	p.mu.Unlock()

	streamCtx, cancel := context.WithCancel(context.Background())
	stream, err := p.ai.client.ProcessVideoStream(streamCtx)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("open ProcessVideoStream: %w", err)
	}

	session := &videoSessionStream{
		key:       key,
		sessionID: sessionID,
		streamID:  streamID,
		stream:    stream,
		cancel:    cancel,
		pending:   make(map[int64]chan videoResult),
		logger:    p.logger,
	}

	configReq := &pb.VideoRequest{
		SessionId: sessionID,
		StreamId:  streamID,
		Payload: &pb.VideoRequest_Config{
			Config: p.config,
		},
	}
	if err := stream.Send(configReq); err != nil {
		cancel()
		return nil, fmt.Errorf("send video config: %w", err)
	}

	p.mu.Lock()
	if old, exists := p.sessions[key]; exists {
		p.mu.Unlock()
		cancel()
		return old, nil
	}
	p.sessions[key] = session
	p.mu.Unlock()

	go p.recvLoop(session)

	p.logger.Info(
		"Opened persistent video gRPC stream",
		zap.String("session_id", sessionID),
		zap.String("stream_id", streamID),
	)

	return session, nil
}

func (p *VideoGRPCProxy) recvLoop(session *videoSessionStream) {
	for {
		resp, err := session.stream.Recv()
		if err != nil {
			if err == io.EOF {
				err = fmt.Errorf("video gRPC stream closed by AI Engine")
			}
			p.closeSession(session, err)
			return
		}

		if resp.GetIsFinal() {
			p.closeSession(session, nil)
			return
		}

		mask := resp.GetMask()
		if mask == nil {
			continue
		}

		session.mu.Lock()
		waiter := session.pending[mask.GetFrameId()]
		if waiter != nil {
			delete(session.pending, mask.GetFrameId())
		}
		session.mu.Unlock()

		if waiter == nil {
			p.logger.Warn(
				"Received video mask without pending request",
				zap.String("session_id", resp.GetSessionId()),
				zap.String("stream_id", resp.GetStreamId()),
				zap.Int64("frame_id", mask.GetFrameId()),
			)
			continue
		}

		waiter <- videoResult{resp: resp}
		close(waiter)
	}
}

func (p *VideoGRPCProxy) closeSession(session *videoSessionStream, err error) {
	session.mu.Lock()
	if session.closed {
		session.mu.Unlock()
		return
	}
	session.closed = true
	session.err = err
	pending := session.pending
	session.pending = make(map[int64]chan videoResult)
	session.mu.Unlock()

	session.cancel()

	for _, waiter := range pending {
		waiter <- videoResult{err: err}
		close(waiter)
	}

	p.mu.Lock()
	if current := p.sessions[session.key]; current == session {
		delete(p.sessions, session.key)
	}
	p.mu.Unlock()

	if err != nil {
		p.logger.Warn(
			"Video gRPC session closed",
			zap.String("session_id", session.sessionID),
			zap.String("stream_id", session.streamID),
			zap.Error(err),
		)
	}
}

func (p *VideoGRPCProxy) Infer(
	ctx context.Context,
	in VideoInferenceRequest,
) ([]byte, error) {
	if in.SessionID == "" {
		return nil, fmt.Errorf("missing session id")
	}
	if len(in.JPEG) == 0 {
		return nil, fmt.Errorf("empty JPEG")
	}

	session, err := p.getOrCreateSession(ctx, in.SessionID, in.StreamID)
	if err != nil {
		return nil, err
	}

	waiter := make(chan videoResult, 1)

	session.mu.Lock()
	if session.closed {
		streamErr := session.err
		session.mu.Unlock()
		if streamErr == nil {
			streamErr = fmt.Errorf("video gRPC stream is closed")
		}
		return nil, streamErr
	}
	if _, exists := session.pending[in.FrameID]; exists {
		session.mu.Unlock()
		return nil, fmt.Errorf("duplicate pending frame_id=%d", in.FrameID)
	}
	session.pending[in.FrameID] = waiter
	session.mu.Unlock()

	req := &pb.VideoRequest{
		SessionId: in.SessionID,
		StreamId:  in.StreamID,
		Payload: &pb.VideoRequest_Frame{
			Frame: &pb.VideoFrame{
				FrameId:        in.FrameID,
				RtpTimestamp:   in.RTPTimestamp,
				OriginalWidth:  in.OriginalWidth,
				OriginalHeight: in.OriginalHeight,
				Encoding:       "jpeg",
				ImageData:      in.JPEG,
			},
		},
	}

	session.sendMu.Lock()
	err = session.stream.Send(req)
	session.sendMu.Unlock()
	if err != nil {
		session.mu.Lock()
		delete(session.pending, in.FrameID)
		session.mu.Unlock()
		p.closeSession(session, err)
		return nil, fmt.Errorf("send video frame over gRPC: %w", err)
	}

	requestCtx := ctx
	var cancel context.CancelFunc
	if p.timeout > 0 {
		requestCtx, cancel = context.WithTimeout(ctx, p.timeout)
		defer cancel()
	}

	select {
	case result := <-waiter:
		if result.err != nil {
			return nil, result.err
		}
		mask := result.resp.GetMask()
		if mask == nil {
			return nil, fmt.Errorf("AI Engine returned empty video mask")
		}

		out := VideoInferenceResponse{
			SessionID:    result.resp.GetSessionId(),
			StreamID:     result.resp.GetStreamId(),
			FrameID:      mask.GetFrameId(),
			RTPTimestamp: mask.GetRtpTimestamp(),
			MaskWidth:    mask.GetMaskWidth(),
			MaskHeight:   mask.GetMaskHeight(),
			RLECounts:    append([]uint32(nil), mask.GetRleCounts()...),
			RLERuns:      len(mask.GetRleCounts()),
			LatencyMS:    mask.GetLatencyMs(),
			Status:       mask.GetStatus(),
			ErrorMessage: mask.GetErrorMessage(),
		}
		return json.Marshal(out)

	case <-requestCtx.Done():
		session.mu.Lock()
		delete(session.pending, in.FrameID)
		session.mu.Unlock()
		return nil, fmt.Errorf("video inference timeout: %w", requestCtx.Err())
	}
}

func (p *VideoGRPCProxy) Close() {
	p.mu.Lock()
	sessions := make([]*videoSessionStream, 0, len(p.sessions))
	for _, session := range p.sessions {
		sessions = append(sessions, session)
	}
	p.sessions = make(map[string]*videoSessionStream)
	p.mu.Unlock()

	for _, session := range sessions {
		session.sendMu.Lock()
		_ = session.stream.Send(&pb.VideoRequest{
			SessionId: session.sessionID,
			StreamId:  session.streamID,
			IsEos:     true,
		})
		_ = session.stream.CloseSend()
		session.sendMu.Unlock()
		p.closeSession(session, nil)
	}
}

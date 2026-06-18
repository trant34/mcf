package ai_client

import (
	"context"
	"io"
	pb "mcf/services/logic/internal/pb/api/proto"
	"time"

	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type VideoFrameInput struct {
	SessionID      string
	FrameID        int64
	RTPTimestamp   int64
	OriginalWidth  int32
	OriginalHeight int32
	Encoding       string
	ImageData      []byte
}

type VideoMaskResult struct {
	SessionID    string
	FrameID      int64
	RTPTimestamp int64
	MaskWidth    int32
	MaskHeight   int32
	RLECounts    []uint32
	LatencyMS    float32
	Status       string
	ErrorMessage string
}

type AIClient struct {
	conn   *grpc.ClientConn
	client pb.TranslationServiceClient
	logger *zap.Logger
}

func NewAIClient(targetAddress string, logger *zap.Logger) (*AIClient, error) {
	logger.Info("Connecting to AI service", zap.String("address", targetAddress))

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, err := grpc.DialContext(ctx, targetAddress, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithBlock())
	if err != nil {
		return nil, err
	}

	logger.Info("Successfully connected to AI service", zap.String("address", targetAddress))
	client := pb.NewTranslationServiceClient(conn)
	return &AIClient{
		conn:   conn,
		client: client,
		logger: logger,
	}, nil
}

func (c *AIClient) Close() {
	c.conn.Close()
}

func (c *AIClient) ProcessStream(ctx context.Context, sessionID string, audioChan <-chan []byte, textChan chan<- string) error {
	stream, err := c.client.ProcessMediaStream(ctx)
	if err != nil {
		return err
	}

	// 1. Goroutine upstream: Send audio data to AI service
	go func() {
		// first data packet must send Config (as oneof in proto)
		configReq := &pb.MediaRequest{
			SessionId: sessionID,
			Payload: &pb.MediaRequest_Config_{
				Config: &pb.MediaRequest_Config{
					SourceLanguage: "vi",
					TargetLanguage: "en",
					EnableTts:      false,
					SampleRate:     16000,
				},
			},
		}
		if err := stream.Send(configReq); err != nil {
			c.logger.Error("Failed to send config request", zap.String("session_id", sessionID), zap.Error(err))
			return
		}

		var seq int64 = 1
		for {
			select {
			case <-ctx.Done():
				// Context cancelled, close the send direction of the stream
				stream.Send(&pb.MediaRequest{SessionId: sessionID, IsEos: true})
				stream.CloseSend()
				return
			case audioChunk := <-audioChan:
				c.logger.Debug("Sending audio chunk", zap.String("session_id", sessionID), zap.Int("size", len(audioChunk)))
				// Send audio chunk as MediaData
				req := &pb.MediaRequest{
					SessionId:      sessionID,
					SequenceNumber: seq,
					Payload: &pb.MediaRequest_AudioChunk{
						AudioChunk: audioChunk,
					},
				}
				if err := stream.Send(req); err != nil {
					c.logger.Error("Failed to send audio chunk", zap.String("session_id", sessionID), zap.Error(err))
					return
				}
				seq++
			}
		}
	}()

	// 2. Downstream: Receive translated text from AI service
	for {
		resp, err := stream.Recv()
		if err == io.EOF {
			c.logger.Info("AI Server closed the stream", zap.String("session_id", sessionID))
			break
		}

		if err != nil {
			if ctx.Err() == nil {
				c.logger.Error("Error receiving text", zap.String("session_id", sessionID), zap.Error(err))
			}
			break
		}

		if resp.TranslatedText != "" {
			textChan <- resp.TranslatedText
		}
	}

	return nil
}

func (c *AIClient) ProcessVideoStream(
	ctx context.Context,
	sessionID string,
	config *pb.VideoConfig,
	frameChan <-chan VideoFrameInput,
	maskChan chan<- VideoMaskResult,
) error {
	stream, err := c.client.ProcessVideoStream(ctx)
	if err != nil {
		return err
	}

	// Gửi config một lần khi mở stream.
	if err := stream.Send(&pb.VideoRequest{
		SessionId: sessionID,
		Payload: &pb.VideoRequest_Config{
			Config: config,
		},
	}); err != nil {
		return err
	}

	errChan := make(chan error, 2)

	// Goroutine gửi frame.
	go func() {
		for {
			select {
			case <-ctx.Done():
				_ = stream.Send(&pb.VideoRequest{
					SessionId: sessionID,
					IsEos:     true,
				})
				_ = stream.CloseSend()
				errChan <- nil
				return

			case frame, ok := <-frameChan:
				if !ok {
					_ = stream.Send(&pb.VideoRequest{
						SessionId: sessionID,
						IsEos:     true,
					})
					_ = stream.CloseSend()
					errChan <- nil
					return
				}

				req := &pb.VideoRequest{
					SessionId: sessionID,
					Payload: &pb.VideoRequest_Frame{
						Frame: &pb.VideoFrame{
							FrameId:        frame.FrameID,
							RtpTimestamp:   frame.RTPTimestamp,
							OriginalWidth:  frame.OriginalWidth,
							OriginalHeight: frame.OriginalHeight,
							Encoding:       frame.Encoding,
							ImageData:      frame.ImageData,
						},
					},
				}

				if err := stream.Send(req); err != nil {
					errChan <- err
					return
				}
			}
		}
	}()

	// Goroutine nhận mask.
	go func() {
		for {
			resp, err := stream.Recv()
			if err == io.EOF {
				errChan <- nil
				return
			}
			if err != nil {
				errChan <- err
				return
			}

			if resp.Mask == nil {
				continue
			}

			result := VideoMaskResult{
				SessionID:    resp.SessionId,
				FrameID:      resp.Mask.FrameId,
				RTPTimestamp: resp.Mask.RtpTimestamp,
				MaskWidth:    resp.Mask.MaskWidth,
				MaskHeight:   resp.Mask.MaskHeight,
				RLECounts:    append([]uint32(nil), resp.Mask.RleCounts...),
				LatencyMS:    resp.Mask.LatencyMs,
				Status:       resp.Mask.Status,
				ErrorMessage: resp.Mask.ErrorMessage,
			}

			select {
			case maskChan <- result:
			case <-ctx.Done():
				errChan <- nil
				return
			}
		}
	}()

	select {
	case <-ctx.Done():
		return nil
	case err := <-errChan:
		return err
	}
}

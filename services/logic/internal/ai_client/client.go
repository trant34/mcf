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

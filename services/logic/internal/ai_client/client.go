package ai_client

import (
	"context"
	"io"
	"log"
	pb "mcf/services/logic/internal/pb/api/proto"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type AIClient struct {
	conn   *grpc.ClientConn
	client pb.TranslationServiceClient
}

func NewAIClient(targetAddress string) (*AIClient, error) {
	log.Printf("[AI Client] Connecting to AI service at %s\n", targetAddress)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	conn, err := grpc.DialContext(ctx, targetAddress, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithBlock())
	if err != nil {
		return nil, err
	}

	log.Printf("[AI Client] Successfully connected to AI service at %s\n", targetAddress)
	client := pb.NewTranslationServiceClient(conn)
	return &AIClient{
		conn:   conn,
		client: client,
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
					SourceLanguage: "en-US",
					TargetLanguage: "vi-VN",
					EnableTts:      false,
					SampleRate:     16000,
				},
			},
		}
		if err := stream.Send(configReq); err != nil {
			log.Printf("[AI Client] Failed to send config request: %v\n", err)
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
				log.Printf("[AI Client] Sending audio chunk of size %d bytes for session [%s]\n", len(audioChunk), sessionID)
				// Send audio chunk as MediaData
				req := &pb.MediaRequest{
					SessionId:      sessionID,
					SequenceNumber: seq,
					Payload: &pb.MediaRequest_AudioChunk{
						AudioChunk: audioChunk,
					},
				}
				if err := stream.Send(req); err != nil {
					log.Printf("[AI Client] Failed to send audio chunk: %v\n", err)
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
			log.Printf("[AI Client] Server AI closed the stream for session [%s]\n", sessionID)
			break
		}

		if err != nil {
			if ctx.Err() == nil {
				log.Printf("[AI Client] Error received text: %v\n", err)
			}
			break
		}

		if resp.TranslatedText != "" {
			textChan <- resp.TranslatedText
		}
	}

	return nil
}

package logic_core

import (
	"context"
	"mcf/services/logic/internal/ai_client"
	"mcf/services/logic/internal/gateway"

	"go.uber.org/zap"
)

type Orchestrator struct {
	httpGW     *gateway.HTTPGateway
	RTPGateway *gateway.RTPGateway
	aiClient   *ai_client.AIClient
	logger     *zap.Logger
}

func NewOrchestrator(httpGW *gateway.HTTPGateway, RTPGateway *gateway.RTPGateway, aiClient *ai_client.AIClient, logger *zap.Logger) *Orchestrator {
	return &Orchestrator{
		httpGW:     httpGW,
		RTPGateway: RTPGateway,
		aiClient:   aiClient,
		logger:     logger,
	}
}

func (o *Orchestrator) HandleCallSession(ctx context.Context, sessionID string) {
	o.logger.Info("Starting session orchestration", zap.String("session_id", sessionID))

	audioChan := make(chan []byte, 100)
	textChan := make(chan string, 50)

	// 1. RTP listener
	go func() {
		if err := o.RTPGateway.StartListening(ctx, sessionID, audioChan); err != nil {
			o.logger.Error("RTPGateway Error", zap.String("session_id", sessionID), zap.Error(err))
		}
	}()

	// 2. AI Client processing
	go func() {
		if err := o.aiClient.ProcessStream(ctx, sessionID, audioChan, textChan); err != nil {
			o.logger.Error("AI Client Stream Error", zap.String("session_id", sessionID), zap.Error(err))
		}
	}()

	// 3. Main loop: Listen for translated text and push to HTTP Gateway
	for {
		select {
		case <-ctx.Done():
			o.logger.Info("Session orchestration stopping", zap.String("session_id", sessionID))
			return
		case text := <-textChan:
			// Push translated text to HTTP Gateway
			o.httpGW.PushSubtitle(sessionID, text)
		}
	}
}

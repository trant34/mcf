package logic_core

import (
	"context"
	"log"
	"mcf/services/logic/internal/ai_client"
	"mcf/services/logic/internal/gateway"
)

type Orchestrator struct {
	httpGW     *gateway.HTTPGateway
	RTPGateway *gateway.RTPGateway
	aiClient   *ai_client.AIClient
}

func NewOrchestrator(httpGW *gateway.HTTPGateway, RTPGateway *gateway.RTPGateway, aiClient *ai_client.AIClient) *Orchestrator {
	return &Orchestrator{
		httpGW:     httpGW,
		RTPGateway: RTPGateway,
		aiClient:   aiClient,
	}
}

func (o *Orchestrator) HandleCallSession(ctx context.Context, sessionID string) {
	log.Printf("[LOGIC] Starting session orchestration for [%s]\n", sessionID)

	audioChan := make(chan []byte, 100)
	textChan := make(chan string, 50)

	// 1. RTP listener
	go func() {
		if err := o.RTPGateway.StartListening(ctx, sessionID, audioChan); err != nil {
			log.Printf("[LOGIC] RTPGateway Error: %v\n", err)
		}
	}()

	// 2. AI Client processing
	go func() {
		if err := o.aiClient.ProcessStream(ctx, sessionID, audioChan, textChan); err != nil {
			log.Printf("[LOGIC] AI Client Stream Error: %v\n", err)
		}
	}()

	// 3. Main loop: Listen for translated text and push to HTTP Gateway
	for {
		select {
		case <-ctx.Done():
			log.Printf("[LOGIC] Session [%s] orchestration stopping...\n", sessionID)
			return
		case text := <-textChan:
			// Push translated text to HTTP Gateway
			o.httpGW.PushSubtitle(sessionID, text)
		}
	}
}

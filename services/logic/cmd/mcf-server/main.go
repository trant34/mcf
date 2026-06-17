package main

import (
	"context"
	"mcf/services/logic/internal/ai_client"
	"mcf/services/logic/internal/config"
	"mcf/services/logic/internal/gateway"
	"mcf/services/logic/internal/logic_core"
	"os"
	"os/signal"
	"syscall"
	"time"
	"net/http"

	"go.uber.org/zap"
)

func main() {
	cfg := config.Load()
	defer cfg.Logger.Sync()

	cfg.Logger.Info("=== Starting MCF Server ===")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Gateway
	httpGW := gateway.NewHTTPGateway(cfg.Logger)
	rtpGW := gateway.NewRTPGateway(cfg.RTPListenAddr, cfg.Logger)

	// AI Client
	aiClient, err := ai_client.NewAIClient(cfg.AIServiceAddress, cfg.Logger)
	if err != nil {
		cfg.Logger.Fatal("Failed to initialize AI client", zap.Error(err))
	}
	defer aiClient.Close()
	videoAI := ai_client.NewVideoHTTPClient(cfg.VideoAIEndpoint, cfg.VideoAITimeout)
	httpGW.SetVideoInferenceHandler(func(r *http.Request, in gateway.VideoInferenceRequest) ([]byte, error) {
		return videoAI.Infer(r.Context(), ai_client.VideoHTTPRequest{
			SessionID: in.SessionID, StreamID: in.StreamID,
			FrameID: in.FrameID, RTPTimestamp: in.RTPTimestamp,
			OriginalWidth: in.OriginalWidth, OriginalHeight: in.OriginalHeight,
			EffectType: in.EffectType, JPEG: in.JPEG,
		})
	})

	// Orchestrator
	orchestrator := logic_core.NewOrchestrator(httpGW, rtpGW, aiClient, cfg.Logger)
	
	go httpGW.StartServer(cfg.HTTPListenAddr)

	// Start RTP listener in a separate goroutine
	go orchestrator.HandleCallSession(ctx, cfg.SessionID)

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	<-sigChan

	cfg.Logger.Info("Received shutdown signal, stopping MCF Server...")
	cancel()
	time.Sleep(1 * time.Second)
	cfg.Logger.Info("=== MCF Server Stopped Successfully ===")
}

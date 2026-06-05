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
	go httpGW.StartServer(cfg.HTTPListenAddr)
	rtpGW := gateway.NewRTPGateway(cfg.RTPListenAddr, cfg.Logger)

	// AI Client
	aiClient, err := ai_client.NewAIClient(cfg.AIServiceAddress, cfg.Logger)
	if err != nil {
		cfg.Logger.Fatal("Failed to initialize AI client", zap.Error(err))
	}
	defer aiClient.Close()

	// Orchestrator
	orchestrator := logic_core.NewOrchestrator(httpGW, rtpGW, aiClient, cfg.Logger)

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

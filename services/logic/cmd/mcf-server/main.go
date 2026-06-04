package main

import (
	"context"
	"log"
	"mcf/services/logic/internal/ai_client"
	"mcf/services/logic/internal/gateway"
	"mcf/services/logic/internal/logic_core"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	log.Println("=== Starting MCF Server ===")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Gateway
	httpGW := gateway.NewHTTPGateway()
	go httpGW.StartServer("0.0.0.0:8080")
	rtpGW := gateway.NewRTPGateway("0.0.0.0:5004")

	// AI Client
	aiClient, err := ai_client.NewAIClient("127.0.0.1:50052")
	if err != nil {
		log.Fatalf("Can not init AI client")
	}
	defer aiClient.Close()

	// Orchestrator
	orchestrator := logic_core.NewOrchestrator(httpGW, rtpGW, aiClient)

	// Start RTP listener in a separate goroutine
	sessionID := "CALL-TEST-002"
	go orchestrator.HandleCallSession(ctx, sessionID)

	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	<-sigChan

	log.Println("Received shutdown signal, stopping MCF Server...")
	cancel()
	time.Sleep(1 * time.Second)
	log.Println("=== MCF Server Stopped Successfully ===")
}

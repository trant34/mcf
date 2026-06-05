package gateway

import (
	"net/http"
	"sync"

	"github.com/gorilla/websocket"
	"go.uber.org/zap"
)

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

type HTTPGateway struct {
	clients   map[*websocket.Conn]bool
	broadcast chan string
	mutex     sync.Mutex
	logger    *zap.Logger
}

func NewHTTPGateway(logger *zap.Logger) *HTTPGateway {
	gw := &HTTPGateway{
		clients:   make(map[*websocket.Conn]bool),
		broadcast: make(chan string, 100),
		logger:    logger,
	}
	go gw.runBroadcaster()
	return gw
}

func (h *HTTPGateway) runBroadcaster() {
	for {
		msg := <-h.broadcast
		h.mutex.Lock()
		for client := range h.clients {
			err := client.WriteMessage(websocket.TextMessage, []byte(msg))
			if err != nil {
				h.logger.Debug("WebSocket client disconnected", zap.Error(err))
				client.Close()
				delete(h.clients, client)
			}
		}
		h.mutex.Unlock()
	}
}

func (h *HTTPGateway) PushSubtitle(sessionID string, text string) error {
	h.logger.Debug("Pushing subtitle", zap.String("session_id", sessionID), zap.String("text", text))
	h.broadcast <- text
	return nil
}

func (h *HTTPGateway) ServeWS(w http.ResponseWriter, r *http.Request) {
	ws, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		h.logger.Error("WebSocket Upgrade Error", zap.Error(err))
		return
	}
	h.mutex.Lock()
	h.clients[ws] = true
	clientCount := len(h.clients)
	h.mutex.Unlock()
	h.logger.Info("New WebSocket client connected", zap.Int("total_clients", clientCount))
}

func (h *HTTPGateway) StartServer(addr string) {
	http.HandleFunc("/ws", h.ServeWS)
	http.Handle("/", http.FileServer(http.Dir("./web")))

	h.logger.Info("Starting HTTP Gateway server", zap.String("addr", addr))
	if err := http.ListenAndServe(addr, nil); err != nil {
		h.logger.Fatal("HTTP Gateway Server Error", zap.Error(err))
	}
}

package gateway

import (
	"log"
	"net/http"
	"sync"

	"github.com/gorilla/websocket"
)

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool { return true },
}

type HTTPGateway struct {
	clients   map[*websocket.Conn]bool
	broadcast chan string
	mutex     sync.Mutex
}

func NewHTTPGateway() *HTTPGateway {
	gw := &HTTPGateway{
		clients:   make(map[*websocket.Conn]bool),
		broadcast: make(chan string, 100),
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
				client.Close()
				delete(h.clients, client)
			}
		}
		h.mutex.Unlock()
	}
}

func (h *HTTPGateway) PushSubtitle(sessionID string, text string) error {
	log.Printf("[HTTPGW] Session [%s] - Pushing subtitle: %s\n", sessionID, text)
	h.broadcast <- text
	return nil
}

func (h *HTTPGateway) ServeWS(w http.ResponseWriter, r *http.Request) {
	ws, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("[HTTPGW] WebSocket Upgrade Error: %v\n", err)
		return
	}
	h.mutex.Lock()
	h.clients[ws] = true
	h.mutex.Unlock()
	log.Printf("[HTTPGW] New WebSocket client connected. Total clients: %d\n", len(h.clients))
}

func (h *HTTPGateway) StartServer(addr string) {
	http.HandleFunc("/ws", h.ServeWS)
	http.Handle("/", http.FileServer(http.Dir("./web")))

	log.Printf("[HTTPGW] Starting HTTP Gateway server on %s\n", addr)
	if err := http.ListenAndServe(addr, nil); err != nil {
		log.Fatalf("[HTTPGW] Server Error: %v\n", err)
	}
}

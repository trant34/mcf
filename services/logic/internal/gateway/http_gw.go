package gateway

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"sync"

	"github.com/gorilla/websocket"
	"go.uber.org/zap"
)

var upgrader = websocket.Upgrader{CheckOrigin: func(r *http.Request) bool { return true }}

type HTTPGateway struct {
	clients   map[*websocket.Conn]bool
	broadcast chan string
	mutex     sync.Mutex
	logger    *zap.Logger

	mux                   *http.ServeMux
	videoInferenceHandler func(*http.Request, VideoInferenceRequest) ([]byte, error)
}

type VideoInferenceRequest struct {
	SessionID      string
	StreamID       string
	FrameID        int64
	RTPTimestamp   int64
	OriginalWidth  int32
	OriginalHeight int32
	EffectType     string
	JPEG           []byte
}

type legacyVideoJSON struct {
	SessionID      string `json:"session_id"`
	StreamID       string `json:"stream_id,omitempty"`
	FrameID        int64  `json:"frame_id"`
	RTPTimestamp   int64  `json:"rtp_timestamp"`
	OriginalWidth  int32  `json:"original_width"`
	OriginalHeight int32  `json:"original_height"`
	EffectType     string `json:"effect_type,omitempty"`
	ImageBase64    string `json:"image_base64"`
}

func NewHTTPGateway(logger *zap.Logger) *HTTPGateway {
	gw := &HTTPGateway{
		clients:   make(map[*websocket.Conn]bool),
		broadcast: make(chan string, 100),
		logger:    logger,
		mux:       http.NewServeMux(),
	}
	gw.mux.HandleFunc("/ws", gw.ServeWS)
	gw.mux.HandleFunc("/v1/video/infer", gw.ServeVideoInference)
	gw.mux.HandleFunc("/v1/video/segmentation", gw.ServeVideoInference) // legacy alias
	gw.mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	})
	gw.mux.Handle("/", http.FileServer(http.Dir("./web")))
	go gw.runBroadcaster()
	return gw
}

func (h *HTTPGateway) SetVideoInferenceHandler(handler func(*http.Request, VideoInferenceRequest) ([]byte, error)) {
	h.videoInferenceHandler = handler
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

func parseIntHeader(r *http.Request, name string, required bool) (int64, error) {
	raw := r.Header.Get(name)
	if raw == "" {
		if required {
			return 0, fmt.Errorf("missing header %s", name)
		}
		return 0, nil
	}
	v, err := strconv.ParseInt(raw, 10, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid %s: %w", name, err)
	}
	return v, nil
}

func (h *HTTPGateway) decodeVideoRequest(r *http.Request) (VideoInferenceRequest, error) {
	if r.Header.Get("Content-Type") == "application/json" {
		var legacy legacyVideoJSON
		if err := json.NewDecoder(io.LimitReader(r.Body, 20<<20)).Decode(&legacy); err != nil {
			return VideoInferenceRequest{}, err
		}
		image, err := base64.StdEncoding.DecodeString(legacy.ImageBase64)
		if err != nil {
			return VideoInferenceRequest{}, err
		}
		return VideoInferenceRequest{
			SessionID: legacy.SessionID, StreamID: legacy.StreamID,
			FrameID: legacy.FrameID, RTPTimestamp: legacy.RTPTimestamp,
			OriginalWidth: legacy.OriginalWidth, OriginalHeight: legacy.OriginalHeight,
			EffectType: legacy.EffectType, JPEG: image,
		}, nil
	}

	frameID, err := parseIntHeader(r, "X-Frame-ID", true)
	if err != nil {
		return VideoInferenceRequest{}, err
	}
	rtpTS, err := parseIntHeader(r, "X-RTP-Timestamp", true)
	if err != nil {
		return VideoInferenceRequest{}, err
	}
	width, err := parseIntHeader(r, "X-Original-Width", false)
	if err != nil {
		return VideoInferenceRequest{}, err
	}
	height, err := parseIntHeader(r, "X-Original-Height", false)
	if err != nil {
		return VideoInferenceRequest{}, err
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, 12<<20))
	if err != nil {
		return VideoInferenceRequest{}, err
	}
	if len(body) == 0 {
		return VideoInferenceRequest{}, fmt.Errorf("empty JPEG body")
	}

	return VideoInferenceRequest{
		SessionID: r.Header.Get("X-Session-ID"),
		StreamID:  r.Header.Get("X-Stream-ID"),
		FrameID:   frameID, RTPTimestamp: rtpTS,
		OriginalWidth: int32(width), OriginalHeight: int32(height),
		EffectType: r.Header.Get("X-Effect-Type"), JPEG: body,
	}, nil
}
func (h *HTTPGateway) ServeVideoInference(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	if h.videoInferenceHandler == nil {
		http.Error(w, "video inference handler unavailable", http.StatusServiceUnavailable)
		return
	}
	in, err := h.decodeVideoRequest(r)
	if err != nil {
		http.Error(w, "invalid video request: "+err.Error(), http.StatusBadRequest)
		return
	}
	if in.SessionID == "" {
		http.Error(w, "missing session id", http.StatusBadRequest)
		return
	}
	out, err := h.videoInferenceHandler(r, in)
	if err != nil {
		h.logger.Error("Video inference failed", zap.String("session_id", in.SessionID), zap.Int64("frame_id", in.FrameID), zap.Error(err))
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(out)
}

// func (h *HTTPGateway) StartServer(addr string) {
// 	http.HandleFunc("/ws", h.ServeWS)
// 	http.Handle("/", http.FileServer(http.Dir("./web")))

//		h.logger.Info("Starting HTTP Gateway server", zap.String("addr", addr))
//		if err := http.ListenAndServe(addr, nil); err != nil {
//			h.logger.Fatal("HTTP Gateway Server Error", zap.Error(err))
//		}
//	}
func (h *HTTPGateway) StartServer(addr string) error {
	h.logger.Info("Starting HTTP Gateway server", zap.String("addr", addr))
	return http.ListenAndServe(addr, h.mux)
}

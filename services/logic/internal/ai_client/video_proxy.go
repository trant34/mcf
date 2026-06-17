package ai_client

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"
)

// VideoHTTPClient forwards sampled JPEG frames from MCF HTTPGW to the
// AI Engine video HTTP endpoint. Audio continues to use the existing gRPC client.
type VideoHTTPClient struct {
	endpoint string
	client   *http.Client
}

type VideoHTTPRequest struct {
	SessionID      string
	StreamID       string
	FrameID        int64
	RTPTimestamp   int64
	OriginalWidth  int32
	OriginalHeight int32
	EffectType     string
	JPEG           []byte
}

func NewVideoHTTPClient(endpoint string, timeout time.Duration) *VideoHTTPClient {
	transport := &http.Transport{
		MaxIdleConns:        256,
		MaxIdleConnsPerHost: 128,
		IdleConnTimeout:     90 * time.Second,
	}
	return &VideoHTTPClient{
		endpoint: endpoint,
		client: &http.Client{
			Transport: transport,
			Timeout:   timeout,
		},
	}
}

func (c *VideoHTTPClient) Infer(ctx context.Context, in VideoHTTPRequest) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.endpoint, bytes.NewReader(in.JPEG))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "image/jpeg")
	req.Header.Set("X-Session-ID", in.SessionID)
	req.Header.Set("X-Stream-ID", in.StreamID)
	req.Header.Set("X-Frame-ID", strconv.FormatInt(in.FrameID, 10))
	req.Header.Set("X-RTP-Timestamp", strconv.FormatInt(in.RTPTimestamp, 10))
	req.Header.Set("X-Original-Width", strconv.FormatInt(int64(in.OriginalWidth), 10))
	req.Header.Set("X-Original-Height", strconv.FormatInt(int64(in.OriginalHeight), 10))
	req.Header.Set("X-Effect-Type", in.EffectType)

	resp, err := c.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("video AI returned %s: %s", resp.Status, string(body))
	}
	return body, nil
}

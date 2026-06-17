package gateway

// import (
// 	"context"
// 	"errors"
// 	"net"
// 	"time"

// 	"github.com/pion/rtp"
// 	"go.uber.org/zap"
// )

// type VideoRTPPacket struct {
// 	SessionID      string
// 	SequenceNumber uint16
// 	Timestamp      uint32
// 	SSRC           uint32
// 	PayloadType    uint8
// 	Marker         bool
// 	Payload        []byte
// 	ReceivedAt     time.Time
// 	RemoteAddress  string
// }

// type VideoRTPGateway struct {
// 	Address string
// 	logger  *zap.Logger
// }

// func NewVideoRTPGateway(address string, logger *zap.Logger) *VideoRTPGateway {
// 	return &VideoRTPGateway{Address: address, logger: logger}
// }

// func (g *VideoRTPGateway) StartListening(ctx context.Context, sessionID string, out chan<- VideoRTPPacket) error {
// 	addr, err := net.ResolveUDPAddr("udp", g.Address)
// 	if err != nil {
// 		return err
// 	}
// 	conn, err := net.ListenUDP("udp", addr)
// 	if err != nil {
// 		return err
// 	}
// 	defer conn.Close()
// 	_ = conn.SetReadBuffer(4 * 1024 * 1024)

// 	g.logger.Info("Listening video RTP", zap.String("addr", g.Address), zap.String("session_id", sessionID))
// 	go func() { <-ctx.Done(); _ = conn.Close() }()

// 	buf := make([]byte, 64*1024)
// 	for {
// 		n, remote, err := conn.ReadFromUDP(buf)
// 		if err != nil {
// 			if ctx.Err() != nil || errors.Is(err, net.ErrClosed) {
// 				return nil
// 			}
// 			g.logger.Warn("video RTP read failed", zap.Error(err))
// 			continue
// 		}
// 		var pkt rtp.Packet
// 		if err := pkt.Unmarshal(buf[:n]); err != nil || len(pkt.Payload) == 0 {
// 			continue
// 		}
// 		item := VideoRTPPacket{
// 			SessionID:      sessionID,
// 			SequenceNumber: pkt.SequenceNumber,
// 			Timestamp:      pkt.Timestamp,
// 			SSRC:           pkt.SSRC,
// 			PayloadType:    pkt.PayloadType,
// 			Marker:         pkt.Marker,
// 			Payload:        append([]byte(nil), pkt.Payload...),
// 			ReceivedAt:     time.Now(),
// 			RemoteAddress:  remote.String(),
// 		}
// 		select {
// 		case out <- item:
// 		case <-ctx.Done():
// 			return nil
// 		}
// 	}
// }

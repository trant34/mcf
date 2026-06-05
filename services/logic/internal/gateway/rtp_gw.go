package gateway

import (
	"context"
	"net"

	"github.com/pion/rtp"
	"go.uber.org/zap"
)

type RTPGateway struct {
	Address string
	logger  *zap.Logger
}

func NewRTPGateway(address string, logger *zap.Logger) *RTPGateway {
	return &RTPGateway{
		Address: address,
		logger:  logger,
	}
}

func (r *RTPGateway) StartListening(ctx context.Context, sessionID string, audioChan chan<- []byte) error {
	udpAddr, err := net.ResolveUDPAddr("udp", r.Address)
	if err != nil {
		return err
	}

	conn, err := net.ListenUDP("udp", udpAddr)
	if err != nil {
		return err
	}
	defer conn.Close()

	r.logger.Info("Listening RTP", zap.String("addr", r.Address), zap.String("session_id", sessionID))

	go func() {
		<-ctx.Done()
		conn.Close()
	}()

	buffer := make([]byte, 2048)

	for {
		n, _, err := conn.ReadFromUDP(buffer)
		if err != nil {
			if ctx.Err() != nil {
				r.logger.Debug("Close UDP connection", zap.String("session_id", sessionID))
				return nil
			}
			r.logger.Error("Reading UDP Error", zap.Error(err))
			continue
		}

		packet := &rtp.Packet{}
		if err := packet.Unmarshal(buffer[:n]); err != nil {
			continue
		}

		payload := packet.Payload
		if len(payload) == 0 {
			continue
		}

		payloadCopy := make([]byte, len(payload))
		copy(payloadCopy, payload)

		audioChan <- payloadCopy
	}
}

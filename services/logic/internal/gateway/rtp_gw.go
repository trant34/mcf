package gateway

import (
	"context"
	"log"
	"net"

	"github.com/pion/rtp"
)

type RTPGateway struct {
	Address string
}

func NewRTPGateway(address string) *RTPGateway {
	return &RTPGateway{
		Address: address,
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

	log.Printf("[RTPGateway] Listening RTP on %s for Session [%s]...\n", r.Address, sessionID)

	go func() {
		<-ctx.Done()
		conn.Close()
	}()

	buffer := make([]byte, 2048)

	for {
		n, _, err := conn.ReadFromUDP(buffer)
		if err != nil {
			if ctx.Err() != nil {
				log.Printf("[RTPGateway] Close UDP connection for Session [%s].\n", sessionID)
				return nil
			}
			log.Printf("[RTPGateway] Reading UDP Error: %v\n", err)
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
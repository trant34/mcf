// Package video: cdecoder.go wraps the C++ decode core (cdecoder/bridge.*)
// via cgo. See rtpgw_design_v3.md section 37.
//
// v3.1 had RTPGW decode H264 with a pure-Go reimplementation (AU
// assembler + H264 depacketizer + ffmpeg subprocess, all in processor.go).
// v3.2 replaces that with the ACTUAL C++ decode code mf_cpp used to run
// locally (copied into cdecoder/, adapted only to drop the cv::Mat
// dependency -- see cdecoder/ffmpeg_decoder.hpp for why), driven from Go
// through this cgo bridge. The pure-Go implementation is removed, not kept
// side-by-side, to avoid two subtly-different decode paths existing in the
// same codebase.
//
// BUILD REQUIREMENT: this file requires CGO_ENABLED=1 and a C++17 compiler
// on the build machine. It does NOT require OpenCV, libavcodec, or any
// other external library -- cdecoder/ only uses the C++ standard library
// and POSIX (fork/exec/pipe), matching mf_cpp's original ffmpeg_decoder.cpp
// dependency footprint minus OpenCV. `ffmpeg` (the CLI binary) must still
// be on PATH at runtime, exactly as before.
package video

/*
#cgo CXXFLAGS: -std=c++17
#cgo LDFLAGS: -lstdc++
#include <stdlib.h>
#include "cdecoder_bridge.h"
*/
import "C"

import (
	"fmt"
	"sync"
	"unsafe"
)

// CDecoder is the Go-facing handle for one video stream's decode session,
// backed by the C++ bridge. Not safe for concurrent Push/PollFrame calls
// from multiple goroutines without external synchronization (matches the
// "1 stream = 1 owner goroutine" rule from rtpgw_design_v3.md section 14 --
// callers should already only ever touch one CDecoder from one goroutine).
type CDecoder struct {
	handle C.rtpgw_decoder_t
	width  int
	height int

	closeOnce sync.Once
}

// NewCDecoder starts an ffmpeg subprocess (via the C++ bridge) decoding to
// width x height bgr24 frames. width/height MUST match what the RtpOpen
// message's decode_width/decode_height carried (i.e. MF's own
// --width/--height), or MF's compose()/writer_.write() will receive
// mis-sized frame_data.
func NewCDecoder(width, height int, ffmpegBinary string) (*CDecoder, error) {
	cBin := C.CString(ffmpegBinary)
	defer C.free(unsafe.Pointer(cBin))

	handle := C.rtpgw_decoder_create(C.int(width), C.int(height), cBin, C.int(0))
	if handle == nil {
		return nil, fmt.Errorf("rtpgw_decoder_create failed (width=%d height=%d ffmpeg=%q)", width, height, ffmpegBinary)
	}
	return &CDecoder{handle: handle, width: width, height: height}, nil
}

// PushPacket feeds one RTP packet's fields into the C++ decode chain
// (AU assembly -> H264 depacketize -> ffmpeg decode).
func (d *CDecoder) PushPacket(seq uint16, ts uint32, ssrc uint32, marker bool, payloadType uint8, payload []byte) {
	var payloadPtr *C.uint8_t
	if len(payload) > 0 {
		payloadPtr = (*C.uint8_t)(unsafe.Pointer(&payload[0]))
	}
	markerInt := C.int(0)
	if marker {
		markerInt = C.int(1)
	}
	C.rtpgw_decoder_push_packet(d.handle, C.uint16_t(seq), C.uint32_t(ts), C.uint32_t(ssrc),
		markerInt, C.uint8_t(payloadType), payloadPtr, C.int(len(payload)))
}

// PollFrame non-blockingly checks for one decoded bgr24 frame. Returns
// ok=false if none was ready yet.
func (d *CDecoder) PollFrame() (frame []byte, rtpTimestamp uint32, ok bool) {
	buf := make([]byte, d.width*d.height*3)
	var cRTS C.uint32_t
	ret := C.rtpgw_decoder_poll_frame(d.handle, (*C.uint8_t)(unsafe.Pointer(&buf[0])), C.int(len(buf)), &cRTS)
	if ret != 1 {
		return nil, 0, false
	}
	return buf, uint32(cRTS), true
}

// Close stops the underlying ffmpeg subprocess. Safe to call multiple
// times; subsequent calls are no-ops.
func (d *CDecoder) Close() {
	d.closeOnce.Do(func() {
		C.rtpgw_decoder_close(d.handle)
		C.rtpgw_decoder_destroy(d.handle)
	})
}

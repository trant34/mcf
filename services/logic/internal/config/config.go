package config

import (
	"os"
	"strconv"
	"time"

	"go.uber.org/zap"
	"go.uber.org/zap/zapcore"
	"gopkg.in/natefinch/lumberjack.v2"
)

type Config struct {
	// Giữ nguyên
	HTTPListenAddr   string
	RTPListenAddr    string
	AIServiceAddress string
	SessionID        string
	LogLevel         string
	Logger           *zap.Logger

	// VideoRTPListenAddr string
	// VideoEffect string  // "bg_blur" | "bg_replace" | "bg_remove"
	VideoAITimeout       time.Duration
	VideoEffect          string
	VideoInferenceWidth  uint32
	VideoInferenceHeight uint32
	VideoInferenceFPS    uint32
	VideoMaskThreshold   float32

	// VideoIngestListenAddr is the MF -> RTPGW gRPC bidirectional stream
	// (api/proto/video_ingest.proto). VideoFrameWidth/Height are now only
	// a FALLBACK default (v3.2, rtpgw_design_v3.md section 37): the real
	// decode resolution normally travels per-call in RtpOpen.decode_width/
	// decode_height (set from MF's own --width/--height), which removes
	// the old footgun of two independently-configured values that had to
	// be kept in sync by hand.
	VideoIngestListenAddr string
	VideoFrameWidth       int
	VideoFrameHeight      int
	VideoFFmpegBinary     string
	VideoJPEGQuality      int
}

func Load() *Config {
	logLevel := getEnv("LOG_LEVEL", "info")
	logger := initLogger(logLevel)
	timeoutMS, _ := strconv.Atoi(getEnv("VIDEO_AI_TIMEOUT_MS", "1500"))

	cfg := &Config{
		HTTPListenAddr:   getEnv("HTTP_LISTEN_ADDR", "0.0.0.0:8080"),
		RTPListenAddr:    getEnv("RTP_LISTEN_ADDR", "0.0.0.0:5004"),
		AIServiceAddress: getEnv("AI_SERVICE_ADDRESS", "127.0.0.1:50052"),
		SessionID:        getEnv("SESSION_ID", "CALL-TEST-001"),
		LogLevel:         logLevel,
		Logger:           logger,
		// VideoAIEndpoint:  getEnv("VIDEO_AI_ENDPOINT", "http://127.0.0.1:50053/v1/video/infer"),
		// VideoAITimeout:   time.Duration(timeoutMS) * time.Millisecond,
		VideoAITimeout:       time.Duration(timeoutMS) * time.Millisecond,
		VideoEffect:          getEnv("VIDEO_EFFECT", "bg_replace"),
		VideoInferenceWidth:  uint32(getEnvInt("VIDEO_INFER_WIDTH", 256)),
		VideoInferenceHeight: uint32(getEnvInt("VIDEO_INFER_HEIGHT", 144)),
		VideoInferenceFPS:    uint32(getEnvInt("VIDEO_INFER_FPS", 5)),
		VideoMaskThreshold:   float32(getEnvFloat("VIDEO_MASK_THRESHOLD", 0.5)),

		VideoIngestListenAddr: getEnv("VIDEO_INGEST_LISTEN_ADDR", "0.0.0.0:50060"),
		VideoFrameWidth:       getEnvInt("VIDEO_FRAME_WIDTH", 240),
		VideoFrameHeight:      getEnvInt("VIDEO_FRAME_HEIGHT", 320),
		VideoFFmpegBinary:     getEnv("VIDEO_FFMPEG_BINARY", "ffmpeg"),
		VideoJPEGQuality:      getEnvInt("VIDEO_JPEG_QUALITY", 80),
	}

	logger.Info("Configuration loaded",
		zap.String("http_listen_addr", cfg.HTTPListenAddr),
		zap.String("rtp_listen_addr", cfg.RTPListenAddr),
		zap.String("ai_service_addr", cfg.AIServiceAddress),
		// zap.String("video_ai_endpoint", cfg.VideoAIEndpoint),
		// zap.Duration("video_ai_timeout", cfg.VideoAITimeout),
		zap.Duration("video_ai_timeout", cfg.VideoAITimeout),
		zap.String("video_effect", cfg.VideoEffect),
		zap.Uint32("video_infer_width", cfg.VideoInferenceWidth),
		zap.Uint32("video_infer_height", cfg.VideoInferenceHeight),
		zap.Uint32("video_infer_fps", cfg.VideoInferenceFPS),
		zap.Float32("video_mask_threshold", cfg.VideoMaskThreshold),

		zap.String("session_id", cfg.SessionID),
		zap.String("log_level", cfg.LogLevel),

		zap.String("video_ingest_listen_addr", cfg.VideoIngestListenAddr),
		zap.Int("video_frame_width", cfg.VideoFrameWidth),
		zap.Int("video_frame_height", cfg.VideoFrameHeight),
	)

	return cfg
}

func initLogger(level string) *zap.Logger {
	var zapLevel zapcore.Level
	switch level {
	case "debug":
		zapLevel = zapcore.DebugLevel
	case "info":
		zapLevel = zapcore.InfoLevel
	case "warn":
		zapLevel = zapcore.WarnLevel
	case "error":
		zapLevel = zapcore.ErrorLevel
	default:
		zapLevel = zapcore.InfoLevel
	}

	encoderConfig := zap.NewDevelopmentEncoderConfig()
	encoderConfig.EncodeTime = zapcore.ISO8601TimeEncoder
	encoderConfig.EncodeLevel = zapcore.CapitalLevelEncoder
	encoderConfig.EncodeCaller = zapcore.ShortCallerEncoder
	encoder := zapcore.NewConsoleEncoder(encoderConfig)

	// File rotater
	w := zapcore.AddSync(&lumberjack.Logger{
		Filename:   "logs/logic_service.log",
		MaxSize:    100, // megabytes
		MaxBackups: 3,
		MaxAge:     28, // days
		Compress:   true,
	})

	core := zapcore.NewTee(
		zapcore.NewCore(encoder, zapcore.AddSync(os.Stdout), zapLevel),
		zapcore.NewCore(encoder, w, zapLevel),
	)

	logger := zap.New(core, zap.AddCaller())
	return logger
}

func getEnv(key, defaultValue string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return defaultValue
}

func getEnvInt(key string, defaultValue int) int {
	raw := getEnv(key, strconv.Itoa(defaultValue))
	value, err := strconv.Atoi(raw)
	if err != nil {
		return defaultValue
	}
	return value
}

func getEnvFloat(key string, defaultValue float64) float64 {
	raw := getEnv(key, strconv.FormatFloat(defaultValue, 'f', -1, 64))
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return defaultValue
	}
	return value
}

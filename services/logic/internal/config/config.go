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
	VideoAIEndpoint string
	VideoAITimeout  time.Duration
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
		VideoAIEndpoint:  getEnv("VIDEO_AI_ENDPOINT", "http://127.0.0.1:50053/v1/video/infer"),
		VideoAITimeout:   time.Duration(timeoutMS) * time.Millisecond,
	}

	logger.Info("Configuration loaded",
		zap.String("http_listen_addr", cfg.HTTPListenAddr),
		zap.String("rtp_listen_addr", cfg.RTPListenAddr),
		zap.String("ai_service_addr", cfg.AIServiceAddress),
		zap.String("video_ai_endpoint", cfg.VideoAIEndpoint),
		zap.Duration("video_ai_timeout", cfg.VideoAITimeout),
		zap.String("session_id", cfg.SessionID),
		zap.String("log_level", cfg.LogLevel),
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

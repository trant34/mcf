import os
import logging
import sys
from logging.handlers import RotatingFileHandler
from datetime import datetime

# Force UTF-8 for console output to avoid 'charmap' errors on Windows
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding='utf-8')

class CustomFormatter(logging.Formatter):
    """Human-readable formatter: [LEVEL] timestamp | logger | message"""
    
    def format(self, record):
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        level = record.levelname
        msg = record.getMessage()
        
        # Add basic fields
        formatted_msg = f"[{level}] {timestamp} | {record.name} | {msg}"
        
        # Add extra context if present
        if hasattr(record, "session_id"):
            formatted_msg += f" | session_id={record.session_id}"
        if hasattr(record, "context"):
            formatted_msg += f" | context={record.context}"
            
        if record.exc_info:
            formatted_msg += f"\n{self.formatException(record.exc_info)}"
            
        return formatted_msg


class Config:
    """AI Engine configuration loaded from environment variables."""
    
    # gRPC Server Configuration
    GRPC_SERVER_ADDRESS = os.getenv("GRPC_SERVER_ADDRESS", "0.0.0.0:50052")
    GRPC_MAX_WORKERS = int(os.getenv("GRPC_MAX_WORKERS", "4"))
    
    # Model Configuration
    DEVICE = os.getenv("DEVICE", "cuda")
    WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")
    COMPUTE_TYPE = os.getenv("COMPUTE_TYPE", "int8")
    
    # Audio Processing Configuration
    SAMPLE_RATE = int(os.getenv("SAMPLE_RATE", "16000"))
    SILENCE_THRESHOLD = float(os.getenv("SILENCE_THRESHOLD", "0.012"))
    SILENCE_TIMEOUT_SECONDS = float(os.getenv("SILENCE_TIMEOUT_SECONDS", "0.6"))
    MAX_AUDIO_SECONDS = float(os.getenv("MAX_AUDIO_SECONDS", "15.0"))
    MIN_AUDIO_SECONDS = float(os.getenv("MIN_AUDIO_SECONDS", "1.0"))
    
    # ── Video Processing Configuration ──────────────────────────
    SEG_MODEL_PATH  = os.getenv("SEG_MODEL_PATH",  "../model_checkpoints/selfie_segmenter.tflite")
    INFER_FPS       = int(os.getenv("INFER_FPS",   "10"))
    INFER_WIDTH     = int(os.getenv("INFER_WIDTH",  "256"))
    INFER_HEIGHT    = int(os.getenv("INFER_HEIGHT", "144"))
    VIDEO_EFFECT    = os.getenv("VIDEO_EFFECT",    "bg_blur")  # bg_blur|bg_replace|bg_remove
    BG_IMAGE_PATH   = os.getenv("BG_IMAGE_PATH",   "")

    # VAD (Voice Activity Detection) Configuration
    VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.5"))
    
    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()
    
    @classmethod
    def setup_logger(cls, name="ai_engine"):
        """Setup human-readable logger with file output and rotation."""
        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, cls.LOG_LEVEL))
        
        # Remove existing handlers
        logger.handlers.clear()
        
        # Create logs directory if not exists
        os.makedirs("logs", exist_ok=True)
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(CustomFormatter())
        logger.addHandler(console_handler)
        
        # Rotating File handler
        file_handler = RotatingFileHandler(
            "logs/ai_engine.log",
            maxBytes=100*1024*1024, # 100MB
            backupCount=3,
            encoding='utf-8'
        )
        file_handler.setFormatter(CustomFormatter())
        logger.addHandler(file_handler)
        
        return logger
    
    @classmethod
    def log_config(cls, logger):
        """Log current configuration."""
        logger.info("AI Engine Configuration loaded", extra={
            "context": {
                "grpc_server_address": cls.GRPC_SERVER_ADDRESS,
                "grpc_max_workers": cls.GRPC_MAX_WORKERS,
                "device": cls.DEVICE,
                "whisper_model_size": cls.WHISPER_MODEL_SIZE,
                "compute_type": cls.COMPUTE_TYPE,
                "sample_rate": cls.SAMPLE_RATE,
                "silence_threshold": cls.SILENCE_THRESHOLD,
                "silence_timeout": cls.SILENCE_TIMEOUT_SECONDS,
                "max_audio": cls.MAX_AUDIO_SECONDS,
                "min_audio": cls.MIN_AUDIO_SECONDS,
                "vad_threshold": cls.VAD_THRESHOLD,
                "log_level": cls.LOG_LEVEL,
            }
        })

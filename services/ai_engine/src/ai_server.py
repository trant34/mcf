import grpc 
from concurrent import futures 
import time 
import sys 
import os 
import numpy as np 
import torch 
import audioop 
from faster_whisper import WhisperModel
from silero_vad import load_silero_vad, get_speech_timestamps

current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(current_dir, 'pb'))

from pb import ai_service_pb2
from pb import ai_service_pb2_grpc
from config import Config

# Setup structured logger
logger = Config.setup_logger("ai_engine")
logger.info("Loading Silero VAD (Pip package) to CPU...")
vad_model = load_silero_vad()

logger.info("Loading Whisper model", extra={"context": {
    "model_size": Config.WHISPER_MODEL_SIZE,
    "device": Config.DEVICE,
    "compute_type": Config.COMPUTE_TYPE
}})
whisper_model = WhisperModel(Config.WHISPER_MODEL_SIZE, device=Config.DEVICE, compute_type=Config.COMPUTE_TYPE)
logger.info("Whisper model loaded, ready to process streams")

Config.log_config(logger)

def decode_pcma_chunk(payload_bytes):
    if not payload_bytes:
        return np.array([], dtype=np.float32)
    # Decode PCMA -> PCM16
    pcm_8k = audioop.alaw2lin(payload_bytes, 2)
    # Resample 8kHz -> 16kHz 
    pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
    # Normalize Float32
    return np.frombuffer(pcm_16k, dtype=np.int16).astype(np.float32) / 32768.0

class RealTranslationService(ai_service_pb2_grpc.TranslationServiceServicer): 
    def ProcessMediaStream(self, request_iterator, context):
        session_id = "UNKNOWN"
        logger.info("Client connected to bi-directional stream")

        SAMPLE_RATE = Config.SAMPLE_RATE
        SILENCE_THRESHOLD = Config.SILENCE_THRESHOLD
        SILENCE_TIMEOUT_SAMPLES = int(Config.SILENCE_TIMEOUT_SECONDS * SAMPLE_RATE)
        MAX_AUDIO_SAMPLES = int(Config.MAX_AUDIO_SECONDS * SAMPLE_RATE)
        MIN_AUDIO_SAMPLES = int(Config.MIN_AUDIO_SECONDS * SAMPLE_RATE)

        current_pcm_samples = []
        consecutive_silence_samples = 0 
        has_speech = False

        try: 
            for request in request_iterator: 
                session_id = request.session_id

                if request.is_eos: 
                    logger.info("Received end-of-stream signal", extra={"session_id": session_id})
                    break
                
                if request.HasField("config"): 
                    session_source_lang = request.config.source_language
                    logger.info("Session configured", extra={"session_id": session_id, "context": {
                        "source_language": request.config.source_language,
                        "target_language": request.config.target_language
                    }})
                    continue

                if request.HasField("audio_chunk"): 
                    raw_bytes = request.audio_chunk 
                    
                    # decode 
                    chunk_np = decode_pcma_chunk(raw_bytes)

                    if len(chunk_np) > 0: 
                        current_pcm_samples.extend(chunk_np)

                        rms = np.sqrt(np.mean(chunk_np ** 2))
                        if rms < SILENCE_THRESHOLD:
                            consecutive_silence_samples += len(chunk_np)
                        else: 
                            consecutive_silence_samples = 0 
                            has_speech = True

                    trigger_split = False 
                    if has_speech and consecutive_silence_samples >= SILENCE_TIMEOUT_SAMPLES:
                        trigger_split = True
                    elif len(current_pcm_samples) >= MAX_AUDIO_SAMPLES: 
                        trigger_split = True

                    if trigger_split: 
                        if len(current_pcm_samples) < MIN_AUDIO_SAMPLES: 
                            current_pcm_samples = []
                            consecutive_silence_samples = 0
                            has_speech = False 
                            continue

                        audio_chunk = np.array(current_pcm_samples, dtype=np.float32)

                        current_pcm_samples = []
                        consecutive_silence_samples = 0 
                        has_speech = False 

                        tensor_audio = torch.from_numpy(audio_chunk)
                        timestamps = get_speech_timestamps(tensor_audio, vad_model, sampling_rate=SAMPLE_RATE, threshold=Config.VAD_THRESHOLD)

                        if len(timestamps) > 0: 
                            inf_start = time.time()

                            # --- ASR --- 
                            segments_vi, _ = whisper_model.transcribe(
                                audio_chunk,
                                task="transcribe",
                                language=session_source_lang,
                                beam_size=1,
                                vad_filter=False
                            )
                            text_vi = "".join([seg.text for seg in segments_vi]).strip()

                            if text_vi: 
                                # --- Translate --- 
                                segments_en, _ = whisper_model.transcribe(
                                    audio_chunk,
                                    task="translate",
                                    language=session_source_lang,
                                    beam_size=1,
                                    vad_filter=False
                                )
                                text_en = "".join([seg.text for seg in segments_en]).strip()

                                latency = (time.time() - inf_start) * 1000
                                duration = len(audio_chunk) / SAMPLE_RATE

                                logger.info("Inference completed", extra={"session_id": session_id, "context": {
                                    "duration_s": round(duration, 1),
                                    "latency_ms": round(latency, 0),
                                    "source_text": text_vi,
                                    "translated_text": text_en
                                }})

                                combined_text = f"{text_vi} \n=> {text_en}"

                                yield ai_service_pb2.MediaResponse(
                                    session_id=session_id,
                                    translated_text=combined_text,
                                    is_final=True
                                )

        except Exception as e: 
            logger.error("Error processing stream", extra={"session_id": session_id}, exc_info=True)

        logger.info("Stream closed", extra={"session_id": session_id})

    @staticmethod
    def serve():
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=Config.GRPC_MAX_WORKERS))
        ai_service_pb2_grpc.add_TranslationServiceServicer_to_server(RealTranslationService(), server)
        server.add_insecure_port(Config.GRPC_SERVER_ADDRESS)

        logger.info("gRPC Server starting", extra={"context": {
            "address": Config.GRPC_SERVER_ADDRESS,
            "max_workers": Config.GRPC_MAX_WORKERS
        }})
        server.start()
        try: 
            server.wait_for_termination()
        except KeyboardInterrupt: 
            logger.info("Stopping gRPC server...")
            server.stop(0)

if __name__ == "__main__":
    RealTranslationService.serve()
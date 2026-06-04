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

print("[AI Engine] Loading Silero VAD (Pip package) to CPU...", flush=True)
vad_model = load_silero_vad()

print("[AI Engine] Loading Whisper (INT8) to GPU...", flush=True)
whisper_model = WhisperModel("small", device="cuda", compute_type="int8")
print("[AI Engine] Ready receiving!", flush=True)

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
        print("\n[AI Engine] Client connected Bi-directional stream", flush=True)

        SAMPLE_RATE = 16000
        SILENCE_THRESHOLD = 0.012
        SILENCE_TIMEOUT_SAMPLES = int(0.6 * SAMPLE_RATE)
        MAX_AUDIO_SAMPLES = int(15.0 * SAMPLE_RATE)
        MIN_AUDIO_SAMPLES = int(1.0 * SAMPLE_RATE)

        current_pcm_samples = []
        consecutive_silence_samples = 0 
        has_speech = False

        try: 
            for request in request_iterator: 
                session_id = request.session_id

                if request.is_eos: 
                    print(f"[AI Engine] Receive ending signal from session {session_id}", flush=True)
                    break
                
                if request.HasField("config"): 
                    print(f"[AI Engine] Session config: {request.config.source_language} -> {request.config.target_language}", flush=True)
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
                        timestamps = get_speech_timestamps(tensor_audio, vad_model, sampling_rate=SAMPLE_RATE, threshold=0.5)

                        if len(timestamps) > 0: 
                            inf_start = time.time()

                            # --- ASR --- 
                            segments_vi, _ = whisper_model.transcribe(
                                audio_chunk,
                                task="transcribe",
                                language="vi",
                                beam_size=1,
                                vad_filter=False
                            )
                            text_vi = "".join([seg.text for seg in segments_vi]).strip()

                            if text_vi: 
                                # --- Translate --- 
                                segments_en, _ = whisper_model.transcribe(
                                    audio_chunk,
                                    task="translate",
                                    language="vi",
                                    beam_size=1,
                                    vad_filter=False
                                )
                                text_en = "".join([seg.text for seg in segments_en]).strip()

                                latency = (time.time() - inf_start) * 1000
                                duration = len(audio_chunk) / SAMPLE_RATE

                                print(f"\n[Dual-Pass] Audio: {duration:.1f}s | Latency: {latency:.0f}ms")
                                print(f"  VI 🇻🇳: {text_vi}")
                                print(f"  EN 🇬🇧: {text_en}")

                                combined_text = f"{text_vi} \n=> {text_en}"

                                yield ai_service_pb2.MediaResponse(
                                    session_id=session_id,
                                    translated_text=combined_text,
                                    is_final=True
                                )

        except Exception as e: 
            print(f"[AI Engine] Error stream session {session_id}: {e}", flush=True)

        print(f"[AI Engine] Stream close for session {session_id}", flush=True)

    @staticmethod
    def serve():
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        ai_service_pb2_grpc.add_TranslationServiceServicer_to_server(RealTranslationService(), server)
        server.add_insecure_port('127.0.0.1:50052')

        print("[AI Engine] gRPC Server listening on port 50052...", flush=True)
        server.start()
        try: 
            server.wait_for_termination()
        except KeyboardInterrupt: 
            print("[AI Engine] Stopping gRPC server...", flush=True)
            server.stop(0)

if __name__ == "__main__":
    RealTranslationService.serve()
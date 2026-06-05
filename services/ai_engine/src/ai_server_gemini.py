# import grpc 
# from concurrent import futures 
# import time 
# import sys 
# import os 
# import numpy as np 
# import torch 
# import audioop 
# import io
# import wave
# from silero_vad import load_silero_vad, get_speech_timestamps

# from google import genai
# from google.genai import types

# current_dir = os.path.dirname(os.path.abspath(__file__))
# sys.path.append(os.path.join(current_dir, 'pb'))

# from pb import ai_service_pb2
# from pb import ai_service_pb2_grpc

# # ====================================================================
# # CẤU HÌNH API & MÔ HÌNH
# # ====================================================================
# # SECURITY FIX: Load API key from environment variables
# GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") 
# client = genai.Client(api_key=GEMINI_API_KEY)

# print("[AI Engine] Loading Silero VAD to CPU...", flush=True)
# vad_model = load_silero_vad()

# # Whisper has been entirely removed to save VRAM and rely purely on Gemini
# print("[AI Engine] Ready receiving!", flush=True)

# def decode_pcma_chunk(payload_bytes):
#     if not payload_bytes:
#         return np.array([], dtype=np.float32)
#     pcm_8k = audioop.alaw2lin(payload_bytes, 2)
#     pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
#     return np.frombuffer(pcm_16k, dtype=np.int16).astype(np.float32) / 32768.0

# def create_wav_bytes(pcm_float_array, sample_rate=16000):
#     """Converts a float32 numpy audio array into in-memory WAV bytes."""
#     # Convert float32 to int16 safely
#     pcm_int16 = np.clip(pcm_float_array * 32768.0, -32768.0, 32767.0).astype(np.int16)
    
#     wav_io = io.BytesIO()
#     with wave.open(wav_io, 'wb') as wav_file:
#         wav_file.setnchannels(1) # Mono
#         wav_file.setsampwidth(2) # 2 bytes (16-bit)
#         wav_file.setframerate(sample_rate)
#         wav_file.writeframes(pcm_int16.tobytes())
    
#     return wav_io.getvalue()

# class RealTranslationService(ai_service_pb2_grpc.TranslationServiceServicer): 
#     def ProcessMediaStream(self, request_iterator, context):
#         session_id = "UNKNOWN"
#         print("\n[AI Engine] Client connected Bi-directional stream", flush=True)

#         SAMPLE_RATE = 16000
#         SILENCE_THRESHOLD = 0.012
#         SILENCE_TIMEOUT_SAMPLES = int(0.6 * SAMPLE_RATE)
#         MAX_AUDIO_SAMPLES = int(10.0 * SAMPLE_RATE)
#         MIN_AUDIO_SAMPLES = int(1.0 * SAMPLE_RATE)

#         current_pcm_samples = []
#         consecutive_silence_samples = 0 
#         has_speech = False
#         session_source_lang = "vi"

#         try: 
#             for request in request_iterator: 
#                 session_id = request.session_id
#                 trigger_process = False # Cờ ép xử lý âm thanh
#                 is_closing = False

#                 if request.is_eos: 
#                     print(f"[AI Engine] Receive EOS from session {session_id}. Flushing buffer...", flush=True)
#                     is_closing = True
#                     if len(current_pcm_samples) > MIN_AUDIO_SAMPLES:
#                         trigger_process = True
#                     else:
#                         break
                
#                 if request.HasField("config"): 
#                     session_source_lang = request.config.source_language
#                     print(f"[AI Engine] Session config: {request.config.source_language} -> {request.config.target_language}", flush=True)
#                     continue

#                 if request.HasField("audio_chunk") and not is_closing: 
#                     raw_bytes = request.audio_chunk 
#                     chunk_np = decode_pcma_chunk(raw_bytes)

#                     if len(chunk_np) > 0: 
#                         current_pcm_samples.extend(chunk_np)
#                         rms = np.sqrt(np.mean(chunk_np ** 2))
#                         if rms < SILENCE_THRESHOLD:
#                             consecutive_silence_samples += len(chunk_np)
#                         else: 
#                             consecutive_silence_samples = 0 
#                             has_speech = True

#                     # Kích hoạt ngắt câu bình thường
#                     if has_speech and consecutive_silence_samples >= SILENCE_TIMEOUT_SAMPLES:
#                         trigger_process = True
#                     elif len(current_pcm_samples) >= MAX_AUDIO_SAMPLES: 
#                         trigger_process = True

#                 # ==========================================
#                 # XỬ LÝ ÂM THANH BẰNG GEMINI
#                 # ==========================================
#                 if trigger_process: 
#                     if len(current_pcm_samples) < MIN_AUDIO_SAMPLES: 
#                         current_pcm_samples = []
#                         consecutive_silence_samples = 0
#                         has_speech = False 
#                         if is_closing: break
#                         continue

#                     audio_chunk = np.array(current_pcm_samples, dtype=np.float32)
#                     current_pcm_samples = []
#                     consecutive_silence_samples = 0 
#                     has_speech = False 

#                     tensor_audio = torch.from_numpy(audio_chunk)
#                     timestamps = get_speech_timestamps(tensor_audio, vad_model, sampling_rate=SAMPLE_RATE, threshold=0.5)

#                     if len(timestamps) > 0: 
#                         inf_start = time.time()
                        
#                         # Chuyển đổi mảng numpy thành định dạng WAV chuẩn trong RAM
#                         wav_bytes = create_wav_bytes(audio_chunk, SAMPLE_RATE)

#                         # Yêu cầu Gemini làm cả 2 việc: Nghe (ASR) và Dịch (Translation)
#                         prompt = (
#                             "Đây là một cuộc gọi hỗ trợ kỹ thuật mạng internet. "
#                             "Hãy nghe đoạn âm thanh, sau đó ghi lại nguyên văn tiếng Việt và dịch sang tiếng Anh tự nhiên. "
#                             "Vui lòng CHỈ trả về kết quả đúng theo định dạng sau, không thêm lời giải thích:\n"
#                             "VI: <Nội dung tiếng Việt>\n"
#                             "EN: <Bản dịch tiếng Anh>"
#                         )
                        
#                         try:
#                             # Đẩy trực tiếp file audio dạng bytes lên Gemini 2.0 Flash (Hỗ trợ Multimodal)
#                             response = client.models.generate_content(
#                                 model='gemini-3.5-flash',
#                                 contents=[
#                                     types.Part.from_bytes(
#                                         data=wav_bytes,
#                                         mime_type='audio/wav',
#                                     ),
#                                     prompt
#                                 ]
#                             )
#                             raw_result = response.text.strip()
                            
#                             # Phân tích kết quả trả về để tách VI và EN
#                             text_vi = ""
#                             text_en = ""
#                             for line in raw_result.split('\n'):
#                                 line = line.strip()
#                                 if line.startswith("VI:"):
#                                     text_vi = line.replace("VI:", "").strip()
#                                 elif line.startswith("EN:"):
#                                     text_en = line.replace("EN:", "").strip()
                                    
#                             # Fallback nếu model trả về sai định dạng
#                             if not text_vi and not text_en:
#                                 text_en = raw_result
#                                 text_vi = "[Audio Processed by Gemini]"

#                         except Exception as api_err:
#                             text_vi = "[API Error]"
#                             text_en = f"Error details: {api_err}"

#                         latency = (time.time() - inf_start) * 1000
#                         duration = len(audio_chunk) / SAMPLE_RATE

#                         print(f"\n[AI Pipeline] Audio: {duration:.1f}s | Latency: {latency:.0f}ms")
#                         print(f"  VI 🇻🇳: {text_vi}")
#                         print(f"  EN 🇬🇧: {text_en}")

#                         combined_text = f"{text_vi} \n=> {text_en}"

#                         yield ai_service_pb2.MediaResponse(
#                             session_id=session_id,
#                             translated_text=combined_text,
#                             is_final=True
#                         )
                    
#                     if is_closing:
#                         break

#         except Exception as e: 
#             print(f"[AI Engine] Error stream session {session_id}: {e}", flush=True)

#         print(f"[AI Engine] Stream close for session {session_id}", flush=True)

#     @staticmethod
#     def serve():
#         server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
#         ai_service_pb2_grpc.add_TranslationServiceServicer_to_server(RealTranslationService(), server)
#         server.add_insecure_port('127.0.0.1:50052')

#         print("[AI Engine] gRPC Server listening on port 50052...", flush=True)
#         server.start()
#         try: 
#             server.wait_for_termination()
#         except KeyboardInterrupt: 
#             print("[AI Engine] Stopping gRPC server...", flush=True)
#             server.stop(0)

# if __name__ == "__main__":
#     RealTranslationService.serve()
# import grpc
# from concurrent import futures
# import time
# import sys
# import os

# current_dir = os.path.dirname(os.path.abspath(__file__))
# sys.path.append(os.path.join(current_dir, 'pb'))

# from pb import ai_service_pb2 
# from pb import ai_service_pb2_grpc

# class MockTranslationService(ai_service_pb2_grpc.TranslationServiceServicer):

#     def ProcessMediaStream(self, request_iterator, context): 
#         session_id = "UNKNOWN"
#         print("\n[AI Engine Mock] Client connected Bi-directional stream", flush=True)

#         try:
#             for request in request_iterator:
#                 session_id = request.session_id 

#                 # end of stream
#                 if request.is_eos:
#                     print(f"[AI Engine Mock] Received EOS for session {session_id}", flush=True)
#                     break
                
#                 # config message
#                 if request.HasField("config"):
#                     print(f"[AI Engine Mock] Received config for session {session_id}: {request.config}", flush=True)
#                     continue

#                 # audio message
#                 if request.HasField("audio_chunk"): 
#                     chunk_size = len(request.audio_chunk)
#                     seq = request.sequence_number
#                     print(f"[AI Engine Mock] Received audio chunk for session {session_id}: seq={seq}, size={chunk_size} bytes", flush=True)

#                     time.sleep(0.01)

#                     fake_transcript = f"Transcription of chunk {seq} for session {session_id}"

#                     yield ai_service_pb2.MediaResponse(
#                         session_id=session_id,
#                         translated_text=fake_transcript,
#                         is_final=False
#                     )
#         except Exception as e: 
#             print(f"[AI Engine Mock] Error processing stream for session {session_id}: {e}", flush=True)

#         print(f"[AI Engine Mock] Stream ended for session {session_id}", flush=True)


#     def serve():
#       server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

#       ai_service_pb2_grpc.add_TranslationServiceServicer_to_server(MockTranslationService(), server) 

#       server.add_insecure_port('127.0.0.1:50052')
#       print("[AI Engine Mock] Starting gRPC server on port 50052...", flush=True)
#       server.start()

#       try: 
#         server.wait_for_termination()
#       except KeyboardInterrupt:
#         print("[AI Engine Mock] Shutting down gRPC server...", flush=True)
#         server.stop(0)

# if __name__ == "__main__":
#     MockTranslationService.serve()

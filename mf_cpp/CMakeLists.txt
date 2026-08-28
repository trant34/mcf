cmake_minimum_required(VERSION 3.16)
project(mf_cpp LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

find_package(OpenCV REQUIRED)
find_package(Threads REQUIRED)

find_package(nlohmann_json QUIET)

# v3.2 (rtpgw_design_v3.md section 37): decode moved into RTPGW, so mf_cpp
# no longer has a local ffmpeg-subprocess decoder or a synchronous HTTP
# inference client. CURL is dropped accordingly (it was only ever used by
# the now-removed mcf_client.cpp).
set(MF_SOURCES
    src/main.cpp
    src/args.cpp
    src/mask_callback_server.cpp
    src/mf.cpp
)

add_executable(mf_cpp ${MF_SOURCES})

target_include_directories(mf_cpp PRIVATE include ${OpenCV_INCLUDE_DIRS} third_party)
target_link_libraries(mf_cpp PRIVATE ${OpenCV_LIBS} Threads::Threads)

if(nlohmann_json_FOUND)
    target_link_libraries(mf_cpp PRIVATE nlohmann_json::nlohmann_json)
else()
    message(STATUS "nlohmann_json not found via find_package(); "
                    "using bundled header in third_party/nlohmann/json.hpp")
endif()

# ---------------------------------------------------------------------------
# v3.2 (rtpgw_design_v3.md section 37): MF -> RTPGW bidirectional gRPC video
# ingest (api/proto/video_ingest.proto) is now the ONLY transport -- there is
# no local-decode fallback anymore (v3.1's --legacy-http-infer is gone).
# Requires generated C++ stubs, produced by scripts/generate_proto.sh (needs
# `protoc` + `grpc_cpp_plugin`, e.g. on Debian/Ubuntu:
# `apt-get install -y protobuf-compiler-grpc libgrpc++-dev`).
#
# If the stubs haven't been generated yet, mf_cpp still CONFIGURES and
# BUILDS (so `cmake ..` doesn't hard-fail before you've had a chance to run
# generate_proto.sh), but the resulting binary throws immediately on
# startup -- see MF::MF() in mf.cpp -- since there is nothing left for it to
# fall back to.
# ---------------------------------------------------------------------------
set(MF_GENERATED_DIR "${CMAKE_SOURCE_DIR}/generated")
if(EXISTS "${MF_GENERATED_DIR}/video_ingest.pb.h" AND EXISTS "${MF_GENERATED_DIR}/video_ingest.grpc.pb.h")
    find_package(Protobuf REQUIRED)
    find_package(gRPC CONFIG REQUIRED)

    target_sources(mf_cpp PRIVATE
        src/video_ingest_client.cpp
        "${MF_GENERATED_DIR}/video_ingest.pb.cc"
        "${MF_GENERATED_DIR}/video_ingest.grpc.pb.cc"
    )
    target_include_directories(mf_cpp PRIVATE "${MF_GENERATED_DIR}")
    target_compile_definitions(mf_cpp PRIVATE MF_HAVE_GRPC_INGEST)
    target_link_libraries(mf_cpp PRIVATE gRPC::grpc++ protobuf::libprotobuf)

    message(STATUS "video_ingest gRPC stubs found -- building with MF -> RTPGW gRPC ingest support")
else()
    message(WARNING "video_ingest gRPC stubs not found under ${MF_GENERATED_DIR}. "
                     "Run scripts/generate_proto.sh (after installing grpc_cpp_plugin) and "
                     "re-run cmake -- mf_cpp v3.2 CANNOT run at all without this (no "
                     "local-decode fallback exists anymore).")
endif()

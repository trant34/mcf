cmake_minimum_required(VERSION 3.16)
project(mf_cpp LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

find_package(OpenCV REQUIRED)
find_package(CURL REQUIRED)
find_package(Threads REQUIRED)

find_package(nlohmann_json QUIET)

add_executable(mf_cpp
    src/main.cpp
    src/args.cpp
    src/ffmpeg_decoder.cpp
    src/mcf_client.cpp
    src/mf.cpp
)

target_include_directories(mf_cpp PRIVATE include ${OpenCV_INCLUDE_DIRS})
target_link_libraries(mf_cpp PRIVATE ${OpenCV_LIBS} CURL::libcurl Threads::Threads)

if(nlohmann_json_FOUND)
    target_link_libraries(mf_cpp PRIVATE nlohmann_json::nlohmann_json)
else()
    message(STATUS "nlohmann_json not found via find_package(); "
                    "using bundled header in third_party/nlohmann/json.hpp")
    target_include_directories(mf_cpp PRIVATE third_party)
endif()

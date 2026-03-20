cmake_minimum_required(VERSION 3.20)

project(yt_asr_launcher LANGUAGES CXX)

add_executable(yt_asr_launcher WIN32
    src/main.cpp
)

target_compile_features(yt_asr_launcher PRIVATE cxx_std_17)
target_compile_definitions(yt_asr_launcher PRIVATE
    UNICODE
    _UNICODE
    WIN32_LEAN_AND_MEAN
    NOMINMAX
)

if(MSVC)
    target_compile_options(yt_asr_launcher PRIVATE /W4 /EHsc /permissive- /utf-8)
else()
    target_compile_options(yt_asr_launcher PRIVATE -Wall -Wextra -Wpedantic)
endif()

target_link_libraries(yt_asr_launcher PRIVATE
    shell32
    urlmon
    user32
)

set_target_properties(yt_asr_launcher PROPERTIES
    OUTPUT_NAME "yt-asr-launcher"
)

add_custom_command(TARGET yt_asr_launcher POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
        $<TARGET_FILE:yt_asr_launcher>
        ${CMAKE_SOURCE_DIR}/../yt-asr-launcher.exe
    COMMENT "Copying yt-asr-launcher.exe to the application root"
)

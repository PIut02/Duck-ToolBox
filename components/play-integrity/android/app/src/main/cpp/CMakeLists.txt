cmake_minimum_required(VERSION 3.22.1)

project(playintegrityfix)

find_package(cxx REQUIRED CONFIG)

link_libraries(cxx::cxx)

enable_language(C CXX ASM)

set(PIF_VENDOR_DIR "${CMAKE_CURRENT_SOURCE_DIR}" CACHE PATH "Pinned engine dependencies")
set(shadowhook_SOURCE_DIR "${PIF_VENDOR_DIR}/shadowhook")
include_directories(${PIF_VENDOR_DIR})

file(GLOB SHADOWHOOK_SRC
    ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/*.c
    ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/common/*.c
)

# xDL is architecture-neutral and used directly by language spoofing, independent
# of ShadowHook's inline-hook backend.
file(GLOB XDL_SRC ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/third_party/xdl/*.c)

if(CMAKE_ANDROID_ARCH_ABI STREQUAL "armeabi-v7a")
    file(GLOB SHADOWHOOK_ARCH_SRC ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/arch/arm/*.c ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/arch/arm/*.S)
    include_directories(${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/arch/arm)
elseif(CMAKE_ANDROID_ARCH_ABI STREQUAL "arm64-v8a")
    file(GLOB SHADOWHOOK_ARCH_SRC ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/arch/arm64/*.c ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/arch/arm64/*.S)
    include_directories(${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/arch/arm64)
endif()

include_directories(
    ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp
    ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/common
    ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/include
    ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/third_party/bsd
    ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/third_party/lss
    ${shadowhook_SOURCE_DIR}/shadowhook/src/main/cpp/third_party/xdl
)

# ShadowHook has no x86_64 backend; keep only its architecture-neutral xDL there
# (language spoofing uses it directly) and let Dobby provide the inline hook.
set(ENGINE_HOOK_SRC ${SHADOWHOOK_SRC} ${SHADOWHOOK_ARCH_SRC})
if(CMAKE_ANDROID_ARCH_ABI STREQUAL "x86_64")
    set(ENGINE_HOOK_SRC "")
    set(DOBBY_BUILD_EXAMPLE OFF CACHE BOOL "" FORCE)
    set(DOBBY_BUILD_TEST OFF CACHE BOOL "" FORCE)
    add_subdirectory(${PIF_VENDOR_DIR}/Dobby ${CMAKE_CURRENT_BINARY_DIR}/dobby)
    # Dobby lists every architecture's .asm unconditionally. Its arm64 assembly uses
    # Apple-style cdecl()/bracket expressions that the x86_64 NDK assembler rejects.
    # x86_64 only needs the x64 trampolines, so drop the ARM assembly sources.
    get_target_property(DOBBY_SOURCES dobby_static SOURCES)
    foreach(source IN LISTS DOBBY_SOURCES)
        if(source MATCHES "/(arm|arm64)/.*\\.asm$")
            set_property(SOURCE "${source}" PROPERTY HEADER_FILE_ONLY TRUE)
        endif()
    endforeach()
endif()

add_library(${CMAKE_PROJECT_NAME} SHARED main.cpp companion.cpp config.cpp property_hook.cpp private_properties.cpp java_injection.cpp observation.cpp duck_hook.cpp ${PIF_VENDOR_DIR}/local_cxa_atexit_finalize_impl/atexit.cpp ${ENGINE_HOOK_SRC} ${XDL_SRC})
if(CMAKE_ANDROID_ARCH_ABI STREQUAL "x86_64")
    target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE dobby_static)
    target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE ${PIF_VENDOR_DIR}/Dobby/include)
endif()

target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE log)

set(PIF_MODULE_DIR "/data/adb/modules/duck-toolbox/play-integrity" CACHE STRING "Engine payload directory")
set(PIF_DATA_DIR "/data/adb/play-integrity" CACHE STRING "Engine state directory")
target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE
    PIF_MODULE_DIR="${PIF_MODULE_DIR}"
    PIF_DATA_DIR="${PIF_DATA_DIR}"
)

set(DUCK_RUNTIME_DIR "" CACHE PATH "Embedded Zygisk composition source")
set(DUCK_FAKER_LIBRARY "" CACHE FILEPATH "ARM64 Device Faker static library")
set(DUCK_FAKER_LIBRARY_X86_64 "" CACHE FILEPATH "x86_64 Device Faker static library")
if(NOT EXISTS "${DUCK_RUNTIME_DIR}/entry.cpp")
    message(FATAL_ERROR "Build through scripts/build-play-integrity.py with the composite runtime")
endif()
target_sources(${CMAKE_PROJECT_NAME} PRIVATE
    ${DUCK_RUNTIME_DIR}/entry.cpp ${DUCK_RUNTIME_DIR}/engine.cpp ${DUCK_RUNTIME_DIR}/channel.cpp
    ${DUCK_RUNTIME_DIR}/pthread_keys.cpp)
target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE "${DUCK_RUNTIME_DIR}")
# Keep static runtime helpers (including compiler-rt emutls) local to this DSO.
target_link_options(${CMAKE_PROJECT_NAME} PRIVATE
    -Wl,--wrap=pthread_key_create -Wl,--wrap=pthread_key_delete -Wl,--exclude-libs,ALL)
set(DUCK_LANGUAGE_DIR "" CACHE PATH "Process-local language spoofing source")
if(NOT EXISTS "${DUCK_LANGUAGE_DIR}/native/entry.cpp")
    message(FATAL_ERROR "Build with the independent language spoofing component")
endif()
set(LSPLANT_BUILD_SHARED OFF CACHE BOOL "" FORCE)
set(LSPLANT_STANDALONE ON CACHE BOOL "" FORCE)
add_subdirectory(${PIF_VENDOR_DIR}/lsplant/lsplant/src/main/jni ${CMAKE_CURRENT_BINARY_DIR}/lsplant)
target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE lsplant_static)
target_sources(${CMAKE_PROJECT_NAME} PRIVATE
    ${DUCK_LANGUAGE_DIR}/native/entry.cpp ${DUCK_LANGUAGE_DIR}/native/config.cpp
    ${DUCK_LANGUAGE_DIR}/native/companion.cpp ${DUCK_LANGUAGE_DIR}/native/hooks.cpp
    ${DUCK_LANGUAGE_DIR}/native/properties.cpp)
target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE ${DUCK_LANGUAGE_DIR}/native)
target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE DUCK_HAS_LANGUAGE_SPOOF=1)
target_include_directories(${CMAKE_PROJECT_NAME} PRIVATE ${CMAKE_CURRENT_SOURCE_DIR})
if(CMAKE_ANDROID_ARCH_ABI STREQUAL "arm64-v8a")
    if(NOT EXISTS "${DUCK_FAKER_LIBRARY}")
        message(FATAL_ERROR "Build the embedded Device Faker static library first")
    endif()
    set(DUCK_FAKER_LINK "${DUCK_FAKER_LIBRARY}")
elseif(CMAKE_ANDROID_ARCH_ABI STREQUAL "x86_64")
    if(NOT EXISTS "${DUCK_FAKER_LIBRARY_X86_64}")
        message(FATAL_ERROR "Build the x86_64 embedded Device Faker static library first")
    endif()
    set(DUCK_FAKER_LINK "${DUCK_FAKER_LIBRARY_X86_64}")
endif()
if(DUCK_FAKER_LINK)
    target_compile_definitions(${CMAKE_PROJECT_NAME} PRIVATE DUCK_HAS_DEVICE_FAKER=1)
    target_link_libraries(${CMAKE_PROJECT_NAME} PRIVATE "${DUCK_FAKER_LINK}" dl m)
endif()

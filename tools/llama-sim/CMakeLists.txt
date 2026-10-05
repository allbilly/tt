cmake_minimum_required(VERSION 3.24)
project(ttsim_smokes LANGUAGES CXX)

set(TT_SOURCE "$ENV{TT_METAL_HOME}")
get_filename_component(LAB_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
if(NOT EXISTS "${TT_SOURCE}/build/include/tt-metalium")
    message(FATAL_ERROR "Prepare the pinned TT-Metal source header layout first")
endif()

add_library(tt_sdk INTERFACE)
target_compile_features(tt_sdk INTERFACE cxx_std_20)
target_compile_definitions(tt_sdk INTERFACE
    FMT_HEADER_ONLY SPDLOG_FMT_EXTERNAL DISABLE_NAMESPACE_STATIC_ASSERT NTEST)
target_include_directories(tt_sdk INTERFACE
    ${TT_SOURCE}/build/include
    ${TT_SOURCE}/tt_metal/api
    ${TT_SOURCE}/ttnn/api
    ${TT_SOURCE}/ttnn/cpp
    ${TT_SOURCE}/tt_metal/third_party/umd/device/api)
target_link_libraries(tt_sdk INTERFACE
    /usr/lib/_ttnncpp.so /usr/lib/libtt_metal.so /usr/lib/libtt_stl.so)

add_executable(metal_integer
    ${TT_SOURCE}/tt_metal/programming_examples/add_2_integers_in_riscv/add_2_integers_in_riscv.cpp)
target_compile_definitions(metal_integer PRIVATE OVERRIDE_KERNEL_PREFIX="tt_metal/programming_examples/")
target_link_libraries(metal_integer PRIVATE tt_sdk)

add_executable(ttnn_add ${LAB_ROOT}/examples/ttnn_add.cpp)
target_include_directories(ttnn_add PRIVATE ${TT_SOURCE}/tt_metal/third_party/tracy/public)
target_compile_options(ttnn_add PRIVATE -O0)
target_link_libraries(ttnn_add PRIVATE tt_sdk)

cmake_minimum_required(VERSION 3.20)
project(exploding_kittens LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(MSVC)
  add_compile_options(/W4 /permissive-)
else()
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

add_library(ek_engine src/game.cpp)
target_include_directories(ek_engine PUBLIC include)

find_package(Threads REQUIRED)
add_executable(ek_server src/server.cpp src/admin_auth.cpp src/client_download.cpp)
target_compile_definitions(ek_server PRIVATE EK_SOURCE_CLIENT_DIR="${CMAKE_CURRENT_SOURCE_DIR}/client")
target_link_libraries(ek_server PRIVATE ek_engine Threads::Threads)
if(MSVC)
  target_compile_options(ek_server PRIVATE /utf-8)
endif()
if(WIN32)
  target_link_libraries(ek_server PRIVATE ws2_32 bcrypt shell32)
else()
  find_package(OpenSSL REQUIRED COMPONENTS Crypto)
  target_link_libraries(ek_server PRIVATE OpenSSL::Crypto)
endif()

# Ship only the allowlisted game-client modules; never bundle admin tools or credentials.
add_custom_target(ek_client_files ALL
  COMMAND ${CMAKE_COMMAND} -E make_directory "$<TARGET_FILE_DIR:ek_server>/client"
  COMMAND ${CMAKE_COMMAND} -E copy_if_different
    "${CMAKE_CURRENT_SOURCE_DIR}/client/main.py"
    "${CMAKE_CURRENT_SOURCE_DIR}/client/i18n.py"
    "${CMAKE_CURRENT_SOURCE_DIR}/client/presentation.py"
    "$<TARGET_FILE_DIR:ek_server>/client"
  VERBATIM)
add_dependencies(ek_server ek_client_files)

enable_testing()
add_executable(ek_game_tests tests/game_tests.cpp)
target_link_libraries(ek_game_tests PRIVATE ek_engine)
add_test(NAME ek_game_tests COMMAND ek_game_tests)

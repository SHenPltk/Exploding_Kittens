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
add_executable(ek_server src/server.cpp)
target_link_libraries(ek_server PRIVATE ek_engine Threads::Threads)
if(WIN32)
  target_link_libraries(ek_server PRIVATE ws2_32)
endif()

enable_testing()
add_executable(ek_game_tests tests/game_tests.cpp)
target_link_libraries(ek_game_tests PRIVATE ek_engine)
add_test(NAME ek_game_tests COMMAND ek_game_tests)

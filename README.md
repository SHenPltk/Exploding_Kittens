# Exploding Kittens LAN

A from-scratch Windows LAN implementation of the original Exploding Kittens rules in `doc/ekoe-instructions-english.pdf`.

## Architecture

- `src/game.cpp`: authoritative C++17 rule engine. The server owns the deck, random choices, hands, turns, pending Nope windows, Defuse insertion, and winner state.
- `src/server.cpp`: Windows Winsock TCP host. It exposes a small newline-delimited, tab-separated protocol and sends public snapshots plus each client's private hand.
- `client/main.py`: Python 3 standard-library Tkinter client. It can connect manually by IPv4 address and port; no third-party Python package is needed.
- `tests/game_tests.cpp`: deterministic C++ tests for setup, counts, attack/skip, Nope, and invalid actions.

Only the server knows the draw-pile order and other players' cards. The client is a view and command sender, not a trust boundary.

## Build on Windows

Use either Visual Studio 2022 or a working MinGW-w64 installation:

```powershell
cmake -S . -B build
cmake --build build --config Release
ctest --test-dir build -C Release --output-on-failure
```

The repository's current development shell includes CMake and MinGW paths, but the bundled MinGW installation may fail before compiling a source file if its temporary directory/toolchain is broken. Visual Studio's `cmake -S . -B build` generator is supported.

## Run

Start the host on the computer that will run the game:

```powershell
.\build\Release\ek_server.exe 8765
```

If using a single-configuration MinGW build, use `build\ek_server.exe` instead. Allow inbound TCP port `8765` through Windows Defender Firewall on a private network. Each player starts `client/main.py`, enters the host's private IPv4 address and port, and joins with a name. The player with id 0 presses **Start game** after at least two players have joined.

For a same-computer smoke test, use `127.0.0.1`. For a LAN game, use the host's `ipconfig` IPv4 address; manual IP entry is intentional because Wi-Fi isolation and Windows firewall settings can block discovery broadcasts.

```powershell
py -3 client\main.py
```

## Protocol quick reference

Every command is one UTF-8 line ending in `\\n`:

```text
JOIN <name>
START
PLAY <card_id> [target_player_id]
COMBO <card_id> <card_id> <target_player_id>
COMBO <card_id> <card_id> <card_id> <target_player_id> <requested_card_name>
DRAW
PASS
NOPE <card_id>
FAVOR <card_id>
DEFUSE <cards_from_top>
EXPLODE
```

The server sends `WELCOME`, `ROSTER`, `STATE`, `PLAYER`, `HAND`, `EVENT`, `PRIVATE`, and `ENDSTATE` lines. `STATE`, `PLAYER`, and `HAND` are rebuilt as a snapshot; a `HAND` line is only sent to the corresponding connection. `PRIVATE SEE_FUTURE` is only sent to the player who used See the Future.

## Rules implemented

- 2–5 players and the 56-card original deck.
- One Defuse plus seven cards per player, then `players - 1` Exploding Kittens in the draw pile.
- Attack stacking, Skip, See the Future, Shuffle, Favor, Nope chains, Defuse, elimination, winner detection, and same-title two-/three-card combos.
- The optional fast-game variant is intentionally not enabled in this first playable build.

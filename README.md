# Exploding Kittens LAN

A from-scratch Windows LAN implementation of the original Exploding Kittens rules in `docs/ekoe-instructions-english.pdf`.

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

With MinGW, put the matching compiler directory first on PATH (for this machine: `D:\mingw64\bin`). Mixing runtime DLLs from Git Bash's MinGW with another MinGW installation can cause the compiler to exit without diagnostics. In Git Bash, use `export PATH="/d/mingw64/bin:$PATH"`. For a single-configuration MinGW build, configure with `-DCMAKE_BUILD_TYPE=Release`. Visual Studio's multi-configuration generator uses `--config Release`.

## Verification

After building the server, run the C++ rule tests and the Python presentation, Tk widget and loopback TCP tests:

```powershell
ctest --test-dir build -C Release --output-on-failure
py -3 -m unittest discover -s tests -p "test_*.py" -v
```

The TCP tests start temporary servers on loopback ports and play 2–5 player games. Build `ek_server` first; TCP tests are skipped if no server executable exists. Tk tests require a Python installation with Tkinter.

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

## 界面与交互 / Interface

- 默认使用简体中文，可在窗口右上角随时切换 English；按钮、牌名、阶段、日志和选牌窗口一起切换。
- 手牌按 `DEFUSE → ATTACK → FAVOR → NOPE → SHUFFLE → SKIP → SEE_FUTURE → 猫猫牌` 排序，同类牌排在一起。协议中的 `SEE_FUTURE` 对应 See the Future / 预知未来。
- 手牌及选牌窗口只显示牌名，卡牌编号保留在协议内部。同名牌依然是可单独选择的实体。
- 摸牌、索要或偷牌获得的新牌会以浅黄色强调约 3 秒，并显示“新获得”提示；普通状态刷新不会重置选牌或延长强调时间。
- 反制阶段显示原出牌者、牌名（含组合中的牌）、目标、最近打出 Nope 的玩家，以及当前取消 / 生效状态。
- 单张猫猫牌无法出牌；仍可组成两张或三张同名组合。服务器也执行此限制。
- 显示服务器统计的牌堆内剩余炸弹及 `炸弹数 / 牌数` 百分比。正在处理、尚未插回的炸弹不计入牌堆；牌堆为空时概率显示为“—”。这只是未知牌序时的牌堆比例，不能替代预知未来或已知插回位置提供的信息。

The language selector switches all client labels and dialogs live. New cards are highlighted for three seconds; card ids remain internal. The response banner identifies the original play and the latest Nope. Bomb odds use the server's actual draw-pile bomb count divided by the draw-pile size, without revealing positions.

All rules and protocol documents are consolidated in `docs/`.

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

The server sends `WELCOME`, `ROSTER`, `STATE`, `DECK`, `PENDING`, `PLAYER`, `DISCARD`, `HAND`, `EVENT`, `PRIVATE`, and `ENDSTATE` lines. `STATE`, `PLAYER`, and `HAND` are rebuilt as a snapshot; a `HAND` line is only sent to the corresponding connection. `PRIVATE SEE_FUTURE` is only sent to the player who used See the Future.

## Rules implemented

- 2–5 players and the 56-card original deck.
- One Defuse plus seven cards per player, then `players - 1` Exploding Kittens in the draw pile.
- Attack stacking, Skip, See the Future, Shuffle, Favor, Nope chains, Defuse, elimination, winner detection, and same-title two-/three-card combos.
- The optional fast-game variant is intentionally not enabled in this first playable build.

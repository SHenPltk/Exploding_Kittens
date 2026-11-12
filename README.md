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

## 局域网下载客户端

服务端同时提供 HTTP 下载页，默认游戏端口 **8765**、下载端口 **8766**。同一局域网设备在浏览器打开 `http://主机IPv4:8766/`，下载 `client.zip`，解压后运行 `start_client.bat`。客户端包包含 Python 源码和启动脚本，需要 Windows 上安装 **Python 3.10+（含 Tkinter）**，无需额外 Python 包。下载内容不包含管理员程序、管理员密码文件或服务端源码。

```powershell
# 自定义游戏端口、下载端口和管理员密码文件路径
.\build\ek_server.exe 8765 8766 .\server_admin.auth
# 下载服务可用 0 关闭
.\build\ek_server.exe 8765 0
```

放行 Windows 私有网络的 TCP 8765 / 8766。分发服务仅开放首页和 `/client.zip`，不会提供通用目录浏览；请不要将游戏和管理端口暴露到公网。游戏及管理协议面向可信局域网，目前不提供 TLS 传输加密。

## 管理员程序

从项目根目录单独运行：

```powershell
py -3 -m admin.main
```

输入主机 IP、游戏端口和管理员密码。初始密码为 **`Admin123`**，登录后可通过“修改密码”设置新密码。服务端将带盐的 PBKDF2-SHA256 校验记录保存在 `ek_server.exe` 同目录的 `server_admin.auth`（可通过第三个参数指定）；密码持久保存，重启不会恢复默认密码。密码修改后其他管理员连接失效，当前修改者保持登录。密码文件已加入 Git 忽略，不包含在下载包内。

管理员窗口可查看全部玩家手牌、牌堆顺序（从顶到底），指定在线玩家为房主、给存活玩家新造任意牌，以及“结束当前一回合并给指定玩家塞炸弹”。造牌和塞炸弹都不抽取或重排原牌堆；管理员造出的炸弹立即触发拆除 / 自动出局，拆除后这张新造炸弹与 DEFUSE 一起弃置，不会插入原牌堆。若当前处于攻击的多回合中，强制结束只消耗一个待执行回合；未处理的反制、Favor 或炸弹阶段不允许强制塞炸弹。

管理员连接与玩家连接隔离，未通过管理员密码认证无法获取其他人手牌或牌堆。房主只拥有模式设置和踢出成员的权限，不自动拥有管理员权限。

## 界面与交互 / Interface

- 默认使用简体中文，可在窗口右上角随时切换 English；按钮、牌名、阶段、日志和选牌窗口一起切换。
- 同一房间不允许重复昵称；服务器去除昵称两端空白后按原文比较（区分大小写）。重名会留在登录页提示，可直接改名重试。
- 摸到炸弹猫且持有 DEFUSE 时自动弹出拆弹窗口，选择秘密插回位置；没有 DEFUSE 时自动出局。Favor 交牌也自动弹窗，不再需要 Resolve prompt 按钮。强制提示会在状态改变或断线时关闭。
- 本局结束后房主可点击“重新开始”，至少两名在线玩家即可立即重新洗牌发牌；已出局但在线的玩家重新入局，断线玩家不参加。房主离线时由下一位在线玩家接替。语言保留，上一局的手牌选择和高亮清空。
- 手牌按 `DEFUSE → ATTACK → FAVOR → NOPE → SHUFFLE → SKIP → SEE_FUTURE → 猫猫牌` 排序，同类牌排在一起。协议中的 `SEE_FUTURE` 对应 See the Future / 预知未来。
- 手牌及选牌窗口只显示牌名，卡牌编号保留在协议内部。同名牌依然是可单独选择的实体。
- 摸牌、索要或偷牌获得的新牌会以浅黄色强调约 3 秒，并显示“新获得”提示；普通状态刷新不会重置选牌或延长强调时间。
- 出牌、组合、被偷牌、交出索要牌或消耗拆除牌等导致手牌减少时，显示约 4 秒的失牌提示（数量与牌名），并保留在本机日志中；只比较本人私有手牌，不向其他玩家泄露牌名。新开局不算失牌。
- 弃牌区只显示最顶一张牌的名称，仍保留弃牌总数。
- 反制阶段显示原出牌者、牌名（含组合中的牌）、目标、最近打出 Nope 的玩家，以及当前取消 / 生效状态。三张组合同时公开索要的牌名。
- 点击 Nope 按钮会自动使用一张 Nope，无需在手牌中勾选。
- 窗口支持左右分屏宽度（最小 420×500），玩家区和操作按钮随宽度重新排布。鼠标移入手牌区后，可用 Up / Down 或鼠标滚轮滚动。
- 房主可点击“踢出成员”，选择其他在线玩家；局内被踢者按弃权处理，未结算动作会安全清理，房主不能踢自己。
- 单张猫猫牌无法出牌；仍可组成两张或三张同名组合。服务器也执行此限制。
- 显示服务器统计的牌堆内剩余炸弹及 `炸弹数 / 牌数` 百分比。正在处理、尚未插回的炸弹不计入牌堆；牌堆为空时概率显示为“—”。这只是未知牌序时的牌堆比例，不能替代预知未来或已知插回位置提供的信息。

The language selector switches all client labels and dialogs live. New cards are highlighted for three seconds; card ids remain internal. The response banner identifies the original play and the latest Nope. Bomb odds use the server's actual draw-pile bomb count divided by the draw-pile size, without revealing positions.

## 自由攻击 / Free Attack

房主可在大厅或结算后开启“下一局自由攻击（自选目标）”，默认关闭，支持 2–5 人。开启后，打出单张 Attack 会先选择其他存活玩家作为目标。攻击仍可被 Nope 取消，回合债务照常叠加；受攻击者完成待执行回合后，从该玩家的下家继续。关闭时沿用基础规则的顺时针下一位目标。

## 快速模式 / Fast mode

房主可在大厅或本局结束后勾选“下一局快速模式（仅 2–3 人）”，随后点击开始 / 重新开始。默认关闭；对局中锁定，其他玩家可看到设置但无法修改。第四名玩家加入大厅时自动关闭快速模式，4–5 人无法开启；之后人数降为 2–3 人需由房主重新开启。

按规则 PDF 的快速变体：发完每人 8 张起手牌之后、放入炸弹之前，随机移除剩余非炸弹牌堆的 `floor(牌数 / 3)` 张。被移除牌的身份不公开，也不放入弃牌堆；随后仍放入 `人数 − 1` 张炸弹并洗牌。2 人开局牌堆为 **24** 张（标准 35 张），3 人为 **20** 张（标准 29 张）。PDF 使用“约三分之一”，本实现统一向下取整。

The host can choose the fast variant before starting or restarting a 2–3 player game. After dealing, remove a random floor(one third) of the remaining non-Kitten draw pile, then insert the normal number of Kittens. Removed card identities stay hidden. The actual current mode and the selection for the next game are shown separately.

All rules and protocol documents are consolidated in `docs/`.

## Protocol quick reference

Every command is one UTF-8 line ending in `\\n`:

```text
JOIN <name>
START
RESTART
SET_FAST <0|1>
SET_FREE_ATTACK <0|1>
KICK <player_id>
PLAY <card_id> [target_player_id]
COMBO <card_id> <card_id> <target_player_id>
COMBO <card_id> <card_id> <card_id> <target_player_id> <requested_card_name>
DRAW
PASS
NOPE <card_id>
FAVOR <card_id>
DEFUSE <cards_from_top>
```

Legacy `EXPLODE` requests are rejected with `DEFUSE_REQUIRED` while the player holds a Defuse.

The server sends `WELCOME`, `ROSTER`, `STATE`, `ROUND`, `SESSION`, `OPTIONS`, `MODE`, `DECK`, `PENDING`, `PLAYER`, `DISCARD`, `HAND`, `EVENT`, `PRIVATE`, and `ENDSTATE` lines. `STATE`, `PLAYER`, and `HAND` are rebuilt as a snapshot; a `HAND` line is only sent to the corresponding connection. `PRIVATE SEE_FUTURE` is only sent to the player who used See the Future.

## Rules implemented

- 2–5 players and the 56-card original deck.
- One Defuse plus seven cards per player, then `players - 1` Exploding Kittens in the draw pile.
- Attack stacking, Skip, See the Future, Shuffle, Favor, Nope chains, Defuse, elimination, winner detection, and same-title two-/three-card combos.
- Host-enabled fast-game variant for 2–3 players; standard setup for 4–5 players.

"""Independent, standard-library administrator for the authoritative LAN server.

Run from the project root with ``python -m admin.main``. No credentials are saved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk


CARD_NAMES = {
    "DEFUSE": "拆除", "ATTACK": "攻击", "FAVOR": "索要", "NOPE": "不行！",
    "SHUFFLE": "洗牌", "SKIP": "跳过", "SEE_FUTURE": "预知未来",
    "TACOCAT": "墨西哥卷饼猫", "CATTERMELON": "西瓜猫",
    "HAIRY_POTATO_CAT": "毛茸茸土豆猫", "RAINBOW_RALPHING_CAT": "彩虹呕吐猫",
    "BEARD_CAT": "胡子猫", "EXPLODING_KITTEN": "炸弹猫",
}
PHASE_NAMES = {
    "LOBBY": "等待开始", "TURN": "玩家回合", "RESPONSE": "等待反制",
    "FAVOR": "等待交牌", "KITTEN": "处理炸弹猫", "GAME_OVER": "游戏结束",
}
SUCCESS_TEXT = {
    "HOST_CHANGED": "房主已修改。", "CARD_ADDED": "已向目标玩家新增所选牌；炸弹猫按正常规则处理。",
    "KITTEN_INJECTED": "已结束当前一回合，并向目标玩家注入新建的炸弹猫。",
    "PASSWORD_CHANGED": "管理密码已修改；本窗口继续登录，其他管理会话已失效。",
}
SUCCESS_COMMANDS = {
    "HOST_CHANGED": "ADMIN_HOST", "CARD_ADDED": "ADMIN_ADD",
    "KITTEN_INJECTED": "ADMIN_BOMB", "PASSWORD_CHANGED": "ADMIN_PASSWORD",
}
ERROR_TEXT = {
    "AUTH_FAILED": "管理密码错误。", "AUTH_EXPIRED": "管理登录已失效，请使用新密码重新登录。",
    "INVALID_PASSWORD": "新密码格式无效。", "PASSWORD_SAVE_FAILED": "服务器无法保存新密码。",
    "GAME_NOT_STARTED": "游戏尚未开始。", "INVALID_PLAYER": "目标玩家无效或已出局。",
    "PLAYER_OFFLINE": "目标玩家未在线。", "INVALID_CARD_TYPE": "牌类型无效。",
    "INVALID_COMMAND_ARGUMENT": "管理命令参数无效。", "UNKNOWN_COMMAND": "服务器不支持该管理命令。",
    "ADMIN_ACTION_REQUIRES_TURN": "此操作需要等待普通玩家回合。", "GAME_OVER": "本局游戏已结束。",
}
Card = tuple[int, str]
Event = tuple[str, list[str]]


class ProtocolError(ValueError):
    """A deliberately generic error: never include wire data or credentials."""

    def __init__(self) -> None:
        super().__init__("Invalid administrator protocol message")


def _number(value: str, minimum: int = 0) -> int:
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise ProtocolError() from None
    if str(result) != value or result < minimum:
        raise ProtocolError()
    return result


def _safe_field(value: str) -> bool:
    return not any(char in value for char in "\t\r\n\0")


def valid_password(value: str) -> bool:
    """The server measures UTF-8 bytes, rather than displayed character count."""
    return _safe_field(value) and 8 <= len(value.encode("utf-8")) <= 128


def parse_card(value: str) -> Card:
    fields = value.split(":")
    if len(fields) != 2 or fields[1] not in CARD_NAMES:
        raise ProtocolError()
    return _number(fields[0], 1), fields[1]


@dataclass(frozen=True)
class Player:
    player_id: int
    name: str
    alive: bool
    online: bool


@dataclass
class Snapshot:
    phase: str = "LOBBY"
    host_id: int = -1
    round_number: int = 0
    players: dict[int, Player] = field(default_factory=dict)
    hands: dict[int, list[Card]] = field(default_factory=dict)
    deck: list[Card] = field(default_factory=list)


class SnapshotParser:
    """Build a private snapshot; publish only a complete, validated ADMIN_END."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._incoming: Snapshot | None = None
        self._deck_seen = False

    def feed(self, command: str, fields: list[str]) -> Snapshot | None:
        try:
            return self._feed(command, fields)
        except (ValueError, IndexError, TypeError):
            self.reset()
            raise ProtocolError() from None

    def _feed(self, command: str, fields: list[str]) -> Snapshot | None:
        if command == "ADMIN_BEGIN":
            if self._incoming is not None or len(fields) != 3 or fields[0] not in PHASE_NAMES:
                raise ProtocolError()
            self._incoming = Snapshot(fields[0], _number(fields[1], -1), _number(fields[2]))
            self._deck_seen = False
            return None
        state = self._incoming
        if state is None:
            raise ProtocolError()
        if command == "ADMIN_PLAYER":
            if (len(fields) != 4 or not fields[1] or not _safe_field(fields[1])
                    or fields[2] not in ("0", "1") or fields[3] not in ("0", "1")):
                raise ProtocolError()
            pid = _number(fields[0])
            if pid in state.players:
                raise ProtocolError()
            state.players[pid] = Player(pid, fields[1], fields[2] == "1", fields[3] == "1")
        elif command == "ADMIN_HAND":
            if not fields:
                raise ProtocolError()
            pid = _number(fields[0])
            if pid in state.hands:
                raise ProtocolError()
            state.hands[pid] = [parse_card(value) for value in fields[1:]]
        elif command == "ADMIN_DECK":
            if self._deck_seen:
                raise ProtocolError()
            state.deck = [parse_card(value) for value in fields]
            self._deck_seen = True
        elif command == "ADMIN_END":
            if (fields or not self._deck_seen or state.players.keys() != state.hands.keys()
                    or (state.host_id != -1 and state.host_id not in state.players)):
                raise ProtocolError()
            cards = [card for hand in state.hands.values() for card in hand] + state.deck
            if len({card_id for card_id, _ in cards}) != len(cards):
                raise ProtocolError()
            self.reset()
            return state
        else:
            raise ProtocolError()
        return None


class Connection:
    """Own the socket on a worker thread; communicate with Tk only via a queue."""

    MAX_LINE = 1024 * 1024

    def __init__(self, events: queue.Queue[Event]) -> None:
        self.events = events
        self.sock: socket.socket | None = None
        self.running = False
        self.authenticated = False
        self._generation = 0
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()

    def connect(self, host: str, port: int, password: str) -> None:
        if not password or not _safe_field(password) or len(password.encode("utf-8")) > 128:
            raise ValueError("Invalid credentials")
        self.close()
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        self.running = True
        generation = self._generation
        # Thread retains its arguments while reading; consume this one-item box so
        # it cannot retain the password after the handshake for the whole session.
        self._thread = threading.Thread(target=self._connect,
                                        args=(host, port, [password], generation), daemon=True)
        self._thread.start()

    def _post(self, generation: int, command: str, *fields: str) -> None:
        with self._state_lock:
            if generation == self._generation:
                self.events.put((command, list(fields)))

    def _connect(self, host: str, port: int, credentials: list[str], generation: int) -> None:
        password = credentials.pop()
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=8)
            sock.settimeout(0.5)
            with self._state_lock:
                if generation != self._generation or not self.running:
                    sock.close()
                    return
                self.sock = sock
            sock.sendall(("ADMIN\t" + password + "\n").encode("utf-8"))
            password = ""
            self._reader(sock, generation)
        except OSError:
            self._post(generation, "ERROR", "CONNECTION_FAILED")
            if sock is not None:
                sock.close()
            with self._state_lock:
                if generation == self._generation:
                    self.running = self.authenticated = False
                    self.sock = None

    def send(self, command: str, *arguments: object) -> None:
        if command not in {"ADMIN_STATE", "ADMIN_HOST", "ADMIN_ADD", "ADMIN_BOMB", "ADMIN_PASSWORD"}:
            raise ValueError("Invalid administrator command")
        fields = [command, *(str(argument) for argument in arguments)]
        if not all(_safe_field(value) for value in fields):
            raise ValueError("Invalid command arguments")
        sock = self.sock
        if sock is None or not self.running or not self.authenticated:
            raise ConnectionError("Administrator is not authenticated")
        try:
            with self._send_lock:
                sock.sendall(("\t".join(fields) + "\n").encode("utf-8"))
        except OSError:
            self._post(self._generation, "ERROR", "SEND_FAILED")
            raise ConnectionError("Administrator connection failed") from None

    def close(self) -> None:
        with self._state_lock:
            self._generation += 1
            self.running = self.authenticated = False
            sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        # A still-connecting worker is invalidated by its generation, without blocking Tk.
        self._thread = None

    def _reader(self, sock: socket.socket, generation: int) -> None:
        buffered = b""
        deadline = time.monotonic() + 8
        error_reported = False
        try:
            while self.running and generation == self._generation:
                if not self.authenticated and time.monotonic() > deadline:
                    raise ProtocolError()
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if generation != self._generation:
                    return
                if not chunk:
                    if buffered:
                        raise ProtocolError()
                    break
                buffered += chunk
                while b"\n" in buffered:
                    raw, buffered = buffered.split(b"\n", 1)
                    if len(raw) > self.MAX_LINE:
                        raise ProtocolError()
                    line = raw.decode("utf-8").removesuffix("\r")
                    parts = line.split("\t")
                    command, fields = parts[0], parts[1:]
                    if command in ("ADMIN_OK", "ADMIN_ERROR"):
                        if len(fields) != 1:
                            raise ProtocolError()
                    elif command not in {"ADMIN_BEGIN", "ADMIN_PLAYER", "ADMIN_HAND", "ADMIN_DECK", "ADMIN_END"}:
                        raise ProtocolError()
                    if not self.authenticated:
                        if command == "ADMIN_OK" and fields == ["AUTHENTICATED"]:
                            with self._state_lock:
                                if generation != self._generation:
                                    return
                                self.authenticated = True
                        elif command != "ADMIN_ERROR":
                            raise ProtocolError()
                    self._post(generation, command, *fields)
                    if command == "ADMIN_ERROR":
                        error_reported = True
                        return
                if len(buffered) > self.MAX_LINE:
                    raise ProtocolError()
        except (UnicodeError, ProtocolError):
            self._post(generation, "ERROR", "PROTOCOL_ERROR")
            error_reported = True
        except OSError:
            self._post(generation, "ERROR", "CONNECTION_FAILED")
            error_reported = True
        finally:
            sock.close()
            with self._state_lock:
                if generation == self._generation:
                    self.sock = None
                    self.running = self.authenticated = False
                    if not error_reported:
                        self.events.put(("ERROR", ["DISCONNECTED"]))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("炸弹猫 · 独立管理员")
        self.geometry("1040x740")
        self.minsize(780, 650)
        self.events: queue.Queue[Event] = queue.Queue()
        self.connection = Connection(self.events)
        self.parser = SnapshotParser()
        self.state = Snapshot()
        self.authenticated = False
        self._connecting = False
        self._pending: str | None = None
        self._pending_deadline: float | None = None
        self._state_deadline: float | None = None
        self.host = tk.StringVar(value="127.0.0.1")
        self.port = tk.StringVar(value="8765")
        self.password = tk.StringVar()
        self.new_password = tk.StringVar()
        self.confirm_password = tk.StringVar()
        self.card_choice = tk.StringVar(value=next(iter(CARD_NAMES.values())))
        self.status = tk.StringVar(value="输入服务器地址与管理密码后登录。")
        self.summary = tk.StringVar(value="尚未登录")
        self._build()
        self._update_controls()
        self._queue_job = self.after(50, self._drain_events)
        self._state_job = self.after(1500, self._poll_state)
        self.protocol("WM_DELETE_WINDOW", self._close)

    @staticmethod
    def _tree(parent: ttk.Frame, columns: tuple[tuple[str, str, int], ...], height: int) -> ttk.Treeview:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        tree = ttk.Treeview(parent, columns=[key for key, _, _ in columns], show="headings", height=height)
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=45, stretch=True)
        yscroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        xscroll = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        return tree

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        login = ttk.LabelFrame(self, text="服务器管理登录", padding=8)
        login.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        login.columnconfigure(1, weight=1)
        for column, label in ((0, "主机 IP"), (2, "端口"), (4, "管理密码")):
            ttk.Label(login, text=label).grid(row=0, column=column, padx=(0, 5))
        self.host_entry = ttk.Entry(login, textvariable=self.host, width=16)
        self.port_entry = ttk.Entry(login, textvariable=self.port, width=6)
        self.password_entry = ttk.Entry(login, textvariable=self.password, show="•", width=18)
        for column, widget in ((1, self.host_entry), (3, self.port_entry), (5, self.password_entry)):
            widget.grid(row=0, column=column, sticky="ew", padx=(0, 8))
        self.password_entry.bind("<Return>", lambda _event: self._login())
        self.login_button = ttk.Button(login, text="登录", command=self._login)
        self.login_button.grid(row=0, column=6, padx=3)
        self.logout_button = ttk.Button(login, text="退出登录", command=self._logout)
        self.logout_button.grid(row=0, column=7, padx=3)
        ttk.Label(login, text="初始默认密码：Admin123；首次登录后请修改。仅在可信局域网使用。"
                  ).grid(row=1, column=0, columnspan=8, sticky="w", pady=(6, 0))
        info = ttk.Frame(self, padding=(10, 2))
        info.grid(row=1, column=0, sticky="ew")
        ttk.Label(info, textvariable=self.summary).pack(anchor="w")
        ttk.Label(info, textvariable=self.status, wraplength=740).pack(anchor="w", pady=(3, 2))
        content = ttk.Frame(self)
        content.grid(row=2, column=0, sticky="nsew", padx=10, pady=5)
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)
        content.rowconfigure(1, weight=2)
        players = ttk.LabelFrame(content, text="玩家（选择管理目标）", padding=5)
        players.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 6))
        self.players_tree = self._tree(players, (("id", "ID", 45), ("name", "昵称", 140),
                                                ("alive", "状态", 65), ("online", "连接", 60),
                                                ("host", "房主", 50), ("cards", "手牌数", 60)), 4)
        self.players_tree.configure(selectmode="browse")
        self.players_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_controls())
        hands = ttk.LabelFrame(content, text="所有玩家手牌（含牌 ID）", padding=5)
        hands.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        self.hands_tree = self._tree(hands, (("player", "玩家", 170), ("id", "牌 ID", 65),
                                            ("type", "牌类型", 200)), 6)
        deck = ttk.LabelFrame(content, text="摸牌堆：从顶到底（第 1 张为下一张）", padding=5)
        deck.grid(row=0, column=1, rowspan=2, sticky="nsew")
        self.deck_tree = self._tree(deck, (("position", "位置", 45), ("id", "牌 ID", 65),
                                          ("type", "牌类型", 160)), 12)
        actions = ttk.LabelFrame(self, text="管理操作（由服务器验证权限与游戏状态）", padding=8)
        actions.grid(row=3, column=0, sticky="ew", padx=10, pady=5)
        actions.columnconfigure(1, weight=1)
        self.host_button = ttk.Button(actions, text="将所选玩家设为房主", command=self._set_host)
        self.host_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.card_selector = ttk.Combobox(actions, textvariable=self.card_choice,
                                           values=list(CARD_NAMES.values()), state="readonly", width=22)
        self.card_selector.grid(row=0, column=1, sticky="ew")
        self.add_button = ttk.Button(actions, text="新增此牌至所选玩家", command=self._add_card)
        self.add_button.grid(row=0, column=2, padx=8)
        self.refresh_button = ttk.Button(actions, text="刷新", command=self._request_state)
        self.refresh_button.grid(row=0, column=3)
        self.bomb_button = ttk.Button(actions, text="结束当前一回合，并向所选玩家注入炸弹猫",
                                      command=self._inject_kitten)
        self.bomb_button.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(7, 0))
        ttk.Label(actions, text="新增牌 / 注入炸弹猫均创建新牌，不从牌堆取牌；注入后由服务器正常处理拆除或出局。",
                  wraplength=740).grid(row=2, column=0, columnspan=4, sticky="w", pady=(5, 0))
        change = ttk.LabelFrame(self, text="修改管理密码（8–128 个 UTF-8 字节；中文通常占 3 字节）", padding=8)
        change.grid(row=4, column=0, sticky="ew", padx=10, pady=(5, 10))
        change.columnconfigure(1, weight=1)
        change.columnconfigure(3, weight=1)
        ttk.Label(change, text="新密码").grid(row=0, column=0, padx=(0, 5))
        self.new_password_entry = ttk.Entry(change, textvariable=self.new_password, show="•", width=18)
        self.new_password_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        ttk.Label(change, text="确认新密码").grid(row=0, column=2, padx=(0, 5))
        self.confirm_password_entry = ttk.Entry(change, textvariable=self.confirm_password, show="•", width=18)
        self.confirm_password_entry.grid(row=0, column=3, sticky="ew", padx=(0, 10))
        self.password_button = ttk.Button(change, text="修改密码", command=self._change_password)
        self.password_button.grid(row=0, column=4)

    def _login(self) -> None:
        if self.authenticated or self._connecting:
            return
        password = self.password.get()
        self.password.set("")
        try:
            port = int(self.port.get())
            host = self.host.get().strip()
            if not host or not 1 <= port <= 65535:
                raise ValueError()
            self.connection.connect(host, port, password)
        except (ValueError, OSError):
            self.status.set("地址、端口或密码格式无效，无法连接。")
            return
        self._connecting = True
        self.status.set("正在连接并验证管理员身份……")
        self._update_controls()

    def _logout(self, status: str = "已退出管理登录。") -> None:
        self.connection.close()
        self.authenticated = self._connecting = False
        self._pending = None
        self._pending_deadline = None
        self._state_deadline = None
        self.parser.reset()
        self.state = Snapshot()
        for variable in (self.password, self.new_password, self.confirm_password):
            variable.set("")
        while not self.events.empty():
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        self._render()
        self.status.set(status)

    def _selected_player(self) -> Player | None:
        selected = self.players_tree.selection()
        return self.state.players.get(int(selected[0])) if selected else None

    def _update_controls(self) -> None:
        ready = self.authenticated and self._pending is None
        player = self._selected_player()
        living = ready and player is not None and player.alive
        for widget, enabled in ((self.login_button, not self.authenticated and not self._connecting),
                                (self.logout_button, self.authenticated or self._connecting),
                                (self.refresh_button, ready), (self.password_button, ready),
                                (self.host_button, ready and player is not None and player.online),
                                (self.add_button, living), (self.bomb_button, living)):
            widget.state(["!disabled"] if enabled else ["disabled"])
        for widget in (self.host_entry, self.port_entry, self.password_entry):
            widget.state(["disabled"] if self.authenticated or self._connecting else ["!disabled"])
        for widget in (self.new_password_entry, self.confirm_password_entry):
            widget.state(["!disabled"] if ready else ["disabled"])
        self.card_selector.configure(state="readonly" if ready else "disabled")

    def _send_mutation(self, command: str, *arguments: object) -> None:
        if not self.authenticated or self._pending is not None:
            return
        try:
            self.connection.send(command, *arguments)
        except (ValueError, ConnectionError):
            self._logout("管理连接异常，已退出登录。")
            return
        self._pending = command
        self._pending_deadline = time.monotonic() + 10
        self.status.set("等待服务器执行管理操作……")
        self._update_controls()

    def _set_host(self) -> None:
        player = self._selected_player()
        if player is not None and player.online:
            self._send_mutation("ADMIN_HOST", player.player_id)

    def _add_card(self) -> None:
        player = self._selected_player()
        kind = next((kind for kind, label in CARD_NAMES.items() if label == self.card_choice.get()), None)
        if player is not None and player.alive and kind is not None:
            self._send_mutation("ADMIN_ADD", player.player_id, kind)

    def _inject_kitten(self) -> None:
        player = self._selected_player()
        if player is not None and player.alive:
            self._send_mutation("ADMIN_BOMB", player.player_id)

    def _change_password(self) -> None:
        if not self.authenticated or self._pending is not None:
            return
        password = self.new_password.get()
        if password != self.confirm_password.get():
            messagebox.showerror("无法修改密码", "两次输入的新密码不一致。", parent=self)
            return
        if not valid_password(password):
            messagebox.showerror("无法修改密码", "密码须为 8–128 个 UTF-8 字节，不能含制表符、换行或空字符。", parent=self)
            return
        self.new_password.set("")
        self.confirm_password.set("")
        self._send_mutation("ADMIN_PASSWORD", password)

    def _request_state(self, force: bool = False) -> None:
        if not self.authenticated or (self._state_deadline is not None and not force):
            return
        try:
            self.connection.send("ADMIN_STATE")
        except (ValueError, ConnectionError):
            self._logout("管理连接异常，已退出登录。")
            return
        self._state_deadline = time.monotonic() + 10

    def _poll_state(self) -> None:
        if self.authenticated:
            now = time.monotonic()
            if self._pending_deadline is not None and now > self._pending_deadline:
                self._logout("服务器操作响应超时，执行结果未知；已退出登录，请重新登录查看状态。")
            elif self._state_deadline is not None and now > self._state_deadline:
                self._logout("服务器状态响应超时，已退出登录。")
            else:
                self._request_state()
        self._state_job = self.after(1500, self._poll_state)

    def _drain_events(self) -> None:
        for _ in range(200):
            try:
                command, fields = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(command, fields)
            except (ProtocolError, ValueError, IndexError, TypeError):
                self._logout("服务器管理协议异常，已退出登录。")
        self._queue_job = self.after(50, self._drain_events)

    def _handle(self, command: str, fields: list[str]) -> None:
        if command == "ERROR":
            self._logout("管理连接已断开或发生异常，请重新登录。")
        elif command == "ADMIN_ERROR":
            if len(fields) != 1:
                raise ProtocolError()
            detail = ERROR_TEXT.get(fields[0], "服务器拒绝了该操作。")
            self._logout(detail + " 已退出登录。")
        elif command == "ADMIN_OK":
            if len(fields) != 1:
                raise ProtocolError()
            code = fields[0]
            if code == "AUTHENTICATED" and not self.authenticated:
                self.authenticated = True
                self._connecting = False
                self.status.set("管理登录成功；状态每 1.5 秒自动刷新。")
                self._request_state()
            elif self.authenticated and SUCCESS_COMMANDS.get(code) == self._pending and self._pending is not None:
                self._pending = None
                self._pending_deadline = None
                self.status.set(SUCCESS_TEXT[code])
                self._request_state(force=True)
            else:
                raise ProtocolError()
            self._update_controls()
        else:
            if not self.authenticated:
                raise ProtocolError()
            state = self.parser.feed(command, fields)
            if state is not None:
                self.state = state
                self._state_deadline = None
                self._render()

    def _render(self) -> None:
        selected = self.players_tree.selection()
        for tree in (self.players_tree, self.hands_tree, self.deck_tree):
            children = tree.get_children()
            if children:
                tree.delete(*children)
        for pid, player in self.state.players.items():
            self.players_tree.insert("", "end", iid=str(pid), values=(
                pid, player.name, "存活" if player.alive else "出局",
                "在线" if player.online else "离线", "房主" if pid == self.state.host_id else "",
                len(self.state.hands.get(pid, []))))
            for card_id, kind in self.state.hands.get(pid, []):
                self.hands_tree.insert("", "end", values=(f"{player.name} [ID {pid}]", card_id, CARD_NAMES[kind]))
        if selected and self.players_tree.exists(selected[0]):
            self.players_tree.selection_set(selected[0])
        for position, (card_id, kind) in enumerate(self.state.deck, 1):
            self.deck_tree.insert("", "end", values=(position, card_id, CARD_NAMES[kind]))
        host = self.state.players.get(self.state.host_id)
        self.summary.set((f"阶段：{PHASE_NAMES[self.state.phase]}  |  第 {self.state.round_number} 局  |  "
                          f"房主：{host.name + ' [ID ' + str(host.player_id) + ']' if host else '无'}  |  "
                          f"牌堆：{len(self.state.deck)} 张") if self.authenticated else "尚未登录")
        self._update_controls()

    def _close(self) -> None:
        self.connection.close()
        self.after_cancel(self._queue_job)
        self.after_cancel(self._state_job)
        self.destroy()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()

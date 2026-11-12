"""Tkinter view for the authoritative C++ LAN server."""
from __future__ import annotations

import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

try:
    from .i18n import LANGUAGES, card_name, event_text, text
    from .presentation import (CAT_TYPES, CARD_ORDER, HandHighlights, PlayerView,
                               State, lost_cards, parse_card, response_description, sorted_hand)
except ImportError:
    from i18n import LANGUAGES, card_name, event_text, text
    from presentation import (CAT_TYPES, CARD_ORDER, HandHighlights, PlayerView,
                              State, lost_cards, parse_card, response_description, sorted_hand)


class Connection:
    def __init__(self, events: queue.Queue[tuple[str, list[str]]]) -> None:
        self.events = events
        self.sock: socket.socket | None = None
        self.running = False
        self._reader_thread: threading.Thread | None = None

    def connect(self, host: str, port: int, name: str) -> None:
        if self.running:
            return
        self.close()
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break
        self.sock = socket.create_connection((host, port), timeout=8)
        self.sock.settimeout(None)
        self.running = True
        self.send("JOIN", name)
        self._reader_thread = threading.Thread(target=self._reader, daemon=True)
        self._reader_thread.start()

    def send(self, command: str, *arguments: object) -> None:
        if not self.sock or not self.running:
            return
        fields = [command, *(str(argument) for argument in arguments)]
        payload = ("\t".join(fields) + "\n").encode("utf-8")
        try:
            self.sock.sendall(payload)
        except OSError as error:
            self.events.put(("ERROR", [str(error)]))
            self.running = False

    def close(self) -> None:
        self.running = False
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()
            self.sock = None
        if self._reader_thread is not None and self._reader_thread is not threading.current_thread():
            self._reader_thread.join(timeout=1)
        self._reader_thread = None

    def _reader(self) -> None:
        sock = self.sock
        assert sock is not None
        buffered = b""
        try:
            while self.running:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffered += chunk
                while b"\n" in buffered:
                    raw, buffered = buffered.split(b"\n", 1)
                    line = raw.decode("utf-8", errors="replace").rstrip("\r")
                    if line:
                        parts = line.split("\t")
                        self.events.put((parts[0], parts[1:]))
        except OSError as error:
            if self.running:
                self.events.put(("ERROR", [str(error)]))
        finally:
            self.running = False
            self.events.put(("ERROR", ["connection closed"]))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.geometry("1100x760")
        self.minsize(420, 500)
        self.events: queue.Queue[tuple[str, list[str]]] = queue.Queue()
        self.connection = Connection(self.events)
        self.state = State()
        self._incoming: State | None = None
        self.player_id = -1
        self.selected_cards: list[int] = []
        self.highlights = HandHighlights()
        self._card_widgets: dict[int, ttk.Checkbutton] = {}
        self._highlight_jobs: set[str] = set()
        self._lost_cards: list[tuple[int, str]] = []
        self._loss_deadline = 0.0
        self._loss_job: str | None = None
        self._bindings: list[tuple[tk.Widget, str]] = []
        self._log_entries: list[tuple[str, object]] = []
        self._last_response: tuple | None = None
        self._join_error = False
        self.language_name = tk.StringVar(value="简体中文")
        self.host = tk.StringVar(value="127.0.0.1")
        self.port = tk.IntVar(value=8765)
        self.name = tk.StringVar(value="")
        self.fast_mode = tk.BooleanVar(value=False)
        self.free_attack_mode = tk.BooleanVar(value=False)
        self._auto_prompt_job: str | None = None
        self._prompt_dialog: tk.Toplevel | None = None
        self._prompt_key: tuple | None = None
        self._prompt_busy = False
        self._prompt_submitted: tuple | None = None
        self._view_width = 0
        self._action_buttons: list[ttk.Button] = []
        self.status = tk.StringVar(value="")
        self.style = ttk.Style(self)
        self.style.configure("NewCard.TCheckbutton", background="#FFF0B3", foreground="#754700")
        self.style.map("NewCard.TCheckbutton", background=[("active", "#FFE28A")])
        self._build_language_bar()
        self._build_login()
        self._change_language()
        self._poll_id = self.after(50, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._close)

    @property
    def language(self) -> str:
        return LANGUAGES[self.language_name.get()]

    def tr(self, key: str, **values: object) -> str:
        return text(self.language, key, **values)

    def _bind_text(self, widget: tk.Widget, key: str) -> tk.Widget:
        self._bindings.append((widget, key))
        widget.configure(text=self.tr(key))
        return widget

    def _build_language_bar(self) -> None:
        toolbar = ttk.Frame(self, padding=(10, 6))
        toolbar.pack(fill="x")
        chooser = ttk.Combobox(toolbar, textvariable=self.language_name,
                               values=list(LANGUAGES), state="readonly", width=12)
        chooser.pack(side="right")
        self._bind_text(ttk.Label(toolbar), "language").pack(side="right", padx=8)
        chooser.bind("<<ComboboxSelected>>", self._change_language)

    def _build_login(self) -> None:
        self.login = ttk.Frame(self, padding=24)
        self.login.pack(expand=True)
        self._bind_text(ttk.Label(self.login, font=("Microsoft YaHei UI", 22, "bold")),
                        "title").grid(row=0, column=0, columnspan=2, pady=(0, 20))
        for row, (key, variable) in enumerate((("host", self.host), ("port", self.port), ("name", self.name)), 1):
            self._bind_text(ttk.Label(self.login), key).grid(row=row, column=0, sticky="e", padx=8, pady=8)
            ttk.Entry(self.login, textvariable=variable, width=28).grid(row=row, column=1, sticky="w", padx=8, pady=8)
        self._bind_text(ttk.Button(self.login, command=self._join), "join").grid(
            row=4, column=0, columnspan=2, pady=16)
        ttk.Label(self.login, textvariable=self.status, wraplength=440).grid(row=5, column=0, columnspan=2)

    def _build_game(self) -> None:
        self.game = ttk.Frame(self, padding=10)
        self.game.pack(fill="both", expand=True)
        self.game.columnconfigure(0, weight=1)
        self.game.columnconfigure(1, weight=2)
        self.game.rowconfigure(3, weight=1)
        self.summary_info = ttk.Label(self.game, textvariable=self.status, wraplength=1040)
        self.summary_info.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self.risk_info = ttk.Label(self.game, font=("Microsoft YaHei UI", 10, "bold"))
        self.risk_info.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.response_info = ttk.Label(self.game, wraplength=1040, foreground="#9C3E12",
                                       font=("Microsoft YaHei UI", 11, "bold"))
        self.response_info.grid(row=2, column=0, columnspan=2, sticky="ew", pady=6)
        self.play_area = ttk.Frame(self.game)
        self.play_area.grid(row=3, column=0, columnspan=2, sticky="nsew")
        self.play_area.columnconfigure(0, weight=1)
        self.play_area.columnconfigure(1, weight=2)
        self.play_area.rowconfigure(0, weight=1)
        self.players_box = self._bind_text(ttk.LabelFrame(self.play_area, padding=6), "players")
        self.players_box.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.hand_box = self._bind_text(ttk.LabelFrame(self.play_area, padding=6), "hand")
        self.hand_box.grid(row=0, column=1, sticky="nsew")
        self.hand_box.columnconfigure(0, weight=1)
        self.hand_box.rowconfigure(0, weight=1)
        self.hand_canvas = tk.Canvas(self.hand_box, highlightthickness=0, width=160, height=100, takefocus=True)
        scroll = ttk.Scrollbar(self.hand_box, orient="vertical", command=self.hand_canvas.yview)
        self.hand_inner = ttk.Frame(self.hand_canvas)
        self.hand_inner.bind("<Configure>", lambda _: self.hand_canvas.configure(scrollregion=self.hand_canvas.bbox("all")))
        self._hand_window = self.hand_canvas.create_window((0, 0), window=self.hand_inner, anchor="nw")
        self.hand_inner.columnconfigure(0, weight=1)
        self.hand_canvas.bind("<Configure>", lambda event: self.hand_canvas.itemconfigure(self._hand_window, width=event.width))
        self._bind_hand_scroll(self.hand_canvas)
        self._bind_hand_scroll(self.hand_inner)
        self.hand_canvas.configure(yscrollcommand=scroll.set)
        self.hand_canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.gain_info = ttk.Label(self.hand_box, wraplength=550, foreground="#754700")
        self.gain_info.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.loss_info = ttk.Label(self.hand_box, wraplength=550, foreground="#A52A2A")
        self.loss_info.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.controls = ttk.Frame(self.game, padding=(0, 6, 0, 0))
        self.controls.grid(row=4, column=0, columnspan=2, sticky="ew")
        buttons = (("draw", lambda: self.connection.send("DRAW")),
                   ("play", self._play_selected), ("combo", self._combo),
                   ("pass", lambda: self.connection.send("PASS")),
                   ("nope", self._play_nope), ("kick", self._kick_player),
                   ("start", lambda: self.connection.send("START")),
                   ("restart", self._restart))
        for key, command in buttons:
            button = self._bind_text(ttk.Button(self.controls, command=command, width=1), key)
            self._action_buttons.append(button)
            setattr(self, key + "_button", button)
        self.options_area = ttk.Frame(self.game)
        self.options_area.grid(row=5, column=0, columnspan=2, sticky="ew")
        self.fast_button = self._bind_text(ttk.Checkbutton(self.options_area, variable=self.fast_mode,
                                                         command=self._set_fast_mode), "fast_option")
        self.fast_button.pack(anchor="w")
        self.free_attack_button = self._bind_text(ttk.Checkbutton(self.options_area, variable=self.free_attack_mode,
                                                         command=self._set_free_attack_mode), "free_attack_option")
        self.free_attack_button.pack(anchor="w")
        self.mode_info = ttk.Label(self.options_area)
        self.mode_info.pack(anchor="w")
        self.log = tk.Text(self.game, height=4, width=1, state="disabled", wrap="word")
        self.log.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.discard_info = ttk.Label(self.game, wraplength=1040)
        self.discard_info.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.risk_hint = self._bind_text(ttk.Label(self.game, wraplength=1040), "risk_hint")
        self.risk_hint.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.game.bind("<Configure>", self._resize_game)
        self._resize_game()

    def _resize_game(self, event=None) -> None:
        width = event.width if event is not None else max(self.winfo_width() - 20, 400)
        if width == self._view_width:
            return
        self._view_width = width
        columns = 4 if width >= 760 else 3 if width >= 530 else 2
        for column in range(4):
            self.controls.columnconfigure(column, weight=1 if column < columns else 0, minsize=0)
        for index, button in enumerate(self._action_buttons):
            button.grid(row=index // columns, column=index % columns, sticky="ew", padx=2, pady=2)
        narrow = width < 660
        if narrow:
            self.players_box.grid(row=0, column=0, columnspan=2, sticky="ew", padx=0)
            self.hand_box.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(4, 0))
            self.play_area.rowconfigure(0, weight=0)
            self.play_area.rowconfigure(1, weight=1)
        else:
            self.players_box.grid(row=0, column=0, columnspan=1, sticky="nsew", padx=(0, 6))
            self.hand_box.grid(row=0, column=1, columnspan=1, sticky="nsew", pady=0)
            self.play_area.rowconfigure(0, weight=1)
            self.play_area.rowconfigure(1, weight=0)
        for widget in (self.summary_info, self.risk_info, self.response_info,
                       self.discard_info, self.risk_hint, self.mode_info):
            widget.configure(wraplength=max(250, width - 20))
        hand_width = width - 40 if narrow else int(width * 0.64) - 20
        for widget in (self.gain_info, self.loss_info):
            widget.configure(wraplength=max(200, hand_width))
        self.log.configure(height=2 if narrow else 4)
        self._layout_players()

    def _layout_players(self) -> None:
        narrow = self._view_width < 660
        for index, label in enumerate(self.players_box.winfo_children()):
            label.grid(row=index // 2 if narrow else index, column=index % 2 if narrow else 0,
                       sticky="w", padx=3, pady=1)
            label.configure(wraplength=max(160, (self._view_width - 60) // 2) if narrow else 230)
        self.players_box.columnconfigure(0, weight=1)
        self.players_box.columnconfigure(1, weight=1 if narrow else 0)

    def _bind_hand_scroll(self, widget) -> None:
        widget.bind("<MouseWheel>", self._scroll_hand)
        widget.bind("<Button-4>", self._scroll_hand)
        widget.bind("<Button-5>", self._scroll_hand)
        widget.bind("<Up>", lambda _: self._scroll_hand_units(-1))
        widget.bind("<Down>", lambda _: self._scroll_hand_units(1))
        widget.bind("<Enter>", lambda _: widget.focus_set())

    def _scroll_hand_units(self, units: int):
        self.hand_canvas.yview_scroll(units, "units")
        return "break"

    def _scroll_hand(self, event):
        if getattr(event, "num", None) == 4:
            units = -1
        elif getattr(event, "num", None) == 5:
            units = 1
        else:
            delta = getattr(event, "delta", 0)
            units = -int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
        return self._scroll_hand_units(units)

    def _change_language(self, _event=None) -> None:
        self.title(self.tr("title"))
        live_bindings = []
        for widget, key in self._bindings:
            if widget.winfo_exists():
                widget.configure(text=self.tr(key))
                live_bindings.append((widget, key))
        self._bindings = live_bindings
        if hasattr(self, "game"):
            self._render()
            self._render_log()
        else:
            self.status.set(self.tr("login_hint"))

    def _join(self) -> None:
        try:
            name = self.name.get().strip() or self.tr("default_name")
            if any(char in name for char in "\r\n\t"):
                messagebox.showerror(self.tr("connection_failed"), self.tr("invalid_name"), parent=self)
                return
            self._join_error = False
            self.connection.connect(self.host.get().strip(), int(self.port.get()), name)
            self.status.set(self.tr("connecting"))
        except (OSError, ValueError, tk.TclError) as error:
            messagebox.showerror(self.tr("connection_failed"), str(error), parent=self)

    def _drain_events(self) -> None:
        while True:
            try:
                kind, parts = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(kind, parts)
            except (ValueError, IndexError, KeyError):
                self._log("message", ("protocol_error", {}))
        self._poll_id = self.after(50, self._drain_events)

    def _handle(self, kind: str, parts: list[str]) -> None:
        if kind == "WELCOME" and parts:
            self.player_id = int(parts[0])
            if not hasattr(self, "game"):
                self.login.destroy()
                self._build_game()
                self._log("message", ("joined", {}))
        elif kind == "ROSTER":
            self.state = State()
            for index in range(0, len(parts) - 1, 2):
                player_id = int(parts[index])
                self.state.players[player_id] = PlayerView(player_id, parts[index + 1], True, 0)
            self._log("message", ("roster", {}))
            self._render()
        elif kind == "STATE":
            self._incoming = State.from_fields(parts)
        elif kind == "PLAYER" and self._incoming is not None:
            player_id, name, alive, hand_size = parts[:4]
            self._incoming.players[int(player_id)] = PlayerView(
                int(player_id), name, alive == "1", int(hand_size), parts[4] if len(parts) > 4 else "-")
        elif kind == "ROUND" and self._incoming is not None:
            self._incoming.round_number = int(parts[0])
        elif kind == "SESSION":
            target = self._incoming if self._incoming is not None else self.state
            target.host_player = int(parts[0])
            target.connected_count = int(parts[1])
            if self._incoming is None:
                self._render()
        elif kind == "ONLINE":
            target = self._incoming if self._incoming is not None else self.state
            target.online_players = [int(value) for value in parts]
        elif kind == "PROMPT" and self._incoming is not None:
            self._incoming.prompt_player = int(parts[0])
        elif kind == "REQUESTED" and self._incoming is not None:
            self._incoming.requested_card = parts[0]
        elif kind == "MODE" and self._incoming is not None:
            self._incoming.fast_mode = parts[0] == "1"
            self._incoming.free_attack_mode = len(parts) > 1 and parts[1] == "1"
        elif kind == "OPTIONS":
            target = self._incoming if self._incoming is not None else self.state
            target.next_fast_mode = parts[0] == "1"
            target.next_free_attack_mode = len(parts) > 1 and parts[1] == "1"
            if self._incoming is None:
                self._render()
        elif kind == "DECK" and self._incoming is not None:
            self._incoming.bombs_remaining = int(parts[0])
        elif kind == "PENDING" and self._incoming is not None:
            self._incoming.pending_actor = int(parts[0])
            self._incoming.pending_cards = parts[1:]
        elif kind == "DISCARD" and self._incoming is not None:
            self._incoming.discard = [parts[0]]
        elif kind == "HAND" and self._incoming is not None:
            self._incoming.hand = sorted_hand([parse_card(token) for token in parts])
        elif kind == "ENDSTATE" and self._incoming is not None:
            self._apply_snapshot()
        elif kind == "EVENT" and len(parts) >= 2:
            self._log("event", parts[1])
            if parts[0] == "ERROR":
                self.status.set(event_text(self.language, parts[1]))
                if parts[1] in {"INVALID_KITTEN_POSITION", "DEFUSE_NOT_AVAILABLE", "CARD_NOT_IN_HAND"}:
                    self._prompt_submitted = None
                    self._schedule_prompt()
            if parts[1] == "KICKED":
                self.connection.close()
                self._update_controls()
            if parts[0] == "ERROR":
                if not hasattr(self, "game"):
                    self._join_error = True
                    messagebox.showerror(self.tr("connection_failed"),
                                         event_text(self.language, parts[1]), parent=self)
        elif kind == "PRIVATE" and parts and parts[0] == "SEE_FUTURE":
            names = [card_name(self.language, parse_card(token)[1]) for token in parts[1:]]
            messagebox.showinfo(self.tr("future_title"),
                                self.tr("future_prompt", cards="\n".join(names) or self.tr("empty")), parent=self)
        elif kind == "ERROR":
            if self._join_error and not hasattr(self, "game") and parts == ["connection closed"]:
                return
            key = "disconnected" if parts == ["connection closed"] else "connection_error"
            values = {} if key == "disconnected" else {"detail": " ".join(parts)}
            self.status.set(self.tr(key, **values))
            self._log("message", (key, values))
            if hasattr(self, "game"):
                self._update_controls()
            self._schedule_prompt()

    def _apply_snapshot(self) -> None:
        assert self._incoming is not None
        new_round = self._incoming.round_number != self.state.round_number
        next_me = self._incoming.players.get(self.player_id)
        removed = (lost_cards(self.state.hand, self._incoming.hand)
                   if not new_round and next_me is not None and next_me.alive else [])
        if new_round:
            if self._loss_job is not None:
                self.after_cancel(self._loss_job)
                self._loss_job = None
            self._lost_cards = []
            self._loss_deadline = 0.0
            for job in self._highlight_jobs:
                self.after_cancel(job)
            self._highlight_jobs.clear()
            self.highlights = HandHighlights()
            self.selected_cards.clear()
            self._last_response = None
        self.state = self._incoming
        self._incoming = None
        gained = self.highlights.update(self.state.hand, time.monotonic())
        if removed:
            self._lost_cards = removed
            self._loss_deadline = time.monotonic() + 4.0
            if self._loss_job is not None:
                self.after_cancel(self._loss_job)
            self._loss_job = self.after(4020, self._expire_loss_notice)
            self._log("lost", tuple(kind for _, kind in removed))
        current_ids = {card_id for card_id, _ in self.state.hand}
        self.selected_cards = [card_id for card_id in self.selected_cards if card_id in current_ids]
        if gained:
            def expire_highlights():
                self._highlight_jobs.discard(job)
                self._refresh_highlights()

            job = self.after(int(self.highlights.duration * 1000) + 20, expire_highlights)
            self._highlight_jobs.add(job)
        signature = (self.state.pending_actor, self.state.pending_type, self.state.requested_card,
                     tuple(self.state.pending_cards), self.state.pending_nopes, self.state.response_leader)
        if self.state.phase == "RESPONSE":
            if signature != self._last_response:
                self._log("response", self.state)
            self._last_response = signature
        else:
            self._last_response = None
        self._render()
        self._schedule_prompt()

    def _render(self) -> None:
        if not hasattr(self, "game"):
            return
        self.status.set(self.tr("summary", phase=self.tr(self.state.phase),
                                player=self.state.player_name(self.state.active_player, self.language),
                                turns=self.state.turns_remaining, draw=self.state.draw_count,
                                discard=self.state.discard_count))
        if self.state.winner >= 0:
            self.status.set(self.tr("winner", player=self.state.player_name(self.state.winner, self.language)))
        probability = self.state.bomb_probability
        self.risk_info.configure(text=self.tr("risk",
            bombs=self.state.bombs_remaining if self.state.bombs_remaining is not None else "—",
            probability=f"{probability:.2%}" if probability is not None else "—"))
        self.response_info.configure(text=response_description(self.state, self.language))
        top_discard = (card_name(self.language, parse_card(self.state.discard[-1])[1])
                       if self.state.discard else self.tr("empty"))
        self.discard_info.configure(text=self.tr("discard", cards=top_discard))
        self.fast_mode.set(self.state.next_fast_mode)
        self.free_attack_mode.set(self.state.next_free_attack_mode)
        mode = self.tr("fast_mode" if self.state.fast_mode else "standard_mode")
        if self.state.free_attack_mode:
            mode += " + " + self.tr("free_attack_mode")
        self.mode_info.configure(text=self.tr("current_mode", mode=mode))
        for child in self.players_box.winfo_children():
            child.destroy()
        for player in sorted(self.state.players.values(), key=lambda item: item.player_id):
            label = self.tr("player_row", marker="▶ " if player.player_id == self.state.active_player else "",
                            name=player.name, you=self.tr("you") if player.player_id == self.player_id else "",
                            alive=self.tr("alive" if player.alive else "out"), count=player.hand_size)
            if player.exploded != "-":
                label += self.tr("exploded")
            ttk.Label(self.players_box, text=label, wraplength=230)
        self._layout_players()
        focus = self.focus_get()
        focused_card = next((cid for cid, widget in self._card_widgets.items() if widget == focus), None)
        for child in self.hand_inner.winfo_children():
            child.destroy()
        self._card_widgets.clear()
        for row, (card_id, kind) in enumerate(sorted_hand(self.state.hand)):
            selected = tk.BooleanVar(value=card_id in self.selected_cards)
            button = ttk.Checkbutton(self.hand_inner, text=card_name(self.language, kind), variable=selected,
                                      command=lambda cid=card_id, var=selected: self._toggle_card(cid, var))
            button.grid(row=row, column=0, sticky="w", pady=3, padx=4)
            self._bind_hand_scroll(button)
            self._card_widgets[card_id] = button
        if focused_card in self._card_widgets:
            self._card_widgets[focused_card].focus_set()
        self._refresh_highlights()
        self._update_controls()

    def _refresh_highlights(self) -> None:
        if not hasattr(self, "game"):
            return
        active = self.highlights.active(time.monotonic())
        for card_id, widget in self._card_widgets.items():
            widget.configure(style="NewCard.TCheckbutton" if card_id in active else "TCheckbutton")
        names = [card_name(self.language, kind) for card_id, kind in self.state.hand if card_id in active]
        self.gain_info.configure(text=self.tr("gained", cards=", ".join(names)) if names else "")
        self._refresh_loss_notice()

    def _refresh_loss_notice(self) -> None:
        names = [card_name(self.language, kind) for _, kind in self._lost_cards]
        visible = names and time.monotonic() < self._loss_deadline
        self.loss_info.configure(text=self.tr("lost", count=len(names), cards=", ".join(names)) if visible else "")

    def _expire_loss_notice(self) -> None:
        self._loss_job = None
        self._lost_cards = []
        self._refresh_loss_notice()

    def _update_controls(self) -> None:
        connected = self.connection.running
        me = self.state.players.get(self.player_id)
        alive = me is not None and me.alive
        is_turn = connected and alive and self.player_id == self.state.active_player and self.state.phase == "TURN"
        selected = [kind for card_id, kind in self.state.hand if card_id in self.selected_cards]
        can_play = len(selected) == 1 and selected[0] not in (*CAT_TYPES, "DEFUSE", "NOPE", "EXPLODING_KITTEN")
        can_combo = len(selected) in (2, 3) and len(set(selected)) == 1
        can_respond = (connected and alive and self.state.phase == "RESPONSE"
                       and self.player_id != self.state.response_leader)
        enabled = {
            "draw": is_turn, "play": is_turn and can_play, "combo": is_turn and can_combo,
            "pass": can_respond, "nope": can_respond and any(kind == "NOPE" for _, kind in self.state.hand),
            "start": connected and self.player_id == self.state.host_player and self.state.phase == "LOBBY" and len(self.state.players) >= 2,
            "kick": connected and self.player_id == self.state.host_player and any(
                pid != self.player_id for pid in self._online_players()),
            "restart": connected and self.player_id == self.state.host_player and self.state.phase == "GAME_OVER"
                       and (self.state.connected_count if self.state.connected_count is not None else len(self.state.players)) >= 2,
        }
        for key, allowed in enabled.items():
            getattr(self, key + "_button").configure(state="normal" if allowed else "disabled")
        count = self.state.connected_count if self.state.connected_count is not None else len(self.state.players)
        can_configure = (connected and self.player_id == self.state.host_player
                         and self.state.phase in {"LOBBY", "GAME_OVER"} and count <= 3)
        self.fast_button.configure(state="normal" if can_configure else "disabled")
        can_configure_attack = (connected and self.player_id == self.state.host_player
                                and self.state.phase in {"LOBBY", "GAME_OVER"})
        self.free_attack_button.configure(state="normal" if can_configure_attack else "disabled")

    def _toggle_card(self, card_id: int, variable: tk.BooleanVar) -> None:
        if variable.get() and card_id not in self.selected_cards:
            self.selected_cards.append(card_id)
        elif not variable.get() and card_id in self.selected_cards:
            self.selected_cards.remove(card_id)
        self._update_controls()

    def _play_selected(self) -> None:
        if len(self.selected_cards) != 1:
            messagebox.showinfo(self.tr("selection"), self.tr("select_one"), parent=self)
            return
        card_id = self.selected_cards[0]
        kind = next(kind for candidate, kind in self.state.hand if candidate == card_id)
        if kind in CAT_TYPES:
            messagebox.showinfo(self.tr("selection"), self.tr("cat_combo_only"), parent=self)
            return
        if kind in {"DEFUSE", "NOPE", "EXPLODING_KITTEN"}:
            messagebox.showinfo(self.tr("selection"), self.tr("special_card"), parent=self)
            return
        target = self._target() if kind == "FAVOR" or (kind == "ATTACK" and self.state.free_attack_mode) else -1
        if target is not None:
            self.connection.send("PLAY", card_id, target)

    def _combo(self) -> None:
        selected = [kind for card_id, kind in self.state.hand if card_id in self.selected_cards]
        if len(selected) not in (2, 3) or len(set(selected)) != 1:
            messagebox.showinfo(self.tr("selection"), self.tr("select_combo"), parent=self)
            return
        target = self._target()
        if target is None:
            return
        fields: list[object] = [*self.selected_cards, target]
        if len(selected) == 3:
            requested = self._choose("request_title", "request_prompt",
                                     [(kind, card_name(self.language, kind)) for kind in CARD_ORDER])
            if requested is None:
                return
            fields.append(requested)
        self.connection.send("COMBO", *fields)

    def _play_nope(self) -> None:
        if self.state.phase != "RESPONSE" or self.player_id == self.state.response_leader:
            return
        card_id = next((cid for cid, kind in sorted_hand(self.state.hand) if kind == "NOPE"), None)
        if card_id is None:
            return
        self.connection.send("NOPE", card_id)

    def _current_prompt_key(self):
        if not self.connection.running:
            return None
        if self.state.phase == "KITTEN":
            target = self.state.prompt_player if self.state.prompt_player >= 0 else self.state.active_player
            if self.player_id == target:
                return (self.state.round_number, "KITTEN", target)
        if self.state.phase == "FAVOR" and self.player_id == self.state.pending_target:
            return (self.state.round_number, "FAVOR", self.state.pending_target)
        return None

    def _schedule_prompt(self) -> None:
        key = self._current_prompt_key()
        if key != self._prompt_key:
            if self._prompt_dialog is not None and self._prompt_dialog.winfo_exists():
                self._prompt_dialog.destroy()
            if self._auto_prompt_job is not None:
                self.after_cancel(self._auto_prompt_job)
                self._auto_prompt_job = None
            self._prompt_key = key
            self._prompt_submitted = None
        if key is not None and key != self._prompt_submitted and not self._prompt_busy and self._auto_prompt_job is None:
            self._auto_prompt_job = self.after(30, self._open_auto_prompt)

    def _open_auto_prompt(self) -> None:
        if self._auto_prompt_job is not None:
            self.after_cancel(self._auto_prompt_job)
            self._auto_prompt_job = None
        key = self._current_prompt_key()
        if key is None or self._prompt_busy:
            return
        self._prompt_busy = True
        try:
            if key[1] == "KITTEN":
                result = self._choose_position(required=True)
                command = "DEFUSE"
            else:
                result = self._choose("favor_title", "favor_prompt",
                    [(cid, card_name(self.language, kind)) for cid, kind in sorted_hand(self.state.hand)], required=True)
                command = "FAVOR"
            if result is not None and self._current_prompt_key() == key:
                self._prompt_submitted = key
                self.connection.send(command, result)
        finally:
            self._prompt_busy = False
        # A stale prompt can be replaced while the modal window is open.
        if self._current_prompt_key() != key:
            self._schedule_prompt()

    def _choose_position(self, required: bool = False) -> int | None:
        dialog = tk.Toplevel(self)
        if required:
            self._prompt_dialog = dialog
        dialog.title(self.tr("defuse_title"))
        dialog.transient(self)
        dialog.resizable(False, False)
        maximum = self.state.draw_count
        ttk.Label(dialog, text=self.tr("defuse_prompt", maximum=maximum), padding=12).pack()
        value = tk.StringVar(value="0")
        spinbox = ttk.Spinbox(dialog, from_=0, to=maximum, textvariable=value, width=12)
        spinbox.pack(padx=12, pady=6)
        result = []

        def accept(_event=None):
            try:
                position = int(value.get())
            except ValueError:
                spinbox.focus_set()
                return
            if not 0 <= position <= maximum:
                spinbox.focus_set()
                return
            result.append(position)
            dialog.destroy()

        controls = ttk.Frame(dialog, padding=12)
        controls.pack(fill="x")
        ttk.Button(controls, text=self.tr("confirm"), command=accept).pack(side="left")
        if not required:
            ttk.Button(controls, text=self.tr("cancel"), command=dialog.destroy).pack(side="right")
            dialog.bind("<Escape>", lambda _: dialog.destroy())
        else:
            dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        dialog.bind("<Return>", accept)
        dialog.grab_set()
        spinbox.focus_set()
        self.wait_window(dialog)
        if self._prompt_dialog == dialog:
            self._prompt_dialog = None
        return result[0] if result else None

    def _set_fast_mode(self) -> None:
        count = self.state.connected_count if self.state.connected_count is not None else len(self.state.players)
        if (self.connection.running and self.player_id == self.state.host_player
                and self.state.phase in {"LOBBY", "GAME_OVER"} and count <= 3):
            self.connection.send("SET_FAST", int(self.fast_mode.get()))
        self.fast_mode.set(self.state.next_fast_mode)

    def _set_free_attack_mode(self) -> None:
        if (self.connection.running and self.player_id == self.state.host_player
                and self.state.phase in {"LOBBY", "GAME_OVER"}):
            self.connection.send("SET_FREE_ATTACK", int(self.free_attack_mode.get()))
        self.free_attack_mode.set(self.state.next_free_attack_mode)

    def _online_players(self):
        return self.state.online_players if self.state.online_players is not None else list(self.state.players)

    def _kick_player(self) -> None:
        if self.player_id != self.state.host_player:
            return
        options = [(pid, self.state.player_name(pid, self.language))
                   for pid in self._online_players() if pid != self.player_id]
        target = self._choose("kick", "kick_prompt", options)
        if target is not None:
            self.connection.send("KICK", target)

    def _restart(self) -> None:
        if (self.connection.running and self.state.phase == "GAME_OVER"
                and self.player_id == self.state.host_player):
            self.connection.send("RESTART")

    def _target(self) -> int | None:
        options = [(player.player_id, player.name) for player in self.state.players.values()
                   if player.alive and player.player_id != self.player_id]
        return self._choose("target_title", "target_prompt", options)

    def _choose(self, title_key: str, prompt_key: str, options: list[tuple[object, str]], required: bool = False):
        if not options:
            messagebox.showinfo(self.tr("selection"), self.tr("no_target"), parent=self)
            return None
        dialog = tk.Toplevel(self)
        if required:
            self._prompt_dialog = dialog
        dialog.title(self.tr(title_key))
        dialog.transient(self)
        dialog.resizable(False, False)
        ttk.Label(dialog, text=self.tr(prompt_key), padding=12).pack(anchor="w")
        listbox = tk.Listbox(dialog, width=40, height=min(12, len(options)), exportselection=False)
        listbox.pack(fill="both", padx=12, pady=4)
        for _, label in options:
            listbox.insert("end", label)
        listbox.selection_set(0)
        result = []

        def accept(_event=None):
            selection = listbox.curselection()
            if selection:
                result.append(options[selection[0]][0])
                dialog.destroy()

        controls = ttk.Frame(dialog, padding=12)
        controls.pack(fill="x")
        ttk.Button(controls, text=self.tr("confirm"), command=accept).pack(side="left", padx=4)
        if not required:
            ttk.Button(controls, text=self.tr("cancel"), command=dialog.destroy).pack(side="right", padx=4)
            dialog.bind("<Escape>", lambda _: dialog.destroy())
        else:
            dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        listbox.bind("<Double-1>", accept)
        dialog.bind("<Return>", accept)
        dialog.grab_set()
        listbox.focus_set()
        self.wait_window(dialog)
        if self._prompt_dialog == dialog:
            self._prompt_dialog = None
        return result[0] if result else None

    def _log(self, kind: str, data: object) -> None:
        self._log_entries.append((kind, data))
        self._log_entries = self._log_entries[-200:]
        self._render_log()

    def _render_log(self) -> None:
        if not hasattr(self, "log"):
            return
        lines = []
        for kind, data in self._log_entries:
            if kind == "event":
                lines.append(event_text(self.language, data))
            elif kind == "response":
                lines.append(response_description(data, self.language))
            elif kind == "lost":
                lines.append(self.tr("lost", count=len(data),
                                     cards=", ".join(card_name(self.language, card) for card in data)))
            else:
                key, values = data
                lines.append(self.tr(key, **values))
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", "\n".join(lines) + ("\n" if lines else ""))
        self.log.see("end")
        self.log.configure(state="disabled")

    def destroy(self) -> None:
        self.connection.close()
        if self._auto_prompt_job is not None:
            self.after_cancel(self._auto_prompt_job)
            self._auto_prompt_job = None
        if self._prompt_dialog is not None and self._prompt_dialog.winfo_exists():
            self._prompt_dialog.destroy()
        self.after_cancel(self._poll_id)
        for job in self._highlight_jobs:
            self.after_cancel(job)
        self._highlight_jobs.clear()
        if self._loss_job is not None:
            self.after_cancel(self._loss_job)
            self._loss_job = None
        super().destroy()

    def _close(self) -> None:
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

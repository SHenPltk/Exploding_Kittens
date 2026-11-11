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
                               State, parse_card, response_description, sorted_hand)
except ImportError:
    from i18n import LANGUAGES, card_name, event_text, text
    from presentation import (CAT_TYPES, CARD_ORDER, HandHighlights, PlayerView,
                              State, parse_card, response_description, sorted_hand)


class Connection:
    def __init__(self, events: queue.Queue[tuple[str, list[str]]]) -> None:
        self.events = events
        self.sock: socket.socket | None = None
        self.running = False

    def connect(self, host: str, port: int, name: str) -> None:
        if self.running:
            return
        self.sock = socket.create_connection((host, port), timeout=8)
        self.sock.settimeout(None)
        self.running = True
        self.send("JOIN", name)
        threading.Thread(target=self._reader, daemon=True).start()

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
        self.minsize(920, 650)
        self.events: queue.Queue[tuple[str, list[str]]] = queue.Queue()
        self.connection = Connection(self.events)
        self.state = State()
        self._incoming: State | None = None
        self.player_id = -1
        self.selected_cards: list[int] = []
        self.highlights = HandHighlights()
        self._card_widgets: dict[int, ttk.Checkbutton] = {}
        self._highlight_jobs: set[str] = set()
        self._bindings: list[tuple[tk.Widget, str]] = []
        self._log_entries: list[tuple[str, object]] = []
        self._last_response: tuple | None = None
        self.language_name = tk.StringVar(value="简体中文")
        self.host = tk.StringVar(value="127.0.0.1")
        self.port = tk.IntVar(value=8765)
        self.name = tk.StringVar(value="")
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
        ttk.Label(self.game, textvariable=self.status, wraplength=1040).grid(
            row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        self.risk_info = ttk.Label(self.game, font=("Microsoft YaHei UI", 10, "bold"))
        self.risk_info.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.response_info = ttk.Label(self.game, wraplength=1040, foreground="#9C3E12",
                                       font=("Microsoft YaHei UI", 11, "bold"))
        self.response_info.grid(row=2, column=0, columnspan=2, sticky="ew", pady=6)
        self.players_box = self._bind_text(ttk.LabelFrame(self.game, padding=8), "players")
        self.players_box.grid(row=3, column=0, sticky="nsew", padx=(0, 8))
        self.hand_box = self._bind_text(ttk.LabelFrame(self.game, padding=8), "hand")
        self.hand_box.grid(row=3, column=1, sticky="nsew")
        self.hand_box.columnconfigure(0, weight=1)
        self.hand_box.rowconfigure(0, weight=1)
        self.hand_canvas = tk.Canvas(self.hand_box, highlightthickness=0)
        scroll = ttk.Scrollbar(self.hand_box, orient="vertical", command=self.hand_canvas.yview)
        self.hand_inner = ttk.Frame(self.hand_canvas)
        self.hand_inner.bind("<Configure>", lambda _: self.hand_canvas.configure(scrollregion=self.hand_canvas.bbox("all")))
        self.hand_canvas.create_window((0, 0), window=self.hand_inner, anchor="nw")
        self.hand_canvas.configure(yscrollcommand=scroll.set)
        self.hand_canvas.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.gain_info = ttk.Label(self.hand_box, wraplength=550, foreground="#754700")
        self.gain_info.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        controls = ttk.Frame(self.game, padding=(0, 8, 0, 0))
        controls.grid(row=4, column=0, columnspan=2, sticky="ew")
        buttons = (("draw", lambda: self.connection.send("DRAW")),
                   ("play", self._play_selected), ("combo", self._combo),
                   ("pass", lambda: self.connection.send("PASS")),
                   ("nope", self._play_nope), ("resolve", self._resolve_prompt))
        for column, (key, command) in enumerate(buttons):
            controls.columnconfigure(column, weight=1)
            button = self._bind_text(ttk.Button(controls, command=command), key)
            button.grid(row=0, column=column, padx=3, sticky="ew")
            setattr(self, key + "_button", button)
        self.start_button = self._bind_text(ttk.Button(controls, command=lambda: self.connection.send("START")), "start")
        self.start_button.grid(row=1, column=0, columnspan=3, padx=3, pady=(6, 0), sticky="ew")
        self.decline_button = self._bind_text(ttk.Button(controls, command=self._decline_defuse), "decline")
        self.decline_button.grid(row=1, column=3, columnspan=3, padx=3, pady=(6, 0), sticky="ew")
        self.log = tk.Text(self.game, height=6, state="disabled", wrap="word")
        self.log.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.discard_info = ttk.Label(self.game, wraplength=1040)
        self.discard_info.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self._bind_text(ttk.Label(self.game, wraplength=1040), "risk_hint").grid(
            row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))

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
        elif kind == "DECK" and self._incoming is not None:
            self._incoming.bombs_remaining = int(parts[0])
        elif kind == "PENDING" and self._incoming is not None:
            self._incoming.pending_actor = int(parts[0])
            self._incoming.pending_cards = parts[1:]
        elif kind == "DISCARD" and self._incoming is not None:
            self._incoming.discard.append(parts[0])
        elif kind == "HAND" and self._incoming is not None:
            self._incoming.hand = sorted_hand([parse_card(token) for token in parts])
        elif kind == "ENDSTATE" and self._incoming is not None:
            self._apply_snapshot()
        elif kind == "EVENT" and len(parts) >= 2:
            self._log("event", parts[1])
            if parts[0] == "ERROR":
                self.status.set(event_text(self.language, parts[1]))
        elif kind == "PRIVATE" and parts and parts[0] == "SEE_FUTURE":
            names = [card_name(self.language, parse_card(token)[1]) for token in parts[1:]]
            messagebox.showinfo(self.tr("future_title"),
                                self.tr("future_prompt", cards="\n".join(names) or self.tr("empty")), parent=self)
        elif kind == "ERROR":
            key = "disconnected" if parts == ["connection closed"] else "connection_error"
            values = {} if key == "disconnected" else {"detail": " ".join(parts)}
            self.status.set(self.tr(key, **values))
            self._log("message", (key, values))
            if hasattr(self, "game"):
                self._update_controls()

    def _apply_snapshot(self) -> None:
        assert self._incoming is not None
        self.state = self._incoming
        self._incoming = None
        gained = self.highlights.update(self.state.hand, time.monotonic())
        current_ids = {card_id for card_id, _ in self.state.hand}
        self.selected_cards = [card_id for card_id in self.selected_cards if card_id in current_ids]
        if gained:
            def expire_highlights():
                self._highlight_jobs.discard(job)
                self._refresh_highlights()

            job = self.after(int(self.highlights.duration * 1000) + 20, expire_highlights)
            self._highlight_jobs.add(job)
        signature = (self.state.pending_actor, self.state.pending_type,
                     tuple(self.state.pending_cards), self.state.pending_nopes, self.state.response_leader)
        if self.state.phase == "RESPONSE":
            if signature != self._last_response:
                self._log("response", self.state)
            self._last_response = signature
        else:
            self._last_response = None
        self._render()

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
        discards = [card_name(self.language, parse_card(token)[1]) for token in self.state.discard[-12:]]
        self.discard_info.configure(text=self.tr("discard", cards=", ".join(discards) or self.tr("empty")))
        for child in self.players_box.winfo_children():
            child.destroy()
        for player in sorted(self.state.players.values(), key=lambda item: item.player_id):
            label = self.tr("player_row", marker="▶ " if player.player_id == self.state.active_player else "",
                            name=player.name, you=self.tr("you") if player.player_id == self.player_id else "",
                            alive=self.tr("alive" if player.alive else "out"), count=player.hand_size)
            if player.exploded != "-":
                label += self.tr("exploded")
            ttk.Label(self.players_box, text=label, wraplength=320).pack(anchor="w", pady=4)
        for child in self.hand_inner.winfo_children():
            child.destroy()
        self._card_widgets.clear()
        for row, (card_id, kind) in enumerate(sorted_hand(self.state.hand)):
            selected = tk.BooleanVar(value=card_id in self.selected_cards)
            button = ttk.Checkbutton(self.hand_inner, text=card_name(self.language, kind), variable=selected,
                                      command=lambda cid=card_id, var=selected: self._toggle_card(cid, var))
            button.grid(row=row, column=0, sticky="w", pady=3, padx=4)
            self._card_widgets[card_id] = button
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
            "pass": can_respond, "nope": can_respond and selected == ["NOPE"],
            "start": connected and self.player_id == 0 and self.state.phase == "LOBBY" and len(self.state.players) >= 2,
            "resolve": connected and alive and ((self.state.phase == "FAVOR" and self.player_id == self.state.pending_target)
                            or (self.state.phase == "KITTEN" and self.player_id == self.state.active_player)),
            "decline": connected and alive and self.state.phase == "KITTEN" and self.player_id == self.state.active_player,
        }
        for key, allowed in enabled.items():
            getattr(self, key + "_button").configure(state="normal" if allowed else "disabled")

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
        target = self._target() if kind == "FAVOR" else -1
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
        selected = [card_id for card_id, kind in self.state.hand
                    if card_id in self.selected_cards and kind == "NOPE"]
        if len(self.selected_cards) != 1 or len(selected) != 1:
            messagebox.showinfo(self.tr("selection"), self.tr("select_nope"), parent=self)
            return
        self.connection.send("NOPE", selected[0])

    def _resolve_prompt(self) -> None:
        if self.state.phase == "FAVOR" and self.player_id == self.state.pending_target:
            card_id = self._choose("favor_title", "favor_prompt",
                                   [(cid, card_name(self.language, kind)) for cid, kind in sorted_hand(self.state.hand)])
            if card_id is not None:
                self.connection.send("FAVOR", card_id)
        elif self.state.phase == "KITTEN" and self.player_id == self.state.active_player:
            position = self._choose_position()
            if position is not None:
                self.connection.send("DEFUSE", position)

    def _choose_position(self) -> int | None:
        dialog = tk.Toplevel(self)
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
        ttk.Button(controls, text=self.tr("cancel"), command=dialog.destroy).pack(side="right")
        dialog.bind("<Return>", accept)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.grab_set()
        spinbox.focus_set()
        self.wait_window(dialog)
        return result[0] if result else None

    def _decline_defuse(self) -> None:
        if messagebox.askyesno(self.tr("decline"), self.tr("decline_confirm"), parent=self):
            self.connection.send("EXPLODE")

    def _target(self) -> int | None:
        options = [(player.player_id, player.name) for player in self.state.players.values()
                   if player.alive and player.player_id != self.player_id]
        return self._choose("target_title", "target_prompt", options)

    def _choose(self, title_key: str, prompt_key: str, options: list[tuple[object, str]]):
        if not options:
            messagebox.showinfo(self.tr("selection"), self.tr("no_target"), parent=self)
            return None
        dialog = tk.Toplevel(self)
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
        ttk.Button(controls, text=self.tr("cancel"), command=dialog.destroy).pack(side="right", padx=4)
        listbox.bind("<Double-1>", accept)
        dialog.bind("<Return>", accept)
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.grab_set()
        listbox.focus_set()
        self.wait_window(dialog)
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
        self.after_cancel(self._poll_id)
        for job in self._highlight_jobs:
            self.after_cancel(job)
        self._highlight_jobs.clear()
        super().destroy()

    def _close(self) -> None:
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

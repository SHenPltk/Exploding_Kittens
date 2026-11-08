"""Tkinter client for the Windows LAN Exploding Kittens server.

The server is authoritative. This client only renders the public snapshot and the
local player's hand, then sends tab-separated commands over TCP.
"""
from __future__ import annotations

import queue
import socket
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from dataclasses import dataclass, field


@dataclass
class PlayerView:
    player_id: int
    name: str
    alive: bool
    hand_size: int
    exploded: str = "-"


@dataclass
class State:
    phase: str = "LOBBY"
    sequence: int = 0
    active_player: int = -1
    turns_remaining: int = 0
    draw_count: int = 0
    discard_count: int = 0
    winner: int = -1
    pending_type: str = ""
    pending_actor: int = -1
    pending_target: int = -1
    pending_nopes: int = 0
    response_leader: int = -1
    players: dict[int, PlayerView] = field(default_factory=dict)
    hand: list[tuple[int, str]] = field(default_factory=list)


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
            self.events.put(("ERROR", [f"connection failed: {error}"]))
            self.running = False

    def close(self) -> None:
        self.running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _reader(self) -> None:
        assert self.sock is not None
        buffered = b""
        try:
            while self.running:
                chunk = self.sock.recv(4096)
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
                self.events.put(("ERROR", [f"connection closed: {error}"]))
        finally:
            self.running = False
            self.events.put(("ERROR", ["connection closed"]))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Exploding Kittens LAN")
        self.geometry("1050x700")
        self.minsize(820, 560)
        self.events: queue.Queue[tuple[str, list[str]]] = queue.Queue()
        self.connection = Connection(self.events)
        self.state = State()
        self.player_id = -1
        self.selected_cards: list[int] = []
        self.host = tk.StringVar(value="127.0.0.1")
        self.port = tk.IntVar(value=8765)
        self.name = tk.StringVar(value="Player")
        self.status = tk.StringVar(value="Enter the host address to join a lobby.")
        self._build_login()
        self.after(50, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_login(self) -> None:
        self.login = ttk.Frame(self, padding=24)
        self.login.pack(expand=True)
        ttk.Label(self.login, text="Exploding Kittens LAN", font=("Segoe UI", 22, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 20)
        )
        for row, (label, variable) in enumerate((("Host IP", self.host), ("Port", self.port), ("Name", self.name)), 1):
            ttk.Label(self.login, text=label).grid(row=row, column=0, sticky="e", padx=8, pady=8)
            ttk.Entry(self.login, textvariable=variable, width=28).grid(row=row, column=1, sticky="w", padx=8, pady=8)
        ttk.Button(self.login, text="Join", command=self._join).grid(row=4, column=0, columnspan=2, pady=16)
        ttk.Label(self.login, textvariable=self.status, wraplength=420).grid(row=5, column=0, columnspan=2)

    def _build_game(self) -> None:
        self.game = ttk.Frame(self, padding=10)
        self.game.pack(fill="both", expand=True)
        self.game.columnconfigure(0, weight=1)
        self.game.columnconfigure(1, weight=2)
        self.game.rowconfigure(1, weight=1)
        self.info = ttk.Label(self.game, textvariable=self.status, anchor="w")
        self.info.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.players_box = ttk.LabelFrame(self.game, text="Players", padding=8)
        self.players_box.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        self.players_box.columnconfigure(0, weight=1)
        self.hand_box = ttk.LabelFrame(self.game, text="Your hand", padding=8)
        self.hand_box.grid(row=1, column=1, sticky="nsew")
        self.hand_box.columnconfigure(0, weight=1)
        self.hand_box.rowconfigure(0, weight=1)
        self.hand_canvas = tk.Canvas(self.hand_box, highlightthickness=0)
        self.hand_scroll = ttk.Scrollbar(self.hand_box, orient="vertical", command=self.hand_canvas.yview)
        self.hand_inner = ttk.Frame(self.hand_canvas)
        self.hand_inner.bind("<Configure>", lambda _: self.hand_canvas.configure(scrollregion=self.hand_canvas.bbox("all")))
        self.hand_canvas.create_window((0, 0), window=self.hand_inner, anchor="nw")
        self.hand_canvas.configure(yscrollcommand=self.hand_scroll.set)
        self.hand_canvas.grid(row=0, column=0, sticky="nsew")
        self.hand_scroll.grid(row=0, column=1, sticky="ns")
        controls = ttk.Frame(self.game, padding=(0, 8, 0, 0))
        controls.grid(row=2, column=0, columnspan=2, sticky="ew")
        for column in range(6):
            controls.columnconfigure(column, weight=1)
        self.draw_button = ttk.Button(controls, text="Draw", command=lambda: self.connection.send("DRAW"))
        self.draw_button.grid(row=0, column=0, padx=4, sticky="ew")
        self.play_button = ttk.Button(controls, text="Play selected", command=self._play_selected)
        self.play_button.grid(row=0, column=1, padx=4, sticky="ew")
        self.combo_button = ttk.Button(controls, text="Two/three-card combo", command=self._combo)
        self.combo_button.grid(row=0, column=2, padx=4, sticky="ew")
        self.pass_button = ttk.Button(controls, text="Pass response", command=lambda: self.connection.send("PASS"))
        self.pass_button.grid(row=0, column=3, padx=4, sticky="ew")
        self.nope_button = ttk.Button(controls, text="Play Nope", command=self._play_nope)
        self.nope_button.grid(row=0, column=4, padx=4, sticky="ew")
        self.action_button = ttk.Button(controls, text="Resolve prompt", command=self._resolve_prompt)
        self.action_button.grid(row=0, column=5, padx=4, sticky="ew")
        self.start_button = ttk.Button(controls, text="Start game", command=lambda: self.connection.send("START"))
        self.start_button.grid(row=1, column=0, columnspan=3, padx=4, pady=(6, 0), sticky="ew")
        self.explode_button = ttk.Button(controls, text="Decline Defuse", command=lambda: self.connection.send("EXPLODE"))
        self.explode_button.grid(row=1, column=3, columnspan=3, padx=4, pady=(6, 0), sticky="ew")
        self.log = tk.Text(self.game, height=7, state="disabled", wrap="word")
        self.log.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _join(self) -> None:
        try:
            self.connection.connect(self.host.get().strip(), int(self.port.get()), self.name.get().strip() or "Player")
            self.status.set("Connecting to lobby...")
        except (OSError, ValueError) as error:
            messagebox.showerror("Connection failed", str(error))

    def _drain_events(self) -> None:
        while True:
            try:
                kind, parts = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle(kind, parts)
        self.after(50, self._drain_events)

    def _handle(self, kind: str, parts: list[str]) -> None:
        if kind == "WELCOME" and parts:
            self.player_id = int(parts[0])
            if not hasattr(self, "game"):
                self.login.destroy()
                self._build_game()
                self._log(f"Joined as player {self.player_id}.")
        elif kind == "ROSTER":
            self.state.players.clear()
            for index in range(0, len(parts) - 1, 2):
                player_id = int(parts[index])
                name = parts[index + 1]
                self.state.players[player_id] = PlayerView(player_id, name, True, 8)
            self._log("Lobby roster updated.")
            self._render()
        elif kind == "STATE":
            self._state(parts)
        elif kind == "PLAYER":
            player_id, name, alive, hand_size = parts[:4]
            exploded = parts[4] if len(parts) > 4 else "-"
            self.state.players[int(player_id)] = PlayerView(
                int(player_id), name, alive == "1", int(hand_size), exploded
            )
        elif kind == "HAND":
            self.state.hand = [(int(token.split(":", 1)[0]), token.split(":", 1)[1]) for token in parts if ":" in token]
            self._render()
        elif kind == "ENDSTATE":
            self._render()
        elif kind == "EVENT":
            if len(parts) >= 2:
                self._log(f"{parts[0]}: {parts[1]}")
                self.status.set(parts[1])
        elif kind == "PRIVATE" and parts and parts[0] == "SEE_FUTURE":
            cards = ", ".join(parts[1:])
            messagebox.showinfo("See the Future", cards or "The draw pile is empty.")
        elif kind == "ERROR":
            self.status.set(" ".join(parts))
            self._log("Error: " + " ".join(parts))

    def _state(self, parts: list[str]) -> None:
        if len(parts) < 12:
            return
        self.state.sequence = int(parts[0])
        self.state.phase = parts[1]
        self.state.active_player = int(parts[2])
        self.state.turns_remaining = int(parts[3])
        self.state.draw_count = int(parts[4])
        self.state.discard_count = int(parts[5])
        self.state.winner = int(parts[6])
        self.state.pending_type = parts[7]
        self.state.pending_actor = int(parts[8])
        self.state.pending_target = int(parts[9])
        self.state.pending_nopes = int(parts[10])
        self.state.response_leader = int(parts[11])
        self.state.players.clear()

    def _render(self) -> None:
        if not hasattr(self, "game"):
            return
        self.status.set(
            f"Phase: {self.state.phase} | Active: {self.state.active_player} | "
            f"Turns: {self.state.turns_remaining} | Draw pile: {self.state.draw_count} | "
            f"Discard: {self.state.discard_count}"
        )
        for child in self.players_box.winfo_children():
            child.destroy()
        for player in sorted(self.state.players.values(), key=lambda item: item.player_id):
            marker = "* " if player.player_id == self.state.active_player else "  "
            alive = "alive" if player.alive else "out"
            exploded = f", exploded: {player.exploded}" if player.exploded != "-" else ""
            ttk.Label(self.players_box, text=f"{marker}{player.player_id}: {player.name} ({alive}, {player.hand_size} cards{exploded})").pack(anchor="w", pady=3)
        for child in self.hand_inner.winfo_children():
            child.destroy()
        self.selected_cards.clear()
        for row, (card_id, card_type) in enumerate(self.state.hand):
            selected = tk.BooleanVar(value=False)
            ttk.Checkbutton(self.hand_inner, text=f"{card_type}  [#{card_id}]", variable=selected,
                            command=lambda cid=card_id, var=selected: self._toggle_card(cid, var)).grid(
                                row=row, column=0, sticky="w", pady=2
                            )
        is_turn = self.player_id == self.state.active_player and self.state.phase == "TURN"
        is_response = self.state.phase == "RESPONSE"
        self.start_button.configure(state="normal" if self.player_id == 0 and self.state.phase == "LOBBY" and len(self.state.players) >= 2 else "disabled")
        self.draw_button.configure(state="normal" if is_turn else "disabled")
        self.play_button.configure(state="normal" if is_turn else "disabled")
        self.combo_button.configure(state="normal" if is_turn else "disabled")
        can_respond = is_response and self.player_id != self.state.response_leader
        self.pass_button.configure(state="normal" if can_respond else "disabled")
        self.nope_button.configure(state="normal" if can_respond else "disabled")
        prompt_for_me = (
            (self.state.phase == "FAVOR" and self.player_id == self.state.pending_target)
            or (self.state.phase == "KITTEN" and self.player_id == self.state.active_player)
        )
        self.action_button.configure(state="normal" if prompt_for_me else "disabled")
        self.explode_button.configure(state="normal" if self.state.phase == "KITTEN" and self.player_id == self.state.active_player else "disabled")

    def _toggle_card(self, card_id: int, variable: tk.BooleanVar) -> None:
        if variable.get():
            if card_id not in self.selected_cards:
                self.selected_cards.append(card_id)
        else:
            if card_id in self.selected_cards:
                self.selected_cards.remove(card_id)

    def _play_selected(self) -> None:
        if len(self.selected_cards) != 1:
            messagebox.showinfo("Select one card", "Select exactly one card.")
            return
        card_id = self.selected_cards[0]
        card_type = next(kind for candidate, kind in self.state.hand if candidate == card_id)
        target = self._target("Target player") if card_type == "FAVOR" else -1
        if target is not None:
            self.connection.send("PLAY", card_id, target)

    def _combo(self) -> None:
        if len(self.selected_cards) not in (2, 3):
            messagebox.showinfo("Select cards", "Select exactly two or three cards.")
            return
        target = self._target("Target player")
        if target is None:
            return
        fields: list[object] = ["COMBO", *self.selected_cards]
        if len(self.selected_cards) == 2:
            fields.append(target)
        else:
            requested = simpledialog.askstring("Three of a kind", "Requested card name (for example DEFUSE):", parent=self)
            if not requested:
                return
            fields.extend((target, requested.strip().upper()))
        self.connection.send(str(fields[0]), *fields[1:])

    def _play_nope(self) -> None:
        selected = [card_id for card_id in self.selected_cards
                    if any(candidate == card_id and kind == "NOPE" for candidate, kind in self.state.hand)]
        if len(selected) != 1:
            messagebox.showinfo("Nope", "Select exactly one Nope card in your hand.")
            return
        self.connection.send("NOPE", selected[0])

    def _resolve_prompt(self) -> None:
        if self.state.phase == "FAVOR":
            target = self.state.pending_target
            if self.player_id != target:
                messagebox.showinfo("Favor", f"Player {target} must choose a card.")
                return
            if not self.state.hand:
                return
            card_id = self._card_dialog("Choose a card to give")
            if card_id is not None:
                self.connection.send("FAVOR", card_id)
        elif self.state.phase == "KITTEN":
            position = simpledialog.askinteger("Defuse", "Insert the kitten this many cards from the top (0 = top):", parent=self, minvalue=0)
            if position is not None:
                self.connection.send("DEFUSE", position)

    def _target(self, title: str) -> int | None:
        value = simpledialog.askstring(title, "Enter player id:", parent=self)
        if value is None or not value.strip():
            return None
        try:
            return int(value.strip())
        except ValueError:
            messagebox.showerror("Invalid target", "Player id must be a number.")
            return None

    def _card_dialog(self, title: str) -> int | None:
        values = [f"{card_type} #{card_id}" for card_id, card_type in self.state.hand]
        value = simpledialog.askstring(title, "Enter card id: " + ", ".join(values), parent=self)
        if value is None:
            return None
        try:
            return int(value.strip())
        except ValueError:
            messagebox.showerror("Invalid card", "Card id must be a number.")
            return None

    def _log(self, text: str) -> None:
        if not hasattr(self, "log"):
            return
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _close(self) -> None:
        self.connection.close()
        self.destroy()


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

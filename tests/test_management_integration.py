"""Real-server coverage of management, host controls, and the client download.

Run after building ek_server, for example::

    PATH=/d/mingw64/bin:$PATH python -m unittest discover -s tests \
        -p test_management_integration.py -v

Every server uses ephemeral loopback ports and a temporary credential record.  No
fixtures or TestCase subclasses are imported from the other integration suite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import http.client
import io
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import quote
import zipfile
import zlib


ROOT = Path(__file__).resolve().parents[1]
SERVER = next((path for path in (ROOT / "build/ek_server.exe",
                               ROOT / "build/Release/ek_server.exe",
                               ROOT / "build/ek_server") if path.is_file()), None)
CARD_TYPES = (
    "DEFUSE", "ATTACK", "SKIP", "SEE_FUTURE", "SHUFFLE", "FAVOR", "NOPE",
    "TACOCAT", "CATTERMELON", "HAIRY_POTATO_CAT", "RAINBOW_RALPHING_CAT", "BEARD_CAT",
)
TIMEOUT = 10


def cards(tokens):
    return [(int(token.split(":", 1)[0]), token.split(":", 1)[1]) for token in tokens]


@dataclass
class Snapshot:
    sequence: int
    phase: str
    active: int
    turns: int
    draw_count: int
    discard_count: int
    winner: int
    pending_type: str
    actor: int
    target: int
    nopes: int
    leader: int
    round_number: int = 0
    host: int = -1
    connected: int = 0
    online: list[int] = field(default_factory=list)
    mode: tuple[str, ...] = ()
    options: tuple[str, ...] = ()
    bombs: int = 0
    pending: list[str] = field(default_factory=list)
    requested: str | None = None
    prompt: int | None = None
    players: dict[int, tuple[str, bool, int, str]] = field(default_factory=dict)
    hand: list[tuple[int, str]] = field(default_factory=list)
    discard: list[str] = field(default_factory=list)
    messages: list[tuple[str, list[str]]] = field(default_factory=list)


class Peer:
    """Small synchronous wire peer, with no background threads or model imports."""

    def __init__(self, port, *hello):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT)
        self.buffer = b""
        self.player_id = -1
        self.history = []
        self.session = None
        self.options = None
        self.closed = False
        if hello:
            self.send(*hello)

    def send(self, *parts):
        self.sock.sendall(("\t".join(map(str, parts)) + "\n").encode("utf-8"))

    def line(self, timeout=TIMEOUT):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            self.sock.settimeout(max(0.001, deadline - time.monotonic()))
            chunk = self.sock.recv(65536)
            if not chunk:
                if self.buffer:
                    raise AssertionError("Server closed with a partial protocol line")
                return None
            self.buffer += chunk
            if len(self.buffer) > 2 * 1024 * 1024:
                raise AssertionError("Server exceeded the protocol message bound")
        raw, self.buffer = self.buffer.split(b"\n", 1)
        kind, *parts = raw.decode("utf-8").rstrip("\r").split("\t")
        message = (kind, parts)
        self.history.append(message)
        if kind == "WELCOME":
            self.player_id = int(parts[0])
        elif kind == "SESSION":
            self.session = tuple(map(int, parts))
        elif kind == "OPTIONS":
            self.options = tuple(parts)
        return message

    def wait_for(self, kind, predicate=lambda parts: True):
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            message = self.line(deadline - time.monotonic())
            if message is None:
                raise AssertionError(f"Server disconnected while waiting for {kind}")
            current, parts = message
            if current == kind and predicate(parts):
                return parts
        raise AssertionError(f"Timed out waiting for {kind}")

    def sync(self):
        self.send("PING")
        self.wait_for("PONG")

    def snapshot(self):
        messages = []
        state = None
        while True:
            message = self.line()
            if message is None:
                raise AssertionError("Server disconnected before ENDSTATE")
            kind, parts = message
            messages.append(message)
            if kind == "STATE":
                if state is not None or len(parts) != 12:
                    raise AssertionError("Malformed or overlapping STATE frame")
                state = Snapshot(int(parts[0]), parts[1], *map(int, parts[2:7]),
                                 parts[7], *map(int, parts[8:12]))
            elif state is not None:
                if kind == "ROUND":
                    state.round_number = int(parts[0])
                elif kind == "SESSION":
                    state.host, state.connected = map(int, parts)
                elif kind == "ONLINE":
                    state.online = list(map(int, parts))
                elif kind == "MODE":
                    state.mode = tuple(parts)
                elif kind == "OPTIONS":
                    state.options = tuple(parts)
                elif kind == "DECK":
                    state.bombs = int(parts[0])
                elif kind == "PENDING":
                    state.pending = parts[1:]
                elif kind == "REQUESTED":
                    state.requested = parts[0]
                elif kind == "PROMPT":
                    state.prompt = int(parts[0])
                elif kind == "PLAYER":
                    pid, name, alive, size, exploded = parts
                    state.players[int(pid)] = (name, alive == "1", int(size), exploded)
                elif kind == "HAND":
                    state.hand = cards(parts)
                elif kind == "DISCARD":
                    state.discard = list(parts)
                elif kind == "ENDSTATE":
                    state.messages = messages
                    return state

    def admin_state(self):
        self.send("ADMIN_STATE")
        begin = self.wait_for("ADMIN_BEGIN")
        state = {"phase": begin[0], "host": int(begin[1]), "round": int(begin[2]),
                 "players": {}, "hands": {}, "deck": None}
        while True:
            message = self.line()
            if message is None:
                raise AssertionError("Server disconnected before ADMIN_END")
            kind, parts = message
            if kind == "ADMIN_PLAYER":
                pid, name, alive, online = parts
                state["players"][int(pid)] = (name, alive == "1", online == "1")
            elif kind == "ADMIN_HAND":
                state["hands"][int(parts[0])] = cards(parts[1:])
            elif kind == "ADMIN_DECK":
                if state["deck"] is not None:
                    raise AssertionError("Duplicate ADMIN_DECK")
                state["deck"] = cards(parts)
            elif kind == "ADMIN_END":
                if state["deck"] is None:
                    raise AssertionError("Missing explicit ADMIN_DECK")
                return state
            else:
                raise AssertionError(f"Unexpected admin snapshot message: {kind}")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class RunningServer:
    def __init__(self, testcase, credentials=None, http=True, port=None):
        if credentials is None:
            directory = tempfile.TemporaryDirectory(prefix="ek-management-管理-")
            testcase.addCleanup(directory.cleanup)
            credentials = Path(directory.name) / "management.auth"
        self.credentials = Path(credentials)
        with socket.socket() as game_probe, socket.socket() as http_probe:
            game_probe.bind(("127.0.0.1", port or 0))
            http_probe.bind(("127.0.0.1", 0))
            self.port = game_probe.getsockname()[1]
            self.http_port = http_probe.getsockname()[1] if http else 0
        # Preserve the parent's PATH, including the MinGW runtime DLL directory.
        self.process = subprocess.Popen(
            [str(SERVER), str(self.port), str(self.http_port), str(self.credentials)],
            cwd=ROOT, env=os.environ.copy(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        testcase.addCleanup(self.stop)
        deadline = time.monotonic() + TIMEOUT
        while True:
            if self.process.poll() is not None:
                detail = self.process.stderr.read().decode("utf-8", errors="replace")
                testcase.fail(f"Server failed to start (exit {self.process.returncode}): {detail}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    testcase.fail("Server did not bind its game port before the deadline")
                time.sleep(0.02)

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stderr is not None and not self.process.stderr.closed:
            self.process.stderr.close()

    def request(self, method, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=TIMEOUT)
        try:
            connection.request(method, path)
            result = connection.getresponse()
            return result.status, dict(result.getheaders()), result.read()
        finally:
            connection.close()


@unittest.skipUnless(SERVER is not None, "Build ek_server before running management integration tests")
class ManagementIntegrationTests(unittest.TestCase):
    def peer(self, server, *hello):
        peer = Peer(server.port, *hello)
        self.addCleanup(peer.close)
        return peer

    def admin(self, server, password="Admin123"):
        admin = self.peer(server, "ADMIN", password)
        self.assertEqual(admin.line(), ("ADMIN_OK", ["AUTHENTICATED"]))
        return admin

    def lobby(self, count, **server_options):
        server = RunningServer(self, **server_options)
        peers = []
        for index in range(count):
            peer = self.peer(server, "JOIN", f"玩家-{index}")
            peer.wait_for("WELCOME")
            peers.append(peer)
        for peer in peers:
            peer.sync()
            self.assertEqual(peer.session, (0, count))
        return server, peers

    def start(self, peers, host=None):
        (host or peers[0]).send("START")
        return self.snapshots(peers)

    def snapshots(self, peers):
        states = [peer.snapshot() for peer in peers]
        common = [(state.sequence, state.phase, state.active, state.turns, state.draw_count,
                   state.discard_count, state.winner, state.pending_type, state.actor,
                   state.target, state.nopes, state.leader, state.round_number, state.host,
                   state.connected, state.online, state.mode, state.options, state.bombs,
                   state.pending, state.requested, state.prompt, state.players, state.discard)
                  for state in states]
        self.assertTrue(all(value == common[0] for value in common))
        for state in states:
            self.assertFalse(any(kind.startswith("ADMIN_") for kind, _ in state.messages))
            self.assertEqual(bool(state.discard), state.discard_count > 0)
            if state.phase != "RESPONSE":
                self.assertEqual(state.pending, [])
                self.assertIsNone(state.requested)
        return states

    def command(self, peers, actor, *parts):
        next(peer for peer in peers if peer.player_id == actor).send(*parts)
        return self.snapshots(peers)

    def admin_command(self, admin, peers, command, *arguments, acknowledgement):
        admin.send(command, *arguments)
        self.assertEqual(admin.line(), ("ADMIN_OK", [acknowledgement]))
        return self.snapshots(peers)

    def provision(self, admin, peers, player, kind):
        states = self.admin_command(admin, peers, "ADMIN_ADD", player, kind,
                                    acknowledgement="CARD_ADDED")
        target = next(state for peer, state in zip(peers, states) if peer.player_id == player)
        self.assertEqual(target.hand[-1][1], kind)
        return states, target.hand[-1][0]

    def settle(self, peers, states):
        state = states[0]
        responders = [pid for pid, player in state.players.items()
                      if player[1] and pid != state.leader]
        for pid in responders:
            if states[0].phase == "RESPONSE":
                states = self.command(peers, pid, "PASS")
        self.assertNotEqual(states[0].phase, "RESPONSE")
        return states

    def assert_event(self, state, code):
        self.assertIn(("EVENT", ["ERROR", code]), state.messages)

    def assert_disconnected(self, peer, kicked=False):
        messages = []
        while True:
            message = peer.line()
            if message is None:
                break
            messages.append(message)
        self.assertEqual(messages, [("EVENT", ["ERROR", "KICKED"])] if kicked else [])
        peer.close()

    def assert_private_ids_unique(self, state):
        collection = state["deck"] + [card for hand in state["hands"].values() for card in hand]
        self.assertEqual(len(collection), len({cid for cid, _ in collection}))
        self.assertEqual(state["players"].keys(), state["hands"].keys())

    def assert_hidden_cards(self, peers, states, full):
        for peer, state in zip(peers, states):
            private = full["deck"] + [card for pid, hand in full["hands"].items()
                                      if pid != peer.player_id for card in hand]
            hidden = {f"{cid}:{kind}" for cid, kind in private}
            observed = {value for _, fields in state.messages for value in fields}
            self.assertEqual(hidden & observed, set(), "Ordinary client received hidden cards")
            self.assertEqual(full["hands"][peer.player_id], state.hand)

    def test_auth_full_state_all_card_types_and_private_snapshots(self):
        server = RunningServer(self)
        admin = self.admin(server)
        empty = admin.admin_state()
        self.assertEqual((empty["phase"], empty["host"], empty["players"], empty["deck"]),
                         ("LOBBY", -1, {}, []))
        for hello, expected in ((["ADMIN", "wrong-pass"], "AUTH_FAILED"),
                                (["ADMIN"], "AUTH_FAILED"),
                                (["ADMIN", ""], "AUTH_FAILED"),
                                (["ADMIN_STATE"], "FIRST_COMMAND_MUST_BE_JOIN")):
            with self.subTest(hello=hello[0], fields=len(hello)):
                rejected = self.peer(server, *hello)
                kind = "ADMIN_ERROR" if hello[0] == "ADMIN" else "EVENT"
                fields = [expected] if kind == "ADMIN_ERROR" else ["ERROR", expected]
                self.assertEqual(rejected.line(), (kind, fields))
                self.assert_disconnected(rejected)
                self.assertFalse(any(message[0] in {"ADMIN_BEGIN", "ADMIN_HAND", "ADMIN_DECK", "STATE"}
                                     for message in rejected.history))
        peers = []
        for index in range(3):
            peer = self.peer(server, "JOIN", f"ordinary-{index}")
            peer.wait_for("WELCOME")
            peers.append(peer)
        for peer in peers:
            peer.sync()
            self.assertEqual(peer.session, (0, 3))
        self.assertEqual(len(admin.admin_state()["players"]), 3)
        states = self.start(peers)
        initial = admin.admin_state()
        self.assertEqual((initial["phase"], initial["round"]), ("TURN", 1))
        self.assertEqual(len(initial["deck"]), states[0].draw_count)
        for peer, state in zip(peers, states):
            self.assertEqual(initial["hands"][peer.player_id], state.hand)
            self.assertEqual(len(state.hand), 8)
        self.assert_private_ids_unique(initial)
        self.assert_hidden_cards(peers, states, initial)

        before = [len(peer.history) for peer in peers]
        admin.admin_state()
        for peer, offset in zip(peers, before):
            peer.sync()
            self.assertEqual(peer.history[offset:], [("PONG", [])])
        states = self.command(peers, 0, "ADMIN_ADD", 1, "NOPE")
        self.assert_event(states[0], "UNKNOWN_COMMAND")
        self.assertEqual(admin.admin_state(), initial)

        deck = initial["deck"]
        seen = {cid for cid, _ in deck}
        seen.update(cid for hand in initial["hands"].values() for cid, _ in hand)
        for kind in CARD_TYPES:
            with self.subTest(card=kind):
                old_hands = [list(state.hand) for state in states]
                states, created = self.provision(admin, peers, 2, kind)
                self.assertNotIn(created, seen)
                seen.add(created)
                self.assertEqual(states[0].hand, old_hands[0])
                self.assertEqual(states[1].hand, old_hands[1])
                self.assertEqual(states[2].hand, old_hands[2] + [(created, kind)])
                for state in states[:2]:
                    self.assertFalse(any(f"{created}:{kind}" in fields for _, fields in state.messages))
                full = admin.admin_state()
                self.assertEqual(full["deck"], deck)
                self.assertEqual(full["hands"][2], states[2].hand)
                self.assert_private_ids_unique(full)
                self.assert_hidden_cards(peers, states, full)

        states, future = self.provision(admin, peers, 0, "SEE_FUTURE")
        states = self.command(peers, 0, "PLAY", future)
        states = self.settle(peers, states)
        expected = ["SEE_FUTURE", *(f"{cid}:{kind}" for cid, kind in deck[:3])]
        self.assertIn(("PRIVATE", expected), states[0].messages)
        for state in states[1:]:
            self.assertFalse(any(kind == "PRIVATE" for kind, _ in state.messages))
        self.assertEqual(admin.admin_state()["deck"], deck)
        states = self.command(peers, 0, "DRAW")
        top_id, top_kind = deck[0]
        if top_kind == "EXPLODING_KITTEN":
            self.assertEqual(states[0].players[0][3], f"{top_id}:{top_kind}")
            states = self.command(peers, 0, "DEFUSE", len(deck) - 1)
            deck = deck[1:] + [deck[0]]
        else:
            self.assertIn(deck[0], states[0].hand)
            deck = deck[1:]
        self.assertEqual(admin.admin_state()["deck"], deck)

        before = states[0]
        states = self.admin_command(admin, peers, "ADMIN_ADD", 2, "EXPLODING_KITTEN",
                                    acknowledgement="CARD_ADDED")
        self.assertEqual((states[0].phase, states[0].prompt, states[0].active, states[0].turns),
                         ("KITTEN", 2, before.active, before.turns))
        injected = int(states[0].players[2][3].split(":")[0])
        self.assertNotIn(injected, seen)
        self.assertEqual(admin.admin_state()["deck"], deck)
        states = self.command(peers, 2, "DEFUSE", 0)
        self.assertEqual((states[0].active, states[0].turns), (before.active, before.turns))
        self.assertEqual(admin.admin_state()["deck"], deck)
        for command, arguments, error in (("ADMIN_ADD", [2, "UNKNOWN"], "INVALID_CARD_TYPE"),
                                           ("ADMIN_ADD", [99, "DEFUSE"], "INVALID_PLAYER"),
                                           ("ADMIN_HOST", [99], "INVALID_PLAYER")):
            admin.send(command, *arguments)
            self.assertEqual(admin.line(), ("ADMIN_ERROR", [error]))

    def test_admin_host_survives_lobby_reindex_game_and_restart(self):
        server, peers = self.lobby(4)
        admin = self.admin(server)
        admin.send("ADMIN_HOST", 2)
        self.assertEqual(admin.line(), ("ADMIN_OK", ["HOST_CHANGED"]))
        for peer in peers:
            peer.sync()
            self.assertEqual(peer.session, (2, 4))
        peers[0].send("SET_FREE_ATTACK", 1)
        self.assertEqual(peers[0].wait_for("EVENT"), ["ERROR", "ONLY_HOST_CAN_SET_OPTIONS"])
        peers[1].close()
        peers = [peers[0], peers[2], peers[3]]
        for peer in peers:
            peer.wait_for("SESSION", lambda fields: fields == ["1", "3"])
            peer.sync()
        self.assertEqual([peer.player_id for peer in peers], [0, 1, 2])
        self.assertEqual(admin.admin_state()["host"], 1)
        peers[1].send("SET_FREE_ATTACK", 1)
        peers[1].sync()
        for peer in peers:
            peer.sync()
            self.assertEqual(peer.options, ("0", "1"))
        states = self.start(peers, peers[1])
        self.assertEqual((states[0].host, states[0].mode), (1, ("0", "1")))
        before = [(state.active, state.turns, state.hand) for state in states]
        states = self.admin_command(admin, peers, "ADMIN_HOST", 2,
                                    acknowledgement="HOST_CHANGED")
        self.assertEqual([(state.active, state.turns, state.hand) for state in states], before)
        self.assertTrue(all(state.host == 2 for state in states))
        peers[1].send("RESTART")
        self.assertEqual(peers[1].wait_for("EVENT"), ["ERROR", "ONLY_HOST_CAN_START"])
        peers[2].send("KICK", 0)
        self.assert_disconnected(peers[0], kicked=True)
        peers = peers[1:]
        states = self.snapshots(peers)
        remaining_defuses = sum(kind == "DEFUSE" for _, kind in states[0].hand)
        for _ in range(remaining_defuses):
            states = self.admin_command(admin, peers, "ADMIN_ADD", 1, "EXPLODING_KITTEN",
                                        acknowledgement="CARD_ADDED")
            states = self.command(peers, 1, "DEFUSE", 0)
        states = self.admin_command(admin, peers, "ADMIN_ADD", 1, "EXPLODING_KITTEN",
                                    acknowledgement="CARD_ADDED")
        self.assertEqual((states[0].phase, states[0].host, states[0].connected), ("GAME_OVER", 2, 2))
        states = self.command(peers, 2, "RESTART")
        self.assertEqual([peer.player_id for peer in peers], [0, 1])
        self.assertEqual((states[0].phase, states[0].host, states[0].round_number,
                          states[0].active, states[0].connected), ("TURN", 1, 2, 0, 2))
        self.assertEqual(states[0].mode, ("0", "1"))
        self.assertEqual([state.players[1][0] for state in states], ["玩家-3", "玩家-3"])
        self.assertEqual(admin.admin_state()["host"], 1)

    def test_admin_bomb_one_stacked_turn_defuse_and_death_preserve_deck(self):
        server, peers = self.lobby(3)
        admin = self.admin(server)
        peers[0].send("SET_FREE_ATTACK", 1)
        for peer in peers:
            peer.sync()
        states = self.start(peers)
        for actor, target in ((0, 2), (2, 1)):
            states, attack = self.provision(admin, peers, actor, "ATTACK")
            states = self.settle(peers, self.command(peers, actor, "PLAY", attack, target))
        self.assertEqual((states[0].active, states[0].turns), (1, 4))
        deck = admin.admin_state()["deck"]
        discard = states[0].discard_count
        defuses = sum(kind == "DEFUSE" for _, kind in states[0].hand)
        states = self.admin_command(admin, peers, "ADMIN_BOMB", 0,
                                    acknowledgement="KITTEN_INJECTED")
        self.assertEqual((states[0].phase, states[0].prompt, states[0].active, states[0].turns),
                         ("KITTEN", 0, 1, 3))
        self.assertEqual(admin.admin_state()["deck"], deck)
        frozen = admin.admin_state()
        admin.send("ADMIN_BOMB", 2)
        self.assertEqual(admin.line(), ("ADMIN_ERROR", ["ADMIN_ACTION_REQUIRES_TURN"]))
        self.assertEqual(admin.admin_state(), frozen)
        states = self.command(peers, 0, "EXPLODE")
        self.assert_event(states[0], "DEFUSE_REQUIRED")
        states = self.command(peers, 0, "DEFUSE", 0)
        self.assertEqual((states[0].phase, states[0].active, states[0].turns), ("TURN", 1, 3))
        self.assertEqual(sum(kind == "DEFUSE" for _, kind in states[0].hand), defuses - 1)
        self.assertEqual(states[0].discard_count, discard + 2)
        self.assertEqual(states[0].players[0][3], "-")
        states = self.admin_command(admin, peers, "ADMIN_BOMB", 1,
                                    acknowledgement="KITTEN_INJECTED")
        self.assertEqual((states[0].prompt, states[0].active, states[0].turns), (1, 1, 2))
        states = self.command(peers, 1, "DEFUSE", len(deck))
        self.assertEqual((states[0].active, states[0].turns), (1, 2))
        for _ in range(sum(kind == "DEFUSE" for _, kind in states[0].hand)):
            states = self.admin_command(admin, peers, "ADMIN_ADD", 0, "EXPLODING_KITTEN",
                                        acknowledgement="CARD_ADDED")
            states = self.command(peers, 0, "DEFUSE", 0)
        states = self.admin_command(admin, peers, "ADMIN_BOMB", 0,
                                    acknowledgement="KITTEN_INJECTED")
        self.assertEqual((states[0].phase, states[0].active, states[0].turns), ("TURN", 1, 1))
        self.assertFalse(states[0].players[0][1])
        self.assertEqual(states[0].hand, [])
        self.assertTrue(states[0].players[0][3].endswith(":EXPLODING_KITTEN"))
        self.assertEqual(admin.admin_state()["deck"], deck)
        for command, arguments in (("ADMIN_BOMB", [0]), ("ADMIN_ADD", [0, "DEFUSE"])):
            admin.send(command, *arguments)
            self.assertEqual(admin.line(), ("ADMIN_ERROR", ["INVALID_PLAYER"]))

    def test_credentials_rotation_pbkdf2_persistence_and_malformed_fail_closed(self):
        server = RunningServer(self)
        admin = self.admin(server)
        other = self.admin(server)

        def verify_record(password):
            record = server.credentials.read_bytes()
            self.assertNotIn(password.encode("utf-8"), record)
            self.assertNotIn(b"Admin123", record)
            version, iterations, salt, verifier = record.decode("ascii").split()
            self.assertEqual((version, iterations), ("EKADMIN1", "600000"))
            self.assertEqual((len(bytes.fromhex(salt)), len(bytes.fromhex(verifier))), (16, 32))
            calculated = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                           bytes.fromhex(salt), int(iterations))
            self.assertEqual(calculated.hex(), verifier)
            return salt

        old_salt = verify_record("Admin123")
        for password in ("short", "a" * 129, "invalid\0password"):
            admin.send("ADMIN_PASSWORD", password)
            self.assertEqual(admin.line(), ("ADMIN_ERROR", ["INVALID_PASSWORD"]))
        self.assertEqual(verify_record("Admin123"), old_salt)
        password = "更换Password-654321"
        admin.send("ADMIN_PASSWORD", password)
        self.assertEqual(admin.line(), ("ADMIN_OK", ["PASSWORD_CHANGED"]))
        self.assertEqual(other.line(), ("ADMIN_ERROR", ["AUTH_EXPIRED"]))
        self.assert_disconnected(other)
        self.assertNotEqual(verify_record(password), old_salt)
        self.assertEqual(admin.admin_state()["phase"], "LOBBY")
        rejected = self.peer(server, "ADMIN", "Admin123")
        self.assertEqual(rejected.line(), ("ADMIN_ERROR", ["AUTH_FAILED"]))
        self.assert_disconnected(rejected)
        self.admin(server, password)
        record = server.credentials.read_bytes()
        server.stop()
        restarted = RunningServer(self, credentials=server.credentials, http=False)
        self.assertEqual(self.admin(restarted, password).admin_state()["players"], {})
        rejected = self.peer(restarted, "ADMIN", "Admin123")
        self.assertEqual(rejected.line(), ("ADMIN_ERROR", ["AUTH_FAILED"]))
        self.assert_disconnected(rejected)
        self.assertEqual(server.credentials.read_bytes(), record)
        self.assertEqual(list(server.credentials.parent.glob("*.tmp.*")), [])

        malformed = [b"", b"plaintext Admin123\n", record.replace(b"600000", b"1", 1),
                     record.rstrip() + b" extra\n", b"EKADMIN1 600000 " + b"z" * 32 + b" " + b"0" * 64,
                     b"x" * 1025]
        for content in malformed:
            with self.subTest(malformed_bytes=len(content)):
                directory = tempfile.TemporaryDirectory(prefix="ek-invalid-auth-")
                self.addCleanup(directory.cleanup)
                path = Path(directory.name) / "invalid.auth"
                path.write_bytes(content)
                with socket.socket() as game_probe, socket.socket() as http_probe:
                    game_probe.bind(("127.0.0.1", 0))
                    http_probe.bind(("127.0.0.1", 0))
                    game_port, http_port = game_probe.getsockname()[1], http_probe.getsockname()[1]
                process = subprocess.Popen([str(SERVER), str(game_port), str(http_port), str(path)],
                                           cwd=ROOT, env=os.environ.copy(), stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
                try:
                    stdout, stderr = process.communicate(timeout=TIMEOUT)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(b"ADMIN_AUTH_FILE_INVALID", stderr)
                self.assertNotIn(b"listening", stdout)
                self.assertEqual(path.read_bytes(), content)
                for port in (game_port, http_port):
                    with self.assertRaises(OSError):
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            pass

    def test_host_kick_permissions_disconnect_lobby_and_phase_recovery(self):
        server, peers = self.lobby(3)
        for actor, target, error in ((1, 2, "ONLY_HOST_CAN_KICK"), (1, 1, "ONLY_HOST_CAN_KICK"),
                                     (0, 0, "CANNOT_KICK_SELF"), (0, 99, "PLAYER_OFFLINE")):
            peers[actor].send("KICK", target)
            self.assertEqual(peers[actor].wait_for("EVENT"), ["ERROR", error])
        peers[0].send("KICK", 1)
        self.assert_disconnected(peers[1], kicked=True)
        peers = [peers[0], peers[2]]
        for peer in peers:
            peer.wait_for("SESSION", lambda fields: fields == ["0", "2"])
            peer.sync()
        self.assertEqual([peer.player_id for peer in peers], [0, 1])
        states = self.start(peers)
        peers[0].send("KICK", 1)
        self.assert_disconnected(peers[1], kicked=True)
        states = self.snapshots(peers[:1])
        self.assertEqual((states[0].phase, states[0].winner, states[0].connected), ("GAME_OVER", 0, 1))
        peers[0].send("RESTART")
        self.assertEqual(peers[0].wait_for("EVENT"), ["ERROR", "PLAYER_COUNT_MUST_BE_2_TO_5"])

        for scenario in ("TURN", "RESPONSE_ACTOR", "RESPONSE_TARGET", "RESPONSE_LEADER",
                         "FAVOR_TARGET", "KITTEN_TARGET"):
            with self.subTest(phase=scenario):
                server, peers = self.lobby(4)
                admin = self.admin(server)
                states = self.start(peers)
                if scenario in ("TURN", "RESPONSE_ACTOR"):
                    states, skip = self.provision(admin, peers, 0, "SKIP")
                    states = self.settle(peers, self.command(peers, 0, "PLAY", skip))
                    self.assertEqual(states[0].active, 1)
                if scenario == "RESPONSE_ACTOR":
                    states, skip = self.provision(admin, peers, 1, "SKIP")
                    states = self.command(peers, 1, "PLAY", skip)
                elif scenario in ("RESPONSE_TARGET", "FAVOR_TARGET"):
                    states, favor = self.provision(admin, peers, 0, "FAVOR")
                    states = self.command(peers, 0, "PLAY", favor, 1)
                    if scenario == "FAVOR_TARGET":
                        states = self.settle(peers, states)
                        self.assertEqual(states[0].phase, "FAVOR")
                elif scenario == "RESPONSE_LEADER":
                    states, skip = self.provision(admin, peers, 0, "SKIP")
                    states, nope = self.provision(admin, peers, 1, "NOPE")
                    states = self.command(peers, 0, "PLAY", skip)
                    states = self.command(peers, 1, "NOPE", nope)
                    self.assertEqual(states[0].leader, 1)
                elif scenario == "KITTEN_TARGET":
                    states = self.admin_command(admin, peers, "ADMIN_ADD", 1, "EXPLODING_KITTEN",
                                                acknowledgement="CARD_ADDED")
                    self.assertEqual((states[0].phase, states[0].prompt), ("KITTEN", 1))
                peers[0].send("KICK", 1)
                self.assert_disconnected(peers[1], kicked=True)
                peers = [peers[0], peers[2], peers[3]]
                states = self.snapshots(peers)
                self.assertEqual((states[0].connected, states[0].online), (3, [0, 2, 3]))
                self.assertFalse(states[0].players[1][1])
                private = admin.admin_state()
                self.assertEqual(private["players"][1][1:], (False, False))
                if scenario == "RESPONSE_LEADER":
                    self.assertEqual((states[0].phase, states[0].leader, states[0].nopes),
                                     ("RESPONSE", 0, 1))
                    states = self.settle(peers, states)
                self.assertEqual(states[0].phase, "TURN")
                self.assertEqual(states[0].active, 2 if scenario in ("TURN", "RESPONSE_ACTOR") else 0)
                self.assertIsNone(states[0].prompt)
                self.assertEqual(states[0].pending, [])
                # Close this subcase now, not just at final unittest cleanup.
                for peer in peers:
                    peer.close()
                admin.close()
                server.stop()

    def test_combo_requested_through_nope_chain_and_free_attack_stacked_targets(self):
        server, peers = self.lobby(4)
        admin = self.admin(server)
        peers[1].send("SET_FREE_ATTACK", 1)
        self.assertEqual(peers[1].wait_for("EVENT"), ["ERROR", "ONLY_HOST_CAN_SET_OPTIONS"])
        peers[0].send("SET_FREE_ATTACK", 2)
        self.assertEqual(peers[0].wait_for("EVENT"), ["ERROR", "INVALID_COMMAND_ARGUMENT"])
        peers[0].send("SET_FREE_ATTACK", 1)
        for peer in peers:
            peer.sync()
            self.assertEqual(peer.options, ("0", "1"))
        states = self.start(peers)
        self.assertTrue(all(state.mode == ("0", "1") for state in states))
        peers[0].send("SET_FREE_ATTACK", 0)
        self.assertEqual(peers[0].wait_for("EVENT"), ["ERROR", "OPTIONS_LOCKED_DURING_GAME"])
        triple = []
        for _ in range(3):
            states, card = self.provision(admin, peers, 0, "TACOCAT")
            triple.append(card)
        states, _ = self.provision(admin, peers, 1, "SHUFFLE")
        states, first_nope = self.provision(admin, peers, 2, "NOPE")
        states, second_nope = self.provision(admin, peers, 3, "NOPE")
        target_count = sum(kind == "SHUFFLE" for _, kind in states[1].hand)
        actor_count = sum(kind == "SHUFFLE" for _, kind in states[0].hand)
        states = self.command(peers, 0, "COMBO", *triple, 1, "SHUFFLE")
        for nopes, leader in ((0, 0), (1, 2), (2, 3)):
            if nopes:
                states = self.command(peers, leader, "NOPE", first_nope if nopes == 1 else second_nope)
            for state in states:
                self.assertEqual((state.phase, state.pending_type, state.requested, state.nopes, state.leader),
                                 ("RESPONSE", "COMBO3", "SHUFFLE", nopes, leader))
                self.assertEqual(state.pending, ["TACOCAT"] * 3)
                self.assertIn(("REQUESTED", ["SHUFFLE"]), state.messages)
        states = self.settle(peers, states)
        self.assertEqual(sum(kind == "SHUFFLE" for _, kind in states[1].hand), target_count - 1)
        self.assertEqual(sum(kind == "SHUFFLE" for _, kind in states[0].hand), actor_count + 1)
        self.assertTrue(all(state.requested is None for state in states))

        states, attack = self.provision(admin, peers, 0, "ATTACK")
        for target in (None, 0, 99):
            old_hand = list(states[0].hand)
            args = ("PLAY", attack) if target is None else ("PLAY", attack, target)
            states = self.command(peers, 0, *args)
            self.assert_event(states[0], "ATTACK_REQUIRES_OTHER_PLAYER")
            self.assertEqual(states[0].hand, old_hand)
        states = self.settle(peers, self.command(peers, 0, "PLAY", attack, 2))
        self.assertEqual((states[0].active, states[0].turns), (2, 2))
        states, attack = self.provision(admin, peers, 2, "ATTACK")
        states = self.settle(peers, self.command(peers, 2, "PLAY", attack, 1))
        self.assertEqual((states[0].active, states[0].turns), (1, 4))
        states, skip = self.provision(admin, peers, 1, "SKIP")
        states = self.settle(peers, self.command(peers, 1, "PLAY", skip))
        self.assertEqual((states[0].active, states[0].turns), (1, 3))
        states, attack = self.provision(admin, peers, 1, "ATTACK")
        states = self.settle(peers, self.command(peers, 1, "PLAY", attack, 0))
        self.assertEqual((states[0].active, states[0].turns), (0, 5))

        standard_server, standard = self.lobby(3)
        standard_admin = self.admin(standard_server)
        self.start(standard)
        states, attack = self.provision(standard_admin, standard, 0, "ATTACK")
        states = self.command(standard, 0, "PLAY", attack, 2)
        self.assert_event(states[0], "UNEXPECTED_TARGET")
        states = self.settle(standard, self.command(standard, 0, "PLAY", attack))
        self.assertEqual((states[0].active, states[0].turns, states[0].mode), (1, 2, ("0", "0")))

    def test_http_exact_zip_crc_head_python_and_no_file_serving_or_disabled_listener(self):
        server = RunningServer(self)
        status, headers, landing = server.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b'/client.zip', landing)
        self.assertIn(str(server.port).encode("ascii"), landing)
        self.assertIn(b"Python 3.10+", landing)
        self.assertIn(b"Tkinter", landing)
        status, head_headers, body = server.request("HEAD", "/")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(int(head_headers["Content-Length"]), len(landing))
        status, headers, archive = server.request("GET", "/client.zip")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertEqual(int(headers["Content-Length"]), len(archive))
        self.assertIn('filename="client.zip"', headers["Content-Disposition"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, head_headers, body = server.request("HEAD", "/client.zip")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(int(head_headers["Content-Length"]), len(archive))
        allowlist = {"main.py", "i18n.py", "presentation.py", "start_client.bat", "README.txt"}
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            self.assertEqual(set(zipped.namelist()), allowlist)
            self.assertEqual(len(zipped.namelist()), len(allowlist))
            self.assertIsNone(zipped.testzip())
            for entry in zipped.infolist():
                data = zipped.read(entry)
                self.assertEqual(zlib.crc32(data) & 0xffffffff, entry.CRC)
                self.assertEqual(entry.file_size, len(data))
                self.assertFalse(entry.is_dir())
                self.assertNotIn(b"EKADMIN1", data)
                if entry.filename.endswith(".py"):
                    self.assertEqual(data, (ROOT / "client" / entry.filename).read_bytes())
            self.assertIn(b"py -3 main.py", zipped.read("start_client.bat"))
            self.assertIn(b'cd /d "%~dp0"', zipped.read("start_client.bat"))
            self.assertIn(b"Python 3.10+", zipped.read("README.txt"))
            self.assertIn(str(server.port).encode("ascii"), zipped.read("README.txt"))
            with tempfile.TemporaryDirectory(prefix="ek-download-") as extracted:
                zipped.extractall(extracted)
                result = subprocess.run([sys.executable, "-m", "py_compile", "main.py", "i18n.py", "presentation.py"],
                                        cwd=extracted, capture_output=True, timeout=TIMEOUT)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                result = subprocess.run([sys.executable, "-c", "import main, i18n, presentation"],
                                        cwd=extracted, capture_output=True, timeout=TIMEOUT)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        secret = server.credentials.parent / "private_note.txt"
        secret.write_text("arbitrary-local-file-secret", encoding="utf-8")
        forbidden = ("/admin", "/admin/main.py", "/server_admin.auth", "/management.auth",
                     "/src/server.cpp", "/main.py", "/client/main.py", "/private_note.txt",
                     "/../src/server.cpp", "/%2e%2e/src/server.cpp", "/..%5cmanagement.auth",
                     "/client.zip/../management.auth", "/client.zip?file=management.auth",
                     "//client.zip", "/C:/Windows/win.ini", "/" + quote(server.credentials.as_posix()))
        for path in forbidden:
            with self.subTest(path=path):
                status, _, body = server.request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"EKADMIN1", body)
                self.assertNotIn(b"arbitrary-local-file-secret", body)
        status, headers, body = server.request("HEAD", "/management.auth")
        self.assertEqual((status, body, int(headers["Content-Length"])), (404, b"", len(b"Not found.\n")))
        status, headers, _ = server.request("POST", "/client.zip")
        self.assertEqual((status, headers["Allow"]), (405, "GET, HEAD"))

        # Occupying game_port + 1 proves an explicit zero does not fall back to
        # the default HTTP listener; the game and admin sockets remain usable.
        blocker = socket.socket()
        self.addCleanup(blocker.close)
        for _ in range(30):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
                if port == 65535:
                    continue
                try:
                    blocker.bind(("127.0.0.1", port + 1))
                except OSError:
                    continue
                break
        else:
            self.fail("Could not reserve adjacent ports for the disabled-HTTP test")
        disabled = RunningServer(self, http=False, port=port)
        self.assertEqual(disabled.http_port, 0)
        self.assertEqual(self.admin(disabled).admin_state()["phase"], "LOBBY")
        player = self.peer(disabled, "JOIN", "http-disabled-player")
        player.wait_for("WELCOME")
        player.sync()
        self.assertEqual(player.session, (0, 1))
        with self.assertRaises(OSError):
            with socket.create_connection(("127.0.0.1", port + 1), timeout=0.2):
                pass

    def test_independent_admin_connection_real_auth_snapshot_mutation_and_rotation(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from admin.main import Connection, SnapshotParser

        server, peers = self.lobby(2)
        states = self.start(peers)
        events = queue.Queue()
        connection = Connection(events)
        workers = []

        def close_connection():
            connection.close()
            for worker in workers:
                worker.join(5)
                self.assertFalse(worker.is_alive(), "Admin Connection worker did not stop")

        self.addCleanup(close_connection)

        def connect(password):
            connection.connect("127.0.0.1", server.port, password)
            if connection._thread is not None:
                workers.append(connection._thread)

        def event(kind, fields):
            self.assertEqual(events.get(timeout=TIMEOUT), (kind, fields))

        def snapshot():
            parser = SnapshotParser()
            connection.send("ADMIN_STATE")
            while True:
                kind, fields = events.get(timeout=TIMEOUT)
                self.assertTrue(kind.startswith("ADMIN_"), kind)
                state = parser.feed(kind, fields)
                if state is not None:
                    return state

        connect("wrong-password")
        event("ADMIN_ERROR", ["AUTH_FAILED"])
        workers[-1].join(5)
        self.assertFalse(connection.authenticated)
        connect("Admin123")
        event("ADMIN_OK", ["AUTHENTICATED"])
        self.assertTrue(connection.authenticated)
        full = snapshot()
        self.assertEqual(full.hands, {peer.player_id: state.hand for peer, state in zip(peers, states)})
        self.assertEqual((full.phase, full.host_id, full.round_number), ("TURN", 0, 1))
        connection.send("ADMIN_HOST", 1)
        event("ADMIN_OK", ["HOST_CHANGED"])
        states = self.snapshots(peers)
        self.assertEqual(snapshot().host_id, 1)
        self.assertTrue(all(state.host == 1 for state in states))
        connection.send("ADMIN_ADD", 0, "DEFUSE")
        event("ADMIN_OK", ["CARD_ADDED"])
        states = self.snapshots(peers)
        added = snapshot()
        self.assertEqual(added.deck, full.deck)
        self.assertEqual(added.hands[0][:-1], full.hands[0])
        self.assertEqual(added.hands[0][-1][1], "DEFUSE")
        connection.send("ADMIN_BOMB", 0)
        event("ADMIN_OK", ["KITTEN_INJECTED"])
        states = self.snapshots(peers)
        self.assertEqual((states[0].phase, states[0].prompt, states[0].active), ("KITTEN", 0, 1))
        self.command(peers, 0, "DEFUSE", 0)
        self.assertEqual(snapshot().deck, full.deck)
        password = "Independent-Admin-579"
        connection.send("ADMIN_PASSWORD", password)
        event("ADMIN_OK", ["PASSWORD_CHANGED"])
        self.assertTrue(connection.authenticated)
        self.assertEqual(snapshot().host_id, 1)
        rejected = self.peer(server, "ADMIN", "Admin123")
        self.assertEqual(rejected.line(), ("ADMIN_ERROR", ["AUTH_FAILED"]))
        self.assert_disconnected(rejected)
        self.assertEqual(self.admin(server, password).admin_state()["host"], 1)


if __name__ == "__main__":
    unittest.main()

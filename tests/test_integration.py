"""Loopback coverage of the real C++ server and the Python snapshot model."""
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from presentation import State, parse_card

ROOT = Path(__file__).resolve().parents[1]
SERVER = next((path for path in (ROOT / "build/ek_server.exe",
                               ROOT / "build/Release/ek_server.exe",
                               ROOT / "build/ek_server") if path.exists()), None)


class Peer:
    def __init__(self, port, name):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.stream = self.sock.makefile("r", encoding="utf-8")
        self.send("JOIN", name)

    def send(self, *parts):
        self.sock.sendall(("\t".join(map(str, parts)) + "\n").encode("utf-8"))

    def snapshot(self):
        state = None
        while True:
            line = self.stream.readline()
            if not line:
                raise AssertionError("Server disconnected during snapshot")
            kind, *parts = line.rstrip("\r\n").split("\t")
            if kind == "STATE":
                state = State.from_fields(parts)
            elif state is not None:
                if kind == "DECK":
                    state.bombs_remaining = int(parts[0])
                elif kind == "PENDING":
                    state.pending_actor = int(parts[0])
                    state.pending_cards = parts[1:]
                elif kind == "HAND":
                    state.hand = [parse_card(token) for token in parts]
                elif kind == "ENDSTATE":
                    return state

    def close(self):
        self.stream.close()
        self.sock.close()


@unittest.skipUnless(SERVER is not None, "Build ek_server before running TCP tests")
class ServerIntegrationTests(unittest.TestCase):
    def start_game(self, count):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        process = subprocess.Popen([str(SERVER), str(port)], cwd=ROOT,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def stop():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stderr.close()

        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while True:
            if process.poll() is not None:
                self.fail("Server did not start: " + process.stderr.read().decode(errors="replace"))
            try:
                peer = Peer(port, "玩家甲")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
        peers = [peer]
        self.addCleanup(peer.close)
        for index in range(1, count):
            peer = Peer(port, "玩家" + str(index))
            self.addCleanup(peer.close)
            peers.append(peer)
        # Each JOIN is registered before its WELCOME is sent. Wait for every peer
        # before issuing START so test timing cannot race lobby membership.
        for peer in peers:
            while True:
                line = peer.stream.readline()
                self.assertTrue(line, "Server closed before WELCOME")
                if line.startswith("WELCOME\t"):
                    break
        peers[0].send("START")
        return peers, [peer.snapshot() for peer in peers]

    def command(self, peers, player, *parts):
        peers[player].send(*parts)
        states = [peer.snapshot() for peer in peers]
        public = {(s.sequence, s.phase, s.draw_count, s.bombs_remaining,
                   s.pending_actor, tuple(s.pending_cards), s.pending_nopes) for s in states}
        self.assertEqual(len(public), 1)
        for state in states:
            if state.phase != "RESPONSE":
                self.assertEqual(state.pending_cards, [])
            if state.draw_count:
                self.assertGreaterEqual(state.bomb_probability, 0)
                self.assertLessEqual(state.bomb_probability, 1)
        return states

    def test_bomb_counts_and_response_details_in_complete_games(self):
        for count, deck_size in ((2, 35), (3, 29), (4, 23), (5, 16)):
            with self.subTest(players=count):
                peers, states = self.start_game(count)
                self.assertEqual(states[0].draw_count, deck_size)
                self.assertEqual(states[0].bombs_remaining, count - 1)
                for state in states:
                    self.assertEqual(len(state.hand), 8)
                ids = [card_id for state in states for card_id, _ in state.hand]
                self.assertEqual(len(ids), len(set(ids)))
                played = False
                defused = False
                for _ in range(250):
                    state = states[0]
                    if state.phase == "GAME_OVER":
                        break
                    actor = state.active_player
                    if state.phase == "KITTEN":
                        old_bombs = state.bombs_remaining
                        if not defused:
                            states = self.command(peers, actor, "DEFUSE", state.draw_count)
                            self.assertEqual(states[0].bombs_remaining, old_bombs + 1)
                            defused = True
                        else:
                            states = self.command(peers, actor, "EXPLODE")
                            self.assertEqual(states[0].bombs_remaining, old_bombs)
                        continue
                    self.assertEqual(state.phase, "TURN")
                    if not played:
                        hand = states[actor].hand
                        card = next(((cid, kind) for cid, kind in hand
                                     if kind in {"ATTACK", "SHUFFLE", "SKIP", "SEE_FUTURE", "FAVOR"}), None)
                        target = (actor + 1) % count
                        if card:
                            command = ["PLAY", card[0]]
                            if card[1] == "FAVOR":
                                command.append(target)
                            played_types = [card[1]]
                        else:
                            pair_type = next(kind for _, kind in hand
                                             if sum(other == kind for _, other in hand) >= 2)
                            pair_ids = [cid for cid, kind in hand if kind == pair_type][:2]
                            command = ["COMBO", *pair_ids, target]
                            played_types = [pair_type, pair_type]
                        if command:
                            states = self.command(peers, actor, *command)
                            self.assertEqual(states[0].pending_actor, actor)
                            self.assertEqual(states[0].pending_cards, played_types)
                            responder = next((pid for pid, other in enumerate(states) if pid != actor
                                              and any(kind == "NOPE" for _, kind in other.hand)), None)
                            if responder is not None:
                                nope = next(cid for cid, kind in states[responder].hand if kind == "NOPE")
                                states = self.command(peers, responder, "NOPE", nope)
                                self.assertEqual(states[0].pending_actor, actor)
                                self.assertEqual(states[0].pending_cards, played_types)
                                self.assertEqual(states[0].response_leader, responder)
                            for pid in range(count):
                                if states[0].phase == "RESPONSE" and pid != states[0].response_leader:
                                    states = self.command(peers, pid, "PASS")
                            if states[0].phase == "FAVOR":
                                recipient = states[0].pending_target
                                states = self.command(peers, recipient, "FAVOR", states[recipient].hand[0][0])
                            self.assertEqual(states[0].phase, "TURN")
                            played = True
                            continue
                    before = states[0]
                    states = self.command(peers, actor, "DRAW")
                    self.assertEqual(states[0].draw_count, before.draw_count - 1)
                    self.assertIn(before.bombs_remaining - states[0].bombs_remaining, (0, 1))
                self.assertEqual(states[0].phase, "GAME_OVER")
                self.assertTrue(played)
                self.assertTrue(defused)
                for peer in peers:
                    peer.close()
                # Keep the server processes isolated; their cleanups run after
                # the test, and every process binds its own ephemeral port.


if __name__ == "__main__":
    unittest.main()

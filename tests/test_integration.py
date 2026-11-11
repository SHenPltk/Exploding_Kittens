"""Loopback coverage of the real C++ server and Python snapshot model."""
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from presentation import PlayerView, State, parse_card
from main import App

ROOT = Path(__file__).resolve().parents[1]
SERVER = next((path for path in (ROOT / "build/ek_server.exe",
                               ROOT / "build/Release/ek_server.exe",
                               ROOT / "build/ek_server") if path.exists()), None)


class Peer:
    def __init__(self, port, name):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.stream = self.sock.makefile("r", encoding="utf-8")
        self.player_id = -1
        self.events = []
        self.next_fast_mode = False
        self.send("JOIN", name)

    def send(self, *parts):
        self.sock.sendall(("\t".join(map(str, parts)) + "\n").encode("utf-8"))

    def line(self):
        line = self.stream.readline()
        if not line:
            raise AssertionError("Server disconnected before expected message")
        kind, *parts = line.rstrip("\r\n").split("\t")
        if kind == "WELCOME":
            self.player_id = int(parts[0])
        elif kind == "EVENT":
            self.events.append(parts)
        elif kind == "OPTIONS":
            self.next_fast_mode = parts[0] == "1"
        return kind, parts

    def wait_for(self, kind):
        while True:
            current, parts = self.line()
            if current == kind:
                return parts

    def snapshot(self):
        state = None
        self.events = []
        while True:
            kind, parts = self.line()
            if kind == "STATE":
                state = State.from_fields(parts)
            elif state is not None:
                if kind == "ROUND":
                    state.round_number = int(parts[0])
                elif kind == "SESSION":
                    state.host_player, state.connected_count = map(int, parts)
                elif kind == "MODE":
                    state.fast_mode = parts[0] == "1"
                elif kind == "OPTIONS":
                    state.next_fast_mode = parts[0] == "1"
                elif kind == "DECK":
                    state.bombs_remaining = int(parts[0])
                elif kind == "PENDING":
                    state.pending_actor = int(parts[0])
                    state.pending_cards = parts[1:]
                elif kind == "PLAYER":
                    pid, name, alive, size, exploded = parts
                    state.players[int(pid)] = PlayerView(int(pid), name, alive == "1", int(size), exploded)
                elif kind == "DISCARD":
                    state.discard.append(parts[0])
                elif kind == "HAND":
                    state.hand = [parse_card(token) for token in parts]
                elif kind == "ENDSTATE":
                    return state

    def close(self):
        self.stream.close()
        self.sock.close()


@unittest.skipUnless(SERVER is not None, "Build ek_server before running TCP tests")
class ServerIntegrationTests(unittest.TestCase):
    def start_lobby(self, count):
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
        for peer in peers:
            peer.wait_for("WELCOME")
        return port, peers

    def start_game(self, count):
        _, peers = self.start_lobby(count)
        peers[0].send("START")
        return peers, [peer.snapshot() for peer in peers]

    def command(self, peers, player, *parts):
        peers[player].send(*parts)
        states = [peer.snapshot() for peer in peers]
        public = {(s.sequence, s.phase, s.draw_count, s.bombs_remaining,
                   s.pending_actor, tuple(s.pending_cards), s.pending_nopes,
                   s.round_number, s.host_player, s.connected_count) for s in states}
        self.assertEqual(len(public), 1)
        for state in states:
            if state.phase != "RESPONSE":
                self.assertEqual(state.pending_cards, [])
            self.assertLessEqual(len(state.discard), 1)
            self.assertEqual(bool(state.discard), state.discard_count > 0)
            if state.draw_count:
                self.assertGreaterEqual(state.bomb_probability, 0)
                self.assertLessEqual(state.bomb_probability, 1)
        return states

    def finish_game(self, peers, states):
        for _ in range(200):
            state = states[0]
            if state.phase == "GAME_OVER":
                return states
            actor = state.active_player
            command = ("DEFUSE", 0) if state.phase == "KITTEN" else ("DRAW",)
            states = self.command(peers, actor, *command)
        self.fail("Game failed to finish within card-count bound")

    def assert_fresh_game(self, states, count, round_number, fast=False):
        expected = {2: 24, 3: 20} if fast else {2: 35, 3: 29, 4: 23, 5: 16}
        for state in states:
            self.assertEqual(state.phase, "TURN")
            self.assertEqual(state.fast_mode, fast)
            self.assertEqual(state.round_number, round_number)
            self.assertEqual(state.active_player, 0)
            self.assertEqual(state.turns_remaining, 1)
            self.assertEqual(state.winner, -1)
            self.assertEqual(state.discard, [])
            self.assertEqual(state.pending_cards, [])
            self.assertEqual(state.draw_count, expected[count])
            self.assertEqual(state.bombs_remaining, count - 1)
            self.assertEqual(len(state.hand), 8)
            self.assertEqual(state.connected_count, count)
            self.assertTrue(all(player.alive and player.exploded == "-" for player in state.players.values()))
        ids = [cid for state in states for cid, _ in state.hand]
        self.assertEqual(len(ids), len(set(ids)))

    def test_bomb_counts_response_and_mandatory_defuse_in_complete_games(self):
        for count in range(2, 6):
            with self.subTest(players=count):
                peers, states = self.start_game(count)
                self.assert_fresh_game(states, count, 1)
                actor = states[0].active_player
                hand = states[actor].hand
                target = (actor + 1) % count
                card = next(((cid, kind) for cid, kind in hand if kind in
                            {"ATTACK", "SHUFFLE", "SKIP", "SEE_FUTURE", "FAVOR"}), None)
                if card:
                    command = ["PLAY", card[0]] + ([target] if card[1] == "FAVOR" else [])
                    played_types = [card[1]]
                else:
                    pair_type = next(kind for _, kind in hand if sum(other == kind for _, other in hand) >= 2)
                    pair_ids = [cid for cid, kind in hand if kind == pair_type][:2]
                    command = ["COMBO", *pair_ids, target]
                    played_types = [pair_type, pair_type]
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
                while states[0].phase == "TURN":
                    states = self.command(peers, states[0].active_player, "DRAW")
                if states[0].phase == "GAME_OVER":
                    # A valid Defuse pair can exhaust the actor's protection;
                    # begin a fresh round so the mandatory-Defuse assertion
                    # always starts from a guaranteed protected deal.
                    states = self.command(peers, 0, "RESTART")
                    while states[0].phase == "TURN":
                        states = self.command(peers, states[0].active_player, "DRAW")
                self.assertEqual(states[0].phase, "KITTEN")
                actor = states[0].active_player
                before = states
                states = self.command(peers, actor, "EXPLODE")
                self.assertIn(["ERROR", "DEFUSE_REQUIRED"], peers[actor].events)
                for old, new in zip(before, states):
                    self.assertEqual((old.phase, old.hand, old.draw_count, old.bombs_remaining, old.discard),
                                     (new.phase, new.hand, new.draw_count, new.bombs_remaining, new.discard))
                old_bombs = states[0].bombs_remaining
                states = self.command(peers, actor, "DEFUSE", 0)
                self.assertEqual(states[0].bombs_remaining, old_bombs + 1)
                states = self.finish_game(peers, states)
                self.assertEqual(states[0].phase, "GAME_OVER")
                next_round = states[0].round_number + 1
                states = self.command(peers, 0, "RESTART")
                self.assert_fresh_game(states, count, next_round)

    def test_fast_mode_host_setting_and_standard_fast_rematches(self):
        for count in (2, 3):
            with self.subTest(players=count):
                _, peers = self.start_lobby(count)
                peers[1].send("SET_FAST", 1)
                self.assertEqual(peers[1].wait_for("EVENT"), ["ERROR", "ONLY_HOST_CAN_SET_OPTIONS"])
                peers[0].send("SET_FAST", "invalid")
                self.assertEqual(peers[0].wait_for("EVENT"), ["ERROR", "INVALID_COMMAND_ARGUMENT"])
                peers[0].send("SET_FAST", 1)
                for peer in peers:
                    while peer.wait_for("OPTIONS") != ["1"]:
                        pass
                peers[0].send("START")
                states = [peer.snapshot() for peer in peers]
                self.assert_fresh_game(states, count, 1, fast=True)
                self.assertTrue(all(state.next_fast_mode for state in states))
                peers[0].send("SET_FAST", 0)
                self.assertEqual(peers[0].wait_for("EVENT"), ["ERROR", "OPTIONS_LOCKED_DURING_GAME"])
                states = self.finish_game(peers, states)
                states = self.command(peers, 0, "SET_FAST", 0)
                self.assertTrue(states[0].fast_mode)
                self.assertFalse(states[0].next_fast_mode)
                states = self.command(peers, 0, "RESTART")
                self.assert_fresh_game(states, count, 2)
                states = self.finish_game(peers, states)
                states = self.command(peers, 0, "SET_FAST", 1)
                self.assertFalse(states[0].fast_mode)
                self.assertTrue(states[0].next_fast_mode)
                states = self.command(peers, 0, "RESTART")
                self.assert_fresh_game(states, count, 3, fast=True)

    def test_fourth_join_disables_fast_option_and_large_games_reject_it(self):
        port, peers = self.start_lobby(3)
        peers[0].send("SET_FAST", 1)
        for peer in peers:
            while peer.wait_for("OPTIONS") != ["1"]:
                pass
        fourth = Peer(port, "玩家四")
        self.addCleanup(fourth.close)
        fourth.wait_for("WELCOME")
        peers.append(fourth)
        for peer in peers:
            self.assertEqual(peer.wait_for("OPTIONS"), ["0"])
        peers[0].send("SET_FAST", 1)
        self.assertEqual(peers[0].wait_for("EVENT"), ["ERROR", "FAST_MODE_REQUIRES_2_OR_3_PLAYERS"])
        peers[0].send("START")
        states = [peer.snapshot() for peer in peers]
        self.assert_fresh_game(states, 4, 1)
        states = self.finish_game(peers, states)
        peers[0].send("SET_FAST", 1)
        self.assertEqual(peers[0].wait_for("EVENT"), ["ERROR", "FAST_MODE_REQUIRES_2_OR_3_PLAYERS"])
        states = self.command(peers, 0, "RESTART")
        self.assert_fresh_game(states, 4, 2)
        _, five = self.start_lobby(5)
        five[0].send("SET_FAST", 1)
        self.assertEqual(five[0].wait_for("EVENT"), ["ERROR", "FAST_MODE_REQUIRES_2_OR_3_PLAYERS"])

    def test_duplicate_names_rejected_and_name_released_after_disconnect(self):
        port, peers = self.start_lobby(1)
        duplicate = Peer(port, "  玩家甲  ")
        self.addCleanup(duplicate.close)
        self.assertEqual(duplicate.wait_for("EVENT"), ["ERROR", "NAME_ALREADY_TAKEN"])
        self.assertEqual(duplicate.player_id, -1)
        self.assertEqual(duplicate.stream.readline(), "")
        unique = Peer(port, "玩家乙")
        self.addCleanup(unique.close)
        unique.wait_for("WELCOME")
        self.assertEqual(unique.player_id, 1)
        peers[0].send("PING")
        peers[0].wait_for("PONG")
        unique.close()
        while True:
            roster = peers[0].wait_for("ROSTER")
            if roster == ["0", "玩家甲"]:
                break
        replacement = Peer(port, "玩家乙")
        self.addCleanup(replacement.close)
        replacement.wait_for("WELCOME")
        self.assertEqual(replacement.player_id, 1)
        peers[0].send("START")
        states = [peer.snapshot() for peer in (peers[0], replacement)]
        self.assert_fresh_game(states, 2, 1)

    def test_tk_client_can_retry_duplicate_name_and_restart_same_connection(self):
        port, peers = self.start_lobby(1)
        host = peers[0]
        app = App()
        self.addCleanup(app.destroy)
        app.withdraw()
        app.host.set("127.0.0.1")
        app.port.set(port)
        app.name.set("玩家甲")

        def pump(predicate):
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                app.update()
                if predicate():
                    return
                time.sleep(0.01)
            self.fail("Tk client did not reach expected network state")

        with patch("main.messagebox.showerror") as error:
            app._join()
            pump(lambda: app._join_error and not app.connection.running)
            self.assertFalse(hasattr(app, "game"))
            self.assertIn("该昵称已被使用", app.status.get())
            error.assert_called_once()
        app.name.set("玩家乙")
        app._join()
        pump(lambda: app.player_id == 1 and len(app.state.players) == 2)
        host.close()
        pump(lambda: app.player_id == 0 and len(app.state.players) == 1)
        other = Peer(port, "玩家丙")
        self.addCleanup(other.close)
        other.wait_for("WELCOME")
        pump(lambda: len(app.state.players) == 2)
        self.assertFalse(app.fast_button.instate(["disabled"]))
        app.fast_button.invoke()
        pump(lambda: app.state.next_fast_mode)
        app.start_button.invoke()
        state = other.snapshot()
        pump(lambda: app.state.round_number == 1)
        self.assertTrue(app.state.fast_mode)
        self.assertTrue(state.fast_mode)
        self.assertEqual(app.state.draw_count, 24)
        self.assertEqual(state.draw_count, 24)
        self.assertIn("快速模式", app.mode_info.cget("text"))

        # Exercise a real private hand loss before finishing the game. Even a
        # hand containing no single action card has a matching pair.
        hand = app.state.hand
        action = next(((cid, kind) for cid, kind in hand if kind in
                       {"ATTACK", "SHUFFLE", "SKIP", "SEE_FUTURE", "FAVOR"}), None)
        if action:
            removed_types = [action[1]]
            command = ["PLAY", action[0]] + ([other.player_id] if action[1] == "FAVOR" else [])
        else:
            pair_type = next(kind for _, kind in hand if sum(other_kind == kind for _, other_kind in hand) >= 2)
            pair = [cid for cid, kind in hand if kind == pair_type][:2]
            removed_types = [pair_type, pair_type]
            command = ["COMBO", *pair, other.player_id]
        with patch("main.messagebox.showinfo"):
            app.connection.send(*command)
            state = other.snapshot()
            pump(lambda: app.state.sequence == state.sequence)
            self.assertIn("失去", app.loss_info.cget("text"))
            self.assertIn(("lost", tuple(removed_types)), app._log_entries)
            other.send("PASS")
            state = other.snapshot()
            pump(lambda: app.state.sequence == state.sequence)
        if state.phase == "FAVOR":
            other.send("FAVOR", state.hand[0][0])
            state = other.snapshot()
            pump(lambda: app.state.sequence == state.sequence)
        self.assertLessEqual(len(state.discard), 1)
        self.assertIn("弃牌堆顶", app.discard_info.cget("text"))
        for _ in range(120):
            if state.phase == "GAME_OVER":
                break
            actor = state.active_player
            if actor == app.player_id:
                if state.phase == "KITTEN":
                    self.assertFalse(hasattr(app, "decline_button"))
                    with patch.object(app, "_choose_position", return_value=0):
                        app.resolve_button.invoke()
                else:
                    app.draw_button.invoke()
            elif state.phase == "KITTEN":
                other.send("DEFUSE", 0)
            else:
                other.send("DRAW")
            state = other.snapshot()
            pump(lambda: app.state.sequence == state.sequence)
        self.assertEqual(state.phase, "GAME_OVER")
        self.assertFalse(app.restart_button.instate(["disabled"]))
        app.language_name.set("English")
        app._change_language()
        app.fast_button.invoke()
        state = other.snapshot()
        pump(lambda: not app.state.next_fast_mode)
        self.assertTrue(app.state.fast_mode)
        app.selected_cards = [99]
        app.restart_button.invoke()
        state = other.snapshot()
        pump(lambda: app.state.round_number == 2)
        self.assertEqual(app.state.phase, "TURN")
        self.assertFalse(app.state.fast_mode)
        self.assertEqual(app.state.draw_count, 35)
        self.assertEqual(app.loss_info.cget("text"), "")
        self.assertEqual(app.selected_cards, [])
        self.assertEqual(app.highlights.deadlines, {})
        self.assertEqual(app.language, "en")
        self.assertEqual(len(app.state.hand), 8)
        self.assertEqual(state.round_number, 2)
        self.assertEqual(len(state.hand), 8)
        self.assertTrue(app.restart_button.instate(["disabled"]))

    def test_restart_permissions_and_repeated_rounds(self):
        peers, states = self.start_game(2)
        peers[1].send("RESTART")
        self.assertEqual(peers[1].wait_for("EVENT"), ["ERROR", "ONLY_HOST_CAN_START"])
        peers[0].send("RESTART")
        self.assertEqual(peers[0].wait_for("EVENT"), ["ERROR", "GAME_NOT_OVER"])
        for next_round in (2, 3):
            states = self.finish_game(peers, states)
            peers[1].send("RESTART")
            self.assertEqual(peers[1].wait_for("EVENT"), ["ERROR", "ONLY_HOST_CAN_START"])
            states = self.command(peers, 0, "RESTART")
            self.assert_fresh_game(states, 2, next_round)

    def test_restart_excludes_disconnected_players_and_transfers_host(self):
        peers, states = self.start_game(4)
        states = self.finish_game(peers, states)
        peers[0].close()
        peers = peers[1:]
        states = [peer.snapshot() for peer in peers]
        self.assertEqual(states[0].host_player, 1)
        self.assertEqual(states[0].connected_count, 3)
        peers[1].close()
        peers = [peers[0], peers[2]]
        states = [peer.snapshot() for peer in peers]
        self.assertEqual(states[0].connected_count, 2)
        states = self.command(peers, 0, "RESTART")
        self.assert_fresh_game(states, 2, 2)
        self.assertEqual([peer.player_id for peer in peers], [0, 1])
        self.assertEqual([p.name for p in states[0].players.values()], ["玩家1", "玩家3"])
        states = self.finish_game(peers, states)
        peers[1].close()
        state = peers[0].snapshot()
        self.assertEqual(state.connected_count, 1)
        peers[0].send("RESTART")
        self.assertEqual(peers[0].wait_for("EVENT"), ["ERROR", "PLAYER_COUNT_MUST_BE_2_TO_5"])


if __name__ == "__main__":
    unittest.main()

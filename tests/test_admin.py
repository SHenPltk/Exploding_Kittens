"""Tests for the independent admin window; no running game server is required."""
import contextlib
import io
import logging
import queue
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from admin.main import (App, CARD_NAMES, Connection, Player, ProtocolError, Snapshot,
                        SnapshotParser, parse_card, valid_password)


class SnapshotTests(unittest.TestCase):
    def snapshot(self, parser, deck=("9:NOPE", "7:SKIP")):
        self.assertIsNone(parser.feed("ADMIN_BEGIN", ["TURN", "0", "2"]))
        self.assertIsNone(parser.feed("ADMIN_PLAYER", ["0", "玩家甲", "1", "1"]))
        self.assertIsNone(parser.feed("ADMIN_HAND", ["0", "3:DEFUSE", "5:ATTACK"]))
        self.assertIsNone(parser.feed("ADMIN_PLAYER", ["1", "玩家乙", "0", "0"]))
        self.assertIsNone(parser.feed("ADMIN_HAND", ["1"]))
        self.assertIsNone(parser.feed("ADMIN_DECK", list(deck)))
        return parser.feed("ADMIN_END", [])

    def test_snapshot_is_explicit_atomic_and_preserves_top_first_deck(self):
        state = self.snapshot(SnapshotParser())
        self.assertEqual((state.phase, state.host_id, state.round_number), ("TURN", 0, 2))
        self.assertEqual(state.players[0], Player(0, "玩家甲", True, True))
        self.assertEqual(state.players[1], Player(1, "玩家乙", False, False))
        self.assertEqual(state.hands, {0: [(3, "DEFUSE"), (5, "ATTACK")], 1: []})
        self.assertEqual(state.deck, [(9, "NOPE"), (7, "SKIP")])

    def test_empty_lobby_has_explicit_empty_deck(self):
        parser = SnapshotParser()
        parser.feed("ADMIN_BEGIN", ["LOBBY", "-1", "0"])
        parser.feed("ADMIN_DECK", [])
        state = parser.feed("ADMIN_END", [])
        self.assertEqual((state.players, state.hands, state.deck), ({}, {}, []))

    def test_new_snapshot_does_not_modify_previous_snapshot(self):
        parser = SnapshotParser()
        previous = self.snapshot(parser)
        parser.feed("ADMIN_BEGIN", ["TURN", "1", "3"])
        parser.feed("ADMIN_PLAYER", ["1", "新玩家", "1", "1"])
        parser.feed("ADMIN_HAND", ["1"])
        self.assertEqual(previous.players[0].name, "玩家甲")
        self.assertEqual(previous.deck[0], (9, "NOPE"))

    def test_partial_or_duplicate_collections_rejected(self):
        invalid = [
            [("ADMIN_END", [])],
            [("ADMIN_PLAYER", ["0", "甲", "1", "1"]), ("ADMIN_DECK", []), ("ADMIN_END", [])],
            [("ADMIN_HAND", ["0"]), ("ADMIN_DECK", []), ("ADMIN_END", [])],
            [("ADMIN_DECK", []), ("ADMIN_DECK", [])],
            [("ADMIN_BEGIN", ["TURN", "-1", "0"])],
            [("ADMIN_HAND", ["0"]), ("ADMIN_HAND", ["0"])],
            [("ADMIN_PLAYER", ["0", "甲", "1", "1"]), ("ADMIN_PLAYER", ["0", "甲", "1", "1"])],
        ]
        for commands in invalid:
            with self.subTest(commands=commands):
                parser = SnapshotParser()
                parser.feed("ADMIN_BEGIN", ["TURN", "-1", "0"])
                with self.assertRaises(ProtocolError):
                    for command, fields in commands:
                        parser.feed(command, fields)
                with self.assertRaises(ProtocolError):
                    parser.feed("ADMIN_DECK", [])

    def test_invalid_cards_names_flags_host_and_duplicate_ids_rejected(self):
        for value in ("0:SKIP", "a:SKIP", "1:UNKNOWN", "1:SKIP:extra", "-1:ATTACK", "01:NOPE"):
            with self.subTest(value=value), self.assertRaises(ProtocolError):
                parse_card(value)
        for fields in (["0", "甲", "yes", "1"], ["0", "甲\n乙", "1", "1"], ["0", "", "1", "1"]):
            parser = SnapshotParser()
            parser.feed("ADMIN_BEGIN", ["TURN", "0", "1"])
            with self.assertRaises(ProtocolError):
                parser.feed("ADMIN_PLAYER", fields)
        parser = SnapshotParser()
        parser.feed("ADMIN_BEGIN", ["TURN", "4", "1"])
        parser.feed("ADMIN_DECK", [])
        with self.assertRaises(ProtocolError):
            parser.feed("ADMIN_END", [])
        with self.assertRaises(ProtocolError):
            self.snapshot(SnapshotParser(), deck=("3:DEFUSE",))

    def test_password_bounds_match_server_utf8_bytes(self):
        for password in ("Admin123", "a" * 128, "密" * 42):
            self.assertTrue(valid_password(password))
        for password in ("1234567", "a" * 129, "密" * 43, "abc\t1234", "abc\n1234", "abc\r1234", "abc\0" * 3):
            self.assertFalse(valid_password(password))


class ConnectionTests(unittest.TestCase):
    def reader(self, chunks):
        events = queue.Queue()
        connection = Connection(events)
        connection.running = True
        sock = Mock()
        sock.recv.side_effect = [*chunks, b""]
        connection.sock = sock
        connection._reader(sock, connection._generation)
        result = []
        while not events.empty():
            result.append(events.get_nowait())
        return connection, sock, result

    def test_fragmented_utf8_and_multiple_lines_decode_after_complete_line(self):
        payload = ("ADMIN_OK\tAUTHENTICATED\r\nADMIN_BEGIN\tTURN\t0\t1\n"
                   "ADMIN_PLAYER\t0\t玩家甲\t1\t1\nADMIN_HAND\t0\t1:DEFUSE\n"
                   "ADMIN_DECK\t3:ATTACK\t2:NOPE\nADMIN_END\n").encode("utf-8")
        split = payload.index("甲".encode("utf-8")) + 1
        _, sock, events = self.reader([payload[:5], payload[5:split], payload[split:]])
        self.assertEqual(events[0], ("ADMIN_OK", ["AUTHENTICATED"]))
        self.assertIn(("ADMIN_PLAYER", ["0", "玩家甲", "1", "1"]), events)
        self.assertIn(("ADMIN_DECK", ["3:ATTACK", "2:NOPE"]), events)
        self.assertEqual(events[-1], ("ERROR", ["DISCONNECTED"]))
        sock.close.assert_called_once()

    def test_auth_error_closes_and_discards_following_frames(self):
        connection, sock, events = self.reader([
            b"ADMIN_ERROR\tAUTH_FAILED\nADMIN_OK\tAUTHENTICATED\n"])
        self.assertEqual(events, [("ADMIN_ERROR", ["AUTH_FAILED"])])
        self.assertFalse(connection.running)
        self.assertFalse(connection.authenticated)
        sock.close.assert_called_once()

    def test_expired_auth_closes(self):
        connection, _, events = self.reader([
            b"ADMIN_OK\tAUTHENTICATED\nADMIN_ERROR\tAUTH_EXPIRED\n"])
        self.assertEqual(events[-1], ("ADMIN_ERROR", ["AUTH_EXPIRED"]))
        self.assertFalse(connection.authenticated)

    def test_state_before_authentication_and_invalid_utf8_fail_closed(self):
        for chunks in ([b"ADMIN_BEGIN\tLOBBY\t-1\t0\n"],
                       [b"ADMIN_OK\tAUTHENTICATED\n\xff\n"],
                       [b"ADMIN_OK\tAUTHENTICATED\nADMIN_DE"],
                       [b"ADMIN_OK\tAUTHENTICATED\tsecret\n"]):
            with self.subTest(chunks=chunks):
                connection, _, events = self.reader(chunks)
                self.assertEqual(events[-1], ("ERROR", ["PROTOCOL_ERROR"]))
                self.assertFalse(connection.authenticated)

    def test_send_uses_exact_wire_commands_and_requires_auth(self):
        connection = Connection(queue.Queue())
        connection.sock = Mock()
        connection.running = True
        with self.assertRaises(ConnectionError):
            connection.send("ADMIN_STATE")
        connection.authenticated = True
        for command, arguments, wire in (
            ("ADMIN_STATE", (), b"ADMIN_STATE\n"),
            ("ADMIN_HOST", (2,), b"ADMIN_HOST\t2\n"),
            ("ADMIN_ADD", (2, "DEFUSE"), b"ADMIN_ADD\t2\tDEFUSE\n"),
            ("ADMIN_BOMB", (2,), b"ADMIN_BOMB\t2\n"),
            ("ADMIN_PASSWORD", ("新密码123",), "ADMIN_PASSWORD\t新密码123\n".encode("utf-8")),
        ):
            connection.sock.reset_mock()
            connection.send(command, *arguments)
            connection.sock.sendall.assert_called_once_with(wire)
        with self.assertRaises(ValueError):
            connection.send("ADMIN_PASSWORD", "new\nADMIN_HOST\t2")
        with self.assertRaises(ValueError):
            connection.send("JOIN", "player")

    def test_send_failure_does_not_log_or_queue_password_or_exception_text(self):
        connection = Connection(queue.Queue())
        connection.sock = Mock()
        connection.running = connection.authenticated = True
        secret = "DoNotLogMyPassword"
        connection.sock.sendall.side_effect = OSError(secret)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
             patch.object(logging, "error") as log, self.assertRaises(ConnectionError) as error:
            connection.send("ADMIN_PASSWORD", secret)
        self.assertNotIn(secret, str(error.exception))
        self.assertEqual(output.getvalue(), "")
        log.assert_not_called()
        self.assertEqual(connection.events.get_nowait(), ("ERROR", ["SEND_FAILED"]))

    def test_connection_worker_handshake_retains_no_password_and_sanitizes_error(self):
        connection = Connection(queue.Queue())
        connection.running = True
        secret = "UniquePassword321"
        sock = Mock()
        sock.recv.side_effect = [b"ADMIN_ERROR\tAUTH_FAILED\n"]
        with patch("admin.main.socket.create_connection", return_value=sock) as create:
            credentials = [secret]
            connection._connect("127.0.0.1", 8765, credentials, connection._generation)
            self.assertEqual(credentials, [])
        create.assert_called_once_with(("127.0.0.1", 8765), timeout=8)
        sock.sendall.assert_called_once_with(("ADMIN\t" + secret + "\n").encode("utf-8"))
        self.assertNotIn(secret, repr(connection.__dict__))
        self.assertEqual(connection.events.get_nowait(), ("ADMIN_ERROR", ["AUTH_FAILED"]))
        connection.running = True
        with patch("admin.main.socket.create_connection", side_effect=OSError(secret)):
            connection._connect("host", 8765, [secret], connection._generation)
        self.assertEqual(connection.events.get_nowait(), ("ERROR", ["CONNECTION_FAILED"]))

    def test_local_tcp_peer_fragmentation_auth_and_state_command(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        received = []
        failures = []
        finished = threading.Event()

        def peer():
            try:
                with listener.accept()[0] as sock:
                    sock.settimeout(3)
                    stream = sock.makefile("rb")
                    received.append(stream.readline())
                    payload = "ADMIN_OK\tAUTHENTICATED\n".encode("utf-8")
                    sock.sendall(payload[:7])
                    sock.sendall(payload[7:])
                    received.append(stream.readline())
                    sock.sendall("ADMIN_BEGIN\tLOBBY\t-1\t0\nADMIN_DECK\nADMIN_END\n".encode("utf-8"))
                    finished.wait(3)
            except Exception as error:
                failures.append(type(error).__name__)
            finally:
                listener.close()

        thread = threading.Thread(target=peer, daemon=True)
        thread.start()
        connection = Connection(queue.Queue())
        try:
            connection.connect("127.0.0.1", listener.getsockname()[1], "Admin123")
            self.assertEqual(connection.events.get(timeout=3), ("ADMIN_OK", ["AUTHENTICATED"]))
            connection.send("ADMIN_STATE")
            self.assertEqual(connection.events.get(timeout=3), ("ADMIN_BEGIN", ["LOBBY", "-1", "0"]))
            self.assertEqual(connection.events.get(timeout=3), ("ADMIN_DECK", []))
            self.assertEqual(connection.events.get(timeout=3), ("ADMIN_END", []))
        finally:
            finished.set()
            connection.close()
            thread.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(received, [b"ADMIN\tAdmin123\n", b"ADMIN_STATE\n"])

    def test_stale_worker_cannot_authenticate_replacement_connection(self):
        connection = Connection(queue.Queue())
        connection.running = True
        generation = connection._generation
        sock = Mock()

        def reconnect(_size):
            connection.close()
            connection.running = True
            return b"ADMIN_OK\tAUTHENTICATED\n"

        sock.recv.side_effect = reconnect
        connection._reader(sock, generation)
        self.assertFalse(connection.authenticated)
        self.assertTrue(connection.events.empty())


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = __import__("tkinter").Tk()
            probe.withdraw()
            probe.destroy()
        except __import__("tkinter").TclError as error:
            raise unittest.SkipTest(f"Tk display unavailable: {error}")

    def setUp(self):
        self.app = App()
        self.app.withdraw()
        self.app.connection = Mock()
        self.app._handle("ADMIN_OK", ["AUTHENTICATED"])
        self.app.connection.reset_mock()
        self.feed_snapshot()
        self.app.players_tree.selection_set("1")
        self.app._update_controls()

    def tearDown(self):
        self.app._close()

    def feed_snapshot(self, deck=("9:SKIP", "8:NOPE")):
        for command, fields in (
            ("ADMIN_BEGIN", ["TURN", "0", "1"]),
            ("ADMIN_PLAYER", ["0", "甲", "1", "1"]),
            ("ADMIN_HAND", ["0", "1:DEFUSE"]),
            ("ADMIN_PLAYER", ["1", "乙", "1", "1"]),
            ("ADMIN_HAND", ["1", "2:ATTACK"]),
            ("ADMIN_DECK", list(deck)),
            ("ADMIN_END", []),
        ):
            self.app._handle(command, fields)

    def test_resizable_minimum_keeps_all_tables_and_controls_visible(self):
        self.app.geometry("780x650")
        self.app.deiconify()
        self.app.update()
        self.assertEqual(self.app.minsize(), (780, 650))
        for tree in (self.app.players_tree, self.app.hands_tree, self.app.deck_tree):
            self.assertGreater(tree.winfo_height(), 75)
        right = self.app.logout_button.winfo_rootx() - self.app.winfo_rootx() + self.app.logout_button.winfo_width()
        self.assertLessEqual(right, self.app.winfo_width())

    def test_login_passwords_are_masked_and_default_is_hint_only(self):
        self.assertEqual(self.app.password.get(), "")
        for entry in (self.app.password_entry, self.app.new_password_entry, self.app.confirm_password_entry):
            self.assertTrue(entry.cget("show"))
        self.assertIn("初始默认密码：Admin123", " ".join(
            str(widget.cget("text")) for widget in self.app.winfo_children()[0].winfo_children()
            if "text" in widget.keys()))

    def test_buttons_send_exact_target_and_card_commands_then_refresh_after_ack(self):
        actions = (
            (self.app.host_button, ("ADMIN_HOST", 1), "HOST_CHANGED"),
            (self.app.add_button, ("ADMIN_ADD", 1, "EXPLODING_KITTEN"), "CARD_ADDED"),
            (self.app.bomb_button, ("ADMIN_BOMB", 1), "KITTEN_INJECTED"),
        )
        self.app.card_choice.set(CARD_NAMES["EXPLODING_KITTEN"])
        for button, expected, ack in actions:
            with self.subTest(expected=expected):
                self.app.connection.reset_mock()
                button.invoke()
                self.app.connection.send.assert_called_once_with(*expected)
                self.assertTrue(button.instate(["disabled"]))
                self.app._handle("ADMIN_OK", [ack])
                self.assertEqual(self.app.connection.send.call_args.args, ("ADMIN_STATE",))
                self.assertTrue(self.app.authenticated)

    def test_password_confirmation_and_limits_prevent_command(self):
        with patch("admin.main.messagebox.showerror") as error:
            for password, confirm in (("newpass123", "different"), ("short", "short"),
                                      ("密" * 43, "密" * 43), ("1234\n6789", "1234\n6789")):
                self.app.new_password.set(password)
                self.app.confirm_password.set(confirm)
                self.app.password_button.invoke()
            self.assertEqual(error.call_count, 4)
            self.app.connection.send.assert_not_called()

    def test_password_change_sends_command_clears_inputs_and_retains_current_auth(self):
        self.app.new_password.set("UpdatedAdmin987")
        self.app.confirm_password.set("UpdatedAdmin987")
        self.app.password_button.invoke()
        self.app.connection.send.assert_called_once_with("ADMIN_PASSWORD", "UpdatedAdmin987")
        self.assertEqual((self.app.new_password.get(), self.app.confirm_password.get()), ("", ""))
        self.app._handle("ADMIN_OK", ["PASSWORD_CHANGED"])
        self.assertTrue(self.app.authenticated)
        self.assertIn("本窗口继续登录", self.app.status.get())
        self.assertEqual(self.app.connection.send.call_args.args, ("ADMIN_STATE",))
        self.assertNotIn("UpdatedAdmin987", self.app.status.get())

    def test_atomic_render_all_hands_and_top_first_deck(self):
        previous = self.app.state
        self.app._handle("ADMIN_BEGIN", ["TURN", "1", "2"])
        self.app._handle("ADMIN_PLAYER", ["1", "乙", "1", "1"])
        self.app._handle("ADMIN_HAND", ["1", "4:NOPE"])
        self.assertIs(self.app.state, previous)
        self.assertEqual(len(self.app.hands_tree.get_children()), 2)
        self.app._handle("ADMIN_DECK", ["10:DEFUSE", "11:ATTACK"])
        self.app._handle("ADMIN_END", [])
        self.assertIsNot(self.app.state, previous)
        rows = [self.app.deck_tree.item(item, "values") for item in self.app.deck_tree.get_children()]
        self.assertEqual(rows, [("1", "10", "拆除"), ("2", "11", "攻击")])
        self.assertEqual(self.app.players_tree.selection(), ("1",))

    def test_expired_auth_and_any_server_error_clear_private_state_and_credentials(self):
        for error in ("AUTH_EXPIRED", "AUTH_FAILED", "INVALID_PLAYER", "SensitivePassword123"):
            self.app.authenticated = True
            self.app.state = Snapshot(players={1: Player(1, "乙", True, True)})
            self.app.password.set("secret-login")
            self.app.new_password.set("secret-change")
            self.app.confirm_password.set("secret-change")
            self.app._handle("ADMIN_ERROR", [error])
            self.assertFalse(self.app.authenticated)
            self.assertEqual(self.app.state.players, {})
            self.assertEqual(self.app.password.get(), "")
            self.assertEqual(self.app.new_password.get(), "")
            self.assertEqual(self.app.confirm_password.get(), "")
            self.assertNotIn("SensitivePassword123", self.app.status.get())
            self.assertTrue(self.app.host_button.instate(["disabled"]))

    def test_failed_send_logs_out_and_does_not_display_exception_password(self):
        self.app.connection.send.side_effect = ConnectionError("password-must-not-appear")
        self.app.host_button.invoke()
        self.assertFalse(self.app.authenticated)
        self.assertNotIn("password-must-not-appear", self.app.status.get())

    def test_dead_players_can_be_online_host_but_cannot_get_cards_or_bomb(self):
        self.app.state.players[1] = Player(1, "乙", False, True)
        self.app._update_controls()
        self.assertFalse(self.app.host_button.instate(["disabled"]))
        self.assertTrue(self.app.add_button.instate(["disabled"]))
        self.assertTrue(self.app.bomb_button.instate(["disabled"]))
        self.app.add_button.invoke()
        self.app.bomb_button.invoke()
        self.app.connection.send.assert_not_called()

    def test_login_is_async_clears_password_and_refreshes_only_after_auth(self):
        self.app._logout()
        self.app.connection.reset_mock()
        self.app.password.set("Admin123")
        self.app.login_button.invoke()
        self.app.connection.connect.assert_called_once_with("127.0.0.1", 8765, "Admin123")
        self.app.connection.send.assert_not_called()
        self.assertEqual(self.app.password.get(), "")
        self.assertTrue(self.app.login_button.instate(["disabled"]))
        self.app._handle("ADMIN_OK", ["AUTHENTICATED"])
        self.app.connection.send.assert_called_once_with("ADMIN_STATE")

    def test_main_thread_queue_publishes_snapshot_and_bad_snapshot_logs_out(self):
        self.app.events.put(("ADMIN_BEGIN", ["LOBBY", "-1", "2"]))
        self.app.events.put(("ADMIN_DECK", []))
        self.app.events.put(("ADMIN_END", []))
        self.app.after_cancel(self.app._queue_job)
        self.app._drain_events()
        self.assertEqual(self.app.state.round_number, 2)
        self.assertEqual(self.app.state.players, {})
        self.app.events.put(("ADMIN_BEGIN", ["TURN", "0", "2"]))
        self.app.events.put(("ADMIN_END", []))
        self.app.after_cancel(self.app._queue_job)
        self.app._drain_events()
        self.assertFalse(self.app.authenticated)

    def test_mutation_timeout_logs_out_and_does_not_retry_unknown_result(self):
        self.app._pending = "ADMIN_BOMB"
        self.app._pending_deadline = 10
        self.app.after_cancel(self.app._state_job)
        with patch("admin.main.time.monotonic", return_value=11):
            self.app._poll_state()
        self.assertFalse(self.app.authenticated)
        self.app.connection.send.assert_not_called()
        self.assertIn("执行结果未知", self.app.status.get())

    def test_unexpected_success_ack_cannot_complete_another_command(self):
        self.app._pending = "ADMIN_ADD"
        with self.assertRaises(ProtocolError):
            self.app._handle("ADMIN_OK", ["PASSWORD_CHANGED"])

    def test_refresh_timeout_logs_out_instead_of_showing_stale_hands(self):
        self.app._state_deadline = 10
        self.app.after_cancel(self.app._state_job)
        with patch("admin.main.time.monotonic", return_value=11):
            self.app._poll_state()
        self.assertFalse(self.app.authenticated)
        self.assertEqual(self.app.hands_tree.get_children(), ())


if __name__ == "__main__":
    unittest.main()

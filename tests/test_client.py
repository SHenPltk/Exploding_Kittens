import queue
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from main import Connection, PlayerView, State  # noqa: E402


class ConnectionTests(unittest.TestCase):
    def test_reader_splits_multiple_lines_and_preserves_partial_line(self):
        events = queue.Queue()
        connection = Connection(events)
        connection.running = True

        class FakeSocket:
            def __init__(self):
                self.chunks = [b"WELCOME\t2\nSTATE\t1\tTUR", b"N\n", b""]

            def recv(self, _size):
                return self.chunks.pop(0)

        connection.sock = FakeSocket()
        connection._reader()

        self.assertEqual(events.get_nowait(), ("WELCOME", ["2"]))
        self.assertEqual(events.get_nowait(), ("STATE", ["1", "TURN"]))
        self.assertEqual(events.get_nowait(), ("ERROR", ["connection closed"]))

    def test_state_and_player_view_defaults(self):
        state = State()
        self.assertEqual(state.phase, "LOBBY")
        self.assertEqual(state.hand, [])
        player = PlayerView(3, "S", True, 8)
        self.assertEqual((player.player_id, player.name, player.alive, player.hand_size),
                         (3, "S", True, 8))


if __name__ == "__main__":
    unittest.main()

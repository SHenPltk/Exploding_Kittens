import queue
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "client"))
from main import App, Connection, PlayerView, State
from i18n import CARD_NAMES, EVENTS, card_name, event_text, text
from presentation import (CARD_ORDER, HandHighlights, parse_card,
                          response_description, sorted_hand)


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

    def test_send_keeps_internal_card_id_in_protocol(self):
        connection = Connection(queue.Queue())
        connection.sock = Mock()
        connection.running = True
        connection.send("PLAY", 17, -1)
        connection.sock.sendall.assert_called_once_with(b"PLAY\t17\t-1\n")

    def test_reader_decodes_chinese_names_split_across_packets(self):
        payload = "PLAYER\t0\t玩家甲\t1\t8\t-\n".encode("utf-8")
        events = queue.Queue()
        connection = Connection(events)
        connection.running = True
        connection.sock = Mock()
        connection.sock.recv.side_effect = [payload[:13], payload[13:], b""]
        connection._reader()
        self.assertEqual(events.get_nowait(), ("PLAYER", ["0", "玩家甲", "1", "8", "-"]))


class PresentationTests(unittest.TestCase):
    def test_state_and_player_view_defaults(self):
        state = State()
        self.assertEqual(state.phase, "LOBBY")
        self.assertEqual(state.hand, [])
        self.assertIsNone(state.bomb_probability)
        player = PlayerView(3, "S", True, 8)
        self.assertEqual((player.player_id, player.name, player.alive, player.hand_size),
                         (3, "S", True, 8))

    def test_sort_uses_requested_order_and_groups_all_cat_types(self):
        hand = [(index + 1, kind) for index, kind in enumerate(reversed(CARD_ORDER))]
        original = list(hand)
        self.assertEqual([kind for _, kind in sorted_hand(hand)], list(CARD_ORDER))
        self.assertEqual(hand, original)
        self.assertEqual(sorted_hand([(6, "DEFUSE"), (2, "DEFUSE")]),
                         [(2, "DEFUSE"), (6, "DEFUSE")])

    def test_card_name_never_contains_the_protocol_id(self):
        card_id, kind = parse_card("321:DEFUSE")
        self.assertEqual(card_id, 321)
        self.assertEqual(card_name("zh", kind), "拆除")
        self.assertEqual(card_name("en", kind), "Defuse")
        self.assertEqual(set(CARD_NAMES), set(CARD_ORDER))

    def test_highlight_only_new_ids_and_expire_after_three_seconds(self):
        highlights = HandHighlights()
        hand = [(1, "DEFUSE"), (2, "ATTACK")]
        self.assertEqual(highlights.update(hand, 10), set())
        self.assertEqual(highlights.update(hand + [(3, "DEFUSE")], 11), {3})
        self.assertEqual(highlights.active(13.9), {3})
        self.assertEqual(highlights.active(14), set())

    def test_duplicate_snapshots_do_not_extend_highlight(self):
        highlights = HandHighlights()
        highlights.update([(1, "DEFUSE")], 0)
        highlights.update([(1, "DEFUSE"), (2, "ATTACK")], 1)
        self.assertEqual(highlights.update([(2, "ATTACK"), (1, "DEFUSE")], 3), set())
        self.assertEqual(highlights.active(4), set())

    def test_leaving_hand_removes_highlight_and_returning_is_a_new_gain(self):
        highlights = HandHighlights()
        highlights.update([(1, "DEFUSE")], 0)
        highlights.update([(1, "DEFUSE"), (2, "ATTACK")], 1)
        highlights.update([(1, "DEFUSE")], 2)
        self.assertEqual(highlights.active(2), set())
        self.assertEqual(highlights.update([(1, "DEFUSE"), (2, "ATTACK")], 3), {2})

    def test_bomb_odds_empty_all_safe_and_all_bombs(self):
        self.assertIsNone(State(draw_count=0, bombs_remaining=0).bomb_probability)
        self.assertIsNone(State(draw_count=8).bomb_probability)
        self.assertEqual(State(draw_count=8, bombs_remaining=0).bomb_probability, 0)
        self.assertEqual(State(draw_count=2, bombs_remaining=2).bomb_probability, 1)
        self.assertAlmostEqual(State(draw_count=35, bombs_remaining=1).bomb_probability, 1 / 35)

    def test_response_banner_keeps_original_combo_and_latest_nope(self):
        state = State(phase="RESPONSE", pending_type="COMBO2", pending_actor=0,
                      pending_target=1, pending_cards=["ATTACK", "ATTACK"],
                      pending_nopes=1, response_leader=1)
        state.players = {0: PlayerView(0, "甲", True, 5), 1: PlayerView(1, "乙", True, 6)}
        zh = response_description(state, "zh")
        self.assertIn("甲 打出了 双张组合 (攻击 + 攻击)", zh)
        self.assertIn("乙 刚打出", zh)
        self.assertIn("取消原动作", zh)
        state.pending_nopes = 2
        state.response_leader = 0
        self.assertIn("执行原动作", response_description(state, "zh"))
        self.assertIn("Two of a kind (Attack + Attack)", response_description(state, "en"))
        state.phase = "TURN"
        self.assertEqual(response_description(state, "zh"), "")

    def test_translation_modes_cover_cards_errors_and_phases(self):
        for kind in CARD_NAMES:
            self.assertNotEqual(card_name("zh", kind), card_name("en", kind))
        for code, labels in EVENTS.items():
            self.assertEqual(event_text("zh", code), labels[0])
            self.assertEqual(event_text("en", code), labels[1])
        self.assertEqual(text("zh", "RESPONSE"), "等待反制")
        self.assertEqual(text("en", "RESPONSE"), "Response")


class AppTests(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.app.withdraw()
        self.app.connection.running = True
        self.app._handle("WELCOME", ["0"])
        self.app._handle("ROSTER", ["0", "甲", "1", "乙"])

    def tearDown(self):
        self.app.connection.close()
        self.app.destroy()

    def snapshot(self, hand, phase="TURN", pending_type="", nopes=0, leader=-1, cards=None):
        fields = ["1", phase, "0", "1", "35", "0", "-1",
                  pending_type, "0", "1", str(nopes), str(leader)]
        self.app._handle("STATE", fields)
        self.app._handle("DECK", ["1"])
        if cards:
            self.app._handle("PENDING", ["0", *cards])
        self.app._handle("PLAYER", ["0", "甲", "1", str(len(hand)), "-"])
        self.app._handle("PLAYER", ["1", "乙", "1", "8", "-"])
        self.app._handle("HAND", hand)

    def test_initial_mode_is_chinese_and_all_visible_labels_switch(self):
        self.assertEqual(self.app.language, "zh")
        self.assertEqual(self.app.draw_button.cget("text"), "摸牌")
        self.app.language_name.set("English")
        self.app._change_language()
        self.assertEqual(self.app.draw_button.cget("text"), "Draw")
        self.app.language_name.set("简体中文")
        self.app._change_language()
        self.assertEqual(self.app.start_button.cget("text"), "开始游戏")

    def test_hand_is_applied_atomically_and_names_only_are_rendered(self):
        self.snapshot(["7:NOPE", "3:ATTACK", "2:DEFUSE"])
        self.assertEqual(self.app.state.hand, [])
        self.app._handle("ENDSTATE", [])
        self.assertEqual(list(self.app._card_widgets), [2, 3, 7])
        self.assertEqual([widget.cget("text") for widget in self.app._card_widgets.values()],
                         ["拆除", "攻击", "不行！"])
        self.assertIn("2.86%", self.app.risk_info.cget("text"))

    def test_selection_and_gain_highlight_survive_repeated_snapshots(self):
        self.snapshot(["1:DEFUSE", "2:ATTACK"])
        self.app._handle("ENDSTATE", [])
        self.app._card_widgets[2].invoke()
        with patch("main.time.monotonic", return_value=100):
            self.snapshot(["1:DEFUSE", "2:ATTACK", "3:NOPE"])
            self.app._handle("ENDSTATE", [])
        self.assertEqual(self.app.selected_cards, [2])
        self.assertEqual(self.app._card_widgets[3].cget("style"), "NewCard.TCheckbutton")
        with patch("main.time.monotonic", return_value=101):
            self.snapshot(["3:NOPE", "2:ATTACK", "1:DEFUSE"])
            self.app._handle("ENDSTATE", [])
        self.assertEqual(self.app.selected_cards, [2])
        self.assertEqual(self.app.highlights.deadlines[3], 103)
        with patch("main.time.monotonic", return_value=104):
            self.app._refresh_highlights()
        self.assertEqual(self.app._card_widgets[3].cget("style"), "TCheckbutton")
        self.assertEqual(self.app.gain_info.cget("text"), "")

    def test_single_cat_cannot_play_but_matching_pair_can_combo(self):
        self.snapshot(["1:TACOCAT", "2:TACOCAT"])
        self.app._handle("ENDSTATE", [])
        self.app._card_widgets[1].invoke()
        self.assertTrue(self.app.play_button.instate(["disabled"]))
        with patch("main.messagebox.showinfo"), patch.object(self.app.connection, "send") as send:
            self.app._play_selected()
            send.assert_not_called()
        self.app._card_widgets[2].invoke()
        self.assertFalse(self.app.combo_button.instate(["disabled"]))
        with patch.object(self.app, "_choose", return_value=1), patch.object(self.app.connection, "send") as send:
            self.app._combo()
            send.assert_called_once_with("COMBO", 1, 2, 1)

    def test_response_banner_and_language_switch_show_played_cards(self):
        self.snapshot(["1:DEFUSE"], phase="RESPONSE", pending_type="COMBO2",
                      nopes=1, leader=1, cards=["TACOCAT", "TACOCAT"])
        self.app._handle("ENDSTATE", [])
        self.assertIn("甲 打出了 双张组合", self.app.response_info.cget("text"))
        self.assertIn("乙 刚打出", self.app.response_info.cget("text"))
        self.app.language_name.set("English")
        self.app._change_language()
        self.assertIn("Tacocat + Tacocat", self.app.response_info.cget("text"))
        self.assertIn("just played Nope", self.app.log.get("1.0", "end"))

    def test_favor_dialog_uses_card_labels_but_sends_internal_id(self):
        self.snapshot(["9:DEFUSE", "4:DEFUSE"], phase="FAVOR")
        self.app._handle("ENDSTATE", [])
        self.app.player_id = 1
        with patch.object(self.app, "_choose", return_value=9) as choose, \
             patch.object(self.app.connection, "send") as send:
            self.app._resolve_prompt()
            self.assertEqual(choose.call_args.args[2], [(4, "拆除"), (9, "拆除")])
            send.assert_called_once_with("FAVOR", 9)

    def test_defuse_prompt_has_no_decline_action(self):
        self.snapshot(["1:DEFUSE"], phase="KITTEN")
        self.app._handle("ENDSTATE", [])
        self.assertFalse(hasattr(self.app, "decline_button"))
        self.assertFalse(self.app.resolve_button.instate(["disabled"]))
        self.assertTrue(self.app.restart_button.instate(["disabled"]))
        with patch.object(self.app, "_choose_position", return_value=0), \
             patch.object(self.app.connection, "send") as send:
            self.app._resolve_prompt()
            send.assert_called_once_with("DEFUSE", 0)

    def test_restart_button_is_only_available_for_online_host_after_game(self):
        self.snapshot(["1:DEFUSE"], phase="GAME_OVER")
        self.app._handle("ROUND", ["1"])
        self.app._handle("SESSION", ["0", "2"])
        self.app._handle("ENDSTATE", [])
        self.assertFalse(self.app.restart_button.instate(["disabled"]))
        with patch.object(self.app.connection, "send") as send:
            self.app.restart_button.invoke()
            send.assert_called_once_with("RESTART")
        self.app.language_name.set("English")
        self.app._change_language()
        self.assertEqual(self.app.restart_button.cget("text"), "Restart game")
        for player, connected, phase, running in ((1, 2, "GAME_OVER", True),
                                                  (0, 1, "GAME_OVER", True),
                                                  (0, 2, "TURN", True),
                                                  (0, 2, "GAME_OVER", False)):
            self.app.player_id = player
            self.app.state.connected_count = connected
            self.app.state.phase = phase
            self.app.connection.running = running
            self.app._update_controls()
            self.assertTrue(self.app.restart_button.instate(["disabled"]))

    def test_new_round_clears_selection_and_highlights_even_for_reused_ids(self):
        self.snapshot(["1:DEFUSE"])
        self.app._handle("ROUND", ["1"])
        self.app._handle("ENDSTATE", [])
        self.snapshot(["1:DEFUSE", "2:ATTACK"])
        self.app._handle("ROUND", ["1"])
        self.app._handle("ENDSTATE", [])
        self.app._card_widgets[2].invoke()
        self.assertEqual(self.app.selected_cards, [2])
        self.assertTrue(self.app.highlights.deadlines)
        self.snapshot(["2:ATTACK", "3:DEFUSE"])
        self.app._handle("ROUND", ["2"])
        self.app._handle("ENDSTATE", [])
        self.assertEqual(self.app.selected_cards, [])
        self.assertEqual(self.app.highlights.deadlines, {})
        self.assertEqual(self.app._highlight_jobs, set())
        self.assertEqual(self.app.gain_info.cget("text"), "")
        self.assertEqual(self.app.state.round_number, 2)

    def test_real_card_picker_shows_duplicate_names_without_ids(self):
        observed = {}

        def select_second():
            dialog = next(widget for widget in self.app.winfo_children()
                          if widget.winfo_class() == "Toplevel")
            listbox = next(widget for widget in dialog.winfo_children()
                           if widget.winfo_class() == "Listbox")
            observed["labels"] = listbox.get(0, "end")
            controls = dialog.winfo_children()[-1]
            observed["buttons"] = [widget.cget("text") for widget in controls.winfo_children()]
            listbox.selection_clear(0, "end")
            listbox.selection_set(1)
            controls.winfo_children()[0].invoke()

        self.app.after(20, select_second)
        result = self.app._choose("favor_title", "favor_prompt", [(4, "拆除"), (9, "拆除")])
        self.assertEqual(result, 9)
        self.assertEqual(observed["labels"], ("拆除", "拆除"))
        self.assertEqual(observed["buttons"], ["确定", "取消"])

    def test_defuse_position_dialog_has_localized_buttons(self):
        observed = {}

        def accept_position():
            dialog = next(widget for widget in self.app.winfo_children()
                          if widget.winfo_class() == "Toplevel")
            children = dialog.winfo_children()
            observed["title"] = dialog.title()
            observed["prompt"] = children[0].cget("text")
            spinbox = children[1]
            spinbox.delete(0, "end")
            spinbox.insert(0, "7")
            observed["buttons"] = [widget.cget("text") for widget in children[2].winfo_children()]
            children[2].winfo_children()[0].invoke()

        self.app.state.draw_count = 12
        self.app.after(20, accept_position)
        self.assertEqual(self.app._choose_position(), 7)
        self.assertIn("拆除", observed["title"])
        self.assertIn("12", observed["prompt"])
        self.assertEqual(observed["buttons"], ["确定", "取消"])


if __name__ == "__main__":
    unittest.main()

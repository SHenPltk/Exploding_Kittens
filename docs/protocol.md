# LAN protocol

The C++ host listens on TCP port 8765 by default. Messages are UTF-8, one line each, with tab-separated fields. Names and free text are sanitized by the server before they are sent back.

## Client commands

| Command | Meaning |
|---|---|
| `JOIN name` | First line on a connection; joins the lobby. |
| `START` | Player 0 starts with 2–5 connected players. |
| `PLAY card [target]` | Play one hand card. Favor needs a target. |
| `COMBO a b target` | Two of a kind. |
| `COMBO a b c target TYPE` | Three of a kind, requesting `TYPE`. |
| `DRAW` | End the acting player's action phase by drawing. |
| `PASS` | Pass the current Nope response window. |
| `NOPE card` | Put a Nope into the current response window. |
| `FAVOR card` | The Favor target gives the selected card. |
| `DEFUSE n` | Secretly reinsert the drawn Kitten `n` cards from the top. |
| `EXPLODE` | Decline the Defuse prompt. |

## Server messages

- `WELCOME player_id`
- `ROSTER player_id name ...`
- `STATE sequence phase active_player turns_remaining draw_count discard_count winner pending_type pending_actor pending_target nope_count response_leader`
- `PLAYER player_id name alive hand_count exploded_card_or_dash`
- `DISCARD card_id:TYPE` (public; repeated for the complete discard pile)
- `HAND card_id:TYPE ...` (private to that connection)
- `EVENT OK|ERROR message`
- `PRIVATE SEE_FUTURE card_id:TYPE ...` (private)
- `ENDSTATE`
- `PONG`

The draw pile, other players' hands, Defuse insertion position, and See the Future result are never broadcast outside the owning player. The server validates every command again even if the GUI disabled the corresponding button.

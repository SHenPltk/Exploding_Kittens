# LAN protocol

The C++ host listens on TCP port 8765 by default. Messages are UTF-8, one line each, with tab-separated fields. Names and free text are sanitized by the server before they are sent back.

## Client commands

| Command | Meaning |
|---|---|
| `JOIN name` | First line; joins the lobby with a unique, nonempty nickname after trimming surrounding ASCII whitespace (case-sensitive). Duplicate names return NAME_ALREADY_TAKEN and close only the rejected connection. |
| `START` | The host starts with 2–5 connected players. |
| `RESTART` | The online host starts a fresh game after GAME_OVER with the remaining 2–5 online players. |
| `PLAY card [target]` | Play one hand card. Favor needs a target. |
| `COMBO a b target` | Two of a kind. |
| `COMBO a b c target TYPE` | Three of a kind, requesting `TYPE`. |
| `DRAW` | End the acting player's action phase by drawing. |
| `PASS` | Pass the current Nope response window. |
| `NOPE card` | Put a Nope into the current response window. |
| `FAVOR card` | The Favor target gives the selected card. |
| `DEFUSE n` | Secretly reinsert the drawn Kitten `n` cards from the top. |
| `EXPLODE` | Legacy command: rejected with DEFUSE_REQUIRED if the prompted player holds Defuse; no-Defuse draws eliminate automatically. |

## Server messages

- `WELCOME player_id`
- `ROSTER player_id name ...`
- `STATE sequence phase active_player turns_remaining draw_count discard_count winner pending_type pending_actor pending_target nope_count response_leader`
- `ROUND round_number` (monotonically increases for every fresh game; card ids may be reused across rounds)
- `SESSION host_player_id connected_count` (current online host and number eligible for restart)
- `DECK bomb_count` (public; count only in the current draw pile)
- `PENDING original_actor TYPE ...` (public; sent only in RESPONSE, including all original combo card types)
- `PLAYER player_id name alive hand_count exploded_card_or_dash`
- `DISCARD card_id:TYPE` (public; repeated for the complete discard pile)
- `HAND card_id:TYPE ...` (private to that connection)
- `EVENT OK|ERROR message`
- `PRIVATE SEE_FUTURE card_id:TYPE ...` (private)
- `ENDSTATE`
- `PONG`

On RESTART, the server requires GAME_OVER and the current online host; otherwise it returns GAME_NOT_OVER or ONLY_HOST_CAN_START. It requires 2–5 online players. Disconnected sessions are excluded; remaining players (including eliminated players) are reseated in their original order. WELCOME updates each player's id before the first new snapshot. The server uses a fresh Game and shuffle, clears the winner/discards/pending effects, increments ROUND, and keeps STATE sequence increasing. Clients reset selections and highlight timers on ROUND changes while preserving language and connections. SESSION lets clients disable restart when only one player remains and transfer the host if the previous host disconnected. JOIN remains closed after the first game starts.

A snapshot starts with STATE and ends with ENDSTATE. Clients assemble it before updating the visible hand. DECK counts only bombs physically in the draw pile: drawing a Kitten reduces it immediately; Defuse reinsertion restores it. The client displays bomb_count / draw_count as a deck-ratio probability (undefined when draw_count is zero), not as a guarantee about a known top card. PENDING includes the original played types throughout a Nope chain; STATE response_leader and nope_count identify the latest Nope and the current action parity.

Card ids are protocol identifiers; the UI shows card names only. Language and hand sorting are client-side presentation and do not change protocol card type names. The protocol calls See the Future SEE_FUTURE.

A single Cat card is rejected with CAT_CARDS_REQUIRE_COMBO without modifying the hand, discard pile, or phase. Two and three matching Cat cards remain valid combos.

The complete draw-pile order, other players' hands, Defuse insertion position, and See the Future result are never broadcast outside the owning player. The server validates every command again even if the GUI disabled the corresponding button.

# LAN protocol

The C++ host listens on TCP port 8765 by default. Messages are UTF-8, one line each, with tab-separated fields. Names and free text are sanitized by the server before they are sent back.

## Client commands

| Command | Meaning |
|---|---|
| `JOIN name` | First line; joins the lobby with a unique, nonempty nickname after trimming surrounding ASCII whitespace (case-sensitive). Duplicate names return NAME_ALREADY_TAKEN and close only the rejected connection. |
| `START` | The host starts with 2–5 connected players. |
| `RESTART` | The online host starts a fresh game after GAME_OVER with the remaining 2–5 online players. |
| `SET_FAST 0\|1` | Host-only next-game option, accepted in the lobby or after GAME_OVER; enabling is limited to at most 3 connected players (START still requires at least 2). |
| `SET_FREE_ATTACK 0\|1` | Host-only next-game option in lobby/GAME_OVER. |
| `KICK player_id` | Host removes a different online player; active players forfeit. |
| `PLAY card [target]` | Play one hand card. Favor and Free Attack require another living target. |
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
- `OPTIONS fast_enabled free_attack_enabled` (two `0`/`1` next-game flags, broadcast in lobby and snapshots)
- `MODE fast_enabled free_attack_enabled` (immutable current-game flags)
- `ONLINE player_id ...` (connected game participants eligible for host removal)
- `REQUESTED TYPE` (RESPONSE COMBO3 only; original requested title remains public throughout Nope chains)
- `PROMPT player_id` (KITTEN only; actual prompted player, which can differ from active_player after admin injection)
- `DECK bomb_count` (public; count only in the current draw pile)
- `PENDING original_actor TYPE ...` (public; sent only in RESPONSE, including all original combo card types)
- `PLAYER player_id name alive hand_count exploded_card_or_dash`
- `DISCARD card_id:TYPE` (public; at most one per snapshot, the top discard; omitted when empty; STATE retains the total discard_count)
- `HAND card_id:TYPE ...` (private to that connection)
- `EVENT OK|ERROR message`
- `PRIVATE SEE_FUTURE card_id:TYPE ...` (private)
- `ENDSTATE`
- `PONG`

On RESTART, the server requires GAME_OVER and the current online host; otherwise it returns GAME_NOT_OVER or ONLY_HOST_CAN_START. It requires 2–5 online players. Disconnected sessions are excluded; remaining players (including eliminated players) are reseated in their original order. WELCOME updates each player's id before the first new snapshot. The server uses a fresh Game and shuffle, clears the winner/discards/pending effects, increments ROUND, and keeps STATE sequence increasing. Clients reset selections and highlight timers on ROUND changes while preserving language and connections. SESSION lets clients disable restart when only one player remains and transfer the host if the previous host disconnected. JOIN remains closed after the first game starts.

SET_FAST changes only OPTIONS. It requires the current online host (ONLY_HOST_CAN_SET_OPTIONS); active games reject it with OPTIONS_LOCKED_DURING_GAME. Enabling for 4–5 players is rejected with FAST_MODE_REQUIRES_2_OR_3_PLAYERS. A fourth lobby join automatically disables the option, and it remains disabled after players leave until the host reenables it. START/RESTART enforce the player limit again. In fast mode setup, after the initial deal and before adding Kittens, shuffle and remove floor(remaining non-Kitten cards / 3) cards without revealing their identities; final draw sizes are 24/20 for 2/3 players. Initial hands remain eight cards. MODE describes the current game; OPTIONS can differ when the host configures the next game after GAME_OVER.

A snapshot starts with STATE and ends with ENDSTATE. Clients assemble it before updating the visible hand. DECK counts only bombs physically in the draw pile: drawing a Kitten reduces it immediately; Defuse reinsertion restores it. The client displays bomb_count / draw_count as a deck-ratio probability (undefined when draw_count is zero), not as a guarantee about a known top card. PENDING includes the original played types throughout a Nope chain; STATE response_leader and nope_count identify the latest Nope and the current action parity.

Clients compare consecutive private HAND snapshots by card id within the same ROUND. Cards absent from the new hand produce a four-second private loss notice and a persistent local log entry, including combo payments, theft, Favor and Defuse. Duplicate snapshots produce no new notice. Round resets and eliminated-hand hiding do not generate a false loss report. These notices are computed locally and never broadcast. The client displays only the latest DISCARD, even if communicating with an older server that sends multiple records.

Card ids are protocol identifiers; the UI shows card names only. Language and hand sorting are client-side presentation and do not change protocol card type names. The protocol calls See the Future SEE_FUTURE.

A single Cat card is rejected with CAT_CARDS_REQUIRE_COMBO without modifying the hand, discard pile, or phase. Two and three matching Cat cards remain valid combos.

Ordinary player connections never receive the complete draw-pile order, other players' hands, Defuse insertion position, or another player's See the Future result. The server validates every command again even if the GUI disabled the corresponding button. A separately authenticated administrator can explicitly inspect all hands and the deck as described below.

## Free Attack and removal

SET_FREE_ATTACK uses the same host/lobby/GAME_OVER permission rules as SET_FAST, without the fast-mode player limit. Default Attack retains next-alive behavior and rejects explicit targets. With Free Attack on, Attack requires another living target; debt transfer/Nope parity remain unchanged. The turn after all victim debt completes is the victim's clockwise successor. If a pending target forfeits, the original action is cancelled without changing the actor's turn.

KICK requires the actual nominated online host (not necessarily player 0); self/offline targets are rejected. The removed connection receives EVENT ERROR KICKED and closes. Lobby players are reindexed with WELCOME; in-game removal uses the engine's forfeit path and cancels or resolves pending prompts safely. Host nomination survives reindex and rematches; if the host leaves, the next online player clockwise becomes host.

PROMPT rather than active_player identifies who must Defuse. Clients auto-open required Defuse/Favor dialogs once per outstanding prompt, close stale dialogs when state changes, and do not require a Resolve button. Clicking Nope picks one private Nope id automatically.

## Authenticated administrator protocol

First line on a separate TCP connection: `ADMIN password` (tab-separated). This connection never joins the player roster. It receives `ADMIN_OK AUTHENTICATED` or `ADMIN_ERROR AUTH_FAILED`; incorrect auth closes the connection. All commands and responses remain tab-separated lines.

| Command | Behavior |
|---|---|
| `ADMIN_STATE` | Private complete snapshot (below). |
| `ADMIN_HOST player_id` | Nominate a connected player as host; broadcast normal SESSION update. |
| `ADMIN_ADD player_id TYPE` | Create a new unique hand card for a living player in an active game. Kitten requires TURN and triggers immediately. |
| `ADMIN_BOMB player_id` | TURN only: finish exactly one current turn, then create and trigger a Kitten for the selected living player. |
| `ADMIN_PASSWORD new_password` | Persist a new 8–128-byte UTF-8 password with no tab/CR/LF/NUL. |

Mutation ACKs are ADMIN_OK HOST_CHANGED / CARD_ADDED / KITTEN_INJECTED / PASSWORD_CHANGED. Failures use ADMIN_ERROR code; no private data accompanies failed auth. ADMIN_STATE replies:

```text
ADMIN_BEGIN phase host_player_id round_number
ADMIN_PLAYER id name alive online
ADMIN_HAND id card_id:TYPE ...
... repeated PLAYER/HAND for all players ...
ADMIN_DECK card_id:TYPE ...
ADMIN_END
```

ADMIN_DECK is **top first**; HAND records include eliminated hands and empty hands. Read snapshots atomically. Administrator card creation never removes/reorders draw-pile cards; a synthetic Kitten is discarded after Defuse (together with Defuse), not inserted into the original deck. An injected Kitten prompt consumes no additional turn; ordinary drawn Kittens still end their own drawn turn normally. Forced turn completion is blocked during RESPONSE/FAVOR/KITTEN.

Credentials default to Admin123 only when no verifier file exists. Server writes a 16-byte random salt and 32-byte PBKDF2-HMAC-SHA256 verifier with 600,000 iterations using atomic replacement. Malformed existing credentials fail closed. Password changes invalidate other administrator sessions with AUTH_EXPIRED; the changer remains authenticated. Bounded authentication attempts, handshake deadlines and session limits apply. The management protocol has no TLS; use it on a trusted LAN.

## HTTP client distribution

`ek_server [game_port=8765] [http_port=game_port+1; 0 disables] [credentials_path]` binds the game and enabled HTTP service to all IPv4 interfaces. GET/HEAD `/` serves a download landing page; GET/HEAD `/client.zip` serves a valid ZIP containing only `main.py`, `i18n.py`, `presentation.py`, `start_client.bat` and client README. Python 3.10+ with Tkinter is required. Other paths, including traversal, administrator code and credentials, return 404. No directory listing or general file endpoint exists. Download files are read from an explicit three-file client allowlist, with generated launcher/instructions; neither the admin program nor verifier is included.

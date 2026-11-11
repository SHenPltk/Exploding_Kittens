"""Client snapshots and display helpers, independent of Tk widgets."""
from __future__ import annotations

from dataclasses import dataclass, field

try:
    from .i18n import card_name, text
except ImportError:
    from i18n import card_name, text


CAT_TYPES = (
    "TACOCAT", "CATTERMELON", "HAIRY_POTATO_CAT",
    "RAINBOW_RALPHING_CAT", "BEARD_CAT",
)
CARD_ORDER = ("DEFUSE", "ATTACK", "FAVOR", "NOPE", "SHUFFLE",
              "SKIP", "SEE_FUTURE", *CAT_TYPES, "EXPLODING_KITTEN")
CARD_RANK = {kind: index for index, kind in enumerate(CARD_ORDER)}


def sorted_hand(cards: list[tuple[int, str]]) -> list[tuple[int, str]]:
    # The internal id breaks ties; duplicate titles remain individually selectable.
    return sorted(cards, key=lambda card: (CARD_RANK.get(card[1], len(CARD_RANK)), card[0]))


def lost_cards(previous: list[tuple[int, str]], current: list[tuple[int, str]]) -> list[tuple[int, str]]:
    current_ids = {card_id for card_id, _ in current}
    return sorted_hand([card for card in previous if card[0] not in current_ids])


def parse_card(token: str) -> tuple[int, str]:
    card_id, kind = token.split(":", 1)
    return int(card_id), kind


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
    round_number: int = 0
    host_player: int = 0
    connected_count: int | None = None
    fast_mode: bool = False
    next_fast_mode: bool = False
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
    bombs_remaining: int | None = None
    pending_cards: list[str] = field(default_factory=list)
    players: dict[int, PlayerView] = field(default_factory=dict)
    hand: list[tuple[int, str]] = field(default_factory=list)
    discard: list[str] = field(default_factory=list)

    @classmethod
    def from_fields(cls, parts: list[str]) -> State:
        if len(parts) < 12:
            raise ValueError("Incomplete STATE message")
        return cls(sequence=int(parts[0]), phase=parts[1], active_player=int(parts[2]),
                   turns_remaining=int(parts[3]), draw_count=int(parts[4]),
                   discard_count=int(parts[5]), winner=int(parts[6]),
                   pending_type=parts[7], pending_actor=int(parts[8]),
                   pending_target=int(parts[9]), pending_nopes=int(parts[10]),
                   response_leader=int(parts[11]))

    @property
    def bomb_probability(self) -> float | None:
        if self.draw_count <= 0 or self.bombs_remaining is None:
            return None
        return self.bombs_remaining / self.draw_count

    def player_name(self, player_id: int, language: str) -> str:
        player = self.players.get(player_id)
        return player.name if player else text(language, "unknown_player")


class HandHighlights:
    """Keep gain highlights alive across duplicate snapshots for three seconds."""
    def __init__(self, duration: float = 3.0) -> None:
        self.duration = duration
        self.initialized = False
        self.previous: set[int] = set()
        self.deadlines: dict[int, float] = {}

    def update(self, cards: list[tuple[int, str]], now: float) -> set[int]:
        current = {card_id for card_id, _ in cards}
        gained = current - self.previous if self.initialized else set()
        self.deadlines = {card_id: deadline for card_id, deadline in self.deadlines.items()
                          if card_id in current and deadline > now}
        for card_id in gained:
            self.deadlines[card_id] = now + self.duration
        self.previous = current
        self.initialized = True
        return gained

    def active(self, now: float) -> set[int]:
        self.deadlines = {card_id: deadline for card_id, deadline in self.deadlines.items()
                          if deadline > now}
        return set(self.deadlines)


def response_description(state: State, language: str) -> str:
    if state.phase != "RESPONSE":
        return ""
    cards = " + ".join(card_name(language, kind) for kind in state.pending_cards)
    if state.pending_type in {"COMBO2", "COMBO3"}:
        combo_name = text(language, state.pending_type.lower())
        cards = f"{combo_name} ({cards})"
    elif not cards:
        cards = card_name(language, state.pending_type)
    result = text(language, "response",
                  player=state.player_name(state.pending_actor, language), cards=cards)
    if state.pending_target >= 0:
        result += text(language, "response_target",
                       target=state.player_name(state.pending_target, language))
    if state.pending_nopes:
        result += text(language, "response_nope",
                       player=state.player_name(state.response_leader, language),
                       count=state.pending_nopes)
    result += text(language, "cancelled" if state.pending_nopes % 2 else "restored")
    return result + text(language, "response_instruction")

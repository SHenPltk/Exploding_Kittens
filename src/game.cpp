#include "exploding_kittens/game.hpp"

#include <algorithm>
#include <iterator>
#include <stdexcept>
#include <utility>

namespace ek {
namespace {
int countFor(CardType type) {
  switch (type) {
    case CardType::ExplodingKitten: return 4;
    case CardType::Defuse: return 6;
    case CardType::Attack: return 4;
    case CardType::Skip: return 4;
    case CardType::SeeFuture: return 5;
    case CardType::Shuffle: return 4;
    case CardType::Favor: return 4;
    case CardType::Nope: return 5;
    default: return 4;
  }
}

const std::vector<CardType>& allTypes() {
  static const std::vector<CardType> types = {
      CardType::ExplodingKitten, CardType::Defuse, CardType::Attack,
      CardType::Skip, CardType::SeeFuture, CardType::Shuffle, CardType::Favor,
      CardType::Nope, CardType::Tacocat, CardType::Cattermelon,
      CardType::HairyPotatoCat, CardType::RainbowRalphingCat, CardType::BeardCat};
  return types;
}
}

std::string cardTypeName(CardType type) {
  switch (type) {
    case CardType::ExplodingKitten: return "EXPLODING_KITTEN";
    case CardType::Defuse: return "DEFUSE";
    case CardType::Attack: return "ATTACK";
    case CardType::Skip: return "SKIP";
    case CardType::SeeFuture: return "SEE_FUTURE";
    case CardType::Shuffle: return "SHUFFLE";
    case CardType::Favor: return "FAVOR";
    case CardType::Nope: return "NOPE";
    case CardType::Tacocat: return "TACOCAT";
    case CardType::Cattermelon: return "CATTERMELON";
    case CardType::HairyPotatoCat: return "HAIRY_POTATO_CAT";
    case CardType::RainbowRalphingCat: return "RAINBOW_RALPHING_CAT";
    case CardType::BeardCat: return "BEARD_CAT";
  }
  return "UNKNOWN";
}

std::optional<CardType> cardTypeFromName(const std::string& name) {
  for (CardType type : allTypes()) {
    if (cardTypeName(type) == name) return type;
  }
  return std::nullopt;
}

bool isCat(CardType type) {
  return type == CardType::Tacocat || type == CardType::Cattermelon ||
         type == CardType::HairyPotatoCat ||
         type == CardType::RainbowRalphingCat || type == CardType::BeardCat;
}

std::string phaseName(Phase phase) {
  switch (phase) {
    case Phase::Lobby: return "LOBBY";
    case Phase::Turn: return "TURN";
    case Phase::Response: return "RESPONSE";
    case Phase::Favor: return "FAVOR";
    case Phase::Kitten: return "KITTEN";
    case Phase::GameOver: return "GAME_OVER";
  }
  return "UNKNOWN";
}

Game::Game(std::vector<std::string> names, std::uint32_t seed) : rng_(seed) {
  if (names.size() < 2 || names.size() > 5) {
    throw std::invalid_argument("player count must be between 2 and 5");
  }
  for (std::size_t i = 0; i < names.size(); ++i) {
    players_.push_back(Player{static_cast<int>(i), std::move(names[i])});
  }
}

void Game::buildDeck() {
  drawPile_.clear();
  discardPile_.clear();
  nextCardId_ = 1;
  for (CardType type : allTypes()) {
    for (int i = 0; i < countFor(type); ++i) {
      drawPile_.push_back(Card{nextCardId_++, type});
    }
  }
}

void Game::setupHands(bool fastMode) {
  std::vector<Card> kittens;
  std::vector<Card> defuses;
  std::vector<Card> regular;
  for (const Card& card : drawPile_) {
    if (card.type == CardType::ExplodingKitten) kittens.push_back(card);
    else if (card.type == CardType::Defuse) defuses.push_back(card);
    else regular.push_back(card);
  }
  drawPile_.clear();
  std::shuffle(regular.begin(), regular.end(), rng_);
  for (Player& player : players_) {
    player.hand.clear();
    player.alive = true;
    player.exploded.reset();
    player.hand.push_back(defuses.back());
    defuses.pop_back();
  }
  // The reference rules return at most two spare Defuses, except a five-player
  // game, which returns only one spare Defuse.
  const std::size_t spare = players_.size() == 5 ? 1 : 2;
  const std::size_t returned = std::min(spare, defuses.size());
  regular.insert(regular.end(), defuses.end() - static_cast<std::ptrdiff_t>(returned),
                 defuses.end());
  std::shuffle(regular.begin(), regular.end(), rng_);
  for (Player& player : players_) {
    for (int i = 0; i < 7; ++i) {
      player.hand.push_back(regular.back());
      regular.pop_back();
    }
  }
  drawPile_ = std::move(regular);
  if (fastMode) {
    shuffleDraw();
    drawPile_.resize(drawPile_.size() - drawPile_.size() / 3);
  }
  kittens.resize(players_.size() - 1);
  drawPile_.insert(drawPile_.end(), kittens.begin(), kittens.end());
  shuffleDraw();
}

void Game::shuffleDraw() { std::shuffle(drawPile_.begin(), drawPile_.end(), rng_); }

int Game::bombCount() const {
  return static_cast<int>(std::count_if(drawPile_.begin(), drawPile_.end(),
      [](const Card& card) { return card.type == CardType::ExplodingKitten; }));
}

ActionResult Game::start(bool fastMode) {
  if (phase_ != Phase::Lobby) return reject("GAME_ALREADY_STARTED");
  if (fastMode && players_.size() > 3) {
    return reject("FAST_MODE_REQUIRES_2_OR_3_PLAYERS");
  }
  buildDeck();
  setupHands(fastMode);
  activePlayer_ = 0;
  turnsRemaining_ = 1;
  activeUnderAttack_ = false;
  phase_ = Phase::Turn;
  fastMode_ = fastMode;
  return accept("GAME_STARTED");
}

bool Game::isAlive(int player) const {
  return player >= 0 && player < static_cast<int>(players_.size()) && players_[player].alive;
}

int Game::aliveCount() const {
  int result = 0;
  for (const Player& player : players_) result += player.alive ? 1 : 0;
  return result;
}

int Game::nextAlive(int from) const {
  for (std::size_t step = 1; step <= players_.size(); ++step) {
    const int candidate = (from + static_cast<int>(step)) % static_cast<int>(players_.size());
    if (isAlive(candidate)) return candidate;
  }
  return from;
}

bool Game::ownsCard(int player, int cardId, std::size_t* index) const {
  if (!isAlive(player)) return false;
  for (std::size_t i = 0; i < players_[player].hand.size(); ++i) {
    if (players_[player].hand[i].id == cardId) {
      if (index != nullptr) *index = i;
      return true;
    }
  }
  return false;
}

Card Game::takeCard(int player, int cardId) {
  std::size_t index = 0;
  if (!ownsCard(player, cardId, &index)) throw std::logic_error("card is not owned");
  Card card = players_[player].hand[index];
  players_[player].hand.erase(players_[player].hand.begin() + static_cast<std::ptrdiff_t>(index));
  return card;
}

void Game::addDiscard(const Card& card) { discardPile_.push_back(card); }

bool Game::allResponded() const {
  for (const Player& player : players_) {
    if (player.alive && player.id != pending_.responseLeader &&
        !pending_.responded.count(player.id)) return false;
  }
  return true;
}

ActionResult Game::playCard(int player, int cardId, int target) {
  if (phase_ != Phase::Turn) return reject("NOT_IN_TURN");
  if (player != activePlayer_) return reject("NOT_YOUR_TURN");
  std::size_t index = 0;
  if (!ownsCard(player, cardId, &index)) return reject("CARD_NOT_IN_HAND");
  const Card card = players_[player].hand[index];
  if (isCat(card.type)) return reject("CAT_CARDS_REQUIRE_COMBO");
  if (card.type == CardType::Defuse || card.type == CardType::ExplodingKitten ||
      card.type == CardType::Nope) {
    return reject("CARD_CANNOT_BE_PLAYED_NOW");
  }
  if (card.type == CardType::Favor && (!isAlive(target) || target == player)) {
    return reject("FAVOR_REQUIRES_OTHER_PLAYER");
  }
  if (card.type != CardType::Favor && target != -1) return reject("UNEXPECTED_TARGET");
  pending_ = Pending{};
  pending_.active = true;
  pending_.type = cardTypeName(card.type);
  pending_.actor = player;
  pending_.target = target;
  pending_.responseLeader = player;
  pending_.cards.push_back(takeCard(player, cardId));
  phase_ = Phase::Response;
  if (allResponded()) return resolvePending();
  return accept("ACTION_WAITING_FOR_RESPONSES");
}

ActionResult Game::playCombo(int player, const std::vector<int>& cardIds, int target,
                             CardType requested) {
  if (phase_ != Phase::Turn) return reject("NOT_IN_TURN");
  if (player != activePlayer_) return reject("NOT_YOUR_TURN");
  if (cardIds.size() != 2 && cardIds.size() != 3) return reject("COMBO_NEEDS_TWO_OR_THREE_CARDS");
  if (!isAlive(target) || target == player) return reject("COMBO_REQUIRES_OTHER_PLAYER");
  if (cardIds.size() == 3 && cardTypeName(requested) == "UNKNOWN") {
    return reject("INVALID_REQUESTED_CARD");
  }
  std::vector<Card> selected;
  for (int id : cardIds) {
    std::size_t index = 0;
    if (!ownsCard(player, id, &index)) return reject("CARD_NOT_IN_HAND");
    const Card card = players_[player].hand[index];
    if (!selected.empty() && card.type != selected.front().type) return reject("COMBO_CARDS_MUST_MATCH");
    if (std::any_of(selected.begin(), selected.end(), [id](const Card& item) { return item.id == id; })) {
      return reject("COMBO_CARD_DUPLICATED");
    }
    selected.push_back(card);
  }
  pending_ = Pending{};
  pending_.active = true;
  pending_.type = cardIds.size() == 2 ? "COMBO2" : "COMBO3";
  pending_.actor = player;
  pending_.target = target;
  pending_.requested = requested;
  pending_.responseLeader = player;
  for (int id : cardIds) pending_.cards.push_back(takeCard(player, id));
  phase_ = Phase::Response;
  if (allResponded()) return resolvePending();
  return accept("COMBO_WAITING_FOR_RESPONSES");
}

ActionResult Game::draw(int player) {
  if (phase_ != Phase::Turn) return reject("NOT_IN_TURN");
  if (player != activePlayer_) return reject("NOT_YOUR_TURN");
  if (drawPile_.empty()) return reject("DRAW_PILE_EMPTY");
  const Card card = drawPile_.back();
  drawPile_.pop_back();
  if (card.type == CardType::ExplodingKitten) {
    kittenPlayer_ = player;
    kitten_ = card;
    players_[player].exploded = card;
    const bool hasDefuse = std::any_of(
        players_[player].hand.begin(), players_[player].hand.end(),
        [](const Card& item) { return item.type == CardType::Defuse; });
    if (!hasDefuse) {
      eliminate(player, card);
      kittenPlayer_ = -1;
      return accept("PLAYER_EXPLODED");
    }
    phase_ = Phase::Kitten;
    return accept("EXPLODING_KITTEN_DRAWN");
  }
  players_[player].hand.push_back(card);
  finishOneTurn();
  return accept("CARD_DRAWN");
}

void Game::finishOneTurn() {
  --turnsRemaining_;
  if (turnsRemaining_ > 0) return;
  moveToNextPlayer();
}

void Game::moveToNextPlayer() {
  if (aliveCount() <= 1) {
    phase_ = Phase::GameOver;
    for (const Player& player : players_) if (player.alive) winner_ = player.id;
    return;
  }
  activePlayer_ = nextAlive(activePlayer_);
  turnsRemaining_ = 1;
  activeUnderAttack_ = false;
  phase_ = Phase::Turn;
}

void Game::eliminate(int player, const Card& kitten) {
  addDiscard(kitten);
  players_[player].alive = false;
  if (aliveCount() <= 1) {
    phase_ = Phase::GameOver;
    for (const Player& item : players_) if (item.alive) winner_ = item.id;
  } else {
    moveToNextPlayer();
  }
}

ActionResult Game::respondNope(int player, int cardId) {
  if (phase_ != Phase::Response || !pending_.active) return reject("NO_RESPONSE_WINDOW");
  if (!isAlive(player) || player == pending_.responseLeader) return reject("CANNOT_RESPOND");
  std::size_t index = 0;
  if (!ownsCard(player, cardId, &index) || players_[player].hand[index].type != CardType::Nope) {
    return reject("NOPE_CARD_REQUIRED");
  }
  addDiscard(takeCard(player, cardId));
  pending_.responded.clear();
  pending_.responseLeader = player;
  ++pending_.nopeCount;
  return accept("NOPE_PLAYED");
}

ActionResult Game::respondPass(int player) {
  if (phase_ != Phase::Response || !pending_.active) return reject("NO_RESPONSE_WINDOW");
  if (!isAlive(player)) return reject("CANNOT_RESPOND");
  if (player == pending_.responseLeader) return reject("ALREADY_RESPONDED");
  pending_.responded.insert(player);
  if (allResponded()) return resolvePending();
  return accept("PASSED_RESPONSE");
}

ActionResult Game::resolvePending() {
  if (!pending_.active) return reject("NO_PENDING_ACTION");
  Pending action = std::move(pending_);
  pending_ = Pending{};
  for (const Card& card : action.cards) addDiscard(card);
  if (action.nopeCount % 2 == 1) {
    phase_ = Phase::Turn;
    return accept("ACTION_CANCELLED_BY_NOPE");
  }
  if (action.type == "ATTACK") {
    const int nextTurns = activeUnderAttack_ ? turnsRemaining_ + 2 : 2;
    activePlayer_ = nextAlive(action.actor);
    turnsRemaining_ = nextTurns;
    activeUnderAttack_ = true;
    phase_ = Phase::Turn;
    return accept("ATTACK_RESOLVED");
  }
  if (action.type == "SKIP") {
    phase_ = Phase::Turn;
    finishOneTurn();
    return accept("SKIP_RESOLVED");
  }
  if (action.type == "SEE_FUTURE") {
    ActionResult result = accept("SEE_FUTURE_RESOLVED");
    result.privatePlayer = action.actor;
    for (std::size_t i = 0; i < std::min<std::size_t>(3, drawPile_.size()); ++i) {
      result.privateCards.push_back(drawPile_[drawPile_.size() - 1 - i]);
    }
    phase_ = Phase::Turn;
    return result;
  }
  if (action.type == "SHUFFLE") {
    shuffleDraw();
    phase_ = Phase::Turn;
    return accept("DRAW_PILE_SHUFFLED");
  }
  if (action.type == "FAVOR") {
    favorActor_ = action.actor;
    favorTarget_ = action.target;
    if (players_[favorTarget_].hand.empty()) {
      favorActor_ = favorTarget_ = -1;
      phase_ = Phase::Turn;
      return accept("FAVOR_TARGET_HAS_NO_CARDS");
    }
    phase_ = Phase::Favor;
    return accept("FAVOR_TARGET_MUST_CHOOSE");
  }
  if (action.type == "COMBO2") {
    if (!players_[action.target].hand.empty()) {
      std::uniform_int_distribution<std::size_t> dist(0, players_[action.target].hand.size() - 1);
      const std::size_t index = dist(rng_);
      players_[action.actor].hand.push_back(players_[action.target].hand[index]);
      players_[action.target].hand.erase(players_[action.target].hand.begin() + static_cast<std::ptrdiff_t>(index));
    }
    phase_ = Phase::Turn;
    return accept("TWO_OF_A_KIND_RESOLVED");
  }
  if (action.type == "COMBO3") {
    auto found = std::find_if(players_[action.target].hand.begin(), players_[action.target].hand.end(),
                              [&action](const Card& item) { return item.type == action.requested; });
    if (found != players_[action.target].hand.end()) {
      players_[action.actor].hand.push_back(*found);
      players_[action.target].hand.erase(found);
    }
    phase_ = Phase::Turn;
    return accept("THREE_OF_A_KIND_RESOLVED");
  }
  phase_ = Phase::Turn;
  return accept("CARD_RESOLVED");
}

ActionResult Game::chooseFavor(int player, int cardId) {
  if (phase_ != Phase::Favor || player != favorTarget_) return reject("NOT_FAVOR_TARGET");
  if (!ownsCard(player, cardId)) return reject("CARD_NOT_IN_HAND");
  players_[favorActor_].hand.push_back(takeCard(player, cardId));
  favorActor_ = favorTarget_ = -1;
  phase_ = Phase::Turn;
  return accept("FAVOR_RESOLVED");
}

ActionResult Game::chooseDefusePosition(int player, std::size_t positionFromTop) {
  if (phase_ != Phase::Kitten || player != kittenPlayer_) return reject("NO_KITTEN_PROMPT");
  if (positionFromTop > drawPile_.size()) return reject("INVALID_KITTEN_POSITION");
  auto defuse = std::find_if(players_[player].hand.begin(), players_[player].hand.end(),
                             [](const Card& item) { return item.type == CardType::Defuse; });
  if (defuse == players_[player].hand.end()) return reject("DEFUSE_NOT_AVAILABLE");
  addDiscard(*defuse);
  players_[player].hand.erase(defuse);
  const auto insertAt = drawPile_.end() - static_cast<std::ptrdiff_t>(positionFromTop);
  drawPile_.insert(insertAt, kitten_);
  players_[player].exploded.reset();
  kittenPlayer_ = -1;
  phase_ = Phase::Turn;
  finishOneTurn();
  return accept("KITTEN_DEFUSED");
}

ActionResult Game::declineDefuse(int player) {
  if (phase_ != Phase::Kitten || player != kittenPlayer_) return reject("NO_KITTEN_PROMPT");
  if (std::any_of(players_[player].hand.begin(), players_[player].hand.end(),
                  [](const Card& card) { return card.type == CardType::Defuse; })) {
    return reject("DEFUSE_REQUIRED");
  }
  eliminate(player, kitten_);
  kittenPlayer_ = -1;
  return accept("PLAYER_EXPLODED");
}

ActionResult Game::forfeit(int player) {
  if (phase_ == Phase::Lobby || phase_ == Phase::GameOver) return reject("NO_ACTIVE_GAME");
  if (!isAlive(player)) return reject("PLAYER_ALREADY_OUT");
  if (phase_ == Phase::Response) {
    if (player == pending_.actor) {
      for (const Card& card : pending_.cards) addDiscard(card);
      pending_ = Pending{};
      players_[player].alive = false;
      if (aliveCount() <= 1) {
        phase_ = Phase::GameOver;
        for (const Player& item : players_) if (item.alive) winner_ = item.id;
      } else {
        activePlayer_ = player;
        moveToNextPlayer();
      }
      return accept("DISCONNECTED_ACTION_CANCELLED");
    }
    players_[player].alive = false;
    if (player == pending_.responseLeader) {
      pending_.responseLeader = pending_.actor;
      pending_.responded.erase(pending_.actor);
    } else {
      pending_.responded.insert(player);
    }
    if (aliveCount() <= 1) {
      phase_ = Phase::GameOver;
      for (const Player& item : players_) if (item.alive) winner_ = item.id;
      return accept("DISCONNECTED_PLAYER_FORFEITED");
    }
    if (allResponded()) return resolvePending();
    return accept("DISCONNECTED_PLAYER_FORFEITED");
  }
  if (phase_ == Phase::Kitten && player == kittenPlayer_) {
    eliminate(player, kitten_);
    kittenPlayer_ = -1;
    return accept("DISCONNECTED_PLAYER_EXPLODED");
  }
  const bool ownedTurn = player == activePlayer_;
  players_[player].alive = false;
  if (phase_ == Phase::Favor && (player == favorActor_ || player == favorTarget_)) {
    favorActor_ = favorTarget_ = -1;
    phase_ = Phase::Turn;
  }
  if (aliveCount() <= 1) {
    phase_ = Phase::GameOver;
    for (const Player& item : players_) if (item.alive) winner_ = item.id;
  } else if (ownedTurn) {
    activePlayer_ = player;
    moveToNextPlayer();
  }
  return accept("DISCONNECTED_PLAYER_FORFEITED");
}

ActionResult Game::reject(const std::string& message) const { return ActionResult{false, message}; }
ActionResult Game::accept(const std::string& message) const { return ActionResult{true, message}; }

}  // namespace ek

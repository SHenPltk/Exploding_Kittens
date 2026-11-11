#include "exploding_kittens/game.hpp"

#include <algorithm>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <memory>
#include <set>
#include <sstream>
#include <stdexcept>

using namespace ek;

#define CHECK(condition) do { if (!(condition)) { \
  std::cerr << "CHECK failed at " << __FILE__ << ':' << __LINE__ << ": " #condition << '\n'; \
  std::exit(1); \
} } while (false)

std::unique_ptr<Game> findGame(const std::function<bool(const Game&)>& predicate,
                               int playerCount = 2, bool fastMode = false) {
  std::vector<std::string> names;
  for (int i = 0; i < playerCount; ++i) names.push_back("P" + std::to_string(i));
  for (std::uint32_t seed = 1; seed < 10000; ++seed) {
    auto game = std::make_unique<Game>(names, seed);
    CHECK(game->start(fastMode).ok);
    if (predicate(*game)) return game;
  }
  throw std::runtime_error("could not find a deterministic test deal");
}

int findCard(const Game& game, int player, CardType type) {
  for (const Card& card : game.hand(player)) {
    if (card.type == type) return card.id;
  }
  return -1;
}

std::vector<Card> findCards(const Game& game, int player, CardType type) {
  std::vector<Card> cards;
  for (const Card& card : game.hand(player)) {
    if (card.type == type) cards.push_back(card);
  }
  return cards;
}

void checkCards(const std::vector<Card>& actual, const std::vector<Card>& expected) {
  CHECK(actual.size() == expected.size());
  for (std::size_t i = 0; i < actual.size(); ++i) {
    CHECK(actual[i].id == expected[i].id);
    CHECK(actual[i].type == expected[i].type);
  }
}

std::string publicState(const Game& game) {
  std::ostringstream state;
  state << phaseName(game.phase()) << ':' << game.fastMode() << ':'
        << game.activePlayer() << ':'
        << game.turnsRemaining() << ':' << game.winner() << ':'
        << game.drawCount() << ':' << game.bombCount() << ':' << game.discardCount() << ':'
        << game.pendingType() << ':' << game.pendingActor() << ':' << game.pendingTarget()
        << ':' << game.pendingNopeCount() << ':' << game.responseLeader();
  const auto appendCards = [&state](const std::vector<Card>& cards) {
    state << '[';
    for (const Card& card : cards) state << card.id << ':' << cardTypeName(card.type) << ',';
    state << ']';
  };
  appendCards(game.drawPile());
  appendCards(game.discardPile());
  appendCards(game.pendingCards());
  for (const Player& player : game.players()) {
    state << ':' << player.id << ':' << player.name << ':' << player.alive << ':';
    if (player.exploded) state << player.exploded->id << ':' << cardTypeName(player.exploded->type);
    appendCards(player.hand);
  }
  return state.str();
}

void checkPending(const Game& game, const std::string& type, int actor, int target,
                  const std::vector<Card>& cards, int nopeCount, int responseLeader) {
  CHECK(game.phase() == Phase::Response);
  CHECK(game.pendingType() == type);
  CHECK(game.pendingActor() == actor);
  CHECK(game.pendingTarget() == target);
  CHECK(game.pendingNopeCount() == nopeCount);
  CHECK(game.responseLeader() == responseLeader);
  checkCards(game.pendingCards(), cards);
}

void checkPendingCleared(const Game& game) {
  CHECK(game.pendingCards().empty());
  CHECK(game.pendingType().empty());
  CHECK(game.pendingActor() == -1);
  CHECK(game.pendingTarget() == -1);
  CHECK(game.pendingNopeCount() == 0);
  CHECK(game.responseLeader() == -1);
}

void checkBombCount(const Game& game) {
  const auto actual = std::count_if(game.drawPile().begin(), game.drawPile().end(),
      [](const Card& card) { return card.type == CardType::ExplodingKitten; });
  CHECK(game.bombCount() == actual);
  CHECK(game.bombCount() >= 0);
  CHECK(game.bombCount() <= game.drawCount());
}

void testSetup() {
  const int expected[] = {0, 0, 35, 29, 23, 16};
  for (int count = 2; count <= 5; ++count) {
    std::vector<std::string> names;
    for (int i = 0; i < count; ++i) names.push_back("P" + std::to_string(i));
    Game game(std::move(names), static_cast<std::uint32_t>(count));
    CHECK(game.bombCount() == 0);
    checkBombCount(game);
    CHECK(game.start().ok);
    CHECK(game.drawCount() == expected[count]);
    CHECK(game.bombCount() == count - 1);
    checkBombCount(game);
    checkPendingCleared(game);
    std::set<int> ids;
    int totalDefuses = 0;
    for (const Player& player : game.players()) {
      CHECK(player.hand.size() == 8);
      int defuses = 0;
      for (const Card& card : player.hand) {
        CHECK(ids.insert(card.id).second);
        CHECK(card.type != CardType::ExplodingKitten);
        defuses += card.type == CardType::Defuse;
      }
      // Spare Defuses are shuffled back before the other seven cards are dealt.
      CHECK(defuses >= 1);
      totalDefuses += defuses;
    }
    int kittens = 0;
    for (const Card& card : game.drawPile()) {
      CHECK(ids.insert(card.id).second);
      kittens += card.type == CardType::ExplodingKitten;
      totalDefuses += card.type == CardType::Defuse;
    }
    CHECK(kittens == count - 1);
    CHECK(totalDefuses == (count == 5 ? 6 : count + 2));
    CHECK(ids.size() == static_cast<std::size_t>(expected[count] + count * 8));
  }
}

void testFastSetup() {
  const int expectedStandard[] = {0, 0, 35, 29};
  const int expectedFast[] = {0, 0, 24, 20};
  for (int count : {2, 3}) {
    std::vector<std::string> names;
    for (int i = 0; i < count; ++i) names.push_back("P" + std::to_string(i));
    std::set<std::set<int>> removedSets;
    bool dealtExtraDefuse = false;
    bool removedSpareDefuse = false;
    bool keptSpareDefuse = false;
    for (std::uint32_t seed = 1; seed <= 64; ++seed) {
      Game standard(names, seed);
      Game explicitStandard(names, seed);
      Game fast(names, seed);
      Game repeated(names, seed);
      CHECK(!standard.fastMode());
      CHECK(!fast.fastMode());
      CHECK(standard.start().ok);
      CHECK(explicitStandard.start(false).ok);
      CHECK(publicState(standard) == publicState(explicitStandard));
      const ActionResult started = fast.start(true);
      CHECK(started.ok);
      CHECK(started.message == "GAME_STARTED");
      CHECK(started.privatePlayer == -1);
      CHECK(started.privateCards.empty());
      CHECK(repeated.start(true).ok);
      CHECK(publicState(fast) == publicState(repeated));
      CHECK(!standard.fastMode());
      CHECK(fast.fastMode());
      CHECK(fast.phase() == Phase::Turn);
      CHECK(fast.activePlayer() == 0);
      CHECK(fast.turnsRemaining() == 1);
      CHECK(fast.winner() == -1);
      CHECK(standard.drawCount() == expectedStandard[count]);
      CHECK(fast.drawCount() == expectedFast[count]);
      CHECK(standard.bombCount() == count - 1);
      CHECK(fast.bombCount() == count - 1);
      CHECK(fast.discardPile().empty());
      checkBombCount(fast);
      checkPendingCleared(fast);

      std::set<int> handIds;
      std::set<int> retainedIds;
      for (const Player& player : fast.players()) {
        CHECK(player.alive);
        CHECK(!player.exploded.has_value());
        CHECK(player.hand.size() == 8);
        checkCards(player.hand, standard.hand(player.id));
        const auto defuses = findCards(fast, player.id, CardType::Defuse).size();
        CHECK(defuses >= 1);
        dealtExtraDefuse = dealtExtraDefuse || defuses > 1;
        for (const Card& card : player.hand) {
          CHECK(card.type != CardType::ExplodingKitten);
          CHECK(handIds.insert(card.id).second);
          CHECK(retainedIds.insert(card.id).second);
        }
      }

      std::set<int> standardDrawIds;
      std::set<int> standardKittens;
      int totalStandardDefuses = 0;
      for (const Player& player : standard.players()) {
        totalStandardDefuses += static_cast<int>(
            findCards(standard, player.id, CardType::Defuse).size());
      }
      for (const Card& card : standard.drawPile()) {
        CHECK(standardDrawIds.insert(card.id).second);
        CHECK(handIds.count(card.id) == 0);
        if (card.type == CardType::ExplodingKitten) standardKittens.insert(card.id);
        totalStandardDefuses += card.type == CardType::Defuse;
      }
      CHECK(totalStandardDefuses == count + 2);

      std::set<int> fastDrawIds;
      std::set<int> fastKittens;
      for (const Card& card : fast.drawPile()) {
        CHECK(retainedIds.insert(card.id).second);
        CHECK(fastDrawIds.insert(card.id).second);
        CHECK(std::any_of(standard.drawPile().begin(), standard.drawPile().end(),
            [&card](const Card& original) {
              return original.id == card.id && original.type == card.type;
            }));
        if (card.type == CardType::ExplodingKitten) fastKittens.insert(card.id);
        keptSpareDefuse = keptSpareDefuse || card.type == CardType::Defuse;
      }
      CHECK(fastKittens == standardKittens);
      CHECK(retainedIds.size() == static_cast<std::size_t>(count * 8 + expectedFast[count]));

      std::set<int> removed;
      for (const Card& card : standard.drawPile()) {
        if (fastDrawIds.count(card.id)) continue;
        CHECK(card.type != CardType::ExplodingKitten);
        CHECK(retainedIds.count(card.id) == 0);
        CHECK(handIds.count(card.id) == 0);
        CHECK(std::none_of(fast.discardPile().begin(), fast.discardPile().end(),
            [&card](const Card& discarded) { return discarded.id == card.id; }));
        CHECK(removed.insert(card.id).second);
        removedSpareDefuse = removedSpareDefuse || card.type == CardType::Defuse;
      }
      const std::size_t remainingNonKittens =
          standard.drawPile().size() - standardKittens.size();
      CHECK(remainingNonKittens == static_cast<std::size_t>(count == 2 ? 34 : 27));
      CHECK(removed.size() == remainingNonKittens / 3);
      CHECK(removed.size() == static_cast<std::size_t>(count == 2 ? 11 : 9));
      removedSets.insert(removed);
    }
    CHECK(removedSets.size() > 1);
    CHECK(dealtExtraDefuse);
    CHECK(removedSpareDefuse);
    CHECK(keptSpareDefuse);
  }
}

void testFastModeStartValidation() {
  for (int count : {4, 5}) {
    std::vector<std::string> names;
    for (int i = 0; i < count; ++i) names.push_back("P" + std::to_string(i));
    Game game(names, 73);
    Game fresh(names, 73);
    const std::string lobbyState = publicState(game);
    for (int attempt = 0; attempt < 2; ++attempt) {
      const ActionResult rejected = game.start(true);
      CHECK(!rejected.ok);
      CHECK(rejected.message == "FAST_MODE_REQUIRES_2_OR_3_PLAYERS");
      CHECK(rejected.privatePlayer == -1);
      CHECK(rejected.privateCards.empty());
      CHECK(!game.fastMode());
      CHECK(publicState(game) == lobbyState);
    }
    CHECK(game.start().ok);
    CHECK(fresh.start().ok);
    // A rejected fast start must not consume RNG or change the subsequent deal.
    CHECK(publicState(game) == publicState(fresh));
    const std::string startedState = publicState(game);
    const ActionResult repeated = game.start(true);
    CHECK(!repeated.ok);
    CHECK(repeated.message == "GAME_ALREADY_STARTED");
    CHECK(publicState(game) == startedState);
  }

  for (int count : {2, 3}) {
    for (bool fastMode : {false, true}) {
      std::vector<std::string> names;
      for (int i = 0; i < count; ++i) names.push_back("P" + std::to_string(i));
      Game game(names, 91);
      CHECK(game.start(fastMode).ok);
      const std::string before = publicState(game);
      for (bool requestedMode : {false, true}) {
        const ActionResult repeated = game.start(requestedMode);
        CHECK(!repeated.ok);
        CHECK(repeated.message == "GAME_ALREADY_STARTED");
        CHECK(game.fastMode() == fastMode);
        CHECK(publicState(game) == before);
      }
    }
  }
}

void testSetupCanDealExtraDefuses() {
  auto game = findGame([](const Game& candidate) {
    for (const Player& player : candidate.players()) {
      const auto defuses = std::count_if(player.hand.begin(), player.hand.end(),
          [](const Card& card) { return card.type == CardType::Defuse; });
      if (defuses > 1) return true;
    }
    return false;
  });
  CHECK(game->drawCount() == 35);
  CHECK(game->players().size() == 2);
  for (const Player& player : game->players()) CHECK(player.hand.size() == 8);
}

void testCatCardsRequireCombo() {
  for (CardType type : {CardType::Tacocat, CardType::Cattermelon, CardType::HairyPotatoCat,
                        CardType::RainbowRalphingCat, CardType::BeardCat}) {
    auto game = findGame([type](const Game& candidate) {
      return findCard(candidate, candidate.activePlayer(), type) >= 0;
    });
    const int actor = game->activePlayer();
    const int cardId = findCard(*game, actor, type);
    const std::string before = publicState(*game);
    for (int target : {-1, 1 - actor}) {
      const ActionResult result = game->playCard(actor, cardId, target);
      CHECK(!result.ok);
      CHECK(result.message == "CAT_CARDS_REQUIRE_COMBO");
      CHECK(publicState(*game) == before);
    }
  }
}

void testCatCombosAndPendingNopeChains() {
  for (CardType type : {CardType::Tacocat, CardType::Cattermelon, CardType::HairyPotatoCat,
                        CardType::RainbowRalphingCat, CardType::BeardCat}) {
    for (std::size_t count : {2U, 3U}) {
      for (bool cancelled : {false, true}) {
        auto game = findGame([type, count](const Game& candidate) {
          const int actor = candidate.activePlayer();
          return findCards(candidate, actor, type).size() >= count &&
                 findCard(candidate, actor, CardType::Nope) >= 0 &&
                 findCard(candidate, 1 - actor, CardType::Nope) >= 0;
        });
        const int actor = game->activePlayer();
        const int other = 1 - actor;
        auto cards = findCards(*game, actor, type);
        cards.resize(count);
        std::vector<int> ids;
        for (const Card& card : cards) ids.push_back(card.id);
        const std::string actionType = count == 2 ? "COMBO2" : "COMBO3";
        const std::size_t actorHand = game->hand(actor).size();
        const std::size_t otherHand = game->hand(other).size();
        const int requestedCard = findCard(*game, other, CardType::Defuse);
        const int drawCount = game->drawCount();
        const int bombCount = game->bombCount();
        CHECK(game->playCombo(actor, ids, other, CardType::Defuse).ok);
        checkPending(*game, actionType, actor, other, cards, 0, actor);
        CHECK(game->hand(actor).size() == actorHand - count);
        CHECK(game->discardCount() == 0);
        CHECK(game->respondNope(other, findCard(*game, other, CardType::Nope)).ok);
        checkPending(*game, actionType, actor, other, cards, 1, other);
        if (!cancelled) {
          CHECK(game->respondNope(actor, findCard(*game, actor, CardType::Nope)).ok);
          checkPending(*game, actionType, actor, other, cards, 2, actor);
        }
        const ActionResult result = game->respondPass(cancelled ? actor : other);
        CHECK(result.ok);
        CHECK(result.message == (cancelled ? "ACTION_CANCELLED_BY_NOPE" :
              count == 2 ? "TWO_OF_A_KIND_RESOLVED" : "THREE_OF_A_KIND_RESOLVED"));
        CHECK(game->phase() == Phase::Turn);
        CHECK(game->activePlayer() == actor);
        CHECK(game->turnsRemaining() == 1);
        checkPendingCleared(*game);
        CHECK(game->drawCount() == drawCount);
        CHECK(game->bombCount() == bombCount);
        CHECK(game->discardCount() == static_cast<int>(count) + (cancelled ? 1 : 2));
        CHECK(game->hand(actor).size() == actorHand - count);
        CHECK(game->hand(other).size() == otherHand - (cancelled ? 1 : 2));
        if (count == 3) {
          CHECK(std::any_of(game->hand(cancelled ? other : actor).begin(),
                            game->hand(cancelled ? other : actor).end(),
              [requestedCard](const Card& card) { return card.id == requestedCard; }));
        }
        const auto& discard = game->discardPile();
        checkCards(std::vector<Card>(discard.end() - static_cast<std::ptrdiff_t>(count),
                                     discard.end()), cards);
      }
    }
  }
}

void testNopeCancelsAttack() {
  auto game = findGame([](const Game& candidate) {
    const int actor = candidate.activePlayer();
    const int other = 1 - actor;
    return findCard(candidate, actor, CardType::Attack) >= 0 &&
           findCard(candidate, other, CardType::Nope) >= 0;
  });
  const int actor = game->activePlayer();
  const int target = 1 - actor;
  CHECK(game->playCard(actor, findCard(*game, actor, CardType::Attack)).ok);
  CHECK(game->respondNope(target, findCard(*game, target, CardType::Nope)).ok);
  CHECK(game->phase() == Phase::Response);
  CHECK(game->respondPass(actor).ok);
  CHECK(game->activePlayer() == actor);
  CHECK(game->phase() == Phase::Turn);
  CHECK(game->turnsRemaining() == 1);
}

void testNopeCanBeNoped() {
  auto game = findGame([](const Game& candidate) {
    const int actor = candidate.activePlayer();
    const int other = 1 - actor;
    return findCard(candidate, actor, CardType::Attack) >= 0 &&
           findCard(candidate, actor, CardType::Nope) >= 0 &&
           findCard(candidate, other, CardType::Nope) >= 0;
  });
  const int actor = game->activePlayer();
  const int other = 1 - actor;
  const std::vector<Card> cards{findCards(*game, actor, CardType::Attack).front()};
  CHECK(game->playCard(actor, cards.front().id).ok);
  checkPending(*game, "ATTACK", actor, -1, cards, 0, actor);
  CHECK(game->respondNope(other, findCard(*game, other, CardType::Nope)).ok);
  checkPending(*game, "ATTACK", actor, -1, cards, 1, other);
  CHECK(game->respondNope(actor, findCard(*game, actor, CardType::Nope)).ok);
  checkPending(*game, "ATTACK", actor, -1, cards, 2, actor);
  CHECK(game->respondPass(other).ok);
  CHECK(game->phase() == Phase::Turn);
  CHECK(game->activePlayer() == other);
  CHECK(game->turnsRemaining() == 2);
  checkPendingCleared(*game);
}

void testAttackAndSkip() {
  auto game = findGame([](const Game& candidate) {
    const int actor = candidate.activePlayer();
    const int victim = 1 - actor;
    return findCard(candidate, actor, CardType::Attack) >= 0 &&
           findCard(candidate, victim, CardType::Skip) >= 0;
  });
  const int actor = game->activePlayer();
  const int victim = 1 - actor;
  CHECK(game->playCard(actor, findCard(*game, actor, CardType::Attack)).ok);
  CHECK(game->respondPass(victim).ok);
  CHECK(game->activePlayer() == victim);
  CHECK(game->turnsRemaining() == 2);
  CHECK(game->playCard(victim, findCard(*game, victim, CardType::Skip)).ok);
  CHECK(game->respondPass(actor).ok);
  CHECK(game->activePlayer() == victim);
  CHECK(game->turnsRemaining() == 1);
  CHECK(game->phase() == Phase::Turn);
  checkPendingCleared(*game);
  CHECK(game->draw(victim).ok);
}

void testBombCountDrawAndRequiredDefuse() {
  for (int playerCount = 2; playerCount <= 5; ++playerCount) {
    auto game = findGame([](const Game& candidate) {
      const auto& pile = candidate.drawPile();
      return pile.back().type != CardType::ExplodingKitten &&
             pile[pile.size() - 2].type == CardType::ExplodingKitten &&
             findCards(candidate, 1, CardType::Defuse).size() > 1;
    }, playerCount);
    const int initialBombs = playerCount - 1;
    const int initialDrawCount = game->drawCount();
    const Card normal = game->drawPile().back();
    const int firstActor = game->activePlayer();
    const ActionResult normalDraw = game->draw(firstActor);
    CHECK(normalDraw.ok);
    CHECK(normalDraw.message == "CARD_DRAWN");
    CHECK(game->drawCount() == initialDrawCount - 1);
    CHECK(game->bombCount() == initialBombs);
    CHECK(game->hand(firstActor).back().id == normal.id);
    checkBombCount(*game);

    const int defuser = game->activePlayer();
    const Card kitten = game->drawPile().back();
    const ActionResult kittenDraw = game->draw(defuser);
    CHECK(kittenDraw.ok);
    CHECK(kittenDraw.message == "EXPLODING_KITTEN_DRAWN");
    CHECK(game->phase() == Phase::Kitten);
    CHECK(game->drawCount() == initialDrawCount - 2);
    CHECK(game->bombCount() == initialBombs - 1);
    CHECK(game->players()[defuser].exploded.has_value());
    CHECK(game->players()[defuser].exploded->id == kitten.id);
    checkBombCount(*game);
    const std::string beforeInvalidPosition = publicState(*game);
    const ActionResult invalidPosition =
        game->chooseDefusePosition(defuser, game->drawPile().size() + 1);
    CHECK(!invalidPosition.ok);
    CHECK(invalidPosition.message == "INVALID_KITTEN_POSITION");
    CHECK(publicState(*game) == beforeInvalidPosition);

    const std::string beforeDecline = publicState(*game);
    for (int other : {-1, firstActor, playerCount}) {
      const ActionResult invalidDecline = game->declineDefuse(other);
      CHECK(!invalidDecline.ok);
      CHECK(invalidDecline.message == "NO_KITTEN_PROMPT");
      CHECK(publicState(*game) == beforeDecline);
    }
    for (int attempt = 0; attempt < 2; ++attempt) {
      const ActionResult declined = game->declineDefuse(defuser);
      CHECK(!declined.ok);
      CHECK(declined.message == "DEFUSE_REQUIRED");
      CHECK(publicState(*game) == beforeDecline);
    }

    const std::size_t defuserHand = game->hand(defuser).size();
    const ActionResult defused = game->chooseDefusePosition(defuser, 0);
    CHECK(defused.ok);
    CHECK(defused.message == "KITTEN_DEFUSED");
    CHECK(game->phase() == Phase::Turn);
    CHECK(game->drawCount() == initialDrawCount - 1);
    CHECK(game->bombCount() == initialBombs);
    CHECK(game->drawPile().back().id == kitten.id);
    CHECK(!game->players()[defuser].exploded.has_value());
    CHECK(game->hand(defuser).size() == defuserHand - 1);
    CHECK(game->discardPile().back().type == CardType::Defuse);
    checkBombCount(*game);

    int availableDefuses = 0;
    for (const Player& player : game->players()) {
      availableDefuses += static_cast<int>(findCards(*game, player.id, CardType::Defuse).size());
    }
    bool exploded = false;
    for (int turn = 0; turn <= availableDefuses; ++turn) {
      const int victim = game->activePlayer();
      const bool hasDefuse = findCard(*game, victim, CardType::Defuse) >= 0;
      const ActionResult result = game->draw(victim);
      CHECK(result.ok);
      CHECK(game->drawCount() == initialDrawCount - 2);
      CHECK(game->bombCount() == initialBombs - 1);
      checkBombCount(*game);
      if (hasDefuse) {
        CHECK(result.message == "EXPLODING_KITTEN_DRAWN");
        CHECK(game->phase() == Phase::Kitten);
        const std::string before = publicState(*game);
        const ActionResult declined = game->declineDefuse(victim);
        CHECK(!declined.ok);
        CHECK(declined.message == "DEFUSE_REQUIRED");
        CHECK(publicState(*game) == before);
        CHECK(game->chooseDefusePosition(victim, 0).ok);
        CHECK(game->phase() == Phase::Turn);
        CHECK(game->drawCount() == initialDrawCount - 1);
        CHECK(game->bombCount() == initialBombs);
        CHECK(game->drawPile().back().id == kitten.id);
        checkBombCount(*game);
        continue;
      }
      CHECK(result.message == "PLAYER_EXPLODED");
      CHECK(!game->players()[victim].alive);
      CHECK(game->players()[victim].exploded.has_value());
      CHECK(game->players()[victim].exploded->id == kitten.id);
      CHECK(game->discardPile().back().id == kitten.id);
      CHECK(game->phase() == (playerCount == 2 ? Phase::GameOver : Phase::Turn));
      exploded = true;
      break;
    }
    CHECK(exploded);
  }
}

void testBombCountAutomaticExplosion() {
  for (int playerCount = 2; playerCount <= 5; ++playerCount) {
    auto game = findGame([](const Game& candidate) {
      if (candidate.drawPile().back().type != CardType::ExplodingKitten) return false;
      for (const Player& player : candidate.players()) {
        if (findCards(candidate, player.id, CardType::Defuse).size() != 1) return false;
      }
      return true;
    }, playerCount);
    const int initialDrawCount = game->drawCount();
    const int firstActor = game->activePlayer();
    const Card kitten = game->drawPile().back();
    for (int turn = 0; turn < playerCount; ++turn) {
      const int actor = game->activePlayer();
      CHECK(game->draw(actor).ok);
      CHECK(game->phase() == Phase::Kitten);
      CHECK(game->drawCount() == initialDrawCount - 1);
      CHECK(game->bombCount() == playerCount - 2);
      checkBombCount(*game);
      CHECK(game->chooseDefusePosition(actor, 0).ok);
      CHECK(game->phase() == Phase::Turn);
      CHECK(game->drawCount() == initialDrawCount);
      CHECK(game->bombCount() == playerCount - 1);
      CHECK(game->drawPile().back().id == kitten.id);
      CHECK(findCard(*game, actor, CardType::Defuse) == -1);
      checkBombCount(*game);
    }
    CHECK(game->activePlayer() == firstActor);
    const ActionResult result = game->draw(firstActor);
    CHECK(result.ok);
    CHECK(result.message == "PLAYER_EXPLODED");
    CHECK(!game->players()[firstActor].alive);
    CHECK(game->players()[firstActor].exploded->id == kitten.id);
    CHECK(game->drawCount() == initialDrawCount - 1);
    CHECK(game->bombCount() == playerCount - 2);
    CHECK(game->discardPile().back().id == kitten.id);
    CHECK(game->phase() == (playerCount == 2 ? Phase::GameOver : Phase::Turn));
    checkBombCount(*game);
  }
}

void testFastGameToWinner() {
  for (int playerCount : {2, 3}) {
    auto game = findGame([](const Game& candidate) {
      return candidate.drawPile().back().type == CardType::ExplodingKitten;
    }, playerCount, true);
    const auto cardIds = [&game]() {
      std::set<int> ids;
      const auto appendCards = [&ids](const std::vector<Card>& cards) {
        for (const Card& card : cards) CHECK(ids.insert(card.id).second);
      };
      appendCards(game->drawPile());
      appendCards(game->discardPile());
      for (const Player& player : game->players()) appendCards(player.hand);
      if (game->phase() == Phase::Kitten) {
        const auto& exploded = game->players()[game->activePlayer()].exploded;
        CHECK(exploded.has_value());
        CHECK(ids.insert(exploded->id).second);
      }
      return ids;
    };
    const std::set<int> initialIds = cardIds();
    int defusesUsed = 0;
    int explosions = 0;
    int actions = 0;
    while (game->phase() != Phase::GameOver && actions++ < 256) {
      CHECK(game->fastMode());
      const int actor = game->activePlayer();
      CHECK(game->players()[actor].alive);
      if (game->phase() == Phase::Turn) {
        CHECK(!game->drawPile().empty());
        const Card top = game->drawPile().back();
        const bool hasDefuse = findCard(*game, actor, CardType::Defuse) >= 0;
        const ActionResult drawn = game->draw(actor);
        CHECK(drawn.ok);
        if (top.type == CardType::ExplodingKitten) {
          if (hasDefuse) {
            CHECK(drawn.message == "EXPLODING_KITTEN_DRAWN");
            CHECK(game->phase() == Phase::Kitten);
          } else {
            CHECK(drawn.message == "PLAYER_EXPLODED");
            CHECK(!game->players()[actor].alive);
            CHECK(game->discardPile().back().id == top.id);
            ++explosions;
          }
        } else {
          CHECK(drawn.message == "CARD_DRAWN");
          CHECK(game->hand(actor).back().id == top.id);
        }
      } else {
        CHECK(game->phase() == Phase::Kitten);
        const int defuseId = findCard(*game, actor, CardType::Defuse);
        CHECK(defuseId >= 0);
        const std::string beforeDecline = publicState(*game);
        const ActionResult declined = game->declineDefuse(actor);
        CHECK(!declined.ok);
        CHECK(declined.message == "DEFUSE_REQUIRED");
        CHECK(publicState(*game) == beforeDecline);
        const ActionResult defused = game->chooseDefusePosition(actor, 0);
        CHECK(defused.ok);
        CHECK(defused.message == "KITTEN_DEFUSED");
        CHECK(game->discardPile().back().id == defuseId);
        CHECK(!game->players()[actor].exploded.has_value());
        ++defusesUsed;
      }
      CHECK(cardIds() == initialIds);
      checkBombCount(*game);
    }
    CHECK(game->phase() == Phase::GameOver);
    CHECK(game->fastMode());
    CHECK(defusesUsed >= playerCount);
    CHECK(explosions == playerCount - 1);
    CHECK(game->bombCount() == 0);
    CHECK(game->winner() >= 0);
    CHECK(game->players()[game->winner()].alive);
    CHECK(std::count_if(game->players().begin(), game->players().end(),
        [](const Player& player) { return player.alive; }) == 1);
    CHECK(std::count_if(game->discardPile().begin(), game->discardPile().end(),
        [](const Card& card) { return card.type == CardType::ExplodingKitten; }) ==
        playerCount - 1);
    const std::string finalState = publicState(*game);
    const ActionResult restarted = game->start(false);
    CHECK(!restarted.ok);
    CHECK(restarted.message == "GAME_ALREADY_STARTED");
    CHECK(publicState(*game) == finalState);
  }
}

void testForfeitWithDefuse() {
  for (int playerCount = 2; playerCount <= 5; ++playerCount) {
    auto game = findGame([](const Game& candidate) {
      return candidate.drawPile().back().type == CardType::ExplodingKitten;
    }, playerCount);
    const int victim = game->activePlayer();
    const Card kitten = game->drawPile().back();
    const std::vector<Card> hand = game->hand(victim);
    CHECK(findCard(*game, victim, CardType::Defuse) >= 0);
    CHECK(game->draw(victim).ok);
    CHECK(game->phase() == Phase::Kitten);
    const std::string beforeDecline = publicState(*game);
    const ActionResult declined = game->declineDefuse(victim);
    CHECK(!declined.ok);
    CHECK(declined.message == "DEFUSE_REQUIRED");
    CHECK(publicState(*game) == beforeDecline);

    const int drawCount = game->drawCount();
    const int bombCount = game->bombCount();
    const int discardCount = game->discardCount();
    const ActionResult result = game->forfeit(victim);
    CHECK(result.ok);
    CHECK(result.message == "DISCONNECTED_PLAYER_EXPLODED");
    CHECK(!game->players()[victim].alive);
    CHECK(game->players()[victim].exploded.has_value());
    CHECK(game->players()[victim].exploded->id == kitten.id);
    checkCards(game->hand(victim), hand);
    CHECK(game->drawCount() == drawCount);
    CHECK(game->bombCount() == bombCount);
    CHECK(game->discardCount() == discardCount + 1);
    CHECK(game->discardPile().back().id == kitten.id);
    CHECK(game->phase() == (playerCount == 2 ? Phase::GameOver : Phase::Turn));
    CHECK(game->winner() == (playerCount == 2 ? 1 : -1));
    if (playerCount > 2) CHECK(game->activePlayer() == 1);
    checkBombCount(*game);
  }
}

void testInvalidActions() {
  Game game({"A", "B"}, 4);
  CHECK(!game.playCard(0, 1).ok);
  const std::string lobbyState = publicState(game);
  const ActionResult lobbyDecline = game.declineDefuse(0);
  CHECK(!lobbyDecline.ok);
  CHECK(lobbyDecline.message == "NO_KITTEN_PROMPT");
  CHECK(publicState(game) == lobbyState);
  CHECK(game.start().ok);
  CHECK(!game.draw(1 - game.activePlayer()).ok);
  const std::string turnState = publicState(game);
  const ActionResult turnDecline = game.declineDefuse(game.activePlayer());
  CHECK(!turnDecline.ok);
  CHECK(turnDecline.message == "NO_KITTEN_PROMPT");
  CHECK(publicState(game) == turnState);
}

int main() {
  testSetup();
  testFastSetup();
  testFastModeStartValidation();
  testFastGameToWinner();
  testSetupCanDealExtraDefuses();
  testCatCardsRequireCombo();
  testCatCombosAndPendingNopeChains();
  testNopeCancelsAttack();
  testNopeCanBeNoped();
  testAttackAndSkip();
  testBombCountDrawAndRequiredDefuse();
  testBombCountAutomaticExplosion();
  testForfeitWithDefuse();
  testInvalidActions();
  std::cout << "game tests passed\n";
}

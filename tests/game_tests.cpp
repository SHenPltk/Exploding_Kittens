#include "exploding_kittens/game.hpp"

#include <algorithm>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <memory>
#include <set>
#include <stdexcept>

using namespace ek;

#define CHECK(condition) do { if (!(condition)) { \
  std::cerr << "CHECK failed at " << __FILE__ << ':' << __LINE__ << ": " #condition << '\n'; \
  std::exit(1); \
} } while (false)

std::unique_ptr<Game> findGame(const std::function<bool(const Game&)>& predicate) {
  for (std::uint32_t seed = 1; seed < 10000; ++seed) {
    auto game = std::make_unique<Game>(std::vector<std::string>{"A", "B"}, seed);
    CHECK(game->start().ok);
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

void testSetup() {
  const int expected[] = {0, 0, 35, 29, 23, 16};
  for (int count = 2; count <= 5; ++count) {
    std::vector<std::string> names;
    for (int i = 0; i < count; ++i) names.push_back("P" + std::to_string(i));
    Game game(std::move(names), static_cast<std::uint32_t>(count));
    CHECK(game.start().ok);
    CHECK(game.drawCount() == expected[count]);
    std::set<int> ids;
    for (const Player& player : game.players()) {
      CHECK(player.hand.size() == 8);
      int defuses = 0;
      for (const Card& card : player.hand) {
        CHECK(ids.insert(card.id).second);
        defuses += card.type == CardType::Defuse;
      }
      CHECK(defuses == 1);
    }
    int kittens = 0;
    for (const Card& card : game.drawPile()) {
      CHECK(ids.insert(card.id).second);
      kittens += card.type == CardType::ExplodingKitten;
    }
    CHECK(kittens == count - 1);
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
  CHECK(game->playCard(actor, findCard(*game, actor, CardType::Attack)).ok);
  CHECK(game->respondNope(other, findCard(*game, other, CardType::Nope)).ok);
  CHECK(game->respondNope(actor, findCard(*game, actor, CardType::Nope)).ok);
  CHECK(game->respondPass(other).ok);
  CHECK(game->activePlayer() == other);
  CHECK(game->turnsRemaining() == 2);
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
}

void testInvalidActions() {
  Game game({"A", "B"}, 4);
  CHECK(!game.playCard(0, 1).ok);
  CHECK(game.start().ok);
  CHECK(!game.draw(1 - game.activePlayer()).ok);
}

int main() {
  testSetup();
  testNopeCancelsAttack();
  testNopeCanBeNoped();
  testAttackAndSkip();
  testInvalidActions();
  std::cout << "game tests passed\n";
}

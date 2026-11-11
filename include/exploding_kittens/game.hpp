#pragma once

#include <cstdint>
#include <optional>
#include <random>
#include <set>
#include <string>
#include <vector>

namespace ek {

enum class CardType { ExplodingKitten, Defuse, Attack, Skip, SeeFuture, Shuffle, Favor, Nope,
                      Tacocat, Cattermelon, HairyPotatoCat, RainbowRalphingCat, BeardCat };
std::string cardTypeName(CardType type);
std::optional<CardType> cardTypeFromName(const std::string& name);
bool isCat(CardType type);

struct Card { int id = 0; CardType type = CardType::Defuse; };
struct Player {
  int id = -1;
  std::string name;
  bool alive = true;
  std::vector<Card> hand;
  std::optional<Card> exploded;
};
enum class Phase { Lobby, Turn, Response, Favor, Kitten, GameOver };
std::string phaseName(Phase phase);
struct ActionResult {
  bool ok = false;
  std::string message;
  int privatePlayer = -1;
  std::vector<Card> privateCards;
};

class Game {
 public:
  Game(std::vector<std::string> names, std::uint32_t seed = 1);
  ActionResult start();
  ActionResult playCard(int player, int cardId, int target = -1);
  ActionResult playCombo(int player, const std::vector<int>& cardIds, int target = -1,
                         CardType requested = CardType::Defuse);
  ActionResult draw(int player);
  ActionResult respondNope(int player, int cardId);
  ActionResult respondPass(int player);
  ActionResult chooseFavor(int player, int cardId);
  ActionResult chooseDefusePosition(int player, std::size_t positionFromTop);
  ActionResult declineDefuse(int player);
  ActionResult forfeit(int player);

  Phase phase() const { return phase_; }
  int activePlayer() const { return activePlayer_; }
  int turnsRemaining() const { return turnsRemaining_; }
  int winner() const { return winner_; }
  int drawCount() const { return static_cast<int>(drawPile_.size()); }
  int bombCount() const;
  int discardCount() const { return static_cast<int>(discardPile_.size()); }
  const std::vector<Player>& players() const { return players_; }
  const std::vector<Card>& drawPile() const { return drawPile_; }
  const std::vector<Card>& discardPile() const { return discardPile_; }
  const std::string& pendingType() const { return pending_.type; }
  const std::vector<Card>& pendingCards() const { return pending_.cards; }
  int pendingActor() const { return pending_.actor; }
  int pendingTarget() const { return phase_ == Phase::Favor ? favorTarget_ : pending_.target; }
  int pendingNopeCount() const { return pending_.nopeCount; }
  int responseLeader() const { return phase_ == Phase::Response ? pending_.responseLeader : -1; }
  const std::vector<Card>& hand(int player) const { return players_.at(player).hand; }

 private:
  struct Pending {
    bool active = false;
    std::string type;
    int actor = -1;
    int target = -1;
    CardType requested = CardType::Defuse;
    std::vector<Card> cards;
    std::set<int> responded;
    int responseLeader = -1;
    int nopeCount = 0;
  };
  std::vector<Player> players_;
  std::vector<Card> drawPile_, discardPile_;
  std::mt19937 rng_;
  Phase phase_ = Phase::Lobby;
  Pending pending_;
  int activePlayer_ = 0, turnsRemaining_ = 1, winner_ = -1, nextCardId_ = 1;
  bool activeUnderAttack_ = false;
  int kittenPlayer_ = -1, favorActor_ = -1, favorTarget_ = -1;
  Card kitten_;

  void buildDeck();
  void setupHands();
  void shuffleDraw();
  bool isAlive(int player) const;
  int aliveCount() const;
  int nextAlive(int from) const;
  bool allResponded() const;
  bool ownsCard(int player, int cardId, std::size_t* index = nullptr) const;
  Card takeCard(int player, int cardId);
  void addDiscard(const Card& card);
  void finishOneTurn();
  void moveToNextPlayer();
  void eliminate(int player, const Card& kitten);
  ActionResult resolvePending();
  ActionResult reject(const std::string& message) const;
  ActionResult accept(const std::string& message) const;
};

}  // namespace ek

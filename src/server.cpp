#include "exploding_kittens/game.hpp"
#include "admin_auth.hpp"
#include "client_download.hpp"

#include <algorithm>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>
#include <optional>
#include <random>
#include <chrono>
#include <filesystem>
#include <map>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <shellapi.h>
using Socket = SOCKET;
constexpr Socket kInvalidSocket = INVALID_SOCKET;
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
using Socket = int;
constexpr Socket kInvalidSocket = -1;
#endif

namespace {
struct Client {
  Socket socket = kInvalidSocket;
  int player = -1;
  std::string name;
  std::mutex writeMutex;
  std::atomic<bool> connected{true};
  bool isHost = false;
  std::uint64_t authGeneration = 0;
};

std::mutex stateMutex;
std::vector<std::shared_ptr<Client>> clients;
std::unique_ptr<ek::Game> game;
std::uint64_t sequence = 0;
std::uint64_t roundNumber = 0;
bool nextFastMode = false;
bool nextFreeAttackMode = false;
std::vector<std::shared_ptr<Client>> administrators;
std::unique_ptr<ek::AdminAuth> adminAuth;
std::atomic<int> pendingHandshakes{0};
std::mutex authRateMutex;
struct AuthRate { int failures = 0; std::chrono::steady_clock::time_point reset; };
std::map<std::uint32_t, AuthRate> authFailures;
AuthRate globalAuthFailures;

void closeSocket(Socket socket) {
#ifdef _WIN32
  closesocket(socket);
#else
  close(socket);
#endif
}

bool sendLine(const std::shared_ptr<Client>& client, const std::string& line) {
  std::lock_guard<std::mutex> lock(client->writeMutex);
  const std::string payload = line + "\n";
  std::size_t sent = 0;
  while (sent < payload.size()) {
#ifdef _WIN32
    const int result = send(client->socket, payload.data() + sent,
                            static_cast<int>(payload.size() - sent), 0);
#else
    const ssize_t result = send(client->socket, payload.data() + sent,
                                payload.size() - sent, MSG_NOSIGNAL);
#endif
    if (result <= 0) return false;
    sent += static_cast<std::size_t>(result);
  }
  return true;
}

bool receiveLine(Socket socket, std::string& output, int deadlineSeconds = 0) {
  output.clear();
  char character = 0;
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(deadlineSeconds);
  while (output.size() < 8192) {
    if (deadlineSeconds && std::chrono::steady_clock::now() >= deadline) return false;
#ifdef _WIN32
    const int result = recv(socket, &character, 1, 0);
#else
    const ssize_t result = recv(socket, &character, 1, 0);
#endif
    if (result <= 0) return false;
    if (character == '\n') {
      if (!output.empty() && output.back() == '\r') output.pop_back();
      return true;
    }
    output.push_back(character);
  }
  return false;
}

std::vector<std::string> split(const std::string& line) {
  std::vector<std::string> result;
  std::stringstream stream(line);
  std::string item;
  while (std::getline(stream, item, '\t')) result.push_back(item);
  if (!line.empty() && line.back() == '\t') result.emplace_back();
  return result;
}

std::string sanitize(std::string value) {
  std::replace(value.begin(), value.end(), '\t', ' ');
  std::replace(value.begin(), value.end(), '\n', ' ');
  std::replace(value.begin(), value.end(), '\r', ' ');
  std::replace(value.begin(), value.end(), '\0', ' ');
  return value;
}

std::string normalizeName(std::string name) {
  const auto first = name.find_first_not_of(" \t\r\n");
  if (first == std::string::npos) return {};
  const auto last = name.find_last_not_of(" \t\r\n");
  return name.substr(first, last - first + 1);
}

int hostPlayerLocked() {
  for (const auto& client : clients) {
    if (client->connected && client->isHost) return client->player;
  }
  return -1;
}

void ensureHostLocked() {
  if (hostPlayerLocked() >= 0) return;
  for (const auto& client : clients) client->isHost = false;
  for (const auto& client : clients) {
    if (client->connected) { client->isHost = true; break; }
  }
}

std::shared_ptr<Client> onlinePlayerLocked(int player) {
  for (const auto& client : clients) {
    if (client->connected && client->player == player) return client;
  }
  return nullptr;
}

int parseInteger(const std::string& text) {
  std::size_t consumed = 0;
  const int result = std::stoi(text, &consumed);
  if (consumed != text.size()) throw std::invalid_argument("integer");
  return result;
}

void shutdownSocket(Socket socket) {
#ifdef _WIN32
  shutdown(socket, SD_BOTH);
#else
  shutdown(socket, SHUT_RDWR);
#endif
}

void setSocketTimeouts(Socket socket, int receiveSeconds, int sendSeconds = 3) {
#ifdef _WIN32
  const DWORD receive = static_cast<DWORD>(receiveSeconds * 1000);
  const DWORD sendTimeout = static_cast<DWORD>(sendSeconds * 1000);
  setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&receive), sizeof(receive));
  setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&sendTimeout), sizeof(sendTimeout));
#else
  const timeval receive{receiveSeconds, 0}, sendTimeout{sendSeconds, 0};
  setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, &receive, sizeof(receive));
  setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, &sendTimeout, sizeof(sendTimeout));
#endif
}

int connectedCountLocked() {
  return static_cast<int>(std::count_if(clients.begin(), clients.end(),
      [](const auto& client) { return client->connected.load(); }));
}

void sendOptionsLocked(const std::shared_ptr<Client>& client) {
  sendLine(client, std::string("OPTIONS\t") + (nextFastMode ? "1" : "0") + "\t" +
                       (nextFreeAttackMode ? "1" : "0"));
}

void sendRosterLocked() {
  if (connectedCountLocked() > 3) nextFastMode = false;
  std::ostringstream roster;
  roster << "ROSTER";
  for (const auto& client : clients) {
    if (client->connected) roster << '\t' << client->player << '\t' << sanitize(client->name);
  }
  for (const auto& client : clients) {
    if (client->connected) {
      sendLine(client, roster.str());
      sendLine(client, "SESSION\t" + std::to_string(hostPlayerLocked()) + "\t" +
                           std::to_string(connectedCountLocked()));
      sendOptionsLocked(client);
    }
  }
}

std::string cardToken(const ek::Card& card) {
  return std::to_string(card.id) + ":" + ek::cardTypeName(card.type);
}

void sendSnapshotLocked() {
  if (!game) {
    sendRosterLocked();
    return;
  }
  ++sequence;
  for (const auto& client : clients) {
    if (!client->connected) continue;
    std::ostringstream state;
    state << "STATE\t" << sequence << '\t' << ek::phaseName(game->phase()) << '\t'
          << game->activePlayer() << '\t' << game->turnsRemaining() << '\t'
          << game->drawCount() << '\t' << game->discardCount() << '\t'
          << game->winner() << '\t' << sanitize(game->pendingType()) << '\t'
          << game->pendingActor() << '\t' << game->pendingTarget() << '\t'
          << game->pendingNopeCount() << '\t' << game->responseLeader();
    sendLine(client, state.str());
    sendLine(client, "ROUND\t" + std::to_string(roundNumber));
    sendLine(client, "SESSION\t" + std::to_string(hostPlayerLocked()) + "\t" +
                         std::to_string(connectedCountLocked()));
    std::ostringstream online;
    online << "ONLINE";
    for (const auto& item : clients) if (item->connected) online << '\t' << item->player;
    sendLine(client, online.str());
    sendLine(client, std::string("MODE\t") + (game->fastMode() ? "1" : "0") + "\t" +
                         (game->freeAttackMode() ? "1" : "0"));
    sendOptionsLocked(client);
    sendLine(client, "DECK\t" + std::to_string(game->bombCount()));
    if (game->phase() == ek::Phase::Response) {
      std::ostringstream pending;
      pending << "PENDING\t" << game->pendingActor();
      for (const auto& card : game->pendingCards()) {
        pending << '\t' << ek::cardTypeName(card.type);
      }
      sendLine(client, pending.str());
      if (game->pendingType() == "COMBO3" && game->pendingRequestedCard()) {
        sendLine(client, "REQUESTED\t" + ek::cardTypeName(*game->pendingRequestedCard()));
      }
    }
    if (game->phase() == ek::Phase::Kitten) {
      sendLine(client, "PROMPT\t" + std::to_string(game->kittenPlayer()));
    }
    for (const auto& player : game->players()) {
      sendLine(client, "PLAYER\t" + std::to_string(player.id) + "\t" +
                           sanitize(player.name) + "\t" + (player.alive ? "1" : "0") +
                           "\t" + std::to_string(player.hand.size()) +
                           "\t" + (player.exploded ? cardToken(*player.exploded) : "-"));
    }
    if (!game->discardPile().empty()) {
      sendLine(client, "DISCARD\t" + cardToken(game->discardPile().back()));
    }
    std::ostringstream hand;
    hand << "HAND";
    if (client->player >= 0 && game->players().at(client->player).alive) {
      for (const auto& card : game->hand(client->player)) hand << '\t' << cardToken(card);
    }
    sendLine(client, hand.str());
    sendLine(client, "ENDSTATE");
  }
}

void sendResultLocked(const ek::ActionResult& result) {
  const std::string event = "EVENT\t" + std::string(result.ok ? "OK\t" : "ERROR\t") +
                            sanitize(result.message);
  for (const auto& client : clients) {
    if (client->connected) sendLine(client, event);
  }
  if (result.privatePlayer >= 0) {
    for (const auto& client : clients) {
      if (client->player != result.privatePlayer || !client->connected) continue;
      std::ostringstream privateMessage;
      privateMessage << "PRIVATE\tSEE_FUTURE";
      for (const auto& card : result.privateCards) privateMessage << '\t' << cardToken(card);
      sendLine(client, privateMessage.str());
    }
  }
}

// Caller owns stateMutex. The connection owner alone closes its socket.
void removeClientLocked(const std::shared_ptr<Client>& client, bool kicked) {
  if (!client->connected.exchange(false)) return;
  if (kicked) sendLine(client, "EVENT\tERROR\tKICKED");
  if (client->isHost) {
    const auto position = std::find(clients.begin(), clients.end(), client);
    if (position != clients.end()) {
      const auto index = static_cast<std::size_t>(position - clients.begin());
      for (std::size_t offset = 1; offset < clients.size(); ++offset) {
        const auto& successor = clients[(index + offset) % clients.size()];
        if (successor->connected) { successor->isHost = true; break; }
      }
    }
  }
  client->isHost = false;
  if (game) {
    if (game->phase() != ek::Phase::GameOver) sendResultLocked(game->forfeit(client->player));
  } else {
    clients.erase(std::remove(clients.begin(), clients.end(), client), clients.end());
    for (std::size_t i = 0; i < clients.size(); ++i) {
      clients[i]->player = static_cast<int>(i);
      if (clients[i]->connected) sendLine(clients[i], "WELCOME\t" + std::to_string(i));
    }
  }
  ensureHostLocked();
  shutdownSocket(client->socket);
  sendSnapshotLocked();
}

void processCommand(const std::shared_ptr<Client>& client, const std::vector<std::string>& parts) {
  std::lock_guard<std::mutex> lock(stateMutex);
  if (parts.empty() || !client->connected) return;
  if (std::find(clients.begin(), clients.end(), client) == clients.end()) return;
  if (parts[0] == "PING") {
    sendLine(client, "PONG");
    return;
  }
  if (parts[0] == "KICK") {
    if (!client->isHost) {
      sendLine(client, "EVENT\tERROR\tONLY_HOST_CAN_KICK");
    } else {
      try {
        if (parts.size() != 2) throw std::invalid_argument("kick");
        const int player = parseInteger(parts[1]);
        const auto target = onlinePlayerLocked(player);
        if (player == client->player) sendLine(client, "EVENT\tERROR\tCANNOT_KICK_SELF");
        else if (!target) sendLine(client, "EVENT\tERROR\tPLAYER_OFFLINE");
        else removeClientLocked(target, true);
      } catch (const std::exception&) {
        sendLine(client, "EVENT\tERROR\tINVALID_COMMAND_ARGUMENT");
      }
    }
    return;
  }
  if (parts[0] == "SET_FAST" || parts[0] == "SET_FREE_ATTACK") {
    if (client->player != hostPlayerLocked()) {
      sendLine(client, "EVENT\tERROR\tONLY_HOST_CAN_SET_OPTIONS");
    } else if (game && game->phase() != ek::Phase::GameOver) {
      sendLine(client, "EVENT\tERROR\tOPTIONS_LOCKED_DURING_GAME");
    } else if (parts.size() != 2 || (parts[1] != "0" && parts[1] != "1")) {
      sendLine(client, "EVENT\tERROR\tINVALID_COMMAND_ARGUMENT");
    } else if (parts[0] == "SET_FAST" && parts[1] == "1" && connectedCountLocked() > 3) {
      sendLine(client, "EVENT\tERROR\tFAST_MODE_REQUIRES_2_OR_3_PLAYERS");
    } else {
      if (parts[0] == "SET_FAST") nextFastMode = parts[1] == "1";
      else nextFreeAttackMode = parts[1] == "1";
      sendSnapshotLocked();
    }
    return;
  }
  if (parts[0] == "START" || parts[0] == "RESTART") {
    const bool restart = parts[0] == "RESTART";
    if (client->player != hostPlayerLocked()) {
      sendLine(client, "EVENT\tERROR\tONLY_HOST_CAN_START");
    } else if (restart && (!game || game->phase() != ek::Phase::GameOver)) {
      sendLine(client, "EVENT\tERROR\tGAME_NOT_OVER");
    } else if (!restart && game) {
      sendLine(client, "EVENT\tERROR\tGAME_ALREADY_STARTED");
    } else if (connectedCountLocked() < 2 || connectedCountLocked() > 5) {
      sendLine(client, "EVENT\tERROR\tPLAYER_COUNT_MUST_BE_2_TO_5");
    } else if (nextFastMode && connectedCountLocked() > 3) {
      sendLine(client, "EVENT\tERROR\tFAST_MODE_REQUIRES_2_OR_3_PLAYERS");
    } else {
      std::vector<std::shared_ptr<Client>> participants;
      std::vector<std::string> names;
      for (const auto& item : clients) {
        if (item->connected) {
          participants.push_back(item);
          names.push_back(item->name);
        }
      }
      std::random_device entropy;
      auto nextGame = std::make_unique<ek::Game>(std::move(names), entropy());
      const auto result = nextGame->start(nextFastMode, nextFreeAttackMode);
      if (!result.ok) {
        sendLine(client, "EVENT\tERROR\t" + result.message);
        return;
      }
      clients = std::move(participants);
      game = std::move(nextGame);
      ++roundNumber;
      for (std::size_t i = 0; i < clients.size(); ++i) {
        clients[i]->player = static_cast<int>(i);
        sendLine(clients[i], "WELCOME\t" + std::to_string(i));
      }
      sendResultLocked(result);
      sendSnapshotLocked();
    }
    return;
  }
  if (!game) {
    sendLine(client, "EVENT\tERROR\tGAME_NOT_STARTED");
    return;
  }
  ek::ActionResult result;
  try {
    if (parts[0] == "PLAY" && (parts.size() == 2 || parts.size() == 3)) {
      const int card = std::stoi(parts[1]);
      const int target = parts.size() == 3 ? std::stoi(parts[2]) : -1;
      result = game->playCard(client->player, card, target);
    } else if (parts[0] == "COMBO" && (parts.size() == 4 || parts.size() == 6)) {
      std::vector<int> cards;
      cards.push_back(std::stoi(parts[1]));
      cards.push_back(std::stoi(parts[2]));
      std::size_t targetIndex = 3;
      std::optional<ek::CardType> requested;
      if (parts.size() == 6) {
        cards.push_back(std::stoi(parts[3]));
        targetIndex = 4;
        requested = ek::cardTypeFromName(parts[5]);
        if (!requested) {
          result = ek::ActionResult{false, "INVALID_REQUESTED_CARD", -1, {}};
          sendResultLocked(result);
          sendSnapshotLocked();
          return;
        }
      }
      const int target = std::stoi(parts[targetIndex]);
      result = game->playCombo(client->player, cards, target,
                               requested.value_or(ek::CardType::Defuse));
    } else if (parts[0] == "DRAW") {
      result = game->draw(client->player);
    } else if (parts[0] == "NOPE" && parts.size() >= 2) {
      result = game->respondNope(client->player, std::stoi(parts[1]));
    } else if (parts[0] == "PASS") {
      result = game->respondPass(client->player);
    } else if (parts[0] == "FAVOR" && parts.size() >= 2) {
      result = game->chooseFavor(client->player, std::stoi(parts[1]));
    } else if (parts[0] == "DEFUSE" && parts.size() >= 2) {
      result = game->chooseDefusePosition(client->player, static_cast<std::size_t>(std::stoul(parts[1])));
    } else if (parts[0] == "EXPLODE") {
      result = game->declineDefuse(client->player);
    } else {
      result = ek::ActionResult{false, "UNKNOWN_COMMAND", -1, {}};
    }
  } catch (const std::exception&) {
    result = ek::ActionResult{false, "INVALID_COMMAND_ARGUMENT", -1, {}};
  }
  sendResultLocked(result);
  sendSnapshotLocked();
}

void clientThread(const std::shared_ptr<Client>& client) {
  std::string line;
  while (client->connected && receiveLine(client->socket, line)) {
    processCommand(client, split(line));
  }
  {
    std::lock_guard<std::mutex> lock(stateMutex);
    removeClientLocked(client, false);
  }
  closeSocket(client->socket);
}

bool allowAdminAttempt(std::uint32_t address) {
  std::lock_guard<std::mutex> lock(authRateMutex);
  const auto now = std::chrono::steady_clock::now();
  for (auto it = authFailures.begin(); it != authFailures.end();) {
    if (now >= it->second.reset) it = authFailures.erase(it); else ++it;
  }
  if (now >= globalAuthFailures.reset) globalAuthFailures = {0, now + std::chrono::seconds(60)};
  if (authFailures.size() >= 1024 && !authFailures.count(address)) return false;
  auto& rate = authFailures[address];
  if (now >= rate.reset) rate = {0, now + std::chrono::seconds(60)};
  if (rate.failures >= 5 || globalAuthFailures.failures >= 30) return false;
  ++rate.failures;
  ++globalAuthFailures.failures;
  return true;
}

void authenticatedSuccessfully(std::uint32_t address) {
  std::lock_guard<std::mutex> lock(authRateMutex);
  authFailures.erase(address);
  if (globalAuthFailures.failures > 0) --globalAuthFailures.failures;
}

void sendAdminStateLocked(const std::shared_ptr<Client>& admin) {
  sendLine(admin, "ADMIN_BEGIN\t" + (game ? ek::phaseName(game->phase()) : "LOBBY") +
                      "\t" + std::to_string(hostPlayerLocked()) + "\t" + std::to_string(roundNumber));
  if (game) {
    for (const auto& player : game->players()) {
      sendLine(admin, "ADMIN_PLAYER\t" + std::to_string(player.id) + "\t" + sanitize(player.name) +
                          "\t" + (player.alive ? "1" : "0") + "\t" +
                          (onlinePlayerLocked(player.id) ? "1" : "0"));
      std::ostringstream hand;
      hand << "ADMIN_HAND\t" << player.id;
      for (const auto& card : player.hand) hand << '\t' << cardToken(card);
      sendLine(admin, hand.str());
    }
  } else {
    for (const auto& player : clients) {
      if (!player->connected) continue;
      sendLine(admin, "ADMIN_PLAYER\t" + std::to_string(player->player) + "\t" +
                          sanitize(player->name) + "\t1\t1");
      sendLine(admin, "ADMIN_HAND\t" + std::to_string(player->player));
    }
  }
  std::ostringstream deck;
  deck << "ADMIN_DECK";
  if (game) {
    for (auto card = game->drawPile().rbegin(); card != game->drawPile().rend(); ++card)
      deck << '\t' << cardToken(*card);
  }
  sendLine(admin, deck.str());
  sendLine(admin, "ADMIN_END");
}

void processAdminCommand(const std::shared_ptr<Client>& admin, const std::vector<std::string>& parts) {
  std::lock_guard<std::mutex> lock(stateMutex);
  if (!admin->connected) return;
  if (admin->authGeneration != adminAuth->generation()) {
    sendLine(admin, "ADMIN_ERROR\tAUTH_EXPIRED");
    admin->connected = false;
    shutdownSocket(admin->socket);
    return;
  }
  auto error = [&admin](const std::string& code) { sendLine(admin, "ADMIN_ERROR\t" + sanitize(code)); };
  if (parts.empty()) { error("UNKNOWN_COMMAND"); return; }
  try {
    if (parts[0] == "ADMIN_STATE" && parts.size() == 1) {
      sendAdminStateLocked(admin);
      return;
    }
    if (parts[0] == "ADMIN_PASSWORD" && parts.size() == 2) {
      std::string message;
      if (!adminAuth->changePassword(parts[1], admin->authGeneration, message)) { error(message); return; }
      admin->authGeneration = adminAuth->generation();
      sendLine(admin, "ADMIN_OK\tPASSWORD_CHANGED");
      for (const auto& other : administrators) {
        if (other == admin || !other->connected) continue;
        sendLine(other, "ADMIN_ERROR\tAUTH_EXPIRED");
        other->connected = false;
        shutdownSocket(other->socket);
      }
      return;
    }
    if (parts[0] == "ADMIN_HOST" && parts.size() == 2) {
      const int player = parseInteger(parts[1]);
      if (player < 0 || player >= static_cast<int>(game ? game->players().size() : clients.size())) {
        error("INVALID_PLAYER"); return;
      }
      const auto target = onlinePlayerLocked(player);
      if (!target) { error("PLAYER_OFFLINE"); return; }
      for (const auto& item : clients) item->isHost = item == target;
      sendLine(admin, "ADMIN_OK\tHOST_CHANGED");
      sendSnapshotLocked();
      return;
    }
    if ((parts[0] == "ADMIN_ADD" && parts.size() == 3) ||
        (parts[0] == "ADMIN_BOMB" && parts.size() == 2)) {
      if (!game) { error("GAME_NOT_STARTED"); return; }
      const int player = parseInteger(parts[1]);
      ek::ActionResult result;
      if (parts[0] == "ADMIN_ADD") {
        const auto type = ek::cardTypeFromName(parts[2]);
        if (!type) { error("INVALID_CARD_TYPE"); return; }
        result = game->adminAddCard(player, *type);
      } else {
        result = game->adminEndTurnWithKitten(player);
      }
      if (!result.ok) { error(result.message); return; }
      sendLine(admin, parts[0] == "ADMIN_ADD" ? "ADMIN_OK\tCARD_ADDED" : "ADMIN_OK\tKITTEN_INJECTED");
      sendResultLocked(result);
      sendSnapshotLocked();
      return;
    }
    if (parts[0] == "ADMIN_STATE" || parts[0] == "ADMIN_HOST" || parts[0] == "ADMIN_ADD" ||
        parts[0] == "ADMIN_BOMB" || parts[0] == "ADMIN_PASSWORD") error("INVALID_COMMAND_ARGUMENT");
    else error("UNKNOWN_COMMAND");
  } catch (const std::exception&) {
    error("INVALID_COMMAND_ARGUMENT");
  }
}

void administratorThread(const std::shared_ptr<Client>& admin) {
  std::string line;
  while (admin->connected && receiveLine(admin->socket, line, 300)) {
    processAdminCommand(admin, split(line));
  }
  {
    std::lock_guard<std::mutex> lock(stateMutex);
    admin->connected = false;
    administrators.erase(std::remove(administrators.begin(), administrators.end(), admin), administrators.end());
  }
  closeSocket(admin->socket);
}

void connectionThread(const std::shared_ptr<Client>& client, std::uint32_t address) {
  struct HandshakeSlot {
    bool held = true;
    void release() { if (held) { --pendingHandshakes; held = false; } }
    ~HandshakeSlot() { release(); }
  } slot;
  setSocketTimeouts(client->socket, 5);
  std::string hello;
  if (!receiveLine(client->socket, hello, 5)) { closeSocket(client->socket); return; }
  const auto parts = split(hello);
  if (!parts.empty() && parts[0] == "ADMIN") {
    const bool allowed = allowAdminAttempt(address);
    if (!allowed || parts.size() != 2 ||
        !adminAuth->verify(parts.size() == 2 ? parts[1] : "", client->authGeneration)) {
      sendLine(client, "ADMIN_ERROR\tAUTH_FAILED");
      closeSocket(client->socket);
      return;
    }
    {
      std::lock_guard<std::mutex> lock(stateMutex);
      if (client->authGeneration != adminAuth->generation()) {
        sendLine(client, "ADMIN_ERROR\tAUTH_EXPIRED"); closeSocket(client->socket); return;
      }
      if (administrators.size() >= 8) {
        sendLine(client, "ADMIN_ERROR\tADMIN_SESSION_LIMIT"); closeSocket(client->socket); return;
      }
      administrators.push_back(client);
      setSocketTimeouts(client->socket, 300);
      sendLine(client, "ADMIN_OK\tAUTHENTICATED");
    }
    authenticatedSuccessfully(address);
    slot.release();
    administratorThread(client);
    return;
  }
  if (parts.size() != 2 || parts[0] != "JOIN") {
    sendLine(client, "EVENT\tERROR\tFIRST_COMMAND_MUST_BE_JOIN"); closeSocket(client->socket); return;
  }
  const std::string name = normalizeName(parts[1]);
  if (name.empty() || name.size() > 128 || name.find_first_of("\r\n\0", 0, 3) != std::string::npos) {
    sendLine(client, "EVENT\tERROR\tINVALID_NAME"); closeSocket(client->socket); return;
  }
  {
    std::lock_guard<std::mutex> lock(stateMutex);
    if (game || clients.size() >= 5) {
      sendLine(client, "EVENT\tERROR\tLOBBY_CLOSED"); closeSocket(client->socket); return;
    }
    const bool duplicate = std::any_of(clients.begin(), clients.end(),
        [&name](const auto& item) { return item->connected && item->name == name; });
    if (duplicate) {
      sendLine(client, "EVENT\tERROR\tNAME_ALREADY_TAKEN"); closeSocket(client->socket); return;
    }
    client->player = static_cast<int>(clients.size());
    client->name = name;
    clients.push_back(client);
    ensureHostLocked();
    setSocketTimeouts(client->socket, 0);
    sendLine(client, "WELCOME\t" + std::to_string(client->player));
    sendRosterLocked();
  }
  slot.release();
  clientThread(client);
}

}  // namespace

int main(int argc, char** argv) {
#ifdef _WIN32
  WSADATA data{};
  if (WSAStartup(MAKEWORD(2, 2), &data) != 0) {
    std::cerr << "WSAStartup failed\n";
    return 1;
  }
#endif
  int port = 8765, httpPort = 8766;
  try {
    if (argc > 4) throw std::invalid_argument("arguments");
    if (argc > 1) port = parseInteger(argv[1]);
    httpPort = argc > 2 ? parseInteger(argv[2]) : port + 1;
    if (port < 1 || port > 65535 || httpPort < 0 || httpPort > 65535 || httpPort == port)
      throw std::invalid_argument("port");
  } catch (const std::exception&) {
    std::cerr << "Usage: ek_server [game_port=8765] [http_port=game_port+1; 0 disables] [credentials_path]\n";
    return 1;
  }
  std::filesystem::path executable, credentials;
  std::string startupError;
  try {
    executable = ek::executableDirectory(argv[0]);
    credentials = executable / "server_admin.auth";
#ifdef _WIN32
    // The narrow CRT argv uses the system code page and can corrupt Unicode paths.
    int wideCount = 0;
    wchar_t** wideArguments = CommandLineToArgvW(GetCommandLineW(), &wideCount);
    if (!wideArguments) throw std::runtime_error("command line");
    if (wideCount > 3) credentials = std::filesystem::path(wideArguments[3]);
    LocalFree(wideArguments);
#else
    if (argc > 3) credentials = std::filesystem::path(argv[3]);
#endif
    adminAuth = std::make_unique<ek::AdminAuth>(credentials);
    if (!adminAuth->initialize(startupError)) {
      std::cerr << "Administrator authentication unavailable: " << startupError << "\n";
      return 1;
    }
  } catch (const std::exception&) {
    std::cerr << "Administrator authentication unavailable: ADMIN_AUTH_PATH_FAILED\n";
    return 1;
  }
  const Socket listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (listener == kInvalidSocket) {
    std::cerr << "could not create socket\n";
    return 1;
  }
  int reuse = 1;
  setsockopt(listener, SOL_SOCKET, SO_REUSEADDR,
#ifdef _WIN32
             reinterpret_cast<const char*>(&reuse), sizeof(reuse));
#else
             &reuse, sizeof(reuse));
#endif
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_ANY);
  address.sin_port = htons(static_cast<unsigned short>(port));
  if (bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) < 0 ||
      listen(listener, 5) < 0) {
    std::cerr << "could not bind/listen on port " << port << "\n";
    closeSocket(listener);
    return 1;
  }
  if (!ek::startClientDownload(httpPort, executable, credentials, port, startupError)) {
    std::cerr << "Client download service unavailable: " << startupError << "\n";
    closeSocket(listener);
    return 1;
  }
  std::cout << "Exploding Kittens server listening on TCP " << port << "\n";
  while (true) {
    sockaddr_in remote{};
#ifdef _WIN32
    int length = sizeof(remote);
#else
    socklen_t length = sizeof(remote);
#endif
    const Socket clientSocket = accept(listener, reinterpret_cast<sockaddr*>(&remote), &length);
    if (clientSocket == kInvalidSocket) continue;
    if (pendingHandshakes.fetch_add(1) >= 32) {
      --pendingHandshakes;
      closeSocket(clientSocket);
      continue;
    }
    auto client = std::make_shared<Client>();
    client->socket = clientSocket;
    std::thread(connectionThread, client, remote.sin_addr.s_addr).detach();
  }
#ifdef _WIN32
  WSACleanup();
#endif
  return 0;
}

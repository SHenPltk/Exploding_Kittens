#include "exploding_kittens/game.hpp"

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

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
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
};

std::mutex stateMutex;
std::vector<std::shared_ptr<Client>> clients;
std::unique_ptr<ek::Game> game;
std::uint64_t sequence = 0;
std::uint64_t roundNumber = 0;
bool nextFastMode = false;

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
                                payload.size() - sent, 0);
#endif
    if (result <= 0) return false;
    sent += static_cast<std::size_t>(result);
  }
  return true;
}

bool receiveLine(Socket socket, std::string& output) {
  output.clear();
  char character = 0;
  while (output.size() < 8192) {
#ifdef _WIN32
    const int result = recv(socket, &character, 1, 0);
#else
    const ssize_t result = recv(socket, &character, 1, 0);
#endif
    if (result <= 0) return false;
    if (character == '\n') return true;
    if (character != '\r') output.push_back(character);
  }
  return false;
}

std::vector<std::string> split(const std::string& line) {
  std::vector<std::string> result;
  std::stringstream stream(line);
  std::string item;
  while (std::getline(stream, item, '\t')) result.push_back(item);
  return result;
}

std::string sanitize(std::string value) {
  std::replace(value.begin(), value.end(), '\t', ' ');
  std::replace(value.begin(), value.end(), '\n', ' ');
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
    if (client->connected) return client->player;
  }
  return -1;
}

int connectedCountLocked() {
  return static_cast<int>(std::count_if(clients.begin(), clients.end(),
      [](const auto& client) { return client->connected.load(); }));
}

void sendOptionsLocked(const std::shared_ptr<Client>& client) {
  sendLine(client, std::string("OPTIONS\t") + (nextFastMode ? "1" : "0"));
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
    sendLine(client, std::string("MODE\t") + (game->fastMode() ? "1" : "0"));
    sendOptionsLocked(client);
    sendLine(client, "DECK\t" + std::to_string(game->bombCount()));
    if (game->phase() == ek::Phase::Response) {
      std::ostringstream pending;
      pending << "PENDING\t" << game->pendingActor();
      for (const auto& card : game->pendingCards()) {
        pending << '\t' << ek::cardTypeName(card.type);
      }
      sendLine(client, pending.str());
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

void processCommand(const std::shared_ptr<Client>& client, const std::vector<std::string>& parts) {
  std::lock_guard<std::mutex> lock(stateMutex);
  if (parts.empty() || !client->connected) return;
  if (std::find(clients.begin(), clients.end(), client) == clients.end()) return;
  if (parts[0] == "PING") {
    sendLine(client, "PONG");
    return;
  }
  if (parts[0] == "SET_FAST") {
    if (client->player != hostPlayerLocked()) {
      sendLine(client, "EVENT\tERROR\tONLY_HOST_CAN_SET_OPTIONS");
    } else if (game && game->phase() != ek::Phase::GameOver) {
      sendLine(client, "EVENT\tERROR\tOPTIONS_LOCKED_DURING_GAME");
    } else if (parts.size() != 2 || (parts[1] != "0" && parts[1] != "1")) {
      sendLine(client, "EVENT\tERROR\tINVALID_COMMAND_ARGUMENT");
    } else if (parts[1] == "1" && connectedCountLocked() > 3) {
      sendLine(client, "EVENT\tERROR\tFAST_MODE_REQUIRES_2_OR_3_PLAYERS");
    } else {
      nextFastMode = parts[1] == "1";
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
      const auto result = nextGame->start(nextFastMode);
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
          result = ek::ActionResult{false, "INVALID_REQUESTED_CARD"};
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
      result = ek::ActionResult{false, "UNKNOWN_COMMAND"};
    }
  } catch (const std::exception&) {
    result = ek::ActionResult{false, "INVALID_COMMAND_ARGUMENT"};
  }
  sendResultLocked(result);
  sendSnapshotLocked();
}

void clientThread(const std::shared_ptr<Client>& client) {
  std::string line;
  while (client->connected && receiveLine(client->socket, line)) {
    processCommand(client, split(line));
  }
  std::lock_guard<std::mutex> lock(stateMutex);
  client->connected = false;
  if (game && game->phase() != ek::Phase::GameOver) {
    sendResultLocked(game->forfeit(client->player));
    sendSnapshotLocked();
  } else if (game) {
    sendSnapshotLocked();
  } else {
    clients.erase(std::remove_if(clients.begin(), clients.end(),
                                 [client](const auto& item) { return item == client; }),
                  clients.end());
    for (std::size_t i = 0; i < clients.size(); ++i) {
      clients[i]->player = static_cast<int>(i);
      sendLine(clients[i], "WELCOME\t" + std::to_string(clients[i]->player));
    }
    sendRosterLocked();
  }
  closeSocket(client->socket);
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
  const int port = argc > 1 ? std::atoi(argv[1]) : 8765;
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
    auto client = std::make_shared<Client>();
    client->socket = clientSocket;
#ifdef _WIN32
    const DWORD handshakeTimeoutMs = 5000;
    setsockopt(clientSocket, SOL_SOCKET, SO_RCVTIMEO,
               reinterpret_cast<const char*>(&handshakeTimeoutMs), sizeof(handshakeTimeoutMs));
#else
    timeval handshakeTimeout{5, 0};
    setsockopt(clientSocket, SOL_SOCKET, SO_RCVTIMEO, &handshakeTimeout, sizeof(handshakeTimeout));
#endif
    std::string hello;
    if (!receiveLine(clientSocket, hello)) {
      closeSocket(clientSocket);
      continue;
    }
    const auto parts = split(hello);
    if (parts.size() != 2 || parts[0] != "JOIN") {
      sendLine(client, "EVENT\tERROR\tFIRST_COMMAND_MUST_BE_JOIN");
      closeSocket(clientSocket);
      continue;
    }
    const std::string name = normalizeName(parts[1]);
    if (name.empty()) {
      sendLine(client, "EVENT\tERROR\tINVALID_NAME");
      closeSocket(clientSocket);
      continue;
    }
    {
      std::lock_guard<std::mutex> lock(stateMutex);
      if (game || clients.size() >= 5) {
        sendLine(client, "EVENT\tERROR\tLOBBY_CLOSED");
        closeSocket(clientSocket);
        continue;
      }
      const bool duplicate = std::any_of(clients.begin(), clients.end(),
          [&name](const auto& item) { return item->connected && item->name == name; });
      if (duplicate) {
        sendLine(client, "EVENT\tERROR\tNAME_ALREADY_TAKEN");
        closeSocket(clientSocket);
        continue;
      }
      client->player = static_cast<int>(clients.size());
      client->name = name;
      clients.push_back(client);
#ifdef _WIN32
      const DWORD noTimeout = 0;
      setsockopt(clientSocket, SOL_SOCKET, SO_RCVTIMEO,
                 reinterpret_cast<const char*>(&noTimeout), sizeof(noTimeout));
#else
      timeval noTimeout{0, 0};
      setsockopt(clientSocket, SOL_SOCKET, SO_RCVTIMEO, &noTimeout, sizeof(noTimeout));
#endif
      sendLine(client, "WELCOME\t" + std::to_string(client->player));
      sendRosterLocked();
    }
    std::thread(clientThread, client).detach();
  }
#ifdef _WIN32
  WSACleanup();
#endif
  return 0;
}

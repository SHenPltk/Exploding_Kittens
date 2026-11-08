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

void sendRosterLocked() {
  std::ostringstream roster;
  roster << "ROSTER";
  for (const auto& client : clients) {
    roster << '\t' << client->player << '\t' << sanitize(client->name);
  }
  for (const auto& client : clients) sendLine(client, roster.str());
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
    for (const auto& player : game->players()) {
      sendLine(client, "PLAYER\t" + std::to_string(player.id) + "\t" +
                           sanitize(player.name) + "\t" + (player.alive ? "1" : "0") +
                           "\t" + std::to_string(player.hand.size()) +
                           "\t" + (player.exploded ? cardToken(*player.exploded) : "-"));
    }
    for (const auto& card : game->discardPile()) {
      sendLine(client, "DISCARD\t" + cardToken(card));
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
  if (parts.empty()) return;
  if (parts[0] == "PING") {
    sendLine(client, "PONG");
    return;
  }
  if (parts[0] == "START") {
    if (game) {
      sendLine(client, "EVENT\tERROR\tGAME_ALREADY_STARTED");
    } else if (client->player != 0) {
      sendLine(client, "EVENT\tERROR\tONLY_HOST_CAN_START");
    } else if (clients.size() < 2 || clients.size() > 5) {
      sendLine(client, "EVENT\tERROR\tPLAYER_COUNT_MUST_BE_2_TO_5");
    } else {
      std::vector<std::string> names;
      for (const auto& item : clients) names.push_back(item->name);
      std::random_device entropy;
      game = std::make_unique<ek::Game>(std::move(names), entropy());
      sendResultLocked(game->start());
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
  } else if (!game) {
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
    if (parts.size() < 2 || parts[0] != "JOIN" || parts[1].empty()) {
      sendLine(client, "EVENT\tERROR\tFIRST_COMMAND_MUST_BE_JOIN");
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
      client->player = static_cast<int>(clients.size());
      client->name = sanitize(parts[1]);
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

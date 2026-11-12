#include "client_download.hpp"

#include <array>
#include <chrono>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <iterator>
#include <sstream>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
using DownloadSocket = SOCKET;
constexpr DownloadSocket kBadDownloadSocket = INVALID_SOCKET;
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
using DownloadSocket = int;
constexpr DownloadSocket kBadDownloadSocket = -1;
#endif

namespace ek {
namespace {
void closeDownloadSocket(DownloadSocket socket) {
#ifdef _WIN32
  closesocket(socket);
#else
  close(socket);
#endif
}

void setTimeout(DownloadSocket socket) {
#ifdef _WIN32
  const DWORD timeout = 3000;
  setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
  setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
#else
  const timeval timeout{3, 0};
  setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
  setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
#endif
}

bool sendBytes(DownloadSocket socket, const std::string& bytes) {
  std::size_t offset = 0;
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
  while (offset < bytes.size() && std::chrono::steady_clock::now() < deadline) {
    const auto size = std::min<std::size_t>(bytes.size() - offset, 65536);
#ifdef _WIN32
    const int sent = send(socket, bytes.data() + offset, static_cast<int>(size), 0);
#else
    const auto sent = send(socket, bytes.data() + offset, size, MSG_NOSIGNAL);
#endif
    if (sent <= 0) return false;
    offset += static_cast<std::size_t>(sent);
  }
  return offset == bytes.size();
}

void append16(std::string& out, std::uint16_t value) {
  out.push_back(static_cast<char>(value & 255));
  out.push_back(static_cast<char>((value >> 8) & 255));
}
void append32(std::string& out, std::uint32_t value) {
  append16(out, static_cast<std::uint16_t>(value));
  append16(out, static_cast<std::uint16_t>(value >> 16));
}
std::uint32_t crc32(const std::string& bytes) {
  std::uint32_t crc = 0xffffffff;
  for (unsigned char byte : bytes) {
    crc ^= byte;
    for (int bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
  }
  return crc ^ 0xffffffff;
}

std::string makeZip(const std::vector<std::pair<std::string, std::string>>& files) {
  std::string archive, central;
  for (const auto& entry : files) {
    const auto offset = static_cast<std::uint32_t>(archive.size());
    const auto size = static_cast<std::uint32_t>(entry.second.size());
    const auto crc = crc32(entry.second);
    append32(archive, 0x04034b50); append16(archive, 20); append16(archive, 0);
    append16(archive, 0); append16(archive, 0); append16(archive, 0x21);
    append32(archive, crc); append32(archive, size); append32(archive, size);
    append16(archive, static_cast<std::uint16_t>(entry.first.size())); append16(archive, 0);
    archive += entry.first; archive += entry.second;
    append32(central, 0x02014b50); append16(central, 20); append16(central, 20);
    append16(central, 0); append16(central, 0); append16(central, 0); append16(central, 0x21);
    append32(central, crc); append32(central, size); append32(central, size);
    append16(central, static_cast<std::uint16_t>(entry.first.size()));
    append16(central, 0); append16(central, 0); append16(central, 0); append16(central, 0);
    append32(central, 0); append32(central, offset); central += entry.first;
  }
  const auto centralOffset = static_cast<std::uint32_t>(archive.size());
  archive += central;
  append32(archive, 0x06054b50); append16(archive, 0); append16(archive, 0);
  append16(archive, static_cast<std::uint16_t>(files.size()));
  append16(archive, static_cast<std::uint16_t>(files.size()));
  append32(archive, static_cast<std::uint32_t>(central.size())); append32(archive, centralOffset);
  append16(archive, 0);
  return archive;
}

bool loadArchive(const std::filesystem::path& directory, const std::filesystem::path& credentials,
                 int gamePort, std::string& archive) {
  std::error_code ec;
  const auto base = std::filesystem::canonical(directory, ec);
  if (ec) return false;
  const auto auth = std::filesystem::weakly_canonical(credentials, ec);
  if (ec) return false;
  std::vector<std::pair<std::string, std::string>> files;
  for (const char* name : {"main.py", "i18n.py", "presentation.py"}) {
    const auto source = base / name;
    const auto status = std::filesystem::symlink_status(source, ec);
    if (ec || !std::filesystem::is_regular_file(status)) return false;
    const auto path = std::filesystem::canonical(source, ec);
    if (ec || path.parent_path() != base || path == auth ||
        std::filesystem::equivalent(path, auth, ec) || ec) return false;
    const auto size = std::filesystem::file_size(path, ec);
    if (ec || size == 0 || size > 4 * 1024 * 1024) return false;
    std::ifstream file(path, std::ios::binary);
    if (!file) return false;
    std::string content(static_cast<std::size_t>(size), '\0');
    if (!file.read(content.data(), static_cast<std::streamsize>(size))) return false;
    files.emplace_back(name, std::move(content));
  }
  files.emplace_back("start_client.bat",
      "@echo off\r\ncd /d \"%~dp0\"\r\npy -3 main.py\r\n"
      "if errorlevel 1 (\r\n  echo Install Python 3.10 or newer with Tkinter, then run py -3 main.py\r\n  pause\r\n)\r\n");
  files.emplace_back("README.txt",
      "Exploding Kittens LAN client / 局域网客户端\r\n\r\n"
      "Requires Python 3.10+ with Tkinter (Tk). Python is not included.\r\n"
      "需要 Python 3.10 或更新版本及 Tkinter；本压缩包不含 Python。\r\n"
      "Windows: extract ALL files, then double-click start_client.bat.\r\n"
      "Other systems: python3 main.py (install your OS Python Tk package if needed).\r\n"
      "Enter the server computer's LAN IPv4 address, TCP port " + std::to_string(gamePort) +
      ", and a unique player name in the client.\r\n"
      "解压全部文件后启动；输入服务器局域网 IPv4 地址、游戏端口及唯一玩家名。\r\n"
      "Allow the game TCP port through the server firewall on your trusted LAN.\r\n");
  archive = makeZip(files);
  return true;
}

void response(DownloadSocket socket, int status, const std::string& reason,
              const std::string& type, const std::string& body, bool head,
              const std::string& additional = {}) {
  std::ostringstream headers;
  headers << "HTTP/1.1 " << status << ' ' << reason << "\r\nContent-Type: " << type
          << "\r\nContent-Length: " << body.size()
          << "\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
          << additional << "\r\n";
  if (sendBytes(socket, headers.str()) && !head) sendBytes(socket, body);
}

void serveRequest(DownloadSocket socket, const std::vector<std::filesystem::path>& directories,
                  const std::filesystem::path& credentials, int gamePort) {
  setTimeout(socket);
  std::string request;
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
  std::array<char, 1024> buffer{};
  while (request.find("\r\n\r\n") == std::string::npos) {
    if (request.size() >= 8192 || std::chrono::steady_clock::now() >= deadline) {
      response(socket, 408, "Request Timeout", "text/plain; charset=utf-8", "Request timed out.\n", false);
      return;
    }
#ifdef _WIN32
    const int count = recv(socket, buffer.data(), static_cast<int>(buffer.size()), 0);
#else
    const auto count = recv(socket, buffer.data(), buffer.size(), 0);
#endif
    if (count <= 0) return;
    request.append(buffer.data(), static_cast<std::size_t>(count));
  }
  if (request.size() > 8192) {
    response(socket, 431, "Request Header Fields Too Large", "text/plain", "Headers too large.\n", false);
    return;
  }
  std::istringstream firstLine(request.substr(0, request.find("\r\n")));
  std::string method, path, version, extra;
  if (!(firstLine >> method >> path >> version) || (firstLine >> extra) ||
      (version != "HTTP/1.1" && version != "HTTP/1.0")) {
    response(socket, 400, "Bad Request", "text/plain", "Bad request.\n", false);
    return;
  }
  const bool head = method == "HEAD";
  if (method != "GET" && !head) {
    response(socket, 405, "Method Not Allowed", "text/plain", "Use GET or HEAD.\n", false,
             "Allow: GET, HEAD\r\n");
    return;
  }
  if (path == "/") {
    const std::string page =
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>Exploding Kittens LAN client</title>"
        "<body><h1>Exploding Kittens / 爆炸猫</h1><p><a href=\"/client.zip\">Download client / 下载客户端 ZIP</a></p>"
        "<p>Requires Python 3.10+ with Tkinter (Tk); Python is not included. Extract all files, then run "
        "start_client.bat on Windows or <code>python3 main.py</code> elsewhere.</p>"
        "<p>需要 Python 3.10 或更新版本及 Tkinter（压缩包不含 Python）。解压全部文件，Windows 双击 "
        "start_client.bat，其他系统运行 python3 main.py。</p><p>Connect to this server's LAN IPv4 address, game TCP port " +
        std::to_string(gamePort) + ". 输入本服务器局域网 IPv4 地址，游戏 TCP 端口 " +
        std::to_string(gamePort) + "。Allow this game port through the server's LAN firewall.</p></body></html>";
    response(socket, 200, "OK", "text/html; charset=utf-8", page, head);
  } else if (path == "/client.zip") {
    std::string archive;
    bool found = false;
    for (const auto& directory : directories) {
      if (loadArchive(directory, credentials, gamePort, archive)) { found = true; break; }
    }
    if (found) response(socket, 200, "OK", "application/zip", archive, head,
                        "Content-Disposition: attachment; filename=\"client.zip\"\r\n");
    else response(socket, 503, "Service Unavailable", "text/plain; charset=utf-8",
                  "Client files unavailable. Ask the host to install the client distribution.\n", head);
  } else {
    response(socket, 404, "Not Found", "text/plain; charset=utf-8", "Not found.\n", head);
  }
}
}  // namespace

bool startClientDownload(int port, const std::filesystem::path& executableDirectory,
                         const std::filesystem::path& credentialPath, int gamePort,
                         std::string& error) {
  if (port == 0) return true;
  const auto listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (listener == kBadDownloadSocket) { error = "HTTP_SOCKET_FAILED"; return false; }
  int reuse = 1;
#ifdef _WIN32
  setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&reuse), sizeof(reuse));
#else
  setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
#endif
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_ANY);
  address.sin_port = htons(static_cast<unsigned short>(port));
  if (bind(listener, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0 ||
      listen(listener, 8) != 0) {
    error = "HTTP_BIND_FAILED"; closeDownloadSocket(listener); return false;
  }
  std::vector<std::filesystem::path> directories;
#ifdef EK_SOURCE_CLIENT_DIR
  directories.emplace_back(std::filesystem::u8path(EK_SOURCE_CLIENT_DIR));
#endif
  directories.push_back(executableDirectory / "client");
  directories.push_back(executableDirectory / "clients" / "client");
  directories.push_back(executableDirectory / "clients");
  std::thread([listener, directories, credentialPath, gamePort] {
    while (true) {
      const auto connection = accept(listener, nullptr, nullptr);
      if (connection == kBadDownloadSocket) continue;
      try { serveRequest(connection, directories, credentialPath, gamePort); }
      catch (const std::exception&) {
        response(connection, 500, "Internal Server Error", "text/plain", "Download unavailable.\n", false);
      }
      closeDownloadSocket(connection);
    }
  }).detach();
  std::cout << "LAN client download: http://<server-LAN-IP>:" << port
            << "/ (Python 3.10+ with Tkinter required)\n";
  return true;
}
}  // namespace ek

#include "admin_auth.hpp"

#include <array>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <system_error>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <bcrypt.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <openssl/evp.h>
#include <openssl/rand.h>
#endif

namespace ek {
namespace {
std::string hex(const std::vector<unsigned char>& bytes) {
  static constexpr char digits[] = "0123456789abcdef";
  std::string result;
  for (auto byte : bytes) {
    result.push_back(digits[byte >> 4]);
    result.push_back(digits[byte & 15]);
  }
  return result;
}

bool unhex(const std::string& text, std::size_t count, std::vector<unsigned char>& out) {
  if (text.size() != count * 2) return false;
  out.clear();
  auto digit = [](char c) -> int {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
  };
  for (std::size_t i = 0; i < text.size(); i += 2) {
    const int a = digit(text[i]), b = digit(text[i + 1]);
    if (a < 0 || b < 0) return false;
    out.push_back(static_cast<unsigned char>((a << 4) | b));
  }
  return true;
}

bool randomBytes(std::vector<unsigned char>& bytes) {
#ifdef _WIN32
  return BCryptGenRandom(nullptr, bytes.data(), static_cast<ULONG>(bytes.size()),
                         BCRYPT_USE_SYSTEM_PREFERRED_RNG) >= 0;
#else
  return RAND_bytes(bytes.data(), static_cast<int>(bytes.size())) == 1;
#endif
}

bool derive(const std::string& password, const std::vector<unsigned char>& salt,
            std::uint64_t iterations, std::vector<unsigned char>& out) {
  out.resize(32);
#ifdef _WIN32
  BCRYPT_ALG_HANDLE algorithm = nullptr;
  if (BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr,
                                  BCRYPT_ALG_HANDLE_HMAC_FLAG) < 0) return false;
  const auto status = BCryptDeriveKeyPBKDF2(
      algorithm, reinterpret_cast<PUCHAR>(const_cast<char*>(password.data())),
      static_cast<ULONG>(password.size()), const_cast<PUCHAR>(salt.data()),
      static_cast<ULONG>(salt.size()), iterations, out.data(),
      static_cast<ULONG>(out.size()), 0);
  BCryptCloseAlgorithmProvider(algorithm, 0);
  return status >= 0;
#else
  return PKCS5_PBKDF2_HMAC(password.data(), static_cast<int>(password.size()),
      salt.data(), static_cast<int>(salt.size()), static_cast<int>(iterations),
      EVP_sha256(), static_cast<int>(out.size()), out.data()) == 1;
#endif
}

bool cryptoSelfTest() {
  std::vector<unsigned char> derived;
  return derive("password", {'s', 'a', 'l', 't'}, 1, derived) &&
      hex(derived) == "120fb6cffcf8b32c43e7225256c4f837a86548c92ccc35480805987cb70be17b";
}
}  // namespace

AdminAuth::AdminAuth(std::filesystem::path path) : path_(std::move(path)) {}

bool AdminAuth::validPassword(const std::string& password) {
  return password.size() >= 8 && password.size() <= 128 &&
      password.find_first_of("\t\r\n\0", 0, 4) == std::string::npos;
}

bool AdminAuth::initialize(std::string& error) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (!cryptoSelfTest()) { error = "ADMIN_CRYPTO_FAILED"; return false; }
  std::error_code ec;
  const auto status = std::filesystem::symlink_status(path_, ec);
  if (ec && ec != std::errc::no_such_file_or_directory) {
    error = "ADMIN_AUTH_FILE_UNREADABLE"; return false;
  }
  if (!std::filesystem::exists(status)) {
    std::vector<unsigned char> salt(16), verifier;
    if (!randomBytes(salt) || !derive("Admin123", salt, iterations_, verifier)) {
      error = "ADMIN_CRYPTO_FAILED"; return false;
    }
    if (!writeRecord(salt, verifier, false, error)) return false;
    salt_ = std::move(salt);
    verifier_ = std::move(verifier);
    return true;
  }
  if (!std::filesystem::is_regular_file(status) ||
      std::filesystem::file_size(path_, ec) > 1024 || ec) {
    error = "ADMIN_AUTH_FILE_INVALID"; return false;
  }
  std::ifstream file(path_, std::ios::binary);
  std::string version, iterations, salt, verifier, extra;
  if (!(file >> version >> iterations >> salt >> verifier) || (file >> extra) ||
      version != "EKADMIN1" || iterations != "600000" ||
      !unhex(salt, 16, salt_) || !unhex(verifier, 32, verifier_)) {
    error = "ADMIN_AUTH_FILE_INVALID"; return false;
  }
  return true;
}

bool AdminAuth::verify(const std::string& password, std::uint64_t& generation) {
  std::lock_guard<std::mutex> lock(mutex_);
  std::vector<unsigned char> candidate;
  // Always perform the KDF for bounded input, even when its length is invalid.
  if (password.size() > 128 || verifier_.size() != 32 ||
      !derive(password, salt_, iterations_, candidate)) return false;
  unsigned int difference = 0;
  for (std::size_t i = 0; i < verifier_.size(); ++i) difference |= candidate[i] ^ verifier_[i];
  if (difference != 0 || !validPassword(password)) return false;
  generation = generation_.load();
  return true;
}

bool AdminAuth::changePassword(const std::string& password, std::uint64_t expectedGeneration,
                               std::string& error) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (generation_.load() != expectedGeneration) { error = "AUTH_EXPIRED"; return false; }
  if (!validPassword(password)) { error = "INVALID_PASSWORD"; return false; }
  std::vector<unsigned char> salt(16), verifier;
  if (!randomBytes(salt) || !derive(password, salt, iterations_, verifier)) {
    error = "ADMIN_CRYPTO_FAILED"; return false;
  }
  if (!writeRecord(salt, verifier, true, error)) return false;
  salt_ = std::move(salt);
  verifier_ = std::move(verifier);
  ++generation_;
  return true;
}

bool AdminAuth::writeRecord(const std::vector<unsigned char>& salt,
                            const std::vector<unsigned char>& verifier, bool replace,
                            std::string& error) {
  std::vector<unsigned char> suffix(12);
  if (!randomBytes(suffix)) { error = "ADMIN_CRYPTO_FAILED"; return false; }
  auto temporary = path_;
  temporary += ".tmp." + hex(suffix);
  const std::string record = "EKADMIN1 600000 " + hex(salt) + " " + hex(verifier) + "\n";
  bool written = false;
#ifdef _WIN32
  HANDLE file = CreateFileW(temporary.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                            FILE_ATTRIBUTE_NORMAL, nullptr);
  if (file != INVALID_HANDLE_VALUE) {
    DWORD count = 0;
    written = WriteFile(file, record.data(), static_cast<DWORD>(record.size()), &count, nullptr) &&
        count == record.size() && FlushFileBuffers(file);
    CloseHandle(file);
    if (written) written = MoveFileExW(temporary.c_str(), path_.c_str(),
        MOVEFILE_WRITE_THROUGH | (replace ? MOVEFILE_REPLACE_EXISTING : 0)) != 0;
  }
#else
  const int file = ::open(temporary.c_str(), O_WRONLY | O_CREAT | O_EXCL, S_IRUSR | S_IWUSR);
  if (file >= 0) {
    std::size_t offset = 0;
    while (offset < record.size()) {
      const auto count = ::write(file, record.data() + offset, record.size() - offset);
      if (count <= 0) break;
      offset += static_cast<std::size_t>(count);
    }
    written = offset == record.size() && ::fsync(file) == 0;
    ::close(file);
    if (written) {
      written = replace ? ::rename(temporary.c_str(), path_.c_str()) == 0
                        : ::link(temporary.c_str(), path_.c_str()) == 0;
    }
  }
#endif
  std::error_code ec;
  std::filesystem::remove(temporary, ec);
  if (!written) error = "PASSWORD_SAVE_FAILED";
  return written;
}

std::filesystem::path executableDirectory(const char* argv0) {
#ifdef _WIN32
  std::vector<wchar_t> buffer(32768);
  const DWORD size = GetModuleFileNameW(nullptr, buffer.data(), static_cast<DWORD>(buffer.size()));
  if (size > 0 && size < buffer.size()) return std::filesystem::path(buffer.data()).parent_path();
#else
  std::array<char, 4096> buffer{};
  const auto size = ::readlink("/proc/self/exe", buffer.data(), buffer.size() - 1);
  if (size > 0) return std::filesystem::path(std::string(buffer.data(), size)).parent_path();
#endif
  return std::filesystem::absolute(argv0).parent_path();
}
}  // namespace ek

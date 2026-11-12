#pragma once

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <mutex>
#include <string>
#include <vector>

namespace ek {

// The credential record contains only a salted PBKDF2-HMAC-SHA256 verifier.
class AdminAuth {
 public:
  explicit AdminAuth(std::filesystem::path path);
  bool initialize(std::string& error);
  bool verify(const std::string& password, std::uint64_t& generation);
  bool changePassword(const std::string& password, std::uint64_t expectedGeneration,
                      std::string& error);
  std::uint64_t generation() const { return generation_.load(); }
  static bool validPassword(const std::string& password);

 private:
  bool writeRecord(const std::vector<unsigned char>& salt,
                   const std::vector<unsigned char>& verifier, bool replace,
                   std::string& error);
  std::filesystem::path path_;
  std::mutex mutex_;
  std::vector<unsigned char> salt_, verifier_;
  std::uint64_t iterations_ = 600000;
  std::atomic<std::uint64_t> generation_{1};
};

std::filesystem::path executableDirectory(const char* argv0);

}  // namespace ek

#pragma once

#include <filesystem>
#include <string>

namespace ek {
// Starts a bounded, allowlisted HTTP service on all IPv4 interfaces. Port zero disables it.
bool startClientDownload(int port, const std::filesystem::path& executableDirectory,
                         const std::filesystem::path& credentialPath, int gamePort,
                         std::string& error);
}  // namespace ek

#pragma once

#include <optional>
#include <string>
#include <vector>

namespace protocol {

struct Command {
    enum Type { SET, GET, DEL, METRICS, SWITCH_POLICY, PING, SELECT, UNKNOWN };
    Type type = UNKNOWN;
    std::vector<std::string> args;
};

// Legacy line/inine parser: one command per '\n'-terminated line.
Command parse_command(const std::string& line);

// RESP / inline frame parser. Tries to parse exactly one command from the
// front of `buf`. On success returns the Command and sets `consumed` to the
// number of bytes used. Returns nullopt (and leaves `consumed` untouched) if
// the buffer does not yet hold a complete frame (call again when more data
// arrives). RESP arrays ("*<n>...") are parsed as binary-safe bulk strings;
// anything else falls back to the inline/line protocol.
std::optional<Command> try_parse(const std::string& buf, std::size_t& consumed);

}  // namespace protocol

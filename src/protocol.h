#pragma once

#include <string>
#include <vector>

namespace protocol {

struct Command {
    enum Type { SET, GET, DEL, METRICS, SWITCH_POLICY, UNKNOWN };
    Type type = UNKNOWN;
    std::vector<std::string> args;
};

Command parse_command(const std::string& line);

}  // namespace protocol

#include "protocol.h"

#include <algorithm>
#include <cctype>

using namespace std;

namespace protocol {

namespace {

string trim(const string& s) {
    const auto first = find_if_not(s.begin(), s.end(), [](unsigned char c) {
        return isspace(c) != 0;
    });
    if (first == s.end()) {
        return {};
    }
    const auto last = find_if_not(s.rbegin(), s.rend(), [](unsigned char c) {
        return isspace(c) != 0;
    }).base();
    return string(first, last);
}

vector<string> split(const string& s) {
    vector<string> tokens;
    string current;
    for (char c : s) {
        if (isspace(static_cast<unsigned char>(c))) {
            if (!current.empty()) {
                tokens.push_back(std::move(current));
                current.clear();
            }
        } else {
            current.push_back(c);
        }
    }
    if (!current.empty()) {
        tokens.push_back(std::move(current));
    }
    return tokens;
}

}  // namespace

Command parse_command(const string& line) {
    Command cmd;

    const string trimmed = trim(line);
    if (trimmed.empty()) {
        return cmd;
    }

    vector<string> tokens = split(trimmed);

    string verb = tokens[0];
    transform(verb.begin(), verb.end(), verb.begin(), [](unsigned char c) {
        return static_cast<char>(toupper(c));
    });

    if (verb == "SET") {
        cmd.type = Command::SET;
    } else if (verb == "GET") {
        cmd.type = Command::GET;
    } else if (verb == "DEL") {
        cmd.type = Command::DEL;
    } else if (verb == "METRICS") {
        cmd.type = Command::METRICS;
    } else if (verb == "SWITCH_POLICY") {
        cmd.type = Command::SWITCH_POLICY;
    } else {
        cmd.type = Command::UNKNOWN;
    }

    cmd.args.assign(tokens.begin() + 1, tokens.end());
    return cmd;
}

}  // namespace protocol

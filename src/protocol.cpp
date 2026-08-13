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

Command classify(const string& verb, vector<string> args) {
    Command cmd;
    string v = verb;
    transform(v.begin(), v.end(), v.begin(), [](unsigned char c) {
        return static_cast<char>(toupper(c));
    });
    if (v == "SET") {
        cmd.type = Command::SET;
    } else if (v == "GET") {
        cmd.type = Command::GET;
    } else if (v == "DEL") {
        cmd.type = Command::DEL;
    } else if (v == "METRICS") {
        cmd.type = Command::METRICS;
    } else if (v == "SWITCH_POLICY") {
        cmd.type = Command::SWITCH_POLICY;
    } else if (v == "PING") {
        cmd.type = Command::PING;
    } else if (v == "SELECT") {
        cmd.type = Command::SELECT;
    } else {
        cmd.type = Command::UNKNOWN;
    }
    cmd.args = std::move(args);
    return cmd;
}

// Parse a RESP2 array of bulk strings: *<n>\r\n then n x ($<len>\r\n<data>\r\n).
// Returns nullopt if the frame is incomplete, else a Command built from the
// array elements (first element = verb, rest = args).
// Safety: caps argument count and bulk length to prevent DoS via giant
// allocations or size_t overflow.
optional<Command> parse_resp(const string& buf, size_t& consumed) {
    static constexpr long kMaxArgs = 1024;
    size_t i = 0;
    if (i >= buf.size() || buf[i] != '*') {
        return nullopt;
    }
    ++i;
    const size_t nl = buf.find('\n', i);
    if (nl == string::npos) {
        return nullopt;
    }
    size_t num_end = nl;
    if (num_end > i && buf[num_end - 1] == '\r') {
        --num_end;
    }
    long count = 0;
    try {
        count = stol(buf.substr(i, num_end - i));
    } catch (...) {
        return nullopt;
    }
    i = nl + 1;
    if (count <= 0) {
        consumed = i;
        return classify("", {});
    }
    if (count > kMaxArgs) {
        return nullopt;
    }

    vector<string> args;
    args.reserve(static_cast<size_t>(count));
    for (long a = 0; a < count; ++a) {
        if (i >= buf.size() || buf[i] != '$') {
            return nullopt;
        }
        ++i;
        const size_t lend = buf.find('\n', i);
        if (lend == string::npos) {
            return nullopt;
        }
        size_t e = lend;
        if (e > i && buf[e - 1] == '\r') {
            --e;
        }
        long len = 0;
        try {
            len = stol(buf.substr(i, e - i));
        } catch (...) {
            return nullopt;
        }
        i = lend + 1;
        if (len < 0) {
            return nullopt;
        }
        const size_t need = static_cast<size_t>(len);
        if (need > buf.size()) {
            return nullopt;
        }
        if (i + need + 2 > buf.size()) {
            return nullopt;
        }
        args.push_back(buf.substr(i, need));
        i += need;
        if (buf[i] == '\r') {
            ++i;
        }
        if (i < buf.size() && buf[i] == '\n') {
            ++i;
        } else {
            return nullopt;
        }
    }

    consumed = i;
    if (args.empty()) {
        return classify("", {});
    }
    string verb = args[0];
    vector<string> rest(args.begin() + 1, args.end());
    return classify(verb, std::move(rest));
}

}  // namespace

optional<Command> try_parse(const string& buf, size_t& consumed) {
    if (buf.empty()) {
        return nullopt;
    }
    if (buf[0] == '*') {
        return parse_resp(buf, consumed);
    }
    const size_t nl = buf.find('\n');
    if (nl == string::npos) {
        return nullopt;
    }
    string line = buf.substr(0, nl);
    if (!line.empty() && line.back() == '\r') {
        line.pop_back();
    }
    consumed = nl + 1;
    return parse_command(line);
}

Command parse_command(const string& line) {
    const string trimmed = trim(line);
    if (trimmed.empty()) {
        return classify("", {});
    }
    vector<string> tokens = split(trimmed);
    if (tokens.empty()) {
        return classify("", {});
    }
    const string verb = tokens[0];
    vector<string> rest(tokens.begin() + 1, tokens.end());
    return classify(verb, std::move(rest));
}

}  // namespace protocol

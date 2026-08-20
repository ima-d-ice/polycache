#include "protocol.h"

#include <algorithm>
#include <cctype>

using namespace std;

namespace protocol {

namespace {

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

}  // namespace

optional<Command> try_parse(const string& buf, size_t& consumed) {
    static constexpr long kMaxArgs = 1024;  // DoS cap on argument count
    size_t i = 0;
    if (buf.empty() || buf[i] != '*') {
        return nullopt;
    }
    ++i;
    const size_t nl = buf.find('\n', i);
    if (nl == string::npos) {
        return nullopt;  // not a complete array header yet
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
        return nullopt;  // DoS guard: absurd argument count
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
            return nullopt;  // not a complete length line yet
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
            return nullopt;  // $-1 (null) is not a valid command argument
        }
        const size_t need = static_cast<size_t>(len);
        if (need > buf.size()) {
            return nullopt;  // DoS guard: declared length exceeds the buffer
        }
        if (i + need + 2 > buf.size()) {
            return nullopt;  // payload (or its CRLF) not fully buffered yet
        }
        args.push_back(buf.substr(i, need));  // verbatim copy = binary-safe
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

}  // namespace protocol
#include "protocol.h"

#include <algorithm>
#include <cctype>
#include <cstring>
#include <utility>

using namespace std;

namespace protocol {

namespace {

enum class LineStatus { NeedMore, Ok, Bad };

// Reads one CRLF/LF-terminated header line into `out`. Headers are tiny
// (argument counts and bulk lengths), so a 32-byte ceiling is generous and
// keeps a hostile peer from growing it without bound.
LineStatus read_line(const char*& data, size_t& len, string& out) {
    while (len > 0) {
        const char c = *data;
        ++data;
        --len;
        if (c == '\n') {
            if (!out.empty() && out.back() == '\r') {
                out.pop_back();
            }
            return LineStatus::Ok;
        }
        out.push_back(c);
        if (out.size() > 32) {
            return LineStatus::Bad;
        }
    }
    return LineStatus::NeedMore;
}

bool parse_uint(const string& text, long& value) {
    if (text.empty() || text.size() > 18) {
        return false;
    }
    unsigned long long v = 0;
    for (const char c : text) {
        if (c < '0' || c > '9') {
            return false;
        }
        v = v * 10 + static_cast<unsigned long long>(c - '0');
    }
    value = static_cast<long>(v);
    return true;
}

Command classify(vector<string>&& argv) {
    Command cmd;
    if (argv.empty()) {
        return cmd;
    }
    cmd.verb = std::move(argv.front());
    argv.erase(argv.begin());

    string upper = cmd.verb;
    transform(upper.begin(), upper.end(), upper.begin(), [](unsigned char c) {
        return static_cast<char>(toupper(c));
    });
    if (upper == "SET") {
        cmd.type = Command::SET;
    } else if (upper == "GET") {
        cmd.type = Command::GET;
    } else if (upper == "DEL") {
        cmd.type = Command::DEL;
    } else if (upper == "METRICS") {
        cmd.type = Command::METRICS;
    } else if (upper == "PING") {
        cmd.type = Command::PING;
    } else if (upper == "SELECT") {
        cmd.type = Command::SELECT;
    } else {
        cmd.type = Command::UNKNOWN;
    }
    cmd.args = std::move(argv);
    return cmd;
}

}  // namespace

Parser::Parser(size_t max_bulk_len) : max_bulk_len_(max_bulk_len) {}

Parser::Status Parser::fail(string message) {
    state_ = State::Failed;
    error_ = std::move(message);
    return Status::Error;
}

Parser::Status Parser::consume(const char*& data, size_t& len, Command& out) {
    for (;;) {
        switch (state_) {
            case State::Ready:
                out = classify(std::move(args_));
                args_.clear();
                state_ = State::ArrayLen;
                return Status::Ready;

            case State::Failed:
                return Status::Error;

            case State::ArrayLen: {
                const LineStatus ls = read_line(data, len, header_);
                if (ls == LineStatus::NeedMore) {
                    return Status::NeedMore;
                }
                if (ls == LineStatus::Bad) {
                    return fail("invalid array header");
                }
                long count = 0;
                if (header_.empty() || header_[0] != '*' ||
                    !parse_uint(header_.substr(1), count)) {
                    header_.clear();
                    return fail("invalid array length");
                }
                header_.clear();
                if (count > kMaxArgs) {
                    return fail("too many arguments");
                }
                argc_ = count;
                arg_index_ = 0;
                args_.clear();
                if (count == 0) {
                    state_ = State::Ready;
                } else {
                    args_.reserve(static_cast<size_t>(count));
                    state_ = State::BulkLen;
                }
                continue;
            }

            case State::BulkLen: {
                const LineStatus ls = read_line(data, len, header_);
                if (ls == LineStatus::NeedMore) {
                    return Status::NeedMore;
                }
                if (ls == LineStatus::Bad) {
                    return fail("invalid bulk header");
                }
                long declared = 0;
                if (header_.empty() || header_[0] != '$' ||
                    !parse_uint(header_.substr(1), declared)) {
                    header_.clear();
                    return fail("invalid bulk length");
                }
                header_.clear();
                const size_t bulk_len = static_cast<size_t>(declared);
                if (bulk_len > max_bulk_len_) {
                    return fail("bulk length exceeds max-value-size");
                }
                try {
                    args_.emplace_back();
                    args_.back().resize(bulk_len);
                } catch (const std::bad_alloc&) {
                    return fail("out of memory");
                }
                bulk_len_ = bulk_len;
                bulk_read_ = 0;
                state_ = bulk_len == 0 ? State::Trailer : State::Payload;
                continue;
            }

            case State::Payload: {
                const size_t need = bulk_len_ - bulk_read_;
                const size_t take = std::min(need, len);
                if (take > 0) {
                    memcpy(args_.back().data() + bulk_read_, data, take);
                    data += take;
                    len -= take;
                    bulk_read_ += take;
                }
                if (bulk_read_ < bulk_len_) {
                    return Status::NeedMore;
                }
                state_ = State::Trailer;
                continue;
            }

            case State::Trailer: {
                if (len == 0) {
                    return Status::NeedMore;
                }
                const char c = *data;
                ++data;
                --len;
                if (c == '\r') {
                    state_ = State::TrailerLf;
                } else if (c == '\n') {
                    ++arg_index_;
                    state_ = arg_index_ < argc_ ? State::BulkLen : State::Ready;
                } else {
                    return fail("expected CRLF after bulk payload");
                }
                continue;
            }

            case State::TrailerLf: {
                if (len == 0) {
                    return Status::NeedMore;
                }
                const char c = *data;
                ++data;
                --len;
                if (c != '\n') {
                    return fail("expected LF after CR");
                }
                ++arg_index_;
                state_ = arg_index_ < argc_ ? State::BulkLen : State::Ready;
                continue;
            }
        }
    }
}

}  // namespace protocol

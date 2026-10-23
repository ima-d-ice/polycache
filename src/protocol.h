#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace protocol {

struct Command {
    enum Type : std::uint8_t { SET, GET, DEL, METRICS, PING, SELECT, UNKNOWN };
    Type type = UNKNOWN;
    std::string verb;              // original verb, for UNKNOWN/error replies
    std::vector<std::string> args; // arguments only (verb excluded)
};

// Incremental RESP2 parser (the Redis wire protocol).
//
// `consume()` reads as much as it can from a caller-owned byte range and
// either produces one complete Command (Status::Ready), asks for more bytes
// (Status::NeedMore), or reports a protocol error (Status::Error). Bulk
// payloads are written straight into the argument string, sized once from the
// declared length, so a 512MB SET never goes through an intermediate frame
// buffer or a second full copy. The parser keeps only the small in-progress
// header and the current argument between calls; pipelined frames left in the
// caller's buffer are handled by calling `consume()` again.
class Parser {
public:
    static constexpr std::size_t kDefaultMaxBulkLen = std::size_t{512} * 1024 * 1024;
    static constexpr long kMaxArgs = 1024;

    explicit Parser(std::size_t max_bulk_len = kDefaultMaxBulkLen);

    enum class Status { NeedMore, Ready, Error };

    Status consume(const char*& data, std::size_t& len, Command& out);

    const std::string& error() const { return error_; }
    std::size_t max_bulk_len() const { return max_bulk_len_; }

private:
    enum class State : std::uint8_t {
        ArrayLen,
        BulkLen,
        Payload,
        Trailer,
        TrailerLf,
        Ready,
        Failed
    };

    Status fail(std::string message);

    std::size_t max_bulk_len_;
    State state_ = State::ArrayLen;
    std::string header_;
    std::string error_;
    long argc_ = 0;
    long arg_index_ = 0;
    std::size_t bulk_len_ = 0;
    std::size_t bulk_read_ = 0;
    std::vector<std::string> args_;
};

}  // namespace protocol

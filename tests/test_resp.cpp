#include "protocol.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <optional>
#include <string>
#include <vector>

#define CHECK(cond)                                                    \
    do {                                                               \
        if (!(cond)) {                                                 \
            std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__,         \
                         __LINE__, #cond);                             \
            return 1;                                                  \
        }                                                              \
    } while (0)

using protocol::Command;

static Command parse(const std::string& buf, size_t& consumed) {
    auto c = protocol::try_parse(buf, consumed);
    if (!c) {
        std::fprintf(stderr, "FAIL %s:%d: try_parse returned nullopt\n",
                     __FILE__, __LINE__);
        std::exit(1);
    }
    return *c;
}

int main() {
    // RESP SET array (binary-safe bulk strings).
    {
        const std::string f = "*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n$3\r\nbar\r\n";
        size_t consumed = 0;
        auto c = parse(f, consumed);
        CHECK(consumed == f.size());
        CHECK(c.type == Command::SET);
        CHECK(c.args.size() == 2);
        CHECK(c.args[0] == "foo");
        CHECK(c.args[1] == "bar");
    }

    // RESP GET.
    {
        const std::string f = "*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n";
        size_t consumed = 0;
        auto c = parse(f, consumed);
        CHECK(consumed == f.size());
        CHECK(c.type == Command::GET);
        CHECK(c.args.size() == 1);
        CHECK(c.args[0] == "foo");
    }

    // RESP PING (used by redis-benchmark).
    {
        const std::string f = "*1\r\n$4\r\nPING\r\n";
        size_t consumed = 0;
        auto c = parse(f, consumed);
        CHECK(c.type == Command::PING);
        CHECK(c.args.empty());
    }

    // RESP SELECT (ignored by server, must parse).
    {
        const std::string f = "*2\r\n$6\r\nSELECT\r\n$1\r\n0\r\n";
        size_t consumed = 0;
        auto c = parse(f, consumed);
        CHECK(c.type == Command::SELECT);
        CHECK(c.args.size() == 1 && c.args[0] == "0");
    }

    // Binary-safe: key/value may contain spaces and newlines.
    {
        const std::string key = "k y";
        const std::string val = "a\nb";
        std::string f = "*3\r\n$3\r\nSET\r\n$3\r\n";
        f += key + "\r\n$" + std::to_string(val.size()) + "\r\n" + val + "\r\n";
        size_t consumed = 0;
        auto c = parse(f, consumed);
        CHECK(c.type == Command::SET);
        CHECK(c.args.size() == 2);
        CHECK(c.args[0] == key);
        CHECK(c.args[1] == val);
    }

    // Incomplete RESP frame returns nullopt; consumed untouched.
    {
        const std::string f = "*3\r\n$3\r\nSET\r\n$3\r\nfoo\r\n";  // missing value
        size_t consumed = 123;
        auto c = protocol::try_parse(f, consumed);
        CHECK(!static_cast<bool>(c));
        CHECK(consumed == 123);
    }

    // Pipelined RESP: two frames in one buffer parse sequentially.
    {
        const std::string a = "*2\r\n$3\r\nGET\r\n$3\r\nfoo\r\n";
        const std::string b = "*1\r\n$4\r\nPING\r\n";
        const std::string buf = a + b;
        size_t consumed = 0;
        auto c1 = parse(buf, consumed);
        CHECK(consumed == a.size());
        CHECK(c1.type == Command::GET);
        auto c2 = parse(buf.substr(consumed), consumed);
        CHECK(consumed == b.size());
        CHECK(c2.type == Command::PING);
    }

    std::printf("ok resp (6 groups)\n");
    return 0;
}

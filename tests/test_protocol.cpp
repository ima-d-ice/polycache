#include "protocol.h"

#include <cstdio>
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

int main() {
    // SET with trailing whitespace + CRLF.
    {
        auto c = protocol::parse_command("SET foo bar 60\r\n");
        CHECK(c.type == Command::SET);
        CHECK(c.args.size() == 3);
        CHECK(c.args[0] == "foo");
        CHECK(c.args[1] == "bar");
        CHECK(c.args[2] == "60");
    }

    // Verb is case-insensitive; args preserved verbatim.
    {
        auto c = protocol::parse_command("sEt Foo BAr");
        CHECK(c.type == Command::SET);
        CHECK(c.args.size() == 2);
        CHECK(c.args[0] == "Foo");
        CHECK(c.args[1] == "BAr");
    }

    // Leading whitespace + tabs split.
    {
        auto c = protocol::parse_command("  \tGET\tfoo\r\n");
        CHECK(c.type == Command::GET);
        CHECK(c.args.size() == 1);
        CHECK(c.args[0] == "foo");
    }

    // DEL keeps all tokens.
    {
        auto c = protocol::parse_command("DEL alpha");
        CHECK(c.type == Command::DEL);
        CHECK(c.args.size() == 1);
        CHECK(c.args[0] == "alpha");
    }

    // METRICS has no args.
    {
        auto c = protocol::parse_command("METRICS");
        CHECK(c.type == Command::METRICS);
        CHECK(c.args.empty());
    }

    // SWITCH_POLICY keeps the policy token.
    {
        auto c = protocol::parse_command("SWITCH_POLICY sieve");
        CHECK(c.type == Command::SWITCH_POLICY);
        CHECK(c.args.size() == 1);
        CHECK(c.args[0] == "sieve");
    }

    // Empty and whitespace-only lines parse to UNKNOWN with no args.
    {
        auto c1 = protocol::parse_command("");
        CHECK(c1.type == Command::UNKNOWN);
        CHECK(c1.args.empty());
        auto c2 = protocol::parse_command("   \t\r\n");
        CHECK(c2.type == Command::UNKNOWN);
        CHECK(c2.args.empty());
    }

    // Unknown verb -> UNKNOWN, args still captured.
    {
        auto c = protocol::parse_command("BANANA x y");
        CHECK(c.type == Command::UNKNOWN);
        CHECK(c.args.size() == 2);
    }

    std::printf("ok protocol (9 groups)\n");
    return 0;
}
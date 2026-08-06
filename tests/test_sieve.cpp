#include "eviction/sieve.h"

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

int main() {
    // 1. Evict on an empty cache -> empty string.
    {
        SIEVE s;
        CHECK(s.evict() == "");
    }

    // 2. Memory accounting: add %s size, updates on re-add, subtract on remove.
    {
        SIEVE s;
        s.add("a", 10);
        CHECK(s.memory_used() == 1 + 10);
        s.add("b", 20);
        CHECK(s.memory_used() == 1 + 10 + 1 + 20);
        s.remove("b");
        CHECK(s.memory_used() == 1 + 10);
        s.add("c", 5);
        CHECK(s.memory_used() == 1 + 10 + 1 + 5);
    }

    // 3. No touches -> eviction order matches insertion (oldest first).
    {
        SIEVE s;
        const std::vector<std::string> keys = {"a", "b", "c", "d", "e"};
        for (const auto& k : keys) {
            s.add(k, 0);
        }
        for (const auto& k : keys) {
            CHECK(s.evict() == k);
        }
        CHECK(s.memory_used() == 0);
        CHECK(s.evict() == "");
    }

    // 4. touch() (visited flag) delays a key past the next unvisited one.
    //    add order (oldest -> newest): a, b, c, d. touch c.
    //    evict walks newest-ward clearing visited flags and takes the first
    //    unvisited victim, so the sequence is a, b, d, c.
    {
        SIEVE s;
        s.add("a", 0);
        s.add("b", 0);
        s.add("c", 0);
        s.add("d", 0);
        s.touch("c");
        CHECK(s.evict() == "a");
        CHECK(s.evict() == "b");
        CHECK(s.evict() == "d");
        CHECK(s.evict() == "c");
        CHECK(s.evict() == "");
    }

    // 5. remove() from the middle keeps the chain intact.
    {
        SIEVE s;
        s.add("a", 0);
        s.add("b", 0);
        s.add("c", 0);
        s.remove("b");
        CHECK(s.evict() == "a");
        CHECK(s.evict() == "c");
        CHECK(s.evict() == "");
    }

    // 6. Re-adding an existing key updates its size without duplicating it.
    {
        SIEVE s;
        s.add("k", 100);
        CHECK(s.memory_used() == 1 + 100);
        s.add("k", 25);
        CHECK(s.memory_used() == 1 + 25);
        CHECK(s.evict() == "k");
        CHECK(s.evict() == "");
    }

    std::printf("ok sieve (6 groups)\n");
    return 0;
}
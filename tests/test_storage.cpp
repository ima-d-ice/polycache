#include "storage.h"

#include <cmath>
#include <cstdio>
#include <string>
#include <thread>

#define CHECK(cond)                                                    \
    do {                                                               \
        if (!(cond)) {                                                 \
            std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__,         \
                         __LINE__, #cond);                             \
            return 1;                                                  \
        }                                                              \
    } while (0)

int main() {
    // 1. Set/get/del + hit/miss counters.
    {
        Storage st;
        CHECK(!st.get("nope").has_value());

        auto m = st.metrics();
        CHECK(m["total_keys"] == 0);
        CHECK(m["misses"] == 1);
        CHECK(m["hits"] == 0);
        CHECK(std::abs(m["hit_rate"].get<double>() - 0.0) < 1e-6);

        st.set("foo", "bar", 0);
        CHECK(st.get("foo").value() == "bar");

        m = st.metrics();
        CHECK(m["total_keys"] == 1);
        CHECK(m["memory_bytes"] == 3 + 3);
        CHECK(m["hits"] == 1);
        CHECK(m["misses"] == 1);
        CHECK(std::abs(m["hit_rate"].get<double>() - 0.5) < 1e-6);

        CHECK(st.del("foo") == true);
        CHECK(st.del("foo") == false);
        CHECK(!st.get("foo").has_value());
    }

    // 2. Deterministic eviction under a small memory limit.
    //    Each entry is key.size() + value.size() = 2 + 5 = 7 bytes; a limit of
    //    20 fits 2 entries, so the 3rd SET must evict exactly once.
    {
        Storage st(20);
        for (int i = 0; i < 3; ++i) {
            st.set("k" + std::to_string(i), std::string(5, 'x'), 0);
        }
        auto m = st.metrics();
        CHECK(m["evictions"] == 1);
        CHECK(m["total_keys"] == 2);
        CHECK(m["memory_bytes"] == 14);
    }

    // 3. switch_policy: case-insensitive, correct names only, keys preserved.
    {
        Storage st;
        st.set("z", "1", 0);
        CHECK(st.switch_policy("LFU") == true);
        CHECK(st.metrics()["policy"] == "lfu");
        CHECK(st.get("z").value() == "1");
        CHECK(st.switch_policy("fifo") == false);
        CHECK(st.metrics()["policy"] == "lfu");
        CHECK(st.switch_policy("sieVE") == true);
        CHECK(st.metrics()["policy"] == "sieve");
        CHECK(st.get("z").value() == "1");
    }

    // 4. TTL: value present before expiry, gone after.
    //    ttl=2s, generous 2.5s sleep so name/thread timing never flakes.
    {
        Storage st;
        st.set("tt", "hello", 2);
        CHECK(st.get("tt").value() == "hello");
        std::this_thread::sleep_for(std::chrono::milliseconds(2500));
        CHECK(!st.get("tt").has_value());
    }

    std::printf("ok storage (4 groups)\n");
    return 0;
}
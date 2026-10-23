#include "storage.h"

#include <cassert>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>

using namespace std;

namespace {

constexpr size_t kMB = 1024 * 1024;

string make_blob(size_t size, unsigned seed) {
    string blob;
    blob.resize(size);
    for (size_t i = 0; i < size; ++i) {
        blob[i] = static_cast<char>((i * 31 + seed) & 0xFF);
    }
    return blob;
}

void test_binary_roundtrip() {
    Storage storage(8 * kMB, 4 * kMB);
    const string blob = make_blob(kMB, 7);
    assert(storage.set("img:1", blob, 0));

    const Blob got = storage.get("img:1");
    assert(got != nullptr);
    assert(got->size() == blob.size());
    assert(memcmp(got->data(), blob.data(), blob.size()) == 0);

    const auto metrics = storage.metrics();
    assert(metrics["total_keys"] == 1);
    assert(metrics["largest_item_bytes"] == kMB);
    assert(metrics["used_memory"].get<size_t>() >= kMB);
}

void test_size_limits() {
    Storage storage(8 * kMB, 4 * kMB);
    const string too_big = make_blob(5 * kMB, 1);
    assert(!storage.can_store("big", too_big));
    assert(!storage.set("big", too_big, 0));

    const string big_key(Storage::kMaxKeySize + 1, 'k');
    assert(!storage.set(big_key, "v", 0));

    const string fits = make_blob(4 * kMB, 2);
    assert(storage.can_store("fits", fits));
    assert(storage.set("fits", fits, 0));
    assert(storage.get("fits") != nullptr);
}

void test_lru_eviction() {
    Storage storage(2 * kMB, 1 * kMB);
    const string blob = make_blob(kMB, 3);
    assert(storage.set("a", blob, 0));
    assert(storage.set("b", blob, 0));

    const auto metrics = storage.metrics();
    assert(metrics["evictions"] == 1);
    assert(metrics["total_keys"] == 1);
    assert(storage.get("a") == nullptr);
    assert(storage.get("b") != nullptr);
}

void test_empty_key_eviction() {
    // Regression: evict() returning an empty key used to be treated as "no
    // victim", which broke eviction whenever the key itself was empty.
    Storage storage(256, 200);
    assert(storage.set("", string(100, 'a'), 0));
    assert(storage.set("b", string(100, 'b'), 0));
    assert(storage.get("") == nullptr);
    assert(storage.get("b") != nullptr);
}

void test_ttl() {
    Storage storage(8 * kMB, 4 * kMB);
    assert(storage.set("short", "lived", 1));
    assert(storage.get("short") != nullptr);
    this_thread::sleep_for(chrono::milliseconds(1200));
    assert(storage.get("short") == nullptr);
    assert(storage.metrics()["expired_keys"].get<size_t>() >= 1);
}

void test_overwrite_accounting() {
    Storage storage(8 * kMB, 4 * kMB);
    assert(storage.set("k", string(kMB, 'a'), 0));
    const size_t first = storage.metrics()["used_memory"].get<size_t>();
    assert(storage.set("k", string(2 * kMB, 'b'), 0));
    const size_t second = storage.metrics()["used_memory"].get<size_t>();
    assert(second > first);
    assert(storage.metrics()["total_keys"] == 1);
    assert(storage.get("k")->size() == 2 * kMB);
}

}  // namespace

int main() {
    test_binary_roundtrip();
    test_size_limits();
    test_lru_eviction();
    test_empty_key_eviction();
    test_ttl();
    test_overwrite_accounting();
    printf("ok test_storage\n");
    return 0;
}

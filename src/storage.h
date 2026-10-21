#pragma once

#include "eviction/policy.h"
#include "third_party/nlohmann/json.hpp"
#include "ttl.h"

#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>

// Values are shared so a response can stream the stored bytes without copying
// them, and so an evicted key stays alive until an in-flight GET finishes.
using Blob = std::shared_ptr<const std::string>;

// Per-key metadata that survives across policy switches so a rebuild can
// preserve the eviction frontier instead of scattering it in hash order.
struct KeyMeta {
    Blob value;
    std::size_t size = 0;                 // bytes handed to the eviction policy
    uint64_t insert_seq = 0;              // global order of first insertion
    uint64_t last_access_seq = 0;         // global order of last set/get (recency)
    std::size_t freq = 0;                 // access count (for LFU)
    bool visited = false;                 // SIEVE visited bit
};

class Storage {
public:
    static constexpr std::size_t kDefaultMemoryLimit = std::size_t{1024} * 1024 * 1024;
    static constexpr std::size_t kDefaultMaxValueSize = std::size_t{512} * 1024 * 1024;
    static constexpr std::size_t kMaxKeySize = std::size_t{1} * 1024 * 1024;
    // KeyMeta + hash node + shared_ptr control block + allocator slack. The
    // limit is a cache budget, not a promise about RSS; this keeps it close.
    static constexpr std::size_t kEntryOverhead = 128;

    explicit Storage(std::size_t memory_limit = kDefaultMemoryLimit,
                     std::size_t max_value_size = kDefaultMaxValueSize);
    ~Storage() = default;

    // Returns false when the key/value is too large for the configured limits.
    bool set(std::string key, std::string value, int ttl_sec);
    bool can_store(const std::string& key, const std::string& value) const;
    Blob get(const std::string& key);
    bool del(const std::string& key);
    bool switch_policy(const std::string& name);
    nlohmann::json metrics() const;

    std::size_t memory_limit() const { return memory_limit_; }
    std::size_t max_value_size() const { return max_value_size_; }

private:
    void evict_if_over_limit();
    void expire_key(const std::string& key);
    std::size_t used_memory() const;
    std::size_t bucket_bytes() const;

    std::unordered_map<std::string, KeyMeta> data_;
    std::unique_ptr<EvictionPolicy> policy_;
    mutable std::mutex lock_;
    TTLManager ttl_;
    uint64_t hits_ = 0;
    uint64_t misses_ = 0;
    uint64_t evictions_ = 0;
    uint64_t expired_keys_ = 0;
    std::size_t value_bytes_ = 0;         // current payload bytes (no metadata)
    std::size_t largest_item_bytes_ = 0;  // largest payload seen since boot
    std::size_t used_memory_peak_ = 0;
    std::string policy_name_ = "lru";
    std::size_t memory_limit_;
    std::size_t max_value_size_;
    uint64_t access_seq_ = 0;             // monotonic counter for recency/frequency
};

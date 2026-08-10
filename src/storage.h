#pragma once

#include "eviction/policy.h"
#include "third_party/nlohmann/json.hpp"
#include "ttl.h"

#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>

class Storage {
public:
    explicit Storage(std::size_t memory_limit = 64 * 1024 * 1024);
    ~Storage() = default;

    void set(const std::string& key, const std::string& value, int ttl_sec);
    std::optional<std::string> get(const std::string& key);
    bool del(const std::string& key);
    bool switch_policy(const std::string& name);
    void mark_preloaded();
    nlohmann::json metrics() const;

private:
    void evict_if_over_limit();
    void expire_key(const std::string& key);

    std::unordered_map<std::string, std::string> data_;
    std::unique_ptr<EvictionPolicy> policy_;
    mutable std::mutex lock_;
    TTLManager ttl_;
    std::size_t hits_ = 0;
    std::size_t misses_ = 0;
    std::size_t evictions_ = 0;
    std::string policy_name_ = "lru";
    std::size_t memory_limit_;
    bool preload_complete_ = false;
};

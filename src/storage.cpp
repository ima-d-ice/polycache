#include "storage.h"

#include "eviction/lfu.h"
#include "eviction/lru.h"
#include "eviction/sieve.h"

#include <algorithm>
#include <cctype>

using namespace std;

Storage::Storage(size_t memory_limit)
    : policy_(make_unique<LRU>()),
      ttl_([this](const string& key) { expire_key(key); }),
      memory_limit_(memory_limit) {}

void Storage::set(const string& key, const string& value, int ttl_sec) {
    lock_guard<mutex> lk(lock_);
    data_[key] = value;
    policy_->add(key, value.size());
    if (ttl_sec > 0) {
        ttl_.set_ttl(key, chrono::seconds(ttl_sec));
    } else {
        ttl_.erase(key);
    }
    evict_if_over_limit();
}

optional<string> Storage::get(const string& key) {
    lock_guard<mutex> lk(lock_);
    if (ttl_.is_expired(key)) {
        data_.erase(key);
        policy_->remove(key);
        ++misses_;
        return nullopt;
    }
    auto it = data_.find(key);
    if (it == data_.end()) {
        ++misses_;
        return nullopt;
    }
    policy_->touch(key);
    ++hits_;
    return it->second;
}

bool Storage::del(const string& key) {
    lock_guard<mutex> lk(lock_);
    auto it = data_.find(key);
    if (it == data_.end()) {
        return false;
    }
    data_.erase(it);
    policy_->remove(key);
    ttl_.erase(key);
    return true;
}

bool Storage::switch_policy(const string& name) {
    lock_guard<mutex> lk(lock_);
    string lower = name;
    transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) {
        return static_cast<char>(tolower(c));
    });
    unique_ptr<EvictionPolicy> next;
    if (lower == "lru") {
        next = make_unique<LRU>();
    } else if (lower == "lfu") {
        next = make_unique<LFU>();
    } else if (lower == "sieve") {
        next = make_unique<SIEVE>();
    } else {
        return false;
    }
    for (const auto& [key, value] : data_) {
        next->add(key, value.size());
    }
    policy_ = std::move(next);
    policy_name_ = lower;
    return true;
}

nlohmann::json Storage::metrics() const {
    lock_guard<mutex> lk(lock_);
    nlohmann::json j;
    j["total_keys"] = static_cast<int>(data_.size());
    j["memory_bytes"] = static_cast<int>(policy_->memory_used());
    j["policy"] = policy_name_;
    j["hits"] = static_cast<int>(hits_);
    j["misses"] = static_cast<int>(misses_);
    j["evictions"] = static_cast<int>(evictions_);
    const auto total = static_cast<float>(hits_ + misses_);
    j["hit_rate"] = total > 0.0f ? static_cast<float>(hits_) / total : 0.0f;
    j["miss_rate"] = total > 0.0f ? static_cast<float>(misses_) / total : 0.0f;
    return j;
}

void Storage::evict_if_over_limit() {
    while (policy_->memory_used() > memory_limit_) {
        const string victim = policy_->evict();
        if (victim.empty()) {
            break;
        }
        data_.erase(victim);
        ttl_.erase(victim);
        ++evictions_;
    }
}

void Storage::expire_key(const string& key) {
    lock_guard<mutex> lk(lock_);
    data_.erase(key);
    policy_->remove(key);
}

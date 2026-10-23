#include "storage.h"

#include "eviction/lfu.h"
#include "eviction/lru.h"
#include "eviction/sieve.h"

#include <algorithm>
#include <cctype>
#include <utility>
#include <vector>

using namespace std;

Storage::Storage(size_t memory_limit, size_t max_value_size)
    : policy_(make_unique<LRU>()),
      ttl_([this](const string& key) { expire_key(key); }),
      memory_limit_(memory_limit),
      max_value_size_(max_value_size) {}

bool Storage::can_store(const string& key, const string& value) const {
    if (key.size() > kMaxKeySize || value.size() > max_value_size_) {
        return false;
    }
    // Account for the allocation, not just the logical length: a std::string
    // can keep spare capacity, and every entry carries metadata and a hash
    // node that the eviction policy must know about to honor the budget.
    const size_t accounted = value.capacity() + kEntryOverhead;
    return accounted + key.size() <= memory_limit_;
}

bool Storage::set(string key, string value, int ttl_sec) {
    if (!can_store(key, value)) {
        return false;
    }

    const size_t payload = value.size();
    const size_t accounted = value.capacity() + kEntryOverhead;

    Blob blob = make_shared<const string>(std::move(value));

    lock_guard<mutex> lk(lock_);
    auto it = data_.find(key);
    if (it == data_.end()) {
        KeyMeta meta;
        meta.value = std::move(blob);
        meta.size = accounted;

        value_bytes_ += payload;
        largest_item_bytes_ = max(largest_item_bytes_, payload);

        auto inserted = data_.emplace(key, std::move(meta));
        policy_->add(inserted.first->first, accounted);
    } else {
        KeyMeta& meta = it->second;
        value_bytes_ -= meta.value ? meta.value->size() : 0;
        value_bytes_ += payload;
        largest_item_bytes_ = max(largest_item_bytes_, payload);

        meta.value = std::move(blob);
        meta.size = accounted;
        policy_->add(it->first, accounted);
    }

    if (ttl_sec > 0) {
        ttl_.set_ttl(key, chrono::seconds(ttl_sec));
    } else {
        ttl_.erase(key);
    }

    // Capture the transient high-water mark before eviction drains the cache.
    used_memory_peak_ = max(used_memory_peak_, used_memory());
    evict_if_over_limit();
    return true;
}

Blob Storage::get(const string& key) {
    lock_guard<mutex> lk(lock_);
    if (ttl_.is_expired(key)) {
        auto it = data_.find(key);
        if (it != data_.end()) {
            value_bytes_ -= it->second.value ? it->second.value->size() : 0;
            data_.erase(it);
            policy_->remove(key);
        }
        ++expired_keys_;
        ++misses_;
        return nullptr;
    }

    auto it = data_.find(key);
    if (it == data_.end()) {
        ++misses_;
        return nullptr;
    }

    policy_->touch(key);
    ++hits_;
    return it->second.value;
}

bool Storage::del(const string& key) {
    lock_guard<mutex> lk(lock_);
    auto it = data_.find(key);
    if (it == data_.end()) {
        return false;
    }
    value_bytes_ -= it->second.value ? it->second.value->size() : 0;
    data_.erase(it);
    policy_->remove(key);
    ttl_.erase(key);
    return true;
}

nlohmann::json Storage::metrics() const {
    lock_guard<mutex> lk(lock_);
    nlohmann::json j;
    const size_t keys = data_.size();
    const size_t used = used_memory();
    j["total_keys"] = keys;
    j["memory_bytes"] = used;  // kept for the existing benchmark tooling
    j["used_memory"] = used;
    j["used_memory_peak"] = max(used_memory_peak_, used);
    j["memory_limit"] = memory_limit_;
    j["max_value_size"] = max_value_size_;
    j["hits"] = hits_;
    j["misses"] = misses_;
    j["evictions"] = evictions_;
    j["expired_keys"] = expired_keys_;
    j["avg_item_bytes"] = keys == 0 ? 0 : value_bytes_ / keys;
    j["largest_item_bytes"] = largest_item_bytes_;
    const double total = static_cast<double>(hits_ + misses_);
    j["hit_rate"] = total > 0.0 ? static_cast<double>(hits_) / total : 0.0;
    j["miss_rate"] = total > 0.0 ? static_cast<double>(misses_) / total : 0.0;
    return j;
}

size_t Storage::bucket_bytes() const {
    return data_.bucket_count() * sizeof(void*);
}

size_t Storage::used_memory() const {
    return policy_->memory_used() + bucket_bytes();
}

void Storage::evict_if_over_limit() {
    while (!data_.empty() && used_memory() > memory_limit_) {
        const string victim = policy_->evict();
        auto it = data_.find(victim);
        if (it == data_.end()) {
            break;
        }
        value_bytes_ -= it->second.value ? it->second.value->size() : 0;
        data_.erase(it);
        ttl_.erase(victim);
        ++evictions_;
    }
}

void Storage::expire_key(const string& key) {
    lock_guard<mutex> lk(lock_);
    auto it = data_.find(key);
    if (it == data_.end()) {
        return;
    }
    value_bytes_ -= it->second.value ? it->second.value->size() : 0;
    data_.erase(it);
    policy_->remove(key);
    ++expired_keys_;
}

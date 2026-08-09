#include "lfu.h"

using namespace std;

void LFU::touch(const string& key) {
    auto it = entries_.find(key);
    if (it == entries_.end()) {
        return;
    }
    Entry& e = it->second;
    buckets_[e.freq].erase(e.it);
    if (buckets_[e.freq].empty() && e.freq == min_freq_) {
        ++min_freq_;
    }
    ++e.freq;
    buckets_[e.freq].push_front(key);
    e.it = buckets_[e.freq].begin();
}

void LFU::add(const string& key, size_t size) {
    auto it = entries_.find(key);
    if (it != entries_.end()) {
        Entry& e = it->second;
        memory_used_ -= (key.size() + e.size);
        e.size = size;
        memory_used_ += (key.size() + size);
        return;
    }
    buckets_[1].push_front(key);
    entries_.emplace(key, Entry{size, 1, buckets_[1].begin()});
    min_freq_ = 1;
    memory_used_ += (key.size() + size);
}

void LFU::remove(const string& key) {
    auto it = entries_.find(key);
    if (it == entries_.end()) {
        return;
    }
    Entry& e = it->second;
    buckets_[e.freq].erase(e.it);
    if (buckets_[e.freq].empty() && e.freq == min_freq_) {
        ++min_freq_;
    }
    memory_used_ -= (key.size() + e.size);
    entries_.erase(it);
}

string LFU::evict() {
    if (entries_.empty()) {
        return {};
    }
    while (buckets_[min_freq_].empty()) {
        ++min_freq_;
    }
    list<string>& bucket = buckets_[min_freq_];
    string key = bucket.back();
    bucket.pop_back();
    memory_used_ -= key.size() + entries_[key].size;
    entries_.erase(key);
    return key;
}

size_t LFU::memory_used() const {
    return memory_used_;
}

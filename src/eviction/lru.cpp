#include "lru.h"

using namespace std;

void LRU::touch(const string& key) {
    auto it = index_.find(key);
    if (it == index_.end()) {
        return;
    }
    list_.splice(list_.begin(), list_, it->second);
}

void LRU::add(const string& key, size_t size) {
    auto it = index_.find(key);
    if (it != index_.end()) {
        list_.splice(list_.begin(), list_, it->second);
        memory_used_ -= it->second->key.size() + it->second->size;
        it->second->size = size;
        memory_used_ += key.size() + size;
        return;
    }
    list_.push_front(Entry{key, size});
    index_.emplace(key, list_.begin());
    memory_used_ += key.size() + size;
}

void LRU::remove(const string& key) {
    auto it = index_.find(key);
    if (it == index_.end()) {
        return;
    }
    memory_used_ -= it->second->key.size() + it->second->size;
    list_.erase(it->second);
    index_.erase(it);
}

string LRU::evict() {
    if (list_.empty()) {
        return {};
    }
    auto it = std::prev(list_.end());
    string key = it->key;
    memory_used_ -= it->key.size() + it->size;
    index_.erase(key);
    list_.pop_back();
    return key;
}

size_t LRU::memory_used() const {
    return memory_used_;
}

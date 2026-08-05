#pragma once

#include "policy.h"

#include <list>
#include <string>
#include <unordered_map>

class LRU : public EvictionPolicy {
public:
    LRU() = default;
    ~LRU() override = default;

    void touch(const std::string& key) override;
    void add(const std::string& key, std::size_t size) override;
    void remove(const std::string& key) override;
    std::string evict() override;
    std::size_t memory_used() const override;

private:
    struct Entry {
        std::string key;
        std::size_t size;
    };

    std::list<Entry> list_;
    std::unordered_map<std::string, std::list<Entry>::iterator> index_;
    std::size_t memory_used_ = 0;
};

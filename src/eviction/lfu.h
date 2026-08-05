#pragma once

#include "policy.h"

#include <list>
#include <string>
#include <unordered_map>

class LFU : public EvictionPolicy {
public:
    LFU() = default;
    ~LFU() override = default;

    void touch(const std::string& key) override;
    void add(const std::string& key, std::size_t size) override;
    void remove(const std::string& key) override;
    std::string evict() override;
    std::size_t memory_used() const override;

private:
    struct Entry {
        std::size_t size;
        std::size_t freq;
        std::list<std::string>::iterator it;
    };

    std::unordered_map<std::string, Entry> entries_;
    std::unordered_map<std::size_t, std::list<std::string>> buckets_;
    std::size_t min_freq_ = 0;
    std::size_t memory_used_ = 0;
};

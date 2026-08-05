#pragma once

#include <cstddef>
#include <string>

class EvictionPolicy {
public:
    virtual ~EvictionPolicy() = default;

    virtual void touch(const std::string& key) = 0;
    virtual void add(const std::string& key, std::size_t size) = 0;
    virtual void remove(const std::string& key) = 0;
    virtual std::string evict() = 0;
    virtual std::size_t memory_used() const = 0;
};

#pragma once

#include <cstddef>
#include <string>

class Policy {
public:
    virtual ~Policy() = default;

    virtual void on_access(const std::string& key) = 0;
    virtual std::string evict() = 0;
    virtual std::size_t size() const = 0;
};

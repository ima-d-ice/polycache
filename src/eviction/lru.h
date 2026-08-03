#pragma once

#include "policy.h"

class LRU : public Policy {
public:
    LRU() = default;
    ~LRU() override = default;

    void on_access(const std::string& key) override;
    std::string evict() override;
    std::size_t size() const override;
};

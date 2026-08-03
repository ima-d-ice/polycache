#pragma once

#include "policy.h"

class LFU : public Policy {
public:
    LFU() = default;
    ~LFU() override = default;

    void on_access(const std::string& key) override;
    std::string evict() override;
    std::size_t size() const override;
};

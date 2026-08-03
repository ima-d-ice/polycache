#pragma once

#include "policy.h"

class SIEVE : public Policy {
public:
    SIEVE() = default;
    ~SIEVE() override = default;

    void on_access(const std::string& key) override;
    std::string evict() override;
    std::size_t size() const override;
};

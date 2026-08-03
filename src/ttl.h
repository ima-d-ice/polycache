#pragma once

#include <cstdint>

class TTL {
public:
    TTL() = default;
    ~TTL() = default;

    void set_ttl(std::int64_t millis);
};

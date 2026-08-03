#include "lru.h"

void LRU::on_access(const std::string&) {}

std::string LRU::evict() {
    return {};
}

std::size_t LRU::size() const {
    return 0;
}

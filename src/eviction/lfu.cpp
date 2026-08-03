#include "lfu.h"

void LFU::on_access(const std::string&) {}

std::string LFU::evict() {
    return {};
}

std::size_t LFU::size() const {
    return 0;
}

#include "storage.h"

bool Storage::has(const std::string&) const {
    return false;
}

std::string Storage::get(const std::string&) const {
    return {};
}

void Storage::set(const std::string&, const std::string&) {}

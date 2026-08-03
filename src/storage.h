#pragma once

#include <string>

class Storage {
public:
    Storage() = default;
    ~Storage() = default;

    bool has(const std::string& key) const;
    std::string get(const std::string& key) const;
    void set(const std::string& key, const std::string& value);
};

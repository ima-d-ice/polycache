#pragma once

#include <fstream>
#include <string>

class Storage;

class AOFLogger {
public:
    explicit AOFLogger(const std::string& filename);
    ~AOFLogger() = default;

    void log_set(const std::string& key, const std::string& value, int ttl);
    void log_del(const std::string& key);
    void replay(Storage* storage);

private:
    void append_line(const std::string& line);

    std::string filename_;
    std::ofstream file_;
};

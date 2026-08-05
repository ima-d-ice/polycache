#pragma once

#include <chrono>
#include <condition_variable>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

class TTLManager {
public:
    using OnExpire = std::function<void(const std::string&)>;

    explicit TTLManager(OnExpire on_expire = {});
    ~TTLManager();
    TTLManager(const TTLManager&) = delete;
    TTLManager& operator=(const TTLManager&) = delete;

    void set_ttl(const std::string& key, std::chrono::seconds duration);
    bool is_expired(const std::string& key);
    std::vector<std::string> expired_keys();
    void erase(const std::string& key);
    void clear();

private:
    void sweep_loop();

    std::unordered_map<std::string, std::chrono::steady_clock::time_point> expiries_;
    mutable std::mutex lock_;
    OnExpire on_expire_;
    bool stop_ = false;
    std::condition_variable cv_;
    std::thread thread_;
};

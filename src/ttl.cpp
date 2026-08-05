#include "ttl.h"

using namespace std;

TTLManager::TTLManager(OnExpire on_expire)
    : on_expire_(std::move(on_expire)),
      thread_([this] { sweep_loop(); }) {}

TTLManager::~TTLManager() {
    {
        lock_guard<mutex> lk(lock_);
        stop_ = true;
    }
    cv_.notify_all();
    if (thread_.joinable()) {
        thread_.join();
    }
}

void TTLManager::set_ttl(const string& key, chrono::seconds duration) {
    lock_guard<mutex> lk(lock_);
    if (duration.count() <= 0) {
        expiries_.erase(key);
        return;
    }
    expiries_[key] = chrono::steady_clock::now() + duration;
}

bool TTLManager::is_expired(const string& key) {
    lock_guard<mutex> lk(lock_);
    auto it = expiries_.find(key);
    if (it == expiries_.end()) {
        return false;
    }
    if (it->second <= chrono::steady_clock::now()) {
        expiries_.erase(it);
        return true;
    }
    return false;
}

vector<string> TTLManager::expired_keys() {
    lock_guard<mutex> lk(lock_);
    vector<string> keys;
    const auto now = chrono::steady_clock::now();
    for (auto it = expiries_.begin(); it != expiries_.end();) {
        if (it->second <= now) {
            keys.push_back(it->first);
            it = expiries_.erase(it);
        } else {
            ++it;
        }
    }
    return keys;
}

void TTLManager::erase(const string& key) {
    lock_guard<mutex> lk(lock_);
    expiries_.erase(key);
}

void TTLManager::clear() {
    lock_guard<mutex> lk(lock_);
    expiries_.clear();
}

void TTLManager::sweep_loop() {
    for (;;) {
        {
            unique_lock<mutex> lk(lock_);
            cv_.wait_for(lk, chrono::milliseconds(100), [this] { return stop_; });
            if (stop_) {
                return;
            }
        }
        const auto expired = expired_keys();
        for (const auto& key : expired) {
            if (on_expire_) {
                on_expire_(key);
            }
        }
    }
}

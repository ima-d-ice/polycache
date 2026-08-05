#pragma once

#include "policy.h"

#include <string>
#include <unordered_map>

class SIEVE : public EvictionPolicy {
public:
    SIEVE() = default;
    ~SIEVE() override;

    void touch(const std::string& key) override;
    void add(const std::string& key, std::size_t size) override;
    void remove(const std::string& key) override;
    std::string evict() override;
    std::size_t memory_used() const override;

private:
    struct Node {
        std::string key;
        std::size_t value;
        bool visited;
        Node* prev;
        Node* next;
    };

    Node* head_ = nullptr;
    Node* tail_ = nullptr;
    Node* hand_ = nullptr;
    std::unordered_map<std::string, Node*> index_;
    std::size_t memory_used_ = 0;
};

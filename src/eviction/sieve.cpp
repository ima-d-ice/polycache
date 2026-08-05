#include "sieve.h"

using namespace std;

SIEVE::~SIEVE() {
    Node* n = head_;
    while (n) {
        Node* next = n->next;
        delete n;
        n = next;
    }
}

void SIEVE::touch(const string& key) {
    auto it = index_.find(key);
    if (it == index_.end()) {
        return;
    }
    it->second->visited = true;
}

void SIEVE::add(const string& key, size_t size) {
    auto it = index_.find(key);
    if (it != index_.end()) {
        Node* n = it->second;
        memory_used_ -= n->key.size() + n->value;
        n->value = size;
        memory_used_ += key.size() + size;
        return;
    }
    Node* n = new Node{key, size, false, nullptr, head_};
    if (head_) {
        head_->prev = n;
    }
    head_ = n;
    if (!tail_) {
        tail_ = n;
    }
    index_.emplace(key, n);
    memory_used_ += key.size() + size;
}

void SIEVE::remove(const string& key) {
    auto it = index_.find(key);
    if (it == index_.end()) {
        return;
    }
    Node* n = it->second;
    index_.erase(it);
    memory_used_ -= n->key.size() + n->value;
    if (n->prev) {
        n->prev->next = n->next;
    } else {
        head_ = n->next;
    }
    if (n->next) {
        n->next->prev = n->prev;
    } else {
        tail_ = n->prev;
    }
    if (hand_ == n) {
        hand_ = n->prev;
    }
    delete n;
}

string SIEVE::evict() {
    if (!tail_) {
        return {};
    }
    Node* victim = hand_ ? hand_ : tail_;
    while (victim && victim->visited) {
        victim->visited = false;
        victim = victim->prev;
    }
    if (!victim) {
        victim = tail_;
    }
    hand_ = victim->prev;
    string key = victim->key;
    memory_used_ -= key.size() + victim->value;
    index_.erase(key);
    if (victim->prev) {
        victim->prev->next = victim->next;
    } else {
        head_ = victim->next;
    }
    if (victim->next) {
        victim->next->prev = victim->prev;
    } else {
        tail_ = victim->prev;
    }
    delete victim;
    return key;
}

size_t SIEVE::memory_used() const {
    return memory_used_;
}

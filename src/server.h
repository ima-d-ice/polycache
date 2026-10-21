#pragma once

#include "protocol.h"

#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <string>
#include <unordered_map>

class Storage;
class AOFLogger;

class Server {
public:
    Server(int port, Storage* storage, AOFLogger* aof,
           std::size_t max_value_size = protocol::Parser::kDefaultMaxBulkLen);
    ~Server();

    Server(const Server&) = delete;
    Server& operator=(const Server&) = delete;

    void start();
    void stop();

private:
    // A response is either a small owned buffer, or a header + shared blob +
    // trailer. The blob path streams the stored value straight from memory;
    // no copy of a multi-megabyte GET ever lands in the outbox.
    struct Response {
        std::string head;
        std::shared_ptr<const std::string> blob;
        std::string tail;
    };

    struct OutChunk {
        std::string head;
        std::size_t head_sent = 0;
        std::shared_ptr<const std::string> blob;
        std::size_t blob_sent = 0;
        std::string tail;
        std::size_t tail_sent = 0;
    };

    void accept_connections();
    void handle_readable(int fd);
    void queue_response(int fd, Response&& response);
    void try_send(int fd);
    void update_watch(int fd);
    void close_client(int fd);
    Response execute_command(protocol::Command&& cmd);

    int port_;
    Storage* storage_;
    AOFLogger* aof_;
    std::size_t max_value_size_;

    int listen_fd_ = -1;
    int epoll_fd_ = -1;
    int wake_pipe_[2] = {-1, -1};
    bool running_ = false;

    std::unordered_map<int, protocol::Parser> parsers_;
    std::unordered_map<int, std::deque<OutChunk>> outbox_;
    std::unordered_map<int, std::uint32_t> watch_flags_;
};

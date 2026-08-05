#pragma once

#include <string>

class Storage;

class AdminServer {
public:
    AdminServer(int port, Storage* storage);
    ~AdminServer();

    AdminServer(const AdminServer&) = delete;
    AdminServer& operator=(const AdminServer&) = delete;

    void start();
    void stop();

private:
    void accept_connections();
    void handle_connection(int fd);
    void close_conn(int fd);
    std::string handle_request(const std::string& request);
    void send_all(int fd, const std::string& response);

    int port_;
    Storage* storage_;

    int listen_fd_ = -1;
    int epoll_fd_ = -1;
    int wake_pipe_[2] = {-1, -1};
    bool running_ = false;
};
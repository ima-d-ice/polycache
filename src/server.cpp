#include "server.h"

#include "aof.h"
#include "epoll_compat.h"
#include "storage.h"

#include <algorithm>
#include <arpa/inet.h>
#include <cctype>
#include <cerrno>
#include <cstdio>
#include <fcntl.h>
#include <netinet/in.h>
#include <string>
#include <sys/socket.h>
#include <unistd.h>
#include <utility>

using namespace std;

namespace {

constexpr int kMaxEvents = 64;
constexpr int kReadChunk = 64 * 1024;
constexpr size_t kMaxQueuedChunks = 1024;

bool parse_int(const string& text, int& out) {
    if (text.empty()) {
        return false;
    }
    size_t i = (text[0] == '-' || text[0] == '+') ? 1 : 0;
    if (i == text.size()) {
        return false;
    }
    for (; i < text.size(); ++i) {
        if (!isdigit(static_cast<unsigned char>(text[i]))) {
            return false;
        }
    }
    try {
        out = stoi(text);
        return true;
    } catch (const exception&) {
        return false;
    }
}

string lowercase(const string& text) {
    string out = text;
    transform(out.begin(), out.end(), out.begin(), [](unsigned char c) {
        return static_cast<char>(tolower(c));
    });
    return out;
}

}  // namespace

Server::Server(int port, Storage* storage, AOFLogger* aof, size_t max_value_size)
    : port_(port),
      storage_(storage),
      aof_(aof),
      max_value_size_(max_value_size) {}

Server::~Server() {
    if (listen_fd_ != -1) {
        close(listen_fd_);
    }
    if (epoll_fd_ != -1) {
        close(epoll_fd_);
    }
    if (wake_pipe_[0] != -1) {
        close(wake_pipe_[0]);
    }
    if (wake_pipe_[1] != -1) {
        close(wake_pipe_[1]);
    }
}

void Server::start() {
    listen_fd_ = socket(AF_INET, SOCK_STREAM, 0);
    if (listen_fd_ < 0) {
        perror("socket");
        return;
    }
    int flags = fcntl(listen_fd_, F_GETFL, 0);
    fcntl(listen_fd_, F_SETFL, flags | O_NONBLOCK);

    int opt = 1;
    setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(static_cast<uint16_t>(port_));
    if (::bind(listen_fd_, reinterpret_cast<const sockaddr*>(&addr), sizeof(addr)) < 0) {
        perror("bind");
        close(listen_fd_);
        listen_fd_ = -1;
        return;
    }
    if (listen(listen_fd_, SOMAXCONN) < 0) {
        perror("listen");
        close(listen_fd_);
        listen_fd_ = -1;
        return;
    }

    epoll_fd_ = epoll_create1(0);
    if (epoll_fd_ < 0) {
        perror("epoll_create1");
        close(listen_fd_);
        listen_fd_ = -1;
        return;
    }

    if (socketpair(AF_UNIX, SOCK_STREAM, 0, wake_pipe_) < 0) {
        perror("socketpair");
        close(epoll_fd_);
        epoll_fd_ = -1;
        close(listen_fd_);
        listen_fd_ = -1;
        return;
    }
    int wake_flags = fcntl(wake_pipe_[0], F_GETFL, 0);
    fcntl(wake_pipe_[0], F_SETFL, wake_flags | O_NONBLOCK);
    wake_flags = fcntl(wake_pipe_[1], F_GETFL, 0);
    fcntl(wake_pipe_[1], F_SETFL, wake_flags | O_NONBLOCK);

    epoll_event ev{};
    ev.events = EPOLLIN;
    ev.data.fd = listen_fd_;
    epoll_ctl(epoll_fd_, EPOLL_CTL_ADD, listen_fd_, &ev);

    ev.data.fd = wake_pipe_[0];
    epoll_ctl(epoll_fd_, EPOLL_CTL_ADD, wake_pipe_[0], &ev);

    running_ = true;
    epoll_event events[kMaxEvents];
    while (running_) {
        const int n = epoll_wait(epoll_fd_, events, kMaxEvents, -1);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            break;
        }
        for (int i = 0; i < n; ++i) {
            const int fd = events[i].data.fd;
            const uint32_t event_flags = events[i].events;
            if (fd == wake_pipe_[0]) {
                char discard[64];
                while (recv(fd, discard, sizeof(discard), 0) > 0) {
                }
                continue;
            }
            if (fd == listen_fd_) {
                accept_connections();
                continue;
            }
            if ((event_flags & (EPOLLERR | EPOLLHUP)) != 0 &&
                (event_flags & EPOLLIN) == 0) {
                close_client(fd);
                continue;
            }
            if ((event_flags & EPOLLIN) != 0) {
                handle_readable(fd);
                if (parsers_.find(fd) == parsers_.end()) {
                    continue;
                }
            }
            if ((event_flags & EPOLLOUT) != 0) {
                try_send(fd);
            }
            if ((event_flags & (EPOLLERR | EPOLLHUP)) != 0 &&
                parsers_.find(fd) != parsers_.end()) {
                close_client(fd);
            }
        }
    }

    close(listen_fd_);
    listen_fd_ = -1;
    close(epoll_fd_);
    epoll_fd_ = -1;
    close(wake_pipe_[0]);
    close(wake_pipe_[1]);
    wake_pipe_[0] = -1;
    wake_pipe_[1] = -1;
}

void Server::stop() {
    running_ = false;
    if (wake_pipe_[1] != -1) {
        const char byte = 1;
        (void)write(wake_pipe_[1], &byte, 1);
    }
}

void Server::accept_connections() {
    for (;;) {
        const int client_fd = accept(listen_fd_, nullptr, nullptr);
        if (client_fd < 0) {
            if (errno == EINTR) {
                continue;
            }
            return;
        }
        int flags = fcntl(client_fd, F_GETFL, 0);
        fcntl(client_fd, F_SETFL, flags | O_NONBLOCK);
        COMPAT_SET_NOSIGPIPE(client_fd);

        epoll_event ev{};
        ev.events = EPOLLIN;
        ev.data.fd = client_fd;
        epoll_ctl(epoll_fd_, EPOLL_CTL_ADD, client_fd, &ev);

        // Let the parser accept a little more than the configured maximum so
        // an oversized value gets a clean -ERR reply instead of a protocol
        // error, while a hostile declaration far above the limit is still
        // rejected before the allocation. Cap at the memory limit too: a
        // 1MB test server must not allocate a 512MB parser buffer just
        // because --max-value-size defaults high.
        constexpr size_t kParseOvershoot = 1024 * 1024;
        const size_t effective_max = min(max_value_size_, storage_->memory_limit());
        const size_t parse_limit = effective_max + kParseOvershoot;
        parsers_.try_emplace(client_fd, parse_limit);
        watch_flags_[client_fd] = EPOLLIN;
    }
}

void Server::close_client(int fd) {
    epoll_ctl(epoll_fd_, EPOLL_CTL_DEL, fd, nullptr);
    close(fd);
    parsers_.erase(fd);
    outbox_.erase(fd);
    watch_flags_.erase(fd);
}

void Server::handle_readable(int fd) {
    char buf[kReadChunk];
    for (;;) {
        const ssize_t n = recv(fd, buf, sizeof(buf), 0);
        if (n > 0) {
            auto parser_it = parsers_.find(fd);
            if (parser_it == parsers_.end()) {
                return;
            }
            protocol::Parser& parser = parser_it->second;

            const char* cursor = buf;
            size_t left = static_cast<size_t>(n);
            while (left > 0) {
                protocol::Command cmd;
                const protocol::Parser::Status status =
                    parser.consume(cursor, left, cmd);
                if (status == protocol::Parser::Status::NeedMore) {
                    break;
                }
                if (status == protocol::Parser::Status::Error) {
                    const string err =
                        "-ERR Protocol error: " + parser.error() + "\r\n";
                    (void)send(fd, err.data(), err.size(), COMPAT_MSG_NOSIGNAL);
                    close_client(fd);
                    return;
                }
                if (cmd.type == protocol::Command::UNKNOWN && cmd.verb.empty()) {
                    continue;  // blank array / keepalive with no verb
                }
                queue_response(fd, execute_command(std::move(cmd)));
                if (parsers_.find(fd) == parsers_.end()) {
                    return;
                }
            }
            continue;
        }
        if (n == 0) {
            close_client(fd);
            return;
        }
        if (errno == EAGAIN || errno == EWOULDBLOCK) {
            break;
        }
        if (errno == EINTR) {
            continue;
        }
        close_client(fd);
        return;
    }
}

Server::Response Server::execute_command(protocol::Command&& cmd) {
    switch (cmd.type) {
        case protocol::Command::PING: {
            if (cmd.args.empty()) {
                return Response{"+PONG", nullptr, ""};
            }
            try {
                auto blob = make_shared<const string>(std::move(cmd.args[0]));
                return Response{"$" + to_string(blob->size()) + "\r\n", blob, "\r\n"};
            } catch (const bad_alloc&) {
                return Response{"-ERR out of memory", nullptr, ""};
            }
        }

        case protocol::Command::SELECT:
            // PolyCache is single-database; SELECT is accepted and ignored.
            return Response{"+OK", nullptr, ""};

        case protocol::Command::SET: {
            if (cmd.args.size() < 2) {
                return Response{"-ERR wrong number of arguments for SET", nullptr, ""};
            }
            if (cmd.args[0].size() > Storage::kMaxKeySize) {
                return Response{"-ERR key too large", nullptr, ""};
            }
            if (cmd.args[1].size() > max_value_size_) {
                return Response{"-ERR value too large (max-value-size)", nullptr, ""};
            }

            int ttl = 0;
            bool saw_expiry = false;
            for (size_t k = 2; k < cmd.args.size(); ++k) {
                const string opt = lowercase(cmd.args[k]);
                if (opt == "ex" || opt == "px") {
                    if (k + 1 >= cmd.args.size()) {
                        return Response{"-ERR syntax error", nullptr, ""};
                    }
                    int amount = 0;
                    if (!parse_int(cmd.args[k + 1], amount) || amount <= 0) {
                        return Response{"-ERR invalid expire value", nullptr, ""};
                    }
                    // TTL storage is second-granularity; round PX up so a
                    // sub-second TTL never turns into "no expiry".
                    ttl = (opt == "px")
                              ? amount / 1000 + (amount % 1000 != 0 ? 1 : 0)
                              : amount;
                    saw_expiry = true;
                    ++k;
                    continue;
                }
                // Legacy positional TTL ("SET key value 60"); keep it only for
                // the first trailing token and only when numeric. Everything
                // else (NX/XX/GET/KEEPTTL) would silently change semantics, so
                // reject it instead of pretending to support it.
                int legacy_ttl = 0;
                if (k == 2 && !saw_expiry && parse_int(cmd.args[k], legacy_ttl)) {
                    ttl = legacy_ttl;
                    continue;
                }
                return Response{"-ERR unsupported SET option: " + cmd.args[k], nullptr, ""};
            }

            if (!storage_->can_store(cmd.args[0], cmd.args[1])) {
                return Response{"-ERR value too large for memory limit", nullptr, ""};
            }

            try {
                if (aof_ != nullptr) {
                    aof_->log_set(cmd.args[0], cmd.args[1], ttl);
                }
                storage_->set(std::move(cmd.args[0]), std::move(cmd.args[1]), ttl);
            } catch (const bad_alloc&) {
                return Response{"-ERR out of memory", nullptr, ""};
            }
            return Response{"+OK", nullptr, ""};
        }

        case protocol::Command::GET: {
            if (cmd.args.empty()) {
                return Response{"-ERR wrong number of arguments for GET", nullptr, ""};
            }
            Blob blob = storage_->get(cmd.args[0]);
            if (!blob) {
                return Response{"$-1", nullptr, ""};
            }
            return Response{"$" + to_string(blob->size()) + "\r\n", blob, "\r\n"};
        }

        case protocol::Command::DEL: {
            if (cmd.args.empty()) {
                return Response{"-ERR wrong number of arguments for DEL", nullptr, ""};
            }
            const bool removed = storage_->del(cmd.args[0]);
            if (removed && aof_ != nullptr) {
                aof_->log_del(cmd.args[0]);
            }
            return Response{removed ? ":1" : ":0", nullptr, ""};
        }

        case protocol::Command::METRICS: {
            const string json = storage_->metrics().dump();
            return Response{"$" + to_string(json.size()) + "\r\n" + json + "\r\n",
                            nullptr, ""};
        }

        case protocol::Command::SWITCH_POLICY: {
            if (cmd.args.empty()) {
                return Response{"-ERR wrong number of arguments for SWITCH_POLICY",
                                nullptr, ""};
            }
            const bool ok = storage_->switch_policy(cmd.args[0]);
            if (!ok) {
                return Response{"-ERR unknown policy: " + cmd.args[0], nullptr, ""};
            }
            return Response{"+OK", nullptr, ""};
        }

        case protocol::Command::UNKNOWN:
        default:
            if (cmd.verb.empty()) {
                return Response{"", nullptr, ""};
            }
            return Response{"-ERR unknown command '" + cmd.verb + "'", nullptr, ""};
    }
}

void Server::queue_response(int fd, Response&& response) {
    if (!response.blob) {
        if (response.head.size() < 2 ||
            response.head.compare(response.head.size() - 2, 2, "\r\n") != 0) {
            response.head += "\r\n";
        }
    }

    auto& queue = outbox_[fd];
    queue.push_back(OutChunk{std::move(response.head), 0, std::move(response.blob), 0,
                             std::move(response.tail), 0});
    if (queue.size() > kMaxQueuedChunks) {
        close_client(fd);
        return;
    }
    try_send(fd);
}

void Server::try_send(int fd) {
    auto it = outbox_.find(fd);
    if (it == outbox_.end()) {
        return;
    }
    auto& queue = it->second;

    while (!queue.empty()) {
        OutChunk& chunk = queue.front();

        const char* ptr = nullptr;
        size_t left = 0;
        enum class Part { Head, Blob, Tail, Done } part = Part::Done;

        if (chunk.head_sent < chunk.head.size()) {
            ptr = chunk.head.data() + chunk.head_sent;
            left = chunk.head.size() - chunk.head_sent;
            part = Part::Head;
        } else if (chunk.blob && chunk.blob_sent < chunk.blob->size()) {
            ptr = chunk.blob->data() + chunk.blob_sent;
            left = chunk.blob->size() - chunk.blob_sent;
            part = Part::Blob;
        } else if (chunk.tail_sent < chunk.tail.size()) {
            ptr = chunk.tail.data() + chunk.tail_sent;
            left = chunk.tail.size() - chunk.tail_sent;
            part = Part::Tail;
        } else {
            queue.pop_front();
            continue;
        }

        const ssize_t n = send(fd, ptr, left, COMPAT_MSG_NOSIGNAL);
        if (n > 0) {
            const size_t sent = static_cast<size_t>(n);
            if (part == Part::Head) {
                chunk.head_sent += sent;
            } else if (part == Part::Blob) {
                chunk.blob_sent += sent;
            } else {
                chunk.tail_sent += sent;
            }
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            break;
        }
        if (n < 0 && errno == EINTR) {
            continue;
        }
        close_client(fd);
        return;
    }

    if (queue.empty()) {
        outbox_.erase(it);
    }
    update_watch(fd);
}

void Server::update_watch(int fd) {
    uint32_t flags = EPOLLIN;
    auto it = outbox_.find(fd);
    if (it != outbox_.end() && !it->second.empty()) {
        flags |= EPOLLOUT;
    }
    auto watch = watch_flags_.find(fd);
    if (watch != watch_flags_.end() && watch->second == flags) {
        return;
    }
    epoll_event ev{};
    ev.events = flags;
    ev.data.fd = fd;
    epoll_ctl(epoll_fd_, EPOLL_CTL_MOD, fd, &ev);
    watch_flags_[fd] = flags;
}

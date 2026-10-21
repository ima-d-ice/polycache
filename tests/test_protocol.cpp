#include "protocol.h"

#include <cassert>
#include <cstdio>
#include <string>
#include <utility>
#include <vector>

using namespace std;
using protocol::Command;
using protocol::Parser;

namespace {

string resp(initializer_list<string> args) {
    string out = "*" + to_string(args.size()) + "\r\n";
    for (const auto& arg : args) {
        out += "$" + to_string(arg.size()) + "\r\n" + arg + "\r\n";
    }
    return out;
}

void feed(Parser& parser, const string& bytes, vector<Command>& out) {
    const char* cursor = bytes.data();
    size_t left = bytes.size();
    while (left > 0) {
        Command cmd;
        const auto status = parser.consume(cursor, left, cmd);
        if (status == Parser::Status::Ready) {
            out.push_back(std::move(cmd));
        } else if (status == Parser::Status::Error) {
            fprintf(stderr, "unexpected parser error: %s\n", parser.error().c_str());
            abort();
        } else {
            break;
        }
    }
}

void test_single_set() {
    Parser parser;
    vector<Command> commands;
    feed(parser, resp({"SET", "foo", "bar"}), commands);
    assert(commands.size() == 1);
    assert(commands[0].type == Command::SET);
    assert(commands[0].verb == "SET");
    assert(commands[0].args.size() == 2);
    assert(commands[0].args[0] == "foo");
    assert(commands[0].args[1] == "bar");
}

void test_byte_by_byte() {
    Parser parser;
    vector<Command> commands;
    const string frame = resp({"GET", "greeting"});
    for (const char c : frame) {
        feed(parser, string(1, c), commands);
    }
    assert(commands.size() == 1);
    assert(commands[0].type == Command::GET);
    assert(commands[0].args.size() == 1);
    assert(commands[0].args[0] == "greeting");
}

void test_pipeline() {
    Parser parser;
    vector<Command> commands;
    feed(parser, resp({"SET", "a", "1"}) + resp({"GET", "a"}) + resp({"DEL", "a"}),
         commands);
    assert(commands.size() == 3);
    assert(commands[0].type == Command::SET);
    assert(commands[1].type == Command::GET);
    assert(commands[2].type == Command::DEL);
}

void test_binary_payload() {
    string payload;
    payload.reserve(1024 * 1024);
    for (size_t i = 0; i < 1024 * 1024; ++i) {
        payload.push_back(static_cast<char>(i * 31 + 7));
    }
    payload += string("\r\n\x00\xff", 4);

    Parser parser;
    vector<Command> commands;
    feed(parser, resp({"SET", "img:1", payload}), commands);
    assert(commands.size() == 1);
    assert(commands[0].args.size() == 2);
    assert(commands[0].args[1] == payload);
}

void test_split_trailer() {
    Parser parser;
    const string frame = resp({"PING", "hello"});
    vector<Command> commands;
    feed(parser, frame.substr(0, frame.size() - 1), commands);
    assert(commands.empty());
    feed(parser, frame.substr(frame.size() - 1), commands);
    assert(commands.size() == 1);
    assert(commands[0].type == Command::PING);
    assert(commands[0].args.size() == 1);
    assert(commands[0].args[0] == "hello");
}

void test_oversized_bulk() {
    Parser parser(8);
    const char* cursor = nullptr;
    size_t left = 0;
    const string frame = resp({"SET", "k", "123456789"});
    cursor = frame.data();
    left = frame.size();
    Command cmd;
    assert(parser.consume(cursor, left, cmd) == Parser::Status::Error);
    assert(!parser.error().empty());
}

void test_bad_header() {
    Parser parser;
    const string frame = "*x\r\n";
    const char* cursor = frame.data();
    size_t left = frame.size();
    Command cmd;
    assert(parser.consume(cursor, left, cmd) == Parser::Status::Error);
}

void test_empty_array() {
    Parser parser;
    const string frame = "*0\r\n";
    const char* cursor = frame.data();
    size_t left = frame.size();
    Command cmd;
    assert(parser.consume(cursor, left, cmd) == Parser::Status::Ready);
    assert(cmd.type == Command::UNKNOWN);
    assert(cmd.verb.empty());
    assert(cmd.args.empty());
}

void test_unknown_verb() {
    Parser parser;
    vector<Command> commands;
    feed(parser, resp({"NOPE", "one"}), commands);
    assert(commands.size() == 1);
    assert(commands[0].type == Command::UNKNOWN);
    assert(commands[0].verb == "NOPE");
    assert(commands[0].args.size() == 1);
    assert(commands[0].args[0] == "one");
}

void test_empty_key_and_value() {
    Parser parser;
    vector<Command> commands;
    feed(parser, resp({"SET", "", ""}), commands);
    assert(commands.size() == 1);
    assert(commands[0].args.size() == 2);
    assert(commands[0].args[0].empty());
    assert(commands[0].args[1].empty());
}

void test_fragmented_pipeline() {
    Parser parser;
    vector<Command> commands;
    const string stream = resp({"SET", "x", "1"}) + resp({"GET", "x"}) +
                          resp({"SET", "y", "2"}) + resp({"GET", "y"});
    for (size_t i = 0; i < stream.size(); i += 7) {
        feed(parser, stream.substr(i, 7), commands);
    }
    assert(commands.size() == 4);
    assert(commands[0].args[0] == "x");
    assert(commands[1].args[0] == "x");
    assert(commands[2].args[0] == "y");
    assert(commands[3].args[0] == "y");
}

}  // namespace

int main() {
    test_single_set();
    test_byte_by_byte();
    test_pipeline();
    test_binary_payload();
    test_split_trailer();
    test_oversized_bulk();
    test_bad_header();
    test_empty_array();
    test_unknown_verb();
    test_empty_key_and_value();
    test_fragmented_pipeline();
    printf("ok test_protocol\n");
    return 0;
}

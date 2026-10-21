#include "aof.h"

#include "storage.h"
#include "third_party/nlohmann/json.hpp"

#include <cstdint>
#include <fstream>
#include <string>

using namespace std;

namespace {

constexpr char kBase64Alphabet[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

string base64_encode(const string& input) {
    string out;
    out.reserve(((input.size() + 2) / 3) * 4);
    size_t i = 0;
    while (i + 3 <= input.size()) {
        const uint32_t v = (static_cast<uint32_t>(static_cast<uint8_t>(input[i])) << 16) |
                           (static_cast<uint32_t>(static_cast<uint8_t>(input[i + 1])) << 8) |
                           static_cast<uint32_t>(static_cast<uint8_t>(input[i + 2]));
        out.push_back(kBase64Alphabet[(v >> 18) & 0x3F]);
        out.push_back(kBase64Alphabet[(v >> 12) & 0x3F]);
        out.push_back(kBase64Alphabet[(v >> 6) & 0x3F]);
        out.push_back(kBase64Alphabet[v & 0x3F]);
        i += 3;
    }
    const size_t remaining = input.size() - i;
    if (remaining == 1) {
        const uint32_t v = static_cast<uint32_t>(static_cast<uint8_t>(input[i])) << 16;
        out.push_back(kBase64Alphabet[(v >> 18) & 0x3F]);
        out.push_back(kBase64Alphabet[(v >> 12) & 0x3F]);
        out.push_back('=');
        out.push_back('=');
    } else if (remaining == 2) {
        const uint32_t v =
            (static_cast<uint32_t>(static_cast<uint8_t>(input[i])) << 16) |
            (static_cast<uint32_t>(static_cast<uint8_t>(input[i + 1])) << 8);
        out.push_back(kBase64Alphabet[(v >> 18) & 0x3F]);
        out.push_back(kBase64Alphabet[(v >> 12) & 0x3F]);
        out.push_back(kBase64Alphabet[(v >> 6) & 0x3F]);
        out.push_back('=');
    }
    return out;
}

int base64_value(char c) {
    if (c >= 'A' && c <= 'Z') {
        return c - 'A';
    }
    if (c >= 'a' && c <= 'z') {
        return c - 'a' + 26;
    }
    if (c >= '0' && c <= '9') {
        return c - '0' + 52;
    }
    if (c == '+') {
        return 62;
    }
    if (c == '/') {
        return 63;
    }
    if (c == '=') {
        return -2;
    }
    return -1;
}

bool base64_decode(const string& input, string& output) {
    if (input.size() % 4 != 0) {
        return false;
    }
    output.clear();
    output.reserve((input.size() / 4) * 3);
    for (size_t i = 0; i < input.size(); i += 4) {
        const int a = base64_value(input[i]);
        const int b = base64_value(input[i + 1]);
        const int c = base64_value(input[i + 2]);
        const int d = base64_value(input[i + 3]);
        if (a < 0 || b < 0 || c == -1 || d == -1) {
            return false;
        }
        const bool last = (i + 4 == input.size());
        if (c == -2) {
            if (!last || d != -2) {
                return false;
            }
        } else if (d == -2 && !last) {
            return false;
        }
        const uint32_t v = (static_cast<uint32_t>(a) << 18) |
                           (static_cast<uint32_t>(b) << 12) |
                           (static_cast<uint32_t>(c < 0 ? 0 : c) << 6) |
                           static_cast<uint32_t>(d < 0 ? 0 : d);
        output.push_back(static_cast<char>((v >> 16) & 0xFF));
        if (c >= 0) {
            output.push_back(static_cast<char>((v >> 8) & 0xFF));
        }
        if (d >= 0) {
            output.push_back(static_cast<char>(v & 0xFF));
        }
    }
    return true;
}

bool decode_field(const nlohmann::json& j, const char* encoded,
                  const char* legacy, string& out) {
    if (j.contains(encoded)) {
        const auto& text = j[encoded].get_ref<const string&>();
        return base64_decode(text, out);
    }
    if (j.contains(legacy)) {
        out = j[legacy].get_ref<const string&>();
        return true;
    }
    return false;
}

}  // namespace

AOFLogger::AOFLogger(const string& filename)
    : filename_(filename),
      file_(filename, ios::out | ios::app) {}

void AOFLogger::append_line(const string& line) {
    file_ << line << '\n';
    file_.flush();
}

void AOFLogger::log_set(const string& key, const string& value, int ttl) {
    nlohmann::json j;
    j["cmd"] = "SET";
    // Base64 keeps the log binary-safe without changing the line-oriented
    // format. Keys are encoded too: RESP keys are binary-safe, and a JPEG or
    // a serialized row in a key must not throw out of json::dump().
    j["key_b64"] = base64_encode(key);
    j["value_b64"] = base64_encode(value);
    j["ttl"] = ttl;
    append_line(j.dump());
}

void AOFLogger::log_del(const string& key) {
    nlohmann::json j;
    j["cmd"] = "DEL";
    j["key_b64"] = base64_encode(key);
    append_line(j.dump());
}

void AOFLogger::replay(Storage* storage) {
    if (storage == nullptr) {
        return;
    }
    file_.close();
    ifstream in(filename_);
    string line;
    while (getline(in, line)) {
        if (line.empty()) {
            continue;
        }
        try {
            const auto j = nlohmann::json::parse(line);
            const string cmd = j.value("cmd", "");
            if (cmd == "SET") {
                string key;
                string value;
                if (!decode_field(j, "key_b64", "key", key) ||
                    !decode_field(j, "value_b64", "value", value)) {
                    continue;
                }
                storage->set(std::move(key), std::move(value), j.value("ttl", 0));
            } else if (cmd == "DEL") {
                string key;
                if (decode_field(j, "key_b64", "key", key)) {
                    storage->del(key);
                }
            }
        } catch (const nlohmann::json::exception&) {
            // Torn tail or a legacy line in an unexpected shape: skip it and
            // keep replaying the rest of the log.
        }
    }
    file_.open(filename_, ios::out | ios::app);
}

CXX      := g++
CXXFLAGS := -std=c++17 -O2 -Wall -Wextra -pthread -MMD -MP
SRCS     := $(shell find src -name '*.cpp')
OBJS     := $(SRCS:.cpp=.o)
DEPS     := $(OBJS:.o=.d)
TARGET   := polycache

STORAGE_TEST_SRCS := src/storage.cpp src/ttl.cpp src/eviction/lru.cpp \
                     src/eviction/lfu.cpp src/eviction/sieve.cpp

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^

%.o: %.cpp
	$(CXX) $(CXXFLAGS) -c $< -o $@

test_protocol: tests/test_protocol.cpp src/protocol.cpp
	$(CXX) $(CXXFLAGS) -I src -o $@ $^

test_storage: tests/test_storage.cpp $(STORAGE_TEST_SRCS)
	$(CXX) $(CXXFLAGS) -I src -o $@ $^

test: test_protocol test_storage
	./test_protocol
	./test_storage

clean:
	rm -f $(OBJS) $(DEPS) $(TARGET) test_protocol test_storage \
	      test_protocol.d test_storage.d

-include $(DEPS)

.PHONY: all clean test

CXX      := g++
CXXFLAGS := -std=c++17 -O2 -Wall -Wextra -pthread -MMD -MP
SRCS     := $(shell find src -name '*.cpp')
OBJS     := $(SRCS:.cpp=.o)
DEPS     := $(OBJS:.o=.d)
TARGET   := cachepilot

all: $(TARGET)

$(TARGET): $(OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $^

%.o: %.cpp
	$(CXX) $(CXXFLAGS) -c $< -o $@

clean:
	rm -f $(OBJS) $(DEPS) $(TARGET)

-include $(DEPS)

.PHONY: all clean

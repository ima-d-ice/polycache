FROM gcc:13-bookworm

WORKDIR /work

COPY Makefile ./
COPY src/ ./src/

RUN make clean && make

CMD ["./polycache"]

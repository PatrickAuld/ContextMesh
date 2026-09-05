FROM rust:1.98-bookworm AS build
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY migrations ./migrations
RUN cargo build --release --locked

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/target/release/contextmesh /usr/local/bin/contextmesh
USER 65532:65532
EXPOSE 8787
ENTRYPOINT ["contextmesh"]
CMD ["run", "--listen", "0.0.0.0:8787", "--config", "/etc/contextmesh/config.json"]

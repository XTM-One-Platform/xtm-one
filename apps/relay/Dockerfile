# XTM One Relay - container image, published as xtmone/relay.
#
# Build context: apps/ (the relay links ../agent-core):
#   docker buildx build -f apps/relay/Dockerfile --platform linux/amd64,linux/arm64 \
#     --build-arg XTM_RELAY_VERSION=1.261002.1 -t xtmone/relay:1.261002.1 apps
#
# Configured entirely by XTM_RELAY_* variables (XTM_RELAY_SERVER_URL,
# XTM_RELAY_ENROLLMENT_KEY, XTM_RELAY_ALLOWED_DESTINATIONS, ...). It enrolls
# itself on first start and keeps its credential and state on the volume, so a
# restarted container is the same relay. It never updates itself: pull a newer
# image. Runs as a non-root user, needs no capability and no inbound port.

# Cross-compiled on the build platform: building arm64 under emulation takes
# several times longer than the native leg.
FROM --platform=$BUILDPLATFORM docker.io/library/rust:1-bookworm AS build
ARG TARGETARCH
ARG XTM_RELAY_VERSION=0.0.0-dev
RUN set -eux; \
    case "$TARGETARCH" in \
        amd64) target=x86_64-unknown-linux-gnu; gcc=x86-64-linux-gnu; libc=amd64 ;; \
        arm64) target=aarch64-unknown-linux-gnu; gcc=aarch64-linux-gnu; libc=arm64 ;; \
        *) echo "unsupported TARGETARCH $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    if [ "$(dpkg --print-architecture)" != "$libc" ]; then \
        apt-get update; \
        apt-get install -y --no-install-recommends "gcc-$gcc" "libc6-dev-$libc-cross"; \
        rm -rf /var/lib/apt/lists/*; \
    fi; \
    rustup target add "$target"; \
    echo "$target" > /tmp/relay-target
# Debian names its native compiler with the triplet too, so these hold for a
# native build as well as a cross one.
ENV CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc \
    CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=x86_64-linux-gnu-gcc \
    CC_x86_64_unknown_linux_gnu=x86_64-linux-gnu-gcc
WORKDIR /src
COPY agent-core/ agent-core/
COPY relay/ relay/
WORKDIR /src/relay
# No self-update in a container: the image is the unit of update.
RUN set -eux; \
    target="$(cat /tmp/relay-target)"; \
    XTM_RELAY_VERSION="$XTM_RELAY_VERSION" \
        cargo build --release --locked --no-default-features --target "$target"; \
    install -D -m 0755 "target/$target/release/xtm-one-relay" /out/xtm-one-relay; \
    install -d -m 0700 /out/state

FROM gcr.io/distroless/cc-debian12:nonroot
ARG XTM_RELAY_VERSION=0.0.0-dev
COPY --from=build /out/xtm-one-relay /usr/local/bin/xtm-one-relay
COPY --from=build --chown=65532:65532 /out/state /var/lib/xtm-one-relay
ENV XTM_RELAY_CONTAINER=1 \
    XTM_RELAY_CONFIG=/var/lib/xtm-one-relay/relay.toml
VOLUME ["/var/lib/xtm-one-relay"]
USER 65532:65532
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD ["/usr/local/bin/xtm-one-relay", "status"]
LABEL org.opencontainers.image.title="XTM One Relay" \
      org.opencontainers.image.description="Pull-based on-premises egress relay for XTM One" \
      org.opencontainers.image.version=$XTM_RELAY_VERSION \
      org.opencontainers.image.source="https://github.com/XTM-One-Platform/xtm-one" \
      org.opencontainers.image.licenses="LicenseRef-XTM-One-Enterprise-Edition"
ENTRYPOINT ["/usr/local/bin/xtm-one-relay"]
CMD ["run"]

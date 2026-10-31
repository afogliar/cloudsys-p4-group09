FROM golang:1.25.3-bookworm AS builder

RUN mkdir -p /out \
    && CGO_ENABLED=0 GOBIN=/out GOMAXPROCS=2 \
       go install -p 2 github.com/minio/minio@RELEASE.2025-10-15T17-29-55Z \
    && test -x /out/minio

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /out/minio /usr/local/bin/minio

EXPOSE 9000 9001

ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]
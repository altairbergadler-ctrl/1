FROM golang:1.25-alpine AS build

ARG UPSTREAM_COMMIT=ed36c6878207d2d248bafb0c6e55786a04296748
RUN apk add --no-cache git
WORKDIR /src
RUN git clone https://github.com/Stmol/yandex-music-downloader.git . \
    && git checkout --detach "${UPSTREAM_COMMIT}" \
    && test "$(git rev-parse HEAD)" = "${UPSTREAM_COMMIT}"

COPY main.go /src/cmd/music-service-yandex-signer/main.go
COPY main_test.go /src/cmd/music-service-yandex-signer/main_test.go
RUN go test ./cmd/music-service-yandex-signer ./ya/lossless
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
    -o /out/yandex-signer ./cmd/music-service-yandex-signer

FROM scratch
COPY --from=build /out/yandex-signer /yandex-signer
USER 65532:65532
EXPOSE 8091
ENTRYPOINT ["/yandex-signer"]

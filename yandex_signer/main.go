package main

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"

	"ya-music/ya/lossless"
)

const (
	listenAddress = ":8091"
	maxBodyBytes  = 4096
)

var trackIDPattern = regexp.MustCompile(`^[0-9]{1,24}$`)

type signRequest struct {
	TrackID   string `json:"track_id"`
	Timestamp int64  `json:"timestamp"`
}

type signResponse struct {
	Timestamp  string `json:"ts"`
	TrackID    string `json:"trackId"`
	Quality    string `json:"quality"`
	Codecs     string `json:"codecs"`
	Transports string `json:"transports"`
	Sign       string `json:"sign"`
}

func authorized(header, token string) bool {
	if len(token) < 16 || !strings.HasPrefix(header, "Bearer ") {
		return false
	}
	candidate := strings.TrimPrefix(header, "Bearer ")
	return len(candidate) == len(token) && subtle.ConstantTimeCompare([]byte(candidate), []byte(token)) == 1
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func newHandler(token string, now func() time.Time) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("POST /sign", func(w http.ResponseWriter, r *http.Request) {
		if !authorized(r.Header.Get("Authorization"), token) {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxBodyBytes)
		decoder := json.NewDecoder(r.Body)
		decoder.DisallowUnknownFields()
		var payload signRequest
		if err := decoder.Decode(&payload); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid request"})
			return
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid request"})
			return
		}
		if !trackIDPattern.MatchString(payload.TrackID) {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid track id"})
			return
		}
		current := now().Unix()
		if payload.Timestamp < current-300 || payload.Timestamp > current+300 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid timestamp"})
			return
		}
		signedURL := lossless.BuildFileInfoURL(
			"https://api.music.yandex.net",
			payload.TrackID,
			payload.Timestamp,
		)
		parsed, err := url.Parse(signedURL)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "signing failed"})
			return
		}
		query := parsed.Query()
		writeJSON(w, http.StatusOK, signResponse{
			Timestamp:  query.Get("ts"),
			TrackID:    query.Get("trackId"),
			Quality:    query.Get("quality"),
			Codecs:     query.Get("codecs"),
			Transports: query.Get("transports"),
			Sign:       query.Get("sign"),
		})
	})
	return mux
}

func healthcheck() int {
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Get("http://127.0.0.1:8091/health")
	if err != nil {
		return 1
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return 1
	}
	return 0
}

func main() {
	if len(os.Args) == 2 && os.Args[1] == "healthcheck" {
		os.Exit(healthcheck())
	}
	token := strings.TrimSpace(os.Getenv("YANDEX_INTERNAL_TOKEN"))
	if len(token) < 16 {
		// Keep the optional service healthy in a default-disabled stack.  The
		// authorization check rejects every /sign request until a real token is
		// configured, while the rest of Music Service can still start normally.
		log.Printf("YANDEX_INTERNAL_TOKEN is not configured; signing is disabled")
	}
	server := &http.Server{
		Addr:              listenAddress,
		Handler:           newHandler(token, time.Now),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       5 * time.Second,
		WriteTimeout:      5 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    8192,
	}
	log.Printf("yandex signer listening on %s", listenAddress)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

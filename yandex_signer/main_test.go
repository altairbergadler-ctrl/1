package main

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestSignEndpointIsAuthenticatedAndReturnsPinnedContract(t *testing.T) {
	now := time.Unix(1724399849, 0)
	server := httptest.NewServer(newHandler("0123456789abcdef", func() time.Time { return now }))
	defer server.Close()

	body, _ := json.Marshal(signRequest{TrackID: "117708948", Timestamp: now.Unix()})
	request, _ := http.NewRequest(http.MethodPost, server.URL+"/sign", bytes.NewReader(body))
	request.Header.Set("Authorization", "Bearer 0123456789abcdef")
	request.Header.Set("Content-Type", "application/json")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("unexpected status: %d", response.StatusCode)
	}
	var result signResponse
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		t.Fatal(err)
	}
	if result.TrackID != "117708948" || result.Quality != "lossless" || result.Transports != "raw" {
		t.Fatalf("unexpected signed contract: %#v", result)
	}
	if result.Codecs != "flac,aac,he-aac,mp3,flac-mp4,aac-mp4,he-aac-mp4" || result.Sign == "" {
		t.Fatalf("unexpected codecs/signature")
	}
}

func TestSignEndpointRejectsMissingAuthorization(t *testing.T) {
	now := time.Unix(1724399849, 0)
	request := httptest.NewRequest(http.MethodPost, "/sign", bytes.NewBufferString(`{"track_id":"1","timestamp":1724399849}`))
	response := httptest.NewRecorder()
	newHandler("0123456789abcdef", func() time.Time { return now }).ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("unexpected status: %d", response.Code)
	}
}

func TestSignEndpointRejectsWhenServerTokenIsMissing(t *testing.T) {
	now := time.Unix(1724399849, 0)
	request := httptest.NewRequest(http.MethodPost, "/sign", bytes.NewBufferString(`{"track_id":"1","timestamp":1724399849}`))
	request.Header.Set("Authorization", "Bearer 0123456789abcdef")
	response := httptest.NewRecorder()
	newHandler("", func() time.Time { return now }).ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("unexpected status: %d", response.Code)
	}
}

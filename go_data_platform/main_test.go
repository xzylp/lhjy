package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func newTestServerConfig() *Config {
	cfg := DefaultConfig()
	cfg.WindowsToken = "test-token"
	return cfg
}

func TestResolveLaneRoutesInternalRequestsToInternalLane(t *testing.T) {
	server := NewServer(newTestServerConfig())
	request := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:18793/system/operations/components", nil)

	lane := server.resolveLane(request)
	if lane != "internal" {
		t.Fatalf("expected internal lane, got %s", lane)
	}
}

func TestNewUpstreamHTTPClientDisablesProxy(t *testing.T) {
	client := newUpstreamHTTPClient(30 * time.Second)
	transport, ok := client.Transport.(*http.Transport)
	if !ok {
		t.Fatalf("expected *http.Transport, got %T", client.Transport)
	}
	if transport.Proxy != nil {
		t.Fatalf("expected upstream transport proxy to be disabled")
	}
}

func TestForwardQmtRequestInjectsWindowsTokenOnlyForQmt(t *testing.T) {
	var receivedToken string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedToken = r.Header.Get("X-Ashare-Token")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()

	cfg := newTestServerConfig()
	cfg.WindowsBaseURL = upstream.URL
	cfg.PythonBaseURL = upstream.URL
	server := NewServer(cfg)

	request := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:18793/qmt/account/asset", nil)
	response, err := server.forward(request, nil)
	if err != nil {
		t.Fatalf("forward qmt request failed: %v", err)
	}
	defer response.Body.Close()
	_, _ = io.ReadAll(response.Body)

	if receivedToken != "test-token" {
		t.Fatalf("expected qmt request to inject token, got %q", receivedToken)
	}
}

func TestForwardInternalRequestDoesNotInjectWindowsToken(t *testing.T) {
	var receivedToken string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedToken = r.Header.Get("X-Ashare-Token")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer upstream.Close()

	cfg := newTestServerConfig()
	cfg.WindowsBaseURL = "http://127.0.0.1:9"
	cfg.PythonBaseURL = upstream.URL
	server := NewServer(cfg)

	request := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:18793/system/account-state", nil)
	response, err := server.forward(request, nil)
	if err != nil {
		t.Fatalf("forward internal request failed: %v", err)
	}
	defer response.Body.Close()
	_, _ = io.ReadAll(response.Body)

	if receivedToken != "" {
		t.Fatalf("expected internal request to skip token injection, got %q", receivedToken)
	}
}

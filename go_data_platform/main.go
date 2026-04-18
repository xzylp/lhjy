package main

import (
	"bytes"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus/promhttp"
	"golang.org/x/sync/singleflight"
)

var (
	sf singleflight.Group
)

type Server struct {
	config    *Config
	scheduler *Scheduler
	cache     *Cache
	client    *http.Client
}

func newUpstreamHTTPClient(timeout time.Duration) *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	transport.DialContext = (&net.Dialer{
		Timeout:   5 * time.Second,
		KeepAlive: 30 * time.Second,
	}).DialContext
	transport.MaxIdleConns = 128
	transport.MaxIdleConnsPerHost = 32
	transport.IdleConnTimeout = 90 * time.Second
	transport.TLSHandshakeTimeout = 5 * time.Second
	transport.ExpectContinueTimeout = 1 * time.Second

	return &http.Client{
		Timeout:   timeout,
		Transport: transport,
	}
}

func resolvePythonBaseURL(cfg *Config) string {
	if explicit := strings.TrimSpace(os.Getenv("ASHARE_PYTHON_CONTROL_BASE_URL")); explicit != "" {
		return explicit
	}
	host := strings.TrimSpace(os.Getenv("ASHARE_SERVICE_HOST"))
	if host == "" || host == "0.0.0.0" || host == "::" || host == "[::]" {
		host = "127.0.0.1"
	}
	port := strings.TrimSpace(os.Getenv("ASHARE_SERVICE_PORT"))
	if port == "" {
		port = "8100"
	}
	if host != "" && port != "" {
		return fmt.Sprintf("http://%s:%s", host, port)
	}
	return cfg.PythonBaseURL
}

func NewServer(cfg *Config) *Server {
	return &Server{
		config:    cfg,
		scheduler: NewScheduler(cfg.Lanes),
		cache:     NewCache(time.Duration(cfg.CacheTTLMS) * time.Millisecond),
		client:    newUpstreamHTTPClient(30 * time.Second), // 显式禁用环境代理，统一项目内外上游链路
	}
}

func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/metrics" {
		promhttp.Handler().ServeHTTP(w, r)
		return
	}

	if r.URL.Path == "/health" {
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"ok","service":"go-data-platform","windows_proxy":"enabled","python_control_plane":"enabled"}`))
		return
	}

	// 1. Determine lane
	laneName := s.resolveLane(r)
	lane := s.scheduler.GetLane(laneName)
	if lane == nil {
		http.Error(w, "invalid lane", http.StatusInternalServerError)
		return
	}

	// 2. Buffer body for POST/PUT requests
	var bodyBytes []byte
	if r.Method == "POST" || r.Method == "PUT" || r.Method == "PATCH" {
		var err error
		bodyBytes, err = io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, "failed to read request body", http.StatusInternalServerError)
			return
		}
		r.Body.Close()
	}

	// 3. Handle caching and singleflight for GET requests
	if r.Method == "GET" && s.isCacheable(r) {
		cacheKey := r.URL.String()
		if data, ok := s.cache.Get(cacheKey); ok {
			w.Header().Set("X-Cache-Hit", "true")
			w.Header().Set("Content-Type", "application/json")
			w.Write(data)
			return
		}

		v, err, _ := sf.Do(cacheKey, func() (interface{}, error) {
			var respBody []byte
			execErr := lane.Execute(r.Context(), func() error {
				resp, err := s.forward(r, bodyBytes)
				if err != nil {
					return err
				}
				defer resp.Body.Close()
				respBody, err = io.ReadAll(resp.Body)
				if err != nil {
					return err
				}
				if resp.StatusCode >= 400 {
					return fmt.Errorf("upstream error: %d - %s", resp.StatusCode, string(respBody))
				}
				return nil
			})
			if execErr != nil {
				return nil, execErr
			}
			s.cache.Set(cacheKey, respBody)
			return respBody, nil
		})

		if err != nil {
			http.Error(w, err.Error(), http.StatusGatewayTimeout)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write(v.([]byte))
		return
	}

	// 4. Forward without caching
	err := lane.Execute(r.Context(), func() error {
		resp, err := s.forward(r, bodyBytes)
		if err != nil {
			return err
		}
		defer resp.Body.Close()

		// Copy headers except Content-Length which will be set by Write
		for k, v := range resp.Header {
			if strings.ToLower(k) == "content-length" {
				continue
			}
			for _, val := range v {
				w.Header().Add(k, val)
			}
		}
		w.WriteHeader(resp.StatusCode)
		_, copyErr := io.Copy(w, resp.Body)
		return copyErr
	})

	if err != nil {
		log.Printf("Proxy error for %s %s: %v", r.Method, r.URL.Path, err)
	}
}

func (s *Server) resolveLane(r *http.Request) string {
	path := r.URL.Path
	priority := r.Header.Get("X-Ashare-Priority")
	if !strings.HasPrefix(path, "/qmt/") {
		return "internal"
	}
	switch priority {
	case "high":
		return "quote"
	case "medium":
		return "account_fast"
	case "low":
		return "trade_slow"
	default:
		if strings.Contains(path, "/trade/") {
			return "trade_slow"
		}
		if strings.Contains(path, "/account/") {
			return "account_fast"
		}
		return "quote"
	}
}

func (s *Server) isCacheable(r *http.Request) bool {
	path := r.URL.Path
	if strings.HasPrefix(path, "/qmt/quote/") {
		return true
	}
	if path == "/qmt/account/asset" || path == "/qmt/account/positions" {
		return true
	}
	return false
}

func (s *Server) forward(r *http.Request, body []byte) (*http.Response, error) {
	targetBaseURL := strings.TrimSuffix(s.config.PythonBaseURL, "/")
	if strings.HasPrefix(r.URL.Path, "/qmt/") {
		targetBaseURL = strings.TrimSuffix(s.config.WindowsBaseURL, "/")
	}

	targetURL := targetBaseURL + r.URL.Path

	if r.URL.RawQuery != "" {
		targetURL += "?" + r.URL.RawQuery
	}

	log.Printf("Forwarding %s %s to %s (body size: %d)", r.Method, r.URL.Path, targetURL, len(body))

	var bodyReader io.Reader
	if len(body) > 0 {
		bodyReader = bytes.NewReader(body)
	}

	req, err := http.NewRequestWithContext(r.Context(), r.Method, targetURL, bodyReader)
	if err != nil {
		return nil, err
	}

	// Important: if body exists, explicitly set Content-Length for some backend compatibility
	if len(body) > 0 {
		req.ContentLength = int64(len(body))
	}

	for k, v := range r.Header {
		if strings.ToLower(k) == "content-length" || strings.ToLower(k) == "host" {
			continue
		}
		for _, val := range v {
			req.Header.Add(k, val)
		}
	}

	if strings.HasPrefix(r.URL.Path, "/qmt/") && req.Header.Get("X-Ashare-Token") == "" && s.config.WindowsToken != "" {
		req.Header.Set("X-Ashare-Token", s.config.WindowsToken)
	}

	return s.client.Do(req)
}

func main() {
	cfg, err := LoadConfig("config.json")
	if err != nil {
		log.Printf("failed to load config: %v, using defaults", err)
		cfg = DefaultConfig()
	}

	if cfg.WindowsToken == "" {
		cfg.WindowsToken = os.Getenv("ASHARE_WINDOWS_GATEWAY_TOKEN")
	}
	if os.Getenv("ASHARE_WINDOWS_GATEWAY_BASE_URL") != "" {
		cfg.WindowsBaseURL = os.Getenv("ASHARE_WINDOWS_GATEWAY_BASE_URL")
	}
	cfg.PythonBaseURL = resolvePythonBaseURL(cfg)

	srv := NewServer(cfg)
	log.Printf("Go Data Platform starting on %s", cfg.ListenAddr)
	log.Printf("Windows Gateway: %s", cfg.WindowsBaseURL)
	log.Printf("Python Control Plane: %s", cfg.PythonBaseURL)

	if err := http.ListenAndServe(cfg.ListenAddr, srv); err != nil {
		log.Fatalf("server failed: %v", err)
	}
}

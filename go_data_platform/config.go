package main

import (
	"encoding/json"
	"os"
)

type LaneConfig struct {
	Workers        int `json:"workers"`
	QueueSize      int `json:"queue_size"`
	QueueTimeoutMS int `json:"queue_timeout_ms"`
	Retries        int `json:"retries"`
}

type Config struct {
	ListenAddr     string                `json:"listen_addr"`
	WindowsBaseURL string                `json:"windows_base_url"`
	PythonBaseURL  string                `json:"python_base_url"`
	WindowsToken   string                `json:"windows_token"`
	Lanes          map[string]LaneConfig `json:"lanes"`
	RetryBackoffMS int                   `json:"retry_backoff_ms"`
	CacheTTLMS     int                   `json:"cache_ttl_ms"`
}

func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return DefaultConfig(), nil
	}
	var cfg Config
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	// TODO: Merge with defaults if missing
	return &cfg, nil
}

func DefaultConfig() *Config {
	return &Config{
		ListenAddr:     "0.0.0.0:18793",
		WindowsBaseURL: "http://192.168.122.66:18791",
		PythonBaseURL:  "http://127.0.0.1:8100",
		Lanes: map[string]LaneConfig{
			"quote":        {Workers: 16, QueueSize: 64, QueueTimeoutMS: 2000, Retries: 2},
			"account_fast": {Workers: 8, QueueSize: 32, QueueTimeoutMS: 5000, Retries: 1},
			"trade_slow":   {Workers: 4, QueueSize: 16, QueueTimeoutMS: 15000, Retries: 0},
			"internal":     {Workers: 12, QueueSize: 48, QueueTimeoutMS: 120000, Retries: 1},
		},
		RetryBackoffMS: 350,
		CacheTTLMS:     1000,
	}
}

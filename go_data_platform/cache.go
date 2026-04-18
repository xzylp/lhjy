package main

import (
	"sync"
	"time"
)

type cacheEntry struct {
	data      []byte
	expiresAt time.Time
}

type Cache struct {
	mu    sync.RWMutex
	items map[string]cacheEntry
	ttl   time.Duration
}

func NewCache(ttl time.Duration) *Cache {
	return &Cache{
		items: make(map[string]cacheEntry),
		ttl:   ttl,
	}
}

func (c *Cache) Get(key string) ([]byte, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	item, ok := c.items[key]
	if !ok || time.Now().After(item.expiresAt) {
		return nil, false
	}
	return item.data, true
}

func (c *Cache) Set(key string, data []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.items[key] = cacheEntry{
		data:      data,
		expiresAt: time.Now().Add(c.ttl),
	}
}

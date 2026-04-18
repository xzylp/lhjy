package main

import (
	"context"
	"fmt"
	"time"
)

type Job func(ctx context.Context) error

type Lane struct {
	name    string
	workers chan struct{}
	queue   chan Job
	timeout time.Duration
}

func NewLane(name string, cfg LaneConfig) *Lane {
	return &Lane{
		name:    name,
		workers: make(chan struct{}, cfg.Workers),
		queue:   make(chan Job, cfg.QueueSize),
		timeout: time.Duration(cfg.QueueTimeoutMS) * time.Millisecond,
	}
}

func (l *Lane) Start() {
	// 启动 worker 协程
	// 注意：目前的实现是常驻 worker
}

func (l *Lane) Submit(ctx context.Context, job Job) error {
	timer := time.NewTimer(l.timeout)
	defer timer.Stop()

	select {
	case l.queue <- job:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return fmt.Errorf("lane %s queue timeout", l.name)
	}
}

// 简单的并发池执行模型
func (l *Lane) Execute(ctx context.Context, job func() error) error {
	done := make(chan error, 1)
	
	err := l.Submit(ctx, func(jobCtx context.Context) error {
		// 进入 worker 池
		select {
		case l.workers <- struct{}{}:
			defer func() { <-l.workers }()
			done <- job()
		case <-jobCtx.Done():
			done <- jobCtx.Err()
		}
		return nil
	})

	if err != nil {
		return err
	}

	select {
	case res := <-done:
		return res
	case <-ctx.Done():
		return ctx.Err()
	}
}

type Scheduler struct {
	lanes map[string]*Lane
}

func NewScheduler(cfg map[string]LaneConfig) *Scheduler {
	lanes := make(map[string]*Lane)
	for name, lcfg := range cfg {
		lane := NewLane(name, lcfg)
		lanes[name] = lane
		// 启动消费循环
		go func(l *Lane) {
			for job := range l.queue {
				go job(context.Background())
			}
		}(lane)
	}
	return &Scheduler{lanes: lanes}
}

func (s *Scheduler) GetLane(name string) *Lane {
	return s.lanes[name]
}

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_catalog (
    dataset_name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    storage_kind TEXT NOT NULL DEFAULT '',
    retention_policy TEXT NOT NULL DEFAULT '',
    owner_module TEXT NOT NULL DEFAULT '',
    primary_keys TEXT NOT NULL DEFAULT '[]',
    partition_keys TEXT NOT NULL DEFAULT '[]',
    version TEXT NOT NULL DEFAULT 'v1',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_partitions (
    partition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_name TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT '',
    trade_date TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    file_format TEXT NOT NULL DEFAULT '',
    row_count INTEGER NOT NULL DEFAULT 0,
    symbol_count INTEGER NOT NULL DEFAULT 0,
    min_time TEXT NOT NULL DEFAULT '',
    max_time TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    checksum TEXT NOT NULL DEFAULT '',
    freshness_status TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    extra_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(dataset_name, period, trade_date, path),
    FOREIGN KEY(dataset_name) REFERENCES dataset_catalog(dataset_name)
);

CREATE INDEX IF NOT EXISTS idx_dataset_partitions_lookup
ON dataset_partitions(dataset_name, trade_date, period, updated_at DESC);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id TEXT PRIMARY KEY,
    dataset_name TEXT NOT NULL,
    period TEXT NOT NULL DEFAULT '',
    trade_date TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'running',
    row_count INTEGER NOT NULL DEFAULT 0,
    symbol_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_ingestion_runs_dataset
ON ingestion_runs(dataset_name, trade_date, started_at DESC);

CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT 'general',
    title TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    trade_date TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_documents_category_updated
ON documents(category, updated_at DESC);

CREATE TABLE IF NOT EXISTS stock_behavior_profiles (
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    style_tag TEXT NOT NULL DEFAULT '',
    optimal_hold_days INTEGER NOT NULL DEFAULT 1,
    board_success_rate_20d REAL NOT NULL DEFAULT 0.0,
    bomb_rate_20d REAL NOT NULL DEFAULT 0.0,
    next_day_premium_20d REAL NOT NULL DEFAULT 0.0,
    reseal_rate_20d REAL NOT NULL DEFAULT 0.0,
    avg_sector_rank_30d REAL NOT NULL DEFAULT 99.0,
    leader_frequency_30d REAL NOT NULL DEFAULT 0.0,
    summary_text TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(symbol, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_stock_behavior_profiles_trade_date
ON stock_behavior_profiles(trade_date, updated_at DESC);

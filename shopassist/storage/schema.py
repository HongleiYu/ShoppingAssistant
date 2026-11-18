"""Versioned schema for the durable conversation store.

`local_sqlite` is the offline demo adapter. The MySQL statements are for a
real MySQL-compatible server. They are not interchangeable labels.
"""

SQLITE_MIGRATION = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_profiles (
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  preferences_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, user_id)
);
CREATE TABLE IF NOT EXISTS session_messages (
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  message_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, session_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_user_time
  ON session_messages (tenant_id, user_id, created_at);
CREATE TABLE IF NOT EXISTS session_state (
  tenant_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, session_id)
);
CREATE TABLE IF NOT EXISTS session_summaries (
  tenant_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  watermark_message_id INTEGER NOT NULL,
  summary_text TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, session_id, version)
);
CREATE TABLE IF NOT EXISTS recent_memories (
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  source_message_id INTEGER NOT NULL,
  body TEXT NOT NULL,
  kind TEXT NOT NULL,
  relevance REAL NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY (tenant_id, user_id, memory_id)
);
"""

MYSQL_MIGRATION = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INT NOT NULL PRIMARY KEY,
  applied_at VARCHAR(40) NOT NULL
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS user_profiles (
  tenant_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  version INT NOT NULL,
  preferences_json LONGTEXT NOT NULL,
  updated_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (tenant_id, user_id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS session_messages (
  tenant_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  session_id VARCHAR(128) NOT NULL,
  message_id BIGINT NOT NULL,
  role VARCHAR(16) NOT NULL,
  body LONGTEXT NOT NULL,
  created_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (tenant_id, session_id, message_id),
  KEY idx_messages_user_time (tenant_id, user_id, created_at)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS session_state (
  tenant_id VARCHAR(64) NOT NULL,
  session_id VARCHAR(128) NOT NULL,
  version INT NOT NULL,
  state_json LONGTEXT NOT NULL,
  updated_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (tenant_id, session_id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS session_summaries (
  tenant_id VARCHAR(64) NOT NULL,
  session_id VARCHAR(128) NOT NULL,
  version INT NOT NULL,
  watermark_message_id BIGINT NOT NULL,
  summary_text LONGTEXT NOT NULL,
  source_kind VARCHAR(32) NOT NULL,
  provenance_json LONGTEXT NOT NULL,
  created_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (tenant_id, session_id, version)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS recent_memories (
  tenant_id VARCHAR(64) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  memory_id VARCHAR(128) NOT NULL,
  session_id VARCHAR(128) NOT NULL,
  source_message_id BIGINT NOT NULL,
  body LONGTEXT NOT NULL,
  kind VARCHAR(32) NOT NULL,
  relevance DOUBLE NOT NULL,
  created_at VARCHAR(40) NOT NULL,
  expires_at VARCHAR(40) NOT NULL,
  PRIMARY KEY (tenant_id, user_id, memory_id),
  KEY idx_recent_window (tenant_id, user_id, created_at)
) ENGINE=InnoDB;
"""

SCHEMA_VERSION = 1

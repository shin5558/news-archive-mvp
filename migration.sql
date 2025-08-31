PRAGMA foreign_keys = ON;

-- 1) users
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  display_name TEXT,
  avatar_url TEXT,
  role TEXT DEFAULT 'user',        -- user / mod / admin
  created_at TEXT
);

-- 2) threads の列追加（存在しないときのみ）
-- SQLite は IF NOT EXISTS が列追加に無いので、失敗してもスルーでOK
ALTER TABLE threads ADD COLUMN created_by INTEGER REFERENCES users(id);
ALTER TABLE threads ADD COLUMN status TEXT DEFAULT 'open'; -- open / locked / hidden

-- 3) posts（返信ツリーは parent_post_id で1段まで想定）
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id),
  parent_post_id INTEGER REFERENCES posts(id),
  content TEXT NOT NULL,
  is_hidden INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_thread_id ON posts(thread_id);
CREATE INDEX IF NOT EXISTS idx_posts_thread_time ON posts(thread_id, created_at);

-- 4) ai_summaries（AI整理結果のキャッシュ）
CREATE TABLE IF NOT EXISTS ai_summaries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  model TEXT,
  mode TEXT,               -- 'conversation' or 'analysis'
  content TEXT NOT NULL,
  hash_key TEXT NOT NULL,  -- 入力のハッシュ
  created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_summaries_thread_hash ON ai_summaries(thread_id, hash_key);

-- 5) reports（通報）
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_type TEXT,        -- 'post' | 'thread' | 'user'
  target_id INTEGER,
  reported_by INTEGER REFERENCES users(id),
  reason TEXT,
  status TEXT DEFAULT 'open', -- open / closed
  created_at TEXT,
  resolved_by INTEGER REFERENCES users(id),
  resolved_at TEXT
);

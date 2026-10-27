-- StateLedger schema, version 1.
-- Applied by stateledger.store.LedgerStore.initialize() through
-- sqlite3.executescript(). The transaction lives here so a failed migration
-- rolls back both the DDL and PRAGMA user_version.

BEGIN IMMEDIATE;

PRAGMA user_version = 1;

CREATE TABLE tasks (
  id          TEXT PRIMARY KEY,
  repo_root   TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE sessions (
  id           TEXT PRIMARY KEY,
  task_id      TEXT NOT NULL REFERENCES tasks(id),
  client       TEXT,
  started_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE checkpoints (
  id             TEXT PRIMARY KEY,
  task_id        TEXT NOT NULL REFERENCES tasks(id),
  seq            INTEGER NOT NULL CHECK (seq >= 1),
  parent_id      TEXT REFERENCES checkpoints(id),
  session_id     TEXT REFERENCES sessions(id),
  goal           TEXT NOT NULL,
  git_commit     TEXT,
  git_branch     TEXT,
  git_dirty      INTEGER CHECK (git_dirty IS NULL OR git_dirty IN (0, 1)),
  git_error      TEXT,
  payload_json   TEXT NOT NULL CHECK (json_valid(payload_json)),
  payload_sha256 TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  UNIQUE (task_id, seq)
);

CREATE TABLE decisions (
  id            TEXT PRIMARY KEY,
  checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
  position      INTEGER NOT NULL CHECK (position >= 0),
  text          TEXT NOT NULL,
  rationale     TEXT,
  supersedes_id TEXT,
  created_at    TEXT NOT NULL,
  UNIQUE (checkpoint_id, position)
);

CREATE TABLE checkpoint_files (
  checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id),
  position      INTEGER NOT NULL CHECK (position >= 0),
  path          TEXT NOT NULL,
  change_type   TEXT NOT NULL CHECK (
    change_type IN ('added', 'modified', 'deleted', 'renamed')
  ),
  sha256        TEXT,
  size_bytes    INTEGER,
  PRIMARY KEY (checkpoint_id, path),
  UNIQUE (checkpoint_id, position)
);

CREATE TABLE evidence (
  id             TEXT PRIMARY KEY,
  checkpoint_id  TEXT NOT NULL REFERENCES checkpoints(id),
  position       INTEGER NOT NULL CHECK (position >= 0),
  check_key      TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (
    kind IN ('test', 'build', 'lint', 'typecheck', 'manual', 'other')
  ),
  command        TEXT NOT NULL,
  status         TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'error')),
  exit_code      INTEGER CHECK (exit_code IS NULL OR exit_code BETWEEN -255 AND 255),
  scope_json     TEXT NOT NULL CHECK (
    json_valid(scope_json) AND json_array_length(scope_json) >= 1
  ),
  output_excerpt TEXT,
  git_commit     TEXT,
  captured_at    TEXT NOT NULL,
  coverage       TEXT NOT NULL CHECK (coverage IN ('files', 'scope_only')),
  superseded_by  TEXT REFERENCES evidence(id),
  trust          TEXT NOT NULL DEFAULT 'agent_reported' CHECK (
    trust IN ('agent_reported', 'server_verified')
  ),
  UNIQUE (checkpoint_id, position)
);

CREATE TABLE evidence_files (
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
  position    INTEGER NOT NULL CHECK (position >= 0),
  path        TEXT NOT NULL,
  sha256      TEXT,
  PRIMARY KEY (evidence_id, path),
  UNIQUE (evidence_id, position)
);

-- Everything below is insert-only. Only these are mutable after insert:
--   sessions.last_seen_at            (touch only; every other column frozen)
--   evidence.superseded_by           (one NULL -> non-NULL link only)
-- No table permits DELETE.

CREATE TRIGGER tasks_immutable_update
BEFORE UPDATE ON tasks
BEGIN
  SELECT RAISE(ABORT, 'tasks are immutable');
END;

CREATE TRIGGER tasks_immutable_delete
BEFORE DELETE ON tasks
BEGIN
  SELECT RAISE(ABORT, 'tasks cannot be deleted');
END;

CREATE TRIGGER sessions_update_last_seen_only
BEFORE UPDATE ON sessions
WHEN NOT (
  NEW.id IS OLD.id
  AND NEW.task_id IS OLD.task_id
  AND NEW.client IS OLD.client
  AND NEW.started_at IS OLD.started_at
  AND NEW.last_seen_at IS NOT OLD.last_seen_at
)
BEGIN
  SELECT RAISE(ABORT, 'sessions are immutable except last_seen_at');
END;

CREATE TRIGGER sessions_immutable_delete
BEFORE DELETE ON sessions
BEGIN
  SELECT RAISE(ABORT, 'sessions cannot be deleted');
END;

CREATE TRIGGER checkpoints_immutable_update
BEFORE UPDATE ON checkpoints
BEGIN
  SELECT RAISE(ABORT, 'checkpoints are append-only');
END;

CREATE TRIGGER checkpoints_immutable_delete
BEFORE DELETE ON checkpoints
BEGIN
  SELECT RAISE(ABORT, 'checkpoints cannot be deleted');
END;

CREATE TRIGGER decisions_immutable_update
BEFORE UPDATE ON decisions
BEGIN
  SELECT RAISE(ABORT, 'decisions are append-only');
END;

CREATE TRIGGER decisions_immutable_delete
BEFORE DELETE ON decisions
BEGIN
  SELECT RAISE(ABORT, 'decisions cannot be deleted');
END;

CREATE TRIGGER checkpoint_files_immutable_update
BEFORE UPDATE ON checkpoint_files
BEGIN
  SELECT RAISE(ABORT, 'checkpoint_files are append-only');
END;

CREATE TRIGGER checkpoint_files_immutable_delete
BEFORE DELETE ON checkpoint_files
BEGIN
  SELECT RAISE(ABORT, 'checkpoint_files cannot be deleted');
END;

CREATE TRIGGER evidence_supersede_once
BEFORE UPDATE ON evidence
WHEN NOT (
  OLD.superseded_by IS NULL
  AND NEW.superseded_by IS NOT NULL
  AND NEW.id IS OLD.id
  AND NEW.checkpoint_id IS OLD.checkpoint_id
  AND NEW.position IS OLD.position
  AND NEW.check_key IS OLD.check_key
  AND NEW.kind IS OLD.kind
  AND NEW.command IS OLD.command
  AND NEW.status IS OLD.status
  AND NEW.exit_code IS OLD.exit_code
  AND NEW.scope_json IS OLD.scope_json
  AND NEW.output_excerpt IS OLD.output_excerpt
  AND NEW.git_commit IS OLD.git_commit
  AND NEW.captured_at IS OLD.captured_at
  AND NEW.coverage IS OLD.coverage
  AND NEW.trust IS OLD.trust
)
BEGIN
  SELECT RAISE(
    ABORT,
    'evidence rows are immutable except a one-time superseded_by link'
  );
END;

CREATE TRIGGER evidence_immutable_delete
BEFORE DELETE ON evidence
BEGIN
  SELECT RAISE(ABORT, 'evidence cannot be deleted');
END;

CREATE TRIGGER evidence_files_immutable_update
BEFORE UPDATE ON evidence_files
BEGIN
  SELECT RAISE(ABORT, 'evidence_files are append-only');
END;

CREATE TRIGGER evidence_files_immutable_delete
BEFORE DELETE ON evidence_files
BEGIN
  SELECT RAISE(ABORT, 'evidence_files cannot be deleted');
END;

COMMIT;
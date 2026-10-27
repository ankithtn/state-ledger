-- StateLedger schema, version 3.
-- Separates verification capture from checkpoint saving:
--   * verification_captures + verification_capture_files hold the immutable
--     result snapshot (Git state and scope hashes) taken when the result was
--     reported;
--   * evidence.capture_id links a checkpoint's evidence row to that snapshot;
--   * checkpoint saving copies the snapshot and never re-hashes the tree.

BEGIN IMMEDIATE;

PRAGMA user_version = 3;

CREATE TABLE verification_captures (
  id             TEXT PRIMARY KEY,
  task_id        TEXT NOT NULL REFERENCES tasks(id),
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
  git_branch     TEXT,
  git_dirty      INTEGER CHECK (git_dirty IS NULL OR git_dirty IN (0, 1)),
  git_error      TEXT,
  captured_at    TEXT NOT NULL,
  coverage       TEXT NOT NULL CHECK (coverage IN ('files', 'scope_only')),
  trust          TEXT NOT NULL DEFAULT 'agent_reported' CHECK (
    trust = 'agent_reported'
  )
);

CREATE TABLE verification_capture_files (
  capture_id TEXT NOT NULL REFERENCES verification_captures(id),
  position   INTEGER NOT NULL CHECK (position >= 0),
  path       TEXT NOT NULL,
  sha256     TEXT NOT NULL CHECK (length(sha256) = 64),
  PRIMARY KEY (capture_id, path),
  UNIQUE (capture_id, position)
);

ALTER TABLE evidence ADD COLUMN capture_id TEXT REFERENCES verification_captures(id);

-- Recreate the one-time supersede guard so capture_id is immutable too.
DROP TRIGGER evidence_supersede_once;

CREATE TRIGGER evidence_supersede_once
BEFORE UPDATE ON evidence
WHEN NOT (
  OLD.superseded_by IS NULL
  AND NEW.superseded_by IS NOT NULL
  AND NEW.id IS OLD.id
  AND NEW.checkpoint_id IS OLD.checkpoint_id
  AND NEW.capture_id IS OLD.capture_id
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

CREATE TRIGGER verification_captures_immutable_update
BEFORE UPDATE ON verification_captures
BEGIN
  SELECT RAISE(ABORT, 'verification captures are append-only');
END;

CREATE TRIGGER verification_captures_immutable_delete
BEFORE DELETE ON verification_captures
BEGIN
  SELECT RAISE(ABORT, 'verification captures cannot be deleted');
END;

CREATE TRIGGER verification_capture_files_immutable_update
BEFORE UPDATE ON verification_capture_files
BEGIN
  SELECT RAISE(ABORT, 'verification capture files are append-only');
END;

CREATE TRIGGER verification_capture_files_immutable_delete
BEFORE DELETE ON verification_capture_files
BEGIN
  SELECT RAISE(ABORT, 'verification capture files cannot be deleted');
END;

COMMIT;
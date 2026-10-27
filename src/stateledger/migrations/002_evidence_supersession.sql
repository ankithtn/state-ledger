-- StateLedger schema, version 2.
-- Hardens evidence supersession without rebuilding the table:
--   * an evidence row cannot supersede itself;
--   * a supersession link cannot cross task boundaries.
-- The one-time NULL -> non-NULL rule from migration 001 still applies.

BEGIN IMMEDIATE;

PRAGMA user_version = 2;

CREATE TRIGGER evidence_no_self_supersede
BEFORE UPDATE OF superseded_by ON evidence
WHEN NEW.superseded_by IS NOT NULL AND NEW.superseded_by = NEW.id
BEGIN
  SELECT RAISE(ABORT, 'evidence cannot supersede itself');
END;

CREATE TRIGGER evidence_supersede_same_task
BEFORE UPDATE OF superseded_by ON evidence
WHEN NEW.superseded_by IS NOT NULL
AND (
  SELECT c.task_id FROM checkpoints c WHERE c.id = NEW.checkpoint_id
) IS NOT (
  SELECT c.task_id
  FROM evidence e
  JOIN checkpoints c ON c.id = e.checkpoint_id
  WHERE e.id = NEW.superseded_by
)
BEGIN
  SELECT RAISE(ABORT, 'evidence supersession must stay within one task');
END;

COMMIT;
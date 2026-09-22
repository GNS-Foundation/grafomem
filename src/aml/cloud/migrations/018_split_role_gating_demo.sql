-- SCRATCH / DEMO ONLY — do NOT merge. Deliberately violates the split-role grant rule:
-- CREATE TABLE with no GRANT to the runtime role, to prove migrations-split-role now GATES (red).
CREATE TABLE split_role_gating_demo (id TEXT PRIMARY KEY);

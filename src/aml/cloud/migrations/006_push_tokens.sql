-- Adds table for approver push tokens

CREATE TABLE IF NOT EXISTS approver_push_tokens (
    approver_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    push_token TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT timezone('utc', now()) NOT NULL,
    UNIQUE(approver_id, push_token)
);

CREATE INDEX IF NOT EXISTS idx_approver_push_tokens_approver_id ON approver_push_tokens(approver_id);

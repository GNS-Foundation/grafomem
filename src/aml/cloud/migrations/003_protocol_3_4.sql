-- Migration: Add Formal Protocol 3.4 Governance Record
-- Date: 2026-06-23
-- Description: Adds a JSONB column to store the exact recomputable 3.4 contract bytes.
-- We do not drop the legacy 'coverage' column to maintain backwards compatibility 
-- with previously-signed Phase 0-2 certificates.

ALTER TABLE erasure_certificates
ADD COLUMN IF NOT EXISTS governance_record JSONB;

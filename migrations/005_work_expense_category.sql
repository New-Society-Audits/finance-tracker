-- Migration 005: Add Work expense category
-- Run in Supabase SQL Editor

INSERT INTO categories (name, type) VALUES
  ('Work', 'expense')
ON CONFLICT (name) DO NOTHING;

-- Migration 003: Add category type (expense vs income) and seed income categories
-- Run in Supabase SQL Editor

ALTER TABLE categories ADD COLUMN type TEXT NOT NULL DEFAULT 'expense';

-- Tag existing categories as expense type (already correct via default)

-- Seed income categories
INSERT INTO categories (name, type) VALUES
  ('Salary', 'income'),
  ('Refund', 'income'),
  ('Gift', 'income'),
  ('Investment', 'income'),
  ('Other Income', 'income')
ON CONFLICT (name) DO NOTHING;

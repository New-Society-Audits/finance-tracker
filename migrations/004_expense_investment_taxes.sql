-- Migration 004: Add Investment and Taxes expense categories
-- Run in Supabase SQL Editor

-- Rename the income "Investment" category to avoid name collision
UPDATE categories SET name = 'Investment Returns' WHERE name = 'Investment' AND type = 'income';

-- Seed new expense categories
INSERT INTO categories (name, type) VALUES
  ('Investment', 'expense'),
  ('Taxes', 'expense')
ON CONFLICT (name) DO NOTHING;

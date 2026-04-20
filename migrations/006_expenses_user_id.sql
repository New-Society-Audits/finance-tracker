-- Migration 006: Scope expenses to a user
-- Run in Supabase SQL Editor.
--
-- Adds user_id to expenses so each row belongs to a specific auth.users account.
-- Categories remain shared across all users.
--
-- BEFORE RUNNING: replace <YOUR_USER_UUID> below with your own auth.users.id
-- (find it in Supabase → Authentication → Users). The backfill assigns every
-- existing expense to that account, then the column is made NOT NULL.

ALTER TABLE expenses
  ADD COLUMN user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE;

UPDATE expenses SET user_id = '<YOUR_USER_UUID>' WHERE user_id IS NULL;

ALTER TABLE expenses ALTER COLUMN user_id SET NOT NULL;

CREATE INDEX expenses_user_id_idx ON expenses(user_id);

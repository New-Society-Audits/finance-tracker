-- Migration 007: Enable Row-Level Security on public tables
-- Run in Supabase SQL Editor.
--
-- The app uses the service-role key for all data operations, which bypasses
-- RLS — so enabling RLS without policies is safe and does not break anything.
-- This locks down the anon/public REST endpoint so the tables can't be read
-- or written by anyone holding just the anon key.

ALTER TABLE expenses   ENABLE ROW LEVEL SECURITY;
ALTER TABLE categories ENABLE ROW LEVEL SECURITY;

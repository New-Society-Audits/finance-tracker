-- Migration 002: Drop custom users table
-- Auth is now handled by Supabase Auth (auth.users), so this table is no longer needed.

DROP TABLE IF EXISTS users;

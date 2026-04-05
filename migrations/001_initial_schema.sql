-- Migration 001: Initial schema
-- Run against Supabase SQL Editor on 2026-04-05
--
-- Creates the three core tables and seeds a default user + categories.
-- This migration is idempotent for seed data (ON CONFLICT DO NOTHING).

-- Single-user auth: stores the login credentials checked by /login
CREATE TABLE users (
    id       SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL
);

-- Predefined expense categories (e.g. Food, Transport)
CREATE TABLE categories (
    id   SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

-- Individual expense entries, optionally linked to a category and receipt file
CREATE TABLE expenses (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    amount       DOUBLE PRECISION NOT NULL,
    date         TEXT NOT NULL,                          -- stored as YYYY-MM-DD string
    category_id  INTEGER REFERENCES categories(id),     -- nullable until categorized
    receipt_path TEXT,                                   -- local path to uploaded image
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Seed a default admin user (single-user app)
INSERT INTO users (username, password) VALUES ('admin', 'admin123')
ON CONFLICT (username) DO NOTHING;

-- Seed default categories
INSERT INTO categories (name) VALUES
  ('Food'), ('Transport'), ('Shopping'),
  ('Entertainment'), ('Health'), ('Utilities'), ('Other')
ON CONFLICT (name) DO NOTHING;

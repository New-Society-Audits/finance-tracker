-- Migration 001: Initial schema
-- Run against Supabase SQL Editor on 2026-04-05

CREATE TABLE users (
    id       SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL
);

CREATE TABLE categories (
    id   SERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE expenses (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    amount       DOUBLE PRECISION NOT NULL,
    date         TEXT NOT NULL,
    category_id  INTEGER REFERENCES categories(id),
    receipt_path TEXT,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Seed data
INSERT INTO users (username, password) VALUES ('admin', 'admin123')
ON CONFLICT (username) DO NOTHING;

INSERT INTO categories (name) VALUES
  ('Food'), ('Transport'), ('Shopping'),
  ('Entertainment'), ('Health'), ('Utilities'), ('Other')
ON CONFLICT (name) DO NOTHING;

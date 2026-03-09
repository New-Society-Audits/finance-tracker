import sqlite3

DB_PATH = "finance.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows behave like dicts
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables() -> None:
    conn = get_connection()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                amount       REAL NOT NULL,
                date         TEXT NOT NULL,
                category_id  INTEGER REFERENCES categories(id),
                receipt_path TEXT,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
    conn.close()


def seed() -> None:
    conn = get_connection()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, password) VALUES (?, ?)",
            ("admin", "admin123"),
        )
        for name in ["Food", "Transport", "Shopping", "Entertainment", "Health", "Utilities", "Other"]:
            conn.execute(
                "INSERT OR IGNORE INTO categories (name) VALUES (?)",
                (name,),
            )
    conn.close()


def init_db() -> None:
    create_tables()
    seed()

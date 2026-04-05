"""
Supabase client setup.

Provides a singleton Supabase client used by all route modules
to query the database. Reads connection credentials from .env.
"""

from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]

# Singleton — created once on first call to get_client()
_client: Optional[Client] = None


def get_client() -> Client:
    """Return the shared Supabase client, creating it on first use."""
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def init_db() -> None:
    """No-op — tables are managed via SQL migrations in the Supabase dashboard."""
    pass

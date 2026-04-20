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
SUPABASE_ANON_KEY = os.environ["SUPABASE_KEY"]        # publishable/anon key (for auth)
SUPABASE_SECRET_KEY = os.environ["SUPABASE_SECRET_KEY"]  # service-role key (for data ops)

# Singletons — created once on first call
_client: Optional[Client] = None
_auth_client: Optional[Client] = None


def get_client() -> Client:
    """Return the service-role Supabase client for data operations (bypasses RLS)."""
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _client


def get_auth_client() -> Client:
    """Return the anon-key Supabase client for authentication (sign in/up/out)."""
    global _auth_client
    if _auth_client is None:
        _auth_client = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return _auth_client

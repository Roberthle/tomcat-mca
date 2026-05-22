"""
Apollo On-Demand Contact Enricher
Used by both Tomcat MCA (port 5051) and Tomcat Capex (port 5050).

Finds decision-makers (Owner/CEO/President) for a company by name + city.
Results are cached in the lead_contact_cache table for 24h — one Apollo
credit per company, not per broker click.

Usage:
    from apollo_enricher import fetch_apollo_contacts, init_contact_cache
    init_contact_cache(conn)
    contacts = fetch_apollo_contacts(company_name, city, state, conn, lead_id)
"""

import os, json, sqlite3, hashlib
from datetime import datetime, timedelta
import requests as _req

APOLLO_KEY = os.environ.get('APOLLO_API_KEY', '')

APOLLO_SEARCH_URL = 'https://api.apollo.io/api/v1/mixed_people/search'

# Decision-maker titles to target, in priority order
DECISION_TITLES = [
    'Owner', 'Co-Owner', 'CEO', 'Chief Executive Officer',
    'President', 'Founder', 'Co-Founder', 'Principal',
    'Managing Partner', 'Partner', 'General Manager',
    'Managing Member', 'Managing Director', 'Proprietor',
    'Executive Director', 'Operations Manager', 'Director',
]

CACHE_TTL_HOURS = 24


# ── DB cache table ────────────────────────────────────────────────────────────

def init_contact_cache(conn):
    """Create the contact cache table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_contact_cache (
            lead_id     TEXT PRIMARY KEY,
            contacts_json TEXT,
            fetched_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            apollo_hit  INTEGER DEFAULT 0   -- 1 if Apollo returned results
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contact_unlocks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id     TEXT NOT NULL,
            buyer_email TEXT,
            unlocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def _get_cached(conn, lead_id):
    """Return cached contacts if fresh, else None."""
    row = conn.execute(
        "SELECT contacts_json, fetched_at FROM lead_contact_cache WHERE lead_id = ?",
        [str(lead_id)]
    ).fetchone()
    if not row:
        return None
    fetched_at = datetime.fromisoformat(row[0 if row[1] is None else 1])
    try:
        fetched_at = datetime.fromisoformat(row[1])
        if datetime.utcnow() - fetched_at < timedelta(hours=CACHE_TTL_HOURS):
            return json.loads(row[0])
    except Exception:
        pass
    return None


def _save_cache(conn, lead_id, contacts):
    conn.execute(
        """INSERT OR REPLACE INTO lead_contact_cache
           (lead_id, contacts_json, fetched_at, apollo_hit)
           VALUES (?, ?, datetime('now'), ?)""",
        [str(lead_id), json.dumps(contacts), 1 if contacts else 0]
    )
    conn.commit()


def _log_unlock(conn, lead_id, buyer_email=''):
    conn.execute(
        "INSERT INTO contact_unlocks (lead_id, buyer_email) VALUES (?, ?)",
        [str(lead_id), buyer_email]
    )
    conn.commit()


# ── Apollo API call ───────────────────────────────────────────────────────────

def _call_apollo(company_name, city, state):
    """
    Query Apollo for top decision-makers at this company.
    Returns list of contact dicts.
    """
    if not APOLLO_KEY:
        return []

    payload = {
        'api_key':             APOLLO_KEY,
        'q_organization_name': company_name,
        'person_titles':       DECISION_TITLES[:8],   # Apollo accepts up to 8
        'person_seniorities':  ['owner', 'c_suite', 'vp', 'director', 'manager'],
        'page':                1,
        'per_page':            5,
    }

    # Add location context if available
    if city:
        payload['person_locations'] = [f"{city}, {state}" if state else city]

    try:
        r = _req.post(APOLLO_SEARCH_URL, json=payload, timeout=12)
        data = r.json()

        people = data.get('people', [])
        contacts = []
        for p in people[:3]:   # top 3 decision-makers
            org = (p.get('organization') or {})
            contact = {
                'name':        f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                'title':       p.get('title', ''),
                'email':       p.get('email', ''),
                'phone':       p.get('phone_numbers', [{}])[0].get('sanitized_number', '')
                               if p.get('phone_numbers') else '',
                'linkedin_url': p.get('linkedin_url', ''),
                'company':     org.get('name', company_name),
                'confidence':  'verified' if p.get('email_status') == 'verified' else 'likely',
            }
            # Only include if we got at least a name
            if contact['name']:
                contacts.append(contact)
        return contacts

    except Exception as e:
        return []


# ── Public interface ──────────────────────────────────────────────────────────

def fetch_apollo_contacts(company_name, city, state, conn, lead_id, buyer_email=''):
    """
    Main entry point. Returns list of up to 3 contact dicts.
    Uses 24h cache — only burns 1 Apollo credit per company per day.

    Each contact dict:
        name, title, email, phone, linkedin_url, company, confidence
    """
    # 1. Check cache first (no credit used)
    cached = _get_cached(conn, lead_id)
    if cached is not None:
        _log_unlock(conn, lead_id, buyer_email)
        return cached

    # 2. Call Apollo (1 credit)
    contacts = _call_apollo(company_name, city, state)

    # 3. Cache result (even empty, to avoid hammering API on dead companies)
    _save_cache(conn, lead_id, contacts)
    _log_unlock(conn, lead_id, buyer_email)

    return contacts


def get_unlock_stats(conn):
    """Return unlock analytics — which leads are being worked."""
    rows = conn.execute("""
        SELECT lead_id, COUNT(*) as unlocks, MAX(unlocked_at) as last_unlock
        FROM contact_unlocks
        GROUP BY lead_id
        ORDER BY unlocks DESC
        LIMIT 50
    """).fetchall()
    return [dict(r) for r in rows]

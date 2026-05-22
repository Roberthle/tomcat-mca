"""
Tomcat MCA — Signal + Enrichment Engine v2
/Users/robertle/tomcat_mca/mca_signal_engine.py

For each MCA lead, stacks real intelligence signals:

  S1_UCC         — Base: verified UCC-1 filing (all leads)
  S2_EXPANSION   — Google News: growth, contracts, new locations (last 365d)
  S3_HIRING      — Indeed: actively hiring staff (revenue signal)
  S4_TAX_LIEN    — News/records: IRS or state tax lien (distress)
  S5_JUDGMENT    — News: court judgment (distress = consolidation play)
  S6_DISTRESS    — News: closed, bankrupt, complaints, financial trouble

Also enriches: phone number (BBB + Yelp) while scanning.

Priority: Hot leads first (days_to_lapse <= 30), then stacked, then lapsed.

Run:
  python3 mca_signal_engine.py --limit 200 --hot-only
  python3 mca_signal_engine.py --limit 500
"""

import os, re, time, sqlite3, json, logging, argparse, random
import requests
from bs4 import BeautifulSoup
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta
from urllib.parse import quote_plus

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'leads', 'tomcat_mca.db')
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [MCA-Signal] %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, 'signal_engine.log'), mode='a'),
    ]
)
log = logging.getLogger('TomcatMCA.Signals')

PHONE_RE = re.compile(r'(?<!\d)(\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4})(?!\d)')
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.3; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15"
]

def get_random_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "application/rss+xml, application/xml, text/xml, */*"
    }


# ── Expansion trigger keywords ────────────────────────────────────────────────

EXPANSION_TRIGGERS = [
    'new location', 'new facility', 'new branch', 'grand opening',
    'opening soon', 'ribbon cutting', 'breaking ground', 'groundbreaking',
    'new warehouse', 'new plant', 'new headquarters', 'expanding to',
    'expands into', 'awarded contract', 'wins contract', 'secures contract',
    'awarded bid', 'major project', 'fleet expansion', 'new equipment',
    'revenue growth', 'record revenue', 'raises funding', 'secures funding',
    'million dollar', 'new investment', 'hiring', 'creating jobs', 'adding jobs',
    'new store', 'new restaurant', 'opened', 'celebrates opening',
]

NOISE_TRIGGERS = [
    'permanently closed', 'bankrupt', 'shutdown', 'laid off', 'mass layoff',
    'ceases operation', 'discontinue', 'evicted',
]

DISTRESS_KW = [
    'bankrupt', 'chapter 11', 'chapter 7', 'insolvent', 'receivership',
    'permanently closed', 'going out of business', 'mass layoff', 'laid off',
    'tax lien', 'irs lien', 'federal tax lien', 'state tax lien', 'delinquent tax',
    'tax debt', 'back taxes',
    'court judgment', 'civil judgment', 'money judgment', 'judgment lien',
    'lawsuit', 'sued', 'plaintiff', 'defendant', 'breach of contract',
    'fraud', 'criminal charge', 'arrested', 'indicted',
    'complaint', 'bbb complaint', 'consumer complaint', 'better business bureau',
    'scam', 'ripoff', 'negative review',
]

# MCA-specific hiring keywords (any hiring = revenue signal for MCA borrowers)
MCA_HIRING_KW = [
    'cashier', 'server', 'cook', 'chef', 'bartender', 'store manager',
    'shift manager', 'retail associate', 'sales associate', 'assistant manager',
    'foreman', 'laborer', 'electrician', 'plumber', 'hvac', 'carpenter',
    'welder', 'construction worker', 'project manager', 'site supervisor',
    'truck driver', 'cdl driver', 'delivery driver', 'dispatcher', 'warehouse',
    'medical assistant', 'dental assistant', 'front desk', 'office manager',
    'billing specialist', 'automotive technician', 'mechanic', 'service advisor',
    'operations manager', 'account manager', 'customer service', 'sales rep',
    'now hiring', 'we are hiring', 'job opening', 'join our team',
    'full time', 'part time', 'immediate opening',
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def clean_name(company: str) -> str:
    """Strip legal suffixes for cleaner news searches."""
    return re.sub(
        r'\b(LLC|INC|CORP|LTD|CO|LP|LLP|PARTNERSHIP|ASSOCIATES|GROUP|DBA)\b\.?',
        '', company, flags=re.IGNORECASE
    ).strip().strip(',').strip()


def get_html(url: str, timeout: int = 12, referer: str = None) -> str:
    h = get_random_headers()
    if referer:
        h['Referer'] = referer
    try:
        r = requests.get(url, headers=h, timeout=timeout, allow_redirects=True)
        if r.ok:
            return r.text
    except Exception:
        pass
    return ''


def extract_phone(text: str) -> str:
    for m in PHONE_RE.findall(text):
        d = re.sub(r'\D', '', m)
        if len(d) == 10 and d[0] not in ('0', '1'):
            return f"({d[:3]}) {d[3:6]}-{d[6:]}"
    return ''


# ── DB ────────────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    for col, default in [
        ('signals_json', "DEFAULT '[]'"),
        ('signal_score', 'DEFAULT 0'),
        ('signal_tier',  "DEFAULT 'S1'"),
        ('signals_checked_at', ''),
        ('phone', ''),
        ('enriched_at', ''),
    ]:
        try:
            conn.execute(f"ALTER TABLE mca_leads ADD COLUMN {col} TEXT {default}")
        except Exception:
            pass
    conn.commit()
    conn.close()


def get_leads(limit: int, hot_only: bool) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    where = "company_name IS NOT NULL AND (signals_checked_at IS NULL OR signals_checked_at = '')"
    if hot_only:
        where += " AND days_to_lapse <= 30 AND days_to_lapse >= -90"
    rows = conn.execute(f"""
        SELECT id, company_name, dba_name, city, state, zipcode,
               source_state, secured_party, collateral_desc,
               days_to_lapse, stack_depth, funder_tier,
               signals_json, phone
        FROM mca_leads WHERE {where}
        ORDER BY
            CASE WHEN days_to_lapse IS NULL THEN 9999 ELSE days_to_lapse END ASC,
            stack_depth DESC
        LIMIT ?
    """, [limit]).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_result(lead_id: int, signals: list, score: int, tier: str, phone: str = ''):
    conn = sqlite3.connect(DB_PATH)
    fields = [
        ('signals_json', json.dumps(signals)),
        ('signal_score', score),
        ('signal_tier', tier),
        ('signals_checked_at', datetime.now().isoformat()),
    ]
    if phone:
        fields += [('phone', phone), ('enriched_at', datetime.now().isoformat())]
    set_sql = ', '.join(f'{k} = ?' for k, _ in fields)
    vals = [v for _, v in fields] + [lead_id]
    conn.execute(f"UPDATE mca_leads SET {set_sql} WHERE id = ?", vals)
    conn.commit()
    conn.close()


# ── Bing News RSS ───────────────────────────────────────────────────────────

def bing_news_search(query: str, days_back: int = 365) -> list:
    """Search Bing News RSS — unblockable, real articles."""
    url = f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss"
    try:
        h = get_random_headers()
        r = requests.get(url, headers=h, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        cutoff = datetime.now() - timedelta(days=days_back)
        items = []
        for item in root.findall('.//item'):
            title  = (item.findtext('title') or '').strip()
            desc   = re.sub(r'<[^>]+>', '', item.findtext('description') or '')
            pub    = (item.findtext('pubDate') or '').strip()
            link   = (item.findtext('link') or '').strip()
            source = (item.findtext('source') or '').strip()
            try:
                from email.utils import parsedate_to_datetime
                if parsedate_to_datetime(pub).replace(tzinfo=None) < cutoff:
                    continue
            except Exception:
                pass
            items.append({'title': title, 'desc': desc[:200], 'pub': pub,
                          'link': link, 'source': source})
        return items
    except Exception:
        return []


# ── Signal Checks ─────────────────────────────────────────────────────────────

def check_expansion(company: str, city: str, state: str) -> dict:
    """Google News: growth, contracts, new locations."""
    clean = clean_name(company)
    queries = [
        f'"{clean}" {city} "new location" OR "expanding" OR "new facility" OR "grand opening"',
        f'"{clean}" {state} contract OR opening OR expansion OR awarded OR "record revenue"',
        f'"{clean}" {city} {state}',
    ]
    best, best_score = None, 0

    for q in queries:
        articles = bing_news_search(q, days_back=365)
        time.sleep(random.uniform(2.0, 4.0))
        for art in articles[:5]:
            text = (art['title'] + ' ' + art['desc']).lower()
            if any(n in text for n in NOISE_TRIGGERS):
                continue
            triggers = [t for t in EXPANSION_TRIGGERS if t in text]
            score = len(triggers) * 10
            if clean.lower()[:8] in art['title'].lower():
                score += 25
            if city.lower() in text:
                score += 10
            if score > best_score:
                best_score = score
                best = {'art': art, 'triggers': triggers}

    if best and best_score >= 20:
        art = best['art']
        triggers = best['triggers']
        snippet = art['title'][:120] or art['desc'][:120]
        title_clean = art['title'].rsplit(' - ', 1)[0].strip()
        return {
            'type': 'S2_EXPANSION',
            'label': '📰 Business Expansion',
            'detail': snippet,
            'source': art.get('source', 'Bing News'),
            'pub': art.get('pub', ''),
            'link': f"https://www.google.com/search?q={quote_plus(title_clean)}",
            'triggers': triggers[:3],
            'weight': 30,
        }
    return {}


def check_hiring(company: str, city: str, state: str) -> dict:
    """Indeed: active job listings = revenue signal."""
    clean = clean_name(company)
    loc = quote_plus(f"{city} {state}".strip())
    q = quote_plus(clean)
    url = f"https://www.indeed.com/jobs?q={q}&l={loc}"
    html = get_html(url, referer='https://www.indeed.com/')
    time.sleep(random.uniform(1.0, 2.0))
    if not html:
        return {}
    soup = BeautifulSoup(html, 'html.parser')
    text = soup.get_text(' ', strip=True).lower()

    matched = [kw for kw in MCA_HIRING_KW if kw in text]
    # Require company name to appear (avoid false positives on broad searches)
    name_fragment = clean.lower()[:8]
    if not matched or name_fragment not in text:
        return {}

    # Count job cards found
    job_cards = soup.select('[data-testid="slider_item"], .job_seen_beacon, .tapItem, .jobsearch-ResultsList li')
    job_count = max(len(job_cards), 1 if matched else 0)

    return {
        'type': 'S3_HIRING',
        'label': f'👷 Actively Hiring ({job_count} roles)',
        'detail': f"Indeed: {job_count} open position(s) — roles: {', '.join(matched[:3])}",
        'source': 'Indeed',
        'triggers': matched[:5],
        'count': job_count,
        'weight': 20,
    }


def check_distress(company: str, city: str, state: str) -> list:
    """
    Google News: tax liens, judgments, closures, complaints.
    Returns a list (can find multiple distress signals at once).
    """
    clean = clean_name(company)
    signals = []

    queries = [
        # Distress / closure
        (f'"{clean}" {city} closed OR bankrupt OR "chapter 11" OR "going out of business" OR lawsuit',
         'closure_lawsuit'),
        # Tax lien specific
        (f'"{clean}" {state} "tax lien" OR "IRS lien" OR "delinquent tax" OR "tax debt"',
         'tax_lien'),
        # Judgment specific
        (f'"{clean}" judgment OR "court order" OR sued OR verdict',
         'judgment'),
    ]

    for q, q_type in queries:
        articles = bing_news_search(q, days_back=730)  # Look back 2 years for distress
        time.sleep(random.uniform(2.0, 4.0))
        for art in articles[:4]:
            text = (art['title'] + ' ' + art['desc']).lower()
            if clean.lower()[:6] not in text:
                continue  # Must be about this company
            matched_kw = [k for k in DISTRESS_KW if k in text]
            if not matched_kw:
                continue

            snippet = art['title'][:120] or art['desc'][:120]
            title_clean = art['title'].rsplit(' - ', 1)[0].strip()
            link = f"https://www.google.com/search?q={quote_plus(title_clean)}"

            # Classify type
            if any(k in text for k in ['tax lien', 'irs lien', 'federal tax lien', 'state tax lien', 'delinquent tax', 'tax debt', 'back taxes']):
                sig_type = 'S4_TAX_LIEN'
                label = '⚠ Tax Lien Detected'
                weight = 30
            elif any(k in text for k in ['court judgment', 'civil judgment', 'money judgment', 'judgment lien', 'lawsuit', 'sued', 'plaintiff', 'defendant', 'verdict']):
                sig_type = 'S5_JUDGMENT'
                label = '⚖ Court Judgment / Lawsuit'
                weight = 25
            else:
                sig_type = 'S6_DISTRESS'
                label = '🚨 Distress Signal'
                weight = 22

            # Avoid duplicates
            if any(s['type'] == sig_type for s in signals):
                continue

            signals.append({
                'type': sig_type,
                'label': label,
                'detail': snippet,
                'source': art.get('source', 'News'),
                'pub': art.get('pub', ''),
                'link': link,
                'triggers': matched_kw[:3],
                'weight': weight,
            })
            log.info(f"  ✅ {label}: {snippet[:60]}")

    return signals


# ── Contact Enrichment (BBB + Yelp) ─────────────────────────────────────────

def enrich_contact(company: str, city: str, state: str) -> dict:
    """Grab phone from BBB or Yelp while we're already scanning the lead."""
    result = {}

    # BBB
    q = quote_plus(f"{company} {city} {state}")
    html = get_html(f"https://www.bbb.org/search?find_text={q}&find_country=USA")
    time.sleep(random.uniform(2.5, 4.0))
    if html:
        soup = BeautifulSoup(html, 'html.parser')
        phone = extract_phone(soup.get_text())
        if phone:
            result['phone'] = phone
            result['source'] = 'BBB'

    # Yelp fallback
    if not result.get('phone'):
        loc = quote_plus(f"{city}, {state}")
        html2 = get_html(
            f"https://www.yelp.com/search?find_desc={quote_plus(company)}&find_loc={loc}",
            referer='https://www.yelp.com/'
        )
        time.sleep(random.uniform(2.0, 3.5))
        if html2:
            soup2 = BeautifulSoup(html2, 'html.parser')
            phone2 = extract_phone(soup2.get_text())
            if phone2:
                result['phone'] = phone2
                result['source'] = 'Yelp'

    return result


# ── Tier + Score ──────────────────────────────────────────────────────────────

def compute_tier(signals: list) -> tuple:
    base = 10
    total = base + sum(s.get('weight', 0) for s in signals)
    types = {s['type'] for s in signals}
    n = len(signals)

    if n >= 3:                                   tier = 'S4'
    elif n >= 2:                                 tier = 'S4'
    elif 'S4_TAX_LIEN' in types:                tier = 'S4'
    elif 'S5_JUDGMENT' in types:                tier = 'S4'
    elif 'S2_EXPANSION' in types:               tier = 'S2'
    elif 'S3_HIRING' in types:                  tier = 'S3'
    elif 'S6_DISTRESS' in types:               tier = 'S3'
    else:                                        tier = 'S1'

    return tier, min(total, 100)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(limit: int = 200, hot_only: bool = False, enrich_contacts: bool = True):
    init_db()
    leads = get_leads(limit, hot_only)
    log.info(f"{'='*60}")
    log.info(f"  MCA Signal Engine v2 — {len(leads)} leads | hot_only={hot_only}")
    log.info(f"  Sources: Google News RSS + Indeed + BBB + Yelp")
    log.info(f"{'='*60}")

    stats = {'expansion': 0, 'hiring': 0, 'tax_lien': 0,
             'judgment': 0, 'distress': 0, 'phone': 0, 'errors': 0}

    for i, lead in enumerate(leads, 1):
        company  = lead['company_name']
        city     = lead.get('city') or ''
        state    = lead.get('state') or lead.get('source_state') or ''
        dtl      = lead.get('days_to_lapse', '?')
        stack    = lead.get('stack_depth', 1) or 1

        log.info(f"[{i}/{len(leads)}] {company} | {city}, {state} | {dtl}d | stack={stack}")

        signals = []
        phone_found = ''

        try:
            # ── Expansion: Google News RSS ────────────────────────────────────
            exp = check_expansion(company, city, state)
            if exp:
                signals.append(exp)
                stats['expansion'] += 1
                log.info(f"  ✅ {exp['label']}: {exp['detail'][:70]}")
            else:
                log.info(f"  ⚪ No expansion news")

            # ── Hiring: Indeed ───────────────────────────────────────────────
            hire = check_hiring(company, city, state)
            if hire:
                signals.append(hire)
                stats['hiring'] += 1
                log.info(f"  ✅ {hire['label']}: {hire['detail'][:70]}")

            # ── Distress: Google News RSS (tax liens, judgments, closures) ───
            # Check all leads; prioritize stacked/lapsed for distress
            if not signals or stack >= 2 or (isinstance(dtl, int) and dtl < 0):
                distress_sigs = check_distress(company, city, state)
                for ds in distress_sigs:
                    signals.append(ds)
                    if ds['type'] == 'S4_TAX_LIEN':    stats['tax_lien'] += 1
                    elif ds['type'] == 'S5_JUDGMENT':   stats['judgment'] += 1
                    else:                               stats['distress'] += 1

            # ── Contact enrichment: BBB + Yelp ───────────────────────────────
            if enrich_contacts and not lead.get('phone'):
                contact = enrich_contact(company, city, state)
                phone_found = contact.get('phone', '')
                if phone_found:
                    stats['phone'] += 1
                    log.info(f"  📞 Phone found ({contact.get('source','')}): {phone_found}")

            # ── Save ──────────────────────────────────────────────────────────
            if not signals:
                log.info(f"  ⚪ S1 — UCC confirmed only (no additional signals)")

            tier, score = compute_tier(signals)
            save_result(lead['id'], signals, score, tier, phone_found)

        except Exception as e:
            log.error(f"  ❌ Error: {e}")
            stats['errors'] += 1
            save_result(lead['id'], [], 10, 'S1')

        time.sleep(random.uniform(2.5, 4.0))

    # ── Summary ───────────────────────────────────────────────────────────────
    conn = sqlite3.connect(DB_PATH)
    scanned = conn.execute(
        "SELECT COUNT(*) FROM mca_leads WHERE signals_checked_at IS NOT NULL AND signals_checked_at != ''"
    ).fetchone()[0]
    conn.close()

    log.info(f"\n{'='*60}")
    log.info(f"  MCA Signal Engine v2 — Complete")
    log.info(f"  Run processed  : {len(leads)}")
    log.info(f"  Total scanned  : {scanned}")
    log.info(f"  ─── Signals found this run ───")
    log.info(f"  📰 Expansion   : {stats['expansion']}")
    log.info(f"  👷 Hiring      : {stats['hiring']}")
    log.info(f"  ⚠  Tax Lien   : {stats['tax_lien']}")
    log.info(f"  ⚖  Judgment   : {stats['judgment']}")
    log.info(f"  🚨 Distress   : {stats['distress']}")
    log.info(f"  📞 Phones      : {stats['phone']}")
    log.info(f"  ❌ Errors      : {stats['errors']}")
    log.info(f"{'='*60}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Tomcat MCA — Signal Engine v2')
    parser.add_argument('--limit',    type=int, default=200)
    parser.add_argument('--hot-only', action='store_true', help='Only hot leads (≤30d)')
    parser.add_argument('--no-enrich', action='store_true', help='Skip BBB/Yelp contact enrichment')
    args = parser.parse_args()
    run(limit=args.limit, hot_only=args.hot_only, enrich_contacts=not args.no_enrich)

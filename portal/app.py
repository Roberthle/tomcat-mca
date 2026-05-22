"""
Tomcat MCA — Merchant Cash Advance Lead Intelligence Portal
Port: 5051
"""
import os, sqlite3, json, urllib.parse, concurrent.futures, time
import stripe
import feedparser
import requests as _req
from flask import Flask, request, jsonify, send_from_directory
from datetime import datetime
from apollo_enricher import fetch_apollo_contacts, init_contact_cache, get_unlock_stats

stripe.api_key = os.environ.get('STRIPE_SECRET_KEY')

app = Flask(__name__, static_folder='static', static_url_path='')
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'leads', 'tomcat_mca.db')
DEFAULT_BROKER = 'demo_broker'
VALID_CREDS = {'demo': 'tomcatmca2026', 'admin': 'admin'}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS mca_leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name TEXT,
        dba_name TEXT,
        address TEXT,
        city TEXT,
        state TEXT,
        zipcode TEXT,
        source_state TEXT,
        secured_party TEXT,
        collateral_desc TEXT,
        filing_date TEXT,
        lapse_date TEXT,
        days_to_lapse INTEGER,
        file_id TEXT,
        stack_depth INTEGER DEFAULT 1,
        position_number INTEGER DEFAULT 1,
        est_advance_amount REAL,
        est_daily_payment REAL,
        funder_tier TEXT,
        phone TEXT,
        email TEXT,
        contact_name TEXT,
        company_website TEXT,
        industry TEXT,
        est_annual_revenue REAL,
        signals_json TEXT DEFAULT '[]',
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS lead_claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER,
        broker_name TEXT,
        status TEXT DEFAULT 'claimed',
        notes TEXT DEFAULT '',
        claimed_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now')),
        UNIQUE(lead_id, broker_name)
    );
    """)
    conn.commit()
    conn.close()


init_db()


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    u, p = data.get('username', ''), data.get('password', '')
    if VALID_CREDS.get(u) == p:
        return jsonify({"ok": True, "broker": u})
    return jsonify({"ok": False, "error": "Invalid credentials"}), 401


# ── Deal of the Day ───────────────────────────────────────────────────────────

@app.route('/api/deal-of-day')
def deal_of_day():
    conn = get_db()
    row = conn.execute("""
        SELECT m.*, NULL as claim_status, NULL as notes, NULL as claimed_at
        FROM mca_leads m
        LEFT JOIN lead_claims lc ON lc.lead_id = m.id
        WHERE lc.id IS NULL
          AND m.company_name IS NOT NULL
          AND m.days_to_lapse >= -90 AND m.days_to_lapse <= 30
        ORDER BY
            CASE WHEN m.funder_tier='D' THEN 0 WHEN m.funder_tier='C' THEN 1
                 WHEN m.funder_tier='B' THEN 2 ELSE 3 END ASC,
            CASE WHEN m.stack_depth IS NULL THEN 0 ELSE m.stack_depth END DESC,
            m.days_to_lapse ASC
        LIMIT 1
    """).fetchone()
    conn.close()
    if not row:
        return jsonify({'lead': None})
    d = dict(row)
    d['deal_score'] = compute_mca_score(d)
    d['score_breakdown'] = compute_score_breakdown(d)
    d['deal_narrative'] = generate_mca_narrative(d)
    return jsonify({'lead': d})


@app.route('/api/new-leads-since')
def new_leads_since():
    since = request.args.get('since', '')
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) FROM mca_leads WHERE created_at > ?", [since]
    ).fetchone()[0]
    conn.close()
    return jsonify({'count': count})


# ── Company name privacy gate ────────────────────────────────────────────────

def _mask_name(name):
    if not name:
        return 'Confidential Business'
    SUFFIXES = {'LLC','INC','CORP','LTD','LP','DBA','L.L.C.','INC.','CORP.','L.P.','CO.','CO'}
    parts = name.split()
    out = []
    for p in parts:
        if p.upper().rstrip('.') in SUFFIXES or len(p) <= 2:
            out.append(p)
        else:
            out.append(p[0] + '\u2022' * min(len(p) - 1, 8))
    return ' '.join(out)


def _is_purchased(lead_id, session_id=None):
    try:
        conn = get_db()
        if session_id:
            row = conn.execute(
                "SELECT id FROM lead_purchases WHERE lead_id=? AND stripe_session_id=? AND status='completed'",
                [str(lead_id), session_id]
            ).fetchone()
        else:
            row = None
        conn.close()
        return row is not None
    except Exception:
        return False


def _apply_mca_mask(d):
    lead_id = d.get('id')
    if _is_purchased(lead_id):
        d['locked'] = False
        return d
    d['company_name']    = _mask_name(d.get('company_name', ''))
    d['dba_name']        = None
    d['address']         = '\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022'
    d['phone']           = None
    d['email']           = None
    d['contact_name']    = None
    d['company_website'] = None
    d['locked']          = True
    return d


# ── Leads API ─────────────────────────────────────────────────────────────────

@app.route('/api/leads')
def get_leads():
    conn = get_db()
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 25))
    offset = (page - 1) * per_page
    search = request.args.get('q', request.args.get('search', '')).strip()
    urgency = request.args.get('urgency', 'all')
    state = request.args.get('state', 'all')
    status = request.args.get('status', 'all')
    stack_filter = request.args.get('stack', 'all')
    signal_filter = request.args.get('signal', '')
    tier_filter   = request.args.get('tier', 'all')

    where_parts = ['1=1']
    params = []
    claim_join = "LEFT JOIN lead_claims lc ON lc.lead_id = m.id AND lc.broker_name = ?"
    params_with_broker = [DEFAULT_BROKER]

    if search:
        where_parts.append("(m.company_name LIKE ? OR m.city LIKE ? OR m.secured_party LIKE ? OR m.dba_name LIKE ?)")
        sw = f'%{search}%'
        params += [sw, sw, sw, sw]
    if urgency == '7d':
        where_parts.append("(m.days_to_lapse >= 0 AND m.days_to_lapse <= 7)")
    elif urgency == '14d':
        where_parts.append("(m.days_to_lapse >= 0 AND m.days_to_lapse <= 14)")
    elif urgency == 'hot':
        where_parts.append("(m.days_to_lapse <= 30 AND m.days_to_lapse >= -90)")
    elif urgency == 'warm':
        where_parts.append("(m.days_to_lapse > 30 AND m.days_to_lapse <= 180)")
    elif urgency == 'cold':
        where_parts.append("(m.days_to_lapse > 180 OR m.days_to_lapse < -90 OR m.days_to_lapse IS NULL)")
    if state != 'all':
        where_parts.append("m.state = ?")
        params.append(state)
    if status == 'unclaimed':
        where_parts.append("lc.id IS NULL")
    elif status == 'claimed':
        where_parts.append("lc.id IS NOT NULL")
    if stack_filter == 'stacked':
        where_parts.append("m.stack_depth >= 3")
    elif stack_filter == 'single':
        where_parts.append("m.stack_depth = 1")
    if signal_filter == 'expansion':
        where_parts.append("m.signals_json LIKE '%S2_EXPANSION%'")
    elif signal_filter == 'hiring':
        where_parts.append("m.signals_json LIKE '%S3_HIRING%'")
    elif signal_filter == 'stacked':
        where_parts.append("m.company_name IN (SELECT company_name FROM mca_leads GROUP BY company_name HAVING COUNT(DISTINCT secured_party) >= 2)")
    elif signal_filter == 'distress':
        where_parts.append("(m.signals_json LIKE '%S4_TAX_LIEN%' OR m.signals_json LIKE '%S5_JUDGMENT%' OR m.signals_json LIKE '%S6_DISTRESS%')")
    if tier_filter != 'all':
        where_parts.append("m.funder_tier = ?")
        params.append(tier_filter)

    where_sql = ' AND '.join(where_parts)

    total = conn.execute(
        f"SELECT COUNT(*) FROM mca_leads m {claim_join} WHERE {where_sql}",
        params_with_broker + params
    ).fetchone()[0]

    leads_sql = f"""
        SELECT m.*, lc.status as claim_status, lc.notes, lc.claimed_at
        FROM mca_leads m
        {claim_join}
        WHERE {where_sql}
        ORDER BY
            CASE WHEN days_to_lapse IS NULL THEN 9999 ELSE days_to_lapse END ASC,
            stack_depth DESC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(leads_sql, params_with_broker + params + [per_page, offset]).fetchall()
    conn.close()

    leads = []
    for row in rows:
        d = dict(row)
        dtl = d.get('days_to_lapse')
        d['urgency_tier'] = 'hot' if (dtl is not None and dtl <= 30) else \
                            'warm' if (dtl is not None and dtl <= 90) else 'cold'
        d['deal_score'] = compute_mca_score(d)
        d['deal_narrative'] = generate_mca_narrative(d)
        d['score_breakdown'] = compute_score_breakdown(d)
        px = estimate_mca_paydex(d)
        d['est_paydex'] = px['score']
        d['est_paydex_label'] = px['label']
        d['est_paydex_rationale'] = px['rationale']
        rv = estimate_mca_revenue(d)
        d['est_revenue_range'] = rv['range']
        d['est_revenue_label'] = rv['label']
        d['est_revenue_color'] = rv['color']
        d['est_revenue_rationale'] = rv['rationale']
        tier_key, tier_info = get_mca_lead_tier(d)
        d['price_tier']    = tier_key
        d['price_display'] = tier_info['label'] + ' · ' + f"${tier_info['price']//100}"
        d['price_cents']   = tier_info['price']
        _apply_mca_mask(d)
        leads.append(d)

    return jsonify({
        "leads": leads, "total": total, "page": page,
        "per_page": per_page, "pages": (total + per_page - 1) // per_page
    })


@app.route('/api/leads/<int:lead_id>/unlock')
def mca_unlock_lead(lead_id):
    """Return full unmasked lead — only after confirmed Stripe purchase."""
    session_id = request.args.get('session_id')
    if not session_id or not _is_purchased(lead_id, session_id):
        return jsonify({'error': 'Purchase required', 'locked': True}), 402
    conn = get_db()
    row = conn.execute('SELECT * FROM mca_leads WHERE id = ?', [lead_id]).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404
    d = dict(row)
    d['locked'] = False
    return jsonify(d)


# ── CSV Export ───────────────────────────────────────────────────────────────

@app.route('/api/export')
def export_leads():
    import csv, io
    conn = get_db()
    state       = request.args.get('state', 'all')
    urgency     = request.args.get('urgency', 'all')
    tier_filter = request.args.get('tier', 'all')
    signal_filter = request.args.get('signal', '')
    search      = request.args.get('q', '').strip()

    where_parts = ['1=1']
    params = []
    if search:
        sw = f'%{search}%'
        where_parts.append("(company_name LIKE ? OR city LIKE ? OR secured_party LIKE ?)")
        params += [sw, sw, sw]
    if urgency == '7d':
        where_parts.append("(days_to_lapse >= 0 AND days_to_lapse <= 7)")
    elif urgency == '14d':
        where_parts.append("(days_to_lapse >= 0 AND days_to_lapse <= 14)")
    elif urgency == 'hot':
        where_parts.append("(days_to_lapse <= 30 AND days_to_lapse >= -90)")
    elif urgency == 'warm':
        where_parts.append("(days_to_lapse > 30 AND days_to_lapse <= 180)")
    elif urgency == 'cold':
        where_parts.append("(days_to_lapse > 180 OR days_to_lapse < -90 OR days_to_lapse IS NULL)")
    if state != 'all':
        where_parts.append("state = ?")
        params.append(state)
    if tier_filter != 'all':
        where_parts.append("funder_tier = ?")
        params.append(tier_filter)
    if signal_filter == 'expansion':
        where_parts.append("signals_json LIKE '%S2_EXPANSION%'")
    elif signal_filter == 'hiring':
        where_parts.append("signals_json LIKE '%S3_HIRING%'")
    elif signal_filter == 'distress':
        where_parts.append("(signals_json LIKE '%S4_TAX_LIEN%' OR signals_json LIKE '%S5_JUDGMENT%')")

    rows = conn.execute(f"""
        SELECT company_name, dba_name, address, city, state, zipcode,
               secured_party, funder_tier, est_advance_amount, stack_depth,
               days_to_lapse, lapse_date, filing_date, phone,
               signal_tier, signals_json, file_id, source_state
        FROM mca_leads WHERE {' AND '.join(where_parts)}
        ORDER BY CASE WHEN days_to_lapse IS NULL THEN 9999 ELSE days_to_lapse END ASC
        LIMIT 5000
    """, params).fetchall()
    conn.close()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['Company','DBA','Address','City','State','ZIP',
                'Lender','Tier','Est Advance','Stack Depth',
                'Days To Lapse','Lapse Date','Filing Date',
                'Phone','Signal Tier','File ID','Source State'])
    for r in rows:
        w.writerow([
            r['company_name'] or '', r['dba_name'] or '',
            r['address'] or '', r['city'] or '', r['state'] or '', r['zipcode'] or '',
            r['secured_party'] or '', r['funder_tier'] or '',
            r['est_advance_amount'] or '', r['stack_depth'] or 1,
            r['days_to_lapse'] if r['days_to_lapse'] is not None else '',
            r['lapse_date'] or '', r['filing_date'] or '',
            r['phone'] or '', r['signal_tier'] or 'S1',
            r['file_id'] or '', r['source_state'] or ''
        ])

    from flask import Response
    fname = f"mca_leads_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    return Response(
        out.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={fname}'}
    )


# ── Lead Status (Mini-CRM) ───────────────────────────────────────────────────

@app.route('/api/lead/<int:lead_id>/status', methods=['PATCH'])
def update_lead_status(lead_id):
    data   = request.get_json() or {}
    status = data.get('status', 'claimed')
    notes  = data.get('notes', '')
    broker = data.get('broker', DEFAULT_BROKER)
    VALID_STATUSES = {'claimed','called','voicemail','interested','not_interested','funded','follow_up'}
    if status not in VALID_STATUSES:
        return jsonify({'error': 'Invalid status'}), 400
    conn = get_db()
    conn.execute("""
        INSERT INTO lead_claims (lead_id, broker_name, status, notes, claimed_at, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(lead_id, broker_name) DO UPDATE SET
            status=excluded.status, notes=excluded.notes, updated_at=datetime('now')
    """, [lead_id, broker, status, notes])
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'status': status})


# ── MCA Deal Score Engine ────────────────────────────────────────────────────

MCA_FUNDER_VULN = {
    'YELLOWSTONE': 10, 'LAST CHANCE': 10, 'LCF': 10, 'GREEN BOX': 9, 'GREENBOX': 9,
    'RAPID ADVANCE': 9, 'NATIONAL FUNDING': 8, 'BUSINESS CAPITAL USA': 8,
    'FUNDKITE': 8, 'FORA FINANCIAL': 8, 'PEARL CAPITAL': 7,
    'ONDECK': 7, 'ON DECK': 7, 'FOX CAPITAL': 7,
    'KABBAGE': 6, 'BLUEVINE': 6, 'CAN CAPITAL': 6,
    'CREDIBLY': 6, 'HEADWAY': 6, 'LIBERTAS': 6,
    'PAYPAL': 5, 'SQUARE': 5, 'STRIPE CAPITAL': 5,
    'AMAZON LENDING': 5, 'SHOPIFY CAPITAL': 5,
    'FUNDBOX': 4, 'CLEARCO': 4, 'PIPE': 4,
    'CHASE': 3, 'WELLS FARGO': 3, 'BANK OF AMERICA': 3,
    # ── New C/D Tier Lenders (from direct lender partner) ──
    'EVEREST': 9, 'EBF': 9, 'FORWARD': 7, 'MEGED': 9, 'APPFUNDINGBETA': 8,
    'FDM': 8, 'LENDINI': 8, 'ESSENTIAL': 7, 'CFG': 7, 'LIKETY': 10,
    'DLP': 8, 'BARCLAY': 8, 'LAZARUS': 10, 'EXPANSION CAPITAL': 7,
    'VADER': 10, 'SEAMLESS': 7, 'ZLUR': 9, 'BITTY': 10,
    'MAZAL': 9, 'STONE FUNDING': 8, 'BLUE ROCK': 8,
}

def get_mca_funder_vuln(name):
    lu = (name or '').upper()
    for key, score in MCA_FUNDER_VULN.items():
        if key in lu:
            return score
    if any(x in lu for x in ['FUNDING','ADVANCE','MERCHANT']): return 7
    if any(x in lu for x in ['CAPITAL','FINANCE']): return 6
    if any(x in lu for x in ['BANK','CREDIT UNION']): return 3
    return 5

def _parse_sig_types(lead):
    """Robustly extract signal type strings from signals_json.
    Handles both dict format [{"type":"S1_UCC"}] and plain string format ["S1_UCC"]."""
    raw = lead.get('signals_json', '[]') or '[]'
    try:
        parsed = json.loads(raw)
        types = set()
        for s in parsed:
            if isinstance(s, dict):
                types.add(s.get('type', ''))
            elif isinstance(s, str):
                types.add(s)
        return types
    except:
        return set()


def compute_mca_score(lead):
    score = 0
    dtl = lead.get('days_to_lapse')
    # Urgency (55 pts max)
    if dtl is not None:
        if dtl < 0:      score += 50
        elif dtl == 0:    score += 55
        elif dtl <= 3:    score += 52
        elif dtl <= 7:    score += 48
        elif dtl <= 14:   score += 42
        elif dtl <= 30:   score += 32
        elif dtl <= 60:   score += 18
        elif dtl <= 90:   score += 9
    # Funder Vulnerability (25 pts max)
    vuln = get_mca_funder_vuln(lead.get('secured_party', ''))
    score += int(vuln * 2.5)
    # Stack Depth bonus (within signals budget)
    stack = lead.get('stack_depth', 1) or 1
    if stack >= 4: score += 8
    elif stack >= 3: score += 5
    elif stack >= 2: score += 2
    # Distress signals
    sig_types = _parse_sig_types(lead)
    if 'S4_TAX_LIEN' in sig_types: score += 8
    if 'S5_JUDGMENT' in sig_types: score += 6
    if 'S6_DISTRESS' in sig_types: score += 5
    if 'S2_EXPANSION' in sig_types: score += 4
    return min(100, max(0, score))

def compute_score_breakdown(lead):
    """Returns list of (label, pts) tuples explaining the deal score."""
    parts = []
    dtl = lead.get('days_to_lapse')
    if dtl is not None:
        if dtl < 0:     pts, lbl = 50, f'Lapsed {abs(dtl)}d ago'
        elif dtl == 0:  pts, lbl = 55, 'Matures TODAY'
        elif dtl <= 3:  pts, lbl = 52, f'Matures in {dtl}d'
        elif dtl <= 7:  pts, lbl = 48, f'Matures in {dtl}d'
        elif dtl <= 14: pts, lbl = 42, f'Matures in {dtl}d'
        elif dtl <= 30: pts, lbl = 32, f'Matures in {dtl}d'
        elif dtl <= 60: pts, lbl = 18, f'Matures in {dtl}d'
        else:           pts, lbl = 9,  f'Matures in {dtl}d'
        parts.append({'label': lbl, 'pts': pts, 'cat': 'urgency'})
    vuln = get_mca_funder_vuln(lead.get('secured_party', ''))
    vpts = int(vuln * 2.5)
    tier = lead.get('funder_tier', '?')
    parts.append({'label': f"{(lead.get('secured_party') or 'Lender')[:30]} (Tier {tier})", 'pts': vpts, 'cat': 'lender'})
    stack = lead.get('stack_depth', 1) or 1
    if stack >= 2:
        spts = 8 if stack >= 4 else 5 if stack >= 3 else 2
        parts.append({'label': f'{stack}-position stack', 'pts': spts, 'cat': 'stack'})
    signals = []
    try: signals = json.loads(lead.get('signals_json', '[]') or '[]')
    except: pass
    sig_map = {'S4_TAX_LIEN': ('Tax Lien', 8), 'S5_JUDGMENT': ('Court Judgment', 6),
               'S6_DISTRESS': ('Distress Signal', 5), 'S2_EXPANSION': ('Expansion News', 4),
               'S3_HIRING': ('Hiring Signal', 3)}
    sig_types = _parse_sig_types(lead)
    for t, (lbl, pts) in sig_map.items():
        if t in sig_types:
            parts.append({'label': lbl, 'pts': pts, 'cat': 'signal'})
    return parts


def generate_mca_narrative(lead):
    funder = lead.get('secured_party', 'Unknown')
    dtl = lead.get('days_to_lapse')
    stack = lead.get('stack_depth', 1) or 1
    amt = lead.get('est_advance_amount', 0) or 0
    tier = lead.get('funder_tier', '?')
    lu = funder.upper()
    if dtl is not None and dtl < 0:
        tp = f"lien lapsed {abs(dtl)}d ago"
    elif dtl is not None and dtl == 0:
        tp = "advance matures today"
    elif dtl is not None and dtl <= 7:
        tp = f"advance matures in {dtl}d"
    elif dtl is not None and dtl <= 30:
        tp = f"advance matures in {dtl}d"
    else:
        tp = "advance approaching maturity"
    angles = {
        'YELLOWSTONE': "Yellowstone is a D-tier stacker — factor rates 1.40+. Any consolidation at 1.25x saves them thousands.",
        'LAST CHANCE': "Last Chance Funding is bottom-tier. Borrower is desperate. Offer consolidation at lower factor rate.",
        'GREENBOX': "Greenbox stacks aggressively — borrower likely has 3+ positions. Consolidation play saves daily payment.",
        'GREEN BOX': "Green Box is a serial stacker. Borrower has multiple positions. Consolidation cuts their daily ACH.",
        'ONDECK': "OnDeck tightened post-Enova acquisition. Renewals are getting declined — position as the alternative.",
        'ON DECK': "OnDeck tightened post-Enova acquisition. Renewals are getting declined — position as the alternative.",
        'RAPID ADVANCE': "Rapid Advance uses high factor rates (1.35-1.49). Beat their rate and show monthly savings.",
        'FOX CAPITAL': "Fox Capital is a mid-tier stacker. Borrower may not realize total cost across positions.",
        'FOX': "Fox Capital is a mid-tier stacker. Borrower may not realize total cost across positions.",
        'NATIONAL FUNDING': "National Funding has slow underwriting (5-7 days). Position with same-day funding.",
        'FUNDKITE': "FundKite uses revenue-based repayment. Fixed daily ACH offer provides payment predictability.",
        'FORA FINANCIAL': "Fora Financial targets 6-month terms. Offer longer terms to reduce daily payment.",
        'KABBAGE': "Kabbage (now AmEx) tightened criteria. Many renewals being declined — capture the fallout.",
        'PAYPAL': "PayPal Working Capital auto-deducts from processing. Offer fixed payments not tied to sales.",
        'SQUARE': "Square Capital deducts from card processing daily. Fixed ACH provides predictable cash flow.",
        'BLUEVINE': "BlueVine shifted to banking — MCA renewals are being deprioritized. Easy displacement.",
        # ── New C/D Tier lender narratives ──
        'EVEREST': "Everest (EBF) is a known C-tier stacker with aggressive daily ACH. Factor rates 1.35-1.45. Beat their rate by 10+ pts and show total savings over term.",
        'EBF': "EBF (Everest) uses aggressive factor rates. Borrower is likely stacked — consolidation pitch with lower daily payment wins.",
        'FORWARD': "Forward Financing has moderate terms but their renewal underwriting is getting tighter. Position with faster approval and better rate.",
        'MEGED': "Meged Financial runs high factor rates (1.38-1.48) on short terms. Borrower is paying a premium — consolidation saves immediately.",
        'APPFUNDINGBETA': "AppFundingBeta is a newer C-tier funder with aggressive stacking. Borrower may not know their total cost. Break it down and offer savings.",
        'FDM': "FDM/Lendini uses short-term high-factor advances. Borrower's daily payment is crushing cash flow — consolidation is the lifeline.",
        'LENDINI': "Lendini (FDM) stacks aggressively with 90-120 day terms at 1.40+. Any longer-term offer at lower factor rate wins.",
        'ESSENTIAL': "Essential Capital targets smaller deals ($10-50K). Borrower is likely a smaller business — position as their growth partner.",
        'CFG': "CFG Merchant Solutions runs standard MCA terms. Position with transparency — show them the total cost comparison.",
        'LIKETY': "Likety Split is a bottom-tier D-funder. Factor rates 1.45+. Borrower is in distress — MCA consolidation is their only option.",
        'DLP': "DLP Capital runs mid-tier advances. Borrower has room for consolidation — show daily payment reduction.",
        'BARCLAY': "Barclay's Advance uses aggressive daily ACH with short terms. Borrower's cash flow is constrained — offer relief.",
        'LAZARUS': "Lazarus Capital is a last-resort D-tier funder. Borrower has been declined everywhere — position as the lifeline with structured repayment.",
        'VADER': "Vader Financial is bottom-tier with extreme factor rates (1.45-1.55). Any consolidation offer saves the borrower significantly.",
        'SEAMLESS': "Seamless Capital runs standard B-tier terms. Position with faster funding and better rate to displace.",
        'ZLUR': "Zlur Funding is a C/D-tier stacker. Borrower likely doesn't know their total cost across positions. Break it down.",
        'BITTY': "Bitty Advance is a micro-advance D-tier funder. Small advances at high factor rates. Consolidation into one position saves them.",
        'MAZAL': "Mazal Funders runs aggressive C-tier terms. Short duration, high factor. Consolidation at longer terms saves daily payment.",
        'STONE FUNDING': "Stone Funding Group uses standard C-tier stacking. Show borrower the total cost across all positions and offer savings.",
        'BLUE ROCK': "Blue Rock Capital runs mid-tier advances. Borrower has room for consolidation — position with competitive rate.",
    }
    angle = "Position with competitive factor rate and faster funding timeline."
    for key, val in angles.items():
        if key in lu:
            angle = val
            break
    # Stack context
    stack_ctx = ""
    if stack >= 3:
        stack_ctx = f" Stacked {stack} deep — consolidation saves significant daily payment."
    elif stack >= 2:
        stack_ctx = f" Position {stack} — consolidation opportunity."
    # Distress context
    sigs = []
    try: sigs = json.loads(lead.get('signals_json', '[]') or '[]')
    except: pass
    sig_types_narr = _parse_sig_types(lead)
    for s in sigs:
        t = s.get('type', '') if isinstance(s, dict) else s
        if t == 'S4_TAX_LIEN':
            stack_ctx += f" Active tax lien — banks will auto-decline. MCA is their only option."
            break
        elif t == 'S5_JUDGMENT':
            stack_ctx += f" Court judgment on file — traditional lending is closed."
            break
    amt_str = f"${amt:,.0f} " if amt else ""
    return f"Their {amt_str}{funder} (Tier {tier}) {tp}. {angle}{stack_ctx}"


def estimate_mca_paydex(lead: dict) -> dict:
    """
    MCA-context Paydex proxy.
    MCA borrowers are by definition bank-declined, so base starts lower.
    Key signals: funder tier, stack depth, distress signals, tenure.
    """
    score = 42  # Base: MCA borrower is already bank-declined territory
    rationale = []

    funder = (lead.get('secured_party') or '').upper()
    dtl    = lead.get('days_to_lapse')
    stack  = lead.get('stack_depth', 1) or 1
    sig_types = _parse_sig_types(lead)
    tier   = (lead.get('funder_tier') or '').upper()

    # ── Funder tier (A = least distressed, D = most distressed) ──
    A_TIER = ['PAYPAL','SQUARE','STRIPE CAPITAL','AMAZON LENDING','SHOPIFY CAPITAL',
              'FUNDBOX','CLEARCO','PIPE','CHASE','WELLS FARGO','BANK OF AMERICA']
    B_TIER = ['BLUEVINE','KABBAGE','CAN CAPITAL','ONDECK','ON DECK',
              'CREDIBLY','HEADWAY','LIBERTAS','FORWARD','ESSENTIAL','CFG']
    C_TIER = ['NATIONAL FUNDING','FUNDKITE','FORA FINANCIAL','PEARL CAPITAL',
              'FOX CAPITAL','RAPID ADVANCE','STONE FUNDING','BLUE ROCK',
              'SEAMLESS','MAZAL','DLP','BARCLAY']
    D_TIER = ['YELLOWSTONE','LAST CHANCE','LCF','GREEN BOX','GREENBOX','LIKETY',
              'VADER','BITTY','LAZARUS','ZLUR','MEGED','EBF','EVEREST','APPFUNDING']

    if any(f in funder for f in A_TIER):
        score += 15
        rationale.append('A-tier funder (+15): fintech/bank platform — higher revenue baseline')
    elif any(f in funder for f in B_TIER):
        score += 6
        rationale.append('B-tier funder (+6): established MCA lender')
    elif any(f in funder for f in C_TIER):
        score -= 5
        rationale.append('C-tier funder (-5): aggressive stacker')
    elif any(f in funder for f in D_TIER):
        score -= 15
        rationale.append('D-tier funder (-15): last-resort / distress lender')

    # Override with explicit tier if available
    if tier == 'A':
        score = max(score, 55); rationale.append('Tier A confirmed (+)')
    elif tier == 'D':
        score = min(score, 35); rationale.append('Tier D confirmed (-)')

    # ── Stack depth penalty (each position = more debt = worse creditworthiness) ──
    if stack >= 5:
        score -= 20
        rationale.append(f'Stack depth {stack} (-20): severely over-leveraged')
    elif stack >= 4:
        score -= 14
        rationale.append(f'Stack depth {stack} (-14): heavily stacked')
    elif stack >= 3:
        score -= 8
        rationale.append(f'Stack depth {stack} (-8): stacked borrower')
    elif stack >= 2:
        score -= 4
        rationale.append(f'Stack depth {stack} (-4): 2nd position')

    # ── Distress signals ──────────────────────────────────────────────────
    if 'S4_TAX_LIEN' in sig_types:
        score -= 12
        rationale.append('Active tax lien (-12): IRS/state priority claim')
    if 'S5_JUDGMENT' in sig_types:
        score -= 10
        rationale.append('Court judgment (-10): creditor legal action')
    if 'S6_DISTRESS' in sig_types:
        score -= 6
        rationale.append('Distress signal (-6)')

    # ── Positive signals ──────────────────────────────────────────────────
    if 'S2_EXPANSION' in sig_types:
        score += 6
        rationale.append('Expansion signal (+6): growing revenue')
    if 'S3_HIRING' in sig_types:
        score += 4
        rationale.append('Hiring signal (+4): operational growth')

    # ── Filing tenure ─────────────────────────────────────────────────────
    filing_date = lead.get('filing_date') or ''
    try:
        from datetime import date
        fd = date.fromisoformat(filing_date[:10])
        tenure_years = (date.today() - fd).days / 365.25
        tenure_bump = min(8, int(tenure_years * 1.5))
        if tenure_bump > 0:
            score += tenure_bump
            rationale.append(f'Filing tenure {tenure_years:.1f}yr (+{tenure_bump})')
    except:
        pass

    # ── Lapsed penalty ────────────────────────────────────────────────────
    if dtl is not None and dtl < -60:
        score -= 6
        rationale.append(f'Lapsed {abs(dtl)}d (-6)')

    score = max(10, min(88, score))

    if score >= 70:   label = 'Low Risk'
    elif score >= 50: label = 'Moderate'
    elif score >= 35: label = 'Elevated Risk'
    else:             label = 'High Risk'

    return {'score': score, 'label': label, 'rationale': rationale}


def estimate_mca_revenue(lead: dict) -> dict:
    """
    Revenue estimate for MCA borrowers.
    MCA advance amounts are directly correlated to monthly revenue
    (funders typically advance 50-150% of monthly revenue).
    Industry and stack depth inform the multiplier.
    """
    amt   = lead.get('est_advance_amount') or 0
    stack = lead.get('stack_depth', 1) or 1
    col   = (lead.get('collateral_desc') or '').lower()
    industry = (lead.get('industry') or '').lower()
    sig_types_rev = _parse_sig_types(lead)
    rationale = []

    if amt and amt > 0:
        # MCA advance = ~70-100% of 1 month revenue on average
        # Stack multiplier: deeper stack = they got approved multiple times = higher revenue
        stack_mult = 1 + (stack - 1) * 0.3  # 1x for pos1, 1.3x for pos2, 1.6x for pos3, etc.
        monthly_rev_est = amt / 0.85 * stack_mult  # 85% advance-to-revenue ratio
        annual_rev_est = monthly_rev_est * 12
        rationale.append(f'Based on ${amt:,.0f} advance amount')
        if stack >= 2:
            rationale.append(f'Stack {stack}x multiplier (+{int((stack_mult-1)*100)}%)')
    else:
        # Fallback: collateral/industry-based tiers
        HEAVY   = ['restaurant','construction','trucking','logistics','fleet','hospitality','hotel']
        MID     = ['retail','medical','dental','salon','spa','auto','mechanic','plumbing','hvac']
        LIGHT   = ['service','consulting','staffing','cleaning','landscaping','delivery']
        if any(k in col + industry for k in HEAVY):   annual_rev_est = 600000
        elif any(k in col + industry for k in MID):   annual_rev_est = 350000
        elif any(k in col + industry for k in LIGHT): annual_rev_est = 180000
        else:                                          annual_rev_est = 300000
        rationale.append('Industry/collateral-based estimate')

    # Signal bumps
    if 'S2_EXPANSION' in sig_types_rev:
        annual_rev_est *= 1.25
        rationale.append('Expansion signal (+25%)')
    if 'S3_HIRING' in sig_types_rev:
        annual_rev_est *= 1.15
        rationale.append('Hiring signal (+15%)')

    # Format the range (±40% band around estimate)
    lo = annual_rev_est * 0.6
    hi = annual_rev_est * 1.4

    def fmt(n):
        if n >= 1_000_000: return f'${n/1_000_000:.1f}M'
        return f'${n/1000:.0f}K'

    rev_range = f'{fmt(lo)}\u2013{fmt(hi)}'

    # Tier label
    if annual_rev_est >= 5_000_000:   label, color = 'Enterprise',  '#a78bfa'
    elif annual_rev_est >= 1_000_000: label, color = 'Mid-Market',  '#22d3ee'
    elif annual_rev_est >= 500_000:   label, color = 'Lower-Mid',   '#fb923c'
    elif annual_rev_est >= 200_000:   label, color = 'Small Biz',   '#fbbf24'
    else:                             label, color = 'Micro',        '#94a3b8'

    return {
        'range': rev_range, 'label': label, 'color': color,
        'est': annual_rev_est, 'rationale': rationale
    }


@app.route('/api/heatmap')
def heatmap():
    state         = request.args.get('state', 'all')
    urgency       = request.args.get('urgency', 'all')
    tier_filter   = request.args.get('tier', 'all')
    status_filter = request.args.get('status', 'all')
    signal_filter = request.args.get('signal', 'all')
    search        = request.args.get('q', '').strip()

    where_parts = ["m.city IS NOT NULL AND m.city != ''"]
    params = []
    claim_join = ""

    if search:
        where_parts.append("(m.company_name LIKE ? OR m.city LIKE ? OR m.secured_party LIKE ? OR m.dba_name LIKE ?)")
        sw = f'%{search}%'
        params += [sw, sw, sw, sw]
    if urgency == '7d':
        where_parts.append("(m.days_to_lapse >= 0 AND m.days_to_lapse <= 7)")
    elif urgency == '14d':
        where_parts.append("(m.days_to_lapse >= 0 AND m.days_to_lapse <= 14)")
    elif urgency == 'hot':
        where_parts.append("(m.days_to_lapse <= 30 AND m.days_to_lapse >= -90)")
    elif urgency == 'warm':
        where_parts.append("(m.days_to_lapse > 30 AND m.days_to_lapse <= 180)")
    elif urgency == 'cold':
        where_parts.append("(m.days_to_lapse > 180 OR m.days_to_lapse < -90 OR m.days_to_lapse IS NULL)")
    if state != 'all':
        where_parts.append("m.source_state = ?")
        params.append(state)
    if tier_filter != 'all':
        where_parts.append("m.funder_tier = ?")
        params.append(tier_filter)
    if signal_filter == 'expansion':
        where_parts.append("m.signals_json LIKE '%S2_EXPANSION%'")
    elif signal_filter == 'hiring':
        where_parts.append("m.signals_json LIKE '%S3_HIRING%'")
    elif signal_filter == 'distress':
        where_parts.append("(m.signals_json LIKE '%S4_TAX_LIEN%' OR m.signals_json LIKE '%S5_JUDGMENT%' OR m.signals_json LIKE '%S6_DISTRESS%')")

    if status_filter != 'all':
        claim_join = "LEFT JOIN lead_claims lc ON lc.lead_id = m.id AND lc.broker_name = ?"
        params.insert(0, DEFAULT_BROKER)
        if status_filter == 'unclaimed':
            where_parts.append("lc.id IS NULL")
        elif status_filter == 'claimed':
            where_parts.append("lc.id IS NOT NULL")

    where_sql = ' AND '.join(where_parts)
    base_from = f"FROM mca_leads m {claim_join} WHERE {where_sql}"

    conn = get_db()
    rows = conn.execute(f"""
        SELECT m.city, m.source_state,
               COUNT(*) as total,
               SUM(CASE WHEN m.days_to_lapse >= -90 AND m.days_to_lapse <= 30 THEN 1 ELSE 0 END) as hot,
               SUM(CASE WHEN m.days_to_lapse < 0 AND m.days_to_lapse >= -90 THEN 1 ELSE 0 END) as lapsed
        {base_from}
        GROUP BY m.city, m.source_state ORDER BY total DESC LIMIT 40
    """, params).fetchall()
    conn.close()
    return jsonify({"cities": [
        {"city": r[0], "state": r[1], "total": r[2], "hot": r[3],
         "lapsed": r[4], "pipeline": (r[2] or 0) * 40000}
        for r in rows
    ]})


@app.route('/api/leads/<lead_id>')
def get_lead(lead_id):
    conn = get_db()
    row = conn.execute("""
        SELECT m.*, lc.status as claim_status, lc.notes, lc.claimed_at
        FROM mca_leads m
        LEFT JOIN lead_claims lc ON lc.lead_id = m.id AND lc.broker_name = ?
        WHERE m.id = ?
    """, [DEFAULT_BROKER, lead_id]).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(row))


@app.route('/api/leads/<lead_id>/claim', methods=['POST'])
def claim_lead(lead_id):
    data = request.get_json() or {}
    status = data.get('status', 'claimed')
    notes = data.get('notes', '')
    conn = get_db()
    conn.execute("""
        INSERT INTO lead_claims (lead_id, broker_name, status, notes)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(lead_id, broker_name) DO UPDATE SET
            status = excluded.status, notes = excluded.notes,
            updated_at = datetime('now')
    """, [lead_id, DEFAULT_BROKER, status, notes])
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "status": status})


@app.route('/api/leads/<lead_id>/unclaim', methods=['POST'])
def unclaim_lead(lead_id):
    conn = get_db()
    conn.execute("DELETE FROM lead_claims WHERE lead_id = ? AND broker_name = ?",
                 [lead_id, DEFAULT_BROKER])
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ── Expiry Timeline API ──────────────────────────────────────────────────────

@app.route('/api/expiry-timeline')
def expiry_timeline():
    state         = request.args.get('state', 'all')
    urgency       = request.args.get('urgency', 'all')
    tier_filter   = request.args.get('tier', 'all')
    status_filter = request.args.get('status', 'all')
    signal_filter = request.args.get('signal', 'all')
    search        = request.args.get('q', '').strip()

    where_parts = ["m.days_to_lapse >= 0 AND m.days_to_lapse <= 90"]
    params = []
    claim_join = ""

    if search:
        where_parts.append("(m.company_name LIKE ? OR m.city LIKE ? OR m.secured_party LIKE ? OR m.dba_name LIKE ?)")
        sw = f'%{search}%'
        params += [sw, sw, sw, sw]
    if urgency == '7d':
        where_parts.append("(m.days_to_lapse >= 0 AND m.days_to_lapse <= 7)")
    elif urgency == '14d':
        where_parts.append("(m.days_to_lapse >= 0 AND m.days_to_lapse <= 14)")
    elif urgency == 'hot':
        where_parts.append("(m.days_to_lapse <= 30 AND m.days_to_lapse >= -90)")
    elif urgency == 'warm':
        where_parts.append("(m.days_to_lapse > 30 AND m.days_to_lapse <= 180)")
    elif urgency == 'cold':
        where_parts.append("(m.days_to_lapse > 180 OR m.days_to_lapse < -90 OR m.days_to_lapse IS NULL)")
    if state != 'all':
        where_parts.append("m.source_state = ?")
        params.append(state)
    if tier_filter != 'all':
        where_parts.append("m.funder_tier = ?")
        params.append(tier_filter)
    if signal_filter == 'expansion':
        where_parts.append("m.signals_json LIKE '%S2_EXPANSION%'")
    elif signal_filter == 'hiring':
        where_parts.append("m.signals_json LIKE '%S3_HIRING%'")
    elif signal_filter == 'distress':
        where_parts.append("(m.signals_json LIKE '%S4_TAX_LIEN%' OR m.signals_json LIKE '%S5_JUDGMENT%' OR m.signals_json LIKE '%S6_DISTRESS%')")

    if status_filter != 'all':
        claim_join = "LEFT JOIN lead_claims lc ON lc.lead_id = m.id AND lc.broker_name = ?"
        params.insert(0, DEFAULT_BROKER)
        if status_filter == 'unclaimed':
            where_parts.append("lc.id IS NULL")
        elif status_filter == 'claimed':
            where_parts.append("lc.id IS NOT NULL")

    where_sql = ' AND '.join(where_parts)
    base_from = f"FROM mca_leads m {claim_join} WHERE {where_sql}"

    conn = get_db()
    rows = conn.execute(f"""
        SELECT CAST((m.days_to_lapse / 7) AS INTEGER) as week_bucket,
               COUNT(*) as count,
               SUM(CASE WHEN m.stack_depth >= 3 THEN 1 ELSE 0 END) as stacked
        {base_from}
        GROUP BY week_bucket ORDER BY week_bucket ASC
    """, params).fetchall()
    conn.close()
    weeks = []
    for r in rows:
        w = r[0]
        weeks.append({"week": w, "label": f"Wk {w+1}", "count": r[1], "enriched": r[2]})
    return jsonify({"weeks": weeks})


# ── Stats API ─────────────────────────────────────────────────────────────────

# ── Market Intelligence Engine ───────────────────────────────────────────────

_INDUSTRY_KW = [
    ('Food & Beverage',      ['restaurant','food','bakery','cafe','grill','catering','pizza','diner','brewery','bistro','kitchen','winery','distillery','tavern']),
    ('Construction',         ['construction','contractor','builder','roofing','concrete','excavat','paving','masonry','plumbing','electric','hvac','renovation']),
    ('Automotive',           ['auto ','motor','car ','tire','collision','mechanic','towing','dealership','autobody','carwash']),
    ('Healthcare',           ['medical','dental','health','clinic','pharmacy','therapy','chiro','urgent care','rehab','physician','veterinary','optometry']),
    ('Retail',               ['retail','store','shop','boutique','market','outlet','supply','wholesale','grocery','hardware']),
    ('Logistics',            ['truck','transport','logistics','freight','trucking','shipping','delivery','courier','moving','warehouse','carrier','fleet']),
    ('Beauty',               ['salon','beauty','spa','nail','barber','hair','lash','wax','cosmetic']),
    ('Real Estate',          ['realty','real estate','propert','housing','apartment','landlord']),
    ('Technology',           ['tech','software','digital',' it ','cyber','data','cloud','network']),
    ('Landscaping',          ['landscap','lawn','garden','tree','irrigation']),
    ('Manufacturing',        ['manufactur','fabricat','machine','industrial','production']),
    ('Legal',                ['law ','legal','attorney','counsel',' llp']),
    ('Hospitality',          ['golf','country club','resort','hotel','motel','inn','tourism','lodge','marina','yacht','hospitality']),
    ('Sports & Recreation',  ['sport','fitness','gym','recreation','athletic','arena','pool','bowling','tennis','yoga','dance','studio']),
]

_INDUSTRY_NEWS_QUERY = {
    'Food & Beverage':     '"restaurant industry" OR "food service" 2026 business outlook trends closures openings',
    'Construction':        'construction industry 2026 contractor outlook permits housing starts material costs',
    'Automotive':          'auto repair shop 2026 independent mechanic service demand vehicle maintenance trends',
    'Healthcare':          'medical practice healthcare 2026 patient demand small business outlook',
    'Retail':              'retail sales 2026 consumer spending small business brick mortar trends',
    'Logistics':           'trucking freight industry 2026 owner operator carrier rates fuel diesel outlook',
    'Beauty':              'salon beauty industry 2026 consumer spending trends small business',
    'Real Estate':         'commercial real estate 2026 property market rents vacancy landlord trends',
    'Technology':          'technology small business 2026 IT spending software market SaaS trends',
    'Landscaping':         'landscaping lawn care industry 2026 seasonal demand labor costs',
    'Manufacturing':       'manufacturing sector 2026 production activity supply chain reshoring',
    'Legal':               'law firm legal services 2026 small firm market trends',
    'Hospitality':         'golf resort hotel hospitality 2026 tourism travel demand industry trends',
    'Sports & Recreation': 'fitness gym recreation industry 2026 consumer health spending trends',
}

_INDUSTRY_SUBS = {
    'Food & Beverage':     'restaurantowners',
    'Construction':        'Contractors',
    'Automotive':          'MechanicAdvice',
    'Healthcare':          'HealthcareWorkers',
    'Retail':              'smallbusiness',
    'Logistics':           'Truckers',
    'Beauty':              'smallbusiness',
    'Technology':          'startups',
    'Landscaping':         'lawncare',
    'Manufacturing':       'manufacturing',
    'Hospitality':         'smallbusiness',
    'Sports & Recreation': 'smallbusiness',
}

_NEG_WORDS = ['closure','bankrupt','default','loss','decline','struggling','layoff','fraud','lien','judgment','delinquent','distress','hardship','shut down']
_POS_WORDS = ['growth','expansion','hiring','record','surge','demand','profit','thriving','contract won','revenue up','funding round','acquisition']


def _infer_industry(company_name, collateral=''):
    """Two-pass classification: company name first, collateral only as fallback.
    Prevents equipment type in collateral from overriding the company's actual sector."""
    import re
    # Pass 1: company name alone
    co_txt = company_name.lower()
    for label, kws in _INDUSTRY_KW:
        if any(k in co_txt for k in kws):
            return label
    # Pass 2: add collateral
    # Strip boilerplate phrases that carry no industry signal
    clean_col = re.sub(r'\([^)]*\)', '', collateral, flags=re.I)
    clean_col = re.sub(r'\ball\s+(?:assets|inventory|equipment|proceeds)\b', '', clean_col, flags=re.I)
    clean_col = re.sub(r'\bfuture\s+receivables?\b', '', clean_col, flags=re.I)
    clean_col = re.sub(r'\baccounts?\s+receivables?\b', '', clean_col, flags=re.I)
    clean_col = re.sub(r'\btech(?:nology)?\s+equipment\b', '', clean_col, flags=re.I)
    clean_col = re.sub(r'\bequipment\s+financing\b', '', clean_col, flags=re.I)
    clean_col = clean_col.strip()
    if not clean_col:  # nothing meaningful left — don't risk a false positive
        return 'Small Business'
    full_txt = (company_name + ' ' + clean_col).lower()
    for label, kws in _INDUSTRY_KW:
        if any(k in full_txt for k in kws):
            return label
    return 'Small Business'


_MCA_STOPWORDS = {'llc','inc','corp','co','ltd','the','and','of','in','for','a','an',
                  'group','services','solutions','enterprise','enterprises','mca','funding','capital'}

def _mca_industry_query(industry, company):
    """Return a targeted news query — fall back to company name words for unclassified businesses."""
    if industry in _INDUSTRY_NEWS_QUERY:
        return _INDUSTRY_NEWS_QUERY[industry]
    words = [w.strip('.,') for w in company.split()
             if len(w) > 2 and w.lower().strip('.,') not in _MCA_STOPWORDS]
    if words:
        return f"{' '.join(words[:3])} industry news business"
    return 'small business MCA financing news'


def _article(headline, url, source, date_str, scope, provider):
    if not headline or not url:
        return None
    return {'headline': headline[:160], 'url': url, 'source': source[:60],
            'date': date_str[:20] if date_str else '', 'scope': scope, 'provider': provider}


_STOPWORDS = {'llc','inc','corp','co','ltd','the','and','of','in','for','a','an',
              'to','is','are','was','were','at','by','with','or','trucking','funding',
              'capital','group','services','solutions','enterprise','enterprises'}

def _is_relevant(headline, company_name):
    """Check if headline contains at least one meaningful word from the company name."""
    tokens = {w.lower().strip('.,!?') for w in company_name.split() if len(w) > 2}
    tokens -= _STOPWORDS
    if not tokens:
        return True  # company name is all stopwords — can't filter
    h = headline.lower()
    return any(t in h for t in tokens)


def _sweep_gdelt(query, scope):
    try:
        # Use quoted exact match for company searches to avoid false positives
        q = urllib.parse.quote(f'{query} small business OR MCA OR financing')
        url = (f'https://api.gdeltproject.org/api/v2/doc/doc?query={q}'
               f'&mode=artlist&maxrecords=8&format=json&timespan=1M&sort=datedesc')
        r = _req.get(url, timeout=6, headers={'User-Agent': 'TomCat-Intel/2.0'})
        out = []
        for a in r.json().get('articles', []):
            art = _article(a.get('title'), a.get('url'), a.get('domain','GDELT'),
                           a.get('seendate',''), scope, 'GDELT')
            if art: out.append(art)
        return out
    except Exception:
        return []


def _sweep_gnews(query, scope):
    try:
        q = urllib.parse.quote(query)
        url = f'https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en'
        feed = feedparser.parse(url)
        out = []
        for e in feed.entries[:7]:
            src = ''
            if hasattr(e, 'source') and isinstance(e.source, dict):
                src = e.source.get('title', 'Google News')
            elif hasattr(e, 'tags') and e.tags:
                src = e.tags[0].get('term','Google News')
            else:
                src = 'Google News'
            art = _article(e.get('title'), e.get('link'), src,
                           e.get('published',''), scope, 'Google News')
            if art: out.append(art)
        return out
    except Exception:
        return []


def _sweep_reddit(company, industry_label):
    out = []
    seen_titles = set()
    headers = {'User-Agent': 'TomCat-Intel/2.0 (research bot)'}

    # Company mentions — only keep results that actually mention the company
    try:
        q = urllib.parse.quote(company)
        r = _req.get(f'https://www.reddit.com/search.json?q={q}&sort=relevance&limit=8&t=year',
                     headers=headers, timeout=5)
        for post in r.json().get('data', {}).get('children', []):
            d = post.get('data', {})
            title = d.get('title', '')
            # Deduplicate by title and enforce relevance
            if title in seen_titles:
                continue
            seen_titles.add(title)
            if not _is_relevant(title, company):
                continue
            art = _article(title, 'https://reddit.com' + d.get('permalink',''),
                           'r/' + d.get('subreddit','reddit'), '', 'company', 'Reddit')
            if art: out.append(art)
    except Exception:
        pass

    # Industry subreddit — always useful, no relevance filter needed
    sub = _INDUSTRY_SUBS.get(industry_label, 'smallbusiness')
    try:
        r = _req.get(f'https://www.reddit.com/r/{sub}/new.json?limit=6',
                     headers=headers, timeout=5)
        for post in r.json().get('data', {}).get('children', []):
            d = post.get('data', {})
            title = d.get('title', '')
            if title in seen_titles:
                continue
            seen_titles.add(title)
            art = _article(title, 'https://reddit.com' + d.get('permalink',''),
                           'r/' + d.get('subreddit', sub), '', 'industry', 'Reddit')
            if art: out.append(art)
    except Exception:
        pass
    return out


def _sweep_bing(query, scope):
    """Bing News RSS — no key needed for basic feed."""
    try:
        q = urllib.parse.quote(query)
        url = f'https://www.bing.com/news/search?q={q}&format=rss'
        feed = feedparser.parse(url)
        out = []
        for e in feed.entries[:6]:
            art = _article(e.get('title'), e.get('link'),
                           e.get('source', {}).get('title', 'Bing News') if hasattr(e, 'source') and isinstance(e.source, dict) else 'Bing News',
                           e.get('published',''), scope, 'Bing News')
            if art: out.append(art)
        return out
    except Exception:
        return []


@app.route('/api/leads/<lead_id>/intel')
def lead_intel(lead_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM mca_leads WHERE id = ?', [lead_id]).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Not found'}), 404

    lead       = dict(row)
    company    = (lead.get('company_name') or '').strip()
    collateral = (lead.get('collateral_desc') or '')
    industry   = _infer_industry(company, collateral)
    ind_query  = _mca_industry_query(industry, company)

    seen_urls, seen_titles, articles = set(), set(), []

    def safe_add(items):
        for a in (items or []):
            u = a.get('url', '')
            h = a.get('headline', '')
            # Skip if company-scoped but headline doesn't mention anything from company name
            if a.get('scope') == 'company' and not _is_relevant(h, company):
                continue
            # Deduplicate by URL and by headline (catches Reddit reposts)
            if u and u in seen_urls: continue
            if h and h in seen_titles: continue
            if u: seen_urls.add(u)
            if h: seen_titles.add(h)
            articles.append(a)

    t0 = time.time()
    # Use quoted exact name for company searches — reduces false positives for private small businesses
    company_q = f'"{company}"'
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {
            ex.submit(_sweep_gdelt,  company_q, 'company'):  'gdelt_co',
            ex.submit(_sweep_gdelt,  ind_query, 'industry'): 'gdelt_ind',
            ex.submit(_sweep_gnews,  company_q, 'company'):  'gnews_co',
            ex.submit(_sweep_gnews,  ind_query, 'industry'): 'gnews_ind',
            ex.submit(_sweep_bing,   company_q, 'company'):  'bing_co',
            ex.submit(_sweep_bing,   ind_query, 'industry'): 'bing_ind',
        }
        for f in concurrent.futures.as_completed(futures, timeout=14):
            try:
                safe_add(f.result())
            except Exception:
                pass

    elapsed = round(time.time() - t0, 2)

    company_arts  = [a for a in articles if a['scope'] == 'company'][:6]
    industry_arts = [a for a in articles if a['scope'] == 'industry'][:10]

    # Sentiment scoring
    score = 0
    for a in industry_arts:
        text = (a['headline'] + ' ' + a.get('summary', '')).lower()
        score += sum(1 for w in _POS_WORDS if w in text)
        score -= sum(1 for w in _NEG_WORDS if w in text)

    if score >= 2:
        sentiment = {'label': 'Positive', 'color': '#34d399', 'icon': '🟢'}
    elif score <= -2:
        sentiment = {'label': 'Negative', 'color': '#f87171', 'icon': '🔴'}
    else:
        sentiment = {'label': 'Neutral',  'color': '#fbbf24', 'icon': '🟡'}

    sources_swept = list({a['provider'] for a in articles})

    return jsonify({
        'company':          company_arts,
        'industry':         industry_arts,
        'company_empty':    len(company_arts) == 0,
        'industry_label':   industry,
        'sector_sentiment': sentiment,
        'sources_swept':    sources_swept,
        'elapsed_s':        elapsed,
        'total_articles':   len(articles),
    })


# ── Lender Stack Escalation Engine ──────────────────────────────────────────

_TIER_RANK  = {'A': 4, 'B': 3, 'C': 2, 'D': 1, None: 0}
_TIER_COLOR = {'A': '#34d399', 'B': '#60a5fa', 'C': '#fbbf24', 'D': '#f87171', None: '#64748b'}
_TIER_LABEL = {
    'A': 'Prime (A-Tier)',
    'B': 'Near-Prime (B-Tier)',
    'C': 'Sub-Prime (C-Tier)',
    'D': 'Distressed (D-Tier)',
    None: 'Unknown'
}

def _clean_lender(name):
    """Normalize lender names to remove series numbers and redundant text."""
    import re
    n = (name or '').strip()
    # Remove series numbers: "LOAN FUNDER LLC SERIES 12345" → "LOAN FUNDER LLC"
    n = re.sub(r'\s+SERIES\s+\d+', '', n, flags=re.I)
    n = re.sub(r'\s+\d{4,}', '', n)  # trailing long numbers
    n = re.sub(r'\s+LLC\b', ' LLC', n, flags=re.I)
    n = n.strip().rstrip(',').strip()
    return n[:45]

def _escalation_narrative(events):
    """
    Build a plain-English credit narrative from a list of funding events.
    Each event: {date, tier, lender, est_advance, days_to_lapse, lapse_date}
    Returns a dict with: story, risk_level, risk_color, headline, key_signals
    """
    if not events:
        return None

    tiers = [e['tier'] for e in events]
    ranks = [_TIER_RANK.get(t, 0) for t in tiers]
    valid_tiers = [t for t in tiers if t]

    n = len(events)
    first = events[0]
    last  = events[-1]
    current = events[-1]  # most recent filing

    # Compute unique lenders (deduped)
    unique_lenders = []
    seen = set()
    for e in events:
        cl = e['lender_clean']
        if cl and cl not in seen:
            seen.add(cl)
            unique_lenders.append({'lender': cl, 'tier': e['tier']})

    # ── Tier trajectory ──────────────────────────────────────────────────────
    if len(ranks) >= 2:
        start_rank = ranks[0]
        end_rank   = ranks[-1]
        rank_delta = end_rank - start_rank   # negative = deteriorated
        # Max rank seen (best funding ever achieved)
        max_rank   = max(ranks)
        min_rank   = min(r for r in ranks if r > 0) if any(r > 0 for r in ranks) else 0
    else:
        start_rank = ranks[0] if ranks else 0
        end_rank   = start_rank
        rank_delta = 0
        max_rank   = start_rank
        min_rank   = start_rank

    # ── Time between fundings ────────────────────────────────────────────────
    cycle_days = []
    for i in range(1, len(events)):
        d0 = events[i-1].get('filing_date', '')
        d1 = events[i].get('filing_date', '')
        if d0 and d1:
            try:
                from datetime import datetime
                diff = (datetime.strptime(d1, '%Y-%m-%d') -
                        datetime.strptime(d0, '%Y-%m-%d')).days
                if 0 < diff < 3000:
                    cycle_days.append(diff)
            except Exception:
                pass

    avg_cycle = int(sum(cycle_days) / len(cycle_days)) if cycle_days else None
    shrinking_cycles = (len(cycle_days) >= 3 and
                        cycle_days[-1] < cycle_days[0] * 0.7) if cycle_days else False

    # ── Key signals ──────────────────────────────────────────────────────────
    key_signals = []

    # First-time borrower vs serial
    if n == 1:
        key_signals.append({'label': 'First-time borrower', 'type': 'positive',
                            'detail': 'No prior MCA history — clean slate, lower risk of over-leverage.'})
    elif n <= 3:
        key_signals.append({'label': f'{n} total fundings', 'type': 'neutral',
                            'detail': f'Moderate MCA history. Has accessed capital {n} times.'})
    else:
        key_signals.append({'label': f'{n} total fundings (serial borrower)', 'type': 'warning',
                            'detail': f'Heavy MCA usage — {n} filings on record. High probability of existing positions at time of any new funding.'})

    # Tier escalation
    if rank_delta < -1:
        key_signals.append({'label': 'Tier escalation detected', 'type': 'negative',
                            'detail': f'Funding quality dropped from {_TIER_LABEL.get(first["tier"])} to {_TIER_LABEL.get(last["tier"])} — signals deteriorating creditworthiness.'})
    elif rank_delta > 1:
        key_signals.append({'label': 'Tier improvement', 'type': 'positive',
                            'detail': f'Funding quality improved from {_TIER_LABEL.get(first["tier"])} to {_TIER_LABEL.get(last["tier"])} — business health trending up.'})
    elif valid_tiers and all(t == valid_tiers[0] for t in valid_tiers):
        key_signals.append({'label': f'Consistent {_TIER_LABEL.get(valid_tiers[0])} borrower', 'type': 'neutral',
                            'detail': 'Lender tier has been stable across all fundings.'})

    # D-tier presence
    if 'D' in tiers:
        key_signals.append({'label': 'D-Tier (distressed) lender on record', 'type': 'negative',
                            'detail': 'At least one funding was from a last-resort, high-rate MCA provider — indicates prior cash flow crisis.'})

    # Shrinking cycles
    if shrinking_cycles:
        key_signals.append({'label': 'Accelerating funding cycles', 'type': 'negative',
                            'detail': f'Time between fundings has shortened (avg {avg_cycle}d) — borrower is returning to market faster, consistent with cash flow compression.'})
    elif avg_cycle and avg_cycle < 180:
        key_signals.append({'label': f'Short funding cycle ({avg_cycle}d avg)', 'type': 'warning',
                            'detail': 'Borrower returns to market every ~{} days on average. May indicate low revenue cushion.'.format(avg_cycle)})

    # Simultaneous stacking
    max_stack = max((e.get('stack_depth', 1) or 1 for e in events), default=1)
    if max_stack >= 4:
        key_signals.append({'label': f'Heavy stacking detected ({max_stack} positions)', 'type': 'negative',
                            'detail': f'Peak of {max_stack} simultaneous MCA positions — extremely high daily payment burden.'})
    elif max_stack >= 2:
        key_signals.append({'label': f'Position stacking ({max_stack}x)', 'type': 'warning',
                            'detail': f'Has carried {max_stack} simultaneous MCA positions — elevated daily payment burden.'})

    # ── Lender loyalty vs. always switching ─────────────────────────────────
    lender_names = [e['lender_clean'] for e in events if e.get('lender_clean')]
    repeat_lenders = len(lender_names) - len(unique_lenders)
    if n >= 3:
        if repeat_lenders >= 2:
            key_signals.append({'label': f'Lender loyalty ({repeat_lenders} repeat engagements)', 'type': 'positive',
                                'detail': f'Borrower has returned to the same lender(s) {repeat_lenders} times — indicates stable relationships and manageable cash flow.'})
        elif len(unique_lenders) == n and n >= 4:
            key_signals.append({'label': 'Always switches lenders', 'type': 'warning',
                                'detail': f'Every one of {n} fundings used a different lender — suggests either prior relationships soured or lenders declined to renew. Warrants scrutiny.'})

    # ── Advance size trend (revenue proxy) ──────────────────────────────────
    advances = [(e['filing_date'], e['advance']) for e in events
                if e.get('advance') and e['advance'] > 0 and e.get('filing_date')]
    advances.sort(key=lambda x: x[0])
    advance_trend = None
    advance_points = []
    if len(advances) >= 2:
        advance_points = [{'date': d, 'amount': a} for d, a in advances]
        first_adv = advances[0][1]
        last_adv  = advances[-1][1]
        pct_change = (last_adv - first_adv) / first_adv * 100 if first_adv else 0
        if pct_change >= 30:
            advance_trend = 'up'
            key_signals.append({'label': f'Advance size growing (+{int(pct_change)}%)', 'type': 'positive',
                                'detail': f'Advance amounts increased from ${first_adv:,} to ${last_adv:,} — lenders are extending more capital, consistent with business growth.'})
        elif pct_change <= -25:
            advance_trend = 'down'
            key_signals.append({'label': f'Advance size shrinking ({int(pct_change)}%)', 'type': 'negative',
                                'detail': f'Advance amounts dropped from ${first_adv:,} to ${last_adv:,} — lenders are pulling back, consistent with declining revenue or deteriorating creditworthiness.'})
        else:
            advance_trend = 'flat'


    neg_count = sum(1 for s in key_signals if s['type'] == 'negative')
    warn_count = sum(1 for s in key_signals if s['type'] == 'warning')

    if neg_count >= 2 or (neg_count >= 1 and n >= 5):
        risk_level = 'HIGH'
        risk_color = '#f87171'
        risk_bg    = 'rgba(239,68,68,.08)'
        risk_border= 'rgba(239,68,68,.25)'
    elif neg_count >= 1 or warn_count >= 2:
        risk_level = 'ELEVATED'
        risk_color = '#fbbf24'
        risk_bg    = 'rgba(251,191,36,.08)'
        risk_border= 'rgba(251,191,36,.25)'
    elif n == 1 and end_rank >= 3:
        risk_level = 'LOW'
        risk_color = '#34d399'
        risk_bg    = 'rgba(52,211,153,.08)'
        risk_border= 'rgba(52,211,153,.25)'
    else:
        risk_level = 'MODERATE'
        risk_color = '#60a5fa'
        risk_bg    = 'rgba(96,165,250,.08)'
        risk_border= 'rgba(96,165,250,.25)'

    # ── Headline narrative sentence ──────────────────────────────────────────
    if n == 1:
        headline = (f"First-ever MCA filing from a {_TIER_LABEL.get(current['tier'])} lender — "
                    f"clean credit history, no prior MCA exposure.")
    elif rank_delta <= -2 and n >= 3:
        headline = (f"{n}-time MCA borrower with confirmed tier decline: "
                    f"{_TIER_LABEL.get(first['tier'])} → {_TIER_LABEL.get(last['tier'])}. "
                    f"Escalating risk profile — this is a distressed credit story.")
    elif 'D' in tiers and n >= 2:
        headline = (f"Serial borrower ({n} fundings) who has reached D-Tier financing — "
                    f"last-resort capital was required at some point. Approach with appropriate structure.")
    elif n >= 5 and shrinking_cycles:
        headline = (f"{n} total fundings with accelerating cycle times — "
                    f"borrower is returning to market faster each round, indicating cash flow pressure.")
    elif rank_delta >= 2 and n >= 2:
        headline = (f"Credit improvement detected: {n} fundings, tier improved from "
                    f"{_TIER_LABEL.get(first['tier'])} to {_TIER_LABEL.get(last['tier'])}. "
                    f"Business appears to be stabilizing.")
    else:
        headline = (f"{n} total MCA fundings across {len(unique_lenders)} lender(s). "
                    f"Current lender is {_TIER_LABEL.get(current['tier'])}. "
                    f"{'Stable borrowing pattern.' if not warn_count else 'Some risk signals present — review below.'}")

    return {
        'headline':        headline,
        'risk_level':      risk_level,
        'risk_color':      risk_color,
        'risk_bg':         risk_bg,
        'risk_border':     risk_border,
        'key_signals':     key_signals,
        'unique_lenders':  unique_lenders,
        'avg_cycle_days':  avg_cycle,
        'total_fundings':  n,
        'tier_start':      first['tier'],
        'tier_end':        last['tier'],
        'tier_start_color':_TIER_COLOR.get(first['tier'], '#64748b'),
        'tier_end_color':  _TIER_COLOR.get(last['tier'], '#64748b'),
        'advance_trend':   advance_trend,
        'advance_points':  advance_points,
    }


@app.route('/api/leads/<lead_id>/stack-history')
def lead_stack_history(lead_id):
    conn = get_db()

    # Get the lead
    lead = conn.execute('SELECT * FROM mca_leads WHERE id = ?', [lead_id]).fetchone()
    if not lead:
        conn.close()
        return jsonify({'error': 'Not found'}), 404

    lead = dict(lead)
    company = lead.get('company_name', '').strip()
    state   = lead.get('state', '').strip()

    # All filings for this company (same state)
    rows = conn.execute("""
        SELECT id, filing_date, lapse_date, funder_tier, secured_party,
               collateral_desc, est_advance_amount, stack_depth, days_to_lapse,
               position_number
        FROM mca_leads
        WHERE LOWER(company_name) = LOWER(?) AND state = ?
        ORDER BY filing_date ASC NULLS LAST, id ASC
        LIMIT 30
    """, [company, state]).fetchall()
    conn.close()

    events = []
    for r in rows:
        tier = r['funder_tier']
        lender_raw   = r['secured_party'] or ''
        lender_clean = _clean_lender(lender_raw)
        events.append({
            'id':           r['id'],
            'filing_date':  r['filing_date'] or '',
            'lapse_date':   r['lapse_date'] or '',
            'tier':         tier,
            'tier_color':   _TIER_COLOR.get(tier, '#64748b'),
            'tier_label':   _TIER_LABEL.get(tier, 'Unknown'),
            'lender_raw':   lender_raw[:60],
            'lender_clean': lender_clean,
            'advance':      r['est_advance_amount'] or 0,
            'stack_depth':  r['stack_depth'] or 1,
            'days_to_lapse': r['days_to_lapse'],
            'is_current':   r['id'] == lead_id or str(r['id']) == str(lead_id),
        })

    narrative = _escalation_narrative(events)

    return jsonify({
        'company':  company,
        'state':    state,
        'events':   events,
        'narrative': narrative,
    })


# ── Court & Regulatory Sweep Engine ─────────────────────────────────────────
import re as _re
from urllib.parse import quote_plus as _qp

_COURT_TO = 7  # per-source timeout seconds

def _court_headers():
    return {'User-Agent': 'Mozilla/5.0 (compatible; TomcatMCA-Research/1.0)'}

def _sweep_courtlistener(company):
    """CourtListener REST API — federal PACER/RECAP records (free, no key)."""
    try:
        url = (f"https://www.courtlistener.com/api/rest/v3/search/"
               f"?q=%22{_qp(company)}%22&type=r&order_by=score+desc&page_size=6")
        r = _req.get(url, headers=_court_headers(), timeout=_COURT_TO)
        if r.status_code != 200:
            return []
        data = r.json()
        out = []
        for item in (data.get('results') or [])[:5]:
            name = (item.get('caseName') or '')[:80]
            court = item.get('court', '')
            is_bk = any(x in name.lower() for x in ['bankrupt', 'chapter 7', 'chapter 11', 'chapter 13'])
            out.append({
                'source': 'CourtListener', 'provider_class': 'court',
                'type': 'Bankruptcy' if is_bk else 'Federal Civil Record',
                'headline': name,
                'detail': f"Court: {court.upper()} · Filed: {(item.get('dateFiled') or '')[:10]}",
                'date': (item.get('dateFiled') or '')[:10],
                'status': item.get('status', ''),
                'url': f"https://www.courtlistener.com{item.get('absolute_url', '')}",
                'severity': 'high' if is_bk else 'medium',
            })
        return out
    except Exception:
        return []

def _sweep_cfpb(company):
    """CFPB Consumer Complaint Database (free, documented API, no key)."""
    try:
        url = (f"https://api.consumerfinance.gov/data/complaints/.json"
               f"?search_term={_qp(company)}&field=company&size=5&sort=created_date_desc")
        r = _req.get(url, headers=_court_headers(), timeout=_COURT_TO)
        if r.status_code != 200:
            return {'count': 0, 'items': []}
        data = r.json()
        hits = data.get('hits', {})
        total = hits.get('total', 0)
        if isinstance(total, dict):
            total = total.get('value', 0)
        items = []
        for h in (hits.get('hits') or [])[:4]:
            s = h.get('_source', {})
            items.append({
                'source': 'CFPB', 'provider_class': 'cfpb',
                'type': 'Consumer Complaint',
                'headline': f"{s.get('product','Unknown Product')}: {(s.get('issue') or '')[:60]}",
                'detail': f"Response: {s.get('company_response','—')}",
                'date': (s.get('date_received') or '')[:10],
                'status': s.get('company_response', ''),
                'url': 'https://www.consumerfinance.gov/data-research/consumer-complaints/',
                'severity': 'medium',
            })
        return {'count': int(total), 'items': items}
    except Exception:
        return {'count': 0, 'items': []}

def _sweep_epa(company):
    """EPA ECHO — Clean Air Act + Hazardous Waste violations (free, no key)."""
    out = []
    for media, label in [('caa', 'Clean Air Act'), ('rcra', 'Hazardous Waste (RCRA)')]:
        try:
            url = (f"https://echodata.epa.gov/echo/{media}_rest_services.get_facilities"
                   f"?p_fn={_qp(company)}&output=JSON&p_rows=3")
            r = _req.get(url, headers=_court_headers(), timeout=_COURT_TO)
            if r.status_code != 200:
                continue
            facilities = (r.json().get('Results') or {}).get('Facilities') or []
            for f in facilities[:2]:
                qtrs = int(f.get(f'{media.upper()}QtrsWithNC', 0) or 0)
                if qtrs == 0:
                    continue
                rid = f.get('RegistryID', '')
                out.append({
                    'source': 'EPA ECHO', 'provider_class': 'epa',
                    'type': label,
                    'headline': f"{f.get('FacilityName', company)[:50]} — {qtrs} quarter(s) non-compliant",
                    'detail': f"Registry ID: {rid}",
                    'date': '',
                    'status': 'Non-Compliant',
                    'url': f"https://echo.epa.gov/facilities/facility-search/results?p_fn={_qp(company)}",
                    'severity': 'high' if qtrs >= 4 else 'medium',
                })
        except Exception:
            continue
    return out

def _sweep_osha(company):
    """OSHA public enforcement search (scrape, free, no key)."""
    try:
        url = (f"https://www.osha.gov/ords/imis/establishment.html"
               f"?establishment_name={_qp(company)}&state=All&officetype=fed"
               f"&startmonth=01&startyear=2015&endmonth=12&endyear=2026"
               f"&action=31&p_start=&p_finish=0&p_sort=14&p_desc=DESC&p_direction=Next&p_show=5")
        r = _req.get(url, headers=_court_headers(), timeout=_COURT_TO)
        if r.status_code != 200:
            return []
        html = r.text
        if 'no records' in html.lower() or 'No Records' in html:
            return []
        # Extract inspection dates and penalties from OSHA HTML table
        rows = _re.findall(
            r'<td[^>]*>\s*(\d{2}/\d{2}/\d{4})\s*</td>.*?penalty.*?>\s*\$?([\d,]*)\s*<',
            html, _re.DOTALL | _re.IGNORECASE)
        out = []
        for date_str, penalty_str in rows[:3]:
            penalty = int(penalty_str.replace(',', '')) if penalty_str.replace(',', '').isdigit() else 0
            out.append({
                'source': 'OSHA', 'provider_class': 'osha',
                'type': 'Workplace Safety Inspection',
                'headline': f"OSHA inspection — ${penalty:,} penalty" if penalty else "OSHA inspection on record",
                'detail': f"Inspection date: {date_str}",
                'date': date_str,
                'status': 'Cited' if penalty > 0 else 'Inspected',
                'url': f"https://www.osha.gov/ords/imis/establishment.html?establishment_name={_qp(company)}",
                'severity': 'high' if penalty >= 5000 else 'medium',
            })
        return out
    except Exception:
        return []

def _legal_risk(records_count, cfpb_count, epa_count, osha_count):
    score = records_count * 3
    score += (3 if cfpb_count >= 10 else 2 if cfpb_count >= 3 else 1 if cfpb_count >= 1 else 0)
    score += epa_count * 3
    score += osha_count * 2
    if score == 0:
        return {'level': 'CLEAR',    'color': '#34d399', 'bg': 'rgba(52,211,153,.08)',  'border': 'rgba(52,211,153,.25)',  'icon': '✓'}
    elif score <= 3:
        return {'level': 'LOW',      'color': '#60a5fa', 'bg': 'rgba(96,165,250,.08)',  'border': 'rgba(96,165,250,.25)',  'icon': '◎'}
    elif score <= 7:
        return {'level': 'ELEVATED', 'color': '#fbbf24', 'bg': 'rgba(251,191,36,.08)',  'border': 'rgba(251,191,36,.25)',  'icon': '⚠'}
    else:
        return {'level': 'HIGH',     'color': '#f87171', 'bg': 'rgba(239,68,68,.08)',   'border': 'rgba(239,68,68,.25)',   'icon': '⛔'}

@app.route('/api/leads/<lead_id>/court-sweep')
def lead_court_sweep(lead_id):
    conn = get_db()
    lead = conn.execute('SELECT * FROM mca_leads WHERE id = ?', [lead_id]).fetchone()
    conn.close()
    if not lead:
        return jsonify({'error': 'Not found'}), 404
    company = dict(lead).get('company_name', '').strip()
    t0 = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        f_court = ex.submit(_sweep_courtlistener, company)
        f_cfpb  = ex.submit(_sweep_cfpb, company)
        f_epa   = ex.submit(_sweep_epa, company)
        f_osha  = ex.submit(_sweep_osha, company)
        try:    court_records = f_court.result(timeout=_COURT_TO + 2)
        except Exception: court_records = []
        try:    cfpb_data     = f_cfpb.result(timeout=_COURT_TO + 2)
        except Exception: cfpb_data = {'count': 0, 'items': []}
        try:    epa_records   = f_epa.result(timeout=_COURT_TO + 2)
        except Exception: epa_records = []
        try:    osha_records  = f_osha.result(timeout=_COURT_TO + 2)
        except Exception: osha_records = []

    cfpb_count = cfpb_data.get('count', 0) if isinstance(cfpb_data, dict) else 0
    all_items  = (court_records +
                  (cfpb_data.get('items') if isinstance(cfpb_data, dict) else []) +
                  epa_records + osha_records)

    risk = _legal_risk(len(court_records), cfpb_count, len(epa_records), len(osha_records))

    sources_hit = []
    if court_records:  sources_hit.append('CourtListener')
    if cfpb_count > 0: sources_hit.append('CFPB')
    if epa_records:    sources_hit.append('EPA ECHO')
    if osha_records:   sources_hit.append('OSHA')

    return jsonify({
        'company':       company,
        'risk':          risk,
        'items':         all_items,
        'cfpb_total':    cfpb_count,
        'sources_hit':   sources_hit,
        'sources_swept': ['CourtListener', 'CFPB', 'EPA ECHO', 'OSHA'],
        'total_findings': len(all_items),
        'elapsed_s':     round(time.time() - t0, 2),
    })


@app.route('/api/stats')
def stats():
    state         = request.args.get('state', 'all')
    urgency       = request.args.get('urgency', 'all')
    tier_filter   = request.args.get('tier', 'all')
    status_filter = request.args.get('status', 'all')
    signal_filter = request.args.get('signal', 'all')
    search        = request.args.get('q', '').strip()

    where_parts = ["1=1"]
    params = []

    if search:
        where_parts.append("(m.company_name LIKE ? OR m.city LIKE ? OR m.secured_party LIKE ? OR m.dba_name LIKE ?)")
        sw = f'%{search}%'
        params += [sw, sw, sw, sw]
    if urgency == '7d':
        where_parts.append("(m.days_to_lapse >= 0 AND m.days_to_lapse <= 7)")
    elif urgency == '14d':
        where_parts.append("(m.days_to_lapse >= 0 AND m.days_to_lapse <= 14)")
    elif urgency == 'hot':
        where_parts.append("(m.days_to_lapse <= 30 AND m.days_to_lapse >= -90)")
    elif urgency == 'warm':
        where_parts.append("(m.days_to_lapse > 30 AND m.days_to_lapse <= 180)")
    elif urgency == 'cold':
        where_parts.append("(m.days_to_lapse > 180 OR m.days_to_lapse < -90 OR m.days_to_lapse IS NULL)")
    if state != 'all':
        where_parts.append("m.state = ?")
        params.append(state)
    if tier_filter != 'all':
        where_parts.append("m.funder_tier = ?")
        params.append(tier_filter)
    if signal_filter == 'expansion':
        where_parts.append("m.signals_json LIKE '%S2_EXPANSION%'")
    elif signal_filter == 'hiring':
        where_parts.append("m.signals_json LIKE '%S3_HIRING%'")
    elif signal_filter == 'distress':
        where_parts.append("(m.signals_json LIKE '%S4_TAX_LIEN%' OR m.signals_json LIKE '%S5_JUDGMENT%' OR m.signals_json LIKE '%S6_DISTRESS%')")

    claim_join = "LEFT JOIN lead_claims lc ON lc.lead_id = m.id AND lc.broker_name = ?"
    params.insert(0, DEFAULT_BROKER)
    if status_filter == 'unclaimed':
        where_parts.append("lc.id IS NULL")
    elif status_filter == 'claimed':
        where_parts.append("lc.id IS NOT NULL")

    where_sql = ' AND '.join(where_parts)
    base_from = f"FROM mca_leads m {claim_join} WHERE {where_sql}"

    conn = get_db()
    total = conn.execute(f"SELECT COUNT(*) {base_from}", params).fetchone()[0]

    hot = conn.execute(f"""
        SELECT COUNT(*) {base_from}
        AND m.days_to_lapse <= 30 AND m.days_to_lapse >= -90
    """, params).fetchone()[0]

    warm = conn.execute(f"""
        SELECT COUNT(*) {base_from}
        AND m.days_to_lapse > 30 AND m.days_to_lapse <= 180
    """, params).fetchone()[0]

    cold = conn.execute(f"""
        SELECT COUNT(*) {base_from}
        AND (m.days_to_lapse > 180 OR m.days_to_lapse < -90 OR m.days_to_lapse IS NULL)
    """, params).fetchone()[0]

    expansion = conn.execute(f"SELECT COUNT(*) {base_from} AND m.signals_json LIKE '%S2_EXPANSION%'", params).fetchone()[0]
    hiring = conn.execute(f"SELECT COUNT(*) {base_from} AND m.signals_json LIKE '%S3_HIRING%'", params).fetchone()[0]
    osint_sweeps = conn.execute(f"SELECT COUNT(*) {base_from} AND m.signals_checked_at IS NOT NULL", params).fetchone()[0]

    stacked = conn.execute(f"""
        SELECT COUNT(*) FROM (
            SELECT m.company_name {base_from}
            GROUP BY m.company_name HAVING COUNT(DISTINCT m.secured_party) >= 2
        )
    """, params).fetchone()[0]

    expiring_week = conn.execute(f"""
        SELECT COUNT(*) {base_from}
        AND m.days_to_lapse >= -7 AND m.days_to_lapse <= 7
    """, params).fetchone()[0]

    avg_stack = conn.execute(f"""
        SELECT AVG(m.stack_depth) {base_from}
        AND m.days_to_lapse <= 30 AND m.days_to_lapse >= -365
    """, params).fetchone()[0] or 1.0

    velocity = conn.execute(f"""
        SELECT COUNT(*) / 13.0 {base_from}
        AND m.days_to_lapse >= -90 AND m.days_to_lapse <= 90
    """, params).fetchone()[0] or 0

    my_claims = conn.execute(f"""
        SELECT lc.status, COUNT(*) {base_from} AND lc.id IS NOT NULL GROUP BY lc.status
    """, params).fetchall()

    tax_liens = conn.execute(f"SELECT COUNT(*) {base_from} AND m.signals_json LIKE '%S4_TAX_LIEN%'", params).fetchone()[0]
    judgments = conn.execute(f"SELECT COUNT(*) {base_from} AND m.signals_json LIKE '%S5_JUDGMENT%'", params).fetchone()[0]
    distress_other = conn.execute(f"SELECT COUNT(*) {base_from} AND m.signals_json LIKE '%S6_DISTRESS%'", params).fetchone()[0]
    distress_total = conn.execute(f"""
        SELECT COUNT(*) {base_from} AND (
        m.signals_json LIKE '%S4_TAX_LIEN%' OR m.signals_json LIKE '%S5_JUDGMENT%' OR m.signals_json LIKE '%S6_DISTRESS%')
    """, params).fetchone()[0]

    lapsed = conn.execute(f"""
        SELECT COUNT(*) {base_from} AND m.days_to_lapse < 0 AND m.days_to_lapse >= -365
    """, params).fetchone()[0]

    uncontested = conn.execute(f"""
        SELECT COUNT(*) {base_from}
        AND m.days_to_lapse < 0
        AND m.company_name NOT IN (
            SELECT company_name FROM mca_leads WHERE days_to_lapse >= 0
        )
    """, params).fetchone()[0]

    hot_leads_data = conn.execute(f"""
        SELECT COALESCE(SUM(m.est_advance_amount), 0),
               COALESCE(AVG(m.est_advance_amount), 40000),
               m.secured_party
        {base_from}
        AND m.days_to_lapse <= 30 AND m.days_to_lapse >= -90
        GROUP BY m.secured_party
    """, params).fetchall()

    raw_sum = conn.execute(f"""
        SELECT COALESCE(SUM(m.est_advance_amount), 0) {base_from}
        AND m.est_advance_amount > 0
    """, params).fetchone()[0]
    pipeline_value = int(raw_sum) if raw_sum > 0 else total * 40000

    hot_funders = conn.execute(f"""
        SELECT m.secured_party {base_from}
        AND m.days_to_lapse <= 30 AND m.days_to_lapse >= -90
    """, params).fetchall()
    gm_total = 0
    gm_count = 0
    D_TIER = ['YELLOWSTONE','LAST CHANCE','LCF','GREEN BOX','GREENBOX','LIKETY',
              'VADER','BITTY','LAZARUS','ZLUR','MEGED','EBF','EVEREST','APPFUNDING']
    C_TIER = ['NATIONAL FUNDING','FUNDKITE','FORA FINANCIAL','PEARL CAPITAL',
              'FOX CAPITAL','RAPID ADVANCE','STONE FUNDING','BLUE ROCK','SEAMLESS','MAZAL']
    B_TIER = ['BLUEVINE','KABBAGE','CAN CAPITAL','ONDECK','ON DECK',
              'CREDIBLY','HEADWAY','LIBERTAS','FORWARD','ESSENTIAL','CFG']
    A_TIER = ['PAYPAL','SQUARE','STRIPE','AMAZON','SHOPIFY','FUNDBOX','CLEARCO','PIPE']
    for (funder_raw,) in hot_funders:
        lu = (funder_raw or '').upper()
        if any(k in lu for k in D_TIER):   gm = 12.0
        elif any(k in lu for k in C_TIER): gm = 9.0
        elif any(k in lu for k in B_TIER): gm = 6.5
        elif any(k in lu for k in A_TIER): gm = 4.0
        else:                              gm = 7.0
        gm_total += gm
        gm_count += 1
    avg_gm = round(gm_total / gm_count, 1) if gm_count > 0 else 7.0
    gm_value = int(pipeline_value * avg_gm / 100)

    conn.close()

    return jsonify({
        "total": total, "hot": hot, "warm": warm, "cold": cold,
        "expansion": expansion, "hiring": hiring, "stacked": stacked,
        "osint_sweeps": osint_sweeps,
        "expiring_week": expiring_week, "avg_stack": round(avg_stack, 1),
        "velocity": round(velocity, 0),
        "tax_liens": tax_liens, "judgments": judgments,
        "distress_other": distress_other, "distress_total": distress_total,
        "lapsed": lapsed, "uncontested": uncontested,
        "pipeline_value": pipeline_value,
        "avg_gm": avg_gm,
        "gm_value": gm_value,
        "my_pipeline": {r[0]: r[1] for r in my_claims}
    })

# ── Lead Purchase System (Stripe) ─────────────────────────────────────────────

MCA_LEAD_TIERS = {
    'hot_urgent': {'label': '🔴 Urgent',  'desc': 'Expires ≤7 days',  'price': 22500},  # $225
    'hot':        {'label': '🔥 Hot',     'desc': 'Expires ≤30 days', 'price': 15000},  # $150
    'warm':       {'label': '🟡 Warm',    'desc': '31–180 days',      'price':  8500},  # $85
    'cold':       {'label': '🔵 Cold',    'desc': '180+ days',        'price':  4500},  # $45
}

MCA_BULK_PACKS = {
    'pack_10': {'qty': 10, 'label': '10-Lead Pack', 'discount': 0.10},
    'pack_25': {'qty': 25, 'label': '25-Lead Pack', 'discount': 0.15},
    'pack_50': {'qty': 50, 'label': '50-Lead Pack', 'discount': 0.20},
}


def get_mca_lead_tier(lead):
    dtl = lead.get('days_to_lapse')
    if dtl is not None and dtl <= 7:
        return 'hot_urgent', MCA_LEAD_TIERS['hot_urgent']
    elif dtl is not None and dtl <= 30:
        return 'hot', MCA_LEAD_TIERS['hot']
    elif dtl is not None and dtl <= 180:
        return 'warm', MCA_LEAD_TIERS['warm']
    return 'cold', MCA_LEAD_TIERS['cold']


def init_mca_purchase_tables():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lead_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT NOT NULL,
            buyer_email TEXT,
            tier TEXT,
            price_cents INTEGER,
            stripe_session_id TEXT,
            stripe_payment_intent TEXT,
            status TEXT DEFAULT 'pending',
            purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(lead_id, status)
        )
    """)
    conn.commit()
    conn.close()


@app.route('/api/leads/<int:lead_id>/pricing')
def mca_lead_pricing(lead_id):
    conn = get_db()
    row = conn.execute('SELECT * FROM mca_leads WHERE id = ?', [lead_id]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    lead = dict(row)
    tier_key, tier = get_mca_lead_tier(lead)
    purchased = conn.execute(
        "SELECT id FROM lead_purchases WHERE lead_id = ? AND status = 'completed'", [lead_id]
    ).fetchone()
    conn.close()
    return jsonify({
        'lead_id':       lead_id,
        'tier':          tier_key,
        'tier_label':    tier['label'],
        'tier_desc':     tier['desc'],
        'price_cents':   tier['price'],
        'price_display': f"${tier['price']/100:.0f}",
        'is_purchased':  purchased is not None,
        'exclusive':     True,
    })


@app.route('/api/leads/<int:lead_id>/checkout', methods=['POST'])
def mca_create_checkout(lead_id):
    if not stripe.api_key:
        return jsonify({'error': 'Stripe not configured'}), 500
    conn = get_db()
    row = conn.execute('SELECT * FROM mca_leads WHERE id = ?', [lead_id]).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    lead = dict(row)
    existing = conn.execute(
        "SELECT id FROM lead_purchases WHERE lead_id = ? AND status = 'completed'", [lead_id]
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'Lead already purchased'}), 409
    tier_key, tier = get_mca_lead_tier(lead)
    company = lead.get('company_name', 'Unknown Company')
    funder  = (lead.get('secured_party') or 'MCA Lender')[:40]
    host    = request.host_url.rstrip('/')
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{'price_data': {
                'currency': 'usd',
                'unit_amount': tier['price'],
                'product_data': {
                    'name': f'Tomcat MCA — {tier["label"]} Lead',
                    'description': f'{company} | {funder} | {tier["desc"]} | Exclusive',
                },
            }, 'quantity': 1}],
            mode='payment',
            success_url=f'{host}/purchase-success?session_id={{CHECKOUT_SESSION_ID}}&lead_id={lead_id}',
            cancel_url=f'{host}/',
            metadata={'lead_id': lead_id, 'tier': tier_key, 'company': company}
        )
        conn.execute(
            "INSERT INTO lead_purchases (lead_id, tier, price_cents, stripe_session_id, status) VALUES (?, ?, ?, ?, 'pending')",
            [lead_id, tier_key, tier['price'], session.id]
        )
        conn.commit()
        conn.close()
        return jsonify({'checkout_url': session.url, 'session_id': session.id})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/purchase/verify')
def mca_verify_purchase():
    session_id = request.args.get('session_id', '')
    lead_id    = request.args.get('lead_id', '')
    if not session_id:
        return jsonify({'error': 'Missing session_id'}), 400
    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status == 'paid':
            conn = get_db()
            conn.execute(
                "UPDATE lead_purchases SET status='completed', buyer_email=?, stripe_payment_intent=? WHERE stripe_session_id=?",
                [session.customer_details.email if session.customer_details else '', session.payment_intent, session_id]
            )
            conn.commit()
            conn.close()
            return jsonify({'status': 'completed', 'lead_id': lead_id})
        return jsonify({'status': session.payment_status})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    endpoint_secret = os.environ.get('STRIPE_WEBHOOK_SECRET', 'whsec_dummy')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, endpoint_secret
        )
    except ValueError:
        return jsonify({'error': 'Invalid payload'}), 400
    except stripe.error.SignatureVerificationError:
        return jsonify({'error': 'Invalid signature'}), 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        session_id = session.get('id')
        payment_intent = session.get('payment_intent')
        buyer_email = session.customer_details.email if session.get('customer_details') else ''

        conn = get_db()
        conn.execute(
            "UPDATE lead_purchases SET status='completed', buyer_email=?, stripe_payment_intent=? WHERE stripe_session_id=?",
            [buyer_email, payment_intent, session_id]
        )
        conn.commit()
        conn.close()

    return jsonify({'status': 'success'}), 200





@app.route('/purchase-success')
def mca_purchase_success():
    return send_from_directory(app.static_folder, 'index.html')


# ── Apollo On-Demand Contact Unlock ────────────────────────────────────────

@app.route('/api/leads/<int:lead_id>/contacts', methods=['POST'])
def mca_contact_unlock(lead_id):
    """
    On-demand Apollo contact fetch. Burns 1 Apollo credit per company per day.
    Gate: lead must be purchased first.
    """
    if not _is_purchased(lead_id):
        return jsonify({'error': 'Purchase required', 'locked': True}), 402

    conn = get_db()
    try:
        # Ensure cache tables exist
        init_contact_cache(conn)

        row = conn.execute(
            'SELECT company_name, city, state FROM mca_leads WHERE id = ?',
            [lead_id]
        ).fetchone()
        if not row:
            conn.close()
            return jsonify({'error': 'Lead not found'}), 404

        company_name = row['company_name'] or ''
        city         = row['city'] or ''
        state        = row['state'] or ''
        buyer_email  = request.get_json(silent=True, force=True) or {}
        buyer_email  = buyer_email.get('email', '')

        contacts = fetch_apollo_contacts(
            company_name, city, state, conn, lead_id, buyer_email
        )
        conn.close()

        return jsonify({
            'lead_id':   lead_id,
            'company':   company_name,
            'contacts':  contacts,
            'count':     len(contacts),
            'source':    'apollo',
            'cached':    len(contacts) > 0,
        })
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/unlock-stats')
def mca_unlock_stats():
    """Admin: see which leads are being unlocked most (engagement analytics)."""
    conn = get_db()
    try:
        init_contact_cache(conn)
        stats = get_unlock_stats(conn)
        conn.close()
        return jsonify({'unlocks': stats, 'count': len(stats)})
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500


# ── Serve portal ──────────────────────────────────────────────────────────────

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, 'index.html')


if __name__ == '__main__':
    init_db()
    init_mca_purchase_tables()
    app.run(host='0.0.0.0', port=5051, debug=True)

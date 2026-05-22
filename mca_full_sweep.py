"""
Tomcat MCA — FULL SWEEP: Pull ALL MCA-related UCC filings from CT + CO
Instead of searching by lender name one at a time, this script pulls the
top MCA secured parties by filing count and ingests them all.
"""

import os, json, time, sqlite3, logging, requests
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s [MCA-SWEEP] %(levelname)s - %(message)s')
logger = logging.getLogger("TomcatMCA.Sweep")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'leads', 'tomcat_mca.db')
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Accept": "application/json"}

# Known MCA lender name patterns — these are NOT equipment/bank/real estate lenders
MCA_LENDER_PATTERNS = [
    "funding", "advance", "capital group", "capital llc", "merchant",
    "factor", "receivable", "mca", "cash advance", "funder",
]

# Exclude traditional banks, equipment lessors, and non-MCA entities
EXCLUDE_PATTERNS = [
    "WELLS FARGO", "JPMORGAN", "BANK OF AMERICA", "CITIZENS BANK", "WEBSTER BANK",
    "TD BANK", "US BANK", "U.S. BANK", "PNC BANK", "REGIONS BANK", "M&T BANK",
    "PEOPLES UNITED", "KEYBANK", "FIFTH THIRD", "SANTANDER", "ION BANK",
    "CATERPILLAR", "DEERE", "KUBOTA", "KOMATSU", "TOYOTA", "DELL", "NISSAN",
    "CISCO", "IBM", "XEROX", "PITNEY", "DE LAGE", "SIEMENS", "MARLIN",
    "SHEFFIELD", "NAVITAS", "CRESTMARK", "SAVINGS BANK", "CREDIT UNION",
    "MUTUAL", "COOPERATIVE", "INSURANCE FUND", "TRUST COMPANY",
    "MORTGAGE", "REAL ESTATE", "REALTY", "PROPERTY", "HOUSING",
    "SECRETARY OF", "DEPARTMENT OF", "STATE OF", "UNITED STATES",
    "INTERNAL REVENUE", "TAX",
]

# Explicitly include these top MCA lenders regardless of pattern match
FORCE_INCLUDE = [
    "EQUITY BASED CAPITAL", "EASTERN FUNDING", "LCF GROUP", "NEWTEK",
    "CFG MERCHANT", "CREDIBLY", "RETAIL CAPITAL", "WAVE ADVANCE", "NU-KO",
    "ACV CAPITAL", "CLOUDFUND", "CLICK CAPITAL", "INTECH FUNDING",
    "BROADVIEW CAPITAL", "IFS FUNDING", "BARCLAYS", "OAK STREET",
    "FORWARD FINANCING", "LAST CHANCE", "CIT SMALL BUSINESS",
    "E ADVANCE", "AMERIFACTORS", "ADVANCE BUSINESS", "CENTRAL INVESTOR",
    "ZLUR", "SECURE CAPITAL", "FLASH FUNDING", "HONEST FUNDING",
    "NATIONAL FUNDING", "BITTY", "PDM CAPITAL", "INSULA CAPITAL",
    "SELLERSFUNDING", "LEAF CAPITAL", "K2 CAPITAL", "BEACON FUNDING",
    "LOAN FUNDER", "VAULT 26", "SNAP ADVANCE", "PANTHERS CAPITAL",
    "IRONHORSE", "EPIC ADVANCE", "FINPOINT", "CWCAPITAL", "ALPINE ADVANCE",
    "YELLOWSTONE", "JONES LANE", "ICB ADVANCE", "SMALL TOWN ADVANCE",
    "FRATELLO", "AMERIFI", "GREENBOX", "GREEN BOX", "PEARL CAPITAL",
    "RAPID FINANCE", "FUNDKITE", "FORA FINANCIAL", "RELIANT FUNDING",
    "ONDECK", "ON DECK", "STONE FUNDING", "MAZAL", "MEGED",
    "FOX CAPITAL", "EXPANSION CAPITAL", "LAZARUS", "DLP FUNDING",
    "LIKETY", "VADER", "SEAMLESS", "LENDINI", "ESSENTIAL CAPITAL",
    "APPFUNDING", "FDM", "EVEREST BUSINESS", "BLUE ROCK",
    "SQUARE CAPITAL", "SHOPIFY", "BLUEVINE", "FUNDBOX",
    "STAR CAPITAL", "FIRST DATA MERCHANT", "GREYSTONE FUNDING",
    "COMMERCIAL CREDIT GROUP", "DWIGHT CAPITAL", "CHESAPEAKE FUNDING",
    "CAPIFY",
]

LENDER_TIERS = {
    'EVEREST': 'C', 'EBF': 'C', 'FORWARD': 'B', 'LAST CHANCE': 'D', 'LCF': 'D',
    'MEGED': 'C', 'APPFUNDING': 'C', 'FDM': 'C', 'LENDINI': 'C',
    'ESSENTIAL': 'B', 'CFG': 'B', 'LIKETY': 'D', 'DLP': 'C',
    'BARCLAY': 'C', 'LAZARUS': 'D', 'EXPANSION': 'B', 'VADER': 'D',
    'SEAMLESS': 'B', 'FOX': 'C', 'ZLUR': 'D', 'BITTY': 'D',
    'MAZAL': 'C', 'STONE': 'C', 'BLUE ROCK': 'C',
    'YELLOWSTONE': 'C', 'GREENBOX': 'C', 'GREEN BOX': 'C', 'PEARL': 'C',
    'RAPID': 'B', 'NATIONAL': 'B', 'FUNDKITE': 'C', 'CREDIBLY': 'B',
    'ONDECK': 'A', 'ON DECK': 'A', 'FORA': 'B', 'RELIANT': 'C',
    'PAYPAL': 'A', 'SQUARE': 'A', 'SHOPIFY': 'A', 'BLUEVINE': 'A',
    'KABBAGE': 'A', 'FUNDBOX': 'A', 'NEWTEK': 'B',
    'EQUITY BASED': 'C', 'EASTERN FUNDING': 'B', 'WAVE ADVANCE': 'C',
    'CLOUDFUND': 'C', 'CLICK CAPITAL': 'C', 'INTECH': 'C', 'BROADVIEW': 'C',
    'IFS FUNDING': 'C', 'CIT SMALL': 'B', 'ADVANCE BUSINESS': 'C',
    'SNAP ADVANCE': 'C', 'FLASH FUNDING': 'C', 'HONEST FUNDING': 'C',
    'STAR CAPITAL': 'B', 'ACV CAPITAL': 'C', 'NU-KO': 'C',
}

def get_tier(name):
    u = (name or '').upper()
    for k, t in LENDER_TIERS.items():
        if k in u: return t
    return 'C'

def is_mca_lender(name):
    u = (name or '').upper()
    if any(exc in u for exc in EXCLUDE_PATTERNS):
        return False
    if any(inc in u for inc in FORCE_INCLUDE):
        return True
    return any(p in u.lower() for p in MCA_LENDER_PATTERNS)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mca_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT, dba_name TEXT, address TEXT, city TEXT, state TEXT,
            zipcode TEXT, source_state TEXT, secured_party TEXT, collateral_desc TEXT,
            filing_date TEXT, lapse_date TEXT, days_to_lapse INTEGER, file_id TEXT,
            stack_depth INTEGER DEFAULT 1, position_number INTEGER DEFAULT 1,
            est_advance_amount REAL, est_daily_payment REAL, funder_tier TEXT,
            phone TEXT, email TEXT, contact_name TEXT, company_website TEXT,
            industry TEXT, est_annual_revenue REAL, signals_json TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')),
            paydex_score INTEGER
        )
    """)
    conn.commit()
    conn.close()

def save_lead(lead):
    conn = sqlite3.connect(DB_PATH)
    try:
        existing = conn.execute(
            "SELECT id FROM mca_leads WHERE company_name=? AND secured_party=? AND source_state=?",
            [lead['company_name'], lead['secured_party'], lead['source_state']]
        ).fetchone()
        if existing: return False
        conn.execute("""
            INSERT INTO mca_leads (
                company_name, address, city, state, zipcode, source_state,
                secured_party, collateral_desc, filing_date, lapse_date, days_to_lapse,
                file_id, funder_tier, signals_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            lead['company_name'], lead.get('address', ''), lead.get('city', ''),
            lead.get('state', ''), lead.get('zipcode', ''), lead['source_state'],
            lead['secured_party'], lead.get('collateral', ''),
            lead.get('filing_date', ''), lead.get('lapse_date', ''),
            lead.get('days_to_lapse'), lead.get('file_id', ''),
            lead.get('funder_tier', 'C'),
            json.dumps([{"type": "S1_UCC", "label": "UCC Filing Confirmed",
                        "detail": f"Blanket lien filed by {lead['secured_party']}"}])
        ])
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"DB error: {e}")
        return False
    finally:
        conn.close()


# ── CONNECTICUT BULK SWEEP ───────────────────────────────────────────────────

CT_URL = "https://data.ct.gov/resource/xfev-8smz.json"

def sweep_connecticut():
    """Pull ALL active MCA UCC filings from Connecticut."""
    logger.info("=== CONNECTICUT FULL SWEEP ===")
    total_leads = []
    total_new = 0
    offset = 0
    page_size = 2000

    while True:
        try:
            params = {
                "$where": "lien_status='Active'",
                "$select": "id_lien_flng_nbr,debtor_nm_bus,debtor_ad_str1,debtor_ad_city,debtor_ad_state,debtor_ad_zip,sec_party_nm_bus,dt_lapse,dt_accept",
                "$limit": page_size,
                "$offset": offset,
                "$order": "dt_lapse ASC",
            }
            r = requests.get(CT_URL, headers=HEADERS, params=params, timeout=30)
            if not r.ok:
                logger.warning(f"CT page at offset {offset} failed: {r.status_code}")
                break

            records = r.json()
            if not records:
                break

            batch_mca = 0
            for rec in records:
                lender = (rec.get('sec_party_nm_bus') or '').strip()
                company = (rec.get('debtor_nm_bus') or '').strip()

                if not company or not lender:
                    continue

                if not is_mca_lender(lender):
                    continue

                lapse_str = rec.get('dt_lapse', '')
                days_to_lapse = None
                if lapse_str:
                    try:
                        days_to_lapse = (datetime.fromisoformat(lapse_str[:10]) - datetime.now()).days
                    except: pass

                lead = {
                    'source_state': 'Connecticut', 'file_id': rec.get('id_lien_flng_nbr', ''),
                    'company_name': company,
                    'address': (rec.get('debtor_ad_str1') or '').strip(),
                    'city': (rec.get('debtor_ad_city') or '').strip(),
                    'state': (rec.get('debtor_ad_state') or 'CT').strip(),
                    'zipcode': (rec.get('debtor_ad_zip') or '').strip(),
                    'secured_party': lender,
                    'collateral': 'MCA — Future receivables and all proceeds',
                    'filing_date': str(rec.get('dt_accept', ''))[:10],
                    'lapse_date': lapse_str[:10],
                    'days_to_lapse': days_to_lapse,
                    'funder_tier': get_tier(lender),
                }
                total_leads.append(lead)
                if save_lead(lead):
                    total_new += 1
                    batch_mca += 1

            logger.info(f"  CT offset {offset}: {len(records)} records, {batch_mca} new MCA leads")

            if len(records) < page_size:
                break
            offset += page_size
            time.sleep(0.5)

        except Exception as e:
            logger.error(f"CT sweep error at offset {offset}: {e}")
            break

    logger.info(f"CT SWEEP: {len(total_leads)} MCA filings found, {total_new} new")
    return total_leads, total_new


# ── COLORADO BULK SWEEP ──────────────────────────────────────────────────────

CO_SECURED_URL = "https://data.colorado.gov/resource/ap62-sav4.json"
CO_FILING_URL = "https://data.colorado.gov/resource/wffy-3uut.json"
CO_DEBTOR_URL = "https://data.colorado.gov/resource/8upq-58vz.json"

def sweep_colorado():
    """Pull MCA UCC filings from Colorado via secured party search."""
    logger.info("=== COLORADO FULL SWEEP ===")
    total_leads = []
    total_new = 0

    # Get ALL secured party records that match MCA patterns
    offset = 0
    all_secured = []
    while True:
        try:
            params = {
                "$select": "fileid,organizationname",
                "$limit": 5000,
                "$offset": offset,
            }
            r = requests.get(CO_SECURED_URL, headers=HEADERS, params=params, timeout=30)
            if not r.ok:
                logger.warning(f"CO secured offset {offset} failed: {r.status_code}")
                time.sleep(5)
                continue

            records = r.json()
            if not records:
                break

            for rec in records:
                name = (rec.get('organizationname') or '').strip()
                if is_mca_lender(name):
                    all_secured.append(rec)

            logger.info(f"  CO offset {offset}: {len(records)} total, {len(all_secured)} MCA so far")

            if len(records) < 5000:
                break
            offset += 5000
            time.sleep(1)

        except Exception as e:
            logger.error(f"CO secured sweep error: {e}")
            time.sleep(5)
            continue

    logger.info(f"CO: Found {len(all_secured)} MCA secured party records")

    # Get unique file IDs
    file_ids = list(set(str(rec.get('fileid', '')) for rec in all_secured if rec.get('fileid')))
    lender_map = {str(rec['fileid']): rec.get('organizationname', '') for rec in all_secured}
    logger.info(f"CO: {len(file_ids)} unique file IDs to process")

    # Batch lookup debtors
    debtor_map = {}
    for i in range(0, len(file_ids), 50):
        batch = file_ids[i:i+50]
        id_list = ",".join(f"'{fid}'" for fid in batch)
        for attempt in range(3):
            try:
                r = requests.get(CO_DEBTOR_URL, headers=HEADERS, params={
                    "$where": f"fileid in ({id_list})",
                    "$select": "fileid,organizationname,address1,city,state,zipcode",
                    "$limit": 200
                }, timeout=30)
                if r.ok:
                    for rec in r.json():
                        fid = str(rec.get('fileid', ''))
                        if rec.get('organizationname'):
                            debtor_map[fid] = rec
                    break
                time.sleep(3)
            except:
                time.sleep(5)
        if i % 200 == 0:
            logger.info(f"  CO debtors: {i}/{len(file_ids)} processed, {len(debtor_map)} found")
        time.sleep(0.3)

    # Batch lookup filing dates
    filing_map = {}
    for i in range(0, len(file_ids), 50):
        batch = file_ids[i:i+50]
        id_list = ",".join(f"'{fid}'" for fid in batch)
        for attempt in range(3):
            try:
                r = requests.get(CO_FILING_URL, headers=HEADERS, params={
                    "$where": f"fileid in ({id_list})",
                    "$select": "fileid,filingdate,lapsedate",
                    "$limit": 200
                }, timeout=30)
                if r.ok:
                    for rec in r.json():
                        filing_map[str(rec.get('fileid', ''))] = rec
                    break
                time.sleep(3)
            except:
                time.sleep(5)
        if i % 200 == 0:
            logger.info(f"  CO filings: {i}/{len(file_ids)} processed")
        time.sleep(0.3)

    # Build leads
    for fid in file_ids:
        debtor = debtor_map.get(fid)
        filing = filing_map.get(fid)
        if not debtor or not debtor.get('organizationname'):
            continue

        lapse_str = filing.get('lapsedate', '') if filing else ''
        days_to_lapse = None
        if lapse_str:
            try:
                days_to_lapse = (datetime.fromisoformat(lapse_str[:10]) - datetime.now()).days
            except: pass

        lender_name = lender_map.get(fid, '')
        lead = {
            'source_state': 'Colorado', 'file_id': fid,
            'company_name': debtor.get('organizationname', '').strip(),
            'address': debtor.get('address1', '').strip(),
            'city': debtor.get('city', '').strip(),
            'state': debtor.get('state', 'CO').strip(),
            'zipcode': debtor.get('zipcode', '').strip(),
            'secured_party': lender_name,
            'collateral': 'MCA — Future receivables and all proceeds',
            'filing_date': (filing.get('filingdate', '') if filing else '')[:10],
            'lapse_date': lapse_str[:10],
            'days_to_lapse': days_to_lapse,
            'funder_tier': get_tier(lender_name),
        }
        total_leads.append(lead)
        if save_lead(lead):
            total_new += 1

    logger.info(f"CO SWEEP: {len(total_leads)} MCA filings found, {total_new} new")
    return total_leads, total_new


# ── MAIN ─────────────────────────────────────────────────────────────────────

def run_full_sweep():
    init_db()

    print("╔═══════════════════════════════════════════════════════════╗")
    print("║  TOMCAT MCA — FULL DATABASE SWEEP                         ║")
    print("║  Pulling ALL MCA lender UCC filings from CT + CO           ║")
    print("╚═══════════════════════════════════════════════════════════╝")

    ct_leads, ct_new = sweep_connecticut()
    print(f"\n  CT: {len(ct_leads)} filings found, {ct_new} new")

    co_leads, co_new = sweep_colorado()
    print(f"\n  CO: {len(co_leads)} filings found, {co_new} new")

    # Final stats
    conn = sqlite3.connect(DB_PATH)
    total_db = conn.execute("SELECT COUNT(*) FROM mca_leads").fetchone()[0]
    by_state = conn.execute("SELECT source_state, COUNT(*) FROM mca_leads GROUP BY source_state ORDER BY COUNT(*) DESC").fetchall()
    top_lenders = conn.execute("""
        SELECT secured_party, COUNT(*) as cnt 
        FROM mca_leads GROUP BY secured_party ORDER BY cnt DESC LIMIT 20
    """).fetchall()
    hot = conn.execute("SELECT COUNT(*) FROM mca_leads WHERE (days_to_lapse <= 30 AND days_to_lapse >= -365) OR days_to_lapse IS NULL").fetchone()[0]
    conn.close()

    print(f"\n{'='*60}")
    print(f"  FULL SWEEP COMPLETE")
    print(f"{'='*60}")
    print(f"  Total in MCA DB  : {total_db}")
    print(f"  Hot leads        : {hot}")
    print(f"\n  BY STATE:")
    for s, c in by_state:
        print(f"    {s:20s}: {c}")
    print(f"\n  TOP LENDERS:")
    for name, cnt in top_lenders:
        print(f"    {cnt:>5}  {name}")

    # Save log
    log_path = os.path.join(BASE_DIR, 'logs', f"full_sweep_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json")
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)
    with open(log_path, 'w') as f:
        json.dump({
            'timestamp': datetime.now().isoformat(),
            'ct_found': len(ct_leads), 'ct_new': ct_new,
            'co_found': len(co_leads), 'co_new': co_new,
            'total_db': total_db,
        }, f, indent=2)

    return total_db


if __name__ == '__main__':
    run_full_sweep()

"""
Tomcat MCA — UCC Lender Search Engine (Multi-State)
Searches state UCC databases for filings by specific MCA C/D tier lenders.

States covered:
  - Connecticut (Socrata API — data.ct.gov)
  - Colorado (Socrata API — data.colorado.gov) 
  - Florida (REST API — publicsearchapi.floridaucc.com)
  - California (Socrata API — data.ca.gov)

These are REAL UCC-1 filings where the secured party matches one of the MCA lenders
from the direct lender's list.
"""

import os, json, time, sqlite3, logging, requests
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s [MCA-UCC] %(levelname)s - %(message)s')
logger = logging.getLogger("TomcatMCA.UCC")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'leads', 'tomcat_mca.db')

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)", "Accept": "application/json"}
FL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Origin": "https://floridaucc.com", "Referer": "https://floridaucc.com/search",
    "Accept": "application/json, text/plain, */*",
}

# ── TARGET MCA LENDERS ───────────────────────────────────────────────────────
MCA_LENDER_SEARCH_TERMS = [
    "Everest Business Funding", "Forward Financing", "Last Chance Funding",
    "LCF Group", "Meged", "AppFundingBeta", "FDM Capital", "Lendini",
    "Essential Capital", "CFG Merchant", "Likety", "DLP Funding",
    "Barclay", "Lazarus", "Expansion Capital", "Vader",
    "Seamless", "Fox Capital", "Zlur", "Bitty Advance",
    "Mazal", "Stone Funding", "Blue Rock",
    "Yellowstone", "Greenbox", "Pearl Capital", "Rapid Finance",
    "National Funding", "Fundkite", "Credibly", "OnDeck",
    "Fora Financial", "Reliant Funding",
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
    'ONDECK': 'A', 'FORA': 'B', 'RELIANT': 'C', 'BUSINESS CAPITAL': 'C',
}

def get_tier(lender_name):
    lu = (lender_name or '').upper()
    for key, tier in LENDER_TIERS.items():
        if key in lu:
            return tier
    return 'C'

# ── DATABASE ─────────────────────────────────────────────────────────────────

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

def save_mca_lead(lead):
    conn = sqlite3.connect(DB_PATH)
    try:
        existing = conn.execute(
            "SELECT id FROM mca_leads WHERE company_name=? AND secured_party=? AND source_state=?",
            [lead['company_name'], lead['secured_party'], lead['source_state']]
        ).fetchone()
        if existing:
            return False
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
        logger.error(f"DB insert error: {e}")
        return False
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# STATE SCRAPERS
# ══════════════════════════════════════════════════════════════════════════════

# ── CONNECTICUT (Socrata) ────────────────────────────────────────────────────

CT_URL = "https://data.ct.gov/resource/xfev-8smz.json"

def search_connecticut_by_lender(lender_term):
    leads = []
    logger.info(f"  [CT] Searching for: '{lender_term}'")
    try:
        params = {
            "$where": f"upper(sec_party_nm_bus) like upper('%{lender_term}%') AND lien_status='Active'",
            "$select": "id_lien_flng_nbr,debtor_nm_bus,debtor_ad_str1,debtor_ad_city,debtor_ad_state,debtor_ad_zip,sec_party_nm_bus,dt_lapse,dt_accept",
            "$limit": 2000, "$order": "dt_lapse ASC",
        }
        r = requests.get(CT_URL, headers=HEADERS, params=params, timeout=30)
        if not r.ok:
            logger.warning(f"  CT failed: {r.status_code}")
            return leads
        records = r.json()
        if not records:
            return leads
        logger.info(f"  [CT] Found {len(records)} filings for '{lender_term}'")
        for rec in records:
            company = (rec.get('debtor_nm_bus') or '').strip()
            lender = (rec.get('sec_party_nm_bus') or '').strip()
            if not company: continue
            lapse_str = rec.get('dt_lapse', '')
            days_to_lapse = None
            if lapse_str:
                try:
                    days_to_lapse = (datetime.fromisoformat(lapse_str[:10]) - datetime.now()).days
                except: pass
            leads.append({
                'source_state': 'Connecticut', 'file_id': rec.get('id_lien_flng_nbr', ''),
                'company_name': company, 'address': (rec.get('debtor_ad_str1') or '').strip(),
                'city': (rec.get('debtor_ad_city') or '').strip(),
                'state': (rec.get('debtor_ad_state') or 'CT').strip(),
                'zipcode': (rec.get('debtor_ad_zip') or '').strip(),
                'secured_party': lender, 'collateral': 'MCA — Future receivables and all proceeds thereof',
                'filing_date': str(rec.get('dt_accept', ''))[:10], 'lapse_date': lapse_str[:10],
                'days_to_lapse': days_to_lapse, 'funder_tier': get_tier(lender),
            })
    except Exception as e:
        logger.error(f"  CT error for '{lender_term}': {e}")
    return leads


# ── COLORADO (Socrata — with retry + longer timeout) ─────────────────────────

CO_SECURED_URL = "https://data.colorado.gov/resource/ap62-sav4.json"
CO_FILING_URL = "https://data.colorado.gov/resource/wffy-3uut.json"
CO_DEBTOR_URL = "https://data.colorado.gov/resource/8upq-58vz.json"

def search_colorado_by_lender(lender_term):
    leads = []
    logger.info(f"  [CO] Searching for: '{lender_term}'")
    for attempt in range(3):
        try:
            params = {
                "$where": f"upper(organizationname) like upper('%{lender_term}%')",
                "$select": "fileid,organizationname", "$limit": 500
            }
            r = requests.get(CO_SECURED_URL, headers=HEADERS, params=params, timeout=30)
            if not r.ok:
                logger.warning(f"  CO attempt {attempt+1} failed: {r.status_code}")
                time.sleep(3)
                continue
            secured_records = r.json()
            if not secured_records:
                return leads
            file_ids = list(set(str(rec.get('fileid', '')) for rec in secured_records if rec.get('fileid')))
            lender_map = {str(rec['fileid']): rec.get('organizationname', lender_term) for rec in secured_records}
            logger.info(f"  [CO] Found {len(file_ids)} filings for '{lender_term}'")
            if not file_ids: return leads

            # Get debtor info
            debtor_map = {}
            for i in range(0, len(file_ids), 50):
                batch = file_ids[i:i+50]
                id_list = ",".join(f"'{fid}'" for fid in batch)
                try:
                    r2 = requests.get(CO_DEBTOR_URL, headers=HEADERS, params={
                        "$where": f"fileid in ({id_list})",
                        "$select": "fileid,organizationname,address1,city,state,zipcode", "$limit": 200
                    }, timeout=30)
                    if r2.ok:
                        for rec in r2.json():
                            fid = str(rec.get('fileid', ''))
                            if rec.get('organizationname'):
                                debtor_map[fid] = rec
                    time.sleep(0.5)
                except: pass

            # Get filing/lapse dates
            filing_map = {}
            for i in range(0, len(file_ids), 50):
                batch = file_ids[i:i+50]
                id_list = ",".join(f"'{fid}'" for fid in batch)
                try:
                    r3 = requests.get(CO_FILING_URL, headers=HEADERS, params={
                        "$where": f"fileid in ({id_list}) AND terminationflag = false",
                        "$select": "fileid,filingdate,lapsedate", "$limit": 200
                    }, timeout=30)
                    if r3.ok:
                        for rec in r3.json():
                            filing_map[str(rec.get('fileid', ''))] = rec
                    time.sleep(0.5)
                except: pass

            for fid in file_ids:
                debtor = debtor_map.get(fid)
                filing = filing_map.get(fid)
                if not debtor or not debtor.get('organizationname'): continue
                lapse_str = filing.get('lapsedate', '') if filing else ''
                days_to_lapse = None
                if lapse_str:
                    try: days_to_lapse = (datetime.fromisoformat(lapse_str[:10]) - datetime.now()).days
                    except: pass
                leads.append({
                    'source_state': 'Colorado', 'file_id': fid,
                    'company_name': debtor.get('organizationname', '').strip(),
                    'address': debtor.get('address1', '').strip(),
                    'city': debtor.get('city', '').strip(),
                    'state': debtor.get('state', 'CO').strip(),
                    'zipcode': debtor.get('zipcode', '').strip(),
                    'secured_party': lender_map.get(fid, lender_term),
                    'collateral': 'Future receivables and all proceeds thereof',
                    'filing_date': (filing.get('filingdate', '') if filing else '')[:10],
                    'lapse_date': lapse_str[:10], 'days_to_lapse': days_to_lapse,
                    'funder_tier': get_tier(lender_map.get(fid, lender_term)),
                })
            break  # success — exit retry loop
        except Exception as e:
            logger.error(f"  CO attempt {attempt+1} error: {e}")
            time.sleep(5)
    return leads


# ── FLORIDA (REST API — search by secured party name) ────────────────────────

FL_API = "https://publicsearchapi.floridaucc.com"

def search_florida_by_lender(lender_term):
    leads = []
    logger.info(f"  [FL] Searching for: '{lender_term}'")
    try:
        # FL API searches by secured party name
        params = {
            "text": lender_term,
            "searchOptionType": "OrganizationSecuredPartyName",
            "searchOptionSubOption": "FiledCompactSecuredPartyNameList",
            "searchCategory": "Standard",
            "pageNumber": 1, "pageSize": 100,
        }
        r = requests.get(f"{FL_API}/search", headers=FL_HEADERS, params=params, timeout=20)
        if not r.ok:
            logger.warning(f"  FL search failed: {r.status_code}")
            return leads

        payload = r.json().get('payload', {})
        secured_parties = payload.get('securedParties', payload.get('debtors', []))
        if not secured_parties:
            logger.info(f"  [FL] No results for '{lender_term}'")
            return leads

        logger.info(f"  [FL] Found {len(secured_parties)} secured party matches for '{lender_term}'")

        # For each secured party match, get their filings (which have debtor info)
        for sp in secured_parties[:50]:  # cap at 50 to avoid rate limits
            sp_id = sp.get('securedPartyId') or sp.get('debtorId') or sp.get('id', '')
            sp_name = (sp.get('organizationName') or sp.get('name') or lender_term).strip()

            if not sp_id:
                continue

            # Get filings for this secured party
            try:
                r2 = requests.get(f"{FL_API}/securedparties/{sp_id}/filings", headers=FL_HEADERS, timeout=15)
                if not r2.ok:
                    # Try alternate endpoint
                    r2 = requests.get(f"{FL_API}/debtors/{sp_id}/filings", headers=FL_HEADERS, timeout=15)
                if not r2.ok:
                    continue
                filings = r2.json().get('payload', [])
                time.sleep(0.2)
            except Exception as e:
                logger.error(f"  FL filing fetch error: {e}")
                continue

            for filing in filings:
                file_num = filing.get('fileNumber') or filing.get('documentNumber') or ''
                lapse_str = filing.get('lapseDate') or filing.get('expirationDate') or ''
                filing_date = filing.get('filedDate') or filing.get('filingDate') or ''

                # Get debtor info from filing
                debtors = filing.get('debtors', [])
                for debtor in debtors:
                    company = (debtor.get('organizationName') or debtor.get('name') or '').strip()
                    if not company:
                        continue

                    days_to_lapse = None
                    if lapse_str:
                        try:
                            days_to_lapse = (datetime.fromisoformat(lapse_str[:10]) - datetime.now()).days
                        except: pass

                    leads.append({
                        'source_state': 'Florida', 'file_id': str(file_num),
                        'company_name': company,
                        'address': (debtor.get('address1') or debtor.get('address') or '').strip(),
                        'city': (debtor.get('city') or '').strip(),
                        'state': (debtor.get('state') or 'FL').strip(),
                        'zipcode': (debtor.get('postalCode') or debtor.get('zip') or '').strip(),
                        'secured_party': sp_name,
                        'collateral': 'MCA — All assets and future receivables',
                        'filing_date': str(filing_date)[:10], 'lapse_date': str(lapse_str)[:10],
                        'days_to_lapse': days_to_lapse, 'funder_tier': get_tier(sp_name),
                    })

    except Exception as e:
        logger.error(f"  FL error for '{lender_term}': {e}")
    return leads


# ── CALIFORNIA (Socrata — data.ca.gov) ───────────────────────────────────────
# CA publishes UCC data via bizfile API — we try the Socrata endpoint

CA_URL = "https://data.ca.gov/api/3/action/datastore_search"
# CA business filings dataset
CA_UCC_URL = "https://data.ca.gov/api/3/action/datastore_search"

def search_california_by_lender(lender_term):
    """Search California for UCC filings via bizfile."""
    leads = []
    logger.info(f"  [CA] Searching for: '{lender_term}'")
    try:
        # CA bizfile public search
        url = f"https://bizfileonline.sos.ca.gov/api/Records/SearchResults"
        params = {
            "searchValue": lender_term,
            "searchType": "UCC",
            "searchCriteria": "SecuredParty",
        }
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        if r.ok:
            try:
                data = r.json()
                if isinstance(data, list):
                    logger.info(f"  [CA] Found {len(data)} results for '{lender_term}'")
                    for rec in data[:200]:
                        company = (rec.get('debtorName') or rec.get('entityName') or '').strip()
                        if not company: continue
                        lapse_str = rec.get('lapseDate') or rec.get('expirationDate') or ''
                        days_to_lapse = None
                        if lapse_str:
                            try: days_to_lapse = (datetime.fromisoformat(lapse_str[:10]) - datetime.now()).days
                            except: pass
                        leads.append({
                            'source_state': 'California', 'file_id': rec.get('fileNumber', ''),
                            'company_name': company,
                            'address': (rec.get('address') or '').strip(),
                            'city': (rec.get('city') or '').strip(),
                            'state': 'CA', 'zipcode': (rec.get('zip') or '').strip(),
                            'secured_party': rec.get('securedPartyName') or lender_term,
                            'collateral': 'MCA — All assets and future receivables',
                            'filing_date': str(rec.get('filingDate') or '')[:10],
                            'lapse_date': str(lapse_str)[:10], 'days_to_lapse': days_to_lapse,
                            'funder_tier': get_tier(rec.get('securedPartyName') or lender_term),
                        })
                else:
                    logger.info(f"  [CA] No usable results for '{lender_term}'")
            except:
                logger.info(f"  [CA] Could not parse response for '{lender_term}'")
        else:
            logger.warning(f"  CA search returned {r.status_code}")
    except Exception as e:
        logger.error(f"  CA error for '{lender_term}': {e}")
    return leads


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def run_mca_ucc_search():
    init_db()

    states = "CT + CO + FL + CA"
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║  TOMCAT MCA — MULTI-STATE UCC LENDER SEARCH               ║")
    print(f"║  {len(MCA_LENDER_SEARCH_TERMS)} lenders × 4 states ({states})                ║")
    print("╚═══════════════════════════════════════════════════════════╝")

    total_found = 0
    total_new = 0
    state_totals = {'Connecticut': 0, 'Colorado': 0, 'Florida': 0, 'California': 0}
    lender_results = {}

    for term in MCA_LENDER_SEARCH_TERMS:
        print(f"\n{'─'*55}")
        print(f"🔍 {term}")

        ct_leads = [] # search_connecticut_by_lender(term)
        time.sleep(0.1)

        co_leads = [] # search_colorado_by_lender(term)
        time.sleep(0.1)

        fl_leads = [] # search_florida_by_lender(term)
        time.sleep(0.1)

        ca_leads = search_california_by_lender(term)
        time.sleep(0.5)

        all_leads = ct_leads + co_leads + fl_leads + ca_leads
        new_count = 0

        for lead in all_leads:
            if save_mca_lead(lead):
                new_count += 1
                state_totals[lead['source_state']] = state_totals.get(lead['source_state'], 0) + 1

        total_found += len(all_leads)
        total_new += new_count
        lender_results[term] = {
            'found': len(all_leads), 'new': new_count,
            'by_state': {'CT': len(ct_leads), 'CO': len(co_leads), 'FL': len(fl_leads), 'CA': len(ca_leads)}
        }

        if all_leads:
            ct_n = f"CT:{len(ct_leads)}" if ct_leads else ""
            co_n = f"CO:{len(co_leads)}" if co_leads else ""
            fl_n = f"FL:{len(fl_leads)}" if fl_leads else ""
            ca_n = f"CA:{len(ca_leads)}" if ca_leads else ""
            breakdown = " | ".join(filter(None, [ct_n, co_n, fl_n, ca_n]))
            print(f"   ✅ {len(all_leads)} found ({new_count} new) [{breakdown}]")
        else:
            print(f"   ⚫ No filings found")

    # Summary
    conn = sqlite3.connect(DB_PATH)
    total_db = conn.execute("SELECT COUNT(*) FROM mca_leads").fetchone()[0]
    hot_db = conn.execute("SELECT COUNT(*) FROM mca_leads WHERE days_to_lapse >= 0 AND days_to_lapse <= 30").fetchone()[0]
    by_state = conn.execute("SELECT source_state, COUNT(*) FROM mca_leads GROUP BY source_state ORDER BY COUNT(*) DESC").fetchall()
    conn.close()

    print(f"\n{'='*60}")
    print(f"  MULTI-STATE MCA UCC SEARCH COMPLETE")
    print(f"{'='*60}")
    print(f"  Total filings found : {total_found}")
    print(f"  New leads added     : {total_new}")
    print(f"  Total in MCA DB     : {total_db}")
    print(f"  Hot leads (≤30d)    : {hot_db}")
    print(f"\n  BY STATE:")
    for state, count in by_state:
        print(f"    {state:20s}: {count} leads")

    summary_path = os.path.join(BASE_DIR, 'logs', f"mca_ucc_search_{datetime.now().strftime('%Y-%m-%d_%H%M')}.json")
    os.makedirs(os.path.join(BASE_DIR, 'logs'), exist_ok=True)
    with open(summary_path, 'w') as f:
        json.dump({
            'timestamp': datetime.now().isoformat(),
            'total_found': total_found, 'total_new': total_new,
            'state_totals': state_totals, 'lender_results': lender_results
        }, f, indent=2)
    print(f"  Results log: {summary_path}")

    return total_found, total_new


if __name__ == '__main__':
    run_mca_ucc_search()

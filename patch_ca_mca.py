import re

with open('ca_mca_scraper.py', 'r') as f:
    content = f.read()

# 1. Change DB path
content = content.replace("os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')", "os.path.join(BASE_DIR, 'leads', 'tomcat_mca.db')")
content = content.replace("DB_PATH = os.path.join(BASE_DIR, 'leads', 'tomcat_capex.db')", "DB_PATH = os.path.join(BASE_DIR, 'leads', 'tomcat_mca.db')")

# 2. Change the Lenders
old_lenders = """CA_TECH_LENDERS = [
    ("DELL FINANCIAL SERVICES", "IT_OEM"),
    ("HEWLETT PACKARD", "IT_OEM"),
    ("LENOVO FINANCIAL", "IT_OEM"),
    ("IBM CREDIT", "IT_OEM"),
    ("CISCO SYSTEMS CAPITAL", "IT_OEM"),
    ("XEROX FINANCIAL", "PRINT_IMAGING"),
    ("CANON FINANCIAL SERVICES", "PRINT_IMAGING"),
    ("KONICA MINOLTA", "PRINT_IMAGING"),
    ("RICOH USA", "PRINT_IMAGING"),
    ("KYOCERA DOCUMENT SOLUTIONS", "PRINT_IMAGING"),
    ("GREATAMERICA FINANCIAL", "IT_CHANNEL"),
    ("MARLIN LEASING", "IT_CHANNEL"),
    ("LEAF COMMERCIAL CAPITAL", "IT_CHANNEL"),
    ("CIT BANK", "IT_CHANNEL"),
    ("AMAZON CAPITAL SERVICES", "CLOUD_SAAS"),
]"""
new_lenders = """MCA_LENDERS = [
    ("Everest Business Funding", "C"), ("Forward Financing", "B"), ("Last Chance Funding", "D"),
    ("LCF Group", "D"), ("Meged", "C"), ("AppFundingBeta", "C"), ("FDM Capital", "C"),
    ("Lendini", "C"), ("Essential Capital", "B"), ("CFG Merchant", "B"), ("Likety", "D"),
    ("DLP Funding", "C"), ("Barclay", "C"), ("Lazarus", "D"), ("Expansion Capital", "B"),
    ("Vader", "D"), ("Seamless", "B"), ("Fox Capital", "C"), ("Zlur", "D"), ("Bitty Advance", "D"),
    ("Mazal", "C"), ("Stone Funding", "C"), ("Blue Rock", "C"), ("Yellowstone", "C"),
    ("Greenbox", "C"), ("Pearl Capital", "C"), ("Rapid Finance", "B"), ("National Funding", "B"),
    ("Fundkite", "C"), ("Credibly", "B"), ("OnDeck", "A"), ("Fora Financial", "B"), ("Reliant Funding", "C"),
]"""
content = content.replace(old_lenders, new_lenders)
content = content.replace("def run_ca_scraper(lenders=CA_TECH_LENDERS, date_range_months=3):", "def run_ca_scraper(lenders=MCA_LENDERS, date_range_months=12):")

# 3. Change DB Save Logic
old_save = """def save_lead(lead: dict) -> bool:
    \"\"\"Insert lead into DB. Returns True if new.\"\"\"
    conn = sqlite3.connect(DB_PATH)
    try:
        # Ensure tech columns exist
        for col in ["tech_company TEXT", "tech_category TEXT", "tech_reason TEXT"]:
            try:
                conn.execute(f"ALTER TABLE ucc_leads ADD COLUMN {col}")
            except:
                pass

        existing = conn.execute(
            "SELECT id FROM ucc_leads WHERE company_name=? AND secured_party=? AND source_state=?",
            [lead['company_name'], lead['secured_party'], lead['source_state']]
        ).fetchone()

        if existing:
            return False

        conn.execute(\"\"\"
            INSERT INTO ucc_leads (
                company_name, address, city, state, zipcode, source_state,
                secured_party, collateral, filing_date, lapse_date,
                days_to_lapse, tech_company, tech_category, tech_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        \"\"\", [
            lead['company_name'], lead.get('address', ''), lead.get('city', ''),
            lead.get('state', ''), lead.get('zipcode', ''), lead['source_state'],
            lead['secured_party'], lead.get('collateral', ''),
            lead.get('filing_date', ''), lead.get('lapse_date', ''),
            lead.get('days_to_lapse'), lead.get('tech_company', ''),
            lead.get('tech_category', ''), lead.get('tech_reason', '')
        ])
        conn.commit()
        return True
    except Exception as e:
        log.error(f"DB insert error: {e}")
        return False
    finally:
        conn.close()"""
new_save = """def save_lead(lead: dict) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        existing = conn.execute(
            "SELECT id FROM mca_leads WHERE company_name=? AND secured_party=? AND source_state=?",
            [lead['company_name'], lead['secured_party'], lead['source_state']]
        ).fetchone()

        if existing:
            return False

        conn.execute(\"\"\"
            INSERT INTO mca_leads (
                company_name, address, city, state, zipcode, source_state,
                secured_party, collateral_desc, filing_date, lapse_date,
                days_to_lapse, funder_tier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        \"\"\", [
            lead['company_name'], lead.get('address', ''), lead.get('city', ''),
            lead.get('state', ''), lead.get('zipcode', ''), lead['source_state'],
            lead['secured_party'], lead.get('collateral', ''),
            lead.get('filing_date', ''), lead.get('lapse_date', ''),
            lead.get('days_to_lapse'), lead.get('funder_tier', 'C')
        ])
        conn.commit()
        return True
    except Exception as e:
        log.error(f"DB insert error: {e}")
        return False
    finally:
        conn.close()"""
content = content.replace(old_save, new_save)

# 4. Change lead generation logic
content = re.sub(r"'tech_company':.*?,", "'funder_tier': tech_category,", content)
content = re.sub(r"'tech_category':.*?,", "", content)
content = re.sub(r"'tech_reason':.*?,", "", content)
content = re.sub(r"Equipment Financing / SaaS", "MCA — Future receivables and all proceeds thereof", content)
content = content.replace("log.info(f\"🔍 Searching CA for: {lender_name} ({tech_category})\")", "log.info(f\"🔍 Searching CA for MCA: {lender_name} (Tier {tech_category})\")")

# 5. Fix dates
content = content.replace("lapse_iso = lapse_dt.strftime(\"%Y-%m-%d\")", "lapse_iso = lapse_date")
# The ca_ucc_scraper gets exact dates from the DOM (lapse_date = cells[6].inner_text()), so it's accurate!

with open('ca_mca_scraper.py', 'w') as f:
    f.write(content)


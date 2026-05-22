"""
Tomcat Capex — California UCC Tech Scraper (Playwright)
/Users/robertle/tomcat_capex/scrapers/ca_ucc_scraper.py

Uses Playwright (headless Chrome) to scrape California's bizfile UCC portal.
The portal uses Incapsula WAF so direct API calls are blocked.

Strategy:
  1. Navigate to https://bizfileonline.sos.ca.gov/search/ucc
  2. For each tech lender, search by name
  3. Use date ranges to stay under the 1000-result limit
  4. Extract results from the DOM
  5. Save to the same SQLite DB

Run: python3 ca_ucc_scraper.py [--limit N]
"""

import os, re, sys, time, json, sqlite3, logging, argparse
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'leads', 'tomcat_mca.db')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [CA-UCC] %(levelname)s - %(message)s'
)
log = logging.getLogger("TomcatCapex.CA_UCC")

# Tech lenders to search for in CA
MCA_LENDERS = [
    ("Everest Business Funding", "C"), ("Forward Financing", "B"), ("Last Chance Funding", "D"),
    ("LCF Group", "D"), ("Meged", "C"), ("AppFundingBeta", "C"), ("FDM Capital", "C"),
    ("Lendini", "C"), ("Essential Capital", "B"), ("CFG Merchant", "B"), ("Likety", "D"),
    ("DLP Funding", "C"), ("Barclay", "C"), ("Lazarus", "D"), ("Expansion Capital", "B"),
    ("Vader", "D"), ("Seamless", "B"), ("Fox Capital", "C"), ("Zlur", "D"), ("Bitty Advance", "D"),
    ("Mazal", "C"), ("Stone Funding", "C"), ("Blue Rock", "C"), ("Yellowstone", "C"),
    ("Greenbox", "C"), ("Pearl Capital", "C"), ("Rapid Finance", "B"), ("National Funding", "B"),
    ("Fundkite", "C"), ("Credibly", "B"), ("OnDeck", "A"), ("Fora Financial", "B"), ("Reliant Funding", "C"),
]


def save_lead(lead: dict) -> bool:
    """Insert lead into DB. Returns True if new."""
    conn = sqlite3.connect(DB_PATH)
    try:
        # Ensure tech columns exist
        for col in ["tech_company TEXT", "tech_category TEXT", "tech_reason TEXT"]:
            try:
                conn.execute(f"ALTER TABLE ucc_leads ADD COLUMN {col}")
            except:
                pass

        conn.execute("""
            INSERT INTO mca_leads (
                company_name, address, city, state, zipcode, source_state,
                secured_party, collateral_desc, filing_date, lapse_date,
                days_to_lapse, funder_tier
            ) VALUES (:company_name, :address, :city, :state, :zipcode, :source_state,
                    :secured_party, :collateral, :filing_date, :lapse_date,
                    :days_to_lapse, :tech_category)
        """, lead)
        inserted = conn.total_changes > 0
        conn.commit()
        return inserted
    except Exception as e:
        log.error(f"DB error: {e}")
        return False
    finally:
        conn.close()


def parse_location(location_str: str) -> dict:
    """Parse 'CITY, ST' format into city and state."""
    parts = location_str.strip().rsplit(",", 1)
    if len(parts) == 2:
        return {"city": parts[0].strip(), "state": parts[1].strip()}
    return {"city": location_str.strip(), "state": "CA"}


def scrape_ca_tech_uccs(lenders=None, date_range_months=6):
    """
    Scrape California UCC filings for tech lenders using Playwright.
    Uses date-windowed searches to handle the 1000-result limit.
    """
    from playwright.sync_api import sync_playwright

    if lenders is None:
        lenders = MCA_LENDERS

    total_new = 0
    total_found = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        # Navigate and let Incapsula resolve
        log.info("Navigating to CA bizfile UCC search...")
        page.goto("https://bizfileonline.sos.ca.gov/search/ucc",
                   wait_until="domcontentloaded", timeout=60000)
        time.sleep(8)  # Let Incapsula JS challenge resolve
        
        # Verify page loaded
        try:
            page.wait_for_selector("input[type='text']", timeout=15000)
            log.info("✅ Page loaded successfully")
        except:
            log.error("❌ Page did not load — Incapsula may have blocked us")
            browser.close()
            return 0

        for lender_name, tech_category in lenders:
            log.info(f"\n{'─'*55}")
            log.info(f"🔍 Searching CA for MCA: {lender_name} (Tier {tech_category})")

            # Generate date windows (3-month chunks going back 5 years)
            now = datetime.now()
            windows = []
            for i in range(0, 60, date_range_months):
                start = now - timedelta(days=30 * (i + date_range_months))
                end = now - timedelta(days=30 * i)
                windows.append((start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")))

            lender_total = 0
            lender_new = 0

            for start_date, end_date in windows:
                try:
                    # Clear and fill search input
                    search_input = page.locator("input[type='text']").first
                    search_input.fill("")
                    search_input.fill(lender_name)

                    # Expand advanced search if not visible
                    advanced_btn = page.locator("text=Advanced")
                    if advanced_btn.count() > 0:
                        try:
                            advanced_btn.first.click()
                            time.sleep(0.5)
                        except:
                            pass

                    # Set status to Active
                    try:
                        status_select = page.locator("select").first
                        status_select.select_option("Active")
                    except:
                        pass

                    # Set file date range
                    date_inputs = page.locator("input[placeholder='MM/DD/YYYY']")
                    if date_inputs.count() >= 2:
                        date_inputs.nth(0).fill(start_date)
                        date_inputs.nth(1).fill(end_date)

                    # Click search
                    search_btn = page.locator("button:has-text('Search')").first
                    search_btn.click()
                    time.sleep(4)  # Wait for results

                    # Wait for results table
                    try:
                        page.wait_for_selector("table", timeout=10000)
                    except:
                        log.debug(f"  No results table for {start_date}-{end_date}")
                        continue

                    # Extract results from table
                    rows = page.locator("table tbody tr").all()
                    batch_count = len(rows)

                    if batch_count == 0:
                        continue

                    log.info(f"  {start_date}-{end_date}: {batch_count} results")

                    for row in rows:
                        cells = row.locator("td").all()
                        if len(cells) < 7:
                            continue

                        try:
                            ucc_type = cells[0].inner_text().strip()
                            debtor_text = cells[1].inner_text().strip()
                            file_number = cells[2].inner_text().strip()
                            secured_party = cells[3].inner_text().strip()
                            status = cells[4].inner_text().strip()
                            filing_date = cells[5].inner_text().strip()
                            lapse_date = cells[6].inner_text().strip()

                            if status != "Active":
                                continue

                            # Parse debtor info (NAME - CITY, ST)
                            debtor_parts = debtor_text.split(" - ", 1)
                            company_name = debtor_parts[0].strip()
                            location = parse_location(debtor_parts[1]) if len(debtor_parts) > 1 else {"city": "", "state": "CA"}

                            # Parse secured party info
                            sp_parts = secured_party.split(" - ", 1)
                            sp_name = sp_parts[0].strip()

                            # Calculate days to lapse
                            dtl = None
                            lapse_iso = ""
                            if lapse_date:
                                try:
                                    lapse_dt = datetime.strptime(lapse_date, "%m/%d/%Y")
                                    dtl = (lapse_dt - datetime.now()).days
                                    lapse_iso = lapse_date
                                except:
                                    pass

                            filing_iso = ""
                            if filing_date:
                                try:
                                    filing_iso = datetime.strptime(filing_date, "%m/%d/%Y").strftime("%Y-%m-%d")
                                except:
                                    pass

                            lead = {
                                "id": f"CA-{file_number}",
                                "source_state": "California",
                                "file_id": file_number,
                                "company_name": company_name,
                                "address": "",
                                "city": location["city"],
                                "state": location["state"],
                                "zipcode": "",
                                "secured_party": sp_name,
                                "collateral": f"Tech Equipment ({lender_name})",
                                "filing_date": filing_iso,
                                "lapse_date": lapse_iso,
                                "days_to_lapse": dtl,
                                "tech_company": "true",
                                "tech_category": tech_category,
                                "tech_reason": f"Tech lender: {lender_name}",
                            }

                            lender_total += 1
                            if save_lead(lead):
                                lender_new += 1

                        except Exception as e:
                            log.debug(f"  Row parse error: {e}")
                            continue

                    # If we got 1000 results, need smaller windows
                    if batch_count >= 1000:
                        log.warning(f"  ⚠️ Hit 1000-result cap for {start_date}-{end_date}")

                except Exception as e:
                    log.error(f"  Search error ({start_date}-{end_date}): {e}")
                    continue

                time.sleep(1.5)  # Rate limit

            total_found += lender_total
            total_new += lender_new
            log.info(f"  CA {lender_name}: {lender_total} found, {lender_new} new")

            # Clear filters between lenders
            try:
                clear_btn = page.locator("button:has-text('Clear Filters')")
                if clear_btn.count() > 0:
                    clear_btn.first.click()
                    time.sleep(1)
            except:
                pass

        browser.close()

    log.info(f"\n{'='*55}")
    log.info(f"  California Tech UCC Scrape Complete")
    log.info(f"  Lenders searched: {len(lenders)}")
    log.info(f"  Total found:     {total_found:,}")
    log.info(f"  New leads saved: {total_new:,}")
    log.info(f"{'='*55}")

    return total_new


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="California Tech UCC Scraper")
    parser.add_argument("--limit", type=int, default=0,
                        help="Max lenders to process (0=all)")
    args = parser.parse_args()

    lenders = CA_TECH_LENDERS[:args.limit] if args.limit else None
    scrape_ca_tech_uccs(lenders=lenders)

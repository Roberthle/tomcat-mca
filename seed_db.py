"""Seed Tomcat MCA database with realistic MCA-style UCC filings."""
import sqlite3, random, json, os
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), 'leads', 'tomcat_mca.db')

# MCA lenders by tier
MCA_LENDERS = {
    'A': ['OnDeck Capital', 'Kabbage/AMEX', 'Square Capital', 'Fundbox', 'BlueVine Capital',
          'PayPal Working Capital', 'Shopify Capital', 'Amazon Lending'],
    'B': ['Rapid Finance', 'Credibly', 'National Funding', 'Fora Financial', 'Libertas Funding',
          'Forward Financing', 'Capytal', 'Expansion Capital Group', 'CFG Merchant Solutions',
          'Essential Capital Group', 'Seamless Capital'],
    'C': ['Greenbox Capital', 'Yellowstone Capital', 'Funding Circle', 'Clear Balance',
          'Fox Capital Group', 'Reliant Funding', 'Business Capital USA', 'Pearl Capital',
          'Everest Business Funding (EBF)', 'Meged Financial', 'AppFundingBeta',
          'FDM Capital (Lendini)', 'DLP Capital', "Barclay's Advance",
          'Blue Rock Capital', 'Stone Funding Group', 'Mazal Funders'],
    'D': ['Last Chance Funding (LCF)', 'Quick Capital Solutions', 'Xtreme Merchant Funding',
          'Power Fund America', 'Capital Stack Solutions', 'Likety Split Funding',
          'Lazarus Capital', 'Vader Financial', 'Zlur Funding', 'Bitty Advance']
}

MCA_COLLATERAL = [
    "All assets", "All business assets and future receivables",
    "All accounts receivable and general intangibles",
    "Future receivables and all proceeds thereof",
    "All present and future accounts, chattel paper, and payment intangibles",
    "All assets including but not limited to accounts receivable",
    "Future credit card receivables and bank deposits",
    "All inventory, equipment, accounts, and general intangibles"
]

INDUSTRIES = [
    'Restaurant', 'Auto Repair', 'Retail Store', 'Beauty Salon', 'Medical Practice',
    'Dental Office', 'Trucking', 'Construction', 'HVAC', 'Plumbing',
    'Gas Station', 'Convenience Store', 'Dry Cleaners', 'Landscaping', 'Roofing',
    'Gym/Fitness', 'Daycare', 'Car Wash', 'Pizza Shop', 'Deli/Bodega',
    'Nail Salon', 'Barbershop', 'Liquor Store', 'Laundromat', 'Moving Company',
    'Towing Service', 'Food Truck', 'Bakery', 'Pet Grooming', 'Florist'
]

# Company name patterns common in MCA
COMPANY_PREFIXES = [
    "A1", "AAA", "All Star", "American", "Best", "Big City", "Blue Sky",
    "Capital", "Central", "City", "Classic", "Coast", "Crown", "Diamond",
    "Eagle", "Elite", "Empire", "Express", "First Choice", "Five Star",
    "Gold", "Golden", "Grand", "Great", "Green", "Harbor", "Heritage",
    "Ideal", "Imperial", "Key", "Liberty", "Luxury", "Main Street",
    "Metro", "National", "New Era", "New York", "Pacific", "Park Avenue",
    "Patriot", "Peak", "Phoenix", "Pioneer", "Platinum", "Premier",
    "Prime", "Pro", "Quality", "Royal", "Silver", "Star", "Summit",
    "Sunrise", "Superior", "Supreme", "Top", "Triple A", "United", "Victory"
]

COMPANY_SUFFIXES_BY_INDUSTRY = {
    'Restaurant': ['Grill', 'Kitchen', 'Bistro', 'Eatery', 'Diner', 'Cafe'],
    'Auto Repair': ['Auto', 'Motors', 'Auto Body', 'Collision', 'Automotive'],
    'Retail Store': ['Goods', 'Mart', 'Trading', 'Supply', 'Outlet'],
    'Construction': ['Construction', 'Builders', 'Contracting', 'Development'],
    'Trucking': ['Trucking', 'Transport', 'Logistics', 'Freight', 'Hauling'],
    'HVAC': ['HVAC', 'Heating & Cooling', 'Climate Control', 'Air Systems'],
    'Plumbing': ['Plumbing', 'Plumbing & Heating', 'Pipe & Drain'],
    'Medical Practice': ['Medical Group', 'Health Associates', 'Medical Center'],
    'Dental Office': ['Dental', 'Dental Care', 'Dental Associates'],
}

STATES = {
    'New York': {'cities': ['New York', 'Brooklyn', 'Queens', 'Bronx', 'Staten Island', 'Buffalo', 'Rochester', 'Yonkers', 'Syracuse', 'Albany'], 'weight': 30},
    'Florida': {'cities': ['Miami', 'Fort Lauderdale', 'Tampa', 'Orlando', 'Jacksonville', 'Hialeah', 'St Petersburg', 'Hollywood', 'Pembroke Pines', 'Boca Raton'], 'weight': 20},
    'California': {'cities': ['Los Angeles', 'San Diego', 'San Francisco', 'Oakland', 'Sacramento', 'Fresno', 'Long Beach', 'Bakersfield', 'Anaheim', 'Riverside'], 'weight': 20},
    'Texas': {'cities': ['Houston', 'Dallas', 'San Antonio', 'Austin', 'Fort Worth', 'El Paso', 'Arlington', 'Plano', 'Laredo', 'Lubbock'], 'weight': 15},
    'New Jersey': {'cities': ['Newark', 'Jersey City', 'Paterson', 'Elizabeth', 'Edison', 'Woodbridge', 'Toms River', 'Hamilton', 'Trenton', 'Camden'], 'weight': 15},
}

def gen_company(industry):
    prefix = random.choice(COMPANY_PREFIXES)
    suffixes = COMPANY_SUFFIXES_BY_INDUSTRY.get(industry, [industry])
    suffix = random.choice(suffixes)
    entity = random.choice(['LLC', 'Inc.', 'Corp.', 'LLC', 'Inc.', 'LP', 'LLC'])
    return f"{prefix} {suffix} {entity}"

def gen_revenue(industry, tier):
    """Estimate annual revenue by industry and funder tier."""
    base = {
        'Restaurant': 600000, 'Auto Repair': 400000, 'Retail Store': 350000,
        'Construction': 900000, 'Trucking': 700000, 'Medical Practice': 1200000,
        'Dental Office': 800000, 'Gas Station': 1500000,
    }.get(industry, 500000)
    mult = {'A': 1.5, 'B': 1.0, 'C': 0.7, 'D': 0.5}.get(tier, 1.0)
    return round(base * mult * random.uniform(0.6, 1.8), -3)

def seed():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM mca_leads")

    # Build weighted state list
    state_pool = []
    for s, info in STATES.items():
        state_pool.extend([s] * info['weight'])

    leads = []
    companies_generated = {}
    num_leads = 500

    for i in range(num_leads):
        industry = random.choice(INDUSTRIES)
        state = random.choice(state_pool)
        city = random.choice(STATES[state]['cities'])
        company = gen_company(industry)

        # Some companies should appear multiple times (stacking)
        if random.random() < 0.35 and companies_generated:
            existing = random.choice(list(companies_generated.keys()))
            company = existing
            industry = companies_generated[existing]['industry']
            state = companies_generated[existing]['state']
            city = companies_generated[existing]['city']
            stack_depth = companies_generated[existing]['stack'] + 1
            companies_generated[existing]['stack'] = stack_depth
        else:
            stack_depth = 1
            companies_generated[company] = {'industry': industry, 'state': state, 'city': city, 'stack': 1}

        tier = random.choices(['A', 'B', 'C', 'D'], weights=[25, 35, 30, 10])[0]
        lender = random.choice(MCA_LENDERS[tier])
        collateral = random.choice(MCA_COLLATERAL)
        revenue = gen_revenue(industry, tier)

        # MCA advance amount (typically 1-1.5x monthly revenue)
        monthly = revenue / 12
        advance = round(monthly * random.uniform(0.8, 1.5), -2)
        factor_rate = {'A': 1.15, 'B': 1.25, 'C': 1.35, 'D': 1.45}[tier]
        payback = advance * (factor_rate + random.uniform(-0.05, 0.05))
        term_days = random.choice([90, 120, 150, 180, 240, 360])
        daily_payment = round(payback / term_days, 2)

        # Filing/lapse dates
        days_to_lapse = random.choices(
            range(-15, 120),
            weights=[1]*15 + [5]*10 + [4]*20 + [3]*30 + [2]*30 + [1]*30
        )[0]
        lapse_date = datetime.now() + timedelta(days=days_to_lapse)
        filing_date = lapse_date - timedelta(days=random.choice([365, 540, 730]))

        # Signals
        signals = [{"type": "S1_UCC", "label": "UCC Filing Confirmed", "detail": f"Blanket lien filed by {lender}"}]
        if random.random() < 0.12:
            signals.append({"type": "S2_EXPANSION", "label": "Business Growth Signal",
                          "detail": f"{company.split()[0]} showing expansion activity in {city}",
                          "source": "Local Business News"})
        if random.random() < 0.08:
            signals.append({"type": "S3_HIRING", "label": "Active Hiring",
                          "detail": f"Hiring for {random.choice(['Manager', 'Driver', 'Technician', 'Sales Rep'])} roles",
                          "source": "Indeed/ZipRecruiter"})

        zipcode = f"{random.randint(10000, 99999)}"

        leads.append((
            company, company.split()[0] + "'s" if random.random() < 0.3 else None,
            f"{random.randint(100,9999)} {random.choice(['Main', 'Broadway', 'Market', 'Oak', 'Elm', 'Pine', 'Cedar'])} St",
            city, state, zipcode, state, lender, collateral,
            filing_date.strftime('%Y-%m-%d'), lapse_date.strftime('%Y-%m-%d'),
            days_to_lapse, f"MCA-{state[:2].upper()}-{random.randint(100000,999999)}",
            stack_depth, min(stack_depth, random.randint(1, 4)),
            advance, daily_payment, tier,
            None, None, None, None,
            industry, revenue,
            json.dumps(signals)
        ))

    conn.executemany("""
        INSERT INTO mca_leads (
            company_name, dba_name, address, city, state, zipcode, source_state,
            secured_party, collateral_desc, filing_date, lapse_date, days_to_lapse,
            file_id, stack_depth, position_number, est_advance_amount, est_daily_payment,
            funder_tier, phone, email, contact_name, company_website,
            industry, est_annual_revenue, signals_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, leads)
    conn.commit()

    # Stats
    total = conn.execute("SELECT COUNT(*) FROM mca_leads").fetchone()[0]
    stacked = conn.execute("SELECT COUNT(*) FROM mca_leads WHERE stack_depth >= 3").fetchone()[0]
    hot = conn.execute("SELECT COUNT(*) FROM mca_leads WHERE days_to_lapse >= 0 AND days_to_lapse <= 30").fetchone()[0]
    print(f"Seeded {total} MCA leads")
    print(f"  Hot (≤30d): {hot}")
    print(f"  Stacked (3+): {stacked}")
    conn.close()

if __name__ == '__main__':
    seed()

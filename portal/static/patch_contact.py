import sys

with open('index.html', 'r') as f:
    text = f.read()

target = """    ${phone || email || website || contactName ? `
    <div class="intel-section">
      <div class="intel-label"><span class="ic ic-phone"></span>Contact Data</div>
      <div class="intel-card">
        ${phone ? `<div class="intel-row"><span class="intel-key">Phone</span><span class="intel-val" style="color:var(--gold);font-weight:700"><a href="tel:${phone.replace(/[^0-9]/g,'')}" style="color:var(--gold);text-decoration:none">${esc(phone)}</a></span></div>` : ''}
        ${email ? `<div class="intel-row"><span class="intel-key">Email</span><span class="intel-val"><a href="mailto:${email}" style="color:var(--gold);text-decoration:none">${esc(email)}</a></span></div>` : ''}
        ${contactName ? `<div class="intel-row"><span class="intel-key">Contact</span><span class="intel-val">${esc(contactName)}</span></div>` : ''}
        ${website ? `<div class="intel-row"><span class="intel-key">Website</span><span class="intel-val"><a href="${website.startsWith('http') ? website : 'https://' + website}" target="_blank" style="color:var(--gold);text-decoration:none">${esc(website)}</a></span></div>` : ''}
      </div>
    </div>
    ` : ''}"""

replacement = """    ${phone || email || website || contactName ? `
    <div class="intel-section" ${lead.locked ? 'style="filter:blur(4px);pointer-events:none;user-select:none;"' : ''}>
      <div class="intel-label"><span class="ic ic-phone"></span>Contact Data</div>
      <div class="intel-card">
        ${phone ? `<div class="intel-row"><span class="intel-key">Phone</span><span class="intel-val" style="color:var(--gold);font-weight:700"><a href="tel:${phone.replace(/[^0-9]/g,'')}" style="color:var(--gold);text-decoration:none">${esc(lead.locked ? '(***) ***-****' : phone)}</a></span></div>` : ''}
        ${email ? `<div class="intel-row"><span class="intel-key">Email</span><span class="intel-val"><a href="mailto:${email}" style="color:var(--gold);text-decoration:none">${esc(lead.locked ? '*****@****.***' : email)}</a></span></div>` : ''}
        ${contactName ? `<div class="intel-row"><span class="intel-key">Contact</span><span class="intel-val">${esc(lead.locked ? 'Contact Hidden' : contactName)}</span></div>` : ''}
        ${website ? `<div class="intel-row"><span class="intel-key">Website</span><span class="intel-val"><a href="${website.startsWith('http') ? website : 'https://' + website}" target="_blank" style="color:var(--gold);text-decoration:none">${esc(lead.locked ? 'www.******.com' : website)}</a></span></div>` : ''}
      </div>
    </div>
    ` : ''}"""

if target in text:
    with open('index.html', 'w') as f:
        f.write(text.replace(target, replacement))
    print('Replaced successfully')
else:
    print('Target not found')

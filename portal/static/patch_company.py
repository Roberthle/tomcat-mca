import sys

with open('index.html', 'r') as f:
    text = f.read()

target = """    <div class="intel-section">
      <div class="intel-label"><span class="ic ic-building"></span>Company Profile</div>
      <div class="intel-card">
        <div class="intel-row"><span class="intel-key">Company</span><span class="intel-val">${esc(lead.company_name)}</span></div>
        ${address ? `<div class="intel-row"><span class="intel-key">Address</span><span class="intel-val" style="font-size:12px">${esc(address)}</span></div>` : ''}
        <div class="intel-row"><span class="intel-key">Location</span><span class="intel-val">${esc((lead.city||'') + ', ' + (lead.state||'') + ' ' + (lead.zipcode||''))}</span></div>
        ${techCat ? `<div class="intel-row"><span class="intel-key">Tech Category</span><span class="intel-val"><span class="signal-pill ucc" style="font-size:10px">${esc(techCat)}</span></span></div>` : ''}
        ${techReason ? `<div class="intel-row"><span class="intel-key">Classification</span><span class="intel-val" style="font-size:11px;color:var(--muted)">${esc(techReason)}</span></div>` : ''}
      </div>
    </div>"""

replacement = """    <div class="intel-section" ${lead.locked ? 'style="filter:blur(4px);pointer-events:none;user-select:none;"' : ''}>
      <div class="intel-label"><span class="ic ic-building"></span>Company Profile</div>
      <div class="intel-card">
        <div class="intel-row"><span class="intel-key">Company</span><span class="intel-val">${esc(lead.locked ? 'Locked Lead' : lead.company_name)}</span></div>
        ${address ? `<div class="intel-row"><span class="intel-key">Address</span><span class="intel-val" style="font-size:12px">${esc(lead.locked ? '*** Hidden Address ***' : address)}</span></div>` : ''}
        <div class="intel-row"><span class="intel-key">Location</span><span class="intel-val">${esc((lead.city||'') + ', ' + (lead.state||'') + ' ' + (lead.locked ? '' : (lead.zipcode||'')))}</span></div>
        ${techCat ? `<div class="intel-row"><span class="intel-key">Tech Category</span><span class="intel-val"><span class="signal-pill ucc" style="font-size:10px">${esc(techCat)}</span></span></div>` : ''}
        ${techReason ? `<div class="intel-row"><span class="intel-key">Classification</span><span class="intel-val" style="font-size:11px;color:var(--muted)">${esc(techReason)}</span></div>` : ''}
      </div>
    </div>"""

if target in text:
    with open('index.html', 'w') as f:
        f.write(text.replace(target, replacement))
    print('Replaced successfully')
else:
    print('Target not found')

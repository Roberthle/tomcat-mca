sed -i '' '1051,1058c\
  document.getElementById('\''dp-company'\'').textContent = lead.locked ? '\''Locked Lead'\'' : lead.company_name;\
  document.getElementById('\''dp-company'\'').style.filter = lead.locked ? '\''blur(4px)'\'' : '\''none'\'';\
  document.getElementById('\''dp-company'\'').style.userSelect = lead.locked ? '\''none'\'' : '\''auto'\'';\
\
  document.getElementById('\''dp-sub'\'').textContent =\
    `${lead.city||'\'''\''}, ${lead.state||'\'''\''} ${lead.locked ? '\'''\'' : (lead.zipcode||'\'''\'')} · MCA Filing`;\
\
  const claimed = lead.claim_status;\
  const claimBtn = document.getElementById('\''dp-claim-btn'\'');\
  if (lead.locked) {\
    claimBtn.textContent = '\''Unlock Lead '\'';\
    claimBtn.className = '\''detail-action-btn primary'\'';\
    claimBtn.onclick = function() { unlockLeadUI(id); };\
  } else {\
    claimBtn.textContent = claimed ? '\''✓ Release Claim'\'' : '\''Claim This Lead'\'';\
    claimBtn.className = `detail-action-btn ${claimed ? '\''secondary'\'' : '\''primary'\''}`;\
    claimBtn.onclick = function() { panelClaim(); };\
  }\
' index.html

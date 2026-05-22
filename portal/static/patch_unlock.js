const fs = require('fs');
let text = fs.readFileSync('index.html', 'utf8');

const target = "// ── Init — load immediately ──────────────────────────────────────────────";

const replacement = `let unlockPollInterval = null;

async function unlockLeadUI(leadId) {
  try {
    const res = await fetch(\`/api/leads/\${leadId}/checkout\`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed to initiate checkout');
    
    // Open Stripe checkout in a new tab
    window.open(data.checkout_url, '_blank');
    
    showToast('Waiting for purchase completion...', '#fbbf24');
    
    // Start background polling
    if (unlockPollInterval) clearInterval(unlockPollInterval);
    unlockPollInterval = setInterval(async () => {
      try {
        const pollRes = await fetch(\`/api/leads/\${leadId}/unlock\`);
        if (pollRes.status === 200) {
          const unlockedLead = await pollRes.json();
          clearInterval(unlockPollInterval);
          unlockPollInterval = null;
          
          // Update the local store
          window._leadsData[leadId] = Object.assign(window._leadsData[leadId] || {}, unlockedLead.lead || unlockedLead);
          
          // Show success
          showToast('Lead unlocked successfully!', '#34d399');
          
          // Refresh the panel and table
          if (_activePanelId === leadId) {
            openPanel(leadId);
          }
          loadLeads(currentPage);
        } else if (pollRes.status !== 402) {
            // Optional: Handle other errors if needed, but 402 means still locked.
        }
      } catch (e) {
        console.error('Polling error:', e);
      }
    }, 3000);
  } catch (err) {
    showToast(err.message, '#f87171');
  }
}

// ── Init — load immediately ──────────────────────────────────────────────`;

if(text.includes(target)) {
    text = text.replace(target, replacement);
    fs.writeFileSync('index.html', text);
    console.log('Replaced successfully');
} else {
    console.log('Target not found');
}

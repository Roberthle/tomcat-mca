import sys

with open('index.html', 'r') as f:
    text = f.read()

target = """      <td onclick="event.stopPropagation()">
        ${claimed
          ? `<button class="action-btn claimed" onclick="unclaimLead('${lead.id}')">✓ Claimed</button>`
          : `<button class="action-btn claim" onclick="claimLead('${lead.id}')">Claim</button>`
        }
      </td>"""

replacement = """      <td onclick="event.stopPropagation()">
        ${lead.locked
          ? `<button class="action-btn claim" onclick="unlockLeadUI('${lead.id}')">Unlock</button>`
          : claimed
            ? `<button class="action-btn claimed" onclick="unclaimLead('${lead.id}')">✓ Claimed</button>`
            : `<button class="action-btn claim" onclick="claimLead('${lead.id}')">Claim</button>`
        }
      </td>"""

if target in text:
    with open('index.html', 'w') as f:
        f.write(text.replace(target, replacement))
    print('Replaced successfully')
else:
    print('Target not found')

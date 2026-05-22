sed -i '' '918,924c\
    // Phone display\
    const phoneHTML = lead.phone\
      ? (lead.locked\
          ? `<div style="margin-top:2px;color:var(--muted);font-size:10px;font-weight:600;filter:blur(3px);user-select:none">📞 (***) ***-****</div>`\
          : `<div style="margin-top:2px"><a href="tel:${lead.phone.replace(/\\D/g,'\'\'')}" onclick="event.stopPropagation()" style="color:#34d399;font-size:10px;font-weight:600;text-decoration:none">📞 ${esc(lead.phone)}</a></div>`)\
      : '\'\'';\

' index.html

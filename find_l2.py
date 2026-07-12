from los_terminator import get_all_tickets

tickets = get_all_tickets()
found_l2 = 0
for t in tickets:
    status_obj = t.get('status')
    status_title = status_obj.get('title', '') if isinstance(status_obj, dict) else str(status_obj or '')
    resp_obj = t.get('responsible')
    resp_title = resp_obj.get('title', '') if isinstance(resp_obj, dict) else str(resp_obj or '')
    
    if '2 лин' in resp_title.lower() or '2 линия' in resp_title.lower():
        found_l2 += 1
        print(f"L2 TICKET: {t.get('ticket_number')}, Status: '{status_title}', Resp: '{resp_title}'")

print(f"Total L2 tickets found: {found_l2} out of {len(tickets)}")

from los_terminator import send_request, API_BASE_URL
url = f"{API_BASE_URL}/ticket-list?page=1&tickets_per_page=50&is_only_open=1"
resp = send_request(url)
if resp and resp.status_code == 200:
    data = resp.json()
    tickets = data.get("data", {}).get("tickets", [])
    for t in tickets:
        dept = t.get('department', {})
        if dept and '2 лин' in dept.get('title', '').lower():
            print(f"FOUND: ID={dept.get('id')}, Title={dept.get('title')}")
            break
else:
    print(f"Failed: {resp.status_code if resp else 'None'}")

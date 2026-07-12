import os
import requests
from dotenv import load_dotenv

load_dotenv()

headers = {'Authorization': f'Bearer {os.getenv("API_TOKEN")}'}
url = f'https://helpdesk.kazakhtelecom.kz/api/v1/support/tickets?status_id=1&department_id=74'

try:
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    data = r.json()
    
    tickets = data.get('data', {}).get('tickets', [])
    print(f"Найдены тикеты: {len(tickets)}")
    
    for i, t in enumerate(tickets[:3]):
        print(f"--- Тикет {i+1} ---")
        
        status_obj = t.get("status")
        status_title = status_obj.get("title", "") if isinstance(status_obj, dict) else str(status_obj or "")
        
        resp_obj = t.get("responsible")
        resp_title = resp_obj.get("title", "") if isinstance(resp_obj, dict) else str(resp_obj or "")
        
        area_obj = t.get("area")
        area_title = area_obj.get("title", "") if isinstance(area_obj, dict) else str(area_obj or "")
        
        print(f"Status Title: '{status_title}'")
        print(f"Resp Title: '{resp_title}'")
        print(f"Area Title: '{area_title}'")
        
        # Моделируем логику фейс-контроля
        if "Ожидает" not in status_title and "Открыт" not in status_title:
            print("❌ Провалил статус")
        elif "2 линия" not in resp_title:
            print("❌ Провалил ответственную линию")
        elif not area_title:
            print("❌ Провалил проверку area_title")
        else:
            print("✅ ФЕЙС КОНТРОЛЬ ПРОЙДЕН")
            
except Exception as e:
    print(f"Error: {e}")

import sys
import argparse
from los_terminator import get_all_tickets, process_ticket, get_emergency_tickets, log

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticket_number", help="Номер заявки")
    args = parser.parse_args()

    all_tickets = get_all_tickets()
    target_ticket = next((t for t in all_tickets if str(t.get("ticket_number")) == args.ticket_number), None)
            
    if not target_ticket:
        print(f"Заявка {args.ticket_number} не найдена в списке открытых!")
        sys.exit(1)

    print(f"Найдена заявка {args.ticket_number}. Данные Face Control:")
    
    status_obj = target_ticket.get("status")
    print("STATUS:", status_obj.get("title", "") if isinstance(status_obj, dict) else str(status_obj or ""))
    
    resp_obj = target_ticket.get("responsible")
    print("RESP:", resp_obj.get("title", "") if isinstance(resp_obj, dict) else str(resp_obj or ""))
    
    area_obj = target_ticket.get("area")
    print("AREA:", area_obj.get("title", "") if isinstance(area_obj, dict) else str(area_obj or ""))
    
    emergency_cache = get_emergency_tickets()
    print("Вызов process_ticket...")
    process_ticket(target_ticket, emergency_cache)
    print("Проверка завершена.")

if __name__ == "__main__":
    main()

import sys
import argparse
from los_terminator import get_all_tickets, process_ticket, get_emergency_tickets, log

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ticket_number", help="Номер заявки (например, 04072026434815)")
    args = parser.parse_args()

    all_tickets = get_all_tickets()
    if not all_tickets:
        print("Нет открытых заявок в системе.")
        sys.exit(0)

    target_ticket = None
    for t in all_tickets:
        if str(t.get("ticket_number")) == args.ticket_number:
            target_ticket = t
            break
            
    if not target_ticket:
        print(f"Заявка {args.ticket_number} не найдена в списке открытых (или не в вашей очереди)!")
        sys.exit(1)

    print(f"Найдена заявка {args.ticket_number}. Запускаем полную проверку...")
    
    from collections import defaultdict
    port_counter = defaultdict(int)
    emergency_cache = get_emergency_tickets()
    
    process_ticket(target_ticket, port_counter, emergency_cache)
    print("Проверка завершена.")

if __name__ == "__main__":
    main()

import sys
import json
import logging
from dotenv import load_dotenv

from los_terminator import get_ticket_detail, IP_LIST_ALMATY, OLT_USER, OLT_PASS
from emergency_logic import (
    parse_emergency_report,
    connect_emergency_olt,
    parse_olt_emergency_data,
    evaluate_emergency_status
)

logging.basicConfig(level=logging.INFO, format="%(message)s")

def main():
    load_dotenv()
    
    if len(sys.argv) < 2:
        print("Использование: python test_emergency.py <номер_заявки>")
        sys.exit(1)

    ticket_id = sys.argv[1]
    print(f"📡 Получаем заявку {ticket_id} из API...")
    
    ticket_html = get_ticket_detail(ticket_id)
    if not ticket_html:
        print(f"❌ Не удалось получить заявку {ticket_id}")
        return

    print("📄 Парсим текст аварии...")
    report_data = parse_emergency_report(ticket_html)
    
    if not report_data or not report_data["olt_name"]:
        print("❌ Не найден 'ОТЧЕТ ОБ АНАЛИЗЕ ОТКЛЮЧЕНИЙ' в заявке!")
        return

    olt_name = report_data["olt_name"]
    port = report_data["port"]
    clients = report_data["clients"]

    print(f"✅ Узел: {olt_name}, Порт: {port}, Клиентов: {len(clients)}")

    olt_ip = IP_LIST_ALMATY.get(olt_name)
    if not olt_ip:
        print(f"❌ IP для узла {olt_name} не найден в словаре!")
        return

    print(f"🚀 Подключаемся к OLT {olt_ip}...")
    inactive_out, active_out, optical_out = connect_emergency_olt(
        olt_ip, OLT_USER, OLT_PASS, port
    )

    if not inactive_out:
        print("❌ Не удалось получить данные с OLT!")
        return

    print("📊 Анализ данных с OLT...")
    inactive_sn, active_sn, optical_data = parse_olt_emergency_data(
        inactive_out, active_out, optical_out
    )

    status, restored, down, bad_signal, details = evaluate_emergency_status(
        clients, inactive_sn, active_sn, optical_data
    )

    print("\n" + "="*50)
    print(f"РЕЗУЛЬТАТ ПРОВЕРКИ АВАРИИ")
    print("="*50)
    print(f"Итоговый статус: {status}")
    print(details)

if __name__ == "__main__":
    main()

import re
import logging
from netmiko import ConnectHandler

log = logging.getLogger(__name__)

# Порог, при котором абонент считается "поднявшимся, но с плохим сигналом"
EMERGENCY_RX_POWER_CRITICAL = -32.0

def parse_emergency_report(text):
    """
    Извлекает данные об аварии из текста 'ОТЧЕТ ОБ АНАЛИЗЕ ОТКЛЮЧЕНИЙ'.
    """
    if not text or "ОТЧЕТ ОБ АНАЛИЗЕ ОТКЛЮЧЕНИЙ" not in text:
        return None

    result = {
        "olt_name": None,
        "port": None,
        "clients": []
    }

    olt_match = re.search(r"ПОДРОБНАЯ ИНФОРМАЦИЯ О ПОРТАХ:[\s\S]*?- ([a-zA-Z0-9_\-]+):", text)
    if olt_match:
        result["olt_name"] = olt_match.group(1).strip()

    port_match = re.search(r"Порт\s+(GPON\d+/\d+)\s*\|", text, re.IGNORECASE)
    if port_match:
        result["port"] = port_match.group(1).replace("GPON", "").strip()

    client_lines = re.findall(r"(GPON\d+/\d+:\d+)\s*\|\s*([^\|]+)\s*\|\s*([A-Z0-9:]+)\s*\|\s*off-line", text, re.IGNORECASE)
    for line in client_lines:
        subport, desc, sn = line
        result["clients"].append({
            "subport": subport.strip(),
            "description": desc.strip(),
            "sn": sn.strip()
        })

    return result

def connect_emergency_olt(olt_ip, olt_user, olt_pass, port):
    """
    Подключается к OLT и собирает данные по всему PON порту (например, 0/13).
    """
    olt_device = {
        "device_type": "cisco_ios_telnet",
        "host": olt_ip,
        "username": olt_user,
        "password": olt_pass,
        "timeout": 15,
        "global_delay_factor": 2,
    }

    try:
        log.info(f"[EMERGENCY-OLT] Подключаемся к {olt_ip} → gpon {port}...")
        net_connect = ConnectHandler(**olt_device)
        net_connect.enable()
        net_connect.send_command_timing("terminal length 0", delay_factor=1)

        # 1. Получаем список неактивных ONU
        inactive_output = net_connect.send_command_timing(
            f"show gpon inactive-onu interface gpon {port}", delay_factor=3
        )

        # 2. Получаем список активных ONU
        active_output = net_connect.send_command_timing(
            f"show gpon active-onu interface gpon {port}", delay_factor=3
        )

        # 3. Получаем затухание для всех активных ONU на порту
        optical_output = net_connect.send_command_timing(
            f"show gpon onu-optical-transceiver-diagnosis interface gpon {port}", delay_factor=4
        )

        log.info(f"[EMERGENCY-OLT] Данные для {port} успешно получены.")
        return inactive_output, active_output, optical_output

    except Exception as e:
        log.error(f"[EMERGENCY-OLT] Ошибка подключения к {olt_ip}: {e}")
        return None, None, None

def parse_olt_emergency_data(inactive_out, active_out, optical_out):
    """
    Парсит сырой вывод OLT и возвращает структурированные данные.
    """
    inactive_sn = set()
    active_sn = set()
    optical_data = {}  # sn -> rx_power

    # Парсим inactive-onu
    # GPON0/2:15     HWTC:BD61DF9B    N/A ...
    if inactive_out:
        for match in re.finditer(r"GPON\d+/\d+:\d+\s+([A-Z0-9:]+)\s+", inactive_out):
            inactive_sn.add(match.group(1).strip())

    # Парсим active-onu
    if active_out:
        for match in re.finditer(r"GPON\d+/\d+:\d+\s+([A-Z0-9:]+)\s+", active_out):
            active_sn.add(match.group(1).strip())

    # Парсим оптику. Формат:
    # interface    Temperature(degree)    Voltage(V)    Current(mA)    RxPower(dBm)    TxPower(dBm)
    # gpon0/2:1    ...
    # Тут проблема в том, что в optical-transceiver-diagnosis нет SN. 
    # Нам нужно сопоставить Subport -> SN из active_out!
    subport_to_sn = {}
    if active_out:
        for match in re.finditer(r"(GPON\d+/\d+:\d+)\s+([A-Z0-9:]+)\s+", active_out, re.IGNORECASE):
            subport_to_sn[match.group(1).lower()] = match.group(2).strip()

    if optical_out:
        for line in optical_out.splitlines():
            line = line.strip()
            # gpon0/2:1  35.1  3.2  11.9  -31.0  1.7
            match = re.match(r"(gpon\d+/\d+:\d+)\s+[\d\.]+\s+[\d\.]+\s+[\d\.]+\s+(-[\d\.]+)", line, re.IGNORECASE)
            if match:
                subp = match.group(1).lower()
                rx = float(match.group(2))
                if subp in subport_to_sn:
                    sn = subport_to_sn[subp]
                    optical_data[sn] = rx

    return inactive_sn, active_sn, optical_data

def evaluate_emergency_status(target_clients, inactive_sn, active_sn, optical_data):
    """
    Оценивает, восстановилась ли авария.
    target_clients = [{"subport": ..., "description": ..., "sn": ...}, ...]
    Возвращает:
      status: 'RESTORED', 'PARTIAL', 'DOWN', 'BAD_SIGNAL'
      restored_count: int
      down_count: int
      bad_signal_count: int
      details_text: str
    """
    total = len(target_clients)
    if total == 0:
        return "UNKNOWN", 0, 0, 0, "Нет целевых клиентов."

    restored = []
    down = []
    bad_signal = []

    for c in target_clients:
        sn = c["sn"]
        if sn in active_sn:
            rx = optical_data.get(sn)
            if rx is not None and rx <= EMERGENCY_RX_POWER_CRITICAL:
                bad_signal.append((c, rx))
            else:
                restored.append((c, rx))
        else:
            down.append(c)

    restored_count = len(restored)
    down_count = len(down)
    bad_signal_count = len(bad_signal)

    # Логика: если <= 3 лежат, считаем что в целом авария устранена (RESTORED), 
    # но если есть bad_signal, возможно статус другой.
    if down_count > 3:
        status = "PARTIAL" if restored_count > 0 else "DOWN"
    else:
        # Если лежат <= 3, считаем восстановлено.
        # Но проверим затухание.
        if bad_signal_count > 0:
            status = "BAD_SIGNAL"
        else:
            status = "RESTORED"

    details = (
        f"Всего аварийных клиентов: {total}\n"
        f"Поднялись с хорошим сигналом: {restored_count}\n"
        f"Поднялись с критическим сигналом (хуже {EMERGENCY_RX_POWER_CRITICAL}): {bad_signal_count}\n"
        f"Всё ещё лежат (off-line): {down_count}\n"
    )

    return status, restored_count, down_count, bad_signal_count, details

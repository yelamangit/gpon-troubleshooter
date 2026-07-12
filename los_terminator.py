#!/usr/bin/env python3
"""
NetDevOps Auto-Dispatcher v3.0
================================
Автообработка заявок ЛОС на OLT с цветным выводом, пагинацией и Telegram.

Поток:
  1. GET → все страницы заявок (пагинация)
  2. Фильтр: «Ожидает выполнения» + «2 линия» + area
  3. GET → детали → regex → OLT
  4. Telnet → OLT → basic-info + optical-info
  5. Массовая авария (на лету) + корреляция
  6. POST → change-status → comment → assign
  7. Telegram → уведомление в группу

Запуск:
  python los_terminator.py              # Одноразовый (все страницы)
  python los_terminator.py --loop       # Демон
  python los_terminator.py --dry-run    # Безопасный
"""

import os
import sys
import time
import re
import argparse
import logging
from datetime import datetime, timedelta
from collections import defaultdict

import requests
from netmiko import ConnectHandler
from dotenv import load_dotenv

# ╔══════════════════════════════════════════════════════════╗
# ║  1. КОНФИГУРАЦИЯ                                        ║
# ╚══════════════════════════════════════════════════════════╝

load_dotenv()

API_TOKEN = os.getenv("API_TOKEN")
OLT_USER = os.getenv("OLT_USER")
OLT_PASS = os.getenv("OLT_PASS")
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

API_BASE_URL = "https://support-center-backend.netcore.kz/api/support/tickets"
MY_MANAGER_ID = 83602
VOLS_DEPT_ID = 96
STATUS_IN_WORK = 17
LOSI_MAX_AGE_DAYS = 4
POLL_INTERVAL_SEC = 60
TICKETS_PER_PAGE = 50

# Проверка массовой аварии на OLT (портировано из SecureCRT core.py)
MASS_OUTAGE_TIME_WINDOW_MIN = 3   # ±3 минуты для определения массовой аварии
MASS_OUTAGE_MIN_CLIENTS = 4       # Минимум клиентов для массовой аварии (3 доп. + 1 текущий)
L1_DEPT_ID = int(os.getenv("L1_DEPT_ID", "0"))  # ID отдела L1 (задай в .env!)

# Пороги оптического сигнала (dBm)
RX_POWER_WEAK = -28.5       # Слабый сигнал (если оба ниже этого - на ВОЛС)
RX_POWER_CRITICAL = -32.0   # Критичный сигнал (если хотя бы один ниже этого - на ВОЛС)
RX_POWER_OLT_CRITICAL = -30.0 # Критичный сигнал именно от клиента до узла

# Множество обработанных тикетов (сохраняется между циклами в --loop)
processed_ids = set()

# ╔══════════════════════════════════════════════════════════╗
# ║  2. ЦВЕТНОЙ ТЕРМИНАЛ (ANSI)                             ║
# ╚══════════════════════════════════════════════════════════╝

# Цвета ANSI (работают в WSL/Linux/Mac терминалах)
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_BLUE = "\033[94m"
C_MAGENTA = "\033[95m"
C_CYAN = "\033[96m"
C_WHITE = "\033[97m"
C_GRAY = "\033[90m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """Цветной форматтер: каждый уровень логов — свой цвет."""
    LEVEL_COLORS = {
        logging.DEBUG:    C_GRAY,
        logging.INFO:     C_WHITE,
        logging.WARNING:  C_YELLOW,
        logging.ERROR:    C_RED,
        logging.CRITICAL: C_MAGENTA,
    }

    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, C_WHITE)
        level = record.levelname
        ts = self.formatTime(record, self.datefmt)
        msg = record.getMessage()

        # Специальная подсветка ключевых слов в сообщении
        msg = msg.replace("✓", f"{C_GREEN}✓{color}")
        msg = msg.replace("★ УСПЕХ", f"{C_GREEN}{C_BOLD}★ УСПЕХ{C_RESET}{color}")
        msg = msg.replace("⚠", f"{C_YELLOW}{C_BOLD}⚠{C_RESET}{color}")
        msg = msg.replace("[DRY_RUN]", f"{C_MAGENTA}[DRY_RUN]{color}")
        msg = msg.replace("[OLT]", f"{C_CYAN}[OLT]{color}")
        msg = msg.replace("ПРОПУСК", f"{C_GRAY}ПРОПУСК{color}")
        msg = msg.replace("Пропуск", f"{C_GRAY}Пропуск{color}")

        return f"{C_GRAY}{ts}{C_RESET} [{color}{level}{C_RESET}] {color}{msg}{C_RESET}"


# Настройка логирования
log = logging.getLogger("LOS")
log.setLevel(logging.INFO)

# Консоль — с цветами
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(ColorFormatter(datefmt="%H:%M:%S"))
log.addHandler(console_handler)

# Файл — без цветов (чтобы лог был читаемый)
file_handler = logging.FileHandler("los_terminator.log", encoding="utf-8")
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
))
log.addHandler(file_handler)

# ╔══════════════════════════════════════════════════════════╗
# ║  3. TELEGRAM-БОТ                                        ║
# ╚══════════════════════════════════════════════════════════╝

def send_telegram(message):
    """
    Отправка уведомления в Telegram-группу.
    Формат: HTML. Если токен не задан — пропускаем молча.
    """
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if resp.status_code == 200:
            log.info(f"{C_GREEN}[TG]{C_RESET} Уведомление отправлено в Telegram ✓")
        else:
            log.error(f"[TG] Ошибка: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        log.error(f"[TG] Ошибка отправки: {e}")


def notify_vols_dispatch(ticket_num, hostname, olt_ip, port, subport, description, reason="ЛОС", assign_str="ВОЛС"):
    """Уведомление в Telegram о переводе заявки на ВОЛС или Филиал."""
    desc = description or "N/A"
    link = f"https://support.nls.kz/ticket/{ticket_num}?hide_back_nav=true"

    msg = (
        f"🔧 <b>Заявка #{ticket_num} → {assign_str}</b>\n"
        f"📍 Узел: <code>{hostname}</code> ({olt_ip})\n"
        f"📡 Порт: GPON{port}:{subport}\n"
        f"👤 {desc}\n"
        f"⚡ Причина: {reason}\n"
        f"🔗 <a href=\"{link}\">Открыть в СЦ</a>"
    )
    send_telegram(msg)

# ╔══════════════════════════════════════════════════════════╗
# ║  4. HTTP-КЛИЕНТ                                         ║
# ╚══════════════════════════════════════════════════════════╝

def auto_login():
    global API_TOKEN
    login_url = "https://support-center-backend.netcore.kz/api/support/auth/login"
    sc_user = os.getenv("SC_USER")
    sc_pass = os.getenv("SC_PASS")
    if not sc_user or not sc_pass:
        log.error("[AUTO_LOGIN] В .env не указаны SC_USER и SC_PASS. Авто-логин невозможен!")
        return False
    
    payload = {"login": sc_user, "password": sc_pass}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    log.info(f"{C_YELLOW}[AUTO_LOGIN] Токен истек! Пытаемся получить новый...{C_RESET}")
    try:
        resp = requests.post(login_url, json=payload, headers=headers, verify=False, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            new_token = data.get("data", {}).get("token") or data.get("token")
            if new_token:
                API_TOKEN = new_token
                env_file_path = os.path.join(os.path.dirname(__file__), ".env")
                if os.path.exists(env_file_path):
                    with open(env_file_path, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    with open(env_file_path, "w", encoding="utf-8") as f:
                        for line in lines:
                            if line.startswith("API_TOKEN="):
                                f.write(f"API_TOKEN={new_token}\n")
                            else:
                                f.write(line)
                log.info(f"{C_GREEN}[AUTO_LOGIN] Успешный авто-логин! Новый токен сохранен.{C_RESET}")
                return True
        log.error(f"[AUTO_LOGIN] Ошибка авто-логина: {resp.text}")
        return False
    except Exception as e:
        log.error(f"[AUTO_LOGIN] Ошибка сети: {e}")
        return False

def send_request(url, payload=None, method="GET", auto_retry=True):
    """Универсальный HTTP. Headers динамические. DRY_RUN блокирует POST."""
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://support.nls.kz",
    }

    if DRY_RUN and method.upper() in ("POST", "PUT", "PATCH"):
        log.info(f"[DRY_RUN] Заблокирован {method} → {url}")
        log.debug(f"[DRY_RUN] Payload: {payload}")
        return None

    try:
        if method.upper() == "GET":
            response = requests.get(url, headers=headers, timeout=10)
        elif method.upper() == "POST":
            response = requests.post(url, json=payload, headers=headers, timeout=10)
        elif method.upper() == "PUT":
            response = requests.put(url, json=payload, headers=headers, timeout=10)
        else:
            return None
            
        if response.status_code == 401 and auto_retry:
            if auto_login():
                return send_request(url, payload, method, auto_retry=False)
            else:
                return response
                
        return response
    except requests.exceptions.Timeout:
        log.error(f"[TIMEOUT] {url}")
    except requests.exceptions.ConnectionError:
        log.error(f"[CONNECTION] {url}")
    except Exception as e:
        log.error(f"[HTTP] {method} {url}: {e}")
    return None

# ╔══════════════════════════════════════════════════════════╗
# ║  5. API САППОРТ-ЦЕНТРА                                  ║
# ╚══════════════════════════════════════════════════════════╝

def get_tickets_page(page=1):
    """Получить одну страницу заявок."""
    url = (f"{API_BASE_URL}/ticket-list?"
           f"page={page}&tickets_per_page={TICKETS_PER_PAGE}&is_only_open=1")
    response = send_request(url)
    if response is None:
        log.error(f"Страница {page}: полное отсутствие ответа (ошибка сети/таймаут)")
        return [], False
        
    if response.status_code != 200:
        log.error(f"Страница {page}: HTTP {response.status_code} - {response.text[:200]}")
        return [], False

    try:
        json_data = response.json()
    except Exception as e:
        log.error(f"Страница {page}: ошибка JSON: {e}")
        return [], False

    inner_data = json_data.get("data")

    tickets = []
    last_page = 1

    if isinstance(inner_data, list):
        tickets = inner_data
    elif isinstance(inner_data, dict):
        last_page = inner_data.get("last_page", 1)
        # Ищем список тикетов внутри dict — ключ "data" внутри "data"
        if "data" in inner_data and isinstance(inner_data["data"], list):
            tickets = inner_data["data"]
        else:
            for key, val in inner_data.items():
                if isinstance(val, list):
                    tickets = val
                    log.info(f"Страница {page}: тикеты найдены под ключом '{key}'")
                    break
    else:
        log.warning(f"Страница {page}: неожиданный тип data: {type(inner_data)}")
        # Пробуем весь ответ
        if isinstance(json_data, list):
            tickets = json_data

    if not tickets:
        log.warning(f"Страница {page}: пустой список. Ключи ответа: {list(json_data.keys()) if isinstance(json_data, dict) else 'не dict'}")
        if isinstance(inner_data, dict):
            log.warning(f"Страница {page}: ключи inner_data: {list(inner_data.keys())}")

    # Запрашиваем следующую страницу, если пришло ровно 50 заявок,
    # НО ограничиваем жестко 3 страницами (150 заявок), 
    # чтобы бот не ушел качать всю базу компании!
    has_more = (len(tickets) == TICKETS_PER_PAGE) and (page < 3)
    return tickets, has_more


def get_all_tickets():
    """
    Забираем ВСЕ страницы заявок.
    Возвращает полный список + общее количество.
    """
    all_tickets = []
    seen_ids = set()
    page = 1

    while True:
        log.info(f"{C_BLUE}📄 Страница {page}...{C_RESET}")
        tickets, has_more = get_tickets_page(page)

        if not tickets:
            break

        new_tickets = []
        for t in tickets:
            t_id = t.get("id") or t.get("ticket_number")
            if t_id not in seen_ids:
                seen_ids.add(t_id)
                new_tickets.append(t)
        
        if not new_tickets:
            log.info(f"   Страница {page}: все заявки дублируются (API зациклилось). Остановка.")
            break

        all_tickets.extend(new_tickets)
        log.info(f"   Страница {page}: {len(new_tickets)} новых заявок (всего: {len(all_tickets)})")

        if not has_more:
            break

        page += 1
        time.sleep(0.5)

    return all_tickets


def get_ticket_detail(ticket_num):
    """Проваливание в заявку → сырой текст."""
    url = f"{API_BASE_URL}/ticket/{ticket_num}"
    response = send_request(url)
    if not response or response.status_code != 200:
        return None
    return response.text


def get_ticket_comments(ticket_id):
    """Получает комментарии заявки по внутреннему ID."""
    url = f"{API_BASE_URL}/ticket-comments?ticket_id={ticket_id}"
    response = send_request(url)
    if not response or response.status_code != 200:
        return None
    return response.text



def get_emergency_tickets():
    """Получает все открытые аварии с поддержкой пагинации."""
    all_tickets = []
    seen_ids = set()
    page = 1
    while True:
        url = f"{API_BASE_URL}/ticket-list?page={page}&tickets_per_page=50&is_only_open=1&ticket_kind_id=5"
        resp = send_request(url)
        if not resp or resp.status_code != 200: break
        data = resp.json().get("data", {})
        tickets_page = data.get("tickets", []) if isinstance(data, dict) else []
        last_page = data.get("meta", {}).get("last_page", 1) if isinstance(data, dict) else 1
        
        if not tickets_page: break
        for t in tickets_page:
            t_id = t.get("id") or t.get("ticket_number")
            if t_id and t_id not in seen_ids:
                seen_ids.add(t_id)
                all_tickets.append(t)
        if page >= last_page: break
        page += 1
        time.sleep(0.3)
    return all_tickets


def take_ticket_in_work(ticket_id):
    """POST change-status → В работу."""
    url = f"{API_BASE_URL}/change-status"
    payload = {
        "ticket_id": ticket_id,
        "status_id": STATUS_IN_WORK,
        "responsible_type": "manager",
        "responsible_id": MY_MANAGER_ID,
    }
    response = send_request(url, payload=payload, method="POST")
    if response is None and DRY_RUN:
        return True
    if response and response.status_code == 200:
        log.info(f"Тикет {ticket_id}: взят в работу ✓")
        return True
    log.error(f"Тикет {ticket_id}: ошибка change-status")
    return False


def post_comment(ticket_id, comment_html):
    """POST ticket-comment-add → комментарий."""
    url = f"{API_BASE_URL}/ticket-comment-add"
    payload = {
        "ticket_id": ticket_id,
        "comment": comment_html,
        "attached_files": [],
    }
    response = send_request(url, payload=payload, method="POST")
    if response is None and DRY_RUN:
        return True
    if response and response.status_code == 200:
        log.info(f"Тикет {ticket_id}: комментарий ✓")
        return True
    log.error(f"Тикет {ticket_id}: ошибка комментария")
    return False


def assign_ticket(ticket_id, resp_type, resp_id):
    """Универсальная функция перевода заявки (отдел или менеджер)."""
    url = f"{API_BASE_URL}/assign"
    payload = {
        "ticket_id": ticket_id,
        "responsible_type": resp_type,
        "responsible_id": resp_id,
    }
    response = send_request(url, payload=payload, method="POST")
    if response is None and DRY_RUN:
        return True
    if response and response.status_code == 200:
        log.info(f"Тикет {ticket_id}: переведен на {resp_type}:{resp_id} ✓")
        return True
    log.error(f"Тикет {ticket_id}: ошибка перевода на {resp_type}:{resp_id}")
    return False


def update_current_situation(ticket_id, message_text):
    """POST update-message-current-situation → Внешний комментарий (снаружи)."""
    url = f"{API_BASE_URL}/update-message-current-situation"
    payload = {
        "id": ticket_id,
        "message": message_text
    }
    response = send_request(url, payload=payload, method="POST")
    if response is None and DRY_RUN:
        return True
    if response and response.status_code == 200:
        log.info(f"Тикет {ticket_id}: внешний коммент обновлен ✓")
        return True
    log.error(f"Тикет {ticket_id}: ошибка update-message-current-situation")
    return False


def update_decision(ticket_id, decision_text):
    """POST update-decision → Решение внутри заявки."""
    url = f"{API_BASE_URL}/update-decision"
    payload = {
        "id": ticket_id,
        "decision": decision_text
    }
    response = send_request(url, payload=payload, method="POST")
    if response is None and DRY_RUN:
        return True
    if response and response.status_code == 200:
        log.info(f"Тикет {ticket_id}: решение обновлено ✓")
        return True
    log.error(f"Тикет {ticket_id}: ошибка update-decision")
    return False

# ╔══════════════════════════════════════════════════════════╗
# ║  6. РАБОТА С OLT                                        ║
# ╚══════════════════════════════════════════════════════════╝

def connect_olt(olt_ip, port, subport):
    """
    Подключение к OLT: 3 команды за одну сессию.

    Команда 1: basic-info → история отключений, серийник, описание
    Команда 2: onu optical-transceiver-diagnosis → RxPower клиента, Temperature, Voltage, TxPower
    Команда 3: optical-transceiver-diagnosis interface → RxPower на стороне OLT

    Формат выхлопа OLT (реальный):
      Команда 2:
        interface    Temperature(degree)    Voltage(V)    Current(mA)    RxPower(dBm)    TxPower(dBm)
        gpon0/6:1    62.2                   3.3           14.3           -22.1           2.0

      Команда 3:
        interface    RxPower(dBm)
        gpon0/6:1    -27.2

    Возвращает (basic_output, onu_optical_output, olt_optical_output).
    """
    olt_device = {
        "device_type": "cisco_ios_telnet",
        "host": olt_ip,
        "username": OLT_USER,
        "password": OLT_PASS,
        "timeout": 10,
        "global_delay_factor": 2,
    }

    net_connect = None
    try:
        log.info(f"[OLT] Подключаемся к {olt_ip} → gpon {port}:{subport}...")
        net_connect = ConnectHandler(**olt_device)
        net_connect.enable()

        # Отключаем пагинацию OLT (--More--), чтобы получить ВСЮ историю
        net_connect.send_command_timing("terminal length 0", delay_factor=1)

        # Команда 1: basic-info
        basic_output = net_connect.send_command_timing(
            f"show gpon interface gpon {port}:{subport} onu basic-info",
            delay_factor=3,
        )

        # Команда 2: optical на стороне ONU (RxPower клиента)
        onu_optical = ""
        try:
            onu_optical = net_connect.send_command_timing(
                f"show gpon interface gpon {port}:{subport} onu optical-transceiver-diagnosis",
                delay_factor=2,
            )
        except Exception:
            log.warning(f"[OLT] Не удалось выполнить onu optical-transceiver-diagnosis")

        # Команда 3: optical на стороне OLT (RxPower узла)
        olt_optical = ""
        try:
            olt_optical = net_connect.send_command_timing(
                f"show gpon optical-transceiver-diagnosis interface gpon {port}:{subport}",
                delay_factor=2,
            )
        except Exception:
            log.warning(f"[OLT] Не удалось выполнить OLT optical-transceiver-diagnosis")

        log.info(f"[OLT] {olt_ip}: данные получены ✓")
        return basic_output, onu_optical, olt_optical

    except Exception as e:
        log.error(f"[OLT] Ошибка связи с {olt_ip}: {e}")
        return None, None, None
    finally:
        if net_connect:
            try:
                net_connect.disconnect()
            except Exception:
                pass

# ╔══════════════════════════════════════════════════════════╗
# ║  7. ПАРСИНГ ДАННЫХ                                      ║
# ╚══════════════════════════════════════════════════════════╝

def parse_olt_from_detail(ticket_num):
    """Regex: hostname(IP) gpon0/8:55 из деталей заявки."""
    detail_text = get_ticket_detail(ticket_num)
    if not detail_text:
        return None

    pattern = (
        r"(?P<hostname>[a-zA-Z0-9_\-]+)"
        r"\((?P<ip>[\d\.]+)\)"
        r"\s+gpon(?P<port>\d+(?:\\/|/)\d+)"
        r":(?P<subport>\d+)"
    )
    match = re.search(pattern, detail_text)
    if not match:
        return None

    return {
        "hostname": match.group("hostname"),
        "olt_ip": match.group("ip"),
        "port": match.group("port").replace("\\/", "/"),
        "subport": match.group("subport"),
    }


def parse_basic_info(olt_output):
    """Парсинг basic-info: серийник, описание, история отключений."""
    result = {
        "serial_number": None,
        "description": None,
        "disconnect_history": [],
        "last_disconnect_time": None,
        "last_disconnect_reason": None,
        "is_active": False,
        "distance": None,
    }
    if not olt_output:
        return result

    serial_match = re.search(r"[Ss]erial\s+[Nn]umber\s*:?\s+(?P<sn>\S+)", olt_output)
    if serial_match:
        result["serial_number"] = serial_match.group("sn")

    desc_match = re.search(r"ONU\s+[Dd]escription\s*:?\s+(?P<desc>\S+)", olt_output)
    if desc_match:
        result["description"] = desc_match.group("desc")

    if re.search(r"\bOnline\b", olt_output, re.IGNORECASE):
        result["is_active"] = True

    dist_match = re.search(r"Distance\s+([-\d.]+)\s*m", olt_output, re.IGNORECASE)
    if dist_match:
        try:
            result["distance"] = float(dist_match.group(1))
        except ValueError:
            pass

    # История: seq  act_time  deact_time  reason
    history_pattern = (
        r"(\d+)\s+"
        r"(\d{4}-\d{2}-\d{2}\s+\d+:\d+:\d+)\s+"
        r"(\d{4}-\d{2}-\d{2}\s+\d+:\d+:\d+)\s+"
        r"(Dying\s*Gasp|Losi|LOS|LOSi|DyingGasp|[A-Za-z_\-]+)"
    )
    matches = re.findall(history_pattern, olt_output)
    for seq, act_time, deact_time, reason in matches:
        result["disconnect_history"].append({
            "seq": int(seq),
            "activation_time": act_time.strip(),
            "deactivation_time": deact_time.strip(),
            "reason": reason.strip(),
        })

    if result["disconnect_history"]:
        last = result["disconnect_history"][-1]
        result["last_disconnect_time"] = last["deactivation_time"]
        result["last_disconnect_reason"] = last["reason"]

    return result


def parse_optical_info(onu_optical_output, olt_optical_output):
    """
    Парсинг двух команд optical-transceiver-diagnosis.

    Команда ONU (onu_optical_output) — табличный формат:
      interface    Temperature(degree)    Voltage(V)    Current(mA)    RxPower(dBm)    TxPower(dBm)
      -----------  ---------------------  ------------  -------------  --------------  --------------
      gpon0/6:1    62.2                   3.3           14.3           -22.1           2.0

    Команда OLT (olt_optical_output) — табличный формат:
      interface    RxPower(dBm)
      -----------  --------------
      gpon0/6:1    -27.2

    Возвращает dict с rx_power (от узла к клиенту) и olt_rx_power (от клиента к узлу).
    """
    result = {
        "rx_power": None,       # RxPower на ONU (сигнал ОТ узла К клиенту)
        "tx_power": None,       # TxPower клиента
        "olt_rx_power": None,   # RxPower на OLT (сигнал ОТ клиента К узлу)
        "temperature": None,
        "voltage": None,
        "bias_current": None,
    }

    # ── Парсинг ONU optical (команда 2) ──
    # Ищем строку данных после разделителя (---)
    # Формат: gpon0/6:1    62.2    3.3    14.3    -22.1    2.0
    if onu_optical_output:
        onu_data_match = re.search(
            r"gpon\d+/\d+:\d+\s+"
            r"([-\d.]+)\s+"      # Temperature
            r"([-\d.]+)\s+"      # Voltage
            r"([-\d.]+)\s+"      # Current
            r"([-\d.]+)\s+"      # RxPower
            r"([-\d.]+)",        # TxPower
            onu_optical_output
        )
        if onu_data_match:
            try:
                result["temperature"] = float(onu_data_match.group(1))
                result["voltage"] = float(onu_data_match.group(2))
                result["bias_current"] = float(onu_data_match.group(3))
                result["rx_power"] = float(onu_data_match.group(4))
                result["tx_power"] = float(onu_data_match.group(5))
            except ValueError:
                pass

    # ── Парсинг OLT optical (команда 3) ──
    # Формат: gpon0/6:1    -27.2
    if olt_optical_output:
        olt_data_match = re.search(
            r"gpon\d+/\d+:\d+\s+([-\d.]+)",
            olt_optical_output
        )
        if olt_data_match:
            try:
                result["olt_rx_power"] = float(olt_data_match.group(1))
            except ValueError:
                pass

    return result

# ╔══════════════════════════════════════════════════════════╗
# ║  8. HTML-КОММЕНТАРИИ                                    ║
# ╚══════════════════════════════════════════════════════════╝

def build_los_comment(hostname, port, subport, basic, optical):
    """Комментарий при ЛОС (нет линка)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    desc = basic["description"] or "N/A"
    serial = basic["serial_number"] or "N/A"
    last_time = basic["last_disconnect_time"] or "N/A"
    last_reason = basic["last_disconnect_reason"] or "N/A"

    lines = [
        "<p>Нет оптического линка, требуется выезд сварной бригады для восстановления.</p>",
        "<p><br></p>",
        f"<p>Дата проверки: {now}</p>",
        f"<p>Линия: GPON{port}:{subport} ({desc})</p>",
        f"<p>Коммутатор: {hostname}</p>",
        f"<p>❌ Состояние: Не активен (нет линка)</p>",
        f"<p>▶ Серийный номер: {serial}</p>",
        f"<p>▶ Описание: {desc}</p>",
        f"<p>▶ Последнее отключение: {last_time}</p>",
        f"<p>▶ Причина: {last_reason}</p>",
    ]

    history = basic["disconnect_history"]
    if history:
        lines.append("<p><br></p>")
        lines.append(f"<p>ИСТОРИЯ ОТКЛЮЧЕНИЙ ({len(history)} всего):</p>")
        lines.append("<p>--------------------------------------------------</p>")
        for i, e in enumerate(history, 1):
            lines.append(f"<p>&nbsp;{i:02d}. {e['deactivation_time']} - {e['reason']}</p>")
        lines.append("<p>--------------------------------------------------</p>")

    return "".join(lines)


def build_flap_comment(hostname, port, subport, basic, flap_events_count):
    """Комментарий при флапе (постоянные обрывы линка)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    desc = basic["description"] or "N/A"
    serial = basic["serial_number"] or "N/A"
    last_time = basic["last_disconnect_time"] or "N/A"
    last_reason = basic["last_disconnect_reason"] or "N/A"

    lines = [
        "<p>Обнаружен флап линка. Требуется проверка оптической линии.</p>",
        "<p><br></p>",
        f"<p>Дата проверки: {now}</p>",
        f"<p>Линия: GPON{port}:{subport} ({desc})</p>",
        f"<p>Коммутатор: {hostname}</p>",
        f"<p>⚠️ Состояние: Флап линка</p>",
        f"<p>▶ Серийный номер: {serial}</p>",
        f"<p>▶ Описание: {desc}</p>",
        f"<p>▶ Последнее отключение: {last_time}</p>",
        f"<p>▶ Причина: {last_reason}</p>",
        "<p><br></p>",
        f"<p>⚡ ОБНАРУЖЕН ФЛАП ЛИНКА: {flap_events_count} событий за последние 24 часа</p>",
    ]

    history = basic["disconnect_history"]
    if history:
        lines.append("<p><br></p>")
        lines.append(f"<p>ИСТОРИЯ ОТКЛЮЧЕНИЙ ({len(history)} всего):</p>")
        lines.append("<p>--------------------------------------------------</p>")
        for i, e in enumerate(history, 1):
            lines.append(f"<p>&nbsp;{i:02d}. {e['deactivation_time']} - {e['reason']}</p>")
        lines.append("<p>--------------------------------------------------</p>")

    return "".join(lines)


def build_weak_signal_comment(hostname, port, subport, basic, optical):
    """Комментарий при слабом сигнале (линк есть, но затухание высокое)."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    desc = basic["description"] or "N/A"
    serial = basic["serial_number"] or "N/A"
    rx = optical["rx_power"]
    olt_rx = optical["olt_rx_power"]
    tx = optical["tx_power"]
    temp = optical["temperature"]
    voltage = optical["voltage"]
    bias = optical.get("bias_current")
    dist = basic.get("distance")

    rx_str = f"{rx} dBm" if rx is not None else "N/A"
    olt_rx_str = f"{olt_rx} dBm" if olt_rx is not None else "N/A"
    tx_str = f"{tx} dBm" if tx is not None else "N/A"
    temp_str = f"{temp}°C" if temp is not None else "N/A"
    volt_str = f"{voltage} В" if voltage is not None else "N/A"
    bias_str = f"{bias} мА" if bias is not None else "N/A"
    dist_str = f"{dist} м" if dist is not None else "N/A"

    if rx is not None and olt_rx is not None:
        if rx <= RX_POWER_CRITICAL and olt_rx <= RX_POWER_CRITICAL:
            intro = f"Линк есть, но сигнал критически слабый в обе стороны (от узла: {rx_str}, к узлу: {olt_rx_str})."
        elif rx <= RX_POWER_WEAK and olt_rx <= RX_POWER_WEAK:
            intro = f"Линк есть, но сигнал в обе стороны слабый (от узла: {rx_str}, к узлу: {olt_rx_str})."
        elif rx <= RX_POWER_CRITICAL:
            intro = f"Линк есть, но сигнал от узла к клиенту критически слабый ({rx_str})."
        elif olt_rx <= RX_POWER_CRITICAL:
            intro = f"Линк есть, но сигнал от клиента к узлу критически слабый ({olt_rx_str})."
        elif olt_rx <= RX_POWER_OLT_CRITICAL:
            intro = f"Линк есть, но затухание от клиента к узлу слишком высокое ({olt_rx_str})."
        else:
            intro = f"Линк есть, но сигнал в обе стороны слабый (от узла: {rx_str}, к узлу: {olt_rx_str})."
    else:
        intro = f"Линк есть, но сигнал слабый (от узла: {rx_str}, к узлу: {olt_rx_str})."

    lines = [
        f"<p>{intro} Нужен выезд ВОЛС для снижения затухания.</p>",
        "<p><br></p>",
        f"<p>Дата проверки: {now}</p>",
        f"<p>Линия: GPON{port}:{subport} ({desc})</p>",
        f"<p>Коммутатор: {hostname}</p>",
        f"<p>✅ Состояние: Работает</p>",
        f"<p>❌ Качество сигнала: Слабое (от узла: {rx_str} / к узлу: {olt_rx_str})</p>",
        "<p><br></p>",
        "<p>ФИЗИЧЕСКИЕ ПАРАМЕТРЫ:</p>",
        f"<p>▶ Температура: {temp_str}</p>",
        f"<p>▶ Напряжение: {volt_str}</p>",
        f"<p>▶ Потребляемый ток: {bias_str}</p>",
        f"<p>▶ Сигнал от узла к клиенту (RxPower): {rx_str}</p>",
        f"<p>▶ Сигнал от клиента к узлу (RxPower OLT): {olt_rx_str}</p>",
        f"<p>▶ Мощность передатчика клиента (TxPower): {tx_str}</p>",
        f"<p>▶ Расстояние до узла: {dist_str}</p>",
        "<p><br></p>",
        f"<p>▶ Серийный номер: {serial}</p>",
        f"<p>▶ Описание: {desc}</p>",
    ]

    # Краткая история если были Losi
    losi_entries = [e for e in basic["disconnect_history"]
                    if "losi" in e["reason"].lower() or "los" in e["reason"].lower()]
    if losi_entries:
        lines.append("<p><br></p>")
        lines.append(f"<p>⚠️ Клиент флапал (Losi), сигнал слабый. "
                     f"Рекомендуется выезд для проверки линии.</p>")

    return "".join(lines)

# ╔══════════════════════════════════════════════════════════╗
# ║  8.5. ПРОВЕРКА МАССОВОЙ АВАРИИ НА OLT                   ║
# ║  (Портировано из SecureCRT core.py check_if_client_down) ║
# ╚══════════════════════════════════════════════════════════╝

def check_mass_outage_on_olt(olt_ip, port, subport, last_losi_time_str):
    """
    Подключается к OLT и проверяет, является ли ЛОС частью массовой аварии.

    Логика (из core.py):
      1. sh gp ina | include <дата_падения>  → все упавшие за эту дату
      2. Парсим порты + время падения
      3. Считаем клиентов, упавших в окне ±MASS_OUTAGE_TIME_WINDOW_MIN от last_losi_time
      4. Если >= MASS_OUTAGE_MIN_CLIENTS → массовая авария

    Возвращает: (is_mass_outage: bool, affected_clients: list[dict])
      affected_clients = [{"port": "GPON0/8:5", "time": "2026-07-12 15:30:14",
                            "reason": "Losi", "description": "...", "serial": "..."}, ...]
    """
    try:
        last_losi_dt = datetime.strptime(last_losi_time_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        log.error(f"[MASS] Ошибка парсинга даты: {last_losi_time_str}")
        return False, []

    log_date = last_losi_time_str[:10]  # "2026-07-12"
    time_min = last_losi_dt - timedelta(minutes=MASS_OUTAGE_TIME_WINDOW_MIN)
    time_max = last_losi_dt + timedelta(minutes=MASS_OUTAGE_TIME_WINDOW_MIN)

    current_port = f"GPON{port}:{subport}"

    olt_device = {
        "device_type": "cisco_ios_telnet",
        "host": olt_ip,
        "username": OLT_USER,
        "password": OLT_PASS,
        "timeout": 10,
        "global_delay_factor": 2,
    }

    net_connect = None
    try:
        log.info(f"[MASS] Подключаемся к {olt_ip} для проверки массовой аварии...")
        net_connect = ConnectHandler(**olt_device)
        net_connect.enable()

        # Отключаем пагинацию (--More--) в привилегированном режиме
        net_connect.send_command_timing("terminal length 0", delay_factor=1)
        net_connect.clear_buffer()

        inactive_output_clean = ""
        detail_output = ""

        # ШАГ 1: Все неактивные (БЕЗ pipe-фильтра, т.к. OLT переносит строки на 80 символах
        #         и ломает дату пополам, из-за чего | include не находит совпадений)
        inactive_cmd = "show gpon inactive-onu"
        log.info(f"[MASS] Выполняем: {inactive_cmd}")
        inactive_output = net_connect.send_command(inactive_cmd, read_timeout=120)
        log.info(f"[MASS] Получено {len(inactive_output)} символов, {len(inactive_output.splitlines())} строк")

        # Склеиваем разорванные строки (OLT рубит на 80 символов)
        fixed_lines = []
        for line in inactive_output.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("GPON") or line.startswith("Interface") or line.startswith("IntfName") or line.startswith("---"):
                fixed_lines.append(line)
            else:
                if fixed_lines:
                    fixed_lines[-1] += line  # БЕЗ пробела — строка разорвана посередине

        inactive_output_clean = "\n".join(fixed_lines)

        # ШАГ 2: Получаем детальную информацию о клиентах на ЭТОМ порту
        # Используем onu-description, так как onu-detail не поддерживается на BDCOM
        port_num = port.split("/")[-1] if "/" in port else port
        detail_cmd = f"show gpon onu-description interface gpon 0/{port_num}"
        log.info(f"[MASS] Выполняем: {detail_cmd}")
        detail_output = net_connect.send_command(detail_cmd, read_timeout=30)


    except Exception as e:
        log.error(f"[MASS] Ошибка подключения к {olt_ip}: {e}")
        return False, []
    finally:
        if net_connect:
            try:
                net_connect.disconnect()
            except Exception:
                pass

    # ШАГ 3: Парсим неактивных и считаем попавших в окно
    affected_clients = []

    # Парсим detail_output для получения description и serial
    client_details = {}  # port -> {"description": ..., "serial": ...}
    if detail_output:
        # Склеиваем разорванные строки из-за ширины экрана
        fixed_detail = []
        for line in detail_output.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("GPON") or line.startswith("Interface") or line.startswith("IntfName") or line.startswith("-"):
                fixed_detail.append(line)
            else:
                if fixed_detail:
                    fixed_detail[-1] += " " + line

        for line in fixed_detail:
            detail_match = re.search(
                r"(GPON[0-9a-zA-Z]+/\d+:\d+)\s+(\S+)\s+(\S+:\S+)\s+\S+\s+(\S+)",
                line, re.IGNORECASE
            )
            if detail_match:
                d_port = detail_match.group(1).upper().replace("GPONO", "GPON0")
                d_desc = detail_match.group(2)
                d_serial = detail_match.group(3)
                d_status = detail_match.group(4)
                client_details[d_port] = {
                    "description": d_desc,
                    "serial": d_serial,
                    "status": d_status,
                }

    # Парсим inactive_output_clean
    if inactive_output_clean:
        for line in inactive_output_clean.splitlines():
            if not line.strip():
                continue

            port_match = re.search(r"(GPON[0-9a-zA-Z]+/\d+:\d+)", line, re.IGNORECASE)
            time_match = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
            reason_match = re.search(r"(Dying\s*Gasp|Losi|LOS|LOSi|Los|Tiwi)", line)

            if not port_match or not time_match:
                continue

            found_port = port_match.group(1).upper().replace("GPONO", "GPON0")

            # Пропускаем текущего клиента (его мы уже знаем)
            if found_port == current_port:
                continue

            # Проверяем, что клиент на ТОМ ЖЕ основном порту
            found_main_port = re.search(r"GPON[0-9a-zA-Z]+/(\d+):", found_port, re.IGNORECASE)
            if found_main_port and found_main_port.group(1) != port_num:
                continue

            try:
                log_time = datetime.strptime(time_match.group(1), "%Y-%m-%d %H:%M:%S")
                if time_min <= log_time <= time_max:
                    # Попал в окно!
                    details = client_details.get(found_port, {})
                    affected_clients.append({
                        "port": found_port,
                        "time": time_match.group(1),
                        "reason": reason_match.group(1) if reason_match else "N/A",
                        "description": details.get("description", "N/A"),
                        "serial": details.get("serial", "N/A"),
                        "status": details.get("status", "off-line"),
                    })
            except ValueError:
                continue

    # +1 за текущего клиента
    total_affected = len(affected_clients) + 1
    is_mass = total_affected >= MASS_OUTAGE_MIN_CLIENTS

    if is_mass:
        log.warning(
            f"[MASS] ⚠ МАССОВАЯ АВАРИЯ ПОДТВЕРЖДЕНА! "
            f"{total_affected} клиентов на {olt_ip} порт {port} "
            f"в окне ±{MASS_OUTAGE_TIME_WINDOW_MIN} мин от {last_losi_time_str}"
        )
    else:
        log.info(
            f"[MASS] Массовая авария НЕ подтверждена. "
            f"Всего {total_affected} клиентов (нужно {MASS_OUTAGE_MIN_CLIENTS}+). "
            f"Продолжаем как одиночный ЛОС."
        )

    return is_mass, affected_clients


def find_matching_emergency(emergency_cache, hostname, port, affected_clients, current_drop_time_str=None):
    """
    Проверяет, существует ли уже открытая аварийная заявка для данного узла/порта,
    анализируя первый (отчетный) комментарий.
    """
    port_num = port.split("/")[-1] if "/" in port else port
    
    current_drop_dt = None
    if current_drop_time_str:
        try:
            current_drop_dt = datetime.strptime(current_drop_time_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    for em in emergency_cache:
        em_num = em.get("ticket_number")
        em_id = em.get("id")

        comments_text = get_ticket_comments(em_id)
        if not comments_text:
            continue

        # Проверяем, есть ли упоминание нашего узла и порта в комментариях
        if hostname in comments_text and (f"/{port_num}" in comments_text or f"GPON{port}" in comments_text or f"порт {port_num}" in comments_text.lower()):
            
            # Проверяем время падения
            if current_drop_dt:
                time_matches = re.findall(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", comments_text)
                found_match = False
                for t_str in time_matches:
                    try:
                        em_time_dt = datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
                        diff = abs((current_drop_dt - em_time_dt).total_seconds()) / 60.0
                        if diff <= 10:  # Разница не более 10 минут
                            found_match = True
                            break
                    except Exception:
                        pass
                
                if found_match:
                    log.info(f"[MASS] Найдена существующая авария #{em_num} по комментариям (совпало время и порт)")
                    return em_num, em_id
                else:
                    log.info(f"[MASS] Авария #{em_num} на том же порту, но время падения отличается > 10 мин. Считаем новой.")
            else:
                log.info(f"[MASS] Найдена авария #{em_num}, но нет времени падения текущего клиента. Привязываем.")
                return em_num, em_id

    return None, None



def build_mass_outage_html_comment(hostname, olt_ip, port, subport, basic,
                                    affected_clients, last_losi_time_str):
    """
    Формирует HTML-комментарий с полным отчётом о массовой аварии.
    Использует классические <p> и разделители без попытки идеально выровнять пробелами.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    desc = basic.get("description") or "N/A"
    serial = basic.get("serial_number") or "N/A"
    total = len(affected_clients) + 1  # +1 текущий клиент
    
    # Собираем всех клиентов в один список для удобства
    all_clients = [
        {
            "port": f"GPON{port}:{subport}".upper().replace("GPONO", "GPON0"),
            "description": desc,
            "serial": serial,
            "status": "off-line N/A",
            "time": last_losi_time_str,
            "reason": basic.get("last_disconnect_reason", "Losi")
        }
    ]
    for c in affected_clients:
        st = c.get("status", "off-line")
        if st == "off-line":
            st = "off-line N/A"
        all_clients.append({
            "port": c.get("port", "N/A"),
            "description": c.get("description", "N/A"),
            "serial": c.get("serial", "N/A"),
            "status": st,
            "time": c.get("time", "N/A"),
            "reason": c.get("reason", "N/A")
        })

    # Сортируем по порту (номеру ONT)
    def extract_onu_id(p):
        try:
            return int(p.split(":")[-1])
        except:
            return 999
    all_clients.sort(key=lambda x: extract_onu_id(x["port"]))

    # Подсчет статистики причин
    reason_counts = {}
    for c in all_clients:
        r = c["reason"]
        reason_counts[r] = reason_counts.get(r, 0) + 1

    port_main = port  # обычно "0/11"

    lines = [
        "<p>===================================================================================</p>",
        "<p><b>ОТЧЕТ ОБ АНАЛИЗЕ ОТКЛЮЧЕНИЙ</b></p>",
        "<p>===================================================================================</p>",
        f"<p>Дата и время анализа: {now}</p>",
        f"<p>Общее количество клиентов: {total}</p>",
        "<p>-----------------------------------------------------------------------------------</p>",
        f"<p>{total} кл {hostname} {port_main} (Частично)</p>",
        "<p><br></p>",
        "<p>СТАТИСТИКА ПО ПРИЧИНАМ ОТКЛЮЧЕНИЯ:</p>"
    ]

    for r, cnt in reason_counts.items():
        pct = (cnt / total) * 100
        lines.append(f"<p>- {r}: {cnt} клиентов ({pct:.1f}%)</p>")

    lines.extend([
        "<p>-----------------------------------------------------------------------------------</p>",
        "<p>СТАТИСТИКА ПО ПОРТАМ:</p>",
        f"<p>- {hostname}:</p>",
        f"<p>Частично отключенные порты: {port_main}</p>",
        "<p>-----------------------------------------------------------------------------------</p>",
        "<p>ПОДРОБНАЯ ИНФОРМАЦИЯ О ПОРТАХ:</p>",
        f"<p>- {hostname}:</p>",
        "<p>-----------------------------------------------------------------------------------</p>",
        f"<p>Порт GPON{port_main} | Описание | Серийный номер | Статус | Отключение | Причина</p>",
        "<p>-----------------------------------------------------------------------------------</p>"
    ])

    for c in all_clients:
        p = c['port']
        desc_str = c['description']
        sn = c['serial']
        st = c['status']
        tm = c['time']
        rs = c['reason']
        # Просто соединяем через ' | ' как на скриншоте пользователя
        client_line = f"{p} | {desc_str} | {sn} | {st} | {tm} | {rs} |"
        lines.append(f"<p>{client_line}</p>")

    lines.extend([
        "<p>===================================================================================</p>"
    ])

    return "".join(lines)


def build_existing_emergency_comment(emergency_ticket_num):
    """Комментарий для заявки, которая относится к уже существующей аварии."""
    link = f"https://support.nls.kz/ticket/{emergency_ticket_num}?hide_back_nav=true"
    return f"<p>Данная заявка относится к общей аварии <a href=\"{link}\">#{emergency_ticket_num}</a></p>"


def assign_to_l1(ticket_id):
    """POST assign → L1 (возврат на первую линию)."""
    if L1_DEPT_ID == 0:
        log.warning(f"[L1] L1_DEPT_ID не задан в .env! Заявка {ticket_id} останется на текущем исполнителе.")
        return False

    url = f"{API_BASE_URL}/assign"
    payload = {
        "ticket_id": ticket_id,
        "responsible_type": "department",
        "responsible_id": L1_DEPT_ID,
    }
    response = send_request(url, payload=payload, method="POST")
    if response is None and DRY_RUN:
        return True
    if response and response.status_code == 200:
        log.info(f"Тикет {ticket_id}: возвращён на L1 ✓")
        return True
    log.error(f"Тикет {ticket_id}: ошибка возврата на L1")
    return False


def notify_mass_outage_telegram(ticket_num, hostname, olt_ip, port, total_clients,
                                 affected_clients, is_existing=False, emergency_num=None):
    """Telegram-уведомление о массовой аварии."""
    link = f"https://support.nls.kz/ticket/{ticket_num}?hide_back_nav=true"

    if is_existing and emergency_num:
        em_link = f"https://support.nls.kz/ticket/{emergency_num}?hide_back_nav=true"
        msg = (
            f"📎 <b>Заявка #{ticket_num} → Общая авария #{emergency_num}</b>\n"
            f"📍 Узел: <code>{hostname}</code> ({olt_ip})\n"
            f"📡 Порт: GPON{port}\n"
            f"ℹ️ Клиент относится к существующей аварии\n"
            f"🔗 <a href=\"{link}\">Заявка</a> | <a href=\"{em_link}\">Авария</a>"
        )
    else:
        # Формируем краткий список упавших
        client_list = ""
        for c in affected_clients[:5]:  # Максимум 5 в ТГ
            client_list += f"\n  • {c.get('port', '?')} — {c.get('description', 'N/A')}"
        if len(affected_clients) > 5:
            client_list += f"\n  ... и ещё {len(affected_clients) - 5}"

        msg = (
            f"🚨 <b>ОБНАРУЖЕНА МАССОВАЯ АВАРИЯ!</b>\n"
            f"📍 Узел: <code>{hostname}</code> ({olt_ip})\n"
            f"📡 Порт: GPON{port}\n"
            f"👥 Упало клиентов: <b>{total_clients}</b>{client_list}\n"
            f"🔗 <a href=\"{link}\">Открыть заявку #{ticket_num}</a>\n\n"
            f"⚠️ <i>Полный отчёт в комментариях.</i>"
        )

    send_telegram(msg)


def handle_mass_outage(ticket_id, ticket_num, hostname, olt_ip, port, subport,
                       basic, optical, affected_clients, last_losi_time_str,
                       emergency_cache):
    """
    Обработка заявки при подтверждённой массовой аварии.

    Два сценария:
      A) Авария уже существует → комментарий + вернуть на L1
      B) Новая авария → отчёт в коммент + TG (остается на Л2)
    """
    desc = basic.get("description") or "N/A"
    total = len(affected_clients) + 1

    # Проверяем существующие аварии
    emergency_num, emergency_id = find_matching_emergency(
        emergency_cache, hostname, port, affected_clients, last_losi_time_str
    )

    if emergency_num:
        # ── СЦЕНАРИЙ А: АВАРИЯ УЖЕ СУЩЕСТВУЕТ ──
        log.info(
            f"{C_YELLOW}{C_BOLD}[{ticket_num}] Клиент относится к существующей аварии "
            f"#{emergency_num}{C_RESET}"
        )

        # Шаг 1: Взять в работу
        take_ticket_in_work(ticket_id)
        time.sleep(1)

        # Шаг 2: Комментарий со ссылкой на аварию
        comment_html = build_existing_emergency_comment(emergency_num)
        post_comment(ticket_id, comment_html)
        time.sleep(1)

        # Шаг 3: Вернуть на L1
        assign_to_l1(ticket_id)

        # Шаг 4: TG
        notify_mass_outage_telegram(
            ticket_num, hostname, olt_ip, port, total,
            affected_clients, is_existing=True, emergency_num=emergency_num
        )

        log.info(f"{C_GREEN}[{ticket_num}] ✓ Привязан к аварии #{emergency_num}, возвращён на L1{C_RESET}")

    else:
        # ── СЦЕНАРИЙ Б: НОВАЯ МАССОВАЯ АВАРИЯ ──
        log.warning(
            f"{C_RED}{C_BOLD}[{ticket_num}] 🚨 НОВАЯ МАССОВАЯ АВАРИЯ! "
            f"{total} клиентов на {hostname} порт GPON{port}{C_RESET}"
        )

        # Шаг 1: Комментарий с полным отчётом
        log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} → Шаг 1/2: Вставляем отчёт об аварии...")
        comment_html = build_mass_outage_html_comment(
            hostname, olt_ip, port, subport, basic,
            affected_clients, last_losi_time_str
        )
        post_comment(ticket_id, comment_html)
        time.sleep(2)

        # Шаг 2: TG уведомление
        log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} → Шаг 2/2: Уведомляем Telegram...")
        notify_mass_outage_telegram(
            ticket_num, hostname, olt_ip, port, total,
            affected_clients, is_existing=False
        )

        log.info(
            f"{C_GREEN}{C_BOLD}[{ticket_num}] ★ МАССОВАЯ АВАРИЯ обработана! "
            f"Отчёт в комментариях (Остается на текущей линии).{C_RESET}"
        )


# ╔══════════════════════════════════════════════════════════╗
# ║  9. ОБРАБОТКА ОДНОЙ ЗАЯВКИ                               ║
# ╚══════════════════════════════════════════════════════════╝

def process_ticket(ticket, emergency_cache):
    """
    Полный цикл обработки. Два сценария:
      A) ЛОС → комментарий ЛОС → ВОЛС
      B) Линк есть, но сигнал слабый → комментарий затухание → ВОЛС
    """
    ticket_id = ticket.get("id")
    ticket_num = ticket.get("ticket_number")

    # Пропуск уже обработанных
    if ticket_id in processed_ids:
        return None

    # ── ФЕЙС-КОНТРОЛЬ ──────────────────────────────────────

    status_obj = ticket.get("status")
    status_title = status_obj.get("title", "") if isinstance(status_obj, dict) else str(status_obj or "")
    if "Ожидает" not in status_title and "Открыт" not in status_title:
        return None

    resp_obj = ticket.get("responsible")
    resp_title = resp_obj.get("title", "") if isinstance(resp_obj, dict) else str(resp_obj or "")
    if "2 линия" not in resp_title:
        return None

    area_obj = ticket.get("area")
    area_title = area_obj.get("title", "") if isinstance(area_obj, dict) else str(area_obj or "")
    # УБРАЛ ЖЕСТКУЮ ПРОВЕРКУ area_title, ТАК КАК ИЗ-ЗА НЕЕ ОТБРАКОВЫВАЛИСЬ ТВОИ ЗАЯВКИ!

    log.info(f"{C_BLUE}{'─' * 50}{C_RESET}")
    log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} Прошёл фейс-контроль...")

    # ── ПАРСИНГ OLT ──────────────────────────────────────────

    olt_data = parse_olt_from_detail(ticket_num)
    if not olt_data:
        log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} Данные OLT не найдены. Пропуск.")
        processed_ids.add(ticket_id)
        return None

    hostname = olt_data["hostname"]
    olt_ip = olt_data["olt_ip"]
    port = olt_data["port"]
    subport = olt_data["subport"]
    port_key = f"{olt_ip}:{port}"

    log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} Узел: {C_BOLD}{hostname}{C_RESET} ({olt_ip}), порт: {port}:{subport}")


    # ── ПОДКЛЮЧЕНИЕ К OLT ────────────────────────────────────

    basic_output, onu_optical, olt_optical = connect_olt(olt_ip, port, subport)
    if not basic_output:
        log.error(f"[{ticket_num}] OLT недоступен. Пропуск.")
        return olt_data

    basic = parse_basic_info(basic_output)
    optical = parse_optical_info(onu_optical, olt_optical)

    log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} Desc: {basic['description']} | Serial: {basic['serial_number']}")
    log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} История: {len(basic['disconnect_history'])} записей")

    if optical["rx_power"] is not None:
        rx_color = C_RED if optical["rx_power"] < RX_POWER_WEAK else C_GREEN
        log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} RxPower: {rx_color}{optical['rx_power']} dBm{C_RESET}")
    if optical["olt_rx_power"] is not None:
        orx_color = C_RED if optical["olt_rx_power"] < RX_POWER_WEAK else C_GREEN
        log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} OLT RxPower: {orx_color}{optical['olt_rx_power']} dBm{C_RESET}")

    # ── ОПРЕДЕЛЯЕМ СТАТУС КЛИЕНТА ─────────────────────────────
    # Если клиент ОНЛАЙН → проверяем сигнал (сценарий B)
    # Если клиент ОФЛАЙН → проверяем ЛОС (сценарий A)

    if basic["is_active"]:
        # ── СЦЕНАРИЙ B: КЛИЕНТ ОНЛАЙН → ПРОВЕРКА СИГНАЛА ─────────
        log.info(f"{C_GREEN}[{ticket_num}] Клиент ОНЛАЙН.{C_RESET} Проверяем уровень сигнала...")

        if optical["rx_power"] is not None and optical["olt_rx_power"] is not None:
            rx = optical["rx_power"]
            orx = optical["olt_rx_power"]
            
            is_critical = False
            reason_msg = ""
            if rx <= RX_POWER_CRITICAL or orx <= RX_POWER_CRITICAL:
                is_critical = True
                reason_msg = f"КРИТИЧНЫЙ СИГНАЛ (< {RX_POWER_CRITICAL})"
            elif rx <= RX_POWER_WEAK and orx <= RX_POWER_WEAK:
                is_critical = True
                reason_msg = f"СЛАБЫЙ СИГНАЛ В ОБЕ СТОРОНЫ (< {RX_POWER_WEAK})"
            elif orx <= RX_POWER_OLT_CRITICAL:
                is_critical = True
                reason_msg = f"ВЫСОКОЕ ЗАТУХАНИЕ ОТ КЛИЕНТА (< {RX_POWER_OLT_CRITICAL})"

            if is_critical:
                log.info(
                    f"{C_YELLOW}{C_BOLD}[{ticket_num}] ⚠ {reason_msg}!{C_RESET} "
                    f"RxPower: {rx} dBm / OLT Rx: {orx} dBm"
                )

                dispatch_to_vols(ticket_id, ticket_num, hostname, olt_ip, port, subport,
                                 basic, optical, area_title=area_title, comment_type="weak_signal")
                processed_ids.add(ticket_id)
                return olt_data
            else:
                log.info(f"[{ticket_num}] Сигнал не требует выезда ВОЛС ({rx} / {orx} dBm). Пропуск.")
        else:
            log.info(f"[{ticket_num}] Оптические данные недоступны. Проверяем флап...")
            # ПРОВЕРКА НА ФЛАП
            recent_flaps = []
            now = datetime.now()
            for e in basic["disconnect_history"]:
                try:
                    dt = datetime.strptime(e["deactivation_time"], "%Y-%m-%d %H:%M:%S")
                    if (now - dt).total_seconds() < 24 * 3600:
                        recent_flaps.append(e)
                except:
                    pass
            
            flap_count = len(recent_flaps)
            if flap_count >= 5:
                log.info(f"{C_YELLOW}{C_BOLD}[{ticket_num}] ⚡ ОБНАРУЖЕН ФЛАП ЛИНКА! ({flap_count} событий){C_RESET}")
                dispatch_to_vols(ticket_id, ticket_num, hostname, olt_ip, port, subport,
                                 basic, optical, area_title=area_title, comment_type="flap", flap_count=flap_count)
                processed_ids.add(ticket_id)
                return olt_data
            else:
                log.info(f"[{ticket_num}] Флап не подтверждён (мало событий). Пропуск.")

    else:
        # ── СЦЕНАРИЙ A: КЛИЕНТ ОФЛАЙН → ПРОВЕРКА ЛОС ────────────
        log.info(f"{C_RED}[{ticket_num}] Клиент ОФЛАЙН.{C_RESET} Проверяем ЛОС...")

        losi_entries = [e for e in basic["disconnect_history"]
                        if "losi" in e["reason"].lower() or "los" == e["reason"].lower()]

        if losi_entries:
            last_losi = losi_entries[-1]
            last_losi_str = last_losi["deactivation_time"]
            try:
                last_losi_time = datetime.strptime(last_losi_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                log.error(f"[{ticket_num}] Ошибка парсинга даты: '{last_losi_str}'")
                processed_ids.add(ticket_id)
                return olt_data

            days_passed = (datetime.now() - last_losi_time).days

            if days_passed <= LOSI_MAX_AGE_DAYS:
                log.info(
                    f"{C_GREEN}{C_BOLD}[{ticket_num}] ✓ СВЕЖИЙ ЛОС!{C_RESET} "
                    f"Дата: {last_losi_str}, давность: {days_passed} дн."
                )

                # ── НОВАЯ ЛОГИКА: ПРОВЕРКА МАССОВОЙ АВАРИИ ПЕРЕД ВОЛС ──
                log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} Проверяем массовую аварию на OLT...")
                is_mass, affected_clients = check_mass_outage_on_olt(
                    olt_ip, port, subport, last_losi_str
                )

                if is_mass:
                    # МАССОВАЯ АВАРИЯ → обработка через handle_mass_outage
                    handle_mass_outage(
                        ticket_id, ticket_num, hostname, olt_ip, port, subport,
                        basic, optical, affected_clients, last_losi_str,
                        emergency_cache
                    )
                else:
                    # ОДИНОЧНЫЙ ЛОС → на ВОЛС (как раньше)
                    dispatch_to_vols(ticket_id, ticket_num, hostname, olt_ip, port, subport,
                                     basic, optical, area_title=area_title, comment_type="los")

                processed_ids.add(ticket_id)
                return olt_data
            else:
                log.info(f"[{ticket_num}] ЛОС старый ({days_passed} дн.). Пропуск.")
        else:
            log.info(f"[{ticket_num}] ЛОС не найден в истории. Пропуск.")

    processed_ids.add(ticket_id)
    return olt_data

# ╔══════════════════════════════════════════════════════════╗
# ║  10. МАРШРУТИЗАЦИЯ НА ВОЛС                              ║
# ╚══════════════════════════════════════════════════════════╝

def dispatch_to_vols(ticket_id, ticket_num, hostname, olt_ip, port, subport,
                     basic, optical, area_title="", comment_type="los", flap_count=0):
    """
    3 шага: change-status → comment → assign.
    + Telegram-уведомление.
    """
    if not area_title:
        log.warning(f"[{ticket_num}] Участок пустой и нет массовой аварии. ПРОПУСКАЕМ (ничего не делаем).")
        return

    desc = basic["description"] or "N/A"

    # Шаг 1/3: Взять в работу
    log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} → Шаг 1/3: Берём в работу...")
    if not take_ticket_in_work(ticket_id):
        log.error(f"[{ticket_num}] Ошибка change-status. Прерываем.")
        return
    time.sleep(2)

    # Шаг 2/3: Комментарий
    log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} → Шаг 2/3: Отправляем диагностику...")
    if comment_type == "los":
        comment_html = build_los_comment(hostname, port, subport, basic, optical)
        reason = "Свежий ЛОС (нет линка)"
    elif comment_type == "flap":
        comment_html = build_flap_comment(hostname, port, subport, basic, flap_count)
        reason = f"Флап линка ({flap_count} событий за 24ч)"
    else:
        comment_html = build_weak_signal_comment(hostname, port, subport, basic, optical)
        reason = f"Слабый сигнал ({optical.get('rx_power')} / {optical.get('olt_rx_power')} dBm)"

    post_comment(ticket_id, comment_html)
    time.sleep(2)

    # Шаг 3/3: Перевести на ВОЛС или Филиал г.Конаев
    is_konaev = "конаев" in area_title.lower()

    if is_konaev:
        log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} → Шаг 3/3: Переводим на Филиал г.Конаев...")
        assign_ticket(ticket_id, "department", 82604)
        time.sleep(1)
        success = assign_ticket(ticket_id, "manager", 142)
        assign_str = "Филиал г.Конаев (Алексей)"
    else:
        log.info(f"{C_CYAN}[{ticket_num}]{C_RESET} → Шаг 3/3: Переводим на ВОЛС...")
        success = assign_ticket(ticket_id, "department", VOLS_DEPT_ID)
        assign_str = "ВОЛС"

    if success:
        log.info(f"{C_GREEN}{C_BOLD}[{ticket_num}] ★ УСПЕХ! Заявка обработана полностью.{C_RESET}")
        # Telegram-уведомление
        notify_vols_dispatch(ticket_num, hostname, olt_ip, port, subport, desc, reason, assign_str)
    else:
        log.error(f"[{ticket_num}] Ошибка перевода заявки.")

# ╔══════════════════════════════════════════════════════════╗
# ║  11. ТОЧКА ВХОДА                                        ║
# ╚══════════════════════════════════════════════════════════╝

def main():
    parser = argparse.ArgumentParser(description="NetDevOps Auto-Dispatcher v3.0")
    parser.add_argument("--loop", action="store_true", help="Режим демона")
    parser.add_argument("--dry-run", action="store_true", help="Безопасный режим")
    args = parser.parse_args()

    global DRY_RUN
    if args.dry_run:
        DRY_RUN = True

    if not API_TOKEN:
        log.error("API_TOKEN не найден! Создай .env")
        sys.exit(1)
    if not OLT_USER or not OLT_PASS:
        log.error("OLT_USER / OLT_PASS не найдены!")
        sys.exit(1)

    mode = f"{C_MAGENTA}DRY_RUN{C_RESET}" if DRY_RUN else f"{C_GREEN}{C_BOLD}БОЕВОЙ{C_RESET}"
    tg_status = f"{C_GREEN}Включён{C_RESET}" if TG_BOT_TOKEN else f"{C_GRAY}Выключен{C_RESET}"

    log.info(f"{C_BOLD}{'═' * 55}{C_RESET}")
    log.info(f"  {C_BOLD}NetDevOps Auto-Dispatcher v3.0{C_RESET}")
    log.info(f"  Режим: {mode}")
    log.info(f"  Telegram: {tg_status}")

    log.info(f"  Порог сигнала: Слабый {RX_POWER_WEAK} dBm, Критичный {RX_POWER_CRITICAL} dBm")
    log.info(f"  Макс. возраст ЛОСа: {LOSI_MAX_AGE_DAYS} дн.")
    log.info(f"{C_BOLD}{'═' * 55}{C_RESET}")

    def run_cycle():
        log.info(f"{C_BLUE}📡 Запрос всех страниц заявок...{C_RESET}")
        all_tickets = get_all_tickets()

        if not all_tickets:
            log.info("Нет заявок.")
            return

        # Сколько новых (ещё не обработанных)
        new_count = sum(1 for t in all_tickets if t.get("id") not in processed_ids)
        log.info(f"Всего: {len(all_tickets)} | Новых: {C_BOLD}{new_count}{C_RESET} | "
                 f"Уже обработано: {len(processed_ids)}")

        if new_count == 0:
            log.info(f"{C_GRAY}Новых заявок нет. Всё обработано.{C_RESET}")
            return

        emergency_cache = get_emergency_tickets()
        log.info(f"Открытых аварий: {len(emergency_cache)}")

        port_counter = defaultdict(int)
        processed_this_cycle = 0

        for t in all_tickets:
            try:
                result = process_ticket(t, emergency_cache)
                if result:
                    processed_this_cycle += 1
            except Exception as e:
                log.error(f"Ошибка в тикете {t.get('ticket_number')}: {e}")

        log.info(f"{C_BOLD}Обработано в этом цикле: {processed_this_cycle}{C_RESET}")



    if args.loop:
        log.info(f"{C_BOLD}Демон: каждые {POLL_INTERVAL_SEC} сек. Ctrl+C = стоп.{C_RESET}")
        while True:
            try:
                run_cycle()
                log.info(f"{C_GRAY}Сон {POLL_INTERVAL_SEC} сек...{C_RESET}")
                time.sleep(POLL_INTERVAL_SEC)
            except KeyboardInterrupt:
                log.info(f"\n{C_YELLOW}Ctrl+C → Завершаем.{C_RESET}")
                break
    else:
        run_cycle()
        log.info(f"{C_GREEN}Прогон завершён.{C_RESET}")


if __name__ == "__main__":
    main()

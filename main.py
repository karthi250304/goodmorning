#!/usr/bin/env python3
"""
Telegram_GoodMorning
====================
Logs in to the HRMS website, reads the FIRST IN time (Movement Count = 1) of one
employee for TODAY from the Employee Movements page, and - if that time is between
08:00 and 11:00 (inclusive, IST) - sends ONE Good Morning message to a Telegram group.

Reliability rules implemented here
----------------------------------
* Only the authenticated web page is used. No HRMS database / backend access.
* A day is marked "sent" ONLY after Telegram answers ok=true.
* Before every send attempt the day is marked "pending" ON DISK. If the send outcome
  is unknown (timeout after the request left, crash, power loss) the script does NOT
  send again - a missed message is preferred over a duplicate.
* Only failures that provably happened BEFORE the message left the PC (no connection,
  DNS failure, HTTP 429) are retried automatically.
* Secrets live in Windows Credential Manager and are redacted from every log line.
"""
from __future__ import annotations

import argparse
import asyncio  # noqa: F401  (used lazily by TelegramUser)
import configparser
import getpass
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.ini"
STATE_PATH = BASE_DIR / "attendance_status.json"
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "automation.log"
LOCK_PATH = BASE_DIR / ".instance.lock"
ENV_PATH = BASE_DIR / "secrets.env"  # optional KEY=VALUE secrets file (servers without Credential Manager)

SERVICE_NAME = "TelegramGoodMorning"
KEY_HRMS = "hrms_password"
KEY_TOKEN = "telegram_bot_token"
KEY_API_ID = "telegram_api_id"
KEY_API_HASH = "telegram_api_hash"
KEY_SESSION = "telegram_session"

# Exact message requested (\U0001F31E = sun with face).
MESSAGE = "\U0001F31E Good Morning All!\nHave a great and productive day!"

# Statuses after which NOTHING more is ever sent/checked for that day.
TERMINAL = {"sent", "pending", "uncertain", "skipped_early", "skipped_late"}

log = logging.getLogger("goodmorning")
_TZ = ZoneInfo("Asia/Kolkata")
_SECRETS: set = set()
_TOKEN_RE = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")


# ----------------------------------------------------------------------------
# Time / logging helpers
# ----------------------------------------------------------------------------
def set_tz(tz: ZoneInfo) -> None:
    global _TZ
    _TZ = tz


def now_ist() -> datetime:
    return datetime.now(_TZ)


def redact(text: str) -> str:
    for sec in _SECRETS:
        if sec and len(sec) >= 4:
            text = text.replace(sec, "***")
    return _TOKEN_RE.sub("bot***", text)


class RedactingFormatter(logging.Formatter):
    """Final safety net: removes password / token (also inside tracebacks)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = RedactingFormatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("goodmorning")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout is not None and (sys.stdout.isatty() or os.environ.get("LOG_TO_CONSOLE") == "1"):
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        root.addHandler(ch)
    for noisy in ("urllib3", "selenium", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def first_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return redact(f"{type(exc).__name__}: {text[0] if text else ''}")


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
class ConfigError(Exception):
    pass


def _hhmm(text: str) -> dtime:
    return datetime.strptime(text.strip(), "%H:%M").time()


class Settings:
    def __init__(self, path: Path):
        cp = configparser.ConfigParser(interpolation=None)
        if not cp.read(path, encoding="utf-8"):
            raise ConfigError(f"Cannot read configuration file: {path}")

        def get(sec: str, key: str, default: str = "") -> str:
            return str(cp.get(sec, key, fallback=default)).strip()

        try:
            self.login_url = get("hrms", "login_url")
            self.movements_url = get("hrms", "movements_url")
            self.username = os.environ.get("HRMS_USERNAME", "").strip() or get("hrms", "username")
            self.config_password = get("hrms", "password")  # optional; less secure than Credential Manager
            # Company rule: HRMS login only works AFTER the IN punch, so a refused login just means "not punched yet".
            self.login_requires_punch = cp.getboolean("hrms", "login_requires_punch", fallback=True)
            self.employee_code = get("hrms", "employee_code", "8933")
            self.tz = ZoneInfo(get("schedule", "timezone", "Asia/Kolkata"))
            self.start = _hhmm(get("schedule", "start_time", "08:00"))
            self.end = _hhmm(get("schedule", "end_time", "11:00"))
            self.interval = max(15, int(get("schedule", "check_interval_seconds", "60")))
            self.grace = int(get("schedule", "final_grace_minutes", "2"))
            self.retry_extra = int(get("schedule", "send_retry_extra_minutes", "15"))
            self.headless = cp.getboolean("browser", "headless", fallback=False)
            self.page_timeout = int(get("browser", "page_timeout_seconds", "60"))
            self.element_timeout = int(get("browser", "element_timeout_seconds", "30"))
            self.settle = float(get("browser", "settle_seconds", "3"))
            self.chromedriver_path = get("browser", "chromedriver_path")
            self.chrome_binary = get("browser", "chrome_binary")
            self.date_formats = [f.strip() for f in get("page", "date_formats", "%d-%m-%Y").split(",") if f.strip()]
            self.date_input_format = get("page", "date_input_format", "%d-%m-%Y")
            self.search_button_selector = get("page", "search_button_selector")
            self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or get("telegram", "chat_id")
            self.tg_mode = get("telegram", "mode", "user").lower()
        except Exception as exc:  # ValueError, ZoneInfoNotFoundError, ...
            raise ConfigError(f"Invalid value in config.ini: {first_line(exc)}") from exc

    def problems(self, need_telegram: bool) -> List[str]:
        p: List[str] = []
        if not self.username or self.username.upper() == "CHANGE_ME":
            p.append("[hrms] username is not set in config.ini")
        if not self.employee_code:
            p.append("[hrms] employee_code is empty")
        if self.tg_mode not in ("user", "bot"):
            p.append("[telegram] mode must be 'user' or 'bot'")
        chat_ok = bool(re.fullmatch(r"-?\d+", self.chat_id or "")) or (
            self.tg_mode == "user" and (self.chat_id or "").lower() == "me")
        if need_telegram and not chat_ok:
            p.append("[telegram] chat_id must be a number such as -1001234567890 (or 'me' for testing)")
        if self.start >= self.end:
            p.append("[schedule] start_time must be earlier than end_time")
        return p


# ----------------------------------------------------------------------------
# Secrets (Windows Credential Manager via keyring; env vars as fallback)
# ----------------------------------------------------------------------------
def get_secret(key: str, env_name: str) -> Optional[str]:
    value = None
    try:
        import keyring
        value = keyring.get_password(SERVICE_NAME, key)
    except Exception as exc:
        log.debug("Credential store unavailable (%s); using environment variables.", first_line(exc))
    value = value or os.environ.get(env_name)
    if value:
        _SECRETS.add(value)
    return value or None


def load_env_file() -> None:
    """Load KEY=VALUE lines from secrets.env into the environment (existing variables win)."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _write_env_file(name: str, value: str) -> None:
    lines = []
    if ENV_PATH.exists():
        lines = [ln for ln in ENV_PATH.read_text(encoding="utf-8").splitlines() if not ln.strip().startswith(name + "=")]
    lines.append(f"{name}={value}")
    fd = os.open(ENV_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only on Linux
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


def store_secret(key: str, env_name: str, value: str) -> str:
    """Windows Credential Manager if available, otherwise the owner-only secrets.env file."""
    try:
        import keyring
        keyring.set_password(SERVICE_NAME, key, value)
        return "the system credential store"
    except Exception:
        _write_env_file(env_name, value)
        return f"{ENV_PATH.name} (owner-only file; keep it private)"


def setup_credentials(mode: str = "user") -> int:
    import keyring
    print("Secrets are stored in Windows Credential Manager for THIS Windows user.")
    print("Press Enter on a prompt to keep the value already stored.\n")
    pw = getpass.getpass("HRMS password (hidden): ")
    if pw:
        where = store_secret(KEY_HRMS, "HRMS_PASSWORD", pw)
        print(f"HRMS password saved in {where}.")
    if mode == "bot":
        tok = getpass.getpass("Telegram bot token (hidden): ")
        if tok:
            if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{20,}", tok.strip()):
                print("WARNING: that does not look like a bot token (expected 123456789:AA...). Saved anyway.")
            where = store_secret(KEY_TOKEN, "TELEGRAM_BOT_TOKEN", tok.strip())
            print(f"Telegram bot token saved in {where}.")
    else:
        print("\nTelegram mode = user (your own account). Next step:")
        print("  .\\.venv\\Scripts\\python.exe main.py --setup-telegram-login")
    return 0


def setup_telegram_login(export: bool = False) -> int:
    """One-time login of YOUR Telegram account. Session is kept in Windows Credential Manager only."""
    import asyncio
    import keyring
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    print("One-time Telegram login (your own account).")
    print("Nothing typed here is shown on screen or saved in any file.\n")
    api_id = input("api_id (the number from my.telegram.org): ").strip()
    api_hash = getpass.getpass("api_hash (hidden): ").strip()
    phone = input("Your phone number with country code, e.g. +919876543210: ").strip()
    if not api_id.isdigit() or not api_hash or not phone:
        print("api_id must be a number; api_hash and phone are required.")
        return 2

    async def go():
        client = TelegramClient(StringSession(), int(api_id), api_hash)
        await client.start(
            phone=lambda: phone,
            code_callback=lambda: input("Login code Telegram sent to you (type it here only): ").strip(),
            password=lambda: getpass.getpass("Two-step verification password (hidden): "),
        )
        me = await client.get_me()
        session = client.session.save()
        await client.disconnect()
        return me, session

    try:
        me, session = asyncio.run(go())
    except Exception as exc:
        print("Login failed:", first_line(exc))
        return 2
    if export:
        _SECRETS.add(session)
        print("\nNEW cloud session created (your PC login is untouched). Copy the long line below into the")
        print("GitHub secret named TELEGRAM_SESSION, then clear this screen (type: cls).\n")
        print(session)
        print("\nAlso create secrets TELEGRAM_API_ID and TELEGRAM_API_HASH with the values you typed above.")
        print("Anyone who has this text can act as your Telegram account - never share it.")
        return 0
    if len(session) > 1200:
        print("Session string unexpectedly long; cannot store in Credential Manager.")
        return 2
    store_secret(KEY_API_ID, "TELEGRAM_API_ID", api_id)
    store_secret(KEY_API_HASH, "TELEGRAM_API_HASH", api_hash)
    where = store_secret(KEY_SESSION, "TELEGRAM_SESSION", session)
    print(f"\nLogged in as: {getattr(me, 'first_name', '')} {getattr(me, 'last_name', '') or ''}".rstrip())
    print(f"Session saved in: {where}")
    print("To revoke it any time: Telegram > Settings > Devices > terminate that session.")
    return 0


def delete_credentials() -> int:
    import keyring
    for key in (KEY_HRMS, KEY_TOKEN, KEY_API_ID, KEY_API_HASH, KEY_SESSION):
        try:
            keyring.delete_password(SERVICE_NAME, key)
            print(f"Deleted {key}")
        except Exception:
            print(f"{key}: nothing to delete")
    return 0



# ----------------------------------------------------------------------------
# Duplicate-proof state store (JSON, atomic writes, backup copy)
# ----------------------------------------------------------------------------
class StateCorruptError(Exception):
    pass


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.bak = path.with_name(path.name + ".bak")

    @staticmethod
    def _read(p: Path) -> dict:
        text = p.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("state file is not a JSON object")
        return data

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return self._read(self.path)
        except Exception as exc:
            log.error("State file unreadable (%s).", first_line(exc))
            if self.bak.exists():
                try:
                    data = self._read(self.bak)
                    log.warning("Recovered state from backup %s", self.bak.name)
                    return data
                except Exception:
                    pass
            raise StateCorruptError(
                f"{self.path.name} and its backup are corrupt. Refusing to send (a duplicate could "
                f"result). Fix or replace the file with {{}} after checking the Telegram group."
            ) from exc

    def get(self, day: str) -> dict:
        return dict(self.load().get(day, {}))

    def update(self, day: str, **fields) -> None:
        data = self.load()
        rec = data.get(day, {})
        rec.update(fields)
        rec["updated_at"] = now_ist().isoformat(timespec="seconds")
        data[day] = rec
        for old in sorted(data)[:-60]:  # keep ~60 days
            del data[old]
        self._write(data)

    def _write(self, data: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        if self.path.exists():
            try:
                self._read(self.path)
                shutil.copy2(self.path, self.bak)
            except Exception:
                pass
        last: Optional[Exception] = None
        for _ in range(6):  # Windows: antivirus/indexer may briefly lock the file
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.5)
        raise last  # type: ignore[misc]


class SingleInstance:
    """OS-level lock so two copies (e.g. two Task Scheduler triggers) never run together."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self) -> bool:
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        return True


# ----------------------------------------------------------------------------
# Telegram Bot API
# ----------------------------------------------------------------------------
@dataclass
class SendResult:
    outcome: str  # delivered | not_delivered | uncertain | fatal
    detail: str = ""
    message_id: Optional[int] = None
    retry_after: int = 0


def failed_before_send(exc: BaseException) -> bool:
    """True only if the failure provably happened while connecting (nothing was sent)."""
    seen, stack = set(), [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, (NewConnectionError, ConnectTimeoutError)):
            return True
        for arg in getattr(cur, "args", ()):
            if isinstance(arg, BaseException):
                stack.append(arg)
        reason = getattr(cur, "reason", None)
        if isinstance(reason, BaseException):
            stack.append(reason)
        if cur.__cause__ is not None:
            stack.append(cur.__cause__)
    return False


class Telegram:
    API = "https://api.telegram.org"

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.session = requests.Session()

    def _call(self, method: str, payload: Optional[dict] = None, timeout=(10, 20)) -> Tuple[int, dict]:
        resp = self.session.post(f"{self.API}/bot{self.token}/{method}", json=payload or {}, timeout=timeout)
        try:
            data = resp.json()
        except ValueError:
            data = {"ok": False, "description": f"non-JSON answer (HTTP {resp.status_code})"}
        return resp.status_code, data

    def send_message(self, text: str, dedupe: bool = False) -> SendResult:
        try:
            status, data = self._call("sendMessage", {"chat_id": self.chat_id, "text": text})
        except requests.exceptions.ConnectTimeout as exc:
            return SendResult("not_delivered", first_line(exc))
        except requests.exceptions.RequestException as exc:
            if failed_before_send(exc):
                return SendResult("not_delivered", "could not connect: " + first_line(exc))
            return SendResult("uncertain", "request may have reached Telegram: " + first_line(exc))
        if status == 200 and data.get("ok"):
            return SendResult("delivered", message_id=(data.get("result") or {}).get("message_id"))
        desc = str(data.get("description", "unknown error"))
        if status == 429:
            wait = int((data.get("parameters") or {}).get("retry_after", 30))
            return SendResult("not_delivered", f"rate limited: {desc}", retry_after=wait)
        if status >= 500:
            return SendResult("uncertain", f"Telegram server error {status}: {desc}")
        return SendResult("fatal", f"HTTP {status}: {desc}")

    def preflight(self, verbose: bool = False) -> Tuple[Optional[bool], str]:
        """No message is sent. True=OK, False=definitely broken, None=could not tell (network)."""
        try:
            st, d = self._call("getMe")
            if not d.get("ok"):
                return False, f"Bot token rejected by Telegram: {d.get('description')}"
            me = d["result"]
            lines = [f"Bot: @{me.get('username')} (id {me['id']})"]
            st, d = self._call("getChat", {"chat_id": self.chat_id})
            if not d.get("ok"):
                return False, f"Bot cannot access chat {self.chat_id}: {d.get('description')} (is the bot a member?)"
            chat = d["result"]
            lines.append(f"Chat: {chat.get('title', '?')} (type: {chat.get('type')})")
            st, d = self._call("getChatMember", {"chat_id": self.chat_id, "user_id": me["id"]})
            if not d.get("ok"):
                return False, f"Cannot read bot membership: {d.get('description')}"
            member = d["result"]
            status = member.get("status")
            lines.append(f"Bot status in chat: {status}")
            if status in ("left", "kicked"):
                return False, "\n".join(lines) + "\nBot is NOT a member of the group."
            if status == "restricted" and not member.get("can_send_messages", True):
                return False, "\n".join(lines) + "\nBot is restricted: cannot send messages."
            perms = chat.get("permissions") or {}
            if status == "member" and perms and not perms.get("can_send_messages", True):
                return False, "\n".join(lines) + "\nGroup only lets admins post: make the bot an administrator."
            if chat.get("type") == "channel" and status != "administrator":
                return False, "\n".join(lines) + "\nIn a channel the bot must be an administrator."
            return True, "\n".join(lines)
        except requests.exceptions.RequestException as exc:
            return None, "Could not reach Telegram: " + first_line(exc)


class TelegramUser:
    """
    Posts as YOUR OWN Telegram account (Telethon). Your account must already be a member of the group.
    chat_id = "me" sends to your own Saved Messages (safe testing).
    Duplicate safety: Telethon re-sends use the same random_id, so Telegram de-duplicates them;
    errors that can happen AFTER the message left (timeouts, 5xx) are reported as 'uncertain'.
    """

    def __init__(self, api_id: int, api_hash: str, session: str, chat_id: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self.is_me = chat_id.strip().lower() == "me"
        self.chat_id = None if self.is_me else int(chat_id)

    def _client(self):
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        return TelegramClient(StringSession(self.session), self.api_id, self.api_hash,
                              connection_retries=2, request_retries=2, retry_delay=2, timeout=15)

    async def _find_entity(self, client):
        if self.is_me:
            return "me"
        async for dialog in client.iter_dialogs():
            if dialog.id == self.chat_id:
                return dialog.entity
        return None

    async def _find_today_message(self, client, entity, text: str) -> Optional[int]:
        """Id of a message with exactly this text, sent by THIS account today (IST), else None."""
        day_start = datetime.combine(now_ist().date(), dtime(0, 0), tzinfo=_TZ)
        async for m in client.iter_messages(entity, limit=200):
            if m.date is not None and m.date.astimezone(_TZ) < day_start:
                break
            if getattr(m, "out", False) and (getattr(m, "message", "") or "").strip() == text.strip():
                return m.id
        return None

    def send_message(self, text: str, dedupe: bool = False) -> SendResult:
        import asyncio
        try:
            return asyncio.run(self._send(text, dedupe))
        except Exception as exc:  # event-loop level failure; nothing was confirmed
            return SendResult("uncertain", first_line(exc))

    async def _send(self, text: str, dedupe: bool = False) -> SendResult:
        import asyncio
        from telethon import errors
        client = self._client()
        try:
            try:  # nothing has been sent while connecting / looking up the chat
                await asyncio.wait_for(client.connect(), 30)
                if not await client.is_user_authorized():
                    return SendResult("fatal", "Telegram login expired. Run: main.py --setup-telegram-login")
                entity = await asyncio.wait_for(self._find_entity(client), 90)
            except Exception as exc:
                return SendResult("not_delivered", "could not reach Telegram: " + first_line(exc))
            if entity is None:
                return SendResult("fatal", f"Your account does not see chat {self.chat_id}: wrong ID or you are not in that group.")
            if dedupe:  # never post it twice, even if no state file survived (cloud runs, reinstalls)
                try:
                    existing = await asyncio.wait_for(self._find_today_message(client, entity, text), 60)
                except Exception as exc:
                    return SendResult("not_delivered", "could not verify whether today's message exists: " + first_line(exc))
                if existing is not None:
                    return SendResult("delivered", "already posted today", message_id=existing)
            try:
                msg = await asyncio.wait_for(client.send_message(entity, text), 40)
                return SendResult("delivered", message_id=getattr(msg, "id", None))
            except (errors.FloodWaitError, errors.SlowModeWaitError) as exc:
                return SendResult("not_delivered", "rate limited: " + first_line(exc),
                                  retry_after=int(getattr(exc, "seconds", 30)))
            except errors.RPCError as exc:
                if int(getattr(exc, "code", 0) or 0) >= 500:
                    return SendResult("uncertain", "Telegram server error: " + first_line(exc))
                return SendResult("fatal", "Telegram refused: " + first_line(exc))
            except Exception as exc:  # timeout / connection lost while sending
                return SendResult("uncertain", "send outcome unknown: " + first_line(exc))
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    def preflight(self, verbose: bool = False) -> Tuple[Optional[bool], str]:
        import asyncio
        try:
            return asyncio.run(self._preflight())
        except Exception as exc:
            return None, "Could not reach Telegram: " + first_line(exc)

    async def _preflight(self) -> Tuple[Optional[bool], str]:
        import asyncio
        client = self._client()
        try:
            await asyncio.wait_for(client.connect(), 30)
            if not await client.is_user_authorized():
                return False, "Telegram login expired. Run: main.py --setup-telegram-login"
            me = await client.get_me()
            lines = [f"Logged in as: {getattr(me, 'first_name', '')} (your own account)"]
            if self.is_me:
                return True, "\n".join(lines + ["Target: your Saved Messages (test mode)"])
            entity = await asyncio.wait_for(self._find_entity(client), 90)
            if entity is None:
                return False, "\n".join(lines + [f"Chat {self.chat_id} not found in your chats: wrong ID, or your account is not in that group."])
            lines.append(f"Chat: {getattr(entity, 'title', '?')}")
            if getattr(entity, "left", False):
                return False, "\n".join(lines + ["Your account has LEFT this group."])
            is_admin = bool(getattr(entity, "creator", False) or getattr(entity, "admin_rights", None))
            default_rights = getattr(entity, "default_banned_rights", None)
            my_rights = getattr(entity, "banned_rights", None)
            if my_rights is not None and getattr(my_rights, "send_messages", False):
                return False, "\n".join(lines + ["You are restricted from sending messages in this group."])
            if default_rights is not None and getattr(default_rights, "send_messages", False) and not is_admin:
                return False, "\n".join(lines + ["Members cannot send messages in this group (admins only)."])
            if getattr(entity, "broadcast", False) and not is_admin:
                return False, "\n".join(lines + ["This is a channel: only admins can post."])
            return True, "\n".join(lines + ["You are a member and appear to be allowed to post."])
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


def build_telegram(s: Settings):
    """Return (client_or_None, list_of_missing_items)."""
    if s.tg_mode == "bot":
        token = get_secret(KEY_TOKEN, "TELEGRAM_BOT_TOKEN")
        if not token:
            return None, ["Telegram bot token not stored (run setup.bat)"]
        return Telegram(token, s.chat_id), []
    api_id = get_secret(KEY_API_ID, "TELEGRAM_API_ID")
    api_hash = get_secret(KEY_API_HASH, "TELEGRAM_API_HASH")
    session = get_secret(KEY_SESSION, "TELEGRAM_SESSION")
    if not (api_id and api_hash and session and api_id.isdigit()):
        return None, ["Telegram login not done (run: .\\.venv\\Scripts\\python.exe main.py --setup-telegram-login)"]
    return TelegramUser(int(api_id), api_hash, session, s.chat_id), []


# ----------------------------------------------------------------------------
# Table parsing (pure functions -> unit-testable without a browser)
# ----------------------------------------------------------------------------
class TableError(Exception):
    pass


class AmbiguousError(Exception):
    pass


class LoginError(Exception):
    pass


@dataclass
class FirstIn:
    status: str  # found | no_record | not_found | date_mismatch
    time: Optional[dtime] = None
    raw: str = ""
    detail: str = ""


_TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AaPp][Mm])?")
_EMPTY_CELLS = {"", "-", "--", "---", "n/a", "na", "null", "none", "nil"}


def parse_times(text: str) -> List[dtime]:
    found: List[dtime] = []
    for m in _TIME_RE.finditer(text or ""):
        hh, mm, ss, ap = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0), m.group(4)
        if ap:
            if not 1 <= hh <= 12:
                continue
            hh = hh % 12 + (12 if ap.lower() == "pm" else 0)
        if hh > 23 or mm > 59 or ss > 59:
            continue
        found.append(dtime(hh, mm, ss))
    return found


def _norm(header: str) -> str:
    return re.sub(r"[^a-z]", "", header.lower())


def parse_first_in(headers: List[str], rows: List[List[str]], code: str) -> FirstIn:
    """Return the In Time of the row where Code == code AND Movement Count == 1."""
    headers = list(headers)
    rows = [list(r) for r in rows]
    if not headers and rows and any("movement" in c.lower() for c in rows[0]):
        headers = rows.pop(0)
    norm = [_norm(h) for h in headers]
    idx = {}
    for key in ("code", "movementcount", "intimedetails"):
        if key not in norm:
            raise TableError(f"Column '{key}' not found. Headers seen: {headers}")
        idx[key] = norm.index(key)
    ncols = len(headers)
    code_re = re.compile(rf"(?<!\d){re.escape(code)}(?!\d)")

    candidates = []
    for r in rows:
        if len(r) != ncols:  # summary / total / "no data" / rowspan continuation rows
            continue
        if not code_re.search(r[idx["code"]]):
            continue
        if re.fullmatch(r"\s*0*1\s*", r[idx["movementcount"]] or ""):
            candidates.append(r)

    if not candidates:
        return FirstIn("not_found", detail=f"no row with Code={code} and Movement Count=1")

    raws = {c[idx["intimedetails"]].strip() for c in candidates}
    if len(raws) > 1:
        raise AmbiguousError(f"{len(candidates)} different first-movement rows for {code}: {sorted(raws)}")
    raw = raws.pop()

    if raw.lower() in _EMPTY_CELLS:
        return FirstIn("no_record", raw=raw, detail="In Time Details is empty")
    times = [t for t in parse_times(raw) if t != dtime(0, 0, 0)]  # 00:00:00 = "no punch" placeholder
    if not times:
        return FirstIn("no_record", raw=raw, detail=f"no time found in cell: {raw!r}")
    if len(times) > 1:
        log.warning("In Time Details of movement 1 contains several times (%r); using the first.", raw)
    return FirstIn("found", time=times[0], raw=raw)


# ----------------------------------------------------------------------------
# HRMS browser automation
# ----------------------------------------------------------------------------
_TABLE_JS = """
var t = document.getElementById('tblempmovement');
if (!t) { return null; }
var clean = function(c){ return ((c.innerText || c.textContent || '').replace(/\\u00a0/g,' ').trim()); };
var headers = [];
var hrows = t.querySelectorAll('thead tr');
if (hrows.length) { headers = Array.from(hrows[hrows.length-1].children).map(clean); }
var rows = [];
Array.from(t.rows).forEach(function(r){
    if (r.parentNode && r.parentNode.tagName === 'THEAD') { return; }
    rows.push(Array.from(r.cells).map(clean));
});
return {headers: headers, rows: rows};
"""


class HRMS:
    def __init__(self, s: Settings, password: str):
        self.s = s
        self.password = password
        self.driver = None

    # -- browser lifecycle -------------------------------------------------
    def start(self) -> None:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service

        opts = Options()
        if self.s.chrome_binary:
            opts.binary_location = self.s.chrome_binary
        if self.s.headless or (os.name != "nt" and not os.environ.get("DISPLAY")):  # servers have no screen
            opts.add_argument("--headless=new")
        if os.name != "nt":
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
        for arg in ("--window-size=1400,1000", "--disable-gpu", "--no-first-run", "--disable-extensions",
                    "--disable-notifications", "--log-level=3"):
            opts.add_argument(arg)
        opts.add_experimental_option("excludeSwitches", ["enable-logging"])
        service = Service(executable_path=self.s.chromedriver_path) if self.s.chromedriver_path else Service()
        self.driver = webdriver.Chrome(service=service, options=opts)  # fresh temp profile each run
        self.driver.set_page_load_timeout(self.s.page_timeout)
        self.driver.set_script_timeout(30)
        log.info("Chrome started (headless=%s).", self.s.headless)

    def stop(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def alive(self) -> bool:
        try:
            _ = self.driver.title  # type: ignore[union-attr]
            return True
        except Exception:
            return False

    def restart(self) -> None:
        log.warning("Restarting Chrome.")
        self.stop()
        self.start()

    def _ensure_driver(self) -> None:
        if self.driver is None:
            self.start()

    # -- helpers -----------------------------------------------------------
    def _on_login_page(self) -> bool:
        from selenium.webdriver.common.by import By
        d = self.driver
        if "auth/login" in (d.current_url or "").lower():
            return True
        return bool(d.find_elements(By.CSS_SELECTOR, "#username") and d.find_elements(By.CSS_SELECTOR, "#signin"))

    def login(self) -> None:
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        d = self.driver
        wait = WebDriverWait(d, self.s.element_timeout)
        log.info("Logging in to HRMS.")
        d.get(self.s.login_url)
        try:
            user = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "#username")))
        except TimeoutException:
            if not self._on_login_page():
                log.info("Already authenticated.")
                return
            raise
        user.clear()
        user.send_keys(self.s.username)
        pw = d.find_element(By.CSS_SELECTOR, "#password")
        pw.clear()
        pw.send_keys(self.password)
        d.find_element(By.CSS_SELECTOR, "#signin").click()
        try:
            WebDriverWait(d, self.s.element_timeout).until(lambda _d: not self._on_login_page())
        except TimeoutException as exc:
            raise LoginError("Still on the login page after Sign In. Page says: %r" % self._login_error_text()) from exc
        log.info("Login successful.")

    def _login_error_text(self) -> str:
        try:
            txt = self.driver.execute_script(
                "var sel='.alert,.text-danger,.invalid-feedback,.error,.swal2-popup,.toast,.notify';"
                "return Array.from(document.querySelectorAll(sel)).map(function(e){return (e.innerText||'').trim();})"
                ".filter(function(t){return t;}).join(' | ');") or ""
        except Exception:
            return ""
        return " ".join(str(txt).split())[:200]

    def open_movements(self) -> None:
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        self._ensure_driver()
        d = self.driver
        d.get(self.s.movements_url)
        if self._on_login_page():
            log.info("Not logged in / session expired -> logging in again.")
            self.login()
            d.get(self.s.movements_url)
            if self._on_login_page():
                raise LoginError("Redirected to the login page again after logging in.")
        try:
            WebDriverWait(d, self.s.element_timeout).until(EC.presence_of_element_located((By.ID, "tblempmovement")))
        except TimeoutException as exc:
            raise TableError("Table #tblempmovement did not appear (page slow or layout changed).") from exc
        time.sleep(self.s.settle)

    def _parse_date(self, text: str) -> Optional[date]:
        text = (text or "").strip()
        for fmt in self.s.date_formats:
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        return None

    def _date_value(self) -> str:
        from selenium.common.exceptions import NoSuchElementException
        from selenium.webdriver.common.by import By
        try:
            return self.driver.find_element(By.ID, "attendancedate").get_attribute("value") or ""
        except NoSuchElementException as exc:
            raise TableError("Field #attendancedate not found on the page.") from exc

    def _set_date(self, today: date) -> None:
        from selenium.webdriver.common.by import By
        value = today.strftime(self.s.date_input_format)
        self.driver.execute_script(
            "var e=document.getElementById('attendancedate'); e.value=arguments[0];"
            "e.dispatchEvent(new Event('input',{bubbles:true}));"
            "e.dispatchEvent(new Event('change',{bubbles:true}));"
            "if(window.jQuery){window.jQuery(e).trigger('change');}", value)
        if self.s.search_button_selector:
            self.driver.find_element(By.CSS_SELECTOR, self.s.search_button_selector).click()
        time.sleep(self.s.settle + 1)

    def ensure_date(self, today: date) -> bool:
        """The page MUST show today's attendance date before anything is read."""
        for attempt in (1, 2):
            raw = self._date_value()
            if self._parse_date(raw) == today:
                return True
            if attempt == 1:
                log.info("Page date is %r (expected %s); trying to set today's date.", raw, today.isoformat())
                self._set_date(today)
        log.warning("Could not confirm that the page shows today's date (shows %r).", self._date_value())
        return False

    def apply_filter(self) -> None:
        """If the table is a DataTables grid with a search box, filter by employee code."""
        from selenium.webdriver.common.by import By
        try:
            boxes = self.driver.find_elements(By.CSS_SELECTOR, "#tblempmovement_filter input")
            if boxes and (boxes[0].get_attribute("value") or "") != self.s.employee_code:
                boxes[0].clear()
                boxes[0].send_keys(self.s.employee_code)
                time.sleep(1)
        except Exception as exc:
            log.debug("Table search box not used: %s", first_line(exc))

    def read_table(self) -> dict:
        data = self.driver.execute_script(_TABLE_JS)
        if data is None:
            raise TableError("Table #tblempmovement not found.")
        if not data["rows"]:
            time.sleep(self.s.settle + 2)  # maybe still loading via AJAX
            data = self.driver.execute_script(_TABLE_JS) or data
        return data

    def fetch_first_in(self, today: date) -> FirstIn:
        self.open_movements()
        if not self.ensure_date(today):
            return FirstIn("date_mismatch", detail="page does not show today's date")
        self.apply_filter()
        data = self.read_table()
        return parse_first_in(data["headers"], data["rows"], self.s.employee_code)


# ----------------------------------------------------------------------------
# Main run loop
# ----------------------------------------------------------------------------
def sleep_until(target: datetime) -> None:
    while True:
        remaining = (target - now_ist()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


def run(s: Settings, args, store: StateStore, password: str, tg: Optional[Telegram]) -> int:
    today = now_ist().date()
    day = today.isoformat()
    rec = store.get(day)
    if rec.get("status") in TERMINAL and not args.ignore_state:
        log.info("Today (%s) is already handled: status=%s. Nothing to do.", day, rec.get("status"))
        return 0

    def at(t: dtime) -> datetime:
        return datetime.combine(today, t, tzinfo=s.tz)

    start_dt, end_dt = at(s.start), at(s.end)
    check_deadline = end_dt + timedelta(minutes=s.grace)
    send_deadline = end_dt + timedelta(minutes=s.retry_extra)

    def save(**fields) -> None:
        if not args.dry_run:
            store.update(day, **fields)

    if not args.once:
        if now_ist() > check_deadline:
            log.info("Window (%s-%s) is over for today; exiting.", s.start.strftime("%H:%M"), s.end.strftime("%H:%M"))
            return 0
        if now_ist() < start_dt:
            log.info("Waiting until %s IST to start checking.", s.start.strftime("%H:%M"))
            sleep_until(start_dt)

    hrms = HRMS(s, password)
    qualified: Optional[FirstIn] = None
    checks = errors = login_failures = 0
    log.info("Monitoring first IN of employee %s for %s (dry_run=%s).", s.employee_code, day, args.dry_run)
    try:
        while True:
            now = now_ist()
            if now.date() != today:
                log.info("Date changed; stopping.")
                return 0
            limit = send_deadline if qualified else check_deadline
            if not args.once and now > limit:
                log.info("Stopped: %s", "IN found but message not delivered in time" if qualified
                         else "no qualifying first IN found in the window")
                return 0

            wait_s = s.interval
            # ---- 1. read HRMS --------------------------------------------------
            if qualified is None:
                checks += 1
                res: Optional[FirstIn] = None
                try:
                    res = hrms.fetch_first_in(today)
                    errors = login_failures = 0
                except LoginError as exc:
                    login_failures += 1
                    if s.login_requires_punch:
                        log.info("Check #%d: HRMS login refused - expected until the IN punch is recorded (%s)",
                                 checks, first_line(exc))
                    else:
                        log.error("Login problem (%d/3): %s", login_failures, first_line(exc))
                        if login_failures >= 3:
                            log.critical("Stopping to avoid locking the HRMS account. Check the stored password.")
                            return 2
                except (TableError, AmbiguousError) as exc:
                    errors += 1
                    log.error("Page problem: %s", first_line(exc))
                except Exception as exc:  # WebDriverException, timeouts, network drops ...
                    errors += 1
                    log.warning("Check #%d failed (%d in a row): %s", checks, errors, first_line(exc))
                    if hrms.driver is not None and not hrms.alive():
                        try:
                            hrms.restart()
                        except Exception as exc2:
                            log.error("Chrome restart failed: %s", first_line(exc2))
                            hrms.driver = None

                if res is not None:
                    if res.status == "found":
                        t = res.time
                        log.info("Check #%d: first IN today = %s", checks, t.strftime("%H:%M:%S"))
                        if t < s.start:
                            save(status="skipped_early", first_in=t.strftime("%H:%M:%S"))
                            log.info("First IN is before %s: no message today.", s.start.strftime("%H:%M"))
                            return 0
                        if t > s.end:
                            save(status="skipped_late", first_in=t.strftime("%H:%M:%S"))
                            log.info("First IN is after %s: no message today.", s.end.strftime("%H:%M"))
                            return 0
                        qualified = res
                    else:
                        log.info("Check #%d: %s - %s", checks, res.status, res.detail)

            # ---- 2. send -------------------------------------------------------
            if qualified is not None:
                if args.dry_run:
                    log.info("DRY-RUN: first IN %s qualifies -> a message WOULD be sent now. Nothing sent, nothing saved.",
                             qualified.time.strftime("%H:%M:%S"))
                    return 0
                if now_ist() > send_deadline:
                    log.warning("Qualifying IN found but too late to send (after %s).", send_deadline.strftime("%H:%M"))
                    return 0
                current = store.get(day).get("status")
                if current in TERMINAL and not args.ignore_state:
                    log.warning("State changed to %r meanwhile; not sending.", current)
                    return 0
                attempts = int(store.get(day).get("attempts", 0)) + 1
                # Mark BEFORE the HTTP call: if we crash after this line we will never double-send.
                store.update(day, status="pending", first_in=qualified.time.strftime("%H:%M:%S"), attempts=attempts)
                result = tg.send_message(MESSAGE, dedupe=True)  # type: ignore[union-attr]
                if result.outcome == "delivered":
                    store.update(day, status="sent", sent_at=now_ist().isoformat(timespec="seconds"),
                                 message_id=result.message_id)
                    if result.detail:
                        log.info("Today's Good Morning message is already in the chat (message_id=%s); nothing new sent. "
                                 "Done for today.", result.message_id)
                    else:
                        log.info("Telegram message DELIVERED (message_id=%s). Done for today.", result.message_id)
                    return 0
                if result.outcome == "not_delivered":
                    store.update(day, status="retrying", last_error=result.detail)
                    log.warning("Message NOT delivered (safe to retry): %s", result.detail)
                    wait_s = max(result.retry_after, 15)
                    if args.once:
                        return 1
                elif result.outcome == "uncertain":
                    store.update(day, status="uncertain", last_error=result.detail)
                    log.error("Delivery UNCERTAIN: %s. Not retrying automatically to avoid a duplicate. "
                              "Check the group; see README 'Uncertain status'.", result.detail)
                    return 1
                else:  # fatal: bad token / bot not in group / chat id wrong ...
                    store.update(day, status="retrying", last_error=result.detail)
                    log.error("Telegram refused the message: %s", result.detail)
                    return 2

            if args.once:
                log.info("--once: single check finished (no qualifying first IN yet).")
                return 0
            remaining = (limit - now_ist()).total_seconds()
            if remaining > 0:
                time.sleep(min(wait_s, remaining + 1))
    finally:
        hrms.stop()


# ----------------------------------------------------------------------------
# Diagnostic commands
# ----------------------------------------------------------------------------
def inspect(s: Settings, password: str) -> int:
    from selenium.webdriver.common.by import By
    hrms = HRMS(s, password)
    try:
        hrms.open_movements()
        today = now_ist().date()
        ok = hrms.ensure_date(today)
        hrms.apply_filter()
        data = hrms.read_table()
        buttons = hrms.driver.execute_script(
            "return Array.from(document.querySelectorAll('button,input[type=button],input[type=submit],a.btn'))"
            ".map(function(e){return e.outerHTML.slice(0,200);});")
        shot = LOG_DIR / "inspect.png"
        hrms.driver.save_screenshot(str(shot))
        out = LOG_DIR / "inspect_table.txt"
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(f"date field value : {hrms._date_value()!r}  (today={today}, matches={ok})\n")
            fh.write(f"headers          : {data['headers']}\n")
            fh.write(f"row count        : {len(data['rows'])}\n\n")
            for i, row in enumerate(data["rows"]):
                fh.write(f"{i:03d} ({len(row)} cells): {row}\n")
            fh.write("\nButtons found on page:\n" + "\n".join(buttons) + "\n")
        print(f"Date on page    : {hrms._date_value()!r} -> matches today: {ok}")
        print(f"Headers         : {data['headers']}")
        print(f"Rows            : {len(data['rows'])}")
        try:
            print("Parse result    :", parse_first_in(data["headers"], data["rows"], s.employee_code))
        except Exception as exc:
            print("Parse problem   :", first_line(exc))
        print(f"\nFull dump: {out}\nScreenshot: {shot}\n(These files may contain other employees' data - keep them private.)")
        return 0
    finally:
        hrms.stop()


def check_config(s: Settings, password: Optional[str]) -> int:
    problems = s.problems(need_telegram=True)
    if not password:
        problems.append("HRMS password is not stored (run setup.bat / --setup-credentials)")
    _, missing = build_telegram(s)
    problems += missing
    if problems:
        print("CONFIGURATION PROBLEMS:")
        for p in problems:
            print("  -", p)
        return 2
    print(f"Configuration OK (telegram mode: {s.tg_mode}; secrets found; values not shown).")
    return 0


def check_telegram(s: Settings) -> int:
    problems = s.problems(need_telegram=True)
    tg, missing = build_telegram(s)
    problems += missing
    if problems or tg is None:
        for p in problems:
            print("-", p)
        return 2
    lock = SingleInstance(LOCK_PATH)
    if not lock.acquire():
        print("The automation is running right now. Wait until it finishes, then run this again.")
        return 2
    ok, msg = tg.preflight(verbose=True)
    print(redact(msg))
    print("RESULT:", {True: "OK - can post to the target chat (nothing was sent)", False: "NOT OK",
                      None: "UNKNOWN (network)"}[ok])
    return 0 if ok else 2


def test_send(s: Settings) -> int:
    """Sends ONE clearly-marked test message, and only to your own Saved Messages (chat_id = me)."""
    if s.tg_mode != "user" or s.chat_id.strip().lower() != "me":
        print("--test-send only works when config.ini has  mode = user  and  chat_id = me")
        print("(so a test can never land in the company group).")
        return 2
    tg, missing = build_telegram(s)
    if missing or tg is None:
        for p in missing:
            print("-", p)
        return 2
    lock = SingleInstance(LOCK_PATH)
    if not lock.acquire():
        print("The automation is running right now; try again later.")
        return 2
    res = tg.send_message("Test message from Telegram_GoodMorning - please ignore.")
    print("Result:", res.outcome, redact(res.detail))
    return 0 if res.outcome == "delivered" else 2


# ----------------------------------------------------------------------------
# Offline self-test (no browser, no Telegram, no network)
# ----------------------------------------------------------------------------
def selftest() -> int:
    results: List[bool] = []

    def check(name: str, cond: bool) -> None:
        results.append(bool(cond))
        print(("PASS  " if cond else "FAIL  ") + name)

    H = ["Code", "Name", "Recruiter", "Designation", "Movement Count", "Out Hrs",
         "In Time Details", "Out Time Details", "In Hrs"]
    rows = [
        ["18933", "Someone Else", "", "", "1", "", "07:30:00 AM", "", ""],
        ["8933", "Me", "R", "Dev", "1", "", "08:52:10 AM", "12:30:00 PM", "3:38"],
        ["", "", "", "", "2", "", "01:15:00 PM", "", ""],
        ["8933", "Me", "R", "Dev", "3", "", "03:40:00 PM", "", ""],
        ["Total", "", "", "", "", "", "", "", "6:10"],
        ["4", "01:50 PM"],
    ]
    r = parse_first_in(H, rows, "8933")
    check("movement 1 of 8933 is used (not 18933, not later IN)", r.status == "found" and r.time == dtime(8, 52, 10))
    r = parse_first_in(H, [rows[0], rows[2], rows[3], rows[4]], "8933")
    check("employee/movement-1 row missing -> not_found", r.status == "not_found")
    r2 = [list(rows[1])]; r2[0][6] = ""
    check("empty In Time -> no_record", parse_first_in(H, r2, "8933").status == "no_record")
    r2[0][6] = "00:00:00"
    check("00:00:00 placeholder -> no_record", parse_first_in(H, r2, "8933").status == "no_record")
    r2[0][6] = "09:05"
    check("24-hour value parsed", parse_first_in(H, r2, "8933").time == dtime(9, 5))
    r2[0][6] = "11:00"
    t = parse_first_in(H, r2, "8933").time
    check("11:00 is inclusive", dtime(8, 0) <= t <= dtime(11, 0))
    r2[0][6] = "11:00:01 AM"
    t = parse_first_in(H, r2, "8933").time
    check("11:00:01 is outside the window", not (dtime(8, 0) <= t <= dtime(11, 0)))
    r2[0][6] = "12:10 PM"
    check("12:10 PM -> 12:10", parse_first_in(H, r2, "8933").time == dtime(12, 10))
    dup = [list(rows[1]), list(rows[1])]; dup[1][6] = "09:00:00 AM"
    try:
        parse_first_in(H, dup, "8933"); check("conflicting duplicate rows rejected", False)
    except AmbiguousError:
        check("conflicting duplicate rows rejected", True)
    try:
        parse_first_in(["A", "B"], rows, "8933"); check("missing columns rejected", False)
    except TableError:
        check("missing columns rejected", True)

    with tempfile.TemporaryDirectory() as tmp:
        st = StateStore(Path(tmp) / "state.json")
        st.update("2026-01-01", status="pending")
        check("pending survives reload", StateStore(Path(tmp) / "state.json").get("2026-01-01")["status"] == "pending")
        st.update("2026-01-01", status="sent")
        check("sent is persisted", st.get("2026-01-01")["status"] == "sent" and "sent" in TERMINAL)
        (Path(tmp) / "state.json").write_text("{broken", encoding="utf-8")
        check("corrupt file recovered from backup", st.get("2026-01-01").get("status") in ("pending", "sent"))
        st.bak.write_text("{broken", encoding="utf-8")
        try:
            st.load(); check("corrupt file + corrupt backup refuses to continue", False)
        except StateCorruptError:
            check("corrupt file + corrupt backup refuses to continue", True)

    _SECRETS.add("SuperSecret123")
    rec = logging.makeLogRecord({"msg": "x /bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456/sendMessage pw=SuperSecret123",
                                 "levelname": "INFO"})
    out = RedactingFormatter("%(message)s").format(rec)
    check("token and password redacted in logs", "ABCdef" not in out and "SuperSecret123" not in out)
    _SECRETS.discard("SuperSecret123")

    try:
        sess = requests.Session(); sess.trust_env = False
        sess.get("http://127.0.0.1:9/", timeout=3)
        print("SKIP  connection-refused classification (port 9 unexpectedly open)")
    except requests.exceptions.ConnectionError as exc:
        check("'cannot connect' is classified as safe-to-retry", failed_before_send(exc))
    except Exception:
        print("SKIP  connection-refused classification")
    check("read timeout is NOT classified as safe-to-retry", not failed_before_send(requests.exceptions.ReadTimeout("x")))

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Send a Good Morning message after the first HRMS IN punch.")
    p.add_argument("--once", action="store_true", help="do one check and exit (ignores the waiting window)")
    p.add_argument("--dry-run", action="store_true", help="never send to Telegram, never write the state file")
    p.add_argument("--ignore-state", action="store_true", help="ignore today's saved status (testing only)")
    p.add_argument("--headed", action="store_true", help="force a visible Chrome window")
    p.add_argument("--inspect", action="store_true", help="open the page, dump the table + screenshot to logs/")
    p.add_argument("--check-config", action="store_true", help="validate config and stored secrets (no network)")
    p.add_argument("--check-telegram", action="store_true", help="verify bot token/group/permissions WITHOUT sending")
    p.add_argument("--setup-telegram-login", action="store_true", help="one-time login of YOUR Telegram account")
    p.add_argument("--export-telegram-session", action="store_true", help="make a NEW separate Telegram login and print it (for GitHub secrets)")
    p.add_argument("--test-send", action="store_true", help="send one test message to your Saved Messages (chat_id = me)")
    p.add_argument("--setup-credentials", action="store_true", help="store HRMS password + bot token securely")
    p.add_argument("--delete-credentials", action="store_true", help="remove stored secrets")
    p.add_argument("--selftest", action="store_true", help="offline tests of parser, state file, redaction")
    return p.parse_args(argv)


def _main(args) -> int:
    if args.selftest:
        return selftest()
    try:
        s = Settings(CONFIG_PATH)
    except ConfigError as exc:
        print(redact(str(exc)))
        log.error("%s", exc)
        return 2
    set_tz(s.tz)
    if args.headed:
        s.headless = False
    if args.setup_credentials:
        return setup_credentials(s.tg_mode)
    if args.setup_telegram_login:
        return setup_telegram_login()
    if args.export_telegram_session:
        return setup_telegram_login(export=True)
    if args.delete_credentials:
        return delete_credentials()

    password = s.config_password or get_secret(KEY_HRMS, "HRMS_PASSWORD")
    if s.config_password:
        _SECRETS.add(s.config_password)  # keep it out of the logs
    if args.check_config:
        return check_config(s, password)
    if args.check_telegram:
        return check_telegram(s)
    if args.test_send:
        return test_send(s)

    need_tg = not args.dry_run and not args.inspect
    problems = s.problems(need_telegram=need_tg)
    if not password:
        problems.append("HRMS password not stored (run setup.bat)")
    tg = None
    if need_tg:
        tg, missing = build_telegram(s)
        problems += missing
    if problems:
        for p in problems:
            log.error("Config problem: %s", p)
        return 2
    if args.inspect:
        return inspect(s, password)

    lock = None
    if not args.dry_run:
        lock = SingleInstance(LOCK_PATH)
        if not lock.acquire():
            log.info("Another instance is already running; exiting.")
            return 0

    store = StateStore(STATE_PATH)
    store.load()  # fail early if the state file is corrupt
    if tg is not None:
        ok, msg = tg.preflight()
        if ok is False:
            log.error("Telegram pre-check failed: %s", msg.replace("\n", " | "))
            return 2
        log.info("Telegram pre-check (%s mode): %s", s.tg_mode, "OK" if ok else "skipped (network unreachable right now)")
    return run(s, args, store, password, tg)  # type: ignore[arg-type]


def main() -> int:
    args = parse_args()
    setup_logging()
    load_env_file()
    try:
        return _main(args)
    except StateCorruptError as exc:
        log.critical("%s", exc)
        return 3
    except KeyboardInterrupt:
        log.warning("Interrupted by user.")
        return 130
    except Exception:
        log.exception("Unhandled error")
        return 1


if __name__ == "__main__":
    sys.exit(main())

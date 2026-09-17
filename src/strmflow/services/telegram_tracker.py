from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
from binascii import Error as BinasciiError
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from telethon import TelegramClient, events
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession

from strmflow.core.config import Settings
from strmflow.core.errors import AppError
from strmflow.core.runtime_logs import RuntimeLogStore
from strmflow.core.security import resolve_session_key
from strmflow.repositories.runtime_settings import RuntimeSettingsRepository
from strmflow.schemas.api import TelegramConfigUpdate
from strmflow.services.bdpan_automation import BdpanAutomationService
from strmflow.services.media import MediaService

BAIDU_LINK = re.compile(
    r"https?://pan\.baidu\.com/s/[A-Za-z0-9_-]+(?:\?[^\s<>()]+)?", re.IGNORECASE
)
EPISODE_HINT = re.compile(r"(?:更新(?:至)?|更至|第)\s*0*(\d{1,4})\s*集", re.IGNORECASE)
SEASON_HINT = re.compile(
    r"(?:第\s*([一二三四五六七八九十百\d]+)\s*季|Season\s*0*(\d{1,2})|S0*(\d{1,2}))", re.IGNORECASE
)
YEAR_HINT = re.compile(r"[（(](20\d{2})[）)]")
QUALITY_NOISE = re.compile(
    r"(?i)(?:\b(?:4k|8k|2160p|1080p|hdr|hq|臻彩|超清|高清)\b|动漫|动画|电视剧|电影|国漫|日番)"
)


@dataclass(frozen=True, slots=True)
class TelegramMediaCandidate:
    heading: str
    baidu_link: str
    episode: int | None
    season: int | None
    year: str


class TelegramTrackerService:
    """Receive MTProto updates and turn matched announcements into bdpan checks."""

    def __init__(
        self,
        settings: Settings,
        repository: RuntimeSettingsRepository,
        media: MediaService,
        bdpan: BdpanAutomationService,
        runtime_logs: RuntimeLogStore,
    ) -> None:
        self.api_id = settings.telegram_api_id
        self.api_hash = settings.telegram_api_hash.get_secret_value().strip()
        encryption_key = hashlib.sha256(
            b"strmflow-telegram-session-v1\0" + resolve_session_key(settings)
        ).digest()
        self._session_cipher = AESGCM(encryption_key)
        self.repository = repository
        self.media = media
        self.bdpan = bdpan
        self.runtime_logs = runtime_logs
        self.config = self._default_config()
        self.client: TelegramClient | None = None
        self._runner: asyncio.Task[None] | None = None
        self._pending_phone = ""
        self._phone_code_hash = ""
        self._last_received_at = ""
        self._last_match = ""
        self._last_error = ""
        self._processed: list[str] = []

    async def initialize(self) -> None:
        stored = await self.repository.load_telegram()
        if stored:
            try:
                self.config = self._normalize_config(stored)
                self._processed = [str(value) for value in stored.get("processed") or []][-500:]
                if any(key in stored for key in ("apiId", "apiHash", "session")):
                    await self._save()
                    self._log("info", "已清理旧版 Telegram API 凭据并加密会话")
            except (TypeError, ValueError):
                self._log("warning", "已忽略格式不正确的 Telegram 运行配置")
        if self.config["enabled"] and self._credentials_ready() and self.config["session"]:
            await self._connect_saved_session()
        self._log(
            "success",
            "Telegram 实时追更服务已初始化",
            enabled=self.config["enabled"],
            sourceCount=len(self.config["sources"]),
            authorized=self._authorized(),
        )

    async def close(self) -> None:
        await self._disconnect()

    async def status(self) -> dict[str, Any]:
        return {
            "config": {
                "enabled": self.config["enabled"],
                "apiConfigured": self._credentials_ready(),
                "phoneConfigured": bool(self.config["phone"]),
                "phoneMasked": self._mask_phone(self.config["phone"]),
                "sources": list(self.config["sources"]),
            },
            "runtime": {
                "connected": bool(self.client and self.client.is_connected()),
                "authorized": self._authorized(),
                "listening": bool(self._runner and not self._runner.done()),
                "lastReceivedAt": self._last_received_at or None,
                "lastMatch": self._last_match,
                "lastError": self._last_error,
            },
        }

    async def update_config(self, body: TelegramConfigUpdate) -> dict[str, Any]:
        previous = dict(self.config)
        updated = self._normalize_config(body.model_dump(by_alias=True))
        if updated["enabled"] and not self._credentials_ready():
            raise AppError(
                409,
                "请先在 .env 中配置 TELEGRAM_API_ID 和 TELEGRAM_API_HASH 并重启服务",
            )
        if updated["phone"] != previous["phone"]:
            updated["session"] = ""
        else:
            updated["session"] = previous["session"]
        self.config = updated
        self._last_error = ""
        await self._disconnect()
        await self._save()
        if self.config["enabled"] and self.config["session"]:
            await self._connect_saved_session()
        self._log(
            "success",
            "Telegram 实时追更配置已更新",
            enabled=self.config["enabled"],
            sourceCount=len(self.config["sources"]),
        )
        return await self.status()

    async def start_login(self, phone: str) -> dict[str, Any]:
        if not self._credentials_ready():
            raise AppError(
                409,
                "请先在 .env 中配置 TELEGRAM_API_ID 和 TELEGRAM_API_HASH 并重启服务",
            )
        normalized_phone = self._normalize_phone(phone)
        await self._disconnect()
        self.client = self._new_client("")
        try:
            await self.client.connect()
            sent = await self.client.send_code_request(normalized_phone)
        except Exception as exc:
            await self._disconnect()
            raise AppError(502, f"Telegram 验证码发送失败：{str(exc)[:300]}") from exc
        self._pending_phone = normalized_phone
        self._phone_code_hash = str(sent.phone_code_hash)
        self._last_error = ""
        self._log("info", "Telegram 登录验证码已发送", phone=self._mask_phone(normalized_phone))
        return {"codeSent": True, "phone": self._mask_phone(normalized_phone)}

    async def complete_login(self, code: str, password: str = "") -> dict[str, Any]:
        if not self.client or not self.client.is_connected() or not self._pending_phone:
            raise AppError(409, "请先发送 Telegram 登录验证码")
        if not str(code).strip() and not password:
            raise AppError(400, "请填写 Telegram 登录验证码")
        try:
            if password:
                await self.client.sign_in(password=password)
            else:
                await self.client.sign_in(
                    phone=self._pending_phone,
                    code=str(code).strip(),
                    phone_code_hash=self._phone_code_hash,
                )
        except SessionPasswordNeededError:
            return {"authorized": False, "passwordRequired": True}
        except PhoneCodeInvalidError as exc:
            raise AppError(400, "Telegram 验证码不正确") from exc
        except Exception as exc:
            raise AppError(502, f"Telegram 登录失败：{str(exc)[:300]}") from exc
        self.config["phone"] = self._pending_phone
        self.config["session"] = StringSession.save(self.client.session)
        self._last_error = ""
        self._pending_phone = ""
        self._phone_code_hash = ""
        await self._save()
        if self.config["enabled"]:
            await self._start_listener()
        else:
            await self._disconnect()
        self._log("success", "Telegram 用户账号授权完成")
        return {"authorized": True, "passwordRequired": False}

    async def logout(self) -> dict[str, Any]:
        client = self.client
        if client and client.is_connected():
            try:
                await client.log_out()
            except Exception as exc:  # noqa: BLE001 - local session is still cleared
                self._log("warning", f"Telegram 远程退出失败，已清理本地会话：{str(exc)[:200]}")
        await self._disconnect()
        self.config["session"] = ""
        self._last_error = ""
        await self._save()
        self._log("success", "Telegram 用户账号已退出")
        return await self.status()

    async def _connect_saved_session(self) -> None:
        await self._disconnect()
        self.client = self._new_client(self.config["session"])
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                self.config["session"] = ""
                await self._save()
                await self._disconnect()
                return
            await self._start_listener()
            self._last_error = ""
        except Exception as exc:  # noqa: BLE001 - service startup must remain available
            self._last_error = str(exc)[:500]
            await self._disconnect()
            self._log("error", f"Telegram 连接失败：{self._last_error}")

    async def _start_listener(self) -> None:
        if not self.client:
            return
        self.client.add_event_handler(self._on_message, events.NewMessage())
        self.client.add_event_handler(self._on_message, events.MessageEdited())
        self._runner = asyncio.create_task(
            self.client.run_until_disconnected(), name="telegram-mtproto-listener"
        )

    async def _disconnect(self) -> None:
        runner, client = self._runner, self.client
        self._runner = None
        self.client = None
        if client and client.is_connected():
            await client.disconnect()
        if runner and runner is not asyncio.current_task() and not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)

    def _new_client(self, session: str) -> TelegramClient:
        return TelegramClient(
            StringSession(session),
            self.api_id,
            self.api_hash,
            sequential_updates=True,
        )

    async def _on_message(self, event: Any) -> None:
        try:
            source = await self._event_source(event)
            if not source:
                return
            text = str(getattr(event, "raw_text", "") or "")
            message_id = int(getattr(event, "id", 0) or 0)
            chat_id = str(getattr(event, "chat_id", "") or "")
            fingerprint = hashlib.sha256(f"{chat_id}\0{message_id}\0{text}".encode()).hexdigest()
            if fingerprint in self._processed:
                return
            self._last_received_at = self.bdpan._iso_now()
            matched = await self.process_message(text, source=source)
            self._processed = [*self._processed[-499:], fingerprint]
            await self._save()
            self._log(
                "info",
                "Telegram 消息处理完成",
                source=source,
                candidateCount=len(self.parse_message(text)),
                matchedCount=matched,
            )
        except Exception as exc:  # noqa: BLE001 - one malformed message must not stop listener
            self._last_error = str(exc)[:500]
            self._log("error", f"Telegram 消息处理失败：{self._last_error}")

    async def _event_source(self, event: Any) -> str:
        if not self.config["enabled"] or self.bdpan.config.get("trackingMode") != "hybrid":
            return ""
        chat = await event.get_chat()
        username = str(getattr(chat, "username", "") or "").casefold()
        chat_id = str(getattr(event, "chat_id", "") or "")
        allowed = set(self.config["sources"])
        if username and f"@{username}" in allowed:
            return f"@{username}"
        if chat_id in allowed:
            return chat_id
        return ""

    async def process_message(self, text: str, *, source: str) -> int:
        candidates = self.parse_message(text)
        if not candidates:
            return 0
        items = [item for item in await self.media.list_items() if item.get("status") == "ongoing"]
        matched = 0
        for candidate in candidates:
            item = self._match_item(candidate, items)
            if not item:
                continue
            link_changed = str(item.get("baiduLink") or "") != candidate.baidu_link
            if link_changed:
                item = await self.media.update_item(item["id"], {"baiduLink": candidate.baidu_link})
            await self.bdpan.telegram_update(
                item,
                link_changed=link_changed,
                source=source,
                episode=candidate.episode,
            )
            matched += 1
            self._last_match = f"{item['name']} · {source}"
        return matched

    @classmethod
    def parse_message(cls, text: str) -> list[TelegramMediaCandidate]:
        lines = [line.strip() for line in str(text).replace("\r", "\n").split("\n")]
        candidates: list[TelegramMediaCandidate] = []
        heading = ""
        links: list[str] = []

        def flush() -> None:
            nonlocal heading, links
            baidu = next(
                (cls._normalize_baidu_link(value) for value in links if BAIDU_LINK.match(value)), ""
            )
            if heading and baidu:
                episode_match = EPISODE_HINT.search(heading)
                season_match = SEASON_HINT.search(heading)
                season = None
                if season_match:
                    raw = next(value for value in season_match.groups() if value)
                    season = cls._season_number(raw)
                year_match = YEAR_HINT.search(heading)
                candidates.append(
                    TelegramMediaCandidate(
                        heading=heading,
                        baidu_link=baidu,
                        episode=int(episode_match.group(1)) if episode_match else None,
                        season=season,
                        year=year_match.group(1) if year_match else "",
                    )
                )
            heading, links = "", []

        for line in [*lines, ""]:
            if not line:
                if links:
                    flush()
                continue
            urls = re.findall(r"https?://[^\s<>()]+", line)
            if urls:
                links.extend(urls)
                continue
            if links:
                flush()
            heading = line
        return candidates

    @classmethod
    def _match_item(
        cls, candidate: TelegramMediaCandidate, items: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        heading = cls._normalize_title(candidate.heading)
        matches: list[tuple[int, dict[str, Any]]] = []
        for item in items:
            title = cls._normalize_title(str(item.get("title") or item.get("name") or ""))
            if not title or title not in heading:
                continue
            if candidate.season and int(item.get("season") or 1) != candidate.season:
                continue
            if candidate.year and str(item.get("year") or "") not in {"", candidate.year}:
                continue
            matches.append((len(title), item))
        if not matches:
            return None
        matches.sort(key=lambda value: value[0], reverse=True)
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            return None
        return matches[0][1]

    @staticmethod
    def _normalize_title(value: str) -> str:
        value = EPISODE_HINT.sub("", value)
        value = SEASON_HINT.sub("", value)
        value = YEAR_HINT.sub("", value)
        value = QUALITY_NOISE.sub("", value)
        return re.sub(r"[^\w\u4e00-\u9fff]+", "", value, flags=re.UNICODE).casefold()

    @staticmethod
    def _normalize_baidu_link(value: str) -> str:
        parsed = urlsplit(value.rstrip(".,，。"))
        query = parse_qs(parsed.query)
        pwd = next(iter(query.get("pwd") or []), "")
        return urlunsplit(("https", "pan.baidu.com", parsed.path, f"pwd={pwd}" if pwd else "", ""))

    @staticmethod
    def _season_number(value: str) -> int | None:
        if value.isdigit():
            return int(value)
        digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        if value == "十":
            return 10
        if "十" in value:
            left, right = value.split("十", 1)
            return (digits.get(left, 1) * 10) + digits.get(right, 0)
        return digits.get(value)

    def _default_config(self) -> dict[str, Any]:
        return {
            "enabled": False,
            "phone": "",
            "sources": [],
            "session": "",
        }

    def _normalize_config(self, value: dict[str, Any]) -> dict[str, Any]:
        sources = [self._normalize_source(source) for source in value.get("sources") or []]
        sources = list(dict.fromkeys(source for source in sources if source))[:100]
        encrypted_session = str(value.get("sessionEncrypted") or "")
        session = (
            self._decrypt_session(encrypted_session)
            if encrypted_session
            else str(value.get("session") or "")
        )
        return {
            "enabled": bool(value.get("enabled", False)),
            "phone": self._normalize_phone(
                str(value.get("phone") or self.config.get("phone") or "")
            ),
            "sources": sources,
            "session": session,
        }

    async def _save(self) -> None:
        await self.repository.save_telegram(
            {
                "enabled": self.config["enabled"],
                "phone": self.config["phone"],
                "sources": list(self.config["sources"]),
                "sessionEncrypted": self._encrypt_session(str(self.config.get("session") or "")),
                "processed": self._processed[-500:],
            }
        )

    def _encrypt_session(self, value: str) -> str:
        if not value:
            return ""
        nonce = os.urandom(12)
        encrypted = self._session_cipher.encrypt(nonce, value.encode(), None)
        return "v1." + base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")

    def _decrypt_session(self, value: str) -> str:
        try:
            version, encoded = value.split(".", 1)
            if version != "v1":
                raise ValueError("unsupported version")
            payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if len(payload) <= 28:
                raise ValueError("invalid encrypted session")
            return self._session_cipher.decrypt(payload[:12], payload[12:], None).decode()
        except (BinasciiError, InvalidTag, UnicodeError, ValueError) as exc:
            raise ValueError("Telegram 加密会话读取失败") from exc

    def _credentials_ready(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def _authorized(self) -> bool:
        return bool(self.config["session"])

    @staticmethod
    def _normalize_source(value: object) -> str:
        source = str(value or "").strip().rstrip("/")
        source = re.sub(r"^https?://t\.me/", "@", source, flags=re.IGNORECASE)
        if re.fullmatch(r"-?\d+", source):
            return source
        source = source.lstrip("@").casefold()
        return f"@{source}" if re.fullmatch(r"[a-z0-9_]{5,32}", source) else ""

    @staticmethod
    def _normalize_phone(value: str) -> str:
        phone = re.sub(r"[^\d+]", "", str(value or "").strip())
        if phone and not phone.startswith("+"):
            phone = "+" + phone
        return phone[:40]

    @staticmethod
    def _mask_phone(value: str) -> str:
        phone = str(value or "")
        return phone[:3] + "****" + phone[-3:] if len(phone) > 8 else ("已配置" if phone else "")

    def _log(self, level: str, message: str, **details: Any) -> None:
        self.runtime_logs.add(category="telegram", level=level, message=message, **details)

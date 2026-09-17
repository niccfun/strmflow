from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from strmflow.core.config import Settings
from strmflow.core.errors import AppError

ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
AUTH_URL = re.compile(r"https://[^\s\x1b]+")
SHARE_URL = re.compile(r"https?://pan\.baidu\.com/s/[A-Za-z0-9_-]+[^\s，,；;]*", re.IGNORECASE)
EXTRACT_CODE = re.compile(r"(?i)(?:提取码|密码|pwd|code)\s*[：:=]?\s*([A-Za-z0-9]{4})")


@dataclass(frozen=True, slots=True)
class BdpanRunResult:
    return_code: int
    stdout: str
    stderr: str
    payload: Any = None


class BdpanCliError(RuntimeError):
    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        self.code = code


class BdpanCli:
    """Small, non-shell adapter for the official bdpan CLI."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.http = http

    @staticmethod
    def executable(binary: str) -> str | None:
        value = str(binary or "bdpan").strip()
        if not value:
            return None
        if "/" in value:
            path = Path(value).expanduser()
            return str(path) if path.is_file() and path.stat().st_mode & 0o111 else None
        return shutil.which(value)

    @staticmethod
    def parse_share_input(value: str, extract_code: str = "") -> tuple[str, str]:
        text = str(value or "").strip()
        match = SHARE_URL.search(text)
        if not match:
            raise AppError(400, "百度网盘分享链接格式不正确")
        url = match.group(0).rstrip(".)]】。！？!")
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        code = str(extract_code or "").strip()
        if not code:
            code = next(
                (
                    str(values[0]).strip()
                    for key, values in query.items()
                    if key.casefold() == "pwd" and values
                ),
                "",
            )
        if not code:
            code_match = EXTRACT_CODE.search(text)
            code = code_match.group(1) if code_match else ""
        if code and not re.fullmatch(r"[A-Za-z0-9]{4}", code):
            raise AppError(400, "百度网盘提取码需要是 4 位字母或数字")
        clean_query = urlencode(
            [
                (key, item)
                for key, values in query.items()
                if key.casefold() != "pwd"
                for item in values
            ]
        )
        clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, clean_query, ""))
        return clean_url, code

    @staticmethod
    def normalize_destination(value: str) -> str:
        raw = str(value or "").strip().replace("\\", "/")
        prefix = "/apps/bdpan/"
        if raw == "/apps/bdpan":
            return ""
        raw = raw.removeprefix(prefix)
        raw = raw.strip("/")
        path = PurePosixPath(raw)
        if not raw or raw.startswith("~") or any(part in {"", ".", ".."} for part in path.parts):
            if not raw:
                return ""
            raise AppError(400, "bdpan 转存目录必须位于 /apps/bdpan/ 内")
        if any("\x00" in part for part in path.parts) or len(raw) > 700:
            raise AppError(400, "bdpan 转存目录格式不正确")
        return path.as_posix()

    def transfer_command(
        self,
        share_url: str,
        destination: str,
        extract_code: str = "",
        *,
        binary: str | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        url, code = self.parse_share_input(share_url, extract_code)
        target = self.normalize_destination(destination)
        args = ["transfer", url]
        if code:
            args.extend(["-p", code])
        if target:
            args.extend(["-d", target])
        return self.command(args, binary=binary, json_output=True, session_id=session_id)

    def select_command(
        self,
        share_url: str,
        fsids: list[str],
        destination: str,
        extract_code: str = "",
        *,
        binary: str | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        url, code = self.parse_share_input(share_url, extract_code)
        ids = [str(value) for value in fsids if re.fullmatch(r"[1-9]\d*", str(value))]
        if not ids or len(ids) != len(fsids):
            raise AppError(400, "分享文件标识格式不正确")
        target = self.normalize_destination(destination)
        args = ["transfer", "select", url, "--fsid", ",".join(ids)]
        if code:
            args.extend(["-p", code])
        if target:
            args.extend(["-d", target])
        return self.command(args, binary=binary, json_output=True, session_id=session_id)

    def list_command(
        self,
        share_url: str,
        *,
        extract_code: str = "",
        source_dir: str = "",
        page: int = 1,
        binary: str | None = None,
        session_id: str | None = None,
    ) -> list[str]:
        url, code = self.parse_share_input(share_url, extract_code)
        args = [
            "transfer",
            "list",
            url,
            "--page",
            str(max(1, page)),
            "--page-size",
            "100",
        ]
        if source_dir:
            args.extend(["--source-dir", source_dir])
        if code:
            args.extend(["-p", code])
        return self.command(args, binary=binary, json_output=True, session_id=session_id)

    def command(
        self,
        args: list[str],
        *,
        binary: str | None = None,
        json_output: bool = False,
        session: bool = True,
        session_id: str | None = None,
    ) -> list[str]:
        argv = [str(binary or self.settings.bdpan_binary), *args]
        if json_output:
            argv.append("--json")
        argv.append("--no-check-update")
        if session:
            argv.extend(
                [
                    "--agentname",
                    "strmflow",
                    "--session-id",
                    session_id or self.new_session_id(),
                ]
            )
        return argv

    @staticmethod
    def new_session_id() -> str:
        return f"{int(time.time())}-{secrets.token_hex(3)}"

    async def execute(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
        stdin: str | None = None,
        require_json: bool = False,
    ) -> BdpanRunResult:
        executable = self.executable(argv[0])
        if not executable:
            raise BdpanCliError(f"未找到 bdpan 二进制：{argv[0]}")
        command = [executable, *argv[1:]]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin.encode() if stdin is not None else None),
                timeout=timeout or self.settings.bdpan_timeout,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise BdpanCliError("bdpan 命令执行超时") from exc
        stdout_full = ANSI_ESCAPE.sub("", stdout.decode(errors="replace"))
        stderr_full = ANSI_ESCAPE.sub("", stderr.decode(errors="replace"))
        payload = self._decode_json(stdout_full) if require_json else None
        stdout_text = self._redact_output(stdout_full[-100_000:], argv)
        stderr_text = self._redact_output(stderr_full[-20_000:], argv)
        result = BdpanRunResult(process.returncode or 0, stdout_text, stderr_text, payload)
        if result.return_code != 0:
            code, message = self._error_details(payload, stderr_text or stdout_text)
            raise BdpanCliError(
                self._redact_output(message, argv) or "bdpan 命令执行失败", code=code
            )
        if require_json and payload is None:
            raise BdpanCliError("bdpan 没有返回可解析的 JSON 数据")
        code, message = self._error_details(payload, "")
        if code and code not in {"0", "200"}:
            raise BdpanCliError(
                self._redact_output(message, argv) or f"bdpan 返回错误 {code}", code=code
            )
        return result

    async def status(self, binary: str | None = None) -> dict[str, Any]:
        selected = str(binary or self.settings.bdpan_binary)
        executable = self.executable(selected)
        if not executable:
            return {
                "available": False,
                "loggedIn": False,
                "username": "",
                "expiresAt": "",
                "tokenExpiresIn": "",
                "version": "",
                "binary": selected,
            }
        session_id = self.new_session_id()
        version_result = await self.execute(
            self.command(["version"], binary=executable, session_id=session_id), timeout=15
        )
        version_match = re.search(r"bdpan:\s*([^\s]+)", version_result.stdout)
        logged_in = False
        username = ""
        expires_at = ""
        token_expires_in = ""
        try:
            identity = await self.execute(
                self.command(
                    ["whoami"],
                    binary=executable,
                    json_output=True,
                    session_id=session_id,
                ),
                timeout=20,
                require_json=True,
            )
            payload = identity.payload if isinstance(identity.payload, dict) else {}
            logged_in = bool(payload.get("authenticated") and payload.get("has_valid_token", True))
            if logged_in:
                username = str(payload.get("username") or "").strip()[:100]
                expires_at = str(payload.get("expires_at") or "").strip()[:100]
                token_expires_in = str(payload.get("token_expires_in") or "").strip()[:100]
        except BdpanCliError:
            # Older bdpan releases may not support JSON output for whoami.
            try:
                identity = await self.execute(
                    self.command(["whoami"], binary=executable, session_id=session_id),
                    timeout=20,
                )
                logged_in = "已登录" in identity.stdout
                match = re.search(r"(?:用户名|账号)\s*[：:]\s*([^\r\n]+)", identity.stdout)
                if logged_in and match:
                    username = match.group(1).strip()[:100]
                expiry_match = re.search(
                    r"Token\s*有效期至\s*[：:]\s*([^\r\n]+)", identity.stdout, re.IGNORECASE
                )
                if logged_in and expiry_match:
                    expires_at = expiry_match.group(1).strip()[:100]
            except BdpanCliError:
                pass
        return {
            "available": True,
            "loggedIn": logged_in,
            "username": username,
            "expiresAt": expires_at,
            "tokenExpiresIn": token_expires_in,
            "version": (
                version_match.group(1)
                if version_match
                else (version_result.stdout.splitlines() or [""])[0][:100]
            ),
            "binary": executable,
        }

    async def quota(self, binary: str | None = None) -> dict[str, Any]:
        """Query the official quota API with bdpan's locally stored OAuth token."""
        del binary
        try:
            token = self.access_token()
        except BdpanCliError as exc:
            return self._empty_quota(str(exc))

        async def request(client: httpx.AsyncClient) -> httpx.Response:
            return await client.get(
                "api/quota",
                params={"access_token": token, "checkfree": "1", "checkexpire": "1"},
                timeout=15,
            )

        try:
            if self.http is not None:
                response = await request(self.http)
            else:
                async with httpx.AsyncClient(base_url="https://pan.baidu.com/") as client:
                    response = await request(client)
            if not response.is_success:
                return self._empty_quota(f"百度容量接口返回 HTTP {response.status_code}")
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError, ValueError):
            return self._empty_quota("百度容量接口请求失败")

        code, message = self._error_details(payload, "")
        if code and code not in {"0", "200"}:
            error = f"百度容量接口返回错误 {code}"
            if message and len(message) <= 100:
                error += f"：{message}"
            return self._empty_quota(error)
        quota = self._quota_values(payload)
        if quota is None:
            return self._empty_quota("百度容量接口返回的数据格式不受支持")
        return quota

    def access_token(self) -> str:
        """Read and, when needed, decrypt bdpan's access token without logging it."""
        config_path = self.config_path()
        try:
            if config_path.stat().st_size > 1_048_576:
                raise BdpanCliError("bdpan 配置文件大小异常")
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BdpanCliError("未找到 bdpan 配置文件") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise BdpanCliError("bdpan 配置文件读取失败") from exc
        auth = payload.get("auth") if isinstance(payload, dict) else None
        token = str(auth.get("access_token") or "").strip() if isinstance(auth, dict) else ""
        if not token:
            raise BdpanCliError("bdpan 配置中没有可用的 access_token")
        if token.startswith("enc:v1:"):
            token = self._decrypt_token(token, config_path.parent / ".token_key")
        if not token or len(token) > 4096 or any(character.isspace() for character in token):
            raise BdpanCliError("bdpan access_token 格式不正确")
        return token

    @staticmethod
    def config_path() -> Path:
        configured = os.getenv("BDPAN_CONFIG_PATH", "").strip()
        if configured:
            path = Path(configured).expanduser()
            return path / "config.json" if path.is_dir() else path
        xdg = os.getenv("XDG_CONFIG_HOME", "").strip()
        root = Path(xdg).expanduser() if xdg else Path.home() / ".config"
        return root / "bdpan" / "config.json"

    @staticmethod
    def _decrypt_token(value: str, key_path: Path) -> str:
        try:
            encoded = value.split(":", 2)[2]
            encrypted = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            key_value = key_path.read_text(encoding="ascii").strip()
            key = bytes.fromhex(key_value)
            if len(key) != 32 or len(encrypted) <= 28:
                raise ValueError
            plaintext = AESGCM(key).decrypt(encrypted[:12], encrypted[12:], None)
            return plaintext.decode("utf-8").strip()
        except FileNotFoundError as exc:
            raise BdpanCliError("未找到 bdpan Token 解密密钥") from exc
        except (OSError, UnicodeError, ValueError, binascii.Error, InvalidTag) as exc:
            raise BdpanCliError("bdpan access_token 解密失败") from exc

    async def start_login(self, binary: str | None = None) -> str:
        argv = self.command(
            ["login", "--get-auth-url", "--accept-disclaimer"],
            binary=binary,
        )
        result = await self.execute(argv, timeout=30)
        match = AUTH_URL.search(result.stdout)
        if not match:
            raise BdpanCliError("bdpan 未返回授权地址")
        return match.group(0).rstrip(".)]】")

    async def complete_login(self, code: str, binary: str | None = None) -> dict[str, Any]:
        value = str(code or "").strip()
        if not re.fullmatch(r"[A-Fa-f0-9]{32}", value):
            raise AppError(400, "授权码需要是 32 位十六进制字符")
        argv = self.command(
            ["login", "--set-code-stdin", "--accept-disclaimer"],
            binary=binary,
        )
        await self.execute(argv, timeout=60, stdin=value + "\n")
        return await self.status(binary)

    async def logout(self, binary: str | None = None) -> None:
        """Clear the OAuth credentials through the official bdpan CLI."""
        argv = self.command(["logout"], binary=binary, session=False)
        await self.execute(argv, timeout=30)

    @staticmethod
    def _decode_json(text: str) -> Any:
        decoder = json.JSONDecoder()
        for index, character in enumerate(text):
            if character not in "[{":
                continue
            try:
                payload, _ = decoder.raw_decode(text[index:])
                return payload
            except json.JSONDecodeError:
                continue
        return None

    @staticmethod
    def _redact_output(text: str, argv: list[str]) -> str:
        result = text
        for index, value in enumerate(argv[:-1]):
            if value in {"-p", "--pwd"}:
                secret = argv[index + 1]
                if secret:
                    result = result.replace(secret, "[提取码已隐藏]")
        return result

    @classmethod
    def _quota_values(cls, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        candidates = [payload]
        for key in ("data", "quota"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
                inner_quota = nested.get("quota")
                if isinstance(inner_quota, dict):
                    candidates.append(inner_quota)

        for candidate in candidates:
            total = cls._quota_integer(
                candidate,
                "total",
                "total_space",
                "totalSpace",
                "total_bytes",
                "totalBytes",
            )
            used = cls._quota_integer(
                candidate,
                "used",
                "used_space",
                "usedSpace",
                "used_bytes",
                "usedBytes",
            )
            if total is None or used is None or total <= 0:
                continue
            total = max(0, total)
            used = max(0, used)
            return {
                "available": True,
                "supported": True,
                "totalBytes": total,
                "usedBytes": used,
                "freeBytes": max(0, total - used),
                "usedPercent": round(min(used / total * 100, 100), 1),
                "source": "bdpan 配置 · 百度开放 API",
                "error": "",
            }
        return None

    @staticmethod
    def _quota_integer(payload: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            value = payload.get(key)
            if isinstance(value, bool) or value is None or value == "":
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _empty_quota(error: str, *, supported: bool = True) -> dict[str, Any]:
        return {
            "available": False,
            "supported": supported,
            "totalBytes": 0,
            "usedBytes": 0,
            "freeBytes": 0,
            "usedPercent": 0,
            "source": "bdpan 配置 · 百度开放 API",
            "error": error[:300],
        }

    @classmethod
    def _error_details(cls, payload: Any, fallback: str) -> tuple[str, str]:
        if not isinstance(payload, dict):
            return "", fallback.strip()
        code = payload.get("errno", payload.get("code", ""))
        if isinstance(code, bool):
            code = int(code)
        data = payload.get("data")
        nested_message = data.get("message") if isinstance(data, dict) else ""
        message = payload.get("error") or payload.get("message") or nested_message
        return str(code) if code not in {None, ""} else "", str(message or fallback).strip()

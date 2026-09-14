from __future__ import annotations

import asyncio
import json
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

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

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

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
                "version": "",
                "binary": selected,
            }
        session_id = self.new_session_id()
        version_result = await self.execute(
            self.command(["version"], binary=executable, session_id=session_id), timeout=15
        )
        version_match = re.search(r"bdpan:\s*([^\s]+)", version_result.stdout)
        try:
            identity = await self.execute(
                self.command(["whoami"], binary=executable, session_id=session_id), timeout=20
            )
            logged_in = "已登录" in identity.stdout
        except BdpanCliError:
            logged_in = False
        return {
            "available": True,
            "loggedIn": logged_in,
            "version": (
                version_match.group(1)
                if version_match
                else (version_result.stdout.splitlines() or [""])[0][:100]
            ),
            "binary": executable,
        }

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

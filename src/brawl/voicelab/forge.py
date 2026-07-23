"""`brawl voice-forge`：用阿里云 Qwen3-TTS 铸造音色并生成试听。"""

from __future__ import annotations

import base64
import binascii
import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import ClassVar

import httpx
from dotenv import dotenv_values
from pydantic import model_validator
from pydantic_settings import (
    BaseSettings,
    CliPositionalArg,
    CliSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from rich.console import Console

from brawl.paths import REPO_ROOT
from brawl.runtime.files import UnsafePathError, atomic_write
from brawl.runtime.tts import TTSError, dashscope_audio_url

# 北京地域。创建音色与合成是两个 endpoint;target_model 必须与合成 model 一致。
_API_BASE = "https://dashscope.aliyuncs.com"
_CREATE_URL = f"{_API_BASE}/api/v1/services/audio/tts/customization"
_SYNTH_URL = f"{_API_BASE}/api/v1/services/aigc/multimodal-generation/generation"
_VD_MODEL = "qwen3-tts-vd-2026-01-26"

# 语气样本:同一句话配两种对立指令,一耳朵听出这副嗓子对 instructions 的响应度。
_SAMPLE_TEXT = "好家伙，这就开始投我了？行，那我也没什么好说的了。"
_SAMPLES = {
    "急了": "用非常愤怒急切的语气，语速很快，几乎是吼出来的",
    "阴阳": "用阴阳怪气、慢悠悠的嘲讽语气，拖长音",
}


class SettingsError(Exception):
    """面向用户的错误(参数/凭证/API 失败):cli 入口只打印消息、不打 traceback。"""


class ForgeSettings(BaseSettings):
    """`brawl voice-forge` 的参数面(settings 即 CLI,flag 自动派生)。"""

    model_config = SettingsConfigDict(
        env_prefix="BRAWL_VOICE_FORGE_",
        cli_kebab_case=True,
        cli_implicit_flags=True,
        extra="forbid",
    )

    _cli_args: ClassVar[list[str] | None] = None

    voice_prompt: CliPositionalArg[str]  # 嗓音描述(VD prompt),≤2048 字符
    name: str = "voice"  # 一两个单词的短描述,拼上日期做云端 preferred_name(便于辨认/清理)
    preview_text: str = "我跳梅林？哥们，我要是梅林这局早就赢了好吧。"  # 用 cast 将来真会说的话来听
    emotion_samples: bool = True  # 铸完追加合成两个语气样本(急了/阴阳),验收 instructions 响应度
    out_dir: Path | None = None  # 试听产物目录;None = /tmp 下自动建临时目录

    @model_validator(mode="after")
    def _check(self) -> ForgeSettings:
        if not self.voice_prompt.strip():
            raise ValueError("voice_prompt 不能为空")
        if len(self.voice_prompt) > 2048:
            raise ValueError(f"voice_prompt 超长:{len(self.voice_prompt)} > 2048 字符")
        if not self.name.isascii() or not self.name.replace("_", "").isalnum():
            raise ValueError(f"name 仅限字母数字和下划线(进云端 preferred_name),收到 {self.name!r}")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            CliSettingsSource(
                settings_cls,
                cli_parse_args=cls._cli_args if cls._cli_args is not None else [],
                cli_prog_name="brawl voice-forge",
            ),
            env_settings,
        )

    @classmethod
    def load(cls, cli_args: list[str]) -> ForgeSettings:
        cls._cli_args = cli_args
        try:
            return cls()
        finally:
            cls._cli_args = None


def _api_key() -> str:
    """边界校验:开工前确认凭证就绪。真实环境变量优先,其次仓库根 .env(对齐各玩法惯例)。"""
    env = os.environ.get("DASHSCOPE_API_KEY")
    if env is not None:
        if not env.strip():
            raise SettingsError("环境变量 DASHSCOPE_API_KEY 不得为空")
        return env
    key = dotenv_values(REPO_ROOT / ".env").get("DASHSCOPE_API_KEY")
    if not key or not key.strip():
        raise SettingsError(".env 缺 DASHSCOPE_API_KEY(阿里云百炼 API key)")
    return key


def _post(client: httpx.Client, url: str, payload: dict) -> dict:
    try:
        resp = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        raise SettingsError(f"DashScope API 请求失败：{exc}") from exc
    if not resp.is_success:
        try:
            error = resp.json()
        except ValueError:
            detail = resp.text.strip()[:200] or "非 JSON 响应"
        else:
            detail = f"{error.get('code')} {error.get('message')}" if isinstance(error, dict) else repr(error)
        raise SettingsError(f"DashScope API 调用失败({resp.status_code})：{detail}")
    try:
        body = resp.json()
    except ValueError as exc:
        raise SettingsError("DashScope API 成功响应不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise SettingsError("DashScope API 成功响应顶层不是对象")
    return body


def _create_voice(client: httpx.Client, s: ForgeSettings) -> tuple[str, bytes]:
    """创建音色:返回 (voice_id, 预览 wav 字节)。"""
    today = datetime.date.today().strftime("%Y%m%d")
    body = _post(
        client,
        _CREATE_URL,
        {
            "model": "qwen-voice-design",
            "input": {
                "action": "create",
                "target_model": _VD_MODEL,
                # 云端 preferred_name 实测只接受字母数字下划线(连字符 400)
                "preferred_name": f"{s.name}_{today}",
                "voice_prompt": s.voice_prompt,
                "preview_text": s.preview_text,
            },
            "parameters": {"sample_rate": 24000, "response_format": "wav"},
        },
    )
    try:
        out = body["output"]
        voice_id = out["voice"]
        preview_data = out["preview_audio"]["data"]
    except (KeyError, TypeError) as exc:
        raise SettingsError("DashScope 创建音色响应缺少 voice 或 preview_audio.data") from exc
    if not isinstance(voice_id, str) or not voice_id.strip():
        raise SettingsError("DashScope 创建音色响应里的 voice 无效")
    try:
        preview = base64.b64decode(preview_data, validate=True)
    except (binascii.Error, TypeError, ValueError) as exc:
        raise SettingsError("DashScope 创建音色响应里的 preview_audio.data 不是合法 base64") from exc
    if not preview:
        raise SettingsError("DashScope 创建音色响应里的 preview_audio.data 为空")
    return voice_id, preview


def _synthesize(
    api_client: httpx.Client,
    audio_client: httpx.Client,
    voice_id: str,
    text: str,
    instructions: str,
) -> bytes:
    """用 voice_id + 语气指令合成一句,当场下载(返回的 OSS URL 有有效期)。"""
    body = _post(
        api_client,
        _SYNTH_URL,
        {"model": _VD_MODEL, "input": {"text": text, "voice": voice_id, "instructions": instructions}},
    )
    try:
        raw_url = body["output"]["audio"]["url"]
        audio_url = dashscope_audio_url(raw_url)
    except (KeyError, TypeError, TTSError) as exc:
        raise SettingsError(f"DashScope 合成响应缺少可信音频地址：{exc}") from exc
    try:
        response = audio_client.get(audio_url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise SettingsError(f"DashScope 音频下载失败：{exc}") from exc
    if not response.content:
        raise SettingsError("DashScope 音频下载结果为空")
    return response.content


def _write_output(path: Path, data: bytes) -> None:
    try:
        atomic_write(path, data)
    except (OSError, UnsafePathError) as exc:
        raise SettingsError(f"无法安全写入试听产物 {path}：{exc}") from exc


def forge(s: ForgeSettings, console: Console) -> None:
    api_key = _api_key()
    out_dir = s.out_dir or Path(tempfile.mkdtemp(prefix=f"voice-forge-{s.name}-"))
    if out_dir.is_symlink():
        raise SettingsError(f"out_dir 不得是符号链接：{out_dir}")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SettingsError(f"无法创建 out_dir {out_dir}：{exc}") from exc
    if not out_dir.is_dir():
        raise SettingsError(f"out_dir 不是目录：{out_dir}")

    with (
        httpx.Client(headers={"Authorization": f"Bearer {api_key}"}, timeout=60) as api_client,
        httpx.Client(timeout=60, follow_redirects=False) as audio_client,
    ):
        voice_id, preview = _create_voice(api_client, s)
        _write_output(out_dir / "preview.wav", preview)
        console.print(f"音色已创建:[bold]{voice_id}[/bold]")
        console.print(f"预览:{out_dir / 'preview.wav'}")

        if s.emotion_samples:
            for label, instructions in _SAMPLES.items():
                path = out_dir / f"sample_{label}.wav"
                _write_output(path, _synthesize(api_client, audio_client, voice_id, _SAMPLE_TEXT, instructions))
                console.print(f"语气样本[{label}]:{path}")

    # 留一份导线:听完隔了几天也能对上"这堆 wav 是哪段描述铸的"。
    _write_output(
        out_dir / "forge.json",
        json.dumps(
            {"voice_id": voice_id, "voice_prompt": s.voice_prompt, "preview_text": s.preview_text},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8"),
    )

    console.print("\n听满意后,把下面片段拷进玩法的 cast.yaml 对应 persona:\n")
    console.print(f'    voice_id: {voice_id}\n    voice_design: "{s.voice_prompt}"')


def main(argv: list[str]) -> None:
    forge(ForgeSettings.load(argv), Console())


__all__ = ["ForgeSettings", "SettingsError", "main"]

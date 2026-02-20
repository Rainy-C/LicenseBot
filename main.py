import base64
import json
from datetime import datetime, timezone

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

from astrbot.core.utils.session_waiter import session_waiter, SessionController


def _safe_int(s: str):
    try:
        return int(str(s).strip())
    except Exception:
        return None


def _looks_like_base64(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    # 允许 URL-safe base64（- _），以及末尾 padding =
    for ch in s:
        if not (ch.isalnum() or ch in "+/=_-"):
            return False
    # 尝试解码验证（不要求一定是 JSON，但至少得能解码出 bytes）
    try:
        # 补齐 padding
        pad = (-len(s)) % 4
        if pad:
            s += "=" * pad
        base64.b64decode(s, validate=False)
        return True
    except Exception:
        return False


def _fmt_expire_ms(ms: int) -> str:
    # ms -> 本地时间字符串（用 UTC+0 也行，这里直接本地化到系统时区）
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return str(ms)


def _render_device_info(system_info: dict, expire_ms: int) -> str:
    # 只输出“设备信息本身”，排版好看点，但不额外加说明废话
    android_id = system_info.get("androidId", "") or ""
    manufacturer = system_info.get("manufacturer", "") or ""
    model = system_info.get("model", "") or ""
    product = system_info.get("product", "") or ""
    expire_str = _fmt_expire_ms(expire_ms)

    lines = [
        "┏━━━━━━━━━━ 设备信息 ━━━━━━━━━━┓",
        f"┃ Android ID     : {android_id}",
        f"┃ Manufacturer   : {manufacturer}",
        f"┃ Model          : {model}",
        f"┃ Product        : {product}",
        f"┃ Expire         : {expire_str}",
        "┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛",
    ]
    return "\n".join(lines)


@register("license_exchange", "chen", "两步问答授权兑换（设备ID + 天数）", "1.0.0", "repo url")
class LicenseExchangePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        # 配置项（可在 WebUI 配置）
        self.api_url = str(self.config.get("api_url", "http://69.165.67.145:5963/exchange")).strip()
        self.timeout_sec = float(self.config.get("timeout_sec", 12))
        self.max_days = int(self.config.get("max_days", 3650))
        self.allow_plain_trigger = bool(self.config.get("allow_plain_trigger", True))

    async def _post_exchange(self, device_b64: str, days: int) -> dict:
        payload = {"deviceBase64": device_b64, "days": days}
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.post(
                self.api_url,
                headers={"Content-Type": "application/json"},
                json=payload,
            ) as resp:
                text = await resp.text()
                if resp.status < 200 or resp.status >= 300:
                    raise RuntimeError(f"HTTP {resp.status}: {text[:300]}")
                try:
                    return json.loads(text)
                except Exception:
                    raise RuntimeError(f"Invalid JSON: {text[:300]}")

    async def _run_flow(self, event: AstrMessageEvent):
        # 禁止默认 LLM 插嘴（不然它总想发表意见）
        try:
            event.should_call_llm(False)
        except Exception:
            pass

        # 1) 询问 deviceBase64
        yield event.plain_result("请提供设备ID")

        @session_waiter(timeout=60, record_history_chains=False)
        async def wait_device(controller: SessionController, e: AstrMessageEvent):
            return (e.message_str or "").strip()

        try:
            device_b64 = await wait_device(event)
        except TimeoutError:
            yield event.plain_result("超时")
            event.stop_event()
            return
        except Exception as e:
            logger.error("wait_device error", exc_info=True)
            yield event.plain_result("错误")
            event.stop_event()
            return

        if not _looks_like_base64(device_b64):
            yield event.plain_result("设备ID格式不正确")
            event.stop_event()
            return

        # 2) 询问 days
        yield event.plain_result("请输入授权天数")

        @session_waiter(timeout=60, record_history_chains=False)
        async def wait_days(controller: SessionController, e: AstrMessageEvent):
            return (e.message_str or "").strip()

        try:
            days_raw = await wait_days(event)
        except TimeoutError:
            yield event.plain_result("超时")
            event.stop_event()
            return
        except Exception:
            logger.error("wait_days error", exc_info=True)
            yield event.plain_result("错误")
            event.stop_event()
            return

        days = _safe_int(days_raw)
        if days is None or days <= 0 or days > self.max_days:
            yield event.plain_result("天数不合法")
            event.stop_event()
            return

        # 3) 请求接口
        try:
            data = await self._post_exchange(device_b64, days)
        except Exception as e:
            logger.error(f"exchange request failed: {e}", exc_info=True)
            yield event.plain_result("请求失败")
            event.stop_event()
            return

        if not isinstance(data, dict) or not data.get("ok"):
            # 尽量把服务端返回带出来，但别太长
            short = str(data)[:400]
            yield event.plain_result(short)
            event.stop_event()
            return

        system_info = data.get("system_info") or {}
        expire = data.get("expire")
        license_str = data.get("license", "")

        # 先发设备信息（排版好看点）
        info_text = _render_device_info(system_info, int(expire) if isinstance(expire, (int, float)) else 0)
        yield event.plain_result(info_text)

        # 再发 License，整条消息只包含 license 本体，不加任何字
        # 注意：有的平台会吞首尾空格，所以直接纯文本发送
        yield event.plain_result(str(license_str))

        event.stop_event()

    # 指令触发：/授权
    @filter.command("授权", alias={"license", "auth"})
    async def cmd_auth(self, event: AstrMessageEvent):
        """授权兑换：依次输入设备ID(Base64)与天数，成功后先回设备信息再回 License"""
        async for r in self._run_flow(event):
            yield r

    # 纯文本触发：直接发“授权”
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def plain_trigger(self, event: AstrMessageEvent):
        if not self.allow_plain_trigger:
            return
        text = (event.message_str or "").strip()
        if text != "授权":
            return

        async for r in self._run_flow(event):
            yield r

    async def terminate(self):
        pass

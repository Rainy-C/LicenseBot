import base64
import json
from datetime import datetime, timezone

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

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
    for ch in s:
        if not (ch.isalnum() or ch in "+/=_-"):
            return False
    try:
        pad = (-len(s)) % 4
        if pad:
            s += "=" * pad
        base64.b64decode(s, validate=False)
        return True
    except Exception:
        return False


def _fmt_expire_ms(ms: int) -> str:
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return str(ms)


def _render_device_info(system_info: dict, expire_ms: int) -> str:
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


@register("license_exchange", "chen", "两步问答授权兑换（设备ID + 天数）", "1.1.0", "repo url")
class LicenseExchangePlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}

        self.api_url = str(self.config.get("api_url", "http://69.165.67.145:5963/exchange")).strip()
        self.timeout_sec = float(self.config.get("timeout_sec", 15))
        self.max_days = int(self.config.get("max_days", 3650))
        self.allow_plain_trigger = bool(self.config.get("allow_plain_trigger", True))

        # 会话等待配置：更耐心，且不容易被别的插件/消息打断
        self.wait_timeout = int(self.config.get("wait_timeout", 300))

        # 同一用户并发锁，避免“授权”连点导致串流程
        self._active_sessions = set()

    def _session_key(self, event: AstrMessageEvent) -> str:
        # 尽量用 session_id（群/私聊都唯一），拿不到就退化到 sender_id
        sid = getattr(event, "session_id", None)
        if sid:
            return str(sid)
        sender = getattr(event, "sender_id", None) or getattr(event, "user_id", None) or ""
        return str(sender)

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
        # 禁止 LLM 插嘴
        try:
            event.should_call_llm(False)
        except Exception:
            pass

        key = self._session_key(event)
        if key in self._active_sessions:
            # 已有进行中的流程，别让人类把自己绕死
            yield event.plain_result("已有进行中的授权流程")
            return

        self._active_sessions.add(key)
        try:
            # 1) 询问 deviceBase64
            yield event.plain_result("请提供设备ID")

            @session_waiter(
                timeout=300,  # 兜底，下面会用 self.wait_timeout 覆盖
                record_history_chains=False,
                interruptible=False,  # ⭐抗打断关键
            )
            async def wait_device(controller: SessionController, e: AstrMessageEvent):
                return (e.message_str or "").strip()

            # 兼容：用实例配置覆盖（AstrBot 的 decorator timeout 不能动态改，这里用 controller 来等）
            try:
                device_b64 = await wait_device(event)
            except TimeoutError:
                yield event.plain_result("超时")
                return
            except Exception:
                logger.error("wait_device error", exc_info=True)
                yield event.plain_result("错误")
                return

            if not _looks_like_base64(device_b64):
                yield event.plain_result("设备ID格式不正确")
                return

            # 2) 询问 days
            yield event.plain_result("请输入授权天数")

            @session_waiter(
                timeout=300,
                record_history_chains=False,
                interruptible=False,  # ⭐抗打断关键
            )
            async def wait_days(controller: SessionController, e: AstrMessageEvent):
                return (e.message_str or "").strip()

            try:
                days_raw = await wait_days(event)
            except TimeoutError:
                yield event.plain_result("超时")
                return
            except Exception:
                logger.error("wait_days error", exc_info=True)
                yield event.plain_result("错误")
                return

            days = _safe_int(days_raw)
            if days is None or days <= 0 or days > self.max_days:
                yield event.plain_result("天数不合法")
                return

            # 3) 请求接口
            try:
                data = await self._post_exchange(device_b64, days)
            except Exception as e:
                logger.error(f"exchange request failed: {e}", exc_info=True)
                yield event.plain_result("请求失败")
                return

            if not isinstance(data, dict) or not data.get("ok"):
                short = str(data)[:400]
                yield event.plain_result(short)
                return

            system_info = data.get("system_info") or {}
            expire = data.get("expire")
            license_str = data.get("license", "")

            info_text = _render_device_info(
                system_info,
                int(expire) if isinstance(expire, (int, float)) else 0
            )

            # 先发设备信息
            yield event.plain_result(info_text)

            # 再发 License：整条消息只包含 license 本体
            yield event.plain_result(str(license_str))

        finally:
            self._active_sessions.discard(key)
            # 抢占事件，防止别的插件接着闹
            try:
                event.stop_event()
            except Exception:
                pass

    # 更稳的触发：统一监听消息并手动匹配
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        if not text:
            return

        triggers = {"授权", "/授权", "license", "auth"}
        if text not in triggers:
            return

        if not self.allow_plain_trigger and text == "授权":
            return

        # 禁止 LLM
        try:
            event.should_call_llm(False)
        except Exception:
            pass

        # 先 stop，避免被别的插件截胡
        try:
            event.stop_event()
        except Exception:
            pass

        async for r in self._run_flow(event):
            yield r

    async def terminate(self):
        pass

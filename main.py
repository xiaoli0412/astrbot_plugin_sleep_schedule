"""作息表插件：夜里自己睡觉，早上醒来把昨晚的留言一起回复。

实现要点
--------
* 拦截层只能用 ``on_llm_request``：``WakingCheckStage`` 之后插 stage 走不通
  （``scheduler.py`` 用 ``STAGES_ORDER.index(x.__name__)`` 排序，插件注册的
  stage 不在名单里会 ValueError）。
* ``event.stop_event()`` 会让 ``internal.py`` 直接 return，连带跳过
  ``_save_to_history`` —— 所以本条消息必须自己落库，早上再喂回去。
* 只拦被动回复：命令（``/`` 开头）和 cron 唤醒（``CronMessageEvent``）放行。
"""

from __future__ import annotations

import asyncio
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

_D = str(Path(__file__).resolve().parent)
if _D not in sys.path:
    # 追加而不是插入 0：避免插件里的 schedule/ 遮蔽 PyPI 上同名的 schedule 包。
    sys.path.append(_D)

try:  # 正常情况：插件以 data.plugins.<name>.main 包形式加载
    from .schedule.clock import (
        DEFAULT_WINDOW,
        is_asleep,
        minutes_to_hhmm,
        next_change,
        parse_windows,
        sleep_state_label,
        windows_str,
    )
    from .schedule.store import PendingStore
except ImportError:  # pragma: no cover - 兜底：按顶层模块加载时走 sys.path
    from schedule.clock import (
        DEFAULT_WINDOW,
        is_asleep,
        minutes_to_hhmm,
        next_change,
        parse_windows,
        sleep_state_label,
        windows_str,
    )
    from schedule.store import PendingStore

PLUGIN_NAME = "astrbot_plugin_sleep_schedule"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "sleep_window": DEFAULT_WINDOW,
    "wake_check_seconds": 60,
    "max_replay_messages": 20,
    "replay_on_wake": True,
    "fallback_persona_id": "",
    "whitelist_umos": "",
    "log_decisions": True,
}

# 单条留言进 prompt 时的最大长度，防止刷屏用户把 prompt 撑爆
_MAX_LINE_CHARS = 200


@register(
    PLUGIN_NAME,
    "xiaoli0412",
    "作息表：夜间静默不回消息，早上醒来把昨晚的留言一起回复",
    "1.0.0",
    "",
)
class SleepSchedulePlugin(Star):
    """作息表：夜间静默 + 早上回放留言。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context, config)
        self.config: AstrBotConfig = config  # type: ignore[assignment]
        self._data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self._store = PendingStore(self._data_dir / "pending.db")
        self._task: asyncio.Task | None = None
        self._windows: list[tuple[int, int]] = parse_windows(DEFAULT_WINDOW)
        self._window_raw = DEFAULT_WINDOW
        self._window_error = ""
        self._last_asleep: bool | None = None
        self._startup_tick = True
        self._fail_until: dict[str, float] = {}
        self._replaying: set[str] = set()
        self._reload_config()
        logger.info(
            f"[sleep] 插件已实例化，作息窗口={windows_str(self._windows)}，"
            f"enabled={self._cfg('enabled', True)}"
        )

    # ================================================================ 配置
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.config.get(key, DEFAULTS.get(key, default))
        except Exception:
            return DEFAULTS.get(key, default)
        return DEFAULTS.get(key, default) if value is None else value

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "1", "yes", "on", "是"}

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except Exception:
            return default

    def _reload_config(self) -> None:
        """每 tick 重读一次，WebUI 改配置不用重启。"""
        spec = str(self._cfg("sleep_window", DEFAULT_WINDOW) or "").strip() or DEFAULT_WINDOW
        try:
            self._windows = parse_windows(spec)
            self._window_error = ""
        except Exception as exc:  # 配置写坏了就退回默认窗口，别把整晚静默搞崩
            message = str(exc)
            if message != self._window_error:
                logger.warning(f"[sleep] 解析 sleep_window 失败，暂用默认 {DEFAULT_WINDOW}：{message}")
            self._windows = parse_windows(DEFAULT_WINDOW)
            self._window_error = message
        self._window_raw = spec

    @property
    def _whitelist(self) -> set[str]:
        raw = str(self._cfg("whitelist_umos", "") or "")
        items = raw.replace("，", ",").replace("；", ",").replace(";", ",").replace("\n", ",")
        return {item.strip() for item in items.split(",") if item.strip()}

    def _is_asleep_now(self, now_ts: float | None = None) -> bool:
        self._reload_config()
        return is_asleep(time.time() if now_ts is None else now_ts, self._windows)

    def _log(self, message: str) -> None:
        if self._cfg_bool("log_decisions", True):
            logger.info(f"[sleep] {message}")

    # ================================================================ 拦截
    @filter.on_llm_request(priority=180)
    async def intercept_night_message(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """夜间拦住被动回复：落库 + stop_event，不 yield 任何东西。"""
        try:
            await self._maybe_intercept(event, req)
        except Exception:
            logger.error(f"[sleep] 拦截逻辑异常（已放行本轮）：{traceback.format_exc()}")

    async def _maybe_intercept(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        if not self._cfg_bool("enabled", True):
            return
        if self._is_cron_event(event):
            return

        now = time.time()
        if not self._is_asleep_now(now):
            return

        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return
        if umo in self._whitelist:
            self._log(f"会话 {umo} 在白名单内，夜间照常回复")
            return

        text = self._describe_message(event)
        if not text:
            return
        if self._looks_like_command(event):
            return

        msg_id = await self._store.add(umo, now, text, "text")
        event.stop_event()
        pending = await self._store.count(umo)
        self._log(f"🌙 已睡着，暂存留言 #{msg_id} ← {umo}（本会话共 {pending} 条待回放）")

    @staticmethod
    def _is_cron_event(event: AstrMessageEvent) -> bool:
        try:
            from astrbot.core.cron.events import CronMessageEvent

            return isinstance(event, CronMessageEvent)
        except Exception:
            return type(event).__name__ == "CronMessageEvent"

    @staticmethod
    def _looks_like_command(event: AstrMessageEvent) -> bool:
        try:
            text = (event.message_str or "").lstrip()
        except Exception:
            text = ""
        return text.startswith("/") or text.startswith("／")

    @staticmethod
    def _describe_message(event: AstrMessageEvent) -> str:
        """把一条消息压成一行文本：正文 + 组件占位符描述。"""
        parts: list[str] = []
        try:
            body = (event.message_str or "").strip()
        except Exception:
            body = ""
        if body:
            parts.append(body)

        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        except Exception:
            chain = []

        for comp in chain:
            name = type(comp).__name__.lower()
            if name == "plain":
                continue
            if name == "image":
                parts.append("[图片]")
            elif name == "record":
                parts.append("[语音]")
            elif name == "video":
                parts.append("[视频]")
            elif name == "file":
                fname = getattr(comp, "name", "") or getattr(comp, "file", "") or ""
                parts.append(f"[文件:{fname}]" if fname else "[文件]")
            elif name == "at":
                parts.append(f"@{getattr(comp, 'name', '') or getattr(comp, 'qq', '')}")
            elif name == "reply":
                parts.append("[引用了一条消息]")
            elif name in {"forward", "node"}:
                parts.append("[合并转发]")
            elif name == "face":
                parts.append("[表情]")
            else:
                parts.append(f"[{name}]")

        return " ".join(p.strip() for p in parts if p and p.strip()).strip()

    # ================================================================ 唤醒回放
    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        # PATCHED(local): 注册 WebUI 作息面板 API
        try:
            reg = getattr(self.context, "register_web_api", None)
            if reg:
                reg(
                    "astrbot_plugin_sleep_schedule/overview",
                    self.api_overview,
                    ["GET"],
                    "作息：状态 + 待回放",
                )
                reg(
                    "astrbot_plugin_sleep_schedule/replay",
                    self.api_replay,
                    ["POST"],
                    "作息：手动回放",
                )
                self._log("[sleep] WebUI API 已注册 2 条")
        except Exception as exc:  # noqa: BLE001
            self._log(f"[sleep] WebUI API 注册失败：{exc}")
        await self._ensure_started()

    async def _ensure_started(self) -> None:
        if self._task and not self._task.done():
            return
        await self._store.init()
        self._task = asyncio.create_task(self._loop(), name="sleep_schedule_wake_loop")
        self._log("后台唤醒检查任务已启动")

    # ------------------------------------------------ WebUI（PATCHED local）
    async def api_overview(self):
        """面板数据：状态 + 窗口 + 待回放。"""
        import time as _t
        asleep = self._is_asleep_now()
        windows = getattr(self, "_windows", None)
        if windows is None:
            try:
                from schedule.clock import parse_windows  # type: ignore
                windows = parse_windows(str(self._cfg("sleep_window", "23:30-07:30")))
            except Exception:
                windows = []

        sessions = []
        try:
            umos = await self._store.pending_umos()
            for umo in umos[:20]:
                rows = await self._store.fetch_pending(umo, 20)
                sessions.append({
                    "umo": umo,
                    "count": len(rows),
                    "items": [
                        {
                            "id": r.get("id"),
                            "ts": _t.strftime("%m-%d %H:%M", _t.localtime(float(r.get("ts") or 0))),
                            "text": (r.get("text") or "")[:160],
                        }
                        for r in rows
                    ],
                })
        except Exception:
            pass

        total = sum(x["count"] for x in sessions)
        return {
            "ok": True,
            "data": {
                "asleep": asleep,
                "sleep_window": str(self._cfg("sleep_window", "23:30-07:30")),
                "enabled": self._cfg_bool("enabled", True),
                "replay_on_wake": self._cfg_bool("replay_on_wake", True),
                "wake_check_seconds": self._cfg_int("wake_check_seconds", 60),
                "replay_provider_id": str(self._cfg("replay_provider_id", "") or ""),
                "pending_total": total,
                "sessions": sessions,
                "now": _t.strftime("%H:%M:%S", _t.localtime()),
            },
        }

    async def api_replay(self):
        """手动触发一次回放。"""
        try:
            n = await self._replay_all(reason="manual")
            return {"ok": True, "data": {"replayed": n}}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    async def terminate(self) -> None:
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._log("后台唤醒检查任务已停止")

    async def _loop(self) -> None:
        await self._store.init()
        while True:
            interval = max(5, self._cfg_int("wake_check_seconds", 60))
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error(f"[sleep] 唤醒检查异常：{traceback.format_exc()}")
            await asyncio.sleep(interval)

    async def _tick(self) -> None:
        now = time.time()
        asleep = self._is_asleep_now(now)
        woke = self._last_asleep is True and not asleep
        first = self._startup_tick
        self._last_asleep = asleep
        self._startup_tick = False

        if woke:
            self._log("☀️ 刚醒，开始回放昨晚的留言")
        if asleep:
            return

        if not self._cfg_bool("replay_on_wake", True) and not woke and not first:
            return

        replayed = await self._replay_all(reason="woke" if woke else "awake")
        if replayed:
            self._log(f"回放完成：{replayed} 个会话")
        try:
            await self._store.purge_replayed(keep_days=30)
        except Exception:
            pass

    async def _replay_all(self, *, reason: str = "manual") -> int:
        umos = await self._store.pending_umos()
        done = 0
        for umo in umos:
            if time.time() < self._fail_until.get(umo, 0):
                continue
            try:
                if await self._replay_one(umo):
                    done += 1
            except Exception:
                self._fail_until[umo] = time.time() + 300
                logger.error(f"[sleep] 回放 {umo} 失败（5 分钟内不再重试）：{traceback.format_exc()}")
        return done

    async def _replay_one(self, umo: str) -> bool:
        if umo in self._replaying:
            return False
        self._replaying.add(umo)
        try:
            limit = max(1, self._cfg_int("max_replay_messages", 20))
            rows = await self._store.fetch_pending(umo, limit)
            if not rows:
                return False

            self._reload_config()
            # 回放优先用配置的模型：会话默认通道可能超时（实测
            # [渠道6]-anti-gemini-3.8-flash 连续 5 次 APITimeoutError，
            # 导致 replayed 永远是 0）。配了 replay_provider_id 就绕开它。
            provider = None
            _pid = str(self._cfg("replay_provider_id", "") or "").strip()
            if _pid:
                try:
                    _p = self.context.get_provider_by_id(_pid)
                    if _p is not None and hasattr(_p, "text_chat"):
                        provider = _p
                        self._log(f"回放使用指定模型 {_pid}")
                except Exception as exc:
                    logger.warning(f"[sleep] 指定模型 {_pid} 取用失败：{exc}")
            if provider is None:
                provider = await asyncio.wait_for(
                    self.context.get_using_provider_async(umo), timeout=15
                )
            if provider is None:
                self._fail_until[umo] = time.time() + 300
                logger.warning(f"[sleep] 会话 {umo} 没有可用 Provider，稍后重试")
                return False
            prompt = self._build_prompt(rows)
            system_prompt = await self._persona_prompt(umo)

            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    session_id=umo,
                    contexts=[],
                    system_prompt=system_prompt or None,
                ),
                timeout=180,
            )
            text = str(getattr(resp, "completion_text", "") or "").strip()
            if not text:
                self._fail_until[umo] = time.time() + 300
                logger.warning(f"[sleep] 回放 {umo} 得到空回复，稍后重试")
                return False

            await self.context.send_message(umo, MessageChain([Plain(text)]))
            await self._store.mark_replayed([int(r["id"]) for r in rows])
            await self._append_history(umo, prompt, text)
            self._log(
                f"☀️ 已回放 {len(rows)} 条留言 → {umo}"
                f"（{_pid or '会话默认模型'}）；"
                f"首行：{text.splitlines()[0][:60] if text else ''}"
            )
            return True
        finally:
            self._replaying.discard(umo)

    def _build_prompt(self, rows: list[dict[str, Any]]) -> str:
        now = datetime.now()
        lines: list[str] = []
        for row in rows:
            stamp = datetime.fromtimestamp(float(row["ts"])).strftime("%H:%M")
            body = " ".join(str(row.get("text") or "").split())
            if len(body) > _MAX_LINE_CHARS:
                body = body[:_MAX_LINE_CHARS] + "…"
            lines.append(f"[{stamp}] {body}")

        window = windows_str(self._windows)
        return (
            f"现在是 {now.strftime('%Y-%m-%d %H:%M')}，你刚睡醒。"
            f"你的作息是 {window}，这段时间你睡着了，没有回复消息。\n"
            f"用户在你睡着之后给你留了下面这些话：\n\n"
            + "\n".join(lines)
            + "\n\n请用你一贯的方式回复这些留言。可以挑重点，不用逐条复述；"
            "语气自然，就像刚醒来拿起手机看到消息那样。"
        )

    async def _persona_prompt(self, umo: str) -> str:
        """先取会话绑定的人格，取不到再用 fallback_persona_id。"""
        persona_id = ""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if cid:
                conv = await cm.get_conversation(umo, cid)
                persona_id = str(getattr(conv, "persona_id", "") or "") if conv else ""
        except Exception:
            logger.debug(f"[sleep] 读取会话人格失败：{traceback.format_exc()}")

        if not persona_id:
            persona_id = str(self._cfg("fallback_persona_id", "") or "").strip()
        if not persona_id:
            return ""

        try:
            db = self.context.get_db()
            # 真实 API 是 get_persona_by_id；hasattr 包一层会永远拿到 None，别这么写。
            getter = getattr(db, "get_persona_by_id", None)
            if getter is None:
                logger.warning("[sleep] 数据库没有 get_persona_by_id，本次不带人格")
                return ""
            row = await getter(persona_id)
            if row is None:
                logger.warning(f"[sleep] 找不到人格 {persona_id}")
                return ""
            return str(getattr(row, "system_prompt", "") or "")
        except Exception:
            logger.warning(f"[sleep] 读取人格 {persona_id} 失败：{traceback.format_exc()}")
            return ""

    async def _append_history(self, umo: str, prompt: str, reply: str) -> None:
        """把「留言 + 回复」写回会话历史，白天接着聊才连贯。"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            await cm.add_message_pair(
                cid,
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": reply},
            )
        except Exception:
            logger.debug(f"[sleep] 写回会话历史失败：{traceback.format_exc()}")

    # ================================================================ 指令
    @filter.command("作息")
    async def cmd_schedule(self, event: AstrMessageEvent):
        """查看作息状态；/作息 回放 手动触发一次回放。"""
        try:
            arg = (event.message_str or "").strip()
            arg = arg[len("作息"):].strip() if arg.startswith("作息") else arg
        except Exception:
            arg = ""

        if arg in {"回放", "replay", "立即回放"}:
            self._fail_until.clear()
            try:
                done = await self._replay_all(reason="manual")
            except Exception:
                logger.error(f"[sleep] 手动回放异常：{traceback.format_exc()}")
                yield event.plain_result("回放时出错了，详情见日志。")
                return
            if done:
                yield event.plain_result(f"已手动回放 {done} 个会话的留言 ☀️")
            else:
                yield event.plain_result("没有可回放的留言（或 Provider 暂不可用）。")
            return

        if arg in {"测试", "status", "状态"}:
            arg = ""

        now = time.time()
        self._reload_config()
        asleep = self._is_asleep_now(now)

        total = await self._store.count()
        umos = await self._store.pending_umos()
        next_ts, next_asleep = next_change(now, self._windows)
        delta = max(0, int(next_ts - now))
        next_desc = (
            f"{datetime.fromtimestamp(next_ts).strftime('%H:%M')} "
            f"{'入睡' if next_asleep else '醒来'}（还有 {delta // 3600}h{delta % 3600 // 60}m）"
            if delta
            else "无切换"
        )

        lines = [
            "🌙 作息表",
            f"状态：{sleep_state_label(asleep)}",
            f"窗口：{windows_str(self._windows)}（配置值 {self._window_raw}）",
            f"下次切换：{next_desc}",
            f"待回放：{total} 条 / {len(umos)} 个会话",
            f"开关：enabled={self._cfg_bool('enabled', True)}，"
            f"replay_on_wake={self._cfg_bool('replay_on_wake', True)}，"
            f"检查间隔={max(5, self._cfg_int('wake_check_seconds', 60))}s",
            f"白名单：{len(self._whitelist)} 个会话",
        ]
        if self._window_error:
            lines.append(f"⚠️ 窗口配置有误，已退回默认：{self._window_error}")
        lines.append("用法：/作息 回放 手动回放一次")
        yield event.plain_result("\n".join(lines))

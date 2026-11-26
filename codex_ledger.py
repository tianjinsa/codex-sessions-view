"""Codex Session Ledger：JSONL 用量解析与价格计算。

只读取本地日志，不访问网络。费用为根据用户配置单价计算的估算值。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable

ZERO = Decimal("0")
MILLION = Decimal("1000000")
UNKNOWN_MODEL = "未知模型"


@dataclass(frozen=True)
class ModelPrice:
    """每百万 tokens 的输入、缓存输入和输出单价。"""

    input: Decimal
    cached_input: Decimal
    output: Decimal
    note: str = ""


@dataclass(frozen=True)
class PriceConfig:
    models: dict[str, ModelPrice]
    currency: str = "USD"
    currency_symbol: str = "$"

    def find(self, model: str) -> ModelPrice | None:
        """优先精确匹配，也允许显式后缀 ``*`` 匹配带版本的模型。"""
        if model in self.models:
            return self.models[model]
        matches = sorted(
            (
                (key, price)
                for key, price in self.models.items()
                if key.endswith("*")
                and not key.startswith("*")
                and model.startswith(key[:-1])
                and len(model) > len(key) - 1
            ),
            key=lambda item: len(item[0]),
            reverse=True,
        )
        return matches[0][1] if matches else None


class ConfigError(ValueError):
    """价格配置存在可以定位的错误。"""


def default_config_path() -> Path:
    """随程序保存配置；打包后仍可写，并可用命令行覆盖路径。"""
    return Path(__file__).resolve().with_name("prices.json")


def default_sessions_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "sessions"


def load_config(path: Path | str = default_config_path()) -> PriceConfig:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ConfigError(f"找不到价格配置：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"价格配置 JSON 格式错误：第 {exc.lineno} 行，{exc.msg}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
        raise ConfigError("价格配置必须包含 models 对象。")
    models: dict[str, ModelPrice] = {}
    for name, item in raw["models"].items():
        if not isinstance(name, str) or not name.strip() or not isinstance(item, dict):
            raise ConfigError("模型名称不能为空，且每个模型配置必须是对象。")
        name = name.strip()
        if any(char in name for char in "*?[]"):
            if name.count("*") != 1 or not name.endswith("*") or len(name) < 2:
                raise ConfigError(f"模型 {name!r} 的通配规则无效；只支持例如 gpt-5-* 的后缀星号。")
        values = []
        for field in ("input", "cached_input", "output"):
            try:
                value = Decimal(str(item.get(field, 0)))
            except (InvalidOperation, ValueError):
                raise ConfigError(f"模型 {name} 的 {field} 不是有效数字。") from None
            if not value.is_finite() or value < 0:
                raise ConfigError(f"模型 {name} 的 {field} 必须是大于等于 0 的有限数字。")
            values.append(value)
        models[name] = ModelPrice(*values, note=str(item.get("note", "")))
    symbol = str(raw.get("currency_symbol", "$")).strip()
    return PriceConfig(
        models=models,
        currency=str(raw.get("currency", "USD")).strip() or "USD",
        currency_symbol=symbol or "$",
    )


def save_config(config: PriceConfig, path: Path | str) -> None:
    """原子保存 GUI 的价格配置，避免中途失败留下半个 JSON。"""
    # 走相同的校验逻辑，确保手动编辑和 GUI 编辑的规则一致。
    payload = config_to_dict(config)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def config_to_dict(config: PriceConfig) -> dict:
    return {
        "currency": config.currency,
        "currency_symbol": config.currency_symbol,
        "models": {
            name: {
                "input": decimal_text(price.input),
                "cached_input": decimal_text(price.cached_input),
                "output": decimal_text(price.output),
                **({"note": price.note} if price.note else {}),
            }
            for name, price in sorted(config.models.items())
        },
    }


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _token(usage: dict, *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    # reasoning_output_tokens 已包含在 output_tokens 内，只用于展示，不额外计费。
    reasoning_output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_output_tokens + other.reasoning_output_tokens,
        )

    @property
    def uncached_input_tokens(self) -> int:
        # 新版本 Codex 的 input_tokens 包括 cached_input_tokens。
        # 兼容偶尔出现的“未缓存输入 + 缓存输入”旧格式，避免算出负数。
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def visible_output_tokens(self) -> int:
        """输出中除思考之外的部分。reasoning 缺失时按 0 处理。"""
        return max(0, self.output_tokens - self.reasoning_output_tokens)

    @property
    def reasoning_rate(self) -> float:
        """思考 token 占全部输出 token 的比例。"""
        return self.reasoning_output_tokens / self.output_tokens if self.output_tokens else 0.0

    @property
    def cache_hit_rate(self) -> float:
        """输入 token 中命中缓存的比例；没有输入时为 0。"""
        return self.cached_input_tokens / self.input_tokens if self.input_tokens else 0.0

    @classmethod
    def from_dict(cls, raw: dict) -> Usage:
        return cls(
            input_tokens=_token(raw, "input_tokens", "prompt_tokens"),
            cached_input_tokens=_token(
                raw, "cached_input_tokens", "cache_read_input_tokens", "cached_tokens"
            ),
            output_tokens=_token(raw, "output_tokens", "completion_tokens"),
            reasoning_output_tokens=_token(raw, "reasoning_output_tokens"),
        )


@dataclass(frozen=True)
class UsageRecord:
    timestamp: datetime
    model: str
    turn_id: str
    response_id: str
    usage: Usage
    line_number: int
    # 这条请求发出前，上下文是否被压缩过。压缩会重建会话历史，
    # 上一次请求的 token 不再全部可复用，因此不能当作缓存上限。
    after_compaction: bool = False

    def cost(self, config: PriceConfig) -> Decimal | None:
        price = config.find(self.model)
        if price is None:
            return None
        return self.cost_breakdown(config)[0]

    def cost_breakdown(self, config: PriceConfig) -> tuple[Decimal, CostParts | None]:
        """返回总额与三部分构成；未配置价格时构成为 None。"""
        price = config.find(self.model)
        if price is None:
            return ZERO, None
        parts = CostParts(
            uncached_input=Decimal(self.usage.uncached_input_tokens) * price.input / MILLION,
            cached_input=Decimal(self.usage.cached_input_tokens) * price.cached_input / MILLION,
            output=Decimal(self.usage.output_tokens) * price.output / MILLION,
        )
        return parts.total, parts


@dataclass(frozen=True)
class AgentMessage:
    author: str
    text: str
    starts_turn: bool
    line_number: int


@dataclass
class Turn:
    """一个任务轮次：从用户提问开始，到 GPT 完成或中断/异常结束为止。

    中间的工具调用续跑、自动压缩产生的内部 turn_id 都会并入这一轮，
    只按 turn_id 记账会把一次任务拆成多段，因此这里以用户提问为起点。
    """

    turn_id: str
    records: list[UsageRecord]
    started_at: datetime | None = None
    completed_at: datetime | None = None
    user_prompt: str = ""
    last_agent_message: str = ""
    aborted: bool = False
    # outcome 说明这一轮怎么结束的：completed / aborted / interrupted / unfinished
    outcome: str = "unfinished"
    # 纯模型内部循环（工具续跑、自动压缩、无提问子轮）并入任务轮时留痕。
    absorbed_auto_turns: int = 0
    # 任务进行中用户补充的消息；它们与首个提问共享 turn_id，不另算轮次。
    follow_ups: list[str] = field(default_factory=list)
    reported_duration: float | None = None
    parent_turn_id: str = ""
    agent_messages: list[AgentMessage] = field(default_factory=list)

    @property
    def ended_unexpectedly(self) -> bool:
        """中断、异常或没有正常收尾，仍算一个任务轮次。"""
        return self.outcome in {"aborted", "interrupted", "unfinished"}

    @property
    def usage(self) -> Usage:
        return sum_usage(record.usage for record in self.records)

    @property
    def models(self) -> list[str]:
        return sorted({record.model for record in self.records if record.model})

    @property
    def start(self) -> datetime | None:
        return self.started_at or (self.records[0].timestamp if self.records else None)

    def title(self) -> str:
        """用用户提问、委派任务或最终回复生成标题。"""
        delegated = next((message.text for message in self.agent_messages if message.starts_turn), "")
        for text in (self.user_prompt, delegated, self.last_agent_message):
            summary = _summary(text, 120)
            if summary:
                return summary
        return f"轮次 {self.turn_id[:8]}" if self.turn_id else "未识别轮次"

    @property
    def duration_seconds(self) -> float | None:
        """任务从开始到完成/中断的墙钟时长；缺任一端时返回 None。"""
        if self.reported_duration is not None:
            return self.reported_duration
        if self.start and self.completed_at:
            return max(0.0, (self.completed_at - self.start).total_seconds())
        return None

    def summary_text(self, limit: int = 64) -> str:
        """表格里用的短标题。"""
        return _summary(self.title(), limit)

    @property
    def status_text(self) -> str:
        return {
            "completed": "已完成",
            "aborted": "已中断",
            "interrupted": "异常结束",
            "unfinished": "未正常收尾",
        }.get(self.outcome, self.outcome)


@dataclass(frozen=True)
class CacheCeiling:
    """缓存命中率的理论上限与额外失效统计。

    上下文是连续累积的：第 N 次请求的输入，等于第 N-1 次的完整上下文
    （input + output）再加上本轮新增内容。因此：

    - **硬上限**：本次 `cached_input_tokens` 不可能超过本次 `input_tokens`；
    - **可复用上限（保守）**：上一轮的 `input_tokens` 已经确定存在于上下文中，
      只要前缀未变就必然能命中。这是"理论上应当命中"的下限保证；
    - **可复用上限（乐观）**：上一轮的 `input + output` 也进入了上下文，
      但其中 output 能否命中取决于服务端前缀缓存的边界。

    首次请求没有"上一次"，不预估上限，直接以它的实际命中作为上限。
    "额外失效"只统计**本次命中低于上一次命中**的下降量：命中不低于上次时，
    说明缓存基本保持，不算额外失效。
    """

    input_tokens: int = 0
    actual_cached: int = 0
    compared_input_tokens: int = 0
    compared_cached: int = 0
    ideal_prompt_only: int = 0
    ideal_with_output: int = 0
    gap_prompt_only: int = 0
    gap_with_output: int = 0
    # 本次命中 < 上次命中的下降量之和，这才是真正的“额外缓存失效”。
    extra_eviction: int = 0
    eviction_events: int = 0
    # 命中超过“上次 input+output”的累计量；这是上下文增长导致的正常情况。
    exceeded_tokens: int = 0
    eligible: int = 0
    requests: int = 0
    exceeded: int = 0

    def __add__(self, other: CacheCeiling) -> CacheCeiling:
        return CacheCeiling(
            input_tokens=self.input_tokens + other.input_tokens,
            actual_cached=self.actual_cached + other.actual_cached,
            compared_input_tokens=self.compared_input_tokens + other.compared_input_tokens,
            compared_cached=self.compared_cached + other.compared_cached,
            ideal_prompt_only=self.ideal_prompt_only + other.ideal_prompt_only,
            ideal_with_output=self.ideal_with_output + other.ideal_with_output,
            gap_prompt_only=self.gap_prompt_only + other.gap_prompt_only,
            gap_with_output=self.gap_with_output + other.gap_with_output,
            extra_eviction=self.extra_eviction + other.extra_eviction,
            eviction_events=self.eviction_events + other.eviction_events,
            exceeded_tokens=self.exceeded_tokens + other.exceeded_tokens,
            eligible=self.eligible + other.eligible,
            requests=self.requests + other.requests,
            exceeded=self.exceeded + other.exceeded,
        )

    @staticmethod
    def _rate(value: int, base: int) -> float:
        return value / base if base else 0.0

    @property
    def actual_rate(self) -> float:
        """全部请求的实际命中率（含第一次请求）。"""
        return self._rate(self.actual_cached, self.input_tokens)

    @property
    def compared_actual_rate(self) -> float:
        """只统计有上一次可复用的请求，与上限同口径。"""
        return self._rate(self.compared_cached, self.compared_input_tokens)

    @property
    def prompt_only_rate(self) -> float:
        return self._rate(self.ideal_prompt_only, self.compared_input_tokens)

    @property
    def with_output_rate(self) -> float:
        return self._rate(self.ideal_with_output, self.compared_input_tokens)

    @property
    def exceeded_ratio(self) -> float:
        """命中高于“上一轮输入+输出”乐观上限的比例。

        这是上下文增长与 tokenizer 边界差异造成的正常现象，
        只用于说明口径，不代表缓存异常。
        """
        return self.exceeded_tokens / self.input_tokens if self.input_tokens else 0.0


def cache_ceiling(records: Iterable[UsageRecord]) -> CacheCeiling:
    """计算一组**同一会话内**请求的缓存命中率理论上限与实际值。

    必须按会话分组调用：跨会话按时间配对，会把无关的请求当成“上一次”，
    让上限失真。

    第一次请求没有“上一次”可参考，因此不预估上限，直接把它视为已达理论最优：
    上限就等于它自己的实际缓存值，缺口为 0。这样所有请求都能归入同一口径，
    上限与实际的分子分母完全一致，相加即可核对。
    """
    ordered = sorted(records, key=lambda record: (record.timestamp, record.line_number))
    result = CacheCeiling()
    previous: Usage | None = None
    for record in ordered:
        usage = record.usage
        if record.after_compaction:
            # 压缩会重建上下文：上一次请求的 token 已不可复用，
            # 但压缩摘要本身可能已被缓存，因此以这条请求自身作为新基线，
            # 避免把压缩开销误算成“缓存变差”。
            previous = usage
            continue
        if not usage.input_tokens:
            previous = usage
            continue
        cached = usage.cached_input_tokens
        if previous is None:
            # 首次请求：不预估，上限取实际值本身。
            ideal_in = ideal_io = cached
            extra_eviction = 0
        else:
            # 保守上限：上一轮输入已确定在上下文里，前缀不变就必然命中。
            ideal_in = min(previous.input_tokens, usage.input_tokens)
            # 乐观上限：上一轮输出也进入了下轮上下文，但服务端是否缓存它取决于
            # 前缀缓存边界。超过这个乐观上限才值得注意。
            ideal_io = min(previous.input_tokens + previous.output_tokens, usage.input_tokens)
            # 只有本次命中低于上次命中，才算额外失效。
            drop = previous.cached_input_tokens - cached
            extra_eviction = max(0, drop)
        gap_in = max(0, ideal_in - cached)
        gap_io = max(0, ideal_io - cached)
        result = result + CacheCeiling(
            input_tokens=usage.input_tokens,
            actual_cached=cached,
            compared_input_tokens=usage.input_tokens,
            compared_cached=cached,
            ideal_prompt_only=ideal_in,
            ideal_with_output=ideal_io,
            gap_prompt_only=gap_in,
            gap_with_output=gap_io,
            extra_eviction=extra_eviction,
            eviction_events=1 if extra_eviction else 0,
            exceeded_tokens=max(0, cached - ideal_io),
            eligible=1,
            requests=1,
            exceeded=1 if cached > ideal_io else 0,
        )
        previous = usage
    return result


def cache_ceiling_many(groups: Iterable[Iterable[UsageRecord]]) -> CacheCeiling:
    """把多个会话的上限相加；不同会话之间互不影响。"""
    total = CacheCeiling()
    for records in groups:
        total = total + cache_ceiling(records)
    return total


@dataclass(frozen=True)
class CostParts:
    """一笔花费按非缓存输入 / 缓存输入 / 输出拆开的结果。"""

    uncached_input: Decimal = ZERO
    cached_input: Decimal = ZERO
    output: Decimal = ZERO

    def __add__(self, other: CostParts) -> CostParts:
        return CostParts(
            self.uncached_input + other.uncached_input,
            self.cached_input + other.cached_input,
            self.output + other.output,
        )

    @property
    def total(self) -> Decimal:
        return self.uncached_input + self.cached_input + self.output

    def ratio(self, value: Decimal) -> float:
        total = self.total
        return float(value / total) if total else 0.0

    def items(self) -> list[tuple[str, Decimal, float]]:
        """(名称, 金额, 占比)，供表格与图表共用。"""
        return [
            ("非缓存输入", self.uncached_input, self.ratio(self.uncached_input)),
            ("缓存输入", self.cached_input, self.ratio(self.cached_input)),
            ("输出", self.output, self.ratio(self.output)),
        ]


def aggregate_cost_breakdown(
    records: Iterable[UsageRecord], config: PriceConfig
) -> tuple[CostParts, int]:
    """汇总费用构成；返回 (构成, 未定价 token 数)。"""
    parts = CostParts()
    unpriced = 0
    for record in records:
        total, record_parts = record.cost_breakdown(config)
        if record_parts is None:
            unpriced += record.usage.total_tokens
        else:
            parts = parts + record_parts
    return parts, unpriced


@dataclass(frozen=True)
class SubagentInfo:
    parent_thread_id: str = ""
    nickname: str = ""
    path: str = ""
    role: str = ""
    depth: int | None = None


@dataclass
class Session:
    path: Path
    session_id: str
    cwd: str
    source: str
    records: list[UsageRecord]
    parse_errors: list[str]
    turns: list[Turn] = field(default_factory=list)
    created_at: datetime | None = None
    recognized: bool = True
    first_prompt: str = ""
    subagent: SubagentInfo | None = None

    @property
    def is_subagent(self) -> bool:
        return self.subagent is not None

    @property
    def start(self) -> datetime | None:
        if self.records:
            return self.records[0].timestamp
        return self.created_at or (self.turns[0].start if self.turns else None)

    @property
    def end(self) -> datetime | None:
        if self.records:
            return self.records[-1].timestamp
        return (self.turns[-1].completed_at or self.turns[-1].start) if self.turns else self.created_at

    @property
    def models(self) -> list[str]:
        return sorted({record.model for record in self.records if record.model})

    @property
    def usage(self) -> Usage:
        return sum_usage(record.usage for record in self.records)

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def title(self) -> str:
        if self.subagent:
            name = self.subagent.nickname or self.subagent.path or self.session_id[:8]
            return f"子智能体 · {name}"
        prompt = self.first_prompt or (self.turns[0].user_prompt if self.turns else "")
        return _summary(prompt, 120) or self.workspace

    @property
    def workspace(self) -> str:
        return self.cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or self.path.stem


@dataclass(frozen=True)
class ScanResult:
    sessions: list[Session]
    skipped_files: int = 0
    skipped: list[str] = field(default_factory=list)
    file_errors: list[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return len(self.sessions) + self.skipped_files

    @property
    def parse_errors(self) -> list[str]:
        return [
            f"{session.path} · {error}"
            for session in self.sessions
            for error in session.parse_errors
        ] + self.file_errors


def sum_usage(usages: Iterable[Usage]) -> Usage:
    result = Usage()
    for usage in usages:
        result = result + usage
    return result


def _timestamp(raw: object) -> datetime | None:
    try:
        if isinstance(raw, str):
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
        if type(raw) is int:
            return datetime.fromtimestamp(raw, timezone.utc)
    except (ValueError, OSError, OverflowError):
        pass
    return None


def _text(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _subagent_info(payload: dict) -> SubagentInfo | None:
    source = payload.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    spawn = spawn if isinstance(spawn, dict) else {}
    parent_thread_id = _text(spawn, "parent_thread_id") or _text(payload, "parent_thread_id")
    nickname = _text(spawn, "agent_nickname") or _text(payload, "agent_nickname")
    path = _text(spawn, "agent_path") or _text(payload, "agent_path")
    thread_source = _text(payload, "thread_source")
    marked = isinstance(subagent, (dict, str)) or source == "subagent" or thread_source == "subagent"
    if not marked and (thread_source == "user" or not (parent_thread_id and (path or nickname))):
        return None
    depth = spawn.get("depth", payload.get("depth"))
    return SubagentInfo(
        parent_thread_id=parent_thread_id,
        nickname=nickname,
        path=path,
        role=_text(spawn, "agent_role") or _text(payload, "agent_role"),
        depth=depth if type(depth) is int and depth >= 0 else None,
    )


def _summary(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def _message_text(content: object, *, user: bool = False) -> str:
    """保留对话换行，过滤响应历史中的环境与指令注入块。"""
    if isinstance(content, str):
        content = [{"text": content}]
    if not isinstance(content, list):
        return ""
    chunks = []
    for chunk in content:
        if not isinstance(chunk, dict):
            continue
        if user and chunk.get("type") in ("input_image", "image", "local_image"):
            chunks.append("［图片］")
            continue
        text = chunk.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if user and text.lstrip().startswith(("<environment_context>", "<user_instructions>", "# AGENTS.md instructions")):
            continue
        chunks.append(text)
    return "\n".join(chunks)


@dataclass(frozen=True)
class _Prompt:
    source: str
    message_id: str
    text: str
    timestamp: datetime | None
    line_number: int


def _merge_prompts(messages: list[_Prompt]) -> list[str]:
    """合并相邻格式的消息回显，保留用户实际发送的重复消息。"""
    prompts: list[tuple[_Prompt, set[str]]] = []
    seen_ids: set[tuple[str, str]] = set()
    for message in messages:
        identity = (message.source, message.message_id)
        if message.message_id:
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
        match = None
        for previous, sources in reversed(prompts):
            if message.line_number - previous.line_number > 24:
                break
            if message.source in sources or message.text.strip() != previous.text.strip():
                continue
            if message.timestamp and previous.timestamp:
                if abs((message.timestamp - previous.timestamp).total_seconds()) > 5:
                    continue
            match = sources
            break
        if match is None:
            prompts.append((message, {message.source}))
        else:
            match.add(message.source)
    return [message.text for message, _ in prompts]


def _legacy_delta(current: Usage, previous: Usage | None) -> Usage:
    if previous is None or (
        current.input_tokens < previous.input_tokens or current.output_tokens < previous.output_tokens
    ):
        return current
    return Usage(
        current.input_tokens - previous.input_tokens,
        current.cached_input_tokens - previous.cached_input_tokens
        if current.cached_input_tokens >= previous.cached_input_tokens
        else current.cached_input_tokens,
        current.output_tokens - previous.output_tokens,
        max(0, current.reasoning_output_tokens - previous.reasoning_output_tokens),
    )


def _deduplicate_usage(
    records: list[UsageRecord],
    legacy_lines: dict[int, tuple[str, Usage | None]],
    replayed_lines: set[int],
    root_turns: dict[str, str],
) -> list[UsageRecord]:
    """只把旧事件与此前尚未匹配的新版用量一对一配对。"""
    unmatched: dict[Usage, list[UsageRecord]] = {}
    matched_snapshot: tuple[str, Usage, Usage] | None = None
    kept = []
    for record in records:
        if record.after_compaction:
            unmatched.clear()
            matched_snapshot = None
        if record.line_number not in legacy_lines:
            unmatched.setdefault(record.usage, []).append(record)
            matched_snapshot = None
        else:
            turn_id, total = legacy_lines[record.line_number]
            snapshot = (turn_id, total, record.usage) if total is not None else None
            # 向子智能体递送消息时，日志可能再次发布上一个调用的同一份累计快照。
            if snapshot is not None and snapshot == matched_snapshot:
                continue
            candidates = unmatched.get(record.usage, [])
            nearest = None
            delay = 120.0
            for index in range(len(candidates) - 1, -1, -1):
                candidate = candidates[index]
                if turn_id and turn_id not in (candidate.turn_id, root_turns.get(candidate.turn_id)):
                    continue
                distance = abs((candidate.timestamp - record.timestamp).total_seconds())
                if distance <= delay:
                    nearest, delay = index, distance
                    if distance == 0:
                        break
            if nearest is not None:
                candidates.pop(nearest)
                if not candidates:
                    del unmatched[record.usage]
                matched_snapshot = snapshot
                continue
            matched_snapshot = None
        if record.line_number not in replayed_lines:
            kept.append(record)
    return kept


class _ScanCancelled(Exception):
    pass


def parse_session(
    path: Path | str, *, cancelled: Callable[[], bool] | None = None
) -> Session:
    """逐行识别 Codex 事件，保留用量和任务的提问、补充与最终回复。"""
    path = Path(path)
    records: list[UsageRecord] = []
    errors: list[str] = []
    model_by_turn: dict[str, str] = {}
    first_model_by_turn: dict[str, str] = {}
    inferred_models: set[int] = set()
    root_turns: dict[str, str] = {}
    active_model = UNKNOWN_MODEL
    active_turn = ""
    session_id = path.stem
    cwd = source = ""
    created_at = None
    subagent = None
    recognized = False
    seen_responses: set[str] = set()
    seen_agent_messages: set[str] = set()
    replayed_lines: set[int] = set()
    legacy_total: Usage | None = None
    legacy_lines: dict[int, tuple[str, Usage | None]] = {}
    turn_meta: dict[str, Turn] = {}
    prompts: dict[str, list[_Prompt]] = {}
    pending_prompts: list[_Prompt] = []
    pending_compaction = False
    agent_message_starts_turn = False

    def turn_span(turn_id: str) -> Turn:
        if turn_id not in turn_meta:
            turn_meta[turn_id] = Turn(turn_id=turn_id, records=[])
        return turn_meta[turn_id]

    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, 1):
            if cancelled and cancelled():
                raise _ScanCancelled
            if not raw.strip():
                continue
            try:
                line = raw.decode("utf-8-sig" if line_number == 1 else "utf-8")
                if not line.strip():
                    continue
                event = json.loads(line)
            except UnicodeDecodeError:
                errors.append(f"第 {line_number} 行：含有无效的 UTF-8 字节")
                continue
            except json.JSONDecodeError as exc:
                errors.append(f"第 {line_number} 行：JSON 无法解析（{exc.msg}）")
                continue
            except (ValueError, RecursionError):
                errors.append(f"第 {line_number} 行：JSON 超出解析限制")
                continue
            if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                continue
            payload = event["payload"]
            kind = _text(event, "type")
            if kind == "inter_agent_communication_metadata":
                agent_message_starts_turn = payload.get("trigger_turn") is True
                continue
            starts_turn, agent_message_starts_turn = agent_message_starts_turn, False
            event_type = _text(payload, "type") if kind == "event_msg" else ""
            response_message = kind == "response_item" and payload.get("type") == "message"
            agent_message = subagent is not None and kind == "response_item" and payload.get("type") == "agent_message"
            if (
                kind not in {"session_meta", "turn_context", "token_usage_record", "compacted"}
                and event_type not in {
                    "task_started", "task_complete", "turn_aborted", "context_compacted",
                    "user_message", "agent_message", "item_completed", "token_count",
                }
                and not (response_message or agent_message)
            ):
                continue

            recognized = True
            timestamp = _timestamp(event.get("timestamp")) or _timestamp(payload.get("timestamp"))
            if kind == "session_meta":
                session_id = _text(payload, "id", "session_id") or session_id
                cwd = _text(payload, "cwd") or cwd
                source = _text(payload, "originator", "source") or source
                subagent = _subagent_info(payload) or subagent
                created_at = created_at or _timestamp(payload.get("timestamp")) or timestamp
                active_model = _text(payload, "model") or active_model
                continue
            if kind == "compacted" or event_type == "context_compacted":
                pending_compaction = True
                continue

            metadata = payload.get("internal_chat_message_metadata_passthrough")
            root_turn = _text(payload, "root_turn_id")
            explicit_turn = _text(payload, "turn_id")
            if not explicit_turn and isinstance(metadata, dict):
                explicit_turn = _text(metadata, "turn_id", "root_turn_id")
            explicit_turn = explicit_turn or root_turn
            if root_turn and explicit_turn != root_turn:
                root_turns[explicit_turn] = root_turn
            if explicit_turn and (kind == "turn_context" or event_type == "task_started"):
                active_turn = explicit_turn
            if explicit_turn and pending_prompts:
                prompts.setdefault(explicit_turn, []).extend(pending_prompts)
                pending_prompts.clear()
                active_turn = active_turn or explicit_turn
            turn_id = explicit_turn or active_turn

            if agent_message and subagent:
                author = _text(payload, "author")
                recipient = _text(payload, "recipient")
                if not turn_id or not author or not recipient or author == recipient:
                    continue
                if recipient not in (subagent.path, session_id):
                    continue
                message_id = _text(payload, "id")
                if message_id and message_id in seen_agent_messages:
                    continue
                text = _message_text(payload.get("content"))
                if text.startswith("Message Type:"):
                    headers, separator, body = text.partition("\nPayload:")
                    if separator:
                        starts_turn = starts_turn or headers.splitlines()[0] == "Message Type: NEW_TASK"
                        text = body.removeprefix(" ").removeprefix("\n")
                if text.strip():
                    turn_span(turn_id).agent_messages.append(AgentMessage(author, text, starts_turn, line_number))
                    if message_id:
                        seen_agent_messages.add(message_id)
                continue

            if kind == "turn_context":
                model = _text(payload, "model")
                if model:
                    active_model = model
                    if turn_id:
                        model_by_turn[turn_id] = model
                        first_model_by_turn.setdefault(turn_id, model)
                continue
            if event_type in {"task_started", "task_complete", "turn_aborted"}:
                if not turn_id and pending_prompts and event_type != "task_started":
                    turn_id = f"line-{pending_prompts[0].line_number}"
                    prompts[turn_id] = pending_prompts.copy()
                    pending_prompts.clear()
                if not turn_id:
                    continue
                span = turn_span(turn_id)
                if event_type == "task_started":
                    span.started_at = span.started_at or timestamp or _timestamp(payload.get("started_at"))
                    attribution = payload.get("turn_attribution")
                    if isinstance(attribution, dict):
                        span.parent_turn_id = _text(attribution, "parent_turn_id") or span.parent_turn_id
                else:
                    span.completed_at = timestamp or _timestamp(payload.get("completed_at")) or span.completed_at
                    span.started_at = span.started_at or _timestamp(payload.get("started_at"))
                    span.aborted = event_type == "turn_aborted"
                    span.outcome = "aborted" if span.aborted else "completed"
                    duration_ms = payload.get("duration_ms")
                    if type(duration_ms) is int and 0 <= duration_ms < 2**64:
                        span.reported_duration = duration_ms / 1000
                    message = _message_text(payload.get("last_agent_message"))
                    if message:
                        span.last_agent_message = message
                    if active_turn == turn_id:
                        active_turn = ""
                continue

            item = payload.get("item") if event_type == "item_completed" else payload
            item = item if isinstance(item, dict) else {}
            role = payload.get("role") if response_message else None
            if event_type == "user_message" or role == "user" or item.get("type") in ("UserMessage", "userMessage"):
                content = payload.get("message") if event_type == "user_message" else item.get("content")
                message = _message_text(content, user=True)
                if not message and event_type == "user_message" and (payload.get("images") or payload.get("local_images")):
                    message = "［图片］"
                if message:
                    message_id = _text(item, "client_id", "message_id", "id")
                    prompt = _Prompt(event_type or kind, message_id, message, timestamp, line_number)
                    if turn_id:
                        prompts.setdefault(turn_id, []).append(prompt)
                        active_turn = active_turn or turn_id
                    else:
                        pending_prompts.append(prompt)
                continue
            if turn_id and item.get("phase") in ("final_answer", "finalAnswer"):
                if event_type == "agent_message" or role == "assistant" or item.get("type") in ("AgentMessage", "agentMessage"):
                    span = turn_span(turn_id)
                    message = _message_text(item.get("text") or item.get("content") or item.get("message"))
                    if message and (span.outcome != "completed" or not span.last_agent_message):
                        span.last_agent_message = message
                continue

            model = _text(payload, "model")
            response_id = ""
            if kind == "token_usage_record":
                raw_usage = payload.get("usage")
                if not isinstance(raw_usage, dict):
                    continue
                usage = Usage.from_dict(raw_usage)
                response_id = _text(payload, "response_id")
            elif event_type == "token_count" and isinstance(payload.get("info"), dict):
                info = payload["info"]
                last, total = info.get("last_token_usage"), info.get("total_token_usage")
                current = Usage.from_dict(total) if isinstance(total, dict) else None
                if isinstance(last, dict):
                    usage = Usage.from_dict(last)
                elif current is not None:
                    usage = _legacy_delta(current, legacy_total)
                else:
                    continue
                legacy_total = current if current is not None else (legacy_total or Usage()) + usage
                model = model or _text(info, "model")
            else:
                continue

            if not (usage.total_tokens or usage.cached_input_tokens):
                continue
            if timestamp is None:
                errors.append(f"第 {line_number} 行：用量记录缺少有效时间戳，已跳过")
                continue
            replayed = bool(response_id and response_id in seen_responses)
            if replayed:
                replayed_lines.add(line_number)
            elif response_id:
                seen_responses.add(response_id)
            if event_type == "token_count":
                legacy_lines[line_number] = explicit_turn, current
            specific_model = model or model_by_turn.get(turn_id) or model_by_turn.get(root_turn)
            if not specific_model and turn_id:
                inferred_models.add(line_number)
            records.append(UsageRecord(
                timestamp=timestamp,
                model=specific_model or active_model,
                turn_id=turn_id,
                response_id=response_id or f"line-{line_number}",
                usage=usage,
                line_number=line_number,
                after_compaction=pending_compaction and not replayed,
            ))
            if not replayed:
                pending_compaction = False

    if pending_prompts:
        prompts[f"line-{pending_prompts[0].line_number}"] = pending_prompts
    for turn_id, messages in prompts.items():
        span = turn_span(turn_id)
        span.user_prompt, *span.follow_ups = _merge_prompts(messages)
        span.started_at = span.started_at or next(
            (message.timestamp for message in messages if message.timestamp), None
        )
    if legacy_lines and len(legacy_lines) != len(records):
        records = _deduplicate_usage(records, legacy_lines, replayed_lines, root_turns)
    elif replayed_lines:
        records = [record for record in records if record.line_number not in replayed_lines]
    if inferred_models:
        for index, record in enumerate(records):
            if record.line_number in inferred_models and record.turn_id in first_model_by_turn:
                records[index] = replace(record, model=first_model_by_turn[record.turn_id])
    records.sort(key=lambda record: (record.timestamp, record.line_number))
    turns = _group_turns(turn_meta, records, root_turns, subagent=subagent is not None)
    return Session(
        path=path.resolve(),
        session_id=session_id,
        cwd=cwd,
        source=source,
        records=records,
        parse_errors=errors,
        turns=turns,
        created_at=created_at,
        recognized=recognized,
        first_prompt=turns[0].user_prompt if turns else "",
        subagent=subagent,
    )


def _group_turns(
    spans: dict[str, Turn],
    records: list[UsageRecord],
    root_turns: dict[str, str],
    *,
    subagent: bool = False,
) -> list[Turn]:
    """按任务 ID 归属用量，旧日志中缺 ID 的请求按时间归属。"""
    unassigned = []
    for record in records:
        if record.turn_id:
            span = spans.get(record.turn_id)
            if span is None:
                span = spans[record.turn_id] = Turn(record.turn_id, [])
            span.records.append(record)
        else:
            unassigned.append(record)
    if subagent and unassigned and not spans:
        spans[""] = Turn("", [])
    ordered = sorted(spans.values(), key=_turn_start, reverse=True)
    for record in unassigned:
        owner = min(ordered, key=lambda span: _turn_distance(span, record.timestamp), default=None)
        if owner:
            owner.records.append(record)
    if subagent:
        # 子智能体由父会话委派，日志通常只有执行边界和回复。
        tasks = [
            span for span in spans.values()
            if span.user_prompt or span.started_at or span.outcome != "unfinished" or span.parent_turn_id
            or any(message.starts_turn for message in span.agent_messages)
        ]
        if not tasks:
            tasks = [span for span in spans.values() if span.records]
    else:
        tasks = [span for span in spans.values() if span.user_prompt]
    tasks.sort(key=_turn_start)
    tasks_by_id = {task.turn_id: task for task in tasks}
    for span in spans.values():
        if span.turn_id in tasks_by_id or not tasks:
            continue
        owner = tasks_by_id.get(root_turns.get(span.turn_id, "")) or _owner_task(tasks, span)
        if owner is None:
            continue
        owner.records.extend(span.records)
        owner.agent_messages.extend(span.agent_messages)
        owner.absorbed_auto_turns += 1
    for task in tasks:
        task.records.sort(key=lambda record: (record.timestamp, record.line_number))
        task.agent_messages.sort(key=lambda message: message.line_number)
    return tasks


def _turn_start(span: Turn) -> datetime:
    return span.start or datetime.min.replace(tzinfo=timezone.utc)


def _owner_task(tasks: list[Turn], span: Turn) -> Turn | None:
    """内部轮归属到开始时间早于它的最近任务轮；早于全部任务时归属第一个。"""
    moment = span.start
    if moment is None:
        return None
    candidates = [task for task in tasks if task.start and task.start <= moment]
    if candidates:
        return candidates[-1]
    return tasks[0] if tasks else None


def _turn_distance(span: Turn, moment: datetime) -> float:
    """记录离轮次区间有多近，用于无 turn_id 日志的归并。"""
    if span.start is None:
        return float("inf")
    end = span.completed_at or span.start
    return max(0.0, (span.start - moment).total_seconds(), (moment - end).total_seconds())


def discover_jsonl(
    root: Path | str, *, cancelled: Callable[[], bool] | None = None
) -> list[Path]:
    """查找 JSONL 文件。选择 Codex 主目录时只扫描其中的 sessions。"""
    root = Path(root).expanduser().resolve()
    if root.is_file():
        return [root] if root.suffix.lower() == ".jsonl" else []
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")
    if (root.name.lower() == ".codex" or root == default_sessions_path().parent.resolve()) and (
        root / "sessions"
    ).is_dir():
        root /= "sessions"
    paths: list[Path] = []
    for folder, _, files in os.walk(root):
        if cancelled and cancelled():
            break
        paths.extend(Path(folder, name) for name in files if name.lower().endswith(".jsonl"))
    return sorted(paths, key=lambda path: (str(path).casefold(), str(path)))


def scan_sessions(
    root: Path | str,
    progress: Callable[[int, int], bool] | None = None,
    *,
    cancelled: Callable[[], bool] | None = None,
) -> ScanResult:
    paths = discover_jsonl(root, cancelled=cancelled)
    sessions = []
    skipped = []
    file_errors = []
    for index, path in enumerate(paths):
        if (cancelled and cancelled()) or (progress and not progress(index, len(paths))):
            break
        try:
            session = parse_session(path, cancelled=cancelled)
        except _ScanCancelled:
            break
        except OSError as exc:
            skipped.append(f"{path} · 无法读取")
            file_errors.append(f"{path} · {exc.strerror or exc}")
            continue
        if session.recognized:
            sessions.append(session)
        else:
            skipped.append(f"{path} · 缺少可识别的 Codex 事件")
            file_errors.extend(f"{path} · {error}" for error in session.parse_errors)
    else:
        if progress:
            progress(len(paths), len(paths))
    return ScanResult(sessions, len(skipped), skipped, file_errors)


def aggregate_cost(
    records: Iterable[UsageRecord], config: PriceConfig
) -> tuple[Decimal, int, set[str]]:
    """返回已定价费用、未定价 token 数和未知模型名称。"""
    total = ZERO
    unpriced_tokens = 0
    unknown_models: set[str] = set()
    for record in records:
        cost = record.cost(config)
        if cost is None:
            unpriced_tokens += record.usage.total_tokens
            unknown_models.add(record.model)
        else:
            total += cost
    return total, unpriced_tokens, unknown_models


def parse_amount(text: str, field: str) -> Decimal:
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise ConfigError(f"{field} 必须是有效数字。") from None
    if not value.is_finite() or value < 0:
        raise ConfigError(f"{field} 必须是大于等于 0 的有限数字。")
    return value


def with_model(config: PriceConfig, name: str, price: ModelPrice) -> PriceConfig:
    models = dict(config.models)
    models[name] = price
    return replace(config, models=models)

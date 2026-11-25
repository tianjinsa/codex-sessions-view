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
        """优先用用户提问，其次用最终回复，最后才用 ID。"""
        for text in (self.user_prompt, self.last_agent_message):
            normalized = " ".join(text.split())
            if normalized:
                return normalized[:120] + ("…" if len(normalized) > 120 else "")
        return f"轮次 {self.turn_id[:8]}" if self.turn_id else "未识别轮次"

    @property
    def duration_seconds(self) -> float | None:
        """任务从开始到完成/中断的墙钟时长；缺任一端时返回 None。"""
        if self.started_at and self.completed_at:
            return max(0.0, (self.completed_at - self.started_at).total_seconds())
        return None

    def summary_text(self, limit: int = 64) -> str:
        """表格里用的短标题；完整提问保留在 title() 和悬停提示中。"""
        text = " ".join(self.title().split())
        return text if len(text) <= limit else text[:limit] + "…"

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


@dataclass
class Session:
    path: Path
    session_id: str
    cwd: str
    source: str
    records: list[UsageRecord]
    parse_errors: list[str]
    turns: list[Turn] = field(default_factory=list)

    @property
    def start(self) -> datetime | None:
        return self.records[0].timestamp if self.records else None

    @property
    def end(self) -> datetime | None:
        return self.records[-1].timestamp if self.records else None

    @property
    def models(self) -> list[str]:
        return sorted({record.model for record in self.records if record.model})

    @property
    def usage(self) -> Usage:
        return sum_usage(record.usage for record in self.records)

    @property
    def turn_count(self) -> int:
        return sum(1 for turn in self.turns if turn.records)

    @property
    def title(self) -> str:
        return Path(self.cwd).name if self.cwd else self.path.stem


@dataclass(frozen=True)
class ScanResult:
    sessions: list[Session]
    skipped_files: int = 0

    @property
    def parse_errors(self) -> list[str]:
        return [error for session in self.sessions for error in session.parse_errors]


def sum_usage(usages: Iterable[Usage]) -> Usage:
    result = Usage()
    for usage in usages:
        result = result + usage
    return result


def _timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _clean_prompt(text: str) -> str:
    """去掉用户文本里的 Markdown 转义反斜杠与多余空白，便于列表辨认。"""
    cleaned = text.replace("\\#", "#").replace("\\*", "*").replace("\\`", "`")
    cleaned = cleaned.replace("\\:", ":").replace("\\/", "/").replace("\\_", "_")
    cleaned = cleaned.replace("\\-", "-").replace("\\[", "[").replace("\\]", "]")
    return " ".join(cleaned.split())


def _user_prompt_text(content: object) -> str:
    """从 content 数组中取出真正由用户输入的文本，忽略环境与指令注入块。"""
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for chunk in content:
        if not isinstance(chunk, dict):
            continue
        text = chunk.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        stripped = text.strip()
        if stripped.startswith("<environment_context>") or stripped.startswith("<user_instructions>"):
            continue
        if stripped.startswith("# AGENTS.md instructions"):
            continue
        chunks.append(stripped)
    return _clean_prompt("\n".join(chunks))


def parse_session(path: Path | str) -> Session:
    """逐行解析大文件；仅保留用量记录，不在内存保存对话内容。"""
    path = Path(path)
    records: list[UsageRecord] = []
    errors: list[str] = []
    model_by_turn: dict[str, str] = {}
    active_model = "未知模型"
    session_id = path.stem
    cwd = ""
    source = ""
    seen_responses: set[str] = set()
    # 旧版 token_count 的累计值以 (会话, turn, 模型) 隔离，遇到回退则重新累计。
    legacy_totals: dict[tuple[str, str], Usage] = {}
    modern_lines: set[int] = set()
    legacy_lines: list[int] = []
    turn_meta: dict[str, Turn] = {}
    # 压缩后的第一条请求无法复用压缩前的上下文。
    compaction_seen = False
    pending_compaction = False

    def turn_span(turn_id: str) -> Turn | None:
        if not turn_id:
            return None
        return turn_meta.setdefault(turn_id, Turn(turn_id=turn_id, records=[]))

    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"第 {line_number} 行：JSON 无法解析（{exc.msg}）")
                continue
            if not isinstance(event, dict):
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = event.get("type")

            if kind == "session_meta":
                session_id = str(payload.get("id") or payload.get("session_id") or session_id)
                cwd = str(payload.get("cwd") or "")
                source = str(payload.get("originator") or payload.get("source") or "")
            elif kind == "turn_context":
                model = payload.get("model")
                if isinstance(model, str) and model:
                    active_model = model
                    turn_id = str(payload.get("turn_id") or payload.get("root_turn_id") or "")
                    if turn_id:
                        model_by_turn[turn_id] = model
            elif kind == "compacted":
                # 新版本遇到压缩后会重置累计 token_count；record 型用量不受影响。
                legacy_totals.clear()
                compaction_seen = True
                pending_compaction = True

            # 轮次边界：task_started / task_complete / turn_aborted 都带 turn_id。
            # 旧版 CLI 还会把用户与 agent 文本回显为 event_msg，这里只取摘要文本。
            if kind == "event_msg":
                event_type = payload.get("type")
                event_turn_id = str(payload.get("turn_id") or payload.get("root_turn_id") or "")
                span = turn_span(event_turn_id)
                if event_type == "task_started" and span:
                    span.started_at = _timestamp(event.get("timestamp")) or span.started_at
                elif event_type == "task_complete" and span:
                    span.completed_at = _timestamp(event.get("timestamp")) or span.completed_at
                    span.outcome = "completed"
                    message = payload.get("last_agent_message")
                    if isinstance(message, str) and message.strip():
                        span.last_agent_message = _clean_prompt(message)
                elif event_type == "turn_aborted" and span:
                    span.aborted = True
                    span.outcome = "aborted"
                    span.completed_at = _timestamp(event.get("timestamp")) or span.completed_at
                elif event_type == "user_message" and span:
                    message = payload.get("message")
                    if isinstance(message, str) and message.strip():
                        prompt = _clean_prompt(message)
                        if not span.user_prompt:
                            span.user_prompt = prompt
                        elif prompt not in span.follow_ups:
                            span.follow_ups.append(prompt)

            if kind == "response_item" and payload.get("type") == "message":
                # 分页模式（0.147+）不写 user_message 回显，用户提问只在 response_item 中。
                if payload.get("role") == "user":
                    metadata = payload.get("internal_chat_message_metadata_passthrough")
                    metadata = metadata if isinstance(metadata, dict) else {}
                    message_turn_id = str(metadata.get("turn_id") or "")
                    prompt = _user_prompt_text(payload.get("content"))
                    span = turn_span(message_turn_id)
                    if span and prompt:
                        if not span.user_prompt:
                            span.user_prompt = prompt
                        else:
                            # 同一 turn_id 的后续消息是任务进行中的补充，不算新轮次。
                            span.follow_ups.append(prompt)

            timestamp = _timestamp(event.get("timestamp"))
            usage = None
            response_id = ""
            turn_id = str(payload.get("turn_id") or "")
            record_key: tuple[str, str] | None = None

            if kind == "token_usage_record" and isinstance(payload.get("usage"), dict):
                # usage 是本次 API 调用；turn_token_usage 和 thread_token_usage 都是累计快照，不能累加。
                usage = Usage.from_dict(payload["usage"])
                response_id = str(payload.get("response_id") or "")
                if response_id and response_id in seen_responses:
                    continue
                if response_id:
                    seen_responses.add(response_id)
                modern_lines.add(line_number)
            elif (
                kind == "event_msg"
                and payload.get("type") == "token_count"
                and isinstance(payload.get("info"), dict)
            ):
                info = payload["info"]
                if isinstance(info.get("last_token_usage"), dict):
                    usage = Usage.from_dict(info["last_token_usage"])
                elif isinstance(info.get("total_token_usage"), dict):
                    current = Usage.from_dict(info["total_token_usage"])
                    turn_key = turn_id or "default"
                    record_key = (turn_key, active_model)
                    previous = legacy_totals.get(record_key, Usage())
                    # total_token_usage 是快照。若新值变小，说明上下文被压缩/重置。
                    # 缓存项可能在新一轮重建而减少；以总输入/输出回退判断快照重置。
                    if (
                        current.input_tokens >= previous.input_tokens
                        and current.output_tokens >= previous.output_tokens
                    ):
                        usage = Usage(
                            current.input_tokens - previous.input_tokens,
                            current.cached_input_tokens
                            if current.cached_input_tokens < previous.cached_input_tokens
                            else current.cached_input_tokens - previous.cached_input_tokens,
                            current.output_tokens - previous.output_tokens,
                        )
                    legacy_totals[record_key] = current
                if record_key is None:
                    record_key = (turn_id or "default", active_model)

            if usage is None or not any(
                (usage.input_tokens, usage.cached_input_tokens, usage.output_tokens)
            ):
                continue
            if timestamp is None:
                errors.append(f"第 {line_number} 行：用量记录缺少有效时间戳，已跳过")
                continue
            model = model_by_turn.get(turn_id, active_model)
            if line_number not in modern_lines:
                legacy_lines.append(len(records))
            records.append(
                UsageRecord(
                    timestamp=timestamp,
                    model=model,
                    turn_id=turn_id,
                    response_id=response_id or f"line-{line_number}",
                    usage=usage,
                    line_number=line_number,
                    after_compaction=pending_compaction,
                )
            )
            pending_compaction = False

    if modern_lines and legacy_lines:
        # 过渡期日志常把同一次调用同时写为 record 和 event_msg。
        # 按用量、邻近时间一对一匹配，不能把文件前段仅有的旧事件一起丢弃。
        legacy_indices = set(legacy_lines)
        unmatched_modern: dict[Usage, list[UsageRecord]] = {}
        for index, record in enumerate(records):
            if index not in legacy_indices:
                unmatched_modern.setdefault(record.usage, []).append(record)
        duplicates: set[int] = set()
        for index in legacy_indices:
            legacy = records[index]
            candidates = [
                candidate
                for candidate in unmatched_modern.get(legacy.usage, [])
                if candidate.line_number < legacy.line_number
            ]
            nearest = min(
                range(len(candidates)),
                key=lambda position: abs((candidates[position].timestamp - legacy.timestamp).total_seconds()),
                default=-1,
            )
            if nearest >= 0 and abs(
                (candidates[nearest].timestamp - legacy.timestamp).total_seconds()
            ) <= 120:
                duplicates.add(index)
                candidates.pop(nearest)
        records = [record for index, record in enumerate(records) if index not in duplicates]
    records.sort(key=lambda record: (record.timestamp, record.line_number))
    turns = _group_turns(turn_meta, records)
    return Session(
        path=path.resolve(),
        session_id=session_id,
        cwd=cwd,
        source=source,
        records=records,
        parse_errors=errors,
        turns=turns,
    )


def _group_turns(turn_meta: dict[str, Turn], records: list[UsageRecord]) -> list[Turn]:
    """把用量归到 turn_id；没有 turn_id 的记录按时间并入最近的已知轮次。

    只返回任务轮次：一次用户提问开启、直到完成或异常结束为止。
    没有任何用户提问的轮次（纯工具续跑、自动压缩、中断的空会话）不计为任务。
    """
    spans: dict[str, Turn] = {}
    for turn_id, span in turn_meta.items():
        spans[turn_id] = Turn(
            turn_id=turn_id,
            records=[],
            started_at=span.started_at,
            completed_at=span.completed_at,
            user_prompt=span.user_prompt,
            last_agent_message=span.last_agent_message,
            aborted=span.aborted,
            outcome=span.outcome,
            follow_ups=list(span.follow_ups),
        )
    for record in records:
        turn_id = record.turn_id or ""
        if turn_id:
            span = spans.get(turn_id)
            if span is None:
                span = Turn(turn_id=turn_id, records=[])
                spans[turn_id] = span
            span.records.append(record)
            continue
        # 早期日志可能没有 turn_id，用与已有轮次的时间距离归并，避免凭空多出一轮。
        if spans:
            owner = min(
                spans.values(),
                key=lambda span: _turn_distance(span, record.timestamp),
            )
            owner.records.append(record)
            continue
        spans.setdefault("", Turn(turn_id="", records=[])).records.append(record)
    _absorb_auto_turns(spans)
    result = []
    for span in spans.values():
        if not span.records:
            continue
        span.records.sort(key=lambda record: (record.timestamp, record.line_number))
        result.append(span)
    result.sort(key=lambda span: span.start or datetime.min.replace(tzinfo=timezone.utc))
    return result


def _is_task_turn(span: Turn) -> bool:
    """只有带真实用户提问的轮次才算任务轮次。"""
    return bool(span.user_prompt.strip())


def _absorb_auto_turns(spans: dict[str, Turn]) -> None:
    """把纯模型内部循环（工具续跑、自动压缩、无提问子轮）并入它所属的任务轮。

    这样既不会把内部轮冒充成任务轮，也不会丢失这些轮次真实产生的 token 费用。
    """
    tasks = [span for span in spans.values() if span.user_prompt.strip()]
    if not tasks:
        # 整份日志没有任何用户提问（如中断的空会话），不把内部轮冒充成任务轮。
        spans.clear()
        return
    tasks.sort(key=lambda span: span.start or datetime.min.replace(tzinfo=timezone.utc))
    for turn_id, span in list(spans.items()):
        if span.user_prompt.strip():
            continue
        owner = _owner_task(tasks, span)
        if owner is None:
            continue
        for record in span.records:
            owner.records.append(replace(record, turn_id=owner.turn_id or record.turn_id))
        owner.absorbed_auto_turns += 1
        owner.follow_ups.extend(span.follow_ups)
        if span.aborted:
            owner.aborted = True
        if span.outcome in {"aborted", "interrupted"}:
            owner.outcome = span.outcome
        if span.completed_at and (owner.completed_at is None or span.completed_at > owner.completed_at):
            owner.completed_at = span.completed_at
        del spans[turn_id]


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
    if span.started_at and span.completed_at:
        if span.started_at <= moment <= span.completed_at:
            return 0.0
        if moment < span.started_at:
            return (span.started_at - moment).total_seconds()
    if span.records:
        if moment < span.records[0].timestamp:
            return (span.records[0].timestamp - moment).total_seconds()
        return (moment - span.records[-1].timestamp).total_seconds()
    return float("inf")


def discover_jsonl(root: Path | str) -> tuple[list[Path], int]:
    """自动处理 .codex/sessions 父目录，避免重复或错误扫描归档。"""
    root = Path(root).expanduser().resolve()
    if root.is_file():
        return ([root] if root.suffix.lower() == ".jsonl" else []), 0
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在：{root}")
    candidates = [root]
    if root.name.lower() == ".codex" and (root / "sessions").is_dir():
        candidates = [root / "sessions"]
    # 归档目录不会自动加入，避免与正常归档副本重复。
    paths: list[Path] = []
    for candidate in candidates:
        paths.extend(candidate.rglob("*.jsonl"))
    return sorted(set(paths), key=lambda p: str(p).lower()), 0


def scan_sessions(
    root: Path | str,
    progress: Callable[[int, int], bool] | None = None,
) -> ScanResult:
    paths, skipped = discover_jsonl(root)
    sessions = []
    for index, path in enumerate(paths, 1):
        if progress and not progress(index - 1, len(paths)):
            break
        try:
            sessions.append(parse_session(path))
        except OSError as exc:
            sessions.append(
                Session(
                    path=path,
                    session_id=path.stem,
                    cwd="",
                    source="",
                    records=[],
                    parse_errors=[f"文件读取失败：{exc}"],
                )
            )
    return ScanResult(sessions=sessions, skipped_files=skipped)


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

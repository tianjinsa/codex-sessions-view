"""核心解析、计价与 Qt 界面的回归测试。运行：python -m unittest -v"""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from codex_ledger import (
    ConfigError,
    ModelPrice,
    PriceConfig,
    Usage,
    UsageRecord,
    aggregate_cost,
    config_to_dict,
    load_config,
    parse_session,
    save_config,
    scan_sessions,
)


def event(kind: str, payload: dict, timestamp="2026-11-18T16:12:00.800Z") -> str:
    return json.dumps(
        {"timestamp": timestamp, "type": kind, "payload": payload}, ensure_ascii=False
    )


def usage(input_tokens: int, cached: int, output: int, reasoning: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": input_tokens + output,
    }


def record(response_id: str, tokens: dict, turn="turn-a", timestamp="2026-11-18T16:12:00.800Z") -> str:
    return event(
        "token_usage_record",
        {
            "turn_id": turn,
            "response_id": response_id,
            "usage": tokens,
            "turn_token_usage": tokens,
            "thread_token_usage": tokens,
        },
        timestamp,
    )


def count(total: dict, last: dict | None, turn="turn-a", timestamp="2026-11-18T16:12:00.800Z") -> str:
    info = {"total_token_usage": total}
    if last is not None:
        info["last_token_usage"] = last
    return event("event_msg", {"type": "token_count", "turn_id": turn, "info": info}, timestamp)


class JsonlTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, lines: list[str], name="session.jsonl") -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def metadata(self):
        return event("session_meta", {"id": "session-a", "cwd": "D:/work/我的项目"})

    def context(self, model="gpt-test", turn="turn-a"):
        return event("turn_context", {"turn_id": turn, "model": model})

    def test_user_sample_uses_increment_not_total_and_reasoning_not_double_billed(self):
        tokens = usage(22767, 14720, 707, 663)
        path = self.write([self.metadata(), self.context(), count(tokens, tokens)])
        session = parse_session(path)
        self.assertEqual(len(session.records), 1)
        self.assertEqual(session.usage, Usage(22767, 14720, 707, 663))
        self.assertEqual(session.usage.reasoning_output_tokens, 663)
        self.assertEqual(session.usage.visible_output_tokens, 44)
        self.assertEqual(session.usage.uncached_input_tokens, 8047)
        config = PriceConfig({"gpt-test": ModelPrice(Decimal(2), Decimal("0.1"), Decimal(10))})
        # 思考 token 已含在输出内，费用不应因它改变。
        self.assertEqual(aggregate_cost(session.records, config)[0], Decimal("0.024636"))

    def test_neighboring_equal_legacy_calls_are_not_deduplicated(self):
        tokens = usage(100, 20, 10)
        path = self.write([
            self.context(), count(tokens, tokens),
            count(tokens, tokens, timestamp="2026-11-18T16:13:00Z"),
            record("new", tokens, timestamp="2026-11-18T16:14:00Z"),
        ])
        self.assertEqual(len(parse_session(path).records), 3)

    def test_modern_record_and_legacy_event_are_not_double_counted(self):
        first = usage(1000, 400, 100)
        second = usage(2000, 1500, 200)
        path = self.write([
            self.metadata(), self.context(),
            record("response-a", first), count(first, first),
            record("response-b", second, timestamp="2026-11-18T16:13:00Z"),
            count(usage(3000, 1900, 300), second, timestamp="2026-11-18T16:13:00Z"),
        ])
        session = parse_session(path)
        self.assertEqual(len(session.records), 2)
        self.assertEqual(session.usage, Usage(3000, 1900, 300))

    def test_mixed_format_keeps_unmatched_legacy_request(self):
        old = usage(500, 100, 50)
        new = usage(1000, 400, 100)
        path = self.write([
            self.context(), count(old, old),
            record("new-response", new, timestamp="2026-11-18T16:20:00Z"),
            count(new, new, timestamp="2026-11-18T16:20:00Z"),
        ])
        session = parse_session(path)
        self.assertEqual(len(session.records), 2)
        self.assertEqual(session.usage, Usage(1500, 500, 150))

    def test_duplicate_response_id_is_ignored(self):
        path = self.write([
            self.context(), record("same", usage(100, 20, 5)), record("same", usage(100, 20, 5)),
        ])
        self.assertEqual(parse_session(path).usage, Usage(100, 20, 5))

    def test_total_only_snapshots_are_differenced(self):
        path = self.write([
            self.context(),
            count(usage(1000, 400, 50), None),
            count(usage(2500, 1200, 150), None, timestamp="2026-11-18T16:13:00Z"),
        ])
        self.assertEqual(parse_session(path).usage, Usage(2500, 1200, 150))
        self.assertEqual(len(parse_session(path).records), 2)

    def test_compaction_resets_legacy_snapshot(self):
        path = self.write([
            self.context(),
            count(usage(1000, 400, 50), None),
            event("compacted", {"turn_id": "turn-a"}, "2026-11-18T16:13:00Z"),
            count(usage(600, 500, 30), None, timestamp="2026-11-18T16:14:00Z"),
        ])
        self.assertEqual(parse_session(path).usage, Usage(1600, 900, 80))

    def test_cache_rebuild_does_not_drop_legacy_usage(self):
        path = self.write([
            self.context(),
            count(usage(1000, 900, 50), None),
            count(usage(1200, 100, 70), None, timestamp="2026-11-18T16:13:00Z"),
        ])
        self.assertEqual(parse_session(path).usage, Usage(1200, 1000, 70))

    def test_reasoning_tokens_are_shown_but_billed_once(self):
        path = self.write([
            self.context("m", "t"),
            record("r1", usage(0, 0, 500, 400), turn="t"),
        ])
        totals = parse_session(path).usage
        self.assertEqual(totals.output_tokens, 500)
        self.assertEqual(totals.reasoning_output_tokens, 400)
        self.assertEqual(totals.visible_output_tokens, 100)
        self.assertAlmostEqual(totals.reasoning_rate, 0.8)
        config = PriceConfig({"m": ModelPrice(Decimal(1), Decimal(1), Decimal(10))})
        # 只按 output_tokens 计费一次：500 × 10 / 1e6 = 0.005。
        self.assertEqual(aggregate_cost(parse_session(path).records, config)[0], Decimal("0.005"))

    def test_usage_addition_keeps_reasoning(self):
        total = Usage(10, 4, 5, 3) + Usage(20, 10, 7, 2)
        self.assertEqual(total, Usage(30, 14, 12, 5))

    def test_models_are_resolved_per_turn(self):
        path = self.write([
            self.context("model-a", "a"), record("r1", usage(10, 2, 1), turn="a"),
            self.context("model-b", "b"), record("r2", usage(20, 4, 2), turn="b", timestamp="2026-11-18T16:13:00Z"),
        ])
        self.assertEqual(parse_session(path).models, ["model-a", "model-b"])

    def user_message(self, turn: str, text: str, timestamp="2026-11-18T16:12:00.800Z") -> str:
        return json.dumps({
            "timestamp": timestamp,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
                "internal_chat_message_metadata_passthrough": {"turn_id": turn},
            },
        }, ensure_ascii=False)

    def task_started(self, turn: str, timestamp="2026-11-18T16:12:00.800Z") -> str:
        return event("event_msg", {"type": "task_started", "turn_id": turn}, timestamp)

    def task_complete(self, turn: str, message: str = "", timestamp="2026-11-18T16:14:00.800Z") -> str:
        return event(
            "event_msg",
            {"type": "task_complete", "turn_id": turn, "last_agent_message": message},
            timestamp,
        )

    def test_turn_costs_sum_to_session_total(self):
        path = self.write([
            self.context("m", "t1"),
            self.user_message("t1", "任务一"),
            self.task_started("t1"),
            record("r1", usage(1000, 400, 50), turn="t1"),
            self.task_complete("t1", "完成一"),
            self.context("m", "t2"),
            self.user_message("t2", "任务二", "2026-11-18T16:20:00Z"),
            record("r2", usage(2000, 1900, 80), turn="t2", timestamp="2026-11-18T16:21:00Z"),
            self.task_complete("t2", "完成二", "2026-11-18T16:22:00Z"),
        ])
        session = parse_session(path)
        config = PriceConfig({"m": ModelPrice(Decimal(2), Decimal("0.1"), Decimal(10))})
        whole = aggregate_cost(session.records, config)[0]
        per_turn = sum(aggregate_cost(turn.records, config)[0] for turn in session.turns)
        self.assertEqual(whole, per_turn)
        self.assertEqual(session.turn_count, 2)

    def test_task_turn_groups_follow_up_messages_not_new_turns(self):
        path = self.write([
            self.context("m", "t1"),
            self.user_message("t1", "启动任务"),
            self.task_started("t1"),
            record("r1", usage(100, 20, 10), turn="t1"),
            # 任务进行中补充消息：同一 turn_id，不能算新轮次。
            self.user_message("t1", "顺便再补充一点", "2026-11-18T16:12:30Z"),
            record("r2", usage(200, 150, 20), turn="t1", timestamp="2026-11-18T16:13:00Z"),
            self.task_complete("t1", "完成"),
        ])
        turns = parse_session(path).turns
        self.assertEqual(len(turns), 1)
        self.assertEqual(len(turns[0].follow_ups), 1)
        self.assertEqual(turns[0].outcome, "completed")
        self.assertEqual(turns[0].usage, Usage(300, 170, 30))

    def test_injected_user_blocks_do_not_count_as_prompt(self):
        path = self.write([
            self.user_message("t1", "# AGENTS.md instructions for D:/code\n<INSTRUCTIONS>x</INSTRUCTIONS>"),
            self.user_message("t1", "<environment_context>\n<cwd>D:/x</cwd>\n</environment_context>"),
            record("r1", usage(10, 2, 1), turn="t1"),
        ])
        turns = parse_session(path).turns
        # 没有任何真实提问，这一轮不是任务轮次。
        self.assertEqual(turns, [])

    def test_tool_only_internal_turn_is_absorbed_into_task_turn(self):
        path = self.write([
            self.context("m", "t1"),
            self.user_message("t1", "启动任务"),
            self.task_started("t1"),
            record("r1", usage(100, 20, 10), turn="t1"),
            # 自动压缩/工具续跑产生的独立 turn_id，没有用户提问。
            self.task_started("t2", "2026-11-18T16:13:00Z"),
            record("r2", usage(500, 480, 40), turn="t2", timestamp="2026-11-18T16:13:30Z"),
            self.task_complete("t2", timestamp="2026-11-18T16:13:40Z"),
            self.task_complete("t1", "完成"),
        ])
        session = parse_session(path)
        self.assertEqual(len(session.turns), 1)
        self.assertEqual(session.turns[0].absorbed_auto_turns, 1)
        self.assertEqual(session.turns[0].usage, Usage(600, 500, 50))
        self.assertEqual(len(session.records), 2)

    def test_aborted_task_turn_still_counts(self):
        path = self.write([
            self.context("m", "t1"),
            self.user_message("t1", "启动任务"),
            self.task_started("t1"),
            record("r1", usage(100, 20, 10), turn="t1"),
            event("event_msg", {"type": "turn_aborted", "turn_id": "t1", "reason": "interrupted"},
                  "2026-11-18T16:13:00Z"),
        ])
        turns = parse_session(path).turns
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].outcome, "aborted")
        self.assertTrue(turns[0].ended_unexpectedly)

    def test_unfinished_task_turn_is_kept(self):
        path = self.write([
            self.context("m", "t1"),
            self.user_message("t1", "启动任务"),
            record("r1", usage(100, 20, 10), turn="t1"),
        ])
        turns = parse_session(path).turns
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].outcome, "unfinished")
        self.assertTrue(turns[0].ended_unexpectedly)

    def test_unknown_model_is_reported_not_free(self):
        path = self.write([self.context("unknown"), record("r1", usage(100, 20, 5))])
        cost, unpriced, models = aggregate_cost(parse_session(path).records, PriceConfig({}))
        self.assertEqual(cost, Decimal(0))
        self.assertEqual(unpriced, 105)
        self.assertEqual(models, {"unknown"})

    def test_invalid_line_is_reported_and_valid_lines_survive(self):
        path = self.write([self.context(), "not json", record("r1", usage(10, 2, 1))])
        session = parse_session(path)
        self.assertEqual(len(session.records), 1)
        self.assertIn("第 2 行", session.parse_errors[0])

    def test_scan_single_file_and_nested_directory(self):
        path = self.write([self.context(), record("r1", usage(10, 2, 1))], "2026/11/a.jsonl")
        self.assertEqual(len(scan_sessions(path).sessions), 1)
        self.assertEqual(len(scan_sessions(self.root).sessions), 1)
        with self.assertRaises(FileNotFoundError):
            scan_sessions(self.root / "none.jsonl")

    def test_codex_home_selects_sessions(self):
        self.write([self.context(), record("r1", usage(10, 2, 1))], ".codex/sessions/2026/a.jsonl")
        self.assertEqual(len(scan_sessions(self.root / ".codex").sessions), 1)


class PriceConfigTest(unittest.TestCase):
    def test_exact_and_longest_prefix_match(self):
        config = PriceConfig({
            "gpt-*": ModelPrice(Decimal(1), Decimal(2), Decimal(3)),
            "gpt-test": ModelPrice(Decimal(4), Decimal(5), Decimal(6)),
        })
        self.assertEqual(config.find("gpt-test").input, 4)
        self.assertEqual(config.find("gpt-other").input, 1)
        self.assertIsNone(config.find("other"))
        self.assertIsNone(config.find("gpt-"))

    def test_decimal_round_trip_and_unicode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.json"
            expected = PriceConfig(
                {"模型-a": ModelPrice(Decimal("0.123456789"), Decimal(0), Decimal(10), "备注")},
                "CNY", "¥",
            )
            save_config(expected, path)
            actual = load_config(path)
            self.assertEqual(actual, expected)
            self.assertEqual(config_to_dict(actual)["models"]["模型-a"]["input"], "0.123456789")

    def test_invalid_config_is_rejected(self):
        for value in (-1, "nan", "abc", "Infinity"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "prices.json"
                path.write_text(json.dumps({"models": {"x": {"input": value}}}), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(path)


# 必须在导入 PyQt6 之前设置无显示服务器环境的渲染方式。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402
from main import (  # noqa: E402
    HOVER_HTML_ROLE,
    LedgerWindow,
    PriceDialog,
    STYLE,
    duration_text,
    filtered_sessions,
    output_item,
    tooltip_html,
)


class TooltipTest(unittest.TestCase):
    def test_blocks_are_split_with_colors_and_rules(self):
        html = tooltip_html([
            ("首条提问", "#11745F", "第一段"),
            ("补充 1", "#4979B8", "第二段"),
        ])
        self.assertIn("第一段", html)
        self.assertIn("第二段", html)
        # 两个区块之间必须有一条分隔线，且各用各自颜色。
        self.assertEqual(html.count("<hr"), 1)
        self.assertIn("#11745F", html)
        self.assertIn("#4979B8", html)

    def test_tooltip_escapes_html_like_prompt_text(self):
        html = tooltip_html([("提问", "#000000", "<img src=x onerror=alert(1)> & <b>粗体</b>")])
        self.assertNotIn("<img", html)
        self.assertNotIn("<b>", html)
        self.assertIn("&lt;img", html)
        self.assertIn("&amp;", html)

    def test_output_tooltip_reports_reasoning(self):
        item = output_item(Usage(1000, 800, 500, 400))
        self.assertEqual(item.text(), "500")
        # 原生 tooltip 必须留空，否则会和可滚动浮层同时弹出。
        self.assertEqual(item.toolTip(), "")
        tip = item.data(HOVER_HTML_ROLE)
        self.assertIn("400", tip)
        self.assertIn("80.0%", tip)
        self.assertIn("100", tip)
        self.assertIn("不额外计费", tip)

    def test_output_tooltip_without_reasoning(self):
        item = output_item(Usage(10, 2, 5))
        self.assertEqual(item.toolTip(), "")
        tip = item.data(HOVER_HTML_ROLE)
        self.assertIn("5 tokens", tip)
        self.assertIn("没有 reasoning", tip)

    def test_duration_text_formats(self):
        self.assertEqual(duration_text(None), "—")
        self.assertEqual(duration_text(33), "00:00:33")
        self.assertEqual(duration_text(95), "00:01:35")
        self.assertEqual(duration_text(3 * 3600 + 2 * 60), "03:02:00")
        self.assertEqual(duration_text(26 * 3600), "26:00:00")


class HoverPanelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_panel(self):
        from main import HoverPanel
        from PyQt6.QtCore import QPoint, QRect
        panel = HoverPanel(None)
        long_html = tooltip_html([("补充 1", "#4979B8", "很长的内容 " * 300)])
        panel.show_html(long_html, QPoint(100, 100), QRect(0, 0, 1200, 700))
        self.addCleanup(panel.close)
        return panel

    def test_panel_height_is_capped_and_scrollable(self):
        panel = self.make_panel()
        self.assertLessEqual(panel.height(), 520)
        bar = panel.scroll.verticalScrollBar()
        # 内容超过浮层高度时必须能滚动，而不是像 QToolTip 那样被截断。
        self.assertGreater(bar.maximum(), 0)

    def test_panel_stays_inside_available_area(self):
        from PyQt6.QtCore import QPoint, QRect
        from main import HoverPanel
        panel = HoverPanel(None)
        self.addCleanup(panel.close)
        panel.show_html(tooltip_html([("x", "#000000", "y")]), QPoint(1180, 690), QRect(0, 0, 1200, 700))
        geometry = panel.geometry()
        self.assertLessEqual(geometry.right(), 1200)
        self.assertLessEqual(geometry.bottom(), 700)

    def test_wheel_scrolls_panel_content(self):
        from PyQt6.QtCore import QPoint, QPointF, Qt
        from PyQt6.QtGui import QWheelEvent
        panel = self.make_panel()
        bar = panel.scroll.verticalScrollBar()
        bar.setValue(0)
        event = QWheelEvent(
            QPointF(10, 10), QPointF(10, 10), QPoint(0, 0), QPoint(0, -120),
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase, False,
        )
        panel.wheelEvent(event)
        self.assertGreater(bar.value(), 0)


class ChartTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_chart(self):
        from main import SpendChart
        chart = SpendChart()
        self.addCleanup(chart.close)
        return chart

    def test_bars_carry_visible_value_labels(self):
        from decimal import Decimal
        chart = self.make_chart()
        chart.set_values([("11-16", Decimal("11.617")), ("11-17", Decimal("15.502"))], "$")
        texts = [text.get_text() for text in chart.axes.texts]
        # 金额直接写在图上，不依赖鼠标悬浮。
        self.assertIn("$11.617", texts)
        self.assertIn("$15.502", texts)

    def test_dates_are_rendered_on_x_axis(self):
        from decimal import Decimal
        chart = self.make_chart()
        chart.set_values([("11-16", Decimal("1")), ("11-17", Decimal("2"))], "$")
        labels = [tick.get_text() for tick in chart.axes.get_xticklabels()]
        self.assertEqual(labels, ["11-16", "11-17"])

    def test_empty_range_shows_hint_instead_of_blank(self):
        chart = self.make_chart()
        chart.set_values([], "$")
        texts = [text.get_text() for text in chart.axes.texts]
        self.assertIn("所选范围内暂无费用记录", texts)

    def test_only_last_fourteen_days_are_kept(self):
        from decimal import Decimal
        chart = self.make_chart()
        chart.set_values([(f"d{i}", Decimal("1")) for i in range(30)], "$")
        self.assertEqual(len(chart.values), 14)


class CacheCeilingTest(unittest.TestCase):
    def record(self, second: int, input_tokens: int, cached: int, output: int = 0):
        return UsageRecord(
            datetime(2026, 11, 18, 16, 12, second, tzinfo=timezone.utc),
            "m", "t", f"r{second}", Usage(input_tokens, cached, output), second,
        )

    def test_ceiling_uses_previous_request_as_upper_bound(self):
        from codex_ledger import cache_ceiling
        records = [
            self.record(0, 1000, 0, 500),
            self.record(1, 2000, 1200, 100),
        ]
        ceiling = cache_ceiling(records)
        # 第二次请求最多能复用上一次的 input（1000）；若含输出则 1500。
        self.assertEqual(ceiling.ideal_prompt_only, 1000)
        self.assertEqual(ceiling.ideal_with_output, 1500)
        self.assertEqual(ceiling.actual_cached, 1200)
        self.assertEqual(ceiling.gap_with_output, 300)

    def test_ceiling_capped_by_current_input(self):
        from codex_ledger import cache_ceiling
        records = [
            self.record(0, 9000, 0, 3000),
            self.record(1, 500, 100, 10),
        ]
        ceiling = cache_ceiling(records)
        # 当前输入只有 500，上限不可能超过它。
        self.assertEqual(ceiling.ideal_prompt_only, 500)
        self.assertEqual(ceiling.ideal_with_output, 500)

    def test_single_request_has_no_reusable_context(self):
        from codex_ledger import cache_ceiling
        ceiling = cache_ceiling([self.record(0, 1000, 640, 100)])
        self.assertEqual(ceiling.requests, 1)
        # 首次请求没有可参考的“上一次”，上限直接取实际值。
        self.assertEqual(ceiling.ideal_prompt_only, 640)
        self.assertEqual(ceiling.gap_prompt_only, 0)
        self.assertAlmostEqual(ceiling.actual_rate, 0.64)
        self.assertEqual(ceiling.exceeded, 0)

    def test_actual_above_ceiling_is_flagged(self):
        from codex_ledger import cache_ceiling
        records = [
            self.record(0, 100, 0, 0),
            self.record(1, 1000, 900, 0),
        ]
        ceiling = cache_ceiling(records)
        # 上一次只产生 100 token，却命中了 900：超出乐观上限的部分会被记录，
        # 但这是上下文增长导致的正常现象，不应影响实际命中率统计。
        self.assertEqual(ceiling.exceeded, 1)
        self.assertEqual(ceiling.exceeded_tokens, 800)
        self.assertAlmostEqual(ceiling.actual_rate, 900 / 1100)

    def test_sessions_are_not_paired_across_boundaries(self):
        from codex_ledger import cache_ceiling_many
        a = [self.record(0, 1000, 0, 500), self.record(1, 1500, 1400, 100)]
        b = [self.record(2, 800, 0, 300), self.record(3, 1100, 900, 50)]
        combined = cache_ceiling_many([a, b])
        # 每个会话的第一次请求都不该拿到另一个会话的上限。
        self.assertEqual(combined.requests, 4)
        self.assertEqual(combined.ideal_prompt_only, min(1000, 1500) + min(800, 1100))
        self.assertEqual(combined.actual_rate, combined.actual_cached / combined.input_tokens)

    def test_gap_is_sum_of_per_request_shortfalls(self):
        from codex_ledger import cache_ceiling
        # 两次请求各有缺口：1000-900=100、1000-500=500，逐请求累加应为 600。
        records = [
            self.record(0, 1000, 0, 0),
            self.record(1, 1000, 900, 0),
            self.record(2, 1000, 500, 0),
        ]
        ceiling = cache_ceiling(records)
        self.assertEqual(ceiling.gap_prompt_only, 600)
        self.assertEqual(ceiling.exceeded, 0)

    def test_compaction_starts_new_baseline(self):
        from codex_ledger import cache_ceiling
        normal = UsageRecord(
            datetime(2026, 11, 18, 16, 12, 0, tzinfo=timezone.utc),
            "m", "t", "r0", Usage(1000, 0, 0), 0,
        )
        compacted = UsageRecord(
            datetime(2026, 11, 18, 16, 12, 1, tzinfo=timezone.utc),
            "m", "t", "r1", Usage(600, 500, 0), 1,
            after_compaction=True,
        )
        after = UsageRecord(
            datetime(2026, 11, 18, 16, 12, 2, tzinfo=timezone.utc),
            "m", "t", "r2", Usage(900, 600, 0), 2,
        )
        ceiling = cache_ceiling([normal, compacted, after])
        # 压缩后的请求本身不计入统计，只作为下一条请求的新基线。
        self.assertEqual(ceiling.requests, 2)
        self.assertEqual(ceiling.eligible, 2)
        # 只有 normal 和 after 计入实际命中（0 + 600）；压缩后那条 500 被跳过。
        self.assertEqual(ceiling.actual_cached, 600)
        # after 的上限取自 compacted 的输入（600），命中 600，缺口为 0。
        self.assertEqual(ceiling.gap_prompt_only, 0)
        # normal 是首次请求，上限取自身实际（0）；after 与 compacted 命中持平。
        self.assertEqual(ceiling.extra_eviction, 0)

    def test_extra_eviction_only_counts_hits_below_previous(self):
        from codex_ledger import cache_ceiling
        records = [
            self.record(0, 1000, 900, 0),   # 首次，不参与失效判定
            self.record(1, 2000, 950, 0),   # 高于上次 900 → 不算失效
            self.record(2, 3000, 700, 0),   # 低于上次 950 → 失效 250
            self.record(3, 4000, 700, 0),   # 与上次持平 → 不算失效
            self.record(4, 5000, 400, 0),   # 低于上次 700 → 失效 300
        ]
        ceiling = cache_ceiling(records)
        self.assertEqual(ceiling.extra_eviction, 550)
        self.assertEqual(ceiling.eviction_events, 2)

    def test_first_request_ceiling_equals_its_actual(self):
        from codex_ledger import cache_ceiling
        # 首次请求没有“上一次”，不预估上限，直接以实际命中作为上限。
        ceiling = cache_ceiling([self.record(0, 1000, 640, 100)])
        self.assertEqual(ceiling.requests, 1)
        self.assertEqual(ceiling.ideal_prompt_only, 640)
        self.assertEqual(ceiling.ideal_with_output, 640)
        self.assertEqual(ceiling.gap_prompt_only, 0)
        # 上限取实际值本身，缺口为 0。
        self.assertEqual(ceiling.gap_with_output, 0)
        self.assertEqual(ceiling.exceeded, 0)

    def test_all_requests_share_one_denominator(self):
        from codex_ledger import cache_ceiling
        records = [
            self.record(0, 1000, 400, 0),
            self.record(1, 2000, 900, 0),
            self.record(2, 3000, 1800, 0),
        ]
        ceiling = cache_ceiling(records)
        # 首次请求也计入可比口径，分子分母覆盖全部 3 次请求。
        self.assertEqual(ceiling.requests, 3)
        self.assertEqual(ceiling.eligible, 3)
        self.assertEqual(ceiling.compared_input_tokens, ceiling.input_tokens)
        self.assertEqual(ceiling.compared_cached, ceiling.actual_cached)
        # 实际 + 缺口 == 上限，这是提示里数字能相加核对的前提。
        self.assertEqual(ceiling.actual_cached + ceiling.gap_prompt_only, ceiling.ideal_prompt_only)
        self.assertEqual(ceiling.actual_cached + ceiling.gap_with_output, ceiling.ideal_with_output)

    def test_compared_rate_excludes_first_request(self):
        from codex_ledger import cache_ceiling
        records = [
            self.record(0, 1000, 400, 0),
            self.record(1, 2000, 900, 0),
            self.record(2, 3000, 1800, 0),
        ]
        ceiling = cache_ceiling(records)
        self.assertEqual(ceiling.input_tokens, 6000)
        self.assertEqual(ceiling.actual_cached, 3100)
        # 首次请求的上限就是它的实际值 400，后两次分别是 1000 和 2000。
        self.assertEqual(ceiling.ideal_prompt_only, 400 + 1000 + 2000)
        self.assertEqual(ceiling.gap_prompt_only, 0 + 100 + 200)
        self.assertEqual(ceiling.exceeded, 0)

    def test_rates_never_exceed_one(self):
        from codex_ledger import cache_ceiling
        ceiling = cache_ceiling([self.record(0, 100, 0), self.record(1, 200, 150)])
        self.assertLessEqual(ceiling.actual_rate, 1.0)
        self.assertLessEqual(ceiling.prompt_only_rate, 1.0)
        self.assertLessEqual(ceiling.with_output_rate, 1.0)


class CostBreakdownTest(unittest.TestCase):
    def test_parts_sum_to_total_and_ratios_sum_to_one(self):
        from codex_ledger import CostParts, aggregate_cost_breakdown
        record = UsageRecord(
            datetime(2026, 11, 18, 16, 12, tzinfo=timezone.utc),
            "m", "t", "r1", Usage(1000, 800, 500, 0), 1,
        )
        config = PriceConfig({"m": ModelPrice(Decimal(2), Decimal("0.1"), Decimal(10))})
        parts, unpriced = aggregate_cost_breakdown([record], config)
        # 非缓存 200×2 + 缓存 800×0.1 + 输出 500×10，全部除以 1e6。
        self.assertEqual(parts.uncached_input, Decimal("0.0004"))
        self.assertEqual(parts.cached_input, Decimal("0.00008"))
        self.assertEqual(parts.output, Decimal("0.005"))
        self.assertEqual(parts.total, Decimal("0.00548"))
        self.assertAlmostEqual(sum(ratio for _, _, ratio in parts.items()), 1.0)
        self.assertEqual(unpriced, 0)

    def test_unknown_model_is_counted_as_unpriced(self):
        from codex_ledger import aggregate_cost_breakdown
        record = UsageRecord(
            datetime(2026, 11, 18, 16, 12, tzinfo=timezone.utc),
            "unknown", "t", "r1", Usage(10, 0, 5), 1,
        )
        parts, unpriced = aggregate_cost_breakdown([record], PriceConfig({}))
        self.assertEqual(parts.total, Decimal(0))
        self.assertEqual(unpriced, 15)

    def test_breakdown_matches_plain_cost(self):
        from codex_ledger import aggregate_cost_breakdown
        records = [
            UsageRecord(
                datetime(2026, 11, 18, 16, 12, tzinfo=timezone.utc),
                "m", "t", "r1", Usage(1000, 800, 500), 1,
            ),
            UsageRecord(
                datetime(2026, 11, 18, 16, 13, tzinfo=timezone.utc),
                "m", "t", "r2", Usage(2000, 1900, 80), 2,
            ),
        ]
        config = PriceConfig({"m": ModelPrice(Decimal(2), Decimal("0.1"), Decimal(10))})
        parts, _ = aggregate_cost_breakdown(records, config)
        self.assertEqual(parts.total, aggregate_cost(records, config)[0])


class QtTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyleSheet(STYLE)

    def make_window(self) -> LedgerWindow:
        config = PriceConfig({"gpt-test": ModelPrice(Decimal(2), Decimal("0.1"), Decimal(10))})
        window = LedgerWindow(Path.cwd(), Path("unused-prices.json"), config)
        window.start_scan = lambda: None
        return window

    def sample_session(self):
        path = Path("sample.jsonl")
        record = UsageRecord(
            datetime(2026, 11, 18, 16, 12, tzinfo=timezone.utc),
            "gpt-test", "turn-a", "r1", Usage(22767, 14720, 707), 2,
        )
        from codex_ledger import Session, Turn
        turn = Turn(
            turn_id="turn-a",
            records=[record],
            started_at=record.timestamp,
            completed_at=record.timestamp,
            user_prompt="测试提问",
            outcome="completed",
        )
        return Session(path, "session-a", "D:/work/demo", "Codex", [record], [], [turn])

    def test_filter_summary_and_all_pages(self):
        window = self.make_window()
        window.scan_finished(type("Result", (), {"sessions": [self.sample_session()], "parse_errors": []})())
        window.show()
        self.app.processEvents()
        self.assertEqual(window.session_table.rowCount(), 1)
        self.assertEqual(window.model_table.rowCount(), 1)
        self.assertEqual(window.day_table.rowCount(), 1)
        self.assertEqual(window.total_card.value.text(), "$0.0246")
        session_row = window.session_table.rowCount() - 1
        headers = [window.session_table.horizontalHeaderItem(c).text() for c in range(window.session_table.columnCount())]
        rate_column = headers.index("缓存命中率")
        self.assertEqual(window.session_table.item(session_row, rate_column).text(), "64.7%")
        self.assertEqual(window.session_table.item(session_row, headers.index("任务轮次")).text(), "1")
        self.assertEqual(window.cache_card.value.text(), "64.7%")
        for page in range(4):
            window.change_page(page)
            self.app.processEvents()
        window.search.setText("not-found")
        window.search_timer.stop()
        window.apply_filters()
        self.assertEqual(window.filtered, [])
        window.close()

    def test_small_window_keeps_layout_readable(self):
        window = self.make_window()
        window.scan_finished(type("Result", (), {"sessions": [self.sample_session()], "parse_errors": []})())
        window.show()
        window.resize(1380, 620)
        self.app.processEvents()
        # 窗口压矮时内容改为滚动，而不是继续压缩卡片造成标签重叠。
        self.assertGreater(window.stack.minimumHeight(), 0)
        self.assertGreaterEqual(window.stack.height(), window.stack.minimumHeight())
        # 侧栏导航只有四行，任何情况下都不该出现滚动条。
        self.assertFalse(window.nav.verticalScrollBar().isVisible())
        window.close()

    def test_switching_to_table_pages_resets_outer_scroll(self):
        window = self.make_window()
        window.scan_finished(type("Result", (), {"sessions": [self.sample_session()], "parse_errors": []})())
        window.show()
        levels = []
        for page in range(4):
            window.change_page(page)
            self.app.processEvents()
            levels.append(window.content_scroll.verticalScrollBar().value())
        # 切到表格页时应把外层滚动位置归零，避免表格被顶出视野。
        self.assertEqual(levels[1:], [0, 0, 0])
        window.close()

    def test_custom_date_range_does_not_crash(self):
        from PyQt6.QtCore import QDate
        window = self.make_window()
        window.scan_finished(type("Result", (), {"sessions": [self.sample_session()], "parse_errors": []})())
        window.show()
        self.app.processEvents()
        # QDate 没有 toPython()，用错方法会直接让整个界面崩溃。
        window.period.setCurrentText("自定义范围")
        self.app.processEvents()
        window.start_date.setDate(QDate(2026, 11, 17))
        window.end_date.setDate(QDate(2026, 11, 18))
        self.app.processEvents()
        start, end = window.date_range()
        self.assertEqual(str(start), "2026-11-17")
        self.assertEqual(str(end), "2026-11-18")
        window.close()

    def test_reversed_custom_range_is_normalized(self):
        from PyQt6.QtCore import QDate
        window = self.make_window()
        window.show()
        window.period.setCurrentText("自定义范围")
        window.start_date.setDate(QDate(2026, 11, 20))
        window.end_date.setDate(QDate(2026, 11, 10))
        self.app.processEvents()
        start, end = window.date_range()
        self.assertLessEqual(start, end)
        window.close()

    def test_double_click_target_stores_turn(self):
        from codex_ledger import load_config as _load
        window = self.make_window()
        window.show()
        from main import SessionDialog
        config = _load(Path("prices.json"))
        dialog = SessionDialog(self.sample_session(), config, window)
        dialog.show()
        self.app.processEvents()
        from PyQt6.QtWidgets import QTabWidget
        table = dialog.findChild(QTabWidget).widget(0)
        stored = table.item(0, 0).data(256 + 2)
        from codex_ledger import Turn
        self.assertIsInstance(stored, Turn)
        dialog.close()
        window.close()

    def test_date_and_model_filters_apply_per_request(self):
        session = self.sample_session()
        filtered = filtered_sessions([session], start=datetime.now().astimezone().date().replace(year=datetime.now().year + 1))
        self.assertEqual(filtered, [])
        self.assertEqual(len(filtered_sessions([session], model="gpt-test")), 1)
        self.assertEqual(filtered_sessions([session], model="other"), [])

    def test_export_current_filter_to_csv(self):
        window = self.make_window()
        window.scan_finished(type("Result", (), {"sessions": [self.sample_session()], "parse_errors": []})())
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "usage.csv"
            original = QFileDialog.getSaveFileName
            original_info = QMessageBox.information
            QFileDialog.getSaveFileName = staticmethod(lambda *args, **kwargs: (str(output), "CSV"))
            QMessageBox.information = lambda *args, **kwargs: QMessageBox.StandardButton.Ok
            try:
                window.export_csv()
            finally:
                QFileDialog.getSaveFileName = original
                QMessageBox.information = original_info
            with output.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["会话ID"], "session-a")
            self.assertEqual(Decimal(rows[0]["已定价费用"]), Decimal("0.024636"))
            self.assertEqual(rows[0]["输出tokens"], "707")
            self.assertEqual(rows[0]["缓存输入tokens"], "14720")
            self.assertEqual(rows[0]["缓存命中率"], "64.7%")
        window.close()

    def test_price_dialog_saves_and_preserves_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.json"
            original = PriceConfig(
                {"gpt-test": ModelPrice(Decimal(1), Decimal(2), Decimal(3), "保留备注")}
            )
            save_config(original, path)
            dialog = PriceDialog(original, path)
            dialog.rows[0]["output"].setText("12.5")
            dialog.add_row("gpt-*", ModelPrice(Decimal(4), Decimal(5), Decimal(6)))
            dialog.save()
            saved = load_config(path)
            self.assertEqual(saved.models["gpt-test"].output, Decimal("12.5"))
            self.assertEqual(saved.models["gpt-test"].note, "保留备注")
            self.assertIn("gpt-*", saved.models)
            dialog.close()


if __name__ == "__main__":
    unittest.main()

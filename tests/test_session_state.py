"""阶段 2 MVP：会话状态（约束 / 最近指令 / 抽取式摘要）。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from unittest import mock

from app.core.config import get_settings
from app.memory import context, session_state
from tests.memory_fixtures import memory_store


def _cfg(**overrides):
    base = replace(
        get_settings(),
        memory_enabled=True,
        memory_context_enabled=True,
        memory_context_budget=900,
        memory_recent_history_messages=8,
        memory_recent_history_tokens=1200,
    )
    return replace(base, **overrides)


class SessionStateTests(unittest.TestCase):
    def test_constraints_are_extracted_and_new_value_supersedes_old(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            first = store.add_user_message(
                session["id"], "字数控制在1500字，语气正式。"
            )
            session_state.refresh(
                store, session["id"], first["content"], first["seq"]
            )
            second = store.add_user_message(
                session["id"], "改为2000字，格式分三段。"
            )
            state = session_state.refresh(
                store, session["id"], second["content"], second["seq"]
            )

            active = [
                item for item in state["constraints"]
                if item["status"] == "active"
            ]
            values = [item["value"] for item in active]
            self.assertTrue(any("2000" in value for value in values))
            self.assertFalse(any("1500" in value for value in values))
            self.assertTrue(any("三段" in value for value in values))

            prompt = session_state.build_session_prompt(state)
            self.assertIn("2000", prompt)
            # 旧指令可以留在“最近指令”里用于理解改稿过程，但不能出现在
            # “当前有效要求”里。
            active_section = prompt.split("### 最近指令")[0]
            self.assertNotIn("1500", active_section)
            self.assertIn("冲突时以当前有效要求", prompt)

    def test_extractive_summary_keeps_dates_and_is_not_fact_source(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            for index in range(7):
                store.add_user_message(
                    session["id"],
                    "第%d次记录：申请人于2026年3月%d日提交补正资料。"
                    % (index + 1, index + 1),
                )
                store.add_assistant_message(
                    session["id"],
                    content="已记录第%d次补正资料。[1]" % (index + 1),
                    status="complete",
                )
            current = store.add_user_message(session["id"], "当前问题")
            state = session_state.refresh(
                store, session["id"], current["content"], current["seq"]
            )
            summary = state.get("summary") or {}
            self.assertTrue(summary.get("text"))
            self.assertIn("不是事实", summary["text"])
            self.assertIn("2026年3月", summary["text"])
            self.assertNotIn("[1]", summary["text"])
            prompt = session_state.build_session_prompt(state)
            self.assertIn("会话摘要", prompt)

    def test_user_facts_are_retained_in_prompt(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            for index, text in enumerate(
                (
                    "申请日期是2026年3月10日。",
                    "补正通知日期是2026年3月20日。",
                    "补正资料提交日期是2026年4月2日。",
                ),
                start=1,
            ):
                message = store.add_user_message(session["id"], text)
                session_state.refresh(
                    store, session["id"], message["content"], message["seq"]
                )
            state = session_state.load_state(store, session["id"])
            self.assertEqual(len(state.get("user_facts") or []), 3)
            prompt = session_state.build_session_prompt(state)
            self.assertIn("用户在本会话提供的事实", prompt)
            self.assertIn("2026年3月10日", prompt)
            self.assertIn("2026年4月2日", prompt)
            self.assertIn("不得替用户下法律结论", prompt)

    def test_state_is_stored_in_session_meta(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            message = store.add_user_message(
                session["id"], "对象：某单位；不得新增事实。"
            )
            session_state.refresh(
                store, session["id"], message["content"], message["seq"]
            )
            row = store.get_session(session["id"])
            self.assertIn("session_state", row["meta"])
            self.assertEqual(
                row["meta"]["session_state"]["version"], 1
            )

    def test_context_includes_session_prompt_within_history_budget(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            first = store.add_user_message(
                session["id"], "字数控制在1200字，语气正式。"
            )
            session_state.refresh(
                store, session["id"], first["content"], first["seq"]
            )
            store.add_assistant_message(
                session["id"], content="好的，按这个要求。", status="complete"
            )
            current = store.add_user_message(session["id"], "开始写第一段。")
            ctx = context.build_chat_context(
                store,
                session["id"],
                current["content"],
                exclude_message_id=current["id"],
                settings=_cfg(),
            )
            self.assertIsNotNone(ctx)
            self.assertIn("会话状态", ctx.session_prompt)
            self.assertIn("1200", ctx.session_prompt)
            self.assertLessEqual(ctx.trace["history_tokens"], 1200)
            self.assertGreater(ctx.trace["session_prompt_tokens"], 0)
            self.assertGreaterEqual(ctx.trace["session_constraints"], 1)

    def test_state_read_failure_can_be_strict(self) -> None:
        with memory_store() as (store, _):
            session = store.create_session()
            with mock.patch.object(
                store, "get_session", side_effect=RuntimeError("db read failed")
            ):
                with self.assertRaises(RuntimeError):
                    session_state.load_state(store, session["id"], strict=True)


if __name__ == "__main__":
    unittest.main()

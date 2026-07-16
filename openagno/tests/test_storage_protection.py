"""存储保护集成测试：走真实 Agno 会话持久化路径（SqliteDb）。

断言 BLOCK_REQUEST / END_CONVERSATION 命中后原始输入不出现在 ``agno_sessions``；
脱敏路径只持久化脱敏后文本。不依赖 Agno 异常传播的假设，直接读库验证。
"""
from __future__ import annotations

import sqlite3

import pytest

agno = pytest.importorskip("agno", reason="需要安装 agno 依赖")

from agno.agent import Agent  # noqa: E402
from agno.db.sqlite import SqliteDb  # noqa: E402
from agno.models.base import Model  # noqa: E402
from agno.models.response import ModelResponse  # noqa: E402

from agno_worker.moderation.config import ModerationConfig  # noqa: E402
from agno_worker.moderation.context import (  # noqa: E402
    new_request_id,
    reset_request_context,
    set_request_context,
)
from agno_worker.moderation.guardrail import (  # noqa: E402
    BLOCKED_INPUT_PLACEHOLDER,
    SensitiveContentGuardrail,
)
from agno_worker.moderation.models import ActionType  # noqa: E402
from agno_worker.moderation.snapshot import SnapshotClient  # noqa: E402

from conftest import CaptureReporter, make_rule, make_snapshot, make_type  # noqa: E402


TENANT = "tenant-a"
ORIGINAL_INPUT = "这句话包含敏感词需要处理"


class FakeModel(Model):
    """不访问网络的假模型，用于驱动真实的 Agent 持久化路径。"""

    def __init__(self) -> None:
        super().__init__(id="fake-model", name="FakeModel", provider="test")

    def invoke(self, *args, **kwargs):
        return ModelResponse(content="模型回复")

    async def ainvoke(self, *args, **kwargs):
        return ModelResponse(content="模型回复")

    def invoke_stream(self, *args, **kwargs):
        yield ModelResponse(content="模型回复")

    async def ainvoke_stream(self, *args, **kwargs):
        yield ModelResponse(content="模型回复")

    def _parse_provider_response(self, response, **kwargs):
        return response

    def _parse_provider_response_delta(self, response):
        return response


def build_agent(tmp_path, action: ActionType, action_config=None):
    cfg = ModerationConfig(
        service_url="http://sensitive-content.test",
        fingerprint_key="fp-key",
        cache_path=str(tmp_path / "cache.json"),
    )
    client = SnapshotClient(cfg)
    client._install_snapshot(
        make_snapshot(
            [make_type(1, action, action_config=action_config)],
            [make_rule(1, 1, "敏感词")],
            tenant_id=TENANT,
        ),
        persist=False,
    )
    guardrail = SensitiveContentGuardrail(
        cfg, snapshot_client=client, reporter=CaptureReporter()
    )
    db_file = str(tmp_path / "sessions.db")
    agent = Agent(
        model=FakeModel(),
        db=SqliteDb(db_file=db_file),
        pre_hooks=[guardrail],
        add_history_to_context=True,
    )
    return agent, db_file


def run_with_context(agent, message: str):
    ctx, token = set_request_context(
        tenant_id=TENANT,
        user_id="user-1",
        session_id="session-1",
        request_id=new_request_id(),
    )
    try:
        return agent.run(message, session_id="session-1", user_id="user-1")
    finally:
        reset_request_context(token)


def dump_db(db_file: str) -> str:
    """把 sqlite 中所有表的所有行拼成一个字符串，用于明文断言。"""
    con = sqlite3.connect(db_file)
    try:
        tables = [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
        blob = ""
        for table in tables:
            for row in con.execute(f"SELECT * FROM {table}"):  # noqa: S608
                blob += repr(row)
        return blob
    finally:
        con.close()


def contains_text(blob: str, text: str) -> bool:
    """明文断言：中文在库内会被 JSON 转义为 \\uXXXX（层数不定）。

    把两侧的反斜杠全部剥掉后比较，兼容任意层转义。
    """
    import json

    normalized_blob = blob.replace("\\", "")
    candidates = [
        text,
        json.dumps(text, ensure_ascii=True)[1:-1].replace("\\", ""),
    ]
    return any(c in blob or c in normalized_blob for c in candidates)


class TestStorageProtection:
    def test_block_request_original_input_not_persisted(self, tmp_path):
        agent, db_file = build_agent(tmp_path, ActionType.BLOCK_REQUEST)
        output = run_with_context(agent, ORIGINAL_INPUT)
        # agno 捕获 InputCheckError 后返回错误 RunOutput
        assert str(output.status).lower().endswith("error")
        blob = dump_db(db_file)
        assert blob, "会话应已持久化"
        # 原始输入（含敏感词）不出现在 agno_sessions
        assert not contains_text(blob, ORIGINAL_INPUT)
        assert not contains_text(blob, "敏感词")
        assert contains_text(blob, BLOCKED_INPUT_PLACEHOLDER)

    def test_end_conversation_original_input_not_persisted(self, tmp_path):
        agent, db_file = build_agent(
            tmp_path, ActionType.END_CONVERSATION, {"reply": "会话结束"}
        )
        run_with_context(agent, ORIGINAL_INPUT)
        blob = dump_db(db_file)
        assert blob
        assert not contains_text(blob, ORIGINAL_INPUT)
        assert not contains_text(blob, "敏感词")

    def test_redact_path_persists_only_redacted_text(self, tmp_path):
        agent, db_file = build_agent(tmp_path, ActionType.REDACT_AND_CONTINUE)
        output = run_with_context(agent, ORIGINAL_INPUT)
        # 正常完成：模型收到脱敏文本并回复
        assert output.content == "模型回复"
        blob = dump_db(db_file)
        assert blob
        # 只持久化脱敏后文本
        assert not contains_text(blob, "敏感词")
        assert contains_text(blob, "这句话包含***需要处理")

    def test_log_only_persists_original(self, tmp_path):
        """LOG_ONLY 放行路径不改写输入（对照组）。"""
        agent, db_file = build_agent(tmp_path, ActionType.LOG_ONLY)
        output = run_with_context(agent, ORIGINAL_INPUT)
        assert output.content == "模型回复"
        assert contains_text(dump_db(db_file), ORIGINAL_INPUT)

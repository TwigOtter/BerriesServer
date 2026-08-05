"""
tests/test_llm_client.py

Unit tests for the backend-agnostic message normalization helpers in
shared/llm_client.py: folding developer-role blocks into the next user
message (for backends without a "developer" role) and merging consecutive
same-role messages (required for Anthropic's strict alternation, applied
for every backend so shape doesn't depend on LLM_BACKEND).
"""

import json

import pytest

import shared.llm_client as llm_client
from shared.llm_client import fold_developer_blocks, merge_consecutive_messages


class TestFoldDeveloperBlocks:
    def test_folds_into_next_user_message(self):
        messages = [
            {"role": "developer", "content": "CONTEXT"},
            {"role": "user", "content": "hello"},
        ]
        assert fold_developer_blocks(messages) == [
            {"role": "user", "content": "CONTEXT\n\nhello"},
        ]

    def test_multiple_developer_blocks_in_a_row_fold_together(self):
        messages = [
            {"role": "developer", "content": "FIRST"},
            {"role": "developer", "content": "SECOND"},
            {"role": "user", "content": "hello"},
        ]
        assert fold_developer_blocks(messages) == [
            {"role": "user", "content": "FIRST\n\nSECOND\n\nhello"},
        ]

    def test_developer_block_mid_conversation_folds_into_next_user_turn(self):
        messages = [
            {"role": "user", "content": "[Twig]: hi"},
            {"role": "assistant", "content": "hey"},
            {"role": "developer", "content": "PROFILE"},
            {"role": "user", "content": "[Twig]: how are you"},
        ]
        assert fold_developer_blocks(messages) == [
            {"role": "user", "content": "[Twig]: hi"},
            {"role": "assistant", "content": "hey"},
            {"role": "user", "content": "PROFILE\n\n[Twig]: how are you"},
        ]

    def test_no_developer_blocks_passes_through_unchanged(self):
        messages = [{"role": "user", "content": "hello"}]
        assert fold_developer_blocks(messages) == messages

    def test_trailing_developer_block_merges_into_last_user_message(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "developer", "content": "TRAILING"},
        ]
        assert fold_developer_blocks(messages) == [
            {"role": "user", "content": "hello\n\nTRAILING"},
        ]

    def test_trailing_developer_block_with_no_user_message_becomes_one(self):
        messages = [{"role": "developer", "content": "ONLY CONTEXT"}]
        assert fold_developer_blocks(messages) == [
            {"role": "user", "content": "ONLY CONTEXT"},
        ]


class TestMergeConsecutiveMessages:
    def test_alternating_messages_pass_through_unchanged(self):
        messages = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        assert merge_consecutive_messages(messages) == messages

    def test_consecutive_same_role_merged_with_newline(self):
        messages = [
            {"role": "user", "content": "[A]: one"},
            {"role": "user", "content": "[B]: two"},
            {"role": "assistant", "content": "reply"},
        ]
        assert merge_consecutive_messages(messages) == [
            {"role": "user", "content": "[A]: one\n[B]: two"},
            {"role": "assistant", "content": "reply"},
        ]

    def test_three_in_a_row_collapse_to_one(self):
        messages = [
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "user", "content": "three"},
        ]
        assert merge_consecutive_messages(messages) == [
            {"role": "user", "content": "one\ntwo\nthree"},
        ]

    def test_empty_list(self):
        assert merge_consecutive_messages([]) == []


class _FakeResponse:
    def __init__(self, captured):
        self._captured = captured

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "choices": [{"message": {"content": "boo"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }


class _FakeAsyncClient:
    """Captures the JSON payload posted to the vLLM server."""

    captured: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, timeout=None):
        _FakeAsyncClient.captured = json
        return _FakeResponse(json)


class TestVllmSamplingParams:
    @pytest.mark.asyncio
    async def test_payload_carries_configured_sampling_params(self, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
        monkeypatch.setattr(llm_client, "VLLM_TEMPERATURE", 0.5)
        monkeypatch.setattr(llm_client, "VLLM_TOP_P", 0.85)
        monkeypatch.setattr(llm_client, "VLLM_TOP_K", 25)
        monkeypatch.setattr(llm_client, "VLLM_REPETITION_PENALTY", 1.2)

        text, usage = await llm_client._vllm_completion(
            "system", [{"role": "user", "content": "hi"}], max_tokens=10
        )

        payload = _FakeAsyncClient.captured
        assert payload["temperature"] == 0.5
        assert payload["top_p"] == 0.85
        assert payload["top_k"] == 25
        assert payload["repetition_penalty"] == 1.2
        assert text == "boo"

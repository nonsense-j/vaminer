"""Tests for Claude protocol initialization evidence."""

import json
import logging

import pytest

from src.miner.runtimes.claude.config import ClaudeCodeConfig
from src.miner.runtimes.claude.errors import ClaudeCodeConfigurationError
from src.miner.runtimes.claude.protocol import (
    ProtocolDecoder,
    RequestCounter,
    StreamMonitor,
    subagent_events,
)
from src.miner.utils.log import logger


def test_mcp_failure_requires_absence_of_later_success():
    failed_init = {
        "type": "system",
        "subtype": "init",
        "plugin_errors": [],
        "mcp_servers": [{"name": "vaminer", "status": "failed"}],
    }
    successful_call = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tool-1",
                    "name": "mcp__vaminer__fetch_cve",
                    "input": {},
                }
            ]
        },
    }
    successful_result = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "{}",
                }
            ]
        },
    }
    decoder = ProtocolDecoder(ClaudeCodeConfig())

    with pytest.raises(
        ClaudeCodeConfigurationError,
        match="failed to connect MCP",
    ):
        decoder.validate_initialization((failed_init,))
    decoder.validate_initialization(
        (failed_init, successful_call, successful_result)
    )


def test_stream_monitor_logs_parent_and_forwarded_subagent_actors(
    caplog: pytest.LogCaptureFixture,
):
    observed = []
    monitor = StreamMonitor(
        task_id="probe-task",
        attempt=1,
        counter=RequestCounter(limit=None),
        event_handler=observed.append,
    )
    parent = {
        "type": "assistant",
        "message": {
            "id": "parent-request",
            "content": [
                {
                    "type": "tool_use",
                    "id": "agent-1",
                    "name": "Agent",
                    "input": {"subagent_type": "vaminer:rule-generator"},
                }
            ],
        },
    }
    child = {
        "type": "assistant",
        "parent_tool_use_id": "agent-1",
        "message": {
            "id": "child-request",
            "content": [
                {
                    "type": "tool_use",
                    "id": "child-tool-1",
                    "name": "Read",
                    "input": {"file_path": "probe.py"},
                }
            ],
        },
    }
    child_result = {
        "type": "user",
        "parent_tool_use_id": "agent-1",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "child-tool-1",
                    "content": "source",
                }
            ]
        },
    }
    subagent_result = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "agent-1",
                    "content": "complete",
                }
            ]
        },
    }

    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            monitor.observe_line(json.dumps(parent))
            monitor.observe_line(json.dumps(child))
            monitor.observe_line(json.dumps(child_result))
            monitor.observe_line(json.dumps(subagent_result))
    finally:
        logger.removeHandler(caplog.handler)

    assert observed == [parent, child, child_result, subagent_result]
    assert monitor.subagent_types == {"agent-1": "vaminer:rule-generator"}
    assert (
        "Claude subagent spawned: task=probe-task attempt=1 "
        "subagent=vaminer:rule-generator parent_tool_use_id=agent-1"
    ) in caplog.text
    assert (
        "tool=Read actor=subagent:vaminer:rule-generator "
        "parent_tool_use_id=agent-1"
    ) in caplog.text
    assert (
        "tool_use_id=agent-1 error=False "
        "actor=subagent:vaminer:rule-generator parent_tool_use_id=agent-1"
    ) in caplog.text
    assert monitor.counter.count == 2


def test_subagent_audit_records_parentage():
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "agent-1",
                        "name": "Agent",
                        "input": {"subagent_type": "vaminer:rule-generator"},
                    }
                ]
            },
        },
        {
            "type": "assistant",
            "parent_tool_use_id": "agent-1",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "agent-2",
                        "name": "Agent",
                        "input": {"subagent_type": "vaminer:ast-grep"},
                    }
                ]
            },
        },
    ]

    assert subagent_events(events) == [
        {
            "event": "spawn",
            "id": "agent-1",
            "actor": "parent",
            "parent_tool_use_id": None,
            "agent_type": "vaminer:rule-generator",
        },
        {
            "event": "spawn",
            "id": "agent-2",
            "actor": "subagent",
            "parent_tool_use_id": "agent-1",
            "agent_type": "vaminer:ast-grep",
        },
    ]

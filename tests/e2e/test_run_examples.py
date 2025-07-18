"""
Run tests from examples rulebook
"""

import asyncio
import logging
from functools import partial

import dpath
import pytest
import websockets.asyncio.server as ws_server

from . import utils

LOGGER = logging.getLogger(__name__)
DEFAULT_TIMEOUT = 15

TEST_DATA = [
    (
        {
            "rules_triggered": 2,
            "events_processed": 4,
            "events_matched": 2,
            "number_of_actions": 2,
            "number_of_session_stats": 2,
            "number_of_rules": 2,
            "number_of_disabled_rules": 0,
        },
        utils.EXAMPLES_PATH / "29_run_module.yml",
        {
            "r1": {
                "action": "run_module",
                "event_key": "m/i",
                "event_value": 1,
            },
            "r2": {
                "action": "print_event",
                "event_key": "m/msg",
                "event_value": "I am Malenia, blade of Miquella",
            },
        },
        "29 run module",
    ),
    (
        {
            "rules_triggered": 1,
            "events_processed": 2,
            "events_matched": 1,
            "number_of_actions": 1,
            "number_of_session_stats": 2,
            "number_of_rules": 1,
            "number_of_disabled_rules": 0,
        },
        utils.EXAMPLES_PATH / "93_event_splitter.yml",
        {
            "r1": {
                "action": "debug",
                "event_key": "m/city",
                "event_value": "Bedrock",
            }
        },
        "93 event splitter",
    ),
]


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "counts, rulebook, rule_matches, rule_set_name", TEST_DATA
)
async def test_run_example_rulebook(
    counts, rulebook, rule_matches, rule_set_name
):
    """
    Verify that we can run an example rulebook
    and compare the action and stats
    """
    # variables
    host = "127.0.0.1"
    endpoint = "/api/ws2"
    proc_id = "42"
    port = utils.get_safe_port()
    websocket_address = f"ws://127.0.0.1:{port}{endpoint}"
    cmd = utils.Command(
        rulebook=rulebook,
        websocket=websocket_address,
        proc_id=proc_id,
        heartbeat=2,
    )

    # run server and ansible-rulebook
    queue = asyncio.Queue()
    handler = partial(utils.msg_handler, queue=queue)
    async with ws_server.serve(handler, host, port):
        LOGGER.info(f"Running command: {cmd}")
        proc = await asyncio.create_subprocess_shell(
            str(cmd),
            cwd=utils.BASE_DATA_PATH,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        await asyncio.wait_for(proc.wait(), timeout=DEFAULT_TIMEOUT)
        assert proc.returncode == 0

    # Verify data
    assert not queue.empty()

    action_counter = 0
    session_stats_counter = 0
    stats = None
    while not queue.empty():
        data = await queue.get()
        assert data["path"] == endpoint
        data = data["payload"]

        if data["type"] == "Action":
            action_counter += 1
            assert data["action_uuid"] is not None
            assert data["ruleset_uuid"] is not None
            assert data["rule_uuid"] is not None
            assert data["status"] == "successful"
            rule_name = data["rule"]
            assert rule_name in rule_matches.keys()

            matching_events = data["matching_events"]
            assert (
                dpath.get(
                    matching_events, rule_matches[rule_name]["event_key"]
                )
                == rule_matches[rule_name]["event_value"]
            )
            assert data["action"] == rule_matches[rule_name]["action"]

        if data["type"] == "SessionStats":
            session_stats_counter += 1
            stats = data["stats"]
            assert stats["ruleSetName"] == rule_set_name
            assert stats["numberOfRules"] == counts["number_of_rules"]
            assert (
                stats["numberOfDisabledRules"]
                == counts["number_of_disabled_rules"]
            )
            assert data["activation_instance_id"] == proc_id

    assert stats["rulesTriggered"] == counts["rules_triggered"]
    assert stats["eventsProcessed"] == counts["events_processed"]
    assert stats["eventsMatched"] == counts["events_matched"]

    assert session_stats_counter >= counts["number_of_session_stats"]
    assert action_counter == counts["number_of_actions"]

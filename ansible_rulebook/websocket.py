#  Copyright 2022 Red Hat, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import asyncio
import base64
import json
import logging
import os
import random
import tempfile
import typing as tp
from dataclasses import dataclass, field

import websockets
import yaml
from websockets.client import WebSocketClientProtocol

from ansible_rulebook import rules_parser as rules_parser
from ansible_rulebook.common import StartupArgs
from ansible_rulebook.conf import settings
from ansible_rulebook.token import renew_token
from ansible_rulebook.util import create_context, validate_url
from ansible_rulebook.vault import Vault, has_vaulted_str

from .exception import InvalidUrlException

logger = logging.getLogger(__name__)
logging.getLogger("websockets").setLevel(logging.ERROR)


BACKOFF_MIN = 1.92
BACKOFF_MAX = 60.0
BACKOFF_FACTOR = 1.618
BACKOFF_INITIAL = 5


async def _wait_before_retry(backoff_delay: float) -> float:
    # Sleep and retry implemention duplicated from
    # websockets.lagacy.client.Client

    # Add a random initial delay between 0 and 5 seconds.
    # See 7.2.3. Recovering from Abnormal Closure in RFC 6544.
    if backoff_delay == BACKOFF_MIN:
        initial_delay = random.random() * BACKOFF_INITIAL
        logger.warning(
            "! websocket connect failed; reconnecting in %.1f seconds",
            initial_delay,
            exc_info=False,
        )
        await asyncio.sleep(initial_delay)
    else:
        logger.warning(
            "! websocket connect failed again; retrying in %d seconds",
            int(backoff_delay),
            exc_info=False,
        )
        await asyncio.sleep(int(backoff_delay))
    # Increase delay with truncated exponential backoff.
    backoff_delay = backoff_delay * BACKOFF_FACTOR
    backoff_delay = min(backoff_delay, BACKOFF_MAX)
    return backoff_delay


async def _update_authorization_header(headers: dict) -> None:
    new_token = await renew_token()
    headers["Authorization"] = f"Bearer {new_token}"


async def _connect_websocket(
    handler: tp.Callable[[WebSocketClientProtocol], tp.Awaitable],
    retry_on_close: bool,
    **kwargs: list,
) -> tp.Any:
    logger.info("websocket %s", settings.websocket_url)
    if not validate_url(settings.websocket_url, "websocket"):
        raise InvalidUrlException("Invalid websocket url.")

    if settings.websocket_access_token:
        extra_headers = {
            "Authorization": f"Bearer {settings.websocket_access_token}"
        }
    else:
        extra_headers = {}

    refresh_token = True

    backoff_delay = BACKOFF_MIN
    while True:
        try:
            logger.info("attempt websocket connection")
            async with websockets.connect(
                settings.websocket_url,
                ssl=_sslcontext(),
                additional_headers=extra_headers,
            ) as websocket:
                # Connection succeeded - reset backoff delay and refresh_token
                backoff_delay = BACKOFF_MIN
                refresh_token = True
                return await handler(websocket, **kwargs)
        except asyncio.CancelledError as e:  # pragma: no cover
            logger.warning(f"websocket aborted by CancelledError: {e}")
            raise
        except websockets.exceptions.InvalidStatusCode as e:
            if refresh_token and e.status_code == 403:
                await _update_authorization_header(extra_headers)
                # Only attempt to refresh token once. If a new token cannot
                # establish the connection, something else must have caused 403
                refresh_token = False
            else:
                logger.warning(f"websocket aborted by InvalidStatusCode: {e}")
                raise  # abort
        except websockets.exceptions.InvalidStatus as e:
            if refresh_token and e.response.status_code == 403:
                await _update_authorization_header(extra_headers)
                refresh_token = False
            else:
                logger.warning(f"websocket aborted by InvalidStatus: {e}")
                raise  # abort
        except OSError as e:
            if "[Errno 61]" in str(e):
                # if connection cannot be established, retry later
                backoff_delay = await _wait_before_retry(backoff_delay)
            else:
                logger.warning(f"websocket aborted by OSError: {e}")
                raise  # abort
        except websockets.exceptions.ConnectionClosedError as e:
            if retry_on_close and e.code != 1011:  # unexpected error
                backoff_delay = await _wait_before_retry(backoff_delay)
            else:
                logger.warning(
                    f"websocket closed by ConnectionClosedError: {e}"
                )
                raise
        except websockets.exceptions.ConnectionClosedOK as e:
            if retry_on_close:
                backoff_delay = await _wait_before_retry(backoff_delay)
            else:
                logger.warning(f"websocket closed by ConnectionClosedOK: {e}")
                raise
        except (
            websockets.exceptions.InvalidMessage,
            asyncio.exceptions.TimeoutError,
        ) as e:
            if retry_on_close:
                backoff_delay = await _wait_before_retry(backoff_delay)
            else:
                logger.warning(f"websocket aborted by {type(e)}: {e}")
                raise
        except Exception as e:
            logger.exception(f"websocket error {type(e)}: {e}")
            raise


async def request_workload(activation_instance_id: str) -> StartupArgs:
    return await _connect_websocket(
        handler=_handle_request_workload,
        retry_on_close=False,
        activation_instance_id=activation_instance_id,
    )


async def _handle_request_workload(
    websocket: WebSocketClientProtocol,
    activation_instance_id: str,
) -> StartupArgs:
    logger.info("workload websocket connected")
    await websocket.send(
        json.dumps(
            dict(
                type="Worker",
                activation_id=activation_instance_id,
                activation_instance_id=activation_instance_id,
            )
        )
    )

    project_data_fh = None
    response = StartupArgs()
    non_fq_key = False
    file_template_vars = {}
    while True:
        msg = await websocket.recv()
        data = json.loads(msg)
        if data.get("type") == "EndOfResponse":
            break
        if data.get("type") == "VaultCollection":
            settings.vault = Vault(passwords=data.get("data"))
        if data.get("type") == "ProjectData":
            if not project_data_fh:
                (
                    project_data_fh,
                    response.project_data_file,
                ) = tempfile.mkstemp()

            if data.get("data") and data.get("more"):
                os.write(project_data_fh, base64.b64decode(data.get("data")))
            if not data.get("data") and not data.get("more"):
                os.close(project_data_fh)
                logger.debug("wrote %s", response.project_data_file)
        if data.get("type") == "FileContents":
            template_key = data.get("template_key")
            raw_data = base64.b64decode(data.get("data"))
            keys = template_key.split(".")
            if len(keys) == 1 and template_key == "template":
                key = "filename"
                non_fq_key = True
            else:
                key = keys[1]
            filename = tempfile.NamedTemporaryFile().name
            with open(filename, "wb") as f:
                f.write(raw_data)
            file_template_vars[key] = filename
            os.chmod(filename, 0o400)
            logger.debug(f"File Content eda.filename.{key} : {filename}")
        if data.get("type") == "Rulebook":
            raw_data = base64.b64decode(data.get("data"))
            response.check_vault = has_vaulted_str(raw_data)
            response.rulesets = rules_parser.parse_rule_sets(
                yaml.safe_load(raw_data)
            )
        if data.get("type") == "ExtraVars":
            response.variables = yaml.safe_load(
                base64.b64decode(data.get("data"))
            )
        if data.get("type") == "EnvVars":
            response.env_vars = yaml.safe_load(
                base64.b64decode(data.get("data"))
            )
        if data.get("type") == "ControllerInfo":
            response.controller_url = data.get("url")
            response.controller_token = data.get("token")
            response.controller_ssl_verify = data.get("ssl_verify")
            response.controller_username = data.get("username", "")
            response.controller_password = data.get("password", "")

    if non_fq_key and "filename" in file_template_vars:
        response.variables["eda"] = {
            "filename": file_template_vars["filename"]
        }
    else:
        response.variables["eda"] = {"filename": file_template_vars}

    for key, value in response.env_vars.items():
        response.variables[key] = value
    return response


@dataclass
class EventLogQueue:
    queue: asyncio.Queue = field(default=None)
    event: dict = field(default=None)


async def send_event_log_to_websocket(event_log: asyncio.Queue):
    logs = EventLogQueue()
    logs.queue = event_log

    return await _connect_websocket(
        handler=_handle_send_event_log,
        retry_on_close=True,
        logs=logs,
    )


async def _handle_send_event_log(
    websocket: WebSocketClientProtocol,
    logs: EventLogQueue,
):
    logger.info("feedback websocket connected")

    if logs.event:
        logger.info("Resending last event...")
        json_str = json.dumps(logs.event)
        await websocket.send(json_str)
        logs.event = None

    while True:
        event = await logs.queue.get()
        logger.debug(f"Event received, {event}")

        if event == dict(type="Exit"):
            logger.info("Exiting feedback websocket task")
            break

        logs.event = event
        json_str = json.dumps(event)
        await websocket.send(json_str)
        logs.event = None


def _sslcontext():
    return create_context(settings.websocket_url, "wss")

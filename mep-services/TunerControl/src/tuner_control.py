# SPDX-FileCopyrightText: Copyright (c) 2026 Massachusetts Institute of Technology
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import dataclasses
import functools
import inspect
import logging
import operator
import os
import socket
import time
import traceback
from typing import Optional

import aiomqtt
import anyio
import exceptiongroup
import jsonargparse
import msgspec
from typing_extensions import Format, get_annotations

from mep_tuners import DummyTunerParams, LMX2820TunerParams, TunerBase, ValonTunerParams

logger = logging.getLogger("tunercontrol")
logger.setLevel(os.environ.get("TUNER_CONTROL_SERVICE_LOG_LEVEL", "INFO"))
logger.propagate = False
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.DEBUG)
logger.addHandler(_console_handler)


def deep_update(mapping: dict, *updating_mappings: dict) -> dict:
    """Update nested dictionary from another nested dictionary"""
    updated_mapping = mapping.copy()
    for updating_mapping in updating_mappings:
        for k, v in updating_mapping.items():
            if (
                k in updated_mapping
                and isinstance(updated_mapping[k], dict)
                and isinstance(v, dict)
            ):
                updated_mapping[k] = deep_update(updated_mapping[k], v)
            else:
                updated_mapping[k] = v
    return updated_mapping


# Holds tuner object parameters to make available through jsonargparse
# (types are ordered by preference)
@dataclasses.dataclass(kw_only=True)
class TunerConfig:
    valon: ValonTunerParams = dataclasses.field(
        default_factory=lambda: ValonTunerParams(
            name="valon",
            external_ref=True,
            external_ref_freq_mhz=10.0,
            pwr_dbm=10.0,
        )
    )
    lmx2820: LMX2820TunerParams = dataclasses.field(
        default_factory=lambda: LMX2820TunerParams(name="lmx2820")
    )
    dummy: DummyTunerParams = dataclasses.field(
        default_factory=lambda: DummyTunerParams(name="dummy")
    )


@dataclasses.dataclass(kw_only=True)
class TunerControlService:
    # service configuration variables
    announce_topic: str = "{service.name}/announce"
    command_topic: str = "{service.name}/command"
    cfg: TunerConfig = dataclasses.field(default_factory=lambda: TunerConfig())
    name: str = "tunercontrol"
    node_id: Optional[str] = None
    response_topic: str = "{service.name}/response"
    status_topic: str = "{service.name}/status"
    # service state variables
    tuner: Optional[TunerBase] = dataclasses.field(default=None, init=False)
    ready: bool = dataclasses.field(default=False, init=False)
    lock_supported: bool = dataclasses.field(default=False, init=False)
    lock_status: Optional[dict] = dataclasses.field(default=None, init=False)
    lock_error: Optional[str] = dataclasses.field(default=None, init=False)
    init_error: Optional[str] = dataclasses.field(default=None, init=False)

    def __post_init__(self):
        if self.node_id is None:
            self.node_id = os.getenv("NODE_ID", socket.gethostname())

        self.announce_topic = self.announce_topic.format(service=self)
        self.command_topic = self.command_topic.format(service=self)
        self.response_topic = self.response_topic.format(service=self)
        self.status_topic = self.status_topic.format(service=self)

        init_tuner(self)


def create_tuner(name, tuner_config):
    try:
        tuner = tuner_config.create_tuner()
    except Exception:
        logger.info(f"Failed to init tuner: {name}")
        logger.debug("Tuner exception:", exc_info=True)
        return None
    else:
        logger.info(f"Initialized tuner: {name}")
        return tuner


def init_tuner(service, force_tuner=None):
    """Iterate through known tuners and take the first ready one"""
    service.ready = False
    service.lock_supported = False
    service.lock_status = None
    service.lock_error = None
    service.init_error = None
    tuner = None
    if force_tuner is not None:
        logger.debug(f"Force initing tuner: {force_tuner}")
        try:
            tuner_config = getattr(service.cfg, force_tuner)
        except AttributeError:
            msg = f"No tuner config for {force_tuner} found"
            logger.info(msg)
            service.tuner = None
            service.init_error = msg
            return
        tuner = create_tuner(force_tuner, tuner_config)
    else:
        for name, tuner_config in service.cfg.__dict__.items():
            # skip dummy tuner for auto-init
            if name == "dummy":
                continue
            logger.debug(f"Trying to init tuner: {name}")
            tuner = create_tuner(name, tuner_config)
            if tuner is not None:
                break
    if tuner is None:
        msg = "No tuners found"
        logger.info(msg)
        service.tuner = None
        service.init_error = msg
        return
    service.tuner = tuner
    service.ready = True
    lock_query = getattr(tuner, "get_lock_status", None)
    service.lock_supported = callable(lock_query)
    if not service.lock_supported:
        return
    try:
        lock_status = lock_query()
        service.lock_status = lock_status
        locks = [value for value in lock_status.values() if isinstance(value, bool)]
        if not locks:
            service.lock_error = "PLL lock response contained no lock values"
    except Exception as exc:
        service.lock_error = f"PLL lock query failed: {exc}"
        logger.warning(service.lock_error)


async def send_announce(client, service):
    # inspect known Tuner classes to get valid tuner commands
    tuner_commands = collections.defaultdict(dict)
    for name, tuner_config in service.cfg.__dict__.items():
        for method_name, fun in inspect.getmembers(
            tuner_config.tuner_class,
            lambda v: inspect.isfunction(v) and not v.__name__.startswith("_"),
        ):
            tuner_commands[name][method_name] = {
                "task_name": method_name,
                "arguments": get_annotations(fun, format=Format.STRING),
                "doc": fun.__doc__,
            }
    payload = {
        "title": "Tuner control",
        "description": "Control and monitor MEP tuner devices",
        "author": "Ryan Volz <rvolz@mit.edu>",
        "output": {
            "status": {
                "type": "mqtt",
                "value": f"{service.status_topic}",
            },
        },
        "version": "0.1",
        "type": "service",
        "time_started": time.time(),
        "command_topic": f"{service.command_topic}",
        "default_response_topic": f"{service.response_topic}",
        "commands": {
            "status": {
                "task_name": "status",
                "arguments": {},
            },
            "init_tuner": {
                "task_name": "init_tuner",
                "arguments": {"force_tuner": "str"},
            },
            "restart_tuner": {
                "task_name": "restart_tuner",
                "arguments": {},
            },
        },
        "tuner_commands": tuner_commands,
    }
    json_payload = msgspec.json.encode(payload)
    logger.debug(
        f"Announcing {service.name} on {service.announce_topic}:\n{json_payload}"
    )
    await client.publish(service.announce_topic, json_payload, retain=True)


async def send_status(client, service):
    payload = {
        "state": "disabled",
        "ready": service.ready,
        "lock_supported": service.lock_supported,
        "lock_status": service.lock_status,
        "lock_error": service.lock_error,
        "error": service.init_error,
        "timestamp": time.time(),
    }
    if service.tuner is not None:
        payload["state"] = "online" if service.ready else "error"
        payload["tuner"] = dataclasses.asdict(service.tuner)
    json_payload = msgspec.json.encode(payload)
    logger.debug(
        f"Sending {service.name} status to {service.status_topic}:\n{json_payload}"
    )
    await client.publish(service.status_topic, json_payload, retain=True)
    return payload


async def send_response(client, service, response, command_payload=None):
    if command_payload is None:
        command_payload = {}
    response_topic = command_payload.get("response_topic", service.response_topic)
    session_id = command_payload.get("session_id", None)
    task_name = command_payload.get("task_name", None)

    response.update(
        session_id=session_id,
        task_name=task_name,
        timestamp=time.time(),
    )
    json_payload = msgspec.json.encode(response)
    logger.debug(
        f"Sending {service.name} response to {response_topic}:\n{json_payload}"
    )
    await client.publish(response_topic, json_payload)
    return response


async def process_config_command(client, service, payload):
    cmd = payload["task_name"].removeprefix("config.")
    args = payload.get("arguments", {})
    try:
        if cmd == "get":
            key = args.get("key", "")
            try:
                if not key:
                    value = service.cfg
                else:
                    # does service.cfg.{key} where key can have additional dot levels
                    value = operator.attrgetter(key)(service.cfg)
            except AttributeError:
                msg = f"Key '{key}' not found."
                logger.warning(msg)
                response = {"exception": msg}
                await send_response(client, service, response, payload)
            else:
                logger.debug(f"Got config key {key}: {value}")
                response = {"value": value}
                await send_response(client, service, response, payload)
        if cmd == "set":
            key = args.get("key", "")
            val = args["value"]
            # convert config to dict so we can deep update it and then use
            # msgspec.convert to go back to a dataclass with type checking
            cfg_dict = dataclasses.asdict(service.cfg)
            if not key:
                update_dict = val
            else:
                update_dict = functools.reduce(
                    lambda v, k: {k: v}, reversed(key.split(".")), val
                )
            updated_cfg_dict = deep_update(cfg_dict, update_dict)
            updated_cfg = msgspec.convert(updated_cfg_dict, type=type(service.cfg))
            service.cfg = updated_cfg
            logger.debug(f"Set config key {key}: {val}")
            response = {"value": dataclasses.asdict(service.cfg)}
            await send_response(client, service, response, payload)
    except Exception:
        logger.exception(
            f"Error processing config payload:\n{msgspec.json.encode(payload)}"
        )
        response = {"exception": traceback.format_exc()}
        await send_response(client, service, response, payload)


async def process_tuner_command(client, service, payload):
    args = payload.get("arguments", {})
    tuner_name = args.pop("tuner", None)
    # init tuner if none are or if the requested tuner is different from the active one
    if (service.tuner is None) or (
        tuner_name is not None and tuner_name != service.tuner.name
    ):
        logger.info(f"Requested tuner {tuner_name} is not active, initializing.")
        msg = init_tuner(service, force_tuner=tuner_name)
        if msg:
            response = {"message": msg}
            await send_response(client, service, response, payload)
    # now service.tuner is either None (failure) or the current/requested tuner
    try:
        cmd = payload["task_name"]
        logger.info(f"Processing {cmd} command")
        fun = getattr(service.tuner, cmd)
        result = fun(**args)
        response = {"value": result}
        await send_response(client, service, response, payload)
        await send_status(client, service)
    except Exception:
        logger.exception(
            f"Error processing command payload:\n{msgspec.json.encode(payload)}"
        )
        response = {"exception": traceback.format_exc()}
        await send_response(client, service, response, payload)


async def process_commands(client, service):
    logger.info(f"Service {service.name} listening for commands")
    async for message in client.messages:
        payload = msgspec.json.decode(message.payload)
        logger.debug(f"Received message:\n{msgspec.json.encode(payload)}")
        if payload["task_name"] == "init_tuner":
            logger.info("Processing init_tuner command")
            args = payload.get("arguments", {})
            init_tuner(service, **args)
            status_response = await send_status(client, service)
            await send_response(client, service, status_response, payload)
        elif payload["task_name"] == "restart_tuner":
            logger.info("Processing restart_tuner command")
            if service.tuner is not None:
                try:
                    service.tuner = dataclasses.replace(service.tuner)
                    init_tuner(service, force_tuner=service.tuner.name)
                except Exception:
                    logger.exception("Error restarting tuner")
                    response = {"exception": traceback.format_exc()}
                    await send_response(client, service, response, payload)
                else:
                    msg = "Restarted tuner successfully"
                    response = {"message": msg}
                    await send_response(client, service, response, payload)
            await send_status(client, service)
        elif payload["task_name"] == "status":
            logger.info("Processing status command")
            status_response = await send_status(client, service)
            await send_response(client, service, status_response, payload)
        elif payload["task_name"].startswith("config."):
            await process_config_command(client, service, payload)
        else:
            await process_tuner_command(client, service, payload)


async def main(service):
    will = aiomqtt.Will(
        service.status_topic,
        payload=msgspec.json.encode({"state": "offline"}),
        qos=0,
        retain=True,
    )
    client = aiomqtt.Client(
        "localhost",
        1883,
        keepalive=60,
        will=will,
    )
    interval = 5  # seconds
    while True:
        try:
            async with client:
                await client.subscribe(service.command_topic)
                await send_announce(client, service)
                await send_status(client, service)
                with exceptiongroup.catch(
                    {
                        Exception: lambda exc: logger.error("Exception", exc_info=exc),
                    }
                ):
                    async with anyio.create_task_group() as tg:
                        tg.start_soon(process_commands, client, service)
        except aiomqtt.MqttError:
            msg = (
                "Connection to MQTT server lost;"
                f" Reconnecting in {interval} seconds ..."
            )
            logger.warning(msg)
            await anyio.sleep(interval)


if __name__ == "__main__":
    logger.info("Starting TunerControl")
    service = jsonargparse.auto_cli(
        TunerControlService, env_prefix="TUNER", default_env=True
    )
    anyio.run(main, service)

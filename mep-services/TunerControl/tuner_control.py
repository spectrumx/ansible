# SPDX-FileCopyrightText: Copyright (c) 2026 Massachusetts Institute of Technology
# SPDX-License-Identifier: Apache-2.0

"""TunerControl service.

Author: John Marino <john.marino@colorado.edu> (09/2026)
"""

import logging
import os
import socket
import time

import aiomqtt
import anyio
import msgspec

SERVICE_NAME = "tunercontrol"
ANNOUNCE_TOPIC = f"{SERVICE_NAME}/announce"
COMMAND_TOPIC = f"{SERVICE_NAME}/command"
RESPONSE_TOPIC = f"{SERVICE_NAME}/response"
STATUS_TOPIC = f"{SERVICE_NAME}/status"
DATA_TOPIC = f"{SERVICE_NAME}/data"
EVENT_TOPIC = f"{SERVICE_NAME}/event"

# Connect to the MQTT broker running on this device.
MQTT_BROKER = "localhost"
MQTT_PORT = 1883

logger = logging.getLogger(SERVICE_NAME)
logger.setLevel(logging.INFO)
logger.propagate = False
logger.addHandler(logging.StreamHandler())


class TunerControlService:
    def __init__(self):
        self.node_id = socket.gethostname()
        self.backend = None
        self.detected_tuners = []
        self.tuner_class = None
        self.tuner = None
        self.ready = False
        self.error = None
        self.started = time.time()

    def initialize(self):
        self.close_tuner()
        self.ready = False
        self.error = None

        # Discover hardware first when the service does not yet have a selected tuner.
        if self.tuner_class is None:
            return self.discover()

        # Create only the backend selected during hardware discovery.
        self.tuner = self.tuner_class()

        # Initialize the selected physical tuner and close it if startup fails.
        try:
            self.tuner.initialize()
            self.ready = True
        except Exception:
            self.tuner.close()
            raise

        return self.status()

    def discover(self):
        from tuners.lmx2820 import LMX2820Tuner
        from tuners.valon import ValonTuner

        # Report every attached tuner while preserving the existing active tuner.
        tuner_classes = [ValonTuner, LMX2820Tuner]
        detected_classes = [tuner_class for tuner_class in tuner_classes if os.path.exists(tuner_class.device)]
        self.detected_tuners = [tuner_class.name for tuner_class in detected_classes]
        if self.tuner_class is not None:
            return self.status()
        if not detected_classes:
            raise RuntimeError("No tuner hardware detected")

        # Select the first detected tuner and initialize it.
        self.tuner_class = detected_classes[0]
        self.backend = self.tuner_class.name
        return self.initialize()

    def close_tuner(self):
        if self.tuner is not None:
            self.tuner.close()
        self.tuner = None
        self.ready = False

    def close(self):
        self.close_tuner()

    def status(self):
        status = {
            "state": "online" if self.ready else "error" if self.error else "offline",
            "ready": self.ready,
            "backend": self.backend,
            "detected_tuners": self.detected_tuners,
            "error": self.error,
            "timestamp": time.time(),
        }
        if self.tuner is not None:
            status.update(self.tuner.status())
        return status

    def commands(self):
        commands = ["status", "discover", "initialize"]
        if self.tuner is not None:
            commands.extend(self.tuner.supported_commands)
        return commands

    def announce(self):
        return {
            "title": "Tuner control",
            "description": "Control and monitor the configured tuner device",
            "type": "service",
            "version": "1.0",
            "service": SERVICE_NAME,
            "node_id": self.node_id,
            "time_started": self.started,
            "topics": {
                "announce": ANNOUNCE_TOPIC,
                "command": COMMAND_TOPIC,
                "response": RESPONSE_TOPIC,
                "status": STATUS_TOPIC,
                "data": DATA_TOPIC,
                "event": EVENT_TOPIC,
            },
            "backend": self.backend,
            "commands": self.commands(),
            "capabilities": self.tuner.capabilities() if self.tuner is not None else {},
        }

    def run_command(self, task_name, arguments):
        if task_name == "status":
            return self.status()
        elif task_name == "discover":
            return self.discover()
        elif task_name == "initialize":
            return self.initialize()
        elif not self.ready or self.tuner is None:
            raise RuntimeError("Tuner is not initialized")
        elif task_name == "set_frequency":
            return self.tuner.set_frequency(float(arguments["frequency_mhz"]))
        elif task_name == "get_frequency":
            return self.tuner.get_frequency()
        elif task_name == "set_power":
            return self.tuner.set_power(float(arguments["power_dbm"]))
        elif task_name == "get_power":
            return self.tuner.get_power()
        elif task_name == "set_external_reference":
            if not isinstance(arguments["enabled"], bool):
                raise ValueError("enabled must be true or false")
            return self.tuner.set_external_reference(arguments["enabled"])
        elif task_name == "get_external_reference":
            return self.tuner.get_external_reference()
        elif task_name == "set_reference_frequency":
            return self.tuner.set_reference_frequency(float(arguments["frequency_mhz"]))
        elif task_name == "get_reference_frequency":
            return self.tuner.get_reference_frequency()
        elif task_name == "get_lock_status":
            return self.tuner.get_lock_status()
        else:
            raise ValueError(f"Unsupported command: {task_name}")

    async def publish(self, client, topic, payload, retain=False):
        await client.publish(topic, msgspec.json.encode(payload), retain=retain)

    async def publish_response(self, client, payload, response):
        response["session_id"] = payload.get("session_id")
        response["task_name"] = payload.get("task_name")
        response["timestamp"] = time.time()
        topic = payload.get("response_topic", RESPONSE_TOPIC)
        await self.publish(client, topic, response)

    async def process_command(self, client, payload):
        task_name = payload.get("task_name")
        arguments = payload.get("arguments", {})
        logger.info("Processing %s command", task_name)

        try:
            value = self.run_command(task_name, arguments)
        except Exception as error:
            self.error = str(error)
            logger.exception("Tuner command failed: %s", task_name)
            await self.publish_response(client, payload, {"success": False, "error": str(error)})
            await self.publish(client, EVENT_TOPIC, {"event": "command_failed", "task_name": task_name, "error": str(error), "timestamp": time.time()})
            await self.publish(client, STATUS_TOPIC, self.status(), retain=True)
            return

        self.error = None
        await self.publish_response(client, payload, {"success": True, "value": value})
        if task_name == "initialize":
            await self.publish(client, EVENT_TOPIC, {"event": "tuner_initialized", "task_name": task_name, "backend": self.backend, "timestamp": time.time()})
        elif task_name == "discover":
            await self.publish(client, EVENT_TOPIC, {"event": "tuners_discovered", "task_name": task_name, "detected_tuners": self.detected_tuners, "backend": self.backend, "timestamp": time.time()})
        await self.publish(client, STATUS_TOPIC, self.status(), retain=True)

    async def process_commands(self, client):
        async for message in client.messages:
            try:
                payload = msgspec.json.decode(message.payload)
            except msgspec.DecodeError as error:
                logger.warning("Ignoring invalid command payload: %s", error)
                continue
            if not isinstance(payload, dict):
                logger.warning("Ignoring command payload that is not an object")
                continue
            await self.process_command(client, payload)

    async def run(self):
        will = aiomqtt.Will(STATUS_TOPIC, payload=msgspec.json.encode({"state": "offline"}), retain=True)
        while True:
            try:
                async with aiomqtt.Client(MQTT_BROKER, MQTT_PORT, keepalive=60, will=will) as client:
                    await client.subscribe(COMMAND_TOPIC)
                    try:
                        self.initialize()
                    except Exception as error:
                        self.error = str(error)
                        logger.exception("Could not initialize tuner")
                        await self.publish(client, EVENT_TOPIC, {"event": "initialization_failed", "backend": self.backend, "error": str(error), "timestamp": time.time()})
                    else:
                        await self.publish(client, EVENT_TOPIC, {"event": "tuner_initialized", "backend": self.backend, "timestamp": time.time()})
                    await self.publish(client, ANNOUNCE_TOPIC, self.announce(), retain=True)
                    await self.publish(client, STATUS_TOPIC, self.status(), retain=True)
                    await self.process_commands(client)
            except aiomqtt.MqttError:
                logger.exception("Connection to MQTT server lost; reconnecting in 5 seconds")
                await anyio.sleep(5)
            finally:
                self.close()


if __name__ == "__main__":
    logger.info("Starting TunerControl")
    service = TunerControlService()
    anyio.run(service.run)

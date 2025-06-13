import paho.mqtt.client as mqtt
import subprocess
import time
import json
import traceback
import os
import threading

service_name = "docker"
compose_base_dir = "/opt/radiohound/docker"  # Base directory where all docker-compose projects are stored
compose_file_name = "compose.yaml"

announce_packet = {
    "title": "Docker Compose Control Script",
    "description": "Manages Docker containers via docker-compose",
    "author": "Randy Herban <rherban@nd.edu>",
    "url": None,
    "source": "github.com/spectrumx/docker/docker-control.py",
    "output": {},
    "version": "0.2",
    "type": "service",
    "time_started": time.time(),
}

if not os.path.exists(os.path.join(compose_base_dir, compose_file_name)):
    print(f"No {compose_file_name} found")
    exit(1)

def run_compose_command(payload):
    command = payload.split()
    try:
        output = ''
        error = ''

        if command[0] == "start":
            print("Starting docker containers...")
            result = subprocess.run( ["docker","compose","up","-d"], cwd=compose_base_dir, capture_output=True, text=True)
            output = result.stdout
            error = result.stderr
        elif command[0] == "stop":
            print("Stopping docker containers...")
            result = subprocess.run( ["docker","compose","down"], cwd=compose_base_dir, capture_output=True, text=True)
            output = result.stdout
            error = result.stderr
        elif command[0] == "update":
            print("Updating docker containers...")
            result0 = subprocess.run( ["docker","compose","down"], cwd=compose_base_dir, capture_output=True, text=True)
            result1 = subprocess.run( ["docker","compose","pull"], cwd=compose_base_dir, capture_output=True, text=True)
            result2 = subprocess.run( ["docker","compose","up","-d","--force-recreate"], cwd=compose_base_dir, capture_output=True, text=True)
            output = result0.stdout + result1.stdout + result2.stdout
            error = result0.stderr + result1.stderr + result2.stderr

            time.sleep(5)

            # Ensure 'mqtt' container is running
            check_mqtt = subprocess.run(
                ["docker", "ps", "--filter", "name=mqtt", "--filter", "status=running", "--format", "{{.Names}}"],
                capture_output=True, text=True
            )
            if "mqtt" not in check_mqtt.stdout:
                print("mqtt container is not running, restarting all containers...")
                restart_result = subprocess.run(
                    ["docker", "compose", "up", "-d"],
                    cwd=compose_base_dir, capture_output=True, text=True
                )
                output += restart_result.stdout
                error += restart_result.stderr
        elif command[0] == "status":
            pass #Status is sent after every command
        else:
            error = f"Unknown command: {command[0]}"

        print(output)
        if not error == '':
            print(error)
        send_status(mqtt_client)
    except Exception as e:
        print(f"Error running docker-compose command: {e}")

def on_connect(client, userdata, flags, rc):
    print(f"{service_name} connected to mqtt: {rc}")
    client.subscribe(service_name + "/command")
    send_status(client)

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        print(f"Received message: {payload}")

        thread = threading.Thread(target=run_compose_command, args=(payload,))
        thread.daemon = True
        thread.start()
    except Exception as err:
        print(f"Failed to parse incoming message: {msg.payload.decode()}\n{err}\n{traceback.format_exc()}")

def send_status(client):
    try:
        result = subprocess.run(["docker", "ps", "--format", "{{json .}}"], capture_output=True, text=True)
        containers_info = [json.loads(line) for line in result.stdout.strip().splitlines()]
        '''
        Example docker ps output:
        {
        "Command": "\"python -u start.py\"",
        "CreatedAt": "2025-05-06 15:15:01 +0000 UTC",
        "ID": "6b9127a80bd0",
        "Image": "randyherban/icarus:latest",
        "Labels": "com.docker.compose.oneoff=False,com.docker.compose.project=docker,com.docker.compose.project.config_files=compose.yaml,com.docker.compose.project.working_dir=/opt/radiohound/docker,com.docker.compose.service=icarus,com.docker.compose.version=1.29.2,com.docker.compose.config-hash=8a3aaee136e9f0fa0acc880034e7bd9ce58220997f29d32963c5966f5a878693,com.docker.compose.container-number=1",
        "LocalVolumes": "0",
        "Mounts": "",
        "Names": "icarus",
        "Networks": "host",
        "Ports": "",
        "RunningFor": "29 hours ago",
        "Size": "5.08kB (virtual 717MB)",
        "State": "running",
        "Status": "Up 8 minutes"
        }
        '''

        payload = {
            "state": "online",
            "timestamp": time.time(),
            "task_name": "tasks.admin.save_docker",
            "arguments": {"containers": containers_info},
        }
        client.publish(service_name + "/status", json.dumps(payload), retain=True)
    except Exception as e:
        print(f"Error fetching status: {e}")

# MQTT setup
mqtt_client = mqtt.Client(client_id="docker-control")
mqtt_client.on_message = on_message
mqtt_client.on_connect = on_connect
mqtt_client.will_set(service_name + "/status", payload=json.dumps({"state": "offline"}), qos=0, retain=True)
mqtt_client.connect('localhost', 1883, 60)
mqtt_client.loop_forever()

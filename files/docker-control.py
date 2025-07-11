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
announce_data = {}
status_data = {}
send_status_timer = None             # Add a global timer reference

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
    global send_status_timer
    command = payload.split()
    try:
        output = ''
        error = ''

        # Stop the send_status_timer right away
        if send_status_timer is not None:
            send_status_timer.cancel()

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

        # Send status after a delay, to account for  any changes or multiple calls
        send_status_timer = threading.Timer(2.0, send_status)
        send_status_timer.daemon = True
        send_status_timer.start()

    except Exception as e:
        print(f"Error running docker compose command: {e}")

def on_connect(client, userdata, flags, rc):
    print(f"{service_name} connected to mqtt: {rc}")
    client.subscribe(service_name + "/command")

    send_status_timer = threading.Timer(2.0, send_status)  # 2 second delay
    send_status_timer.daemon = True
    send_status_timer.start()

def on_message(client, userdata, msg):
    global send_status_timer
    try:
        payload = msg.payload.decode()
        if 'announce' in msg.topic:
            # topic: announce/<container_name>
            announce_data[msg.topic.split('/')[1]] = json.loads(payload)
            return
        elif 'status' in msg.topic:
            # topic: <container_name>/status
            status_data[msg.topic.split('/')[0]] = json.loads(payload)
            return
        else:
            thread = threading.Thread(target=run_compose_command, args=(payload,))
            thread.daemon = True
            thread.start()

        # Debounce send_status: cancel previous timer and start a new one
        if send_status_timer is not None:
            send_status_timer.cancel()
        send_status_timer = threading.Timer(2.0, send_status)  # 2 second delay
        send_status_timer.daemon = True
        send_status_timer.start()
    except Exception as err:
        print(f"Failed to parse incoming message: {msg.payload.decode()}\n{err}\n{traceback.format_exc()}")

def send_status():
    global mqtt_client
    '''
    Fetch the list of running containers and some status information, sends to Icarus to be sent to server
    '''
    try:
        containers_info = []

        # Step 1: Get list of running containers
        ps_output = subprocess.check_output(["docker", "ps", "--format", "{{.Names}}"]).decode("utf-8").strip().splitlines()

        # Step 2: For each container, inspect and extract info
        for name in ps_output:
            try:
                inspect_json = subprocess.check_output(["docker", "inspect", name]).decode("utf-8")
                inspect_data = json.loads(inspect_json)[0]

                # Extract build_version label if it exists
                labels = inspect_data.get("Config", {}).get("Labels", {})
                build_version = labels.get("build_version", "unknown")

                container_record = {
                    "name": name,
                    "ID": inspect_data.get("Id"),
                    "docker": {
                        "build_version": build_version,
                        "Created": inspect_data.get("Created"),
                        "Image": inspect_data.get("Config", {}).get("Image"),
                        "Status": inspect_data.get("State", {}).get("Status"),
                        "CPUPercent": inspect_data.get("HostConfig", {}).get("CpuPercent")
                    }
                }

                # Attach announce data if available
                if name in announce_data:
                    container_record["announce"] = announce_data[name]

                # Attach status data if available
                if name in status_data:
                    container_record["status"] = status_data[name]

                containers_info.append(container_record)

            except subprocess.CalledProcessError as e:
                print(f"Error inspecting container {name}: {e}")
    

        payload = {
            "state": "online",
            "timestamp": time.time(),
            "task_name": "tasks.admin.save_docker",
            "arguments": {"containers": containers_info},
        }
        mqtt_client.publish(service_name + "/status", json.dumps(payload), retain=True)
    except Exception as e:
        print(f"Error fetching status: {e}")

# MQTT setup
mqtt_client = mqtt.Client(client_id="docker-control")
mqtt_client.on_message = on_message
mqtt_client.on_connect = on_connect
mqtt_client.will_set(service_name + "/status", payload=json.dumps({"state": "offline"}), qos=0, retain=True)
mqtt_client.connect('localhost', 1883, 60)
mqtt_client.subscribe('announce/#')
mqtt_client.subscribe('+/status')
mqtt_client.loop_forever()

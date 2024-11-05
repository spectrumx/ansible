
# MQTT local command schema
This defines how the local services will interact with each other. Icarus will maintain connection to the sensing platform and relay messages to local services as necessary.  

## Topic categories:
- `docker-control/#` - script to stop/start/update docker containers
- `radiohound/clients/command_to_all` - Icarus - connect to sensing platform via MQTT
- `rfsoc/#` - RFSoC control - change freq, gain, etc
- `available-processing/[name]` - List of currently running processing scripts, see below for examples.  Name subject to change.
- `processing/fft` - Perform FFT - TBD
- `processing/filter` - Perform filter - TBD
- `processing/custom` - Perform custom processing - TBD


### Docker control schema:
The full topic should include the name of the requesting service: `docker-control/icarus`, `docker-control/rfsoc`

Messages should be a JSON formatted string like so:
```
{
  "task_name": "start|stop|restart|pull",
  "image": "docker container name",
  "version": "optional version for pulling new container"
}
```

### Icarus
Commands will be sent to `radiohound/clients/command_to_all` which mimics the command structure of the sensing platform: `radiohound/clients/command/MAC_ADDRESS_OF_TARGET`

Messages should be a JSON formatted string like so:
```
{ # Scan request
  "task_name": "tasks.rf.scan.periodogram",
  "arguments": 
    {
      'fmin': 100e6,
      'fmax': 110e6,
      'gain': 0,
      'batch_id': 0,
      'N_periodogram_points': 1024,
      'output_topic': 'radiohound/clients/data/<mac_address>'
    }
}
```
or
```
{ # Ring buffer control
  "task_name": "tasks.digitalrf.copy_ring_buffer",
  "arguments": 
    {
      // to be determined
    }
}
```
or
```
{ # Upload data to SDS
  "task_name": "tasks.digitalrf.upload",
  "arguments": 
    {
      // to be determined
    }
}
```


### Available processing scripts
As a processing script comes online, it should send an mqtt message to `available-processing/[name]` with it's appropriate name (fft, filter, trigger_2.4, etc), the retain flag set to True and a last will and testament to clear the message when the script ends.

Code snippet:
```
import paho.mqtt.client as mqtt

topic = 'available-processing/fft'
lwt = (topic "offline", 0, True)

client = mqtt.Client('fft')
client.will_set(*lwt)

# Define the on_connect callback
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        client.publish(topic, "online", qos=0, retain=True)



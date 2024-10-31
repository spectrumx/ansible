# Ansible Configuration Scripts
This is meant to be ran on a fresh image and will setup all necessary packages and configurations to tie it into the SpectrumX platform.  

## Usage

- Create `/opt/radiohound`
- Place system key in `/opt/radiohound/.ssh/id_rsa`.  See Randy for more info about the key.
- Place icarus key in `/opt/radiohound/.ssh/icarus_key`.  See Randy for more info about the key.
- Clone this repo to `/opt/ansible`
```
GIT_SSH_COMMAND='ssh -i /opt/radiohound/.ssh/ansible_key -o IdentitiesOnly=yes' git clone git@github.com:spectrumx/ansible.git
```
- Run the following:
```
python3 /opt/ansible/run.py
```


## What's managed?
- Icarus, the application that manages connection to the SpectrumX sensing platform
- Ensures Ansible runs nightly
- Installs dependency packages (Docker, Mosquitto)



# To build Jetson image

## Physical setup:
- Ubuntu 2204 machine
- Jetson booted in recovery mode, once booted remove jumper
- Ubuntu machine connected (via USB-A) to Jetson (USB-C)
- NVME drive in a USB-C drive chassis, connected with USB-A adapter to the Jetson

## Software setup on Ubuntu machine:
- Confirm Jetson is connected with lsusb
- Install sdkmanager (https://docs.nvidia.com/sdk-manager/install-with-sdkm-jetson/index.html)
- Run sdkmanager to download stuff
```
sdkmanager --cli --action downloadonly --flash skip --login-type devzone --product Jetson --target-os Linux --version 6.1 --host --target JETSON_ORIN_NANO_TARGETS --select 'Jetson Linux' --select 'Jetson Linux image' --select 'Flash Jetson Linux' --select 'Jetson Runtime Components' --select 'Additional Setups' --select 'CUDA Runtime' --select 'NVIDIA Container Runtime' --deselect 'CUDA X-AI Runtime' --deselect 'Computer Vision Runtime' --deselect Multimedia --deselect 'Jetson SDK Components' --deselect CUDA --deselect 'CUDA-X AI' --deselect 'Computer Vision' --deselect 'Developer Tools' --deselect 'Jetson Platform Services - Coming Soon' --deselect 'Jetson Platform Services - Coming Soon'
```

This will download the required packages in `~/nvidia`, in my case:
```
/home/rherban/nvidia/nvidia_sdk/JetPack_6.1_Linux_JETSON_ORIN_NANO_TARGETS/Linux_for_Tegra
```

Prepare the partition table so we can have a separate /data.
```
cp tools/kernel_flash/flash_l4t_external.xml mep_partition.xml
```
Add these lines near the bottom, after the APP partition:
```
        <partition name="DATA" id="2" type="data">
            <allocation_policy> sequential </allocation_policy>
            <filesystem_type> basic </filesystem_type>
            <size> 0 </size>
            <file_system_attribute> 0 </file_system_attribute>
            <allocation_attribute> 0x808 </allocation_attribute>
            <align_boundary> 16384 </align_boundary>
            <percent_reserved> 0 </percent_reserved>
            <filename> data.img </filename>
            <description> Space for recorded data.</description>
        </partition>
```
Prepare the disk image:
```
dd if=/dev/zero of=data.img bs=1M count=10 
mkfs.ext4 data.img  
```

Run this command to build image and flash the Jetson

```
ADDITIONAL_DTB_OVERLAY_OPT="BootOrderNvme.dtbo" ./tools/kernel_flash/l4t_initrd_flash.sh --external-device sda1 -c tools/kernel_flash/flash_l4t_external.xml -p "-c bootloader/generic/cfg/flash_t234_qspi.xml" --showlogs --network usb0 jetson-orin-nano-devkit sda1
```


# TODO:
- Add ansible to initial image
- Add separate /data partition
- Skip Jetson first-boot questions
- Fix Icarus' virtualenv path






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



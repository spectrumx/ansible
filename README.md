# Ansible Configuration Scripts
This is meant to be ran on a fresh image and will setup all necessary packages and configurations to tie it into the SpectrumX platform.  

## Usage

- Create `/opt/radiohound`
- Place deploy key in `/opt/radiohound/.ssh/ansible_key`.  See Randy for more info about the key.
- Place system github key in `/opt/radiohound/.ssh/icarus_key`.  See Randy for more info about the key.
- Clone this repo to `/opt/radiohound/ansible`
```
GIT_SSH_COMMAND='ssh -i /opt/radiohound/.ssh/ansible_key -o IdentitiesOnly=yes' git clone git@github.com:spectrumx/ansible.git
```
- Run the following:
```
python3 /opt/radiohound/ansible/run.py
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
sdkmanager --cli --action downloadonly --login-type devzone --product Jetson --target-os Linux --version 6.1 --host --target JETSON_ORIN_NANO_TARGETS --select 'Jetson Linux' --select 'Jetson Linux image' --select 'Flash Jetson Linux' --select 'Jetson Runtime Components' --select 'Additional Setups' --select 'CUDA Runtime' --select 'NVIDIA Container Runtime' --deselect 'CUDA X-AI Runtime' --deselect 'Computer Vision Runtime' --deselect Multimedia --deselect 'Jetson SDK Components' --deselect CUDA --deselect 'CUDA-X AI' --deselect 'Computer Vision' --deselect 'Developer Tools' --deselect 'Jetson Platform Services - Coming Soon' --deselect 'Jetson Platform Services - Coming Soon'
```

This will download the required packages in `~/nvidia`, in my case:
```
/home/rherban/nvidia/nvidia_sdk/JetPack_6.1_Linux_JETSON_ORIN_NANO_TARGETS/Linux_for_Tegra
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













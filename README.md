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
- Installs dependency packages (Radioconda/GNURadio, Docker, Mosquitto)



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
sdkmanager --cli --action downloadonly --flash skip --login-type devzone --product Jetson --target-os Linux --version 6.1 --host --target JETSON_ORIN_NANO_TARGETS --license accept --select 'Jetson Linux' --select 'Jetson Linux image' --select 'Flash Jetson Linux' --select 'Jetson Runtime Components' --select 'Additional Setups' --select 'CUDA Runtime' --select 'NVIDIA Container Runtime' --deselect 'CUDA X-AI Runtime' --deselect 'Computer Vision Runtime' --deselect Multimedia --deselect 'Jetson SDK Components' --deselect CUDA --deselect 'CUDA-X AI' --deselect 'Computer Vision' --deselect 'Developer Tools' --deselect 'Jetson Platform Services - Coming Soon' --deselect 'Jetson Platform Services - Coming Soon'
```

This will download the required packages in `~/nvidia`, in my case:
```
/home/rherban/nvidia/nvidia_sdk/JetPack_6.1_Linux_JETSON_ORIN_NANO_TARGETS/Linux_for_Tegra
```

<!-- ### DOES NOT WORK CURRENTLY, JUST USE SINGLE PARTITION: Prepare the partition table so we can have a separate /data.
```
cp tools/kernel_flash/flash_l4t_external.xml mep_partition.xml
```
Add these lines near the bottom, after the APP partition:
```
        <partition name="DATA" id="3" type="data">
            <allocation_policy> sequential </allocation_policy>
            <filesystem_type> basic </filesystem_type>
            <size> 0 </size>
            <file_system_attribute> 0 </file_system_attribute>
            <allocation_attribute> 0x808 </allocation_attribute>
            <align_boundary> 16384 </align_boundary>
            <percent_reserved> 0 </percent_reserved>
            <unique_guid> DATAUUID </unique_guid>
            <filename> data.img </filename>
            <description> Space for recorded data.</description>
        </partition>
```
Prepare the disk image:
```
dd if=/dev/zero of=data.img bs=1M count=10 
mkfs.ext4 data.img  
``` -->

### Add files to rootfs before building the image
```
cd /home/rherban
git clone git@github.com:spectrumx/ansible.git
cd /home/rherban/nvidia/nvidia_sdk/JetPack_6.1_Linux_JETSON_ORIN_NANO_TARGETS/Linux_for_Tegra/rootfs
mkdir opt/radiohound
cp -r /path/to/keys/.ssh opt/radiohound/.ssh    # MUST GET KEYS FROM RANDY
cp /home/rherban/ansible/files/setup_ansible.service etc/systemd/system/
cp /home/rherban/ansible/run.py root/setup_ansible.py
chmod 755 root/setup_ansible.py
ln -s etc/systemd/system/setup_ansible.service etc/systemd/system/multi-user.target.wants/setup_ansible.service
```


Run this command to build image and flash the Jetson

```
ADDITIONAL_DTB_OVERLAY_OPT="BootOrderNvme.dtbo" ./tools/kernel_flash/l4t_initrd_flash.sh --external-device sda1 -c tools/kernel_flash/flash_l4t_external.xml -p "-c bootloader/generic/cfg/flash_t234_qspi.xml" --showlogs --network usb0 jetson-orin-nano-devkit sda1
```


# TODO:
- Add separate /data partition
- Fix Icarus' virtualenv path






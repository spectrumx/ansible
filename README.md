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



# To build Jetson image with A608 Carrier Board

## Physical setup:
- Ubuntu 2204 machine
- Jetson booted in recovery mode, once booted remove jumper
- Ubuntu machine connected (via USB-A) to Jetson (USB-C)
- NVME drive in a USB-C drive chassis, connected with USB-A adapter to the Jetson
- Confirm Jetson is connected by running `lsusb` and looking for Nvidia device

## Software setup on Ubuntu machine:
Directions taken from https://wiki.seeedstudio.com/reComputer_A608_Flash_System
- Download Driver Package (BSP) and Sample Root Filesystem from https://developer.nvidia.com/embedded/jetson-linux-r363
- Download peripheral drivers from https://szseeedstudio-my.sharepoint.cn/:u:/g/personal/youjiang_yu_szseeedstudio_partner_onmschina_cn/EbF6_Z1ocnZKnEfynnxDZ7UBkQTAHwq7H1dsga3RITPwhw?e=395QXx


```
cd <path to where you downloaded the above files>
sudo apt install unzip 
sudo tar xpf Tegra_Linux_Sample-Root-Filesystem_R35.4.1_aarch64.tbz2 -C Linux_for_Tegra/rootfs/
cd Linux_for_Tegra/
sudo ./apply_binaries.sh
sudo ./tools/l4t_flash_prerequisites.sh
cd ..
unzip a608_jp60.zip
sudo cp -r ./608_jp60/Linux_for_Tegra/* ./Linux_for_Tegra/
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
cd Linux_for_Tegra/rootfs
mkdir opt/radiohound
cp -r /home/rherban/ssh opt/radiohound/.ssh    # MUST GET KEYS FROM RANDY
chmod 600 opt/radiohound/.ssh/id_rsa
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
- Add SDS pip package






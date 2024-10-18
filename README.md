# Ansible Configuration Scripts
This is meant to be ran on a fresh image and will setup all necessary packages and configurations to tie it into the SpectrumX platform.  

## Usage

- Create `/opt/radiohound`
- Place SSH private key in `/opt/radiohound/.ssh/ansible_key`.  See Randy for more info about the key.
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
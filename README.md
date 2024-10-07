# Ansible Configuration Scripts
This is meant to be ran on a fresh image and will setup all necessary packages and configurations to tie it into the SpectrumX platform.  

## Usage

- Create `/opt/radiohound`
- Clone this repo to `/opt/radiohound/ansible`
- Place SSH private key in `/opt/radiohound/.ssh/id_rsa`.  See Randy for more info about the key.
- Run the following:
```
python3 /opt/radiohound/ansible/run.py
```


## What's managed?
- Icarus, the application that manages connection to the SpectrumX sensing platform
- Ensures Ansible runs nightly
- Installs dependency packages (Docker, Mosquitto)
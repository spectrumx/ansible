import configparser
import os
from random import randint
import socket
import subprocess
import sys
from time import sleep

homedir="/opt/ansible"
logfile="/var/log/ansible-pull.log"
ssh_file="/opt/radiohound/.ssh/id_rsa"  # SSH KEY MUST EXIST
if not os.path.exists(ssh_file):
  print(f"SSH KEY NOT FOUND!\nPlease provide key in {ssh_file} to continue")
  exit(1)

if not os.path.exists(homedir):
  subprocess.call(["git","clone","git@github.com:spectrumx/ansible.git"],env=dict(GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new -i ' + ssh_file), cwd="/opt")
if not os.path.exists(homedir):
  print("FAILED to checkout git repo.  Aborting install...")
  sys.exit(1)
os.chdir(homedir)

# If host doesn't exist, add it to 'all' category otherwise Ansible complains
# This allows us to specify host based requirements in ansible without having a full inventory list (since most nodes are ephemeral)
# Hosts can be added to multiple groups
hostname = socket.gethostname()
config = configparser.ConfigParser(allow_no_value=True)
config.read("inventory/inventory.ini")
if not config.has_section("all"):
  config.add_section("all")
config.set("all",hostname,None)
with open("inventory/inventory-runtime.ini", "w") as configfile:    # save
  config.set("all","; Hosts will be automatically added to 'all' group via Ansible run script and should be added to additinal groups in Git if required.", None)
  config.write(configfile)


# Do we need to install ansible?
if not os.path.isfile("/usr/bin/ansible-playbook"):
  print("Installing ansible")
  # Ignore time checks for apt update as we might run early enough in the boot process that ntp hasn't ran yet.  
  subprocess.call(["/usr/bin/apt", "-o","Acquire::Check-Valid-Until=false","-o","Acquire::Check-Date=false", "update"])
  subprocess.call(["/usr/bin/apt", "-o","Acquire::Check-Valid-Until=false","-o","Acquire::Check-Date=false", "install","-y","ansible","curl"])

# Check arguments
verbose = False
if len(sys.argv) > 1 and "verbose" in sys.argv:
    verbose = True
    sys.argv.remove("verbose")
ansible_verbosity = ["-vvv"] if verbose else []

# If you specify 'git' as an argument or we're running through cron (no tty), then update git
if (len(sys.argv) > 1 and "git" in sys.argv) or not sys.stdout.isatty():
  print("Updating git repo")
  subprocess.call(["git","reset","--hard"],env=dict(GIT_SSH_COMMAND='ssh -i ' + ssh_file))
  subprocess.call(["git","fetch","-a"],env=dict(GIT_SSH_COMMAND='ssh -i ' + ssh_file))
  subprocess.call(["git","pull"],env=dict(GIT_SSH_COMMAND='ssh -i ' + ssh_file))


if not sys.stdout.isatty():
  # Add delay if ran from cron.  Helps prevent stampeding herd problem on central services
  sleep(randint(10,120))
  subprocess.call(["curl","-s","-L","http://radiohound.ee.nd.edu/config/known_hosts","-o","/opt/radiohound/.ssh/known_hosts"])
  subprocess.call(["cp","/opt/radiohound/.ssh/known_hosts","/root/.ssh/known_hosts"])


# If argument is a yml file, run it directly rather than whole playbook
if len(sys.argv) > 1 and "yml" in sys.argv[1]:
  file = sys.argv[1]
else:
  file = 'master_playbook.yml'

output = open(logfile,'w')
ansible_cmd = ["/usr/bin/ansible-playbook","-e", f"actual_hostname={hostname}","-i","inventory/inventory-runtime.ini","--connection=local"] + ansible_verbosity + [file]
if sys.stdout.isatty() or 'setup_ansible' in os.path.basename(__file__):
  subprocess.call(ansible_cmd, env=dict(PATH='/usr/bin/:/bin:/usr/local/sbin:/usr/sbin:/sbin',HOME=homedir))
else:
  subprocess.call(ansible_cmd, stdout=output, stderr=subprocess.STDOUT, env=dict(PATH='/usr/bin/:/bin:/usr/local/sbin:/usr/sbin:/sbin',HOME=homedir))
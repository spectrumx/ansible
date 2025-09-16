#!/usr/bin/env python3
#
# afe_service.py
#
# MIT Haystack Observatory
# Ben Welchman 08-01-2025
#
# --------------------------
#
# !! IMPORTANT !!
#
# - gpsd must be initialized before afe_service.py
#     >> sudo systemctl start gpsd.service gpsd.socket
#
# - /opt/ansible/files/gpsd must include:
#
#    # Devices gpsd should collect to at boot time.
#    # They need to be read/writeable, either by user gpsd or the group dialout.
#    DEVICES="/dev/ttyGNSS1"
#
#    # Other options you want to pass to gpsd
#    GPSD_OPTIONS="-n -r -s 460800 -D 3 -F /var/run/gpsd.sock"
#
#    # Automatically hot add/remove USB GPS devices via gpsdctl
#    USBAUTO="false"
#
# - /opt/ansible/gpsd.yml must include the following tasks
#   AFTER "Deploy gpsd default configuration" and
#   BEFORE "Enable gpsd.service":
#
#   - name: Add mepuser to gpsd group
#     user:
#       name: "mepuser"
#       groups: "gpsd"
#       append: true
#
#   - name: Create systemd directory for gpsd.socket
#     file:
#       path: /etc/systemd/system/gpsd.socket.d
#       state: directory
#       mode: '0755'
#
#   - name: Make gpsd.socket world-writable
#     copy:
#       dest: /etc/systemd/system/gpsd.socket.d/override.conf
#       mode: '0644'
#       content: |
#         [Socket]
#         SocketMode=0666
#
#
# --------------------------
#
# Class: Telemetry
#
#   __init__
#   request_telem
#   request_registers
#   print
#   log
#
#
# List of Functions:
#
#   send_nmea_command
#   start_command_server
#   handle_commands
#   gpsd_monitor
#   nmea_to_epoch
#   reduce
#   eval_packet
#   add_cksum
#   write_max
#   init_all
#   main
#
# --------------------------
#
# Future work:
#
# - Add tuner status to telemetry
# - Documentation and housekeeping
# - Integrate commands for:
#   - manually set or query time
#   - set or query magnetometer parameters
#   - set or query imu parameters
#   - reset/re-tare magnetometer or imu
#
# --------------------------

import os
import time
import socket
import threading
from datetime import datetime, timezone
import numpy as np
import csv

SOCKET_PATH = '/tmp/afe_service.sock'  # Path for comms with afe.py

global device
global rate
global new_run
global gpsd
global gps_in_progress
global telem_in_progress
global regs_in_progress

device = '/dev/ttyGNSS1'               # Device name for RP2040
rate = 60                              # Defaults to 60s logging period
gps_in_progress = True                 # Flag to avoid printing an incomplete log
telem_in_progress = True
regs_in_progress = True

def send_nmea_command(cmd_str):

  if not cmd_str.endswith("\r\n"):
    cmd_str += "\r\n"

  hexcmd = cmd_str.encode("ascii").hex()
  message = f"&{device}={hexcmd}\n"

  gpsd_out = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  gpsd_out.connect("/var/run/gpsd.sock")

  gpsd_out.sendall(message.encode("ascii"))

  reply = gpsd_out.recv(4096).decode("ascii").strip()

  if reply != "OK":
    raise RuntimeError(f"gpsd error on send. reply: {reply}")

  gpsd_out.close

class Telemetry:

    def __init__(self):

      self.gps = []
      self.telem = []
      self.registers = []
      self.RTCtime = None

    def request_telem(self):

      self.telem.clear()

      msg_draft = "$TELEM?*"
      msg = add_cksum(msg_draft)
      send_nmea_command(msg)

    def request_registers(self):

      self.registers.clear()

      msg_draft = "$MAX?*"
      msg = add_cksum(msg_draft)
      send_nmea_command(msg)

    def print(self):

      global gps_in_progress
      global telem_in_progress
      global regs_in_progress

      self.request_telem()
      self.request_registers()

      telem_in_progress = True
      regs_in_progress = True

      all_items = []

      start = time.monotonic()
      wait = 0
      while gps_in_progress is True and wait < 1.2:
        wait = time.monotonic() - start
        time.sleep(0.001)

      for i in range(len(self.gps)):
        all_items.append(str(self.gps[i]))

      start = time.monotonic()
      wait = 0
      while telem_in_progress is True and wait < 1.2:
        wait = time.monotonic() - start
        time.sleep(0.001)

      for i in range(len(self.telem)):
        all_items.append(str(self.telem[i]))

      start = time.monotonic()
      wait = 0
      while regs_in_progress is True and wait < 1.2:
            wait = time.monotonic() - start
            time.sleep(0.001)

      for row in range(7):

        if row == 0:
          tag = "MAINREG:"

        elif row in (1, 2):
          idx = str(row)
          tag = "TX" + idx + "REG: "

        elif row in (3, 4, 5, 6):
          idx = str(row - 2)
          tag = "RX" + idx + "REG: "

        all_items.append(tag + str(self.registers[i]))

      return all_items

    def log(self):

        global new_run
        global path
        global gps_in_progress
        global telem_in_progress
        global regs_in_progress

        self.request_telem()
        self.request_registers()

        telem_in_progress = True
        regs_in_progress = True

        start = time.monotonic()
        wait = 0
        while gps_in_progress is True and wait < 1.2:
          wait = time.monotonic() - start
          time.sleep(0.001)

        if self.RTCtime is None:
          self.RTCtime = int(datetime.now(timezone.utc).timestamp())

        t = datetime.fromtimestamp(self.RTCtime, tz=timezone.utc)
        timestamp = t.strftime("%Y-%m-%d_%H:%M:%S")

        if new_run is True:
          base = "/data/telemetry_log"
          folder_name = f"mep-telemetry-log_{timestamp}"
          path = os.path.join(base, folder_name)
          os.makedirs(path, exist_ok=True)
          new_run = False

        filename = f"telemetry_{timestamp}.csv"
        full_path = os.path.join(path, filename)

        with open(full_path, "w", newline="", encoding="utf-8") as telem_csv:
          writer = csv.writer(telem_csv)

          for i in range(len(self.gps)):
            try:
              line = self.gps[i].split('*')[0]
              line = line.split(',')
              writer.writerow(line)
            except IndexError:
              print("GPS Index Error on: ", line)
            except AttributeError:
              print("GPS Attribute Error on: ", line)

          start = time.monotonic()
          wait = 0
          while telem_in_progress is True and wait < 0.4:
            wait = time.monotonic() - start
            time.sleep(0.001)

          for i in range(len(self.telem)):
            try:
              line = self.telem[i].split('*')[0]
              line = line.split(',')
              writer.writerow(line)
            except IndexError:
              print("Telemetry Index Error on: ", line)
            except AttributeError:
              print("Telemetry Attribute Error on: ", line)

#         writer.writerow(<tuner>) # ADD TUNER TELEM HERE

          start = time.monotonic()
          wait = 0
          while regs_in_progress is True and wait < 0.4:
            wait = time.monotonic() - start
            time.sleep(0.001)

          for row in range(7):

            if row == 0:
              line = ["MAINREG"]

            elif row in (1, 2):
              idx = str(row)
              line = ["TX" + idx + "REG"]

            elif row in (3, 4, 5, 6):
              idx = str(row - 2)
              line = ["RX" + idx + "REG"]

            for column in range(10):
              try:
                state = self.registers[row][column]
              except IndexError:
                state = "n/a"
              line.append(state)

            writer.writerow(line)
            line = []

        print("Telemetry logged at: ", filename)

global_telemetry = Telemetry()

def start_command_server():

  if os.path.exists(SOCKET_PATH):
    try:
      os.remove(SOCKET_PATH)
    except OSError:
      raise

  server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  server.bind(SOCKET_PATH)
  server.listen(1)

  while True:
    conn, _ = server.accept()
    threading.Thread(target=handle_commands, args=(conn,), daemon=True).start()

def handle_commands(conn):

  global rate

  raw = conn.recv(4096)
  if not raw:
    conn.close()
    return

  command = raw.decode('utf-8', errors='ignore').split()

  block, channel, addr, bit = map(int, command)

  if block in (0, 1, 2):          # Instruction is to write a register
    write_max(block, channel, addr, bit)
    global_telemetry.log()
    conn.sendall(b"AFE Controls Updated\n")

  elif block == 3:
    all_items = global_telemetry.print()
    response = f"All Telemetry:\n"
    for row in all_items:
      response = response + row + "\n"
    conn.sendall(response.encode('ascii'))

  elif block == 4:
    global_telemetry.log()
    conn.sendall(b"Telemetry Logged\n")

  elif block == 5:
    rate = channel
    rateStr = str(rate)
    response = f"Telemetry Log Period: {rateStr}s\n"
    conn.sendall(response.encode('ascii'))

  conn.close()

def gpsd_monitor():

  global gps_in_progress
  global telem_in_progress
  global regs_in_progress

  msg = gpsd.makefile('r', encoding='ascii', newline='\n')
  _ = msg.readline()

  for line in msg:

    line = line.strip()

    if line.startswith('$PGPS'):
      gps_in_progress = True
      if global_telemetry.gps is not None:
        global_telemetry.gps.clear()

    elif line.startswith('$PGPN'):
      gps_in_progress = False

    elif line.startswith('$PTEL'):
      global_telemetry.telem.clear()

    elif line.startswith('$G'):
      global_telemetry.gps.append(line)

      if line.startswith('$GNRMC'):
        error, RTCtime = nmea_to_epoch(line)
        if not error:
          global_telemetry.RTCtime = RTCtime

    elif line.startswith('$PMIT') and '$PMITSR' not in line:
      global_telemetry.telem.append(line)
      if line.startswith('$PMITG'):
        telem_in_progress = False

    elif line.startswith('$PMAX'):
      split = line.split(',')
      for row in range(1,8):
        reg_row = []
        for col in range(10):
          reg_row.append(int(split[row][col]))
        global_telemetry.registers.append(reg_row)
      regs_in_progress = False

    else:
      continue

def nmea_to_epoch(nmea):

  error = False

  nmea_str = str(nmea)

  aa = nmea_str.split(",")

  try:
    hh = int(aa[1][0:2])
    mm = int(aa[1][2:4])
    ss = int(aa[1][4:6])
    DD = int(aa[9][0:2])
    MM = int(aa[9][2:4])
    YY = int(aa[9][4:6]) + 2000

    dt = datetime(YY, MM, DD, hh, mm, ss, tzinfo=timezone.utc)
    epoch_time = int(dt.timestamp())

  except:
    error = True
    epoch_time = None

  return error, epoch_time

def reduce(iterable, initializer=None):
    it = iter(iterable)
    if initializer is None:
        value = next(it)
    else:
        value = initializer
    for element in it:
        value = value ^ element
    return value

def eval_packet(packet,gen_cksm_f=False,debug_f=False):
     err_code = 0
     ascii_checksum = 0
     calculated_checksum = 0

     dollar_cnt = packet.count("$")
     star_cnt = packet.count("*")

     if (packet[0] == '$'):
       if (dollar_cnt != star_cnt):               # lost data
         err_code = -1
     else:                                       # pre-checked, should not happen
       print("data does not begin with '$'", dollar_cnt, star_cnt)
       err_code = -2
     # end else error

     if (err_code == 0):
       packet = packet.strip("$\n")

       nmeadata, ascii_checksum = packet.split("*",1)

       checksum = None
       if (not gen_cksm_f):              # checksum present, don't generate
         try:
           checksum = int(ascii_checksum,16)
         except Exception as eobj:       # encountered noise instead of checksum
           err_code = -3
         # end except
       else:
         pass   # no checksum to extract
       # else no checksum
     # endif not error

     if (err_code == 0):
       calculated_checksum = reduce((ord(s) for s in nmeadata), 0)
       if (checksum == calculated_checksum):
         pass
       else:
         if (not gen_cksm_f):   # compare generated vs read
           err_code = -4
         else:
           checksum = calculated_checksum
         # end else use manufactured checksum
       # end else bad checksum
     # endif parse passes checks
     return err_code, ascii_checksum, calculated_checksum

def add_cksum(pkt_in):

  packet_out = ""

  err_code, dck, cck = eval_packet(pkt_in,gen_cksm_f=True,debug_f=False)
  if (err_code != 0):
    err_f = True
  # endif

  ck_hex = hex(cck)             # to string
  ck_split = ck_hex.split('x')  # split at hex identifier
  ck_str = ck_split[1].upper()  # upper case
  if ( len(ck_str) == 1):       # if single digit
    ck_str = "0" + ck_str       # then prefix with '0', ex: "0A"
  # endif single digit

  packet_out = pkt_in + ck_str

  return packet_out

def write_max(block, channel, addr, bit):

  msg_draft = "$PMIT"

  if block == 0:
    msg_draft += "MAX"

  elif block == 1:
    msg_draft += "XT"
    msg_draft += str(channel)

  elif block == 2:
    msg_draft += "XR"
    msg_draft += str(channel)

  else:
    return 0

  msg_draft += "," + str(addr) + "," + str(bit) + "*"
  msg = add_cksum(msg_draft)
  send_nmea_command(msg)

  return

def init_all():

  global gpsd

  gpsd = socket.create_connection(("127.0.0.1", 2947))
  gpsd.sendall(b'?WATCH={"enable":true, "raw":1};\n')

  threading.Thread(target=start_command_server, daemon=True).start()
  monitor = threading.Thread(target=gpsd_monitor, daemon=False)
  monitor.start()

  print("GPSD Initialized")

def main():

  global rate

  global_telemetry.log()

  start = time.monotonic()

  while True:
    if time.monotonic() - start >= rate:
      start = time.monotonic()
      global_telemetry.log()

if __name__ == '__main__':

  new_run = True

  init_all()

  main()

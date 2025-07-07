#!/usr/bin/env python3
#
# afe_service.py
#
# MIT Haystack Observatory
# Ben Welchman 06-23-2025 -- 07-03-2025
#

# --------------------------
#
# List of Functions:
#
#   open_port                
#   log_telemetry
#   ctrlc
#   print_help
#   parse_command_line
#   reduce
#   xor
#   eval_packet
#   add_cksum
#   write_max
#   request_reg_states
#   main_loop
#
# --------------------------

import os
import time
import socket
import threading
import serial
from datetime import datetime, timezone
import signal
import sys
import numpy as np
import csv

SOCKET_PATH = '/tmp/afe_service.sock'

uart_port = '/dev/ttyGNSS1control' # for MEPs updated after 07-01-2025, otherwise change as needed
uart_baud =  460800 # formerly 921600, changed for GPSD requirements
timeout = 1.0    # increase to 1.5 for desperate debugging
global rate

rate = 60
global last_write
last_write = 0.0
debounce = 1 # increase to 2 for desperate debugging


#For debouncing
def write_uart(msg):
  global last_write
  now = time.monotonic()

  while now - last_write < debounce:
    now = time.monotonic()
    time.sleep(0.01)

  uart.write(msg)
  last_write = now

class Telemetry:
    
    def __init__(self):
        self.telem = []
        self.registers = []
        self.NMEAtime = 0

    def add_telem(self, data):
        self.telem.append(data)

    def add_registers(self, data):
        self.registers = data

    def request_telem(self):

      self.telem.clear()

      uart.readall()

      msg_draft = "$TELEM?*"
      msg = add_cksum(msg_draft)

      write_uart(msg.encode())

      msg_end = False

      while msg_end == False:
        
        line = uart.readline()

        if line:
          lineStr = line.decode()
          print(lineStr)

          try: 
            if (lineStr[1] == 'G') or (lineStr[5] in ('T', 'M', 'H', 'A', 'G')):
              self.add_telem(lineStr)
            line = None
          except:
            line = None

          if (lineStr[0:8] == "$PMITGYR"):
            if "," not in lineStr[9:19]:
              self.NMEAtime = int(lineStr[9:19])
            else:
              self.NMEAtime = int(datetime.now(timezone.utc).timestamp())
            msg_end = True  

    def size(self):
        return len(self.telem)
    
    def print(self):

      self.request_telem()

      request_reg_states()

      return np.stack(self.telem, self.registers)
    
    def log(self):
        
        global new_run

        global path
        
        self.request_telem()

        if len(self.registers) == 0:
          request_reg_states()
        
        t = datetime.fromtimestamp(self.NMEAtime, tz=timezone.utc)
        timestamp = t.strftime("%Y-%m-%d_%H:%M:%S")

        if new_run is True:
          base = "/data/telemetry_log"
          folder_name = f"mep-telemetry-log_{timestamp}"
          path = os.path.join(base, folder_name)
          os.makedirs(path, exist_ok=True)
          new_run = False

        filename = f"telemetry_{timestamp}.csv"

        full_path = os.path.join(path, filename)

        #while

        with open(full_path, "w", newline="", encoding="utf-8") as telem_csv:
          writer = csv.writer(telem_csv)
          for i in range(len(self.telem)):
            try:
              line = self.telem[i].split('*')[0]
              line = line.split(',')
              writer.writerow(line)
            except IndexError as e:
              print("Index Error on: ", line)
            
#          writer.writerow(<tuner>) # ADD TUNER TELEM HERE

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
              state = self.registers[row][column]
              line.append(state)
            
            writer.writerow(line) 
            line = []
        
        print(self.registers)
        print(np.stack(self.telem))
        print("Telemetry logged at: ", filename) # /data/metadata

global_telemetry = Telemetry()

def handle_commands(conn):

  global rate
  
  raw = conn.recv(1024)
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

    array = global_telemetry.print()
    msg = array.tobytes()
    print(msg)                                #    NEED TO FIX
    conn.sendall(b"Placeholder\n")

  elif block == 4:
    
    global_telemetry.log()
    conn.sendall(b"Telemetry logged\n")

  elif block == 5:
    rate = channel
    threading.Timer(rate-2, tick).start()
    str_rate = str(channel)
    print(rate)
    conn.sendall(b"Telemetry log period updated")

  conn.close()

def start_command_server():
  
  try:
    os.remove(SOCKET_PATH)
  except OSError:
    pass
  
  server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  server.bind(SOCKET_PATH)
  server.listen(1)

  while True:
    conn, _ = server.accept()
    threading.Thread(target=handle_commands, args=(conn,), daemon=True).start()

def clear_line():

  count = 0

  while len(uart.readline().decode()) != 0:
    line = uart.readall()
    print("LINE FULL:", line)
    if count > 0:
      uart.close()
      print("soft rebooting")
      main()
    count += 1

def open_port(port, timeout):
  uart = serial.Serial(port, uart_baud, timeout=timeout)
  return uart

def ctrlc(signal, frame):

        sys.exit(0)

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
     verbose_f = False

     if (debug_f):
       print("packet:", packet)
     # endif debug

     dollar_cnt = packet.count("$")
     star_cnt = packet.count("*")

     if (packet[0] == '$'):
       if (dollar_cnt != star_cnt):               # lost data
         if (verbose_f):
           print("err: be", dollar_cnt, star_cnt)   # be = begin end
         # endif extra info
         err_code = -1
       else:
         if (debug_f):
           pass #print("number of sentences in data:",dollar_cnt,"len=",len(packet))
         # endif
     else:                                       # pre-checked, should not happen
       print("data does not begin with '$'", dollar_cnt, star_cnt) 
       err_code = -2
     # end else error

     if (err_code == 0):
       packet = packet.strip("$\n")
       
       nmeadata, ascii_checksum = packet.split("*",1)
       if (debug_f):
           print("nmeadata=", nmeadata)
       # endif

       checksum = None
       if (not gen_cksm_f):              # checksum present, don't generate
         try:
           checksum = int(ascii_checksum,16)
         except Exception as eobj:       # encountered noise instead of checksum
           if (debug_f):
             print("err: ck(1)")             # ck = checksum
           # endif
           err_code = -3
         # end except
       else:
         pass   # no checksum to extract
       # else no checksum 
     # endif not error
      
     if (err_code == 0):
       calculated_checksum = reduce((ord(s) for s in nmeadata), 0)
       if (checksum == calculated_checksum):
         if (debug_f):
           print("success,checksum=",hex(checksum),                                   
                                     hex(calculated_checksum),
                                     ascii_checksum)
         # endif debug
       else:
         if (not gen_cksm_f):   # compare generated vs read
           err_code = -4
           if (debug_f):
             print("err: ck(2)") 
           # endif 
           if (debug_f or verbose_f):
             print("The NMEA cksum != calc cksum:(bin,calc,read)",
                                       hex(checksum),                                   
                                       hex(calculated_checksum),
                                       ascii_checksum)
           # endif debug
         else:
           checksum = calculated_checksum
           if (debug_f):
             print("synthetic checksum=",hex(checksum))
           # endif debug
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

  uart.write(msg.encode())

  print("Writing to MAX: " + msg)

  return

def request_reg_states():

  main_reg = []
  
  tx1_reg = []
  tx2_reg = []

  rx1_reg = []
  rx2_reg = []
  rx3_reg = []
  rx4_reg = []

  uart.readall()

  msg_draft = "$PMITMA?*"
  msg = add_cksum(msg_draft)
  write_uart(msg.encode())

  line = None

  while line is None:
    line = uart.readline()
    line = line.decode()
    for i in range(14, 33, 2):
      main_reg.append(line[i])

  line = None

  msg_draft = "$PMITXT1?*"
  msg = add_cksum(msg_draft)
  uart.write(msg.encode())

  while line is None:
    line = uart.readline()
    line = line.decode()
    for i in range(14, 33, 2):
      tx1_reg.append(line[i])

  line = None

  msg_draft = "$PMITXT2?*"
  msg = add_cksum(msg_draft)
  uart.write(msg.encode())

  while line is None:
    line = uart.readline()
    line = line.decode()
    for i in range(14, 33, 2):
      tx2_reg.append(line[i])

  line = None

  msg_draft = "$PMITXR1?*"
  msg = add_cksum(msg_draft)
  uart.write(msg.encode())

  while line is None:
    line = uart.readline()
    line = line.decode()
    for i in range(14, 33, 2):
      rx1_reg.append(line[i])

  line = None

  msg_draft = "$PMITXR2?*"
  msg = add_cksum(msg_draft)
  uart.write(msg.encode())

  while line is None:
    line = uart.readline()
    line = line.decode()
    for i in range(14, 33, 2):
      rx2_reg.append(line[i])

  line = None
  msg_draft = "$PMITXR3?*"
  msg = add_cksum(msg_draft)
  uart.write(msg.encode())

  while line is None:
    line = uart.readline()
    line = line.decode()
    for i in range(14, 33, 2):
      rx3_reg.append(line[i])

  line = None

  msg_draft = "$PMITXR4?*"
  msg = add_cksum(msg_draft)
  uart.write(msg.encode())

  while line is None:
    line = uart.readline()
    line = line.decode()
    for i in range(14, 33, 2):
      rx4_reg.append(line[i])

  line = None

  reg_list = main_reg, tx1_reg, tx2_reg, rx1_reg, rx2_reg, rx3_reg, rx4_reg
  global_telemetry.add_registers(np.stack(reg_list))

  return

def tick():

  global rate

  print("Periodic log:")
  global_telemetry.log()
  threading.Timer(rate-1, tick).start() #rate offset will need to vary as the script is updated, likely rate -1

def main():

  global rate

  signal.signal(signal.SIGINT, ctrlc) # for control c

  print("Initial log:")
  global_telemetry.log()

  threading.Timer(rate, tick).start()
  
  #while True:

if __name__ == '__main__':

  global new_run

  new_run = True

  try:
    uart = open_port(uart_port, timeout)
    print("UART initialized at", uart_port)
  except:
    print("Failed to initialize UART")
    sys.exit()

  threading.Thread(target=start_command_server, daemon=True).start()

  main()

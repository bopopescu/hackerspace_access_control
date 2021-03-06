#!/bin/python -tt

import sys
from Crypto.Cipher import AES
import argparse
import os
import aes_eax
import threading
import struct

sys.path.insert(0, '../../mrbus_bootloader')
import mrbus


def ItoNetwork(x):
  return [ord(a) for a in struct.pack("<I", x)]
  

def strfrombytes(b):
  s=''
  for bb in b:
    s+=str(chr(bb))
  return s


def intargparse(arg):
  if arg==None:
    return arg
  elif arg.startswith('0x') or arg.startswith('0X'):
    return int(arg[2:], 16)
  else:
    return int(arg)








class RDP(object):
  RDP_SEQ_N=16

  STATE_CLOSED     = 0
  STATE_LISTEN     = 1
  STATE_SYN_SENT   = 2
  STATE_SYN_RCVD   = 3
  STATE_OPEN       = 4
  STATE_CLOSE_WAIT = 5

  PKT_SYN = 0
  PKT_SYNACK = 1
  PKT_ACK = 2
  PKT_RST = 3
  PKT_DATA = 4

  OVERLOADALLOWED = 4

  lock = threading.Lock()
  condition = threading.Condition(lock)
  timerHint = None

  def _time_handler(self):
    with self.lock:
      if self.state==self.STATE_SYN_SENT:
        self.node.sendpkt([self.RDP_SEQ_N-128, self.PKT_SYN])
        self._timer(.3)
      elif self.state==self.STATE_OPEN:
        #if datapktwaiting for ack
          #asdf
        #else:#just a long time since we've seen any data
          pass#send an empty?
      elif self.state==self.STATE_CLOSE_WAIT:
        self.state=self.STATE_CLOSED
        self.run=False

  def _pkt_handler_stub(self, p):
    if p.cmd >= 0x80 and p.cmd < 0x80+self.RDP_SEQ_N+1:
      self._pkt_handler(p)
      return True #eat packet

  def _sendMore(self):
    with self.lock:
      if self.lastSentPacket - self.lastAckedPacket >= self.OVERLOADALLOWED or not self.sendbuf:
       return
      sendlen = min(self.sendbuf, 9)
      pkt=self.sendbuf[:sendlen]
      self.sendbuf=self.sendbuf[sendlen:]
      self.pkts.append(pkt)
      self.lastSentPacket += 1
      self.node.sendpkt([self.RDP_SEQ_N-128, self.PKT_DATA]+ItoNetwork(self.lastSentPacket)+pkt)
      self._timer(2)

  def _pkt_handler(self, p):
    print 'RDP handler:', p
    with self.lock:
      if self.state==self.STATE_SYN_SENT:
        if p == [256+self.RDP_SEQ_N-128, self.PKT_SYNACK]:
          self.node.sendpkt([self.RDP_SEQ_N-128, self.PKT_ACK])
          self.state=self.STATE_OPEN
          self._timer(30)
          self.condition.notify()
        else:
          self._doclose(3)
      elif self.state==self.STATE_OPEN:
        if p.cmd == 256+self.RDP_SEQ_N-128: #control
          if p == [256+self.RDP_SEQ_N-128, self.PKT_SYNACK]:#late synack. maybe our ack was missed
            self.node.sendpkt([self.RDP_SEQ_N-128, self.PKT_ACK])
            self._timer(30)
          else:
            pass
				#data in control packet
				#buf at rxBuffer+6+1+4
				#buf is rxBuffer[MRBUS_PKT_LEN]-6-1-4
				#seq num *(uint32_t*)(rxBuffer+7)
        else: # NORMAL DATA, NOT DONE YET data
          pass
      elif self.state==self.STATE_CLOSE_WAIT:
        self._doclose(1)
      elif self.state==self.STATE_CLOSED:
        self._doclose(2, False)

  def _timer(self, t):
    if self.timerHint:
        self.mrb.removeTimer(self.timerHint)
    self.mrb.installTimer(t, lambda:self._time_handler())

  def _doclose(self, why, setstate=True):
    self.node.sendpkt([self.RDP_SEQ_N-128, self.PKT_RST, why])
    if setstate:
      self.state=self.STATE_CLOSE_WAIT
      self._timer(5)

  def __init__(self, mrb):
    self.mrb=mrb
    self.node = None
    self.state = self.STATE_CLOSED
    self.run=False
    self.thread = None
    self.sendbuf=[]
    self.pkts=[]
    self.lastSentPacket = 0
    self.lastAckedPacket =0 

  def _install(self):
    def runner():
      self.hint=self.node.install(lambda p:self._pkt_handler_stub(p))
      while self.run:
        self.node.pump(1)#this isn't very friendly to multiple RDP connections.  node or mrb should be able to spawn a pump thread.
      self.node.remove(self.hint)
      if self.timerHint:
        self.mrb.removeTimer(timerHint)
    self.thread = threading.Thread(group=None, target=runner, name='RDPPump')
    self.thread.start()

  def __del__(self):
    if self.thread:
      self.close()

  def close(self):
    with self.lock:
      if self.state not in [self.STATE_CLOSE_WAIT, self.STATE_CLOSED]:
        self._doclose(0)
    self.thread.join()
    self.node = None
    self.state = self.STATE_CLOSED
    self.thread = None
    self.sendbuf=[]
   
  def open(self, addr):
    with self.lock:
      assert self.state in [self.STATE_LISTEN, self.STATE_CLOSED]
      self.node = self.mrb.getnode(addr)
      self.run=True
      self._install()
      self.sendbuf=[]

      self.node.sendpkt([self.RDP_SEQ_N-128, self.PKT_SYN])
      self.state=self.STATE_SYN_SENT
      self._timer(.3)
      self.condition.wait(3)
      if self.state != self.STATE_OPEN:
        self._doclose(5)
        raise IOError("open timed out")


  def send(self, channel, data):
    assert channel < 16
    assert len(data) <= 0xfff
    with self.lock:
      self.sendbuf += [len(data)&0xff, (channel<<4) | (len(data)>>8)] + data
    self._sendMore()

if __name__ == '__main__':
  key='yourkeygoeshere\x00'
  parser = argparse.ArgumentParser(description='nfc door term prog')
  parser.add_argument('-p', '--port', type=str,help='port for mrbus CI2 interface. Will guess /dev/ttyUSB? if not specified')
  parser.add_argument('-a', '--addr-host', help='mrbus address to use for host.  Will scan for an unused address if not specified')
  parser.add_argument('-d', '--addr', default=None, help='mrbus address of node to program.  Will scan for a singular NfcDoor node if not specified')
  args = parser.parse_args()

  args.addr_host = intargparse(args.addr_host)
  args.addr = intargparse(args.addr)


  if args.port == None:
    args.port = [d for d in os.listdir('/dev/') if d.startswith('ttyUSB')]
    if len(args.port) == 0:
      print 'no port specified, and can\'t find a default one'
      sys.exit(1)
    elif len(args.port) > 1:
      print 'no port specified, and there is more than one to guess from.  giving up.'
      sys.exit(1)
    args.port='/dev/'+args.port[0]
  
  mrb = mrbus.mrbus(args.port, addr=args.addr_host, logall=True, logfile=sys.stdout, extra=True)

  def debughandler(p):
    if p.cmd==ord('*'):
      print 'debug:', p
      return True #eat packet
    return False #dont eat packet
  mrb.install(debughandler, 0)


  if args.addr == None:
    nodes = mrb.scannodes(pkttype='V')
    nodes = [n.src for n in nodes if ''.join(map(chr, n.data[:7]))=='NfcDoor']
    if len(nodes) == 0:
      print 'no node found'
      sys.exit(1)
    if len(nodes) > 1:
      print 'found more than one node found. specify an address.'
      sys.exit(1)
    args.addr = nodes[0]
    print 'found node @', args.addr

  r=RDP(mrb)
  r.open(args.addr)
  try:
    r.open(args.addr)
#    r.putScreen('hi')

    while 1:
      pass
  except:
    pass
  dt.stop()

#  print node.cmp.isSupported(timeout=200)



#############################
#AES TEST
#  t=[1]
#  enc = AES.new(key, AES.MODE_CBC, '\x00'*16)
#  print map(ord, enc.encrypt(strfrombytes(t + [0]*(16-len(t)))))

#  node.pumpout()
#  node.sendpkt(['Z']+t)
#  #print node.getfilteredpkt(lambda p: p.cmd==ord('z')).data
#  print node.gettypefilteredpktdata('z')
#############################


#############################
#CMAC TEST
#  t=283945720348972302934857
#  tag = aes_eax.OMAC(aes_eax.intfromstr(key), t, 14)
#  print hex(tag)

#  node.pumpout()
#  node.sendpkt(['1']+[s for s in aes_eax.strfromint(t,14)])
#  r = node.gettypefilteredpktdata('2')
#  print map(hex, r)
#############################


#############################
#EAX enc TEST
#
#  nonce=[1]
#  nl=len(nonce)
#  head=[1,2]
#  hl=len(head)
#  data=[2,3,4]
#  dl=len(data)
#
#  assert nl<16 and hl < 16 and dl < 16 and nl+hl+dl <= 13
#
#  print hex(aes_eax.intfrombytes(data))
#
#  node.pumpout()
#  node.sendpkt(['3']+[(hl<<4)|nl]+nonce+head+data)
#  r = node.gettypefilteredpktdata('4')
#  print map(hex, r)
#  print hex(aes_eax.aead_eax_aes_dec(aes_eax.intfromstr(key), aes_eax.intfrombytes(nonce), nl, aes_eax.intfrombytes(head), hl, aes_eax.intfrombytes(r), len(r), 3))
#
#############################






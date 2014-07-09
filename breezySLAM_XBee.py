#!/usr/bin/env python

# This file is the python base station code 
# for use with the Aerospace Robotics SLAM Rover project. 
# Do whatever you want with this code as long as it doesn't kill people.
# No liability et cetera.
# http://www.aerospacerobotics.com/             June-August 2014
#                     Michael Searing & Bill Warner


# from breezyslam.algorithms import CoreSLAM
from breezyslam.algorithms import RMHC_SLAM
from breezyslam.components import Laser
from breezyslam.robots import WheeledRobot

import sys
print "Python %s" % '.'.join([str(el) for el in sys.version_info[0:3]])
if sys.version_info[0] < 3: # 2.x
    import Tkinter as tk
else: # 3.x
    import tkinter as tk
    def raw_input(inStr): return input(inStr)
from tkMessageBox import askokcancel

import time # wait for robot to do things that take time
import threading # allow serial checking to happen on top of tkinter interface things
import Queue # deal with sending data across threads

import serial # bind hardware serial
from serial.tools import list_ports # get computer's port info
import struct # parse incoming serial data
import numpy as np # create map array
from scipy.ndimage.interpolation import rotate

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2TkAgg
import matplotlib.pyplot as plt

# note that Data.saveImage() imports PIL and subprocess for map image saving and viewing


PKT_LEN = 4 # length of scan data packet
ENC_LEN = 8 # length of encoder data packet
ENC_FLAG = '\xFE' # response indicator (&thorn)
SCN_FLAG = '\xFF' # end line terminator (&yuml)
DFAC = 0.5; # distance resolution factor [1/mm]
AFAC = 8.0; # angle resolution factor [1/deg]
MASK = 0b0000111111111111
XBEE_BAUD = 250000

# "sizes" all refer to half of the image (radius)
viewSize = 2 # default size of region to be shown in display [m]
roomSize = 8 # size of region to be mapped [m]
rad = roomSize*1000 # size of data matrix [mm]
mapRes = 100 # number of pixels of data per meter [pix/m]
mapSize = roomSize*mapRes # number of pixels on each side of the origin
mm2pix = mapRes/1000.0 # [pix/mm]
deg2rad = np.pi/180.0
maxVal = 10 # depth of data points on map (higher=more sure that it exists)
robotVal = maxVal + 1 # value of robot in map storage
numSamp = 400 # desired number of valid points per scan
# updateHz = 1980points/sec * scan/400points

commandRate = 100 # minimum time between auto-send commands [ms]
dataRate = 50 # minimum time between updating data from lidar [ms]
mapRate = 500 # minimum time between updating map [ms]

# distance and angle conversions into encoder ticks (wheel revolution != robot rotation)
ticksPerRev = 1000.0/3
wheelDia = 58.2 # diameter [mm]
mmPerRev = np.pi*wheelDia # circumference [mm]
mm2ticks = ticksPerRev/mmPerRev # [ticks/mm]
ticks2mm = mmPerRev/ticksPerRev # [mm/tick]

ANGULAR_FLUX = 1.62;
wheelTrack = 185 # separation of wheels [mm]
mmPerRot = np.pi*wheelTrack # circumference of wheel turning circle [mm]
degPerRot = 360 # [deg]
degPerRev = degPerRot*(mmPerRev/mmPerRot) # degrees of rotation per wheel revolution [deg]
deg2ticks = ticksPerRev/degPerRev * ANGULAR_FLUX # [ticks/deg]
ticks2deg = degPerRev/ticksPerRev * 1.0/ANGULAR_FLUX # [deg/tick]


print "Each pixel is",round(1000.0/mapRes,1),"mm, or",round(1000.0/mapRes/25.4,2),"in"

def float2int(x):
  return int(0.5 + x)

class Root(tk.Tk): # Tkinter window, inheriting from Tkinter module
  # init draws and displays window, creates data matrix, and calls updateData and updateMap
  # updateData calls getScanData, and updates the slam object and data matrix with this data, and loops to itself
  # updateMap draws a new map, using whatever data is available, and loops to itself
  # getScanData pulls LIDAR data directly from the serial port and does preliminary processing
  # resetAll recreates slam object to remove previous data it and wipes current data matrix

  def __init__(self):
    tk.Tk.__init__(self) # explicitly initialize base class and create window
    self.geometry('+100+100') # position windows 100,100 pixels from top-left corner
    self.resetting = False

    # newWindow = tk.Toplevel()

    self.serQueue = Queue.Queue() # FIFO queue by default
    self.numLost = tk.StringVar() # status of serThread (should be a queue...)
    self.serThread = SerialThread(self.serQueue, self.numLost) # initialize thread object

    self.initUI() # create all the pretty stuff in the Tkinter window
    self.restartAll(rootInit=True)

  def initUI(self):
    self.wm_title("Mapping LIDAR") # name window
    self.fig = plt.figure(figsize=(8, 5), dpi=131) # create matplotlib figure
    self.ax = self.fig.add_subplot(111) # add plot to figure
    self.ax.set_title("RPLIDAR Plotting") # name and label plot
    self.ax.set_xlabel("X Position [mm]")
    self.ax.set_ylabel("Y Position [mm]")

    cmap = plt.get_cmap("binary")
    # cmap = plt.get_cmap("jet")
    cmap.set_over("red") # robot is set to higher than maxVal
    dummyInitMat = np.zeros((2,2), dtype=int)
    self.myImg = self.ax.imshow(dummyInitMat, interpolation="none", cmap=cmap, vmin=0, vmax=maxVal, # plot data
              extent=[-rad, rad, -rad, rad]) # extent sets labels by matching limits to edges of matrix
    self.ax.set_xlim(-viewSize*1000,viewSize*1000) # pre-zoom image to defined default viewSize
    self.ax.set_ylim(-viewSize*1000,viewSize*1000)
    self.cbar = self.fig.colorbar(self.myImg, orientation="vertical") # create colorbar

    # turn figure data into matplotlib draggable canvas
    self.canvas = FigureCanvasTkAgg(self.fig, master=self) # tkinter interrupt function
    self.canvas.draw()
    self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=1) # put figure at top of window

    # add matplotlib toolbar for easy navigation around map
    NavigationToolbar2TkAgg(self.canvas, self).update() # tkinter interrupt function
    self.canvas._tkcanvas.pack(side="top", fill=tk.BOTH, expand=1) # actually goes on bottom...not sure why

    # bind keyboard inputs to functions
    self.bind('<Escape>', lambda event: self.closeWin()) # tkinter interrupt function
    self.bind('r', lambda event: self.restartAll()) # tkinter interrupt function
    self.bind('<Return>', lambda event: self.sendCommand(loop=False)) # tkinter interrupt function

    # create buttons
    tk.Button(self, text="Quit (esc)", command=self.closeWin).pack(side="left", padx=5, pady=5) # tkinter interrupt function
    tk.Button(self, text="Restart (r)", command=self.restartAll).pack(side=tk.LEFT, padx = 5) # tkinter interrupt function
    tk.Label(self, text="Command: ").pack(side="left")
    self.entryBox = tk.Entry(master=self)
    self.entryBox.pack(side="left", padx = 5)
    tk.Button(self, text="Send (enter)", command=lambda: self.sendCommand(loop=False)).pack(side=tk.LEFT, padx=5) # tkinter interrupt function
    tk.Label(self, textvariable=self.numLost).pack(side="left", padx=5, pady=5)
    tk.Button(self, text="Save Map", command=self.saveImage).pack(side=tk.LEFT, padx=5) # tkinter interrupt function

  def restartAll(self, rootInit=False):
    if not rootInit: # reset called during program
      self.resetting = True # stop currently running loops
      self.after(2000, lambda: self.restartAll_2(rootInit)) # let other tkinter things run
    else: # don't need to stop currently running loops, so go right to second half of restart
      self.restartAll_2(rootInit)

  def restartAll_2(self, rootInit):
    if not rootInit: # reset called during program
      self.resetting = False

    self.data = Data()
    self.slam = Slam()
    self.updateData(init=True) # pull data from queue, put into data matrix
    self.updateMap() # draw new data matrix
    self.sendCommand(loop=True) # check for user input and automatically send it

  def closeWin(self):
    if askokcancel("Quit?", "Are you sure you want to quit?"):
      self.serThread.stop() # tell serial thread to stop running
      self.quit() # kills interpreter (necessary for some reason)
      # self.destroy() # ends this instance of a tk object (not sufficient...?)

  def saveImage(self):
    self.data.saveImage()

  def sendCommand(self, loop=True, resendCount=0): # loop indicates how function is called: auto (True) or manual (False)
    # first, figure out which string to send # note that ACK-checking only applies to value-setting commands in 'vwasd'
    if self.serThread.cmdSent and not self.serThread.cmdRcvd and resendCount <= 2: # expecting ACK, but not received yet, and not resent 3 times
      resend = True
      strIn = self.serThread.sentCmd # get previous command

    else: # not resending last command
      if (self.serThread.cmdSent and self.serThread.cmdRcvd) or resendCount >= 3: # ACK received, or command resent too many times (failed)
        self.serThread.cmdSent = False # reset state values
        self.serThread.cmdRcvd = False
        resendCount = 0
      resend = False
      strIn = self.entryBox.get() # get new command

    # then, send it in the proper manner corresponding to the command
    if strIn: # string is not empty
      if not loop: # manual-send mode of function
        self.serThread.writeCmd(strIn)
        self.entryBox.delete(0,"end") # clear box after manual-send

      elif strIn in 'WASD': # auto-send continuous drive commands (capitalized normal commands)
        self.serThread.writeCmd(strIn)

      elif resend: # auto-resend command until ACK received
        resendCount += 1 # keep track of how many times we're resending command
        self.serThread.writeCmd(strIn)

      else:
        pass # wait until manual-send for other commands

    if loop and not self.resetting: self.after(commandRate, lambda: self.sendCommand(resendCount=resendCount)) # tkinter interrupt function

  def updateData(self, init=False):
    if self.serQueue.qsize() > numSamp or init:
      self.getScanData() # pull LIDAR data via serial from robot
      if init: self.slam.prevEncPos = self.slam.currEncPos # set both values the first time through

      self.data.robot_mm = self.slam.updateSlam(self.data.dists, self.data.angs) # send data to slam to do stuff
      if init: self.data.robot_mm0 = self.data.robot_mm # set initial position during initialization

      self.data.drawPoints() # draw points, using updated slam, on the data matrix

    if not self.resetting: self.after(dataRate, self.updateData) # tkinter interrupt function

  def updateMap(self):
    self.myImg.set_data(self.data.matrix) # 15ms
    self.canvas.draw() #400ms

    if not self.resetting: self.after(mapRate, lambda: self.updateMap()) # tkinter interrupt function

  def getScanData(self): # 50ms
    i = 0
    while i < numSamp:
      queueItem = self.serQueue.get()
      if isinstance(queueItem[0], float):
        self.data.dists[i], self.data.angs[i] = queueItem
      elif isinstance(queueItem[1], int):
        self.slam.currEncPos = queueItem
      else:
        print "what"
      i += 1
    

# class Slam(CoreSLAM):
class Slam(RMHC_SLAM):
  # updateSlam takes LIDAR data and uses BreezySLAM to calculate the robot's new position
  # getVelocities takes the encoder data (lWheel, rWheel, timeStamp) and finds change in translational and angular position and in time

  def __init__(self):
    self.scanLen = 361 # number of points per scan to be passed into BreezySLAM
    # CoreSLAM.__init__(self, Laser(self.scanLen, 6, 0, +360, 7.0, 0, -0.02), 2*mapSize, 1000/mapRes)
    RMHC_SLAM.__init__(self, Laser(self.scanLen, 6, 0, +360, 7000, 0, -35), 2*mapSize, 1000/mapRes, random_seed=0xabcd)
    self.prevEncPos = () # robot encoder data
    self.currEncPos = () # left wheel [ticks], right wheel [ticks], timestamp [ms]

  def updateSlam(self, dists, angs): # 15ms
    distVec = [0.0 for i in range(self.scanLen)]
    velocities = None # odometry data, passed in from robot's encoders

    for i in range(numSamp): # create breezySLAM-compatible data from raw scan data
      index = float2int(angs[i])
      if not 0 <= index <= 360: continue
      distVec[index] = dists[i] if distVec[index] == 0 else (dists[i]+distVec[index])/2

    # note that breezySLAM switches the x- and y- axes (their x is forward, 0deg; y is right, +90deg)
    print distVec
    self.update(distVec, self.getVelocities()) # update slam information using particle filtering on LIDAR scan data // 10ms
    x, y, theta = self.getpos()
    return (y, x, theta)

  def getVelocities(self):
    dLeft, dRight, dt = [curr - prev for (curr,prev) in zip(self.currEncPos, self.prevEncPos)]
    self.prevEncPos = self.currEncPos

    # overflow correction:
    if dLeft > 2**15: dLeft -= 2**16-1 # signed short
    elif dLeft < -2**15: dLeft += 2**16-1

    if dRight > 2**15: dRight -= 2**16-1 # signed short
    elif dRight < -2**15: dRight += 2**16-1

    if dt < -2**15: dt += 2**16-1 # unsigned short # time always increases, so only check positive overflow

    dxy = ticks2mm * (dLeft + dRight)/2 # forward change in position
    dtheta = ticks2deg * (dLeft - dRight)/2 # positive theta is clockwise

    # print dxy/1000, dtheta, dt/1000.0
    return dxy, dtheta, dt/1000.0 # [mm], [deg], [s]


class Data():
  # init creates data matrix and information vectors for processing
  # drawPoints adds scan data to data matrix
  # drawRobot adds robot position to data matrix, generally using slam to find this position
  # saveImage uses PIL to write an image file from the data matrix

  def __init__(self):
    self.matrix = np.zeros((2*mapSize+1, 2*mapSize+1), dtype=int) # initialize data matrix
    self.dists = [0.0 for ind in range(numSamp)] # current scan data
    self.angs = [0.0 for ind in range(numSamp)]
    self.robot_mm0 = () # robot location data
    self.robot_mm = () # x [mm], y [mm], th [deg], defined from lower-left corner of map
    self.robot_pix = () # x, y [pix]

  def drawPoints(self): # 7ms
    xRobot_mm = (self.robot_mm[0] - self.robot_mm0[0]) # displacement from start position (center of map)
    yRobot_mm = (self.robot_mm[1] - self.robot_mm0[1])
    Tdeg = self.robot_mm[2] - self.robot_mm0[2]

    xRobot_pix = mapSize + xRobot_mm*mm2pix # robot wrt map center (mO) + mO wrt matrix origin (xO)  = robot wrt xO [pix]
    yRobot_pix = mapSize - yRobot_mm*mm2pix # negative because np array rows increase downwards (+y_mm = -y_pix = -np rows)
    self.robot_pix = (float2int(xRobot_pix), float2int(yRobot_pix))
    self.drawRobot(1) # new robot position

    for i in range(numSamp):
      x_pix = float2int( mapSize + ( xRobot_mm + self.dists[i]*np.sin((self.angs[i]+Tdeg)*deg2rad) )*mm2pix ) # pixel location of scan point
      y_pix = float2int( mapSize - ( yRobot_mm + self.dists[i]*np.cos((self.angs[i]+Tdeg)*deg2rad) )*mm2pix ) # point wrt robot + robot wrt xO
      try:
        self.matrix[y_pix, x_pix] += self.matrix[y_pix, x_pix] < maxVal # increment value at location if below maximum value
      except IndexError:
        # pass
        print("scan out of bounds")

  def drawRobot(self, size):
    xLoc = self.robot_pix[0]
    yLoc = self.robot_pix[1]
    th = self.robot_mm[2] - self.robot_mm0[2]

    robotMat = np.array([[0,0,1,0,0], # shape of robot on map
                         [0,0,1,0,0],
                         [0,0,1,0,0],
                         [0,1,1,1,0],
                         [0,1,1,1,0],
                         [1,1,1,1,1],
                         [1,1,1,1,1]])
    robotMat = rotate(robotMat, -th, output=int)

    rShape = robotMat.shape # rows, columns
    hgt = (rShape[0]-1)/2 # indices of center of robot
    wid = (rShape[1]-1)/2
    for x in range(-wid, wid+1): # relative to center of robot
      for y in range(-hgt, hgt+1):
        if robotMat[y+hgt, x+wid] == 1: # robot should exist here
          try:
            self.matrix[yLoc+y, xLoc+x] = robotVal
          except IndexError:
            # pass
            print("robot out of bounds")

  def saveImage(self):
    from PIL import Image # don't have PIL? sorry (try pypng)

    imgData = maxVal - self.matrix # invert colors to make 0 black and maxVal white
    robot = imgData < 0 # save location of robot
    imgData[:] = np.where(robot, 0, imgData) # make robot path black
    GB = (255.0/maxVal*imgData).astype(np.uint8) # scale map data and assign to red, green, and blue layers
    R = GB + (255*robot).astype(np.uint8) # add robot path to red layer

    im = Image.fromarray(np.dstack((R,GB,GB))) # create image from depth stack of three layers
    filename = str(mapRes)+"_pixels_per_meter.png" # filename is map resolution
    im.save(filename) # save image
    print("Image saved to " + filename)

    import subprocess
    subprocess.call(["eog", filename]) # open with eye of gnome


class SerialThread(threading.Thread):
  def __init__(self, serQueue, numLost):
    super(SerialThread, self).__init__() # nicer way to initialize base class (only works with new-style classes)
    self.serQueue = serQueue
    self.numLost = numLost
    self._stop = threading.Event() # create flag
    self.cmdSent = False
    self.cmdRcvd = False

    self.connectToPort() # initialize serial connection with XBee
    self.talkToXBee() # optional (see function)
    self.waitForResponse() # start LIDAR and make sure Arduino is sending stuff back to us

    self.start() # put serial data into queue

  def connectToPort(self):
    # first select a port to use
    portList = sorted([port[0] for port in list_ports.comports() if 'USB' in port[0] or 'COM' in port[0]])
    if not portList: # list empty
      sys.exit("Check your COM ports for connected devices and compatible drivers.")
    elif len(portList) == 1: # if there's only one device connected, use it
      portName = portList[0]
    else: # query user for which port to use
      print("Available COM ports:")
      print(portList)
      portName = portList[0][0:-1] + raw_input("Please select a port number [%s]: " % ', '.join([str(el[-1]) for el in portList]))

    # then try to connect to it
    try:
      self.ser = serial.Serial(portName, baudrate=XBEE_BAUD)
    except serial.serialutil.SerialException:
      sys.exit("Invalid port.")
    else:
      time.sleep(1) # give time to connect to serial port
      print("Connection successful using %s" % portName)

  def talkToXBee(self): # allow direct interaction with XBee (Arduino code needs modification)
    if raw_input("Configure this (Arduino's) XBee before mapping? [y/N]: ") == 'y':
      while True:
        inputStr = raw_input("Enter XBee command: ")
        sendStr = inputStr if (inputStr == "+++" or inputStr == '') else inputStr + '\x0D'
        self.ser.write(sendStr)
        tstart = time.clock()
        while time.clock() < tstart + 1: # give XBee 1 sec to respond
          if self.ser.inWaiting(): print self.ser.read()
        if inputStr.lower() == "atcn": # we've told the XBee to exit command mode, so we should, too...
          self.ser.write('q') # tells Arduino to stop talking to XBee (shouldn't affect computer XBee...)
          break
      sys.exit("Please restart XBee and then re-run this code.") # XBee has to power cycle for change to take effect

  def waitForResponse(self):
    tryCount = 0
    while True:
      self.ser.write('l') # tell robot to start lidar
      print("Start command sent to LIDAR... waiting for response...")
      time.sleep(2)

      if self.ser.inWaiting(): break # until it sends something back

      tryCount += 1
      if tryCount >= 5: sys.exit("No response received from Arduino.")

    print("Data received... live data processing commencing...")

  def stop(self, try1 = True):
    if try1:
      self._stop.set() # set stop flag to True
      time.sleep(0.2) # give serial reading loop time to finish current point before flushInput()
      # prevents: SerialException: device reports readiness to read but returned no data (device disconnected?)
    self.ser.write('o') # tell robot to turn lidar off
    self.ser.flushInput() # empty input serial buffer
    time.sleep(0.5) # give time to see if data is still coming in
    if self.ser.inWaiting(): self.stop(try1=False)

  def writeCmd(self, outByte):
    command = outByte[0]
    if command in 'vwasd': # we're giving the robot a value for a command
      self.ser.write(command) # send the command
      try:
        num = float(outByte[1:])

      except IndexError:
        if command == 'v': print("Invalid command. Enter speed for speed set command.")
        else: print("Invalid command. Enter distance/angle for movement commands.")

      except ValueError:
        print("Invalid command. Enter a number after command.")

      else:
        self.cmdSent = True
        self.sentCmd = outByte
        if command == 'v': self.ser.write(str(num)) # send motor speed as given
        elif command in 'ws': self.ser.write(str(int(mm2ticks*num))) # convert millimeters to ticks
        elif command in 'ad': self.ser.write(str(int(deg2ticks*num))) # convert degrees to ticks

    else:
      self.ser.write(command) # otherwise send only first character

  def run(self):
    tstart = time.clock()
    lagged, missed, total = 0,0,0
    while not self._stop.isSet(): # pulls and processes all incoming serial data
      if self.ser.inWaiting() > 300*PKT_LEN: # data in buffer is too old to be useful (300 packets old)
        lagged += self.ser.inWaiting()/PKT_LEN
        self.ser.flushInput()

      # in order sent (bytes comma-separated):                  dist[0:7], dist[8:12] ang[0:3], ang[4:12]
      # in order received (future data chunks comma-separated): dist[0:12]            ang[0:3], ang[4:12]
      pointLine = self.ser.read(PKT_LEN) # distance and angle (blocking)
      # pointLine = '\x7D\x00\xB4' # 250mm distance and 360deg angle

      if pointLine == ENC_FLAG*PKT_LEN:
        self.cmdRcvd = True # ACK from Arduino
        continue # move to the next point

      if pointLine[0:2] == ENC_FLAG*2:
        pointLine += self.ser.read(ENC_LEN-PKT_LEN) # read more bytes to complete longer packet
        self.serQueue.put(struct.unpack('<hhH',pointLine[2:ENC_LEN+1])) # little-endian 2 signed shorts, 1 unsigned short
        continue

      bytes12, byte3 = struct.unpack('<HB',pointLine[0:PKT_LEN-1]) # little-endian 2 bytes and 1 byte
      distCurr = (bytes12 & MASK)/DFAC # 12 least-significant (sent first) bytes12 bits
      angleCurr = ((bytes12 & ~MASK) >> 12 | byte3 << 4)/AFAC # 4 most-significant (sent last) bytes2 bits, 8 byte1 bits

      if pointLine[PKT_LEN-1] == SCN_FLAG and 100 < distCurr < 6000 and angleCurr <= 360: # data matches what was transmitted
        total += 1
        self.serQueue.put((distCurr, angleCurr))

      else: # invalid point received (communication error)
        missed += 1
        while self.ser.read(1) != SCN_FLAG: pass # delete current packet up to and including SCN_FLAG byte

      if time.clock() > tstart + 1.0: # 1 second later
        tstart += 1.0
        self.numLost.set("Last sec: "+str(lagged)+" lagged, "+str(missed)+" errors out of "+str(total)+" points")
        lagged, missed, total = 0,0,0


if __name__ == '__main__':
  root = Root() # create Tkinter window, containing entire App
  root.protocol("WM_DELETE_WINDOW", root.closeWin)
  # print "window desired:",root.winfo_reqwidth(),root.winfo_reqheight() # get desired size of window
  # print "window actual:",root.winfo_width(),root.winfo_height() # get actual size of window
  root.mainloop() # start Tkinter loop

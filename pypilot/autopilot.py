#!/usr/bin/env python
#
#   Copyright (C) 2021 Sean D'Epagnier
#
# This Program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation; either
# version 3 of the License, or (at your option) any later version.  

# autopilot base handles reading from the imu (boatimu)

import sys, os, math, time
print('autopilot start', time.monotonic())

pypilot_dir = os.getenv('HOME') + '/.pypilot/'
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from server import pypilotServer
from client import pypilotClient
from values import *
from boatimu import *
from resolv import *
import tacking, servo
from version import strversion
from sensors import Sensors
import pilots

def minmax(value, r):
    return min(max(value, -r), r)

def compute_true_wind(gps_speed, wind_speed, wind_direction):
    rd = math.radians(wind_direction)
    windv = wind_speed*math.sin(rd), wind_speed*math.cos(rd)
    truewind = math.degrees(math.atan2(windv[0], windv[1] - gps_speed))
    #print 'truewind', truewind
    return truewind

class ModeProperty(EnumProperty):
    def __init__(self, name):
        self.ap = False
        super(ModeProperty, self).__init__(name, 'compass', ['compass', 'gps', 'wind', 'true wind'], persistent=True)

    def set(self, value):
        # update the preferred mode when the mode changes from user
        if self.ap:
            self.ap.preferred_mode.update(value)
        self.set_internal(value)

    def set_internal(self, value):
        super(ModeProperty, self).set(value)

class HeadingOffset(object):
    def __init__(self):
        self.value = 0

    def update(self, offset, d):
        offset = resolv(offset, self.value)
        self.value = resolv(d*offset + (1-d)*self.value)

class HeadingProperty(RangeProperty):
    def __init__(self, name, mode):
        self.mode = mode
        super(HeadingProperty, self).__init__(name, 0, -180, 360)

    # +-180 for wind modes 0-360 for compass and gps modes
    def set(self, value):
        value = resolv(value, 0 if 'wind' in self.mode.value else 180)
        super(HeadingProperty, self).set(value)

class TimeStamp(SensorValue):
    def __init__(self):
        super(TimeStamp, self).__init__('timestamp', 0)
        self.info['type'] = 'TimeStamp' # not a sensor value to be scoped

class TimedQueue(object):
  def __init__(self, length):
    self.data = []
    self.length = length

  def add(self, data):
    t = time.monotonic()
    while self.data and self.data[0][1] < t-self.length:
      self.data = self.data[1:]
    self.data.append((data, t))

  def take(self, t):
    while self.data and self.data[0][1] < t:
        self.data = self.data[1:]
    if self.data:
      return self.data[0][0]
    return 0
        
class Autopilot(object):
    def __init__(self):
        super(Autopilot, self).__init__()
        self.watchdog_device = False

        self.server = pypilotServer()
        self.client = pypilotClient(self.server)
        self.boatimu = BoatIMU(self.client)
        self.sensors = Sensors(self.client, self.boatimu)
        self.servo = servo.Servo(self.client, self.sensors)
        self.version = self.register(Value, 'version', 'pypilot' + ' ' + strversion)
        self.timestamp = self.client.register(TimeStamp())
        self.starttime = time.monotonic()
        self.mode = self.register(ModeProperty, 'mode')

        self.preferred_mode = self.register(Value, 'preferred_mode', 'compass')
        self.lastmode = False    
        self.mode.ap = self
        self.dt = 0
        
        self.heading_command = self.register(HeadingProperty, 'heading_command', self.mode)
        self.enabled = self.register(BooleanProperty, 'enabled', False)
        self.lastenabled = False
        
        self.last_heading = False
        self.last_heading_off = self.boatimu.heading_off.value

        self.heading = self.register(SensorValue, 'heading', directional=True)
        self.heading_error = self.register(SensorValue, 'heading_error')
        self.heading_error_int = self.register(SensorValue, 'heading_error_int')
        self.heading_error_int_time = time.monotonic()

        # track heading command changes
        self.heading_command_rate = self.register(SensorValue, 'heading_command_rate')
        self.heading_command_rate.time = 0
        #self.servocommand_queue = TimedQueue(10) # remember at most 10 seconds
        
        self.pilots = {}
        for pilot_type in pilots.default:
            #try:
                pilot = pilot_type(self)
                self.pilots[pilot.name] = pilot
            #except Exception as e:
            #    print(_('failed to load pilot'), pilot_type, e)

        pilot_names = list(self.pilots)
        print(_('Available Pilots') + ':', pilot_names)
        self.pilot = self.register(EnumProperty, 'pilot', 'basic', pilot_names, persistent=True)

        self.tack = tacking.Tack(self)

        self.gps_compass_offset = HeadingOffset()
        self.gps_speed = 0

        self.wind_compass_offset = HeadingOffset()
        self.true_wind_compass_offset = HeadingOffset()

        self.wind_direction = self.register(SensorValue, 'wind_direction', directional=True)
        self.wind_speed = 0

        self.runtime = self.register(TimeValue, 'runtime') #, persistent=True)
        self.timings = self.register(SensorValue, 'timings', False)
        self.last_heading_mode = False

        device = '/dev/watchdog0'
        try:
            self.watchdog_device = open(device, 'w')
        except:
            print(_('warning: failed to open special file'), device, _('for writing'))
            print('         ' + _('cannot stroke the watchdog'))

        self.server.poll() # setup process before we switch main process to realtime
        if os.system('sudo chrt -pf 1 %d 2>&1 > /dev/null' % os.getpid()):
            print(_('warning: failed to make autopilot process realtime'))
    
        self.lasttime = time.monotonic()

        # setup all processes to exit on any signal
        self.childprocesses = [self.boatimu.imu, self.boatimu.auto_cal,
                               self.sensors.nmea, self.sensors.gpsd,
                               self.sensors.signalk, self.server]
        def cleanup(signal_number, frame=None):
            #print('got signal', signal_number, 'cleaning up')
            if signal_number == signal.SIGCHLD:
                pid = os.waitpid(-1, os.WNOHANG)
                #print('sigchld waitpid', pid)

            if signal_number != 'atexit': # don't get this signal again
                signal.signal(signal_number, signal.SIG_IGN)

            while self.childprocesses:
                process = self.childprocesses.pop().process
                if process:
                    pid = process.pid
                    #print('kill', pid, process)
                    try:
                        os.kill(pid, signal.SIGTERM) # get backtrace
                    except Exception as e:
                        pass
                        #print('kill failed', e)
            sys.stdout.flush()
            if signal_number != 'atexit':
                raise KeyboardInterrupt # to get backtrace on all processes

        # unfortunately we occasionally get this signal,
        # some sort of timing issue where python doesn't realize the pipe
        # is broken yet, so doesn't raise an exception
        def printpipewarning(signal_number, frame):
            print('got SIGPIPE, ignoring')

        import signal
        for s in range(1, 16):
            if s == 13:
                signal.signal(s, printpipewarning)
            elif s != 9:
                signal.signal(s, cleanup)

        signal.signal(signal.SIGCHLD, cleanup)
        import atexit
        atexit.register(lambda : cleanup('atexit'))
    
    def __del__(self):
        print('closing autopilot')
        self.server.__del__()

        if self.watchdog_device:
            print('close watchdog')
            self.watchdog_device.write('V')
            self.watchdog_device.close()

    def register(self, _type, name, *args, **kwargs):
        return self.client.register(_type(*(['ap.' + name] + list(args)), **kwargs))

    def adjust_mode(self, pilot):
        # if the mode must change
        newmode = pilot.best_mode(self.preferred_mode.value)
        if self.mode.value != newmode:
            self.mode.set_internal(newmode)

    def compute_offsets(self):
        # compute difference between compass to gps and compass to wind
        compass = self.boatimu.SensorValues['heading_lowpass'].value
        if self.sensors.gps.source.value != 'none':
            d = .002
            gps_speed = self.sensors.gps.speed.value
            self.gps_speed = (1-d)*self.gps_speed + d*gps_speed
            if gps_speed > 1: # don't update gps offset below 1 knot
                gps_track  = self.sensors.gps.track.value
                # weight gps compass offset higher with more gps speed
                d = .005*math.log(self.gps_speed + 1)
                self.gps_compass_offset.update(gps_track - compass, d)

        if self.sensors.wind.source.value != 'none':
            d = .005
            wind_speed = self.sensors.wind.speed.value
            self.wind_speed = (1-d)*self.wind_speed + d*wind_speed
            # weight wind direction more with higher wind speed
            d = .05*math.log(wind_speed/5.0 + 1.2)
            wind_direction = resolv(self.sensors.wind.direction.value, self.wind_direction.value)
            wind_direction = (1-d)*self.wind_direction.value + d*wind_direction
            self.wind_direction.set(resolv(wind_direction))
            self.wind_compass_offset.update(wind_direction + compass, d)

            if self.sensors.gps.source.value != 'none':
                true_wind = compute_true_wind(self.gps_speed, self.wind_speed,
                                              self.wind_direction.value)
                offset = resolv(true_wind + compass, self.true_wind_compass_offset.value)
                d = .05
                self.true_wind_compass_offset.update(offset, d)
    
    def fix_compass_calibration_change(self, data, t0):
        headingrate = self.boatimu.SensorValues['headingrate_lowpass'].value
        dt = min(t0 - self.lasttime, .25) # maximum dt of .25 seconds
        self.lasttime = t0
        #if the compass gets a new fix, or the alignment changes,
        # update the autopilot command so the course remains constant
        self.compass_change = 0
        if data:
            if 'compass_calibration_updated' in data and self.last_heading:
                # with compass calibration updates, adjust the compass offset to hold the same course
                # to prevent actual course change
                last_heading = resolv(self.last_heading, data['heading'])
                self.compass_change += data['heading'] - headingrate*dt - last_heading
            self.last_heading = data['heading']

        # if heading offset alignment changed, keep same course
        if self.last_heading_off != self.boatimu.heading_off.value:
            self.last_heading_off = resolv(self.last_heading_off, self.boatimu.heading_off.value)
            self.compass_change += self.boatimu.heading_off.value - self.last_heading_off
            self.last_heading_off = self.boatimu.heading_off.value
            
        if self.compass_change:
            self.gps_compass_offset.value -= self.compass_change
            self.wind_compass_offset.value += self.compass_change
            self.true_wind_compass_offset.value += self.compass_change
            if self.mode.value == 'compass':
                heading_command = self.heading_command.value + self.compass_change
                self.heading_command.set(resolv(heading_command, 180))
          
    def compute_heading_error(self, t):
        heading = self.heading.value
        windmode = 'wind' in self.mode.value

        # keep same heading if mode changes
        if self.mode.value != self.lastmode:
            error = self.heading_error.value
            if windmode:
                error = -error # wind error is reversed
            self.heading_command.set(heading - error)
            self.lastmode = self.mode.value
      
        # compute heading error
        heading_command = self.heading_command.value

        # error +- 60 degrees
        err = minmax(resolv(heading - heading_command), 60)
      
        # since wind direction is where the wind is from, the sign is reversed
        if 'wind' in self.mode.value:
            err = -err
        self.heading_error.set(err)

        # compute integral for I gain
        dt = t - self.heading_error_int_time
        dt = min(dt, 1) # ensure dt is less than 1
        self.heading_error_int_time = t
        # int error +- 1, from 0 to 1500 deg/s
        self.heading_error_int.set(minmax(self.heading_error_int.value + \
                                          (self.heading_error.value/1500)*dt, 1))          
    def iteration(self):
        data = False
        t0 = time.monotonic()

        self.server.poll() # needed if not multiprocessed
        msgs = self.client.receive(self.dt)
        for msg in msgs: # we aren't usually subscribed to anything
            print('autopilot main process received:', msg, msgs[msg])

        if not self.enabled.value:
            self.servo.poll()

        t1 = time.monotonic()
        period = 1/self.boatimu.rate.value
        if t1 - t0 > period/2 + self.dt:
            print(_('server/client is running too _slowly_'), t1-t0)

        self.sensors.poll()

        t2 = time.monotonic()
        if t2-t1 > period/2:
            print(_('sensors is running too _slowly_'), t2-t1)

        sp = 0
        for tries in range(14): # try 14 times to read from imu
            timu = time.monotonic()
            data = self.boatimu.read()
            if data:
                break
            pd10 = period/10
            sp += pd10
            time.sleep(pd10)

            #if not data:
            #print('autopilot failed to read imu at time:', time.monotonic(), period)

        t3 = time.monotonic()
        if t3-t2 > period/2 and data:
            print('read imu running too _slowly_', t3-t2, period)

        self.fix_compass_calibration_change(data, t0)
        self.compute_offsets()

        pilot = self.pilots[self.pilot.value] # select pilot

        self.adjust_mode(pilot)
        pilot.compute_heading()
        self.compute_heading_error(t0)

        if self.enabled.value:
            self.runtime.update()
        else:
            self.runtime.stop()

        # reset filters when autopilot is enabled
        if self.enabled.value != self.lastenabled:
            self.lastenabled = self.enabled.value
            if self.enabled.value:
                self.heading_error_int.set(0) # reset integral
                self.heading_command_rate.set(0)
                # reset feed-forward gain
                self.last_heading_mode = False

        # reset feed-forward error if mode changed, or last command is older than 1 second
        if self.last_heading_mode != self.mode.value or t0 - self.heading_command_rate.time > 1:
            self.last_heading_command = self.heading_command.value

        # filter the heading command to compute feed-forward gain
        heading_command_diff = resolv(self.heading_command.value - self.last_heading_command)
        if not 'wind' in self.mode.value: # wind modes need opposite gain
            heading_command_diff = -heading_command_diff

        self.last_heading_command = self.heading_command.value        
        self.heading_command_rate.time = t0
        lp = .1
        command_rate = (1-lp)*self.heading_command_rate.value + lp*heading_command_diff
        self.heading_command_rate.update(command_rate)
            
        self.last_heading_mode = self.mode.value
                
        # perform tacking or pilot specific calculation
        if not self.tack.process():
            # if disabled, only compute if a client cares
            compute = True
            if not self.enabled.value:
                for gain in pilot.gains:
                    if pilot.gains[gain]['sensor'].watch:
                        break
                else:
                    compute = False
            if compute:
                pilot.process() # implementation specific process

        # servo can only disengage under manual control
        self.servo.force_engaged = self.enabled.value

        t4 = time.monotonic()
        if t4-t3 > period/2:
            print(_('autopilot routine is running too _slowly_'), t4-t3)

        if self.enabled.value:
            self.servo.poll()

        self.boatimu.send_cal_data() # after critical loop is done

        t5 = time.monotonic()
        if t5-t4 > period/2 and self.servo.driver:
            print(_('servo is running too _slowly_'), t5-t4)

        self.timings.set([t1-t0, t2-t1, t3-t2, t4-t3, t5-t4, t5-t0])
        self.timestamp.set(t0-self.starttime)
          
        if self.watchdog_device:
            self.watchdog_device.write('c')
            
        while True: # sleep remainder of period
            dt = period - (time.monotonic() - t0) + sp
            if dt >= period or dt <= 0:
                break

            time.sleep(dt)
            # do waiting in client if not enabled to reduce lag
            self.dt = 0 if self.enabled.value else dt*.8

def main():
    ap = Autopilot()
    print('autopilot init complete', time.monotonic())
    while True:
        ap.iteration()

if __name__ == '__main__':
    main()

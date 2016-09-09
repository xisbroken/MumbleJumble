#!/usr/bin/env python
from __future__ import print_function

from collections import deque
import os
import imp
import sys
import audioop
import time
import traceback
import threading
import json
import handles

SCRIPTPATH = os.path.dirname(__file__)
# Add pymumble folder to python PATH for importing
sys.path.append(os.path.join(SCRIPTPATH, 'pymumble'))
import pymumble

PIDFILE = '/tmp/mj.pid'

def get_arg_value(arg):
    """Retrieves the values associated to command line arguments
    Possible arguments:
    --server    Mumble server address
    --port      Mumble server port
    --user      Mumble bot's username
    --password  Mumble server password
    --certfile  Mumble certification
    --reconnect Bot reconnects to the server if disconnected
    --debug     Debug=True will generate a lot of stdout messages
    """
    if arg in sys.argv[1:]:
        try:
            return sys.argv[1:][sys.argv[1:].index(arg) + 1]
        except IndexError:
            sys.exit('Value of parameter ' + arg + ' is missing!')


def arg_in_arglist(arg, args_list):
    if arg in args_list:
        return True
    else:
        return False


def num_scripts():
    if os.path.isfile(PIDFILE):
        with open(PIDFILE) as f:
            return len(f.readlines())
    return 0


def writepid():
    mode = 'a' if os.path.isfile(PIDFILE) else 'w'
    with open(PIDFILE, mode) as f:
        f.write(str(os.getpid()) + '\n')
        

def deletepid():
    with open(PIDFILE, 'r') as f:
        lines = f.readlines()
    with open(PIDFILE, 'w') as f:
        for line in lines:
            if line != str(os.getpid()) + '\n':
                f.write(line)


class MJModule:
    """Object of MumbleJumble's modules"""
    def __init__(self, call, loop=None):
        self.call = call
        self.loop = loop


class MumbleJumble:
    """Represents the Mumble client interacting with users and outputting sound
    """
    def __init__(self):
        with open(os.path.join(SCRIPTPATH, 'config.json')) as json_config_file:
            self.config = json.load(json_config_file)

        pymumble_parameters = {}
        arglist = ['--server', '--port', '--user', '--password', '--certfile', 
                   '--reconnect', '--debug']

        for arg in arglist:
            if arg == '--user':
                try:
                    pymumble_parameters[arg[2:]] = self.config['bot'][arg[2:]][num_scripts()]
                except IndexError:
                    if arg in sys.argv[1:]:
                        pymumble_parameters[arg[2:]] = get_arg_value(arg)
                    else:
                        sys.exit('Usernames already taken')
            else:
                pymumble_parameters[arg[2:]] = self.config['bot'][arg[2:]]
            if arg_in_arglist(arg, sys.argv[1:]):
                pymumble_parameters[arg[2:]] = get_arg_value(arg)
            if arg == '--server':
                if pymumble_parameters['server'] == "":
                    sys.exit('Server address is missing!')

        writepid()

        self.client = pymumble.Mumble(host=pymumble_parameters['server'], 
                                      port=pymumble_parameters['port'],
                                      user=pymumble_parameters['user'], 
                                      password=pymumble_parameters['password'],
                                      certfile=pymumble_parameters['certfile'],
                                      reconnect=pymumble_parameters['reconnect'], 
                                      debug=pymumble_parameters['debug'])

        # Sets to client to call command_received when a user sends text
        self.client.callbacks.set_callback('text_received', self.command_received)

        self.audio_queue = deque([]) # Queue of audio ready to be sent

        self.client.start() # Start the mumble thread

        self.volume = 1.00
        self.paused = False
        self.skipFlag = False
        self.startStream = False
        self.reload_count = 0
        self.client.is_ready() # Wait for the connection
        self.client.set_bandwidth(200000)
        self.client.users.myself.unmute() # Be sure the client is not muted
        with open(os.path.join(SCRIPTPATH, 'comment')) as comment:
            self.client.users.myself.comment(comment.read())

        self.load_modules()

        self.ffmpegthread = ffmpegThread(self)
        self.ffmpegthread.daemon = True
        self.ffmpegthread.start()

        self.loopthread = LoopThread(self)
        self.loopthread.daemon = True
        self.loopthread.start()

        self.audio_loop() # Loops the main thread

    def load_modules(self):
        print('\nLoading bot modules')
        self.registered_commands = {'c' :('built-in', self.clear_queue),
                                    'clear' :('built-in', self.clear_queue),
                                    'p' :('built-in', self.toggle_pause),
                                    'pause' :('built-in', self.toggle_pause),
                                    'q' :('built-in', self.print_queue),
                                    'queue' :('built-in', self.print_queue),
                                    'r' :('built-in', self.reload_modules),
                                    'reload' :('built-in', self.reload_modules),
                                    's' :('built-in', self.skip),
                                    'seek' :('built-in', self.seek),
                                    'skip' :('built-in', self.skip),
                                    'v' :('built-in', self.chg_vol),
                                    'vol' :('built-in', self.chg_vol),
                                    'volume' :('built-in', self.chg_vol)}
        self.registered_modules = [] # List of module objects

        # Lists modules
        filenames = []
        for fn in os.listdir(os.path.join(SCRIPTPATH, 'modules')):
            if fn.endswith('.py') and not fn.startswith('_'):
                filenames.append(os.path.join(SCRIPTPATH, 'modules', fn))

        # Tries to import modules
        modules = []
        for filename in filenames:
            name = os.path.basename(filename)[:-3]
            try: module = imp.load_source(name, filename)
            except Exception as e:
                print('Could not load module ' + name)
                print('  ' + str(e))
                continue
            modules.append(module)

        # Registers modules and creates modules objects
        for module in modules:
            try:
                if hasattr(module, 'register'):
                    if hasattr(module.register, 'enabled') and not module.register.enabled:
                        continue

                    print('Loading module ', module.__name__)
                    module.register(self)

                    if hasattr(module, 'loop'):
                        module_object = MJModule(module.call, module.loop)
                    else:
                        module_object = MJModule(module.call)

                    self.registered_modules.append(module_object)

                    try:
                        for command in module.register.commands:
                            if command in self.registered_commands.keys():
                                print('Command "{0}" already registered'.format(command), file=sys.stderr)
                            else:
                                print("  Registering '{0}' - for module '{1}'".format(command, module.__name__))
                                self.registered_commands[command] = ('module', module_object.call)
                    except TypeError:
                        print("  No commands registered for module '{0}'".format(module.__name__))

                else:
                    print("Could not register '{0}', for it is missing the 'register' function".format(module), file=sys.stderr)
            except Exception as e:
                print("Error registering module '{0}'".format(module.__name__))
                traceback.print_exc()
        return len(modules)


    def reload_modules(self, command, arguments):
        self.reload_count += 1
        loaded_count = self.load_modules()
        self.send_msg_current_channel('Reloaded <b>{0}</b> bot modules'.format(loaded_count))


    def command_received(self, text):
        """Main function that reads commands in chat and outputs accordingly
        Takes text, a class from pymumble.mumble_pb2. Commands have to start with a !
        """
        message = text.message.lstrip().split(' ', 1)
        if message[0].startswith('!'):
            command = message[0][1:]
            arguments = ''.join(message[1]).strip(' ') if len(message) > 1 else ''

            # Module loaded commands
            if command in self.registered_commands.keys():
                if self.registered_commands[command][0] == 'built-in':
                    self.registered_commands[command][1](command, arguments)

                elif self.registered_commands[command][0] == 'module':
                    self.registered_commands[command][1](self, command, arguments)

                else:
                    print('Error handling command "{0}":'.format(command))
                    traceback.print_exc()
     

    def append_audio(self, audio_file, audio_type, audio_title='N/A'):
        if self.startStream == True and audio_type == 'complete':
            self.send_msg_current_channel('Stream in progress, cannot add audio')
        elif self.startStream == True and audio_type == 'stream':
            self.ffmpegthread.audio2process.append((audio_file, audio_type, audio_title))
        elif self.startStream == False:
            self.ffmpegthread.audio2process.append((audio_file, audio_type, audio_title))


    def get_current_channel(self):
        """Get the client's current channel (a dict)"""
        try:
            return self.client.channels[self.client.users.myself['channel_id']]
        except KeyError:
            print('Currently assuming bot is in channel 0, try moving it')
            return self.client.channels[0]


    def send_msg_current_channel(self, msg):
        """Send a message in the client's current channel"""
        channel = self.get_current_channel()
        channel.send_text_message(msg)


    def skip(self, command, arguments):
        if arguments != '':
            try:
                select = int(arguments)
                self.audio_queue.remove(self.audio_queue[select - 1])
            except (ValueError, IndexError):
                self.send_msg_current_channel('Not a valid value!')
        else:
            self.skipFlag = True


    def chg_vol(self, command, arguments):
        if arguments != '':
            try:
                self.volume = float(arguments)
                self.send_msg_current_channel('Changing volume to <b>{0}</b>'.format(self.volume)) 
            except ValueError:
                self.send_msg_current_channel('Not a valid value!')
        else:
            self.send_msg_current_channel('Current volume: <b>{0}</b>'.format(self.volume))


    def clear_queue(self, command, arguments):
        self.skipFlag = True
        self.audio_queue = deque([])


    def print_queue(self, command, arguments):
        """Creates a printable queue suited for the Mumble chat. Associated with
        the queue command. Checks the processing and processed song lists of the
        subthread. Possible states: Paused, Playing, Ready, Downloading.
        """
        if len(self.audio_queue) == 0:
            queue = 'Queue is empty'
        else:
            queue = ''
            for i in range(len(self.audio_queue)):
                if i == 0:
                    if self.paused:
                        queue += '<br />{0}<b> - Paused - {1}</b>'.format(
                                self.audio_queue[0].printable_queue_format()[0], 
                                self.audio_queue[0].printable_queue_format()[1])
                    elif not self.paused:
                        queue += '<br />{0}<b> - Playing - {1}</b>'.format(
                                self.audio_queue[0].printable_queue_format()[0], 
                                self.audio_queue[0].printable_queue_format()[1])

                else:
                    queue += '<br />{0}<b> - Ready - {1}</b>'.format(
                                            self.audio_queue[i].printable_queue_format()[0],
                                            self.audio_queue[i].printable_queue_format()[1])
        self.send_msg_current_channel(queue)


    def toggle_pause(self, command, arguments):
        """Toggle the pause command"""
        if self.paused:
            self.paused = False
        else:
            self.paused = True


    def seek(self, command, arguments):
        mod_arg = arguments.replace(':', '').zfill(6)
        new_time = '{0}:{1}:{2}.00'.format(mod_arg[0:2], mod_arg[2:4], mod_arg[4:6])
        try:
            seconds = handles.duration2sec(new_time)
            if 0 <= seconds <= handles.duration2sec(self.audio_queue[0].duration):
                self.audio_queue[0].seek(seconds)
            else:
                self.send_msg_current_channel('Cannot seek to specified value.')
        except:
            self.send_msg_current_channel('Cannot seek to specified value.')



    def audio_loop(self):
        """Main loop that sends audio samples to the server. Sends the first
        song in SubThread's song queue
        """
        while True:
            try:
                if len(self.audio_queue) > 0:
                    while self.audio_queue[0].current_sample <= self.audio_queue[0].total_samples:
                        while self.paused:
                            time.sleep(0.1)
                        while self.client.sound_output.get_buffer_size() > 0.5:
                            time.sleep(0.1)
                        if not self.skipFlag:
                            self.client.sound_output.add_sound(audioop.mul(
                                            self.audio_queue[0].samples[self.audio_queue[0].current_sample],
                                            2, self.volume))
                            self.audio_queue[0].current_sample += 1
                        elif self.skipFlag:
                            self.skipFlag = False
                            break
                    try:
                        # Removes the first song from the queue
                        # Will fail if clear command is passed, not a problem though
                        self.audio_queue.popleft()
                    except:
                        pass
                    finally:
                        time.sleep(1) # To allow time between songs
                else:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                deletepid()
                sys.exit('Exiting!')


class ffmpegThread(threading.Thread):
    """Tuple (audio_file, audio_type, audio_title)"""
    def __init__(self, parent):
        threading.Thread.__init__(self)
        self.audio2process = deque([])
        self.parent = parent


    def run(self):
        while True:
            if len(self.audio2process) > 0:
                if self.audio2process[0][1] == 'complete':
                    audio = handles.AudioFileHandle(self.audio2process[0][0], self.audio2process[0][2])
                elif self.audio2process[0][1] == 'stream':
                    audio = handles.StreamHandle(self.audio2process[0][0], self.audio2process[0][2])
                try:
                    audio.process()
                    self.parent.audio_queue.append(audio)
                    self.audio2process.popleft()
                except Exception as e:
                    print(e)
                    print('Cannot process audio file, aborting!')
                    self.audio2process.popleft()
            else:
                time.sleep(0.5)


class LoopThread(threading.Thread):
    def __init__(self, parent):
        threading.Thread.__init__(self)
        self.parent = parent
        self.counter = 0

    def run(self):
        while True:
            time.sleep(1)
            self.counter += 1 
            for module in self.parent.registered_modules:
                if hasattr(module, 'loop') and hasattr(module.loop, 'time'):
                    if self.counter % module.loop.time == 0:
                        module.loop(self.parent)


if __name__ == '__main__':
    musicbot = MumbleJumble()

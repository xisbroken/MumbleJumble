#!/usr/bin/env python
from __future__ import print_function

from collections import deque
import os
import imp
import sys
import audioop
import time
import traceback

# Add pymumble folder to python PATH for importing
sys.path.append(os.path.join(os.path.dirname(__file__), "pymumble"))
import pymumble


def get_arg_value(arg, args_list, default=None):
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
    if arg in args_list:
        return args_list[args_list.index(arg) + 1]
    elif default != None or arg == '--certfile':
        # Falls back to default if no parameter given
        # Parameter --certfile's default has to be None
        return default
    else:
        sys.exit('Parameter ' + arg + ' is missing!')
        

class MumbleJumble:
    """Represents the Mumble client interacting with users and outputting sound
    """
    def __init__(self):
        host = get_arg_value('--server', sys.argv[1:])
        port = int(get_arg_value('--port', sys.argv[1:], default=64738))
        user = get_arg_value('--user', sys.argv[1:], default='@MumbleJumble')
        password = get_arg_value('--password', sys.argv[1:], default='')
        certfile = get_arg_value('--certfile', sys.argv[1:], default=None)
        reconnect = get_arg_value('--reconnect', sys.argv[1:], default=False)
        debug = get_arg_value('--debug', sys.argv[1:], default=False)

        self.bot = pymumble.Mumble(host=host, port=port, user=user, password=
                                   password, certfile=certfile,
                                   reconnect=reconnect, debug=debug)

        # Sets to bot to call command_received when a user sends text
        self.bot.callbacks.set_callback('text_received', self.command_received)

        self.threads = {} # Dict of threads running, excluding the main thread
        self.audio_queue = deque([]) # Queue of audio ready to be sent

        self.bot.start() # Start the mumble thread

        self.setup()
        self.loop() # Loops the main thread

    def setup(self):
        print()
        print("Loading bot modules")
        self.registered_commands = {}
        self.volume = 1.00
        self.paused = False
        self.skipFlag = False
        self.current_song_sample = 0
        self.bot.is_ready() # Wait for the connection
        self.bot.set_bandwidth(200000)
        self.bot.users.myself.unmute() # Be sure the bot is not muted

        home = os.path.dirname(__file__)
        filenames = []
        for fn in os.listdir(os.path.join(home, 'modules')): 
            if fn.endswith('.py') and not fn.startswith('_'): 
                filenames.append(os.path.join(home, 'modules', fn))

        modules = []
        for filename in filenames: 
            name = os.path.basename(filename)[:-3]
            try: module = imp.load_source(name, filename)
            except Exception as e:
                print(e)
            modules.append(module)
        for module in modules:
            print("Loaded module '{0}'".format(module.__name__))
            try:
                if hasattr(module, 'register'): 
                    module.register(self)
                    for command in module.register.commands:
                        if command in self.registered_commands.keys():
                            print("Command '{0}' already registered by another module".format(command), file=sys.stderr)
                            sys.exit(1)
                        else:
                            print("  Registering '{0}' - for module '{1}'".format(command, module.__name__))
                            self.registered_commands[command] = module.call
                else:
                    print("Could not register '{0}', for it is missing the 'register' function".format(module), file=sys.stderr)
            except Exception as e:
                print("Error registering module '{0}'".format(module.__name__))
                traceback.print_exc()


    def get_current_channel(self):
        """Get the bot's current channel (a dict)"""
        try:
            return self.bot.channels[self.bot.users.myself['channel_id']]
        except KeyError:
            print('Currently assuming bot is in channel 0, try moving it')
            return self.bot.channels[0]


    def send_msg_current_channel(self, msg):
        """Send a message in the bot's current channel"""
        channel = self.get_current_channel()
        channel.send_text_message(msg)


    def command_received(self, text):
        """Main function that reads commands in chat and outputs accordingly
        Takes text, a class from pymumble.mumble_pb2
        The main loop pickups the change of states and the non-empty song queue
        Commands have to start with a !:
        a, add          Adds a song from URL to the current queue
        c, clear        Clears the queue and stops current song
        p, pause        Pause the current playing song
        q, queue        Displays the current queue in the chat
        s, skip         Skips the song currently playing
        v, vol, volume  Returns the current volume or changes it
        """
        message = text.message
        if message[0] == '!':
            message = message[1:].split(' ', 1)
            command = message[0]
            if len(message) == 1:

                if command == 'v' or command == 'vol' or command == 'volume':
                    self.send_msg_current_channel('Current volume: ' + '<b>'
                                              + str(self.volume) + '</b>')

                elif command == 'c' or command == 'clear':
                    self.skipFlag = True
                    self.threads['yt_thread'].new_songs = deque([])
                    self.audio_queue = deque([])

                elif command == 'p' or command == 'pause':
                    self.toggle_pause()

                elif command == 'q' or command == 'queue':
                    self.send_msg_current_channel(self.printable_queue())

                elif command == 's' or command == 'skip':
                    self.skipFlag = True

            else:
                arguments = message[1]
                if command == 'v' or command == 'vol' or command == 'volume':
                    try:
                        self.volume = float(arguments)
                        self.send_msg_current_channel('Changing volume to '
                                                          + '<b>' + str(self.volume)
                                                          + '</b>')
                    except ValueError:
                        self.send_msg_current_channel('Not a valid value!')

                elif command in self.registered_commands.keys():
                    try:
                        self.registered_commands[command](self, str(command), str(arguments))
                    except Exception as e:
                        print("Error handling command '{0}':".format(command))
                        traceback.print_exc()


    def current_song_status(self):
        """Returns the completion of the song in %. Associated with the queue
        command.
        """
        return float(self.current_song_sample) / float(
                self.audio_queue[0].samples['total_samples']) * 100


    def printable_queue(self):
        """Creates a printable queue suited for the Mumble chat. Associated with
        the queue command. Checks the processing and processed song lists of the
        subthread. Possible states: Paused, Playing, Ready, Downloading.
        """
        queue = []
        if len(self.audio_queue) + len(self.threads['yt_thread'].new_songs) == 0:
            return 'Queue is empty'
        else:
            for i in range(len(self.audio_queue)):
                if i == 0:
                    if self.paused:
                        queue.append('%s <b>Paused - %i %%</b>' %
                                (self.audio_queue[i].title, self.current_song_status()))
                    elif not self.paused:
                        queue.append('%s <b>Playing - %i %%</b>' %
                                (self.audio_queue[i].title, self.current_song_status()))
                else:
                    queue.append(self.audio_queue[i].title + ' <b>Ready</b>')
            for j in range(len(self.threads['yt_thread'].new_songs)):
                queue.append(self.threads['yt_thread'].new_songs[j].title + ' <b>Downloading</b>')
            return ', '.join(queue)


    def toggle_pause(self):
        """Toggle the pause command"""
        if self.paused:
            self.paused = False
        else:
            self.paused = True


    def loop(self):
        """Main loop that sends audio samples to the server. Sends the first
        song in SubThread's song queue
        """
        while True:
            if len(self.audio_queue) > 0:
                for i in range(self.audio_queue[0].samples['total_samples']):
                    self.current_song_sample = i
                    while self.paused:
                        time.sleep(0.1)
                    while self.bot.sound_output.get_buffer_size() > 0.5:
                        time.sleep(0.01)
                    if not self.skipFlag:
                        self.bot.sound_output.add_sound(audioop.mul(
                                        self.audio_queue[0].samples[i],
                                        2, self.volume))
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


if __name__ == '__main__':
    musicbot = MumbleJumble()
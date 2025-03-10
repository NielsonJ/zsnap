#!/usr/bin/env python
"""
The SNAP experiment launcher program. To be run on the subject's PC.

* Installation notes: see INSTALLATION NOTES.TXT

* This program can launch experiment modules that are specified in the modules directory
  (one at a time).

* The module to be launched (and various other options) can be specified at the command line;
    here is a complete listing of all possible config options and their defaults:
  launcher.py --module Sample1 --studypath studies/Sample1 --autolaunch 1 --developer 1 \\
  --engineconfig defaultsettings.prc --datariver 0 --labstreaming 1 --fullscreen 0 --windowsize 800x600 \\
  --windoworigin 50/50 --noborder 0 --nomousecursor 0 --timecompensation 1
    
* If in developer mode, several key bindings are enabled:
   Esc: exit program
   F1: start module
   F2: cancel module
  
* In addition to modules, there are "study configuration files" (aka study configs),
  which are in in the studies directory. These specify the module to launch in the first line
  and assignments to member variables of the module instance in the remaining lines (all Python syntax allowed).
  
  A config can be specified in the command line just by passing the appropriate .cfg file name,
  as in the following example.
  In addition, the directory where to look for the .cfg file can be specified as the studypath.
  launcher.py --module=test1.cfg --studypath=studies/DAS
  
* The program can be remote-controlled via a simple TCP text-format network protocol (on port 7899) supporting
    the following messages:
  start                  --> start the current module
  cancel                 --> cancel execution of the current module
  load modulename        --> load the module named modulename
  config configname.cfg  --> load a config named configname.cfg
                                                    (make sure that the studypath is set correctly so that it's found)
  setup name=value       --> assign a value to a member variable in the current module instance
                             can also involve multiple assignments separated by semicolons, full Python syntax allowed.
   
* The underlying Panda3d engine can be configured via a custom .prc file (specified as --engineconfig=filename.prc), see
  http://www.panda3d.org/manual/index.php/Configuring_Panda3D
  
* For quick-and-dirty testing you may also override the launch options below under "Default Launcher Configuration",
    but note that you cannot check these changes back into the main source repository of SNAP.
    
"""
import fnmatch
import optparse
import os
import queue
# network support
import socket
import socketserver
import sys
# thread coordination
import threading
import time
import traceback

# panda3d support
from direct.showbase.ShowBase import ShowBase
from direct.task.Task import Task
from panda3d.core import loadPrcFile, loadPrcFileData, Filename, DSearchPath
from pandac.PandaModules import WindowProperties

from framework import shared_lock
from framework import OSCClient, OSCMessage
from framework.eventmarkers import init_markers, shutdown_markers

SNAP_VERSION = '1.02'

# -----------------------------------------------------------------------------------------
# --- Default Launcher Configuration (selectively overridden by command-line arguments) ---
# -----------------------------------------------------------------------------------------


# If non-empty, this is the module that will be initially loaded if nothing
# else is specified. Can also be a .cfg file of a study.
LOAD_MODULE = "Sample1"

# If true, the selected module will be launched automatically; otherwise it will
# only be (pre-)loaded; the user needs to press F1 (or issue the "start" command remotely) to start the module 
AUTO_LAUNCH = True

# The directory in which to look for .cfg files, if passed as module or via
# remote control messages.
STUDYPATH = "studies/SampleStudy"

# The default engine configuration.
ENGINE_CONFIG = "engine_default_settings.prc"

# Set this to True or False to override the settings in the engineconfig file (.prc)
FULLSCREEN = None

# Set this to a resolution like "1024x768" (with quotes) to override the settings in the engineconfig file
WINDOWSIZE = None

# Set this to a pixel offset from left top corner, e.g. "50/50"
# (with quotes) to override the window location in the engineconfig file
WINDOWORIGIN = None

# Set this to True or False to override the window border setting in the engineconfig file
NOBORDER = None

# Set this to True or False to override the mouse cursor setting in the engineconfig file
NOMOUSECURSOR = None

# Enable DataRiver support for marker sending.
DATA_RIVER = False

# Enable lab streaming layer support for marker sending.
LAB_STREAMING = True

# This is the default port on which the launcher listens for remote control 
# commands (e.g. launching an experiment module)
SERVER_PORT = 7897

# Whether the Launcher starts in developer mode; if true, modules can be loaded,
# started and cancelled via keyboard shortcuts (not recommended for production 
# experiments)
DEVELOPER_MODE = True

# Whether lost time (e.g., to processing or jitter) is compensated for by making the next sleep() slightly shorter
COMPENSATE_LOST_TIME = True

# Which serial port to use to transmit events (0=disabled)
COM_PORT = 0

# Whether to use OSC for sound playback
OSC_SOUND = False

# these are the IP addresses for the involved OSC machines
OSC_MACHINE_IP = {"array": "10.0.0.105",
                  "surround": "10.0.0.108"}
# {"array":"10.0.0.105"}
# {"array":"10.0.0.105",
#  "surround":"10.0.0.108"}
# {"surround":"10.0.0.108"}
# {"array":"10.0.0.105"}
# {"array":"10.0.0.105",
#  "surround":"10.0.0.106"}

# OSC sound volume
OSC_VOLUME = -33.0

# ------------------------------
# --- Startup Initialization ---
# ------------------------------

print('This is SNAP version ' + SNAP_VERSION + "\n\n")

# --- Parse console arguments ---

print('Reading command-line options...')
parser = optparse.OptionParser()
parser.add_option("-m", "--module", dest="module", default=LOAD_MODULE,
                  help="Experiment module to load upon startup (see modules). "
                       "Can also be a .cfg file of a study (see studies and --studypath).")
parser.add_option("-s", "--studypath", dest="studypath", default=STUDYPATH,
                  help="The directory in which to look for .cfg files, media, .prc files etc. for a particular study.")
parser.add_option("-a", "--autolaunch", dest="autolaunch", default=AUTO_LAUNCH,
                  help="Whether to automatically launch the selected module.")
parser.add_option("-d", "--developer", dest="developer", default=DEVELOPER_MODE,
                  help="Whether to launch in developer mode; if true, allows to load, start, and cancel "
                       "experiment modules via keyboard shortcuts.")
parser.add_option("-e", "--engineconfig", dest="engineconfig", default=ENGINE_CONFIG,
                  help="A configuration file for the Panda3d engine "
                       "(allows to change many engine-level settings, such as the renderer; "
                       "note that the format is dictated by Panda3d).")
parser.add_option("-f", "--fullscreen", dest="fullscreen", default=FULLSCREEN,
                  help="Whether to go fullscreen (default: according to current engine config).")
parser.add_option("-w", "--windowsize", dest="windowsize", default=WINDOWSIZE,
                  help="Window size, formatted as in --windowsize 1024x768 to select the main window size in pixels "
                       "(default: accoding to current engine config).")
parser.add_option("-o", "--windoworigin", dest="windoworigin", default=WINDOWORIGIN,
                  help="Window origin, formatted as in --windoworigin 50/50 to select the main window origin, "
                       "i.e. left upper corner in pixes (default: accoding to current engine config).")
parser.add_option("-b", "--noborder", dest="noborder", default=NOBORDER,
                  help="Disable window borders (default: accoding to current engine config).")
parser.add_option("-c", "--nomousecursor", dest="nomousecursor", default=NOMOUSECURSOR,
                  help="Disable mouse cursor (default: accoding to current engine config).")
parser.add_option("-r", "--datariver", dest="datariver", default=DATA_RIVER,
                  help="Whether to enable DataRiver support in the launcher.")
parser.add_option("-l", "--labstreaming", dest="labstreaming", default=LAB_STREAMING,
                  help="Whether to enable lab streaming layer (LSL) support in the launcher.")
parser.add_option("-p", "--serverport", dest="serverport", default=SERVER_PORT,
                  help="The port on which the launcher listens for remote control commands (e.g. loading a module).")
parser.add_option("-t", "--timecompensation", dest="timecompensation", default=COMPENSATE_LOST_TIME,
                  help="Compensate time lost to processing or jitter by making the successive sleep() call "
                       "shorter by a corresponding amount of time "
                       "(good for real time, can be a hindrance during debugging).")
parser.add_option("--comport", dest="comport", default=COM_PORT,
                  help="The COM port over which to send markers, or 0 if disabled.")
parser.add_option("-x", "--xoscsound", dest="oscsound", default=OSC_SOUND,
                  help="Use OSC for sound playback.")
parser.add_option("-v", "--volumeosc", dest="volumeosc", default=OSC_VOLUME,
                  help="Override OSC volume.")
parser.add_option("-i", "--idosc", dest="idosc", default='0',
                  help="The OSC client ID (determines which sound ID range it gets).")
(opts, args) = parser.parse_args()

# --- Pre-engine initialization ---
print('Performing pre-engine initialization...')
init_markers(opts.labstreaming, False, opts.datariver, int(opts.comport), socket.gethostname() + "_" + opts.module)

print("Applying the engine configuration file/settings...")
# load the selected engine configuration (studypath takes precedence over the SNAP root path)
config_searchpath = DSearchPath()
config_searchpath.appendDirectory(Filename.fromOsSpecific(opts.studypath))
config_searchpath.appendDirectory(Filename.fromOsSpecific('.'))
loadPrcFile(config_searchpath.findFile(Filename.fromOsSpecific(opts.engineconfig)))

# add a few more media search paths (in particular, media can be in the media directory, or in the studypath)
loadPrcFileData('', 'model-path ' + opts.studypath + '/media')
loadPrcFileData('', 'model-path ' + opts.studypath)
loadPrcFileData('', 'model-path media')

# override engine settings according to the command line arguments, if specified
if opts.fullscreen is not None:
    loadPrcFileData('', 'fullscreen ' + opts.fullscreen)
if opts.windowsize is not None:
    loadPrcFileData('', 'win-size ' + opts.windowsize.replace('x', ' '))
if opts.windoworigin is not None:
    loadPrcFileData('', 'win-origin ' + opts.windoworigin.replace('/', ' '))
if opts.noborder is not None:
    loadPrcFileData('', 'undecorated ' + opts.noborder)
if opts.nomousecursor is not None:
    loadPrcFileData('', 'nomousecursor ' + opts.nomousecursor)

# init OSC sound
oscclient = None
if opts.oscsound:
    print("Loading sound system...")
    oscclient = {}
    for m in OSC_MACHINE_IP.keys():
        # there are multiple machines responsible for sound playback over different speaker groups:
        # connect to each of them
        print("Connecting to", m, "(" + OSC_MACHINE_IP[m] + ")...")
        try:
            oscclient[m] = OSCClient()
            # hack in some management for the assining numbers to sources...
            if opts.idosc == '0':
                oscclient[m].idrange = [1, 2, 3, 4]
            elif opts.idosc == '1':
                oscclient[m].idrange = [5, 6, 7, 8]
            elif opts.idosc == '2':
                oscclient[m].idrange = [9, 10, 11, 12]
            else:
                raise Exception("Unsupported OSC ID specified.")
            oscclient[m].current_source = 0
            oscclient[m].projectname = 'SCCN'
            oscclient[m].connect((OSC_MACHINE_IP[m], 15003))
            if opts.idosc == '1':
                print("sending OSC master commands...")
                msg = OSCMessage("/AM/Load")
                msg += ["/" + oscclient[m].projectname]
                oscclient[m].send(msg)
                # wait a few seconds...
                time.sleep(4)
                # load the default preset
                msg = OSCMessage("/" + oscclient[m].projectname + "/system")
                msg += ["preset", 1]
                oscclient[m].send(msg)
                msg = OSCMessage("/AM/Volume")
                msg.append(float(opts.volumeosc))
                oscclient[m].send(msg)
                msg = OSCMessage("/" + oscclient[m].projectname + "/surround/1/point")
                msg += [0, "stop"]
                oscclient[m].send(msg)
                msg = OSCMessage("/" + oscclient[m].projectname + "/surround/2/point")
                msg += [0, "stop"]
                oscclient[m].send(msg)
                msg = OSCMessage("/" + oscclient[m].projectname + "/array/1/point")
                msg += [0, "stop"]
                oscclient[m].send(msg)
                msg = OSCMessage("/" + oscclient[m].projectname + "/array/2/point")
                msg += [0, "stop"]
                oscclient[m].send(msg)
            print("success.")
        except Exception as e:
            print("failed:" + e)

global is_running
is_running = True


# -----------------------------------
# --- Main application definition ---
# -----------------------------------

class MainApp(ShowBase):
    """The Main SNAP application."""

    def __init__(self, opts):
        ShowBase.__init__(self)

        self._module = None  # the currently loaded module
        self._instance = None  # instance of the module's Main class
        self._executing = False  # whether we are executing the module
        self._remote_commands = queue.Queue()  # a message queue filled by the TCP server
        self._opts = opts  # the configuration options
        self._console = None  # graphical console, if any

        # send an initial start marker
        # send_marker(999)

        # preload some data and init some settings
        self.set_defaults()

        # register the main loop
        self._main_task = self.taskMgr.add(self._main_loop_tick, "main_loop_tick")

        # register global keys if desired
        if opts.developer:
            self.accept("escape", self.terminate)
            self.accept("f1", self._remote_commands.put, ['start'])
            self.accept("f2", self._remote_commands.put, ['cancel'])
            self.accept("f5", self._remote_commands.put, ['prune'])
            self.accept("f12", self._init_console)

        # load the initial module or config if desired
        if opts.module is not None:
            if opts.module.endswith(".cfg"):
                self.load_config(opts.module)
            else:
                self.load_module(opts.module)

        # start the module if desired
        if opts.autolaunch or (opts.autolaunch == '1'):
            self.start_module()

        # start the TCP server for remote control
        self._init_server(opts.serverport)

    def set_defaults(self):
        """Sets some environment defaults that might be overridden by the modules."""
        font = self.loader.loadFont('arial.ttf', textureMargin=5)
        font.setPixelsPerUnit(128)
        self.win.setClearColorActive(True)
        self.win.setClearColor((0.3, 0.3, 0.3, 1))
        winprops = WindowProperties()
        winprops.setTitle('SNAP')
        self.win.requestProperties(winprops)

    def load_module(self, name):
        """Try to load the given module, if any.
        The module should be somewhere on the PYTHONPATH or
        in any (sub-)folder under the local 'modules' folder."""

        if name is not None and len(name) > 0:
            print(f'Importing experiment module "{name}"... ', end="\r")
            if self._instance is not None:
                self.prune_module()
            self.set_defaults()

            # try importing the module from the expected locations (e.g., site-packages)
            try:
                self._module = __import__(name)
                print(f'done importing {self._module}')
            except ModuleNotFoundError:
                # add new search places and try again
                locations = []
                if os.path.exists("modules"):
                    # find it under modules...
                    print(f"Searching the module {name} locally in the 'modules' folder... ", end="\r")
                    for root, _, filenames in os.walk('modules'):
                        if len(fnmatch.filter(filenames, name + '.py')) >= 1:
                            locations.append(root)
                        if len(fnmatch.filter(filenames, name + '.pyc')) >= 1:
                            locations.append(root)
                        if len(fnmatch.filter(filenames, name)) >= 1:
                            locations.append(root)

                    # Add these paths to the search path
                    for loc in locations:
                        if loc not in sys.path:
                            sys.path.insert(0, loc)
                try:
                    self._module = __import__(name)
                    print(f'done importing {self._module}')
                except ModuleNotFoundError as err:
                    print(f"The module named '{name}' could not be found.")
                    raise err
                except ImportError as err:
                    print(f"The experiment module '{name}' could not be imported correctly. "
                          f"Make sure that its own imports are properly found by Python;"
                          f"\n reason: {err}")
                    traceback.print_exc()
                    raise err

            print("Instantiating the module's Main class...")
            self._instance = self._module.Main()
            self._instance._make_up_for_lost_time = self._opts.timecompensation
            self._instance._oscclient = oscclient
            print('done.')

    def load_config(self, name):
        """Try to load a study config file (see studies directory)."""
        print(f'Attempting to load config "{name}"...')
        try:
            if os.path.exists(name):
                file = name
            elif os.path.exists(os.path.join(self._opts.studypath, name)):
                file = os.path.join(self._opts.studypath, name)
            else:
                print(f'The file "{name}" was not found locally nor in the {self._opts.studypath}.')
                return

            with open(file, 'r') as f:
                # The module name is expected to be on the first line
                self.load_module(f.readline().strip())

                print('Now setting variables...')
                for line in f.readlines():
                    exec(line, self._instance.__dict__)
                print('done; config is loaded.')
        except Exception as err:
            print(f'Error while loading the study config file "{name}".')
            print(err)
            traceback.print_exc()

    # start executing the currently loaded module
    def start_module(self):
        if self._instance is not None:
            self.cancel_module()
            print('Starting module execution...')
            self._instance.start()
            print('done.')
            self._executing = True

    # cancel executing the currently loaded module (may be started again later)
    def cancel_module(self):
        if (self._instance is not None) and self._executing:
            print('Canceling module execution...')
            self._instance.cancel()
            print('done.')
        self._executing = False

    # prune a currently loaded module's resources
    def prune_module(self):
        if self._instance is not None:
            print("Pruning current module's resources...")
            try:
                self._instance.prune()
            except Exception as inst:
                print("Exception during prune:")
                print(inst)
            print('done.')

    # --- internal ---

    def _init_server(self, port):
        """Initialize the remote control server."""
        destination = self._remote_commands

        class ThreadedTCPRequestHandler(socketserver.StreamRequestHandler):
            def handle(self):
                try:
                    print("Client connection opened.")
                    while True:
                        data = self.rfile.readline().strip()
                        if len(data) == 0:
                            break
                        destination.put(data)
                except:
                    print("Connection closed by client.")

        class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            pass

        print("Bringing up remote-control server on port", port, "...")
        try:
            server = ThreadedTCPServer(("", port), ThreadedTCPRequestHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            print("done.")
        except:
            print("failed; the port is already taken (probably the previous process is still around).")

    # init a console that is scoped to the current module
    def _init_console(self):
        """Initialize a pull-down console. Note that this console is a bit glitchy -- use at your own risk."""
        if self._console is None:
            try:
                print("Initializing console...")
                from framework.console.interactiveConsole import pandaConsole, INPUT_CONSOLE, INPUT_GUI, OUTPUT_PYTHON
                self._console = pandaConsole(INPUT_CONSOLE | INPUT_GUI | OUTPUT_PYTHON, self._instance.__dict__)
                print("done.")
            except Exception as inst:
                print("failed:")
                print(inst)

    # main loop step, ticked every frame
    def _main_loop_tick(self, task):
        # engine_lock.release()
        shared_lock.release()

        # process any queued-up remote control messages
        try:
            while True:
                cmd = str(self._remote_commands.get_nowait()).strip()
                if cmd == "start":
                    self.start_module()
                elif (cmd == "cancel") or (cmd == "stop"):
                    self.cancel_module()
                elif cmd == "prune":
                    self.prune_module()
                elif cmd.startswith("load "):
                    self.load_module(cmd[5:])
                elif cmd.startswith("setup "):
                    try:
                        exec(cmd[6:], self._instance.__dict__)
                    except:
                        pass
                elif cmd.startswith("config "):
                    if not cmd.endswith(".cfg"):
                        self.load_config(cmd[7:] + ".cfg")
                    else:
                        self.load_config(cmd[7:])
        except queue.Empty:
            pass

        # tick the current module
        if (self._instance is not None) and self._executing:
            self._instance.tick()

        shared_lock.acquire()
        # engine_lock.acquire()
        return task.cont

    def terminate(self):
        exit()
        global is_running
        is_running = False


# ----------------------
# --- SNAP Main Loop ---
# ----------------------
try:
    app = MainApp(opts)
    # shared_lock.acquire()
    # app.run()
    # shared_lock.release()

    while is_running:
        shared_lock.acquire()
        # engine_lock.acquire()
        app.taskMgr.step()
        # engine_lock.release()
        shared_lock.release()
except Exception as e:
    print('Error in main loop: ', e)
    traceback.print_exc()

# --------------------------------
# --- Finalization and cleanup ---
# --------------------------------
print('Terminating launcher...')
shutdown_markers()

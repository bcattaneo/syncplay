
import asynchat
import asyncore
import os
import random
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from syncplay import constants, utils
from syncplay.messages import getMessage
from syncplay.players.basePlayer import BasePlayer
from syncplay.utils import isBSD, isLinux, isWindows, isMacOS


class VlcPlayer(BasePlayer):
    speedSupported = True
    customOpenDialog = False
    chatOSDSupported = False
    alertOSDSupported = True
    osdMessageSeparator = "; "

    RE_ANSWER = re.compile(constants.VLC_ANSWER_REGEX)
    SLAVE_ARGS = constants.VLC_SLAVE_ARGS
    if isMacOS():
        SLAVE_ARGS.extend(constants.VLC_SLAVE_MACOS_ARGS)
    else:
        SLAVE_ARGS.extend(constants.VLC_SLAVE_NONMACOS_ARGS)
    vlcport = random.randrange(constants.VLC_MIN_PORT, constants.VLC_MAX_PORT) if (constants.VLC_MIN_PORT < constants.VLC_MAX_PORT) else constants.VLC_MIN_PORT

    def __init__(self, client, playerPath, filePath, args):
        from twisted.internet import reactor
        self.reactor = reactor
        self._client = client
        self._paused = None
        self._duration = None
        self._filename = None
        self._filepath = None
        self._filechanged = False
        self._lastVLCPositionUpdate = None
        self.shownVLCLatencyError = False
        self._previousPreviousPosition = -2
        self._previousPosition = -1
        self._position = 0
        try:  # Hack to fix locale issue without importing locale library
            self.radixChar = "{:n}".format(1.5)[1:2]
            if self.radixChar == "" or self.radixChar == "1" or self.radixChar == "5":
                raise ValueError
        except:
            self._client.ui.showErrorMessage(
                "Failed to determine locale. As a fallback Syncplay is using the following radix character: \".\".")
            self.radixChar = "."

        self._durationAsk = threading.Event()
        self._filenameAsk = threading.Event()
        self._pathAsk = threading.Event()
        self._positionAsk = threading.Event()
        self._pausedAsk = threading.Event()
        self._vlcready = threading.Event()
        self._vlcclosed = threading.Event()
        self._listener = None
        try:
            self._listener = self.__Listener(self, playerPath, filePath, args, self._vlcready, self._vlcclosed)
        except ValueError:
            self._client.ui.showErrorMessage(getMessage("vlc-failed-connection"), True)
            self.reactor.callFromThread(self._client.stop, True,)
            return
        try:
            self._listener.setDaemon(True)
            self._listener.start()
            if not self._vlcready.wait(constants.VLC_OPEN_MAX_WAIT_TIME):
                self._vlcready.set()
                self._client.ui.showErrorMessage(getMessage("vlc-failed-connection"), True)
                self.reactor.callFromThread(self._client.stop, True,)
            self.reactor.callFromThread(self._client.initPlayer, self,)
        except:
            pass

    def _fileUpdateClearEvents(self):
        self._durationAsk.clear()
        self._filenameAsk.clear()
        self._pathAsk.clear()

    def _fileUpdateWaitEvents(self):
        self._durationAsk.wait()
        self._filenameAsk.wait()
        self._pathAsk.wait()

    def _onFileUpdate(self):
        self._fileUpdateClearEvents()
        self._getFileInfo()
        self._fileUpdateWaitEvents()
        args = (self._filename, self._duration, self._filepath)
        self.reactor.callFromThread(self._client.updateFile, *args)
        self.setPaused(self._client.getGlobalPaused())
        self.setPosition(self._client.getGlobalPosition())

    def askForStatus(self):
        self._filechanged = False
        self._positionAsk.clear()
        self._pausedAsk.clear()
        self._listener.sendLine(".")
        if self._filename and not self._filechanged:
            self._positionAsk.wait(constants.PLAYER_ASK_DELAY)
            self._client.updatePlayerStatus(self._paused, self.getCalculatedPosition())
        else:
            self._client.updatePlayerStatus(self._client.getGlobalPaused(), self._client.getGlobalPosition())

    def getCalculatedPosition(self):
        if self._lastVLCPositionUpdate is None:
            return self._client.getGlobalPosition()
        diff = time.time() - self._lastVLCPositionUpdate
        if diff > constants.PLAYER_ASK_DELAY and not self._paused:
            self._client.ui.showDebugMessage("VLC did not response in time, so assuming position is {} ({}+{})".format(
                self._position + diff, self._position, diff))
            if diff > constants.VLC_LATENCY_ERROR_THRESHOLD:
                if not self.shownVLCLatencyError or constants.DEBUG_MODE:
                    self._client.ui.showErrorMessage(getMessage("media-player-latency-warning").format(int(diff)))
                    self.shownVLCLatencyError = True
            return self._position + diff
        else:
            return self._position

    def displayMessage(
        self, message,
        duration=constants.OSD_DURATION * 1000, OSDType=constants.OSD_DURATION, mood=constants.MESSAGE_NEUTRAL
    ):
        duration /= 1000
        if OSDType != constants.OSD_ALERT:
            self._listener.sendLine('display-osd: {}, {}, {}'.format('top-right', duration, message))
        else:
            self._listener.sendLine('display-secondary-osd: {}, {}, {}'.format('center', duration, message))

    def setSpeed(self, value):
        self._listener.sendLine("set-rate: {:.2n}".format(value))

    def setFeatures(self, featureList):
        pass

    def setPosition(self, value):
        self._lastVLCPositionUpdate = time.time()
        self._listener.sendLine("set-position: {}".format(value).replace(".", self.radixChar))

    def setPaused(self, value):
        self._paused = value
        if not value:
            self._lastVLCPositionUpdate = time.time()
        self._listener.sendLine('set-playstate: {}'.format("paused" if value else "playing"))

    def getMRL(self, fileURL):
        if utils.isURL(fileURL):
            fileURL = urllib.parse.quote(fileURL, safe="%/:=&?~#+!$,;'@()*")
            return fileURL

        fileURL = fileURL.replace('\\', '/')
        fileURL = fileURL.encode('utf8')
        fileURL = urllib.parse.quote_plus(fileURL)
        if isWindows():
            fileURL = "file:///" + fileURL
        else:
            fileURL = "file://" + fileURL
        fileURL = fileURL.replace("+", "%20")
        return fileURL

    def openFile(self, filePath, resetPosition=False):
        if not utils.isURL(filePath):
            normedPath = os.path.normpath(filePath)
            if os.path.isfile(normedPath):
                filePath = normedPath
        if utils.isASCII(filePath) and not utils.isURL(filePath):
            self._listener.sendLine('load-file: {}'.format(filePath))
        else:
            fileURL = self.getMRL(filePath)
            self._listener.sendLine('load-file: {}'.format(fileURL))

    def _getFileInfo(self):
        self._listener.sendLine("get-duration")
        self._listener.sendLine("get-filepath")
        self._listener.sendLine("get-filename")

    def lineReceived(self, line):
        # try:
        line = line.decode('utf-8')
        self._client.ui.showDebugMessage("player << {}".format(line))
        # except:
            # pass
        match, name, value = self.RE_ANSWER.match(line), "", ""
        if match:
            name, value = match.group('command'), match.group('argument')

        if line == "filepath-change-notification":
            self._filechanged = True
            t = threading.Thread(target=self._onFileUpdate)
            t.setDaemon(True)
            t.start()
        elif name == "filepath":
            self._filechanged = True
            if value == "no-input":
                self._filepath = None
            else:
                if "file://" in value:
                    value = value.replace("file://", "")
                    if not os.path.isfile(value):
                        value = value.lstrip("/")
                elif utils.isURL(value):
                    value = urllib.parse.unquote(value)
                    # value = value.decode('utf-8')
                self._filepath = value
            self._pathAsk.set()
        elif name == "duration":
            if value == "no-input":
                self._duration = 0
            elif value == "invalid-32-bit-value":
                self._duration = 0
                self.drop(getMessage("vlc-failed-versioncheck"))
            else:
                self._duration = float(value.replace(",", "."))
            self._durationAsk.set()
        elif name == "playstate":
            self._paused = bool(value != 'playing') if (value != "no-input" and self._filechanged == False) else self._client.getGlobalPaused()
            diff = time.time() - self._lastVLCPositionUpdate if self._lastVLCPositionUpdate else 0
            if (
                self._paused == False and
                self._position == self._previousPreviousPosition and
                self._previousPosition == self._position and
                self._duration and
                self._duration > constants.PLAYLIST_LOAD_NEXT_FILE_MINIMUM_LENGTH and
                (self._duration - self._position) < constants.VLC_EOF_DURATION_THRESHOLD and
                diff > constants.VLC_LATENCY_ERROR_THRESHOLD
            ):
                self._client.ui.showDebugMessage("Treating 'playing' response as 'paused' due to VLC EOF bug")
                self.setPaused(True)
            self._pausedAsk.set()
        elif name == "position":
            newPosition = float(value.replace(",", ".")) if (value != "no-input" and not self._filechanged) else self._client.getGlobalPosition()
            if newPosition == self._previousPosition and newPosition != self._duration and self._paused is False:
                self._client.ui.showDebugMessage(
                    "Not considering position {} duplicate as new time because of VLC time precision bug".format(
                        newPosition))
                self._previousPreviousPosition = self._previousPosition
                self._previousPosition = self._position
                self._positionAsk.set()
                return
            self._previousPreviousPosition = self._previousPosition
            self._previousPosition = self._position
            self._position = newPosition
            if self._position < 0 and self._duration > 2147 and self._vlcVersion == "3.0.0":
                self.drop(getMessage("vlc-failed-versioncheck"))
            self._lastVLCPositionUpdate = time.time()
            self._positionAsk.set()
        elif name == "filename":
            self._filechanged = True
            self._filename = value
            self._filenameAsk.set()
        elif line.startswith("vlc-version: "):
            self._vlcVersion = line.split(': ')[1].replace(' ', '-').split('-')[0]
            if not utils.meetsMinVersion(self._vlcVersion, constants.VLC_MIN_VERSION):
                self._client.ui.showErrorMessage(getMessage("vlc-version-mismatch").format(constants.VLC_MIN_VERSION))
            self._vlcready.set()

    @staticmethod
    def run(client, playerPath, filePath, args):
        vlc = VlcPlayer(client, VlcPlayer.getExpandedPath(playerPath), filePath, args)
        return vlc

    @staticmethod
    def getDefaultPlayerPathsList():
        l = []
        for path in constants.VLC_PATHS:
            p = VlcPlayer.getExpandedPath(path)
            if p:
                l.append(p)
        return l

    @staticmethod
    def isValidPlayerPath(path):
        if "vlc" in path.lower() and VlcPlayer.getExpandedPath(path):
            return True
        return False

    @staticmethod
    def getPlayerPathErrors(playerPath, filePath):
        return None

    @staticmethod
    def getIconPath(path):
        return constants.VLC_ICONPATH

    @staticmethod
    def getExpandedPath(playerPath):
        if not os.path.isfile(playerPath):
            if os.path.isfile(playerPath + "vlc.exe"):
                playerPath += "vlc.exe"
                return playerPath
            elif os.path.isfile(playerPath + "\\vlc.exe"):
                playerPath += "\\vlc.exe"
                return playerPath
            elif os.path.isfile(playerPath + "VLCPortable.exe"):
                playerPath += "VLCPortable.exe"
                return playerPath
            elif os.path.isfile(playerPath + "\\VLCPortable.exe"):
                playerPath += "\\VLCPortable.exe"
                return playerPath
        if os.access(playerPath, os.X_OK):
            return playerPath
        for path in os.environ['PATH'].split(':'):
            path = os.path.join(os.path.realpath(path), playerPath)
            if os.access(path, os.X_OK):
                return path

    def drop(self, dropErrorMessage=None):
        if self._listener:
            self._vlcclosed.clear()
            self._listener.sendLine('close-vlc')
            self._vlcclosed.wait()
        self._durationAsk.set()
        self._filenameAsk.set()
        self._pathAsk.set()
        self._positionAsk.set()
        self._vlcready.set()
        self._pausedAsk.set()
        if dropErrorMessage:
            self.reactor.callFromThread(self._client.ui.showErrorMessage, dropErrorMessage, True)
        self.reactor.callFromThread(self._client.stop, False,)

    class __Listener(threading.Thread, asynchat.async_chat):
        def __init__(self, playerController, playerPath, filePath, args, vlcReady, vlcClosed):
            self.__playerController = playerController
            self.requestedVLCVersion = False
            self.vlcHasResponded = False
            self.oldIntfVersion = None
            self.timeVLCLaunched = None
            call = [playerPath]
            if filePath:
                if utils.isASCII(filePath):
                    call.append(filePath)
                else:
                    call.append(self.__playerController.getMRL(filePath))
            if isLinux():
                playerController.vlcIntfPath = "/usr/lib/vlc/lua/intf/"
                playerController.vlcIntfUserPath = os.path.join(os.getenv('HOME', '.'), ".local/share/vlc/lua/intf/")
            elif isMacOS():
                playerController.vlcIntfPath = "/Applications/VLC.app/Contents/MacOS/share/lua/intf/"
                playerController.vlcIntfUserPath = os.path.join(
                    os.getenv('HOME', '.'), "Library/Application Support/org.videolan.vlc/lua/intf/")
            elif isBSD():
                # *BSD ports/pkgs install to /usr/local by default.
                # This should also work for all the other BSDs, such as OpenBSD or DragonFly.
                playerController.vlcIntfPath = "/usr/local/lib/vlc/lua/intf/"
                playerController.vlcIntfUserPath = os.path.join(os.getenv('HOME', '.'), ".local/share/vlc/lua/intf/")
            else:
                playerController.vlcIntfPath = os.path.dirname(playerPath).replace("\\", "/") + "/lua/intf/"
                playerController.vlcIntfUserPath = os.path.join(os.getenv('APPDATA', '.'), "VLC\\lua\\intf\\")
            playerController.vlcModulePath = playerController.vlcIntfPath + "modules/?.luac"
            def _createIntfFolder(vlcSyncplayInterfaceDir):
                self.__playerController._client.ui.showDebugMessage("Checking if syncplay.lua intf directory exists")
                from pathlib import Path
                if os.path.exists(vlcSyncplayInterfaceDir):
                    self.__playerController._client.ui.showDebugMessage("Found syncplay.lua intf directory:'{}'".format(vlcSyncplayInterfaceDir))
                else:
                    self.__playerController._client.ui.showDebugMessage("syncplay.lua intf directory not found, so creating directory '{}'".format(vlcSyncplayInterfaceDir))
                    Path(vlcSyncplayInterfaceDir).mkdir(mode=0o755, parents=True, exist_ok=True)
            def _intfNeedsUpdating(vlcSyncplayInterfacePath):
                self.__playerController._client.ui.showDebugMessage("Checking if '{}' exists and if it is the expected version".format(vlcSyncplayInterfacePath))
                if not os.path.isfile(vlcSyncplayInterfacePath):
                    self.__playerController._client.ui.showDebugMessage("syncplay.lua not found, so file needs copying")
                    return True
                if os.path.isfile(vlcSyncplayInterfacePath):
                    with open(vlcSyncplayInterfacePath, 'rU') as interfacefile:
                        for line in interfacefile:
                            if "local connectorversion" in line:
                                interface_version = line[26:31]
                                if interface_version == constants.VLC_INTERFACE_VERSION:
                                    self.__playerController._client.ui.showDebugMessage("syncplay.lua exists and is expected version, so no file needs copying")
                                    return False
                                else:
                                    self.oldIntfVersion = line[26:31]
                                    self.__playerController._client.ui.showDebugMessage("syncplay.lua is {} but expected version is {} so file needs to be copied".format(interface_version, constants.VLC_INTERFACE_VERSION))
                                    return True
                self.__playerController._client.ui.showDebugMessage("Up-to-dateness checks failed, so copy the file.")
                return True
            if _intfNeedsUpdating(os.path.join(playerController.vlcIntfUserPath, "syncplay.lua")):
                try:
                    _createIntfFolder(playerController.vlcIntfUserPath)
                    copyForm = utils.findResourcePath("syncplay.lua")
                    copyTo = os.path.join(playerController.vlcIntfUserPath, "syncplay.lua")
                    self.__playerController._client.ui.showDebugMessage("Copying VLC Lua Interface from '{}' to '{}'".format(copyForm, copyTo))
                    import shutil
                    if os.path.exists(copyTo):
                        os.chmod(copyTo, 0o755)
                    shutil.copyfile(copyForm, copyTo)
                    os.chmod(copyTo, 0o755)
                except Exception as e:
                    playerController._client.ui.showErrorMessage(e)
                    return
            if isLinux():
                playerController.vlcDataPath = "/usr/lib/syncplay/resources"
            else:
                playerController.vlcDataPath = utils.findWorkingDir() + "\\resources"
            playerController.SLAVE_ARGS.append('--data-path={}'.format(playerController.vlcDataPath))
            playerController.SLAVE_ARGS.append(
                '--lua-config=syncplay={{modulepath=\"{}\",port=\"{}\"}}'.format(
                    playerController.vlcModulePath, str(playerController.vlcport)))

            call.extend(playerController.SLAVE_ARGS)
            if args:
                call.extend(args)

            self._vlcready = vlcReady
            self._vlcclosed = vlcClosed
            self._vlcVersion = None

            if isWindows() and getattr(sys, 'frozen', '') and getattr(sys, '_MEIPASS', '') is not None:  # Needed for pyinstaller --onefile bundle
                self.__process = subprocess.Popen(
                    call, stdin=subprocess.PIPE, stderr=subprocess.PIPE, stdout=subprocess.PIPE,
                    shell=False, creationflags=0x08000000)
            else:
                self.__process = subprocess.Popen(call, stderr=subprocess.PIPE, stdout=subprocess.PIPE)
            self.timeVLCLaunched = time.time()
            if self._shouldListenForSTDOUT():
                for line in iter(self.__process.stderr.readline, ''):
                    line = line.decode('utf-8')
                    self.vlcHasResponded = True
                    self.timeVLCLaunched = None
                    if "[syncplay]" in line:
                        if "Listening on host" in line:
                            break
                        if "Hosting Syncplay" in line:
                            break
                        elif "Couldn't find lua interface" in line:
                            playerController._client.ui.showErrorMessage(
                                getMessage("vlc-failed-noscript").format(line), True)
                            break
                        elif "lua interface error" in line:
                            playerController._client.ui.showErrorMessage(
                                getMessage("media-player-error").format(line), True)
                            break
            if not isMacOS():
                self.__process.stderr = None
            else:
                vlcoutputthread = threading.Thread(target=self.handle_vlcoutput, args=())
                vlcoutputthread.setDaemon(True)
                vlcoutputthread.start()
            threading.Thread.__init__(self, name="VLC Listener")
            asynchat.async_chat.__init__(self)
            self.set_terminator(b'\n')
            self._ibuffer = []
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sendingData = threading.Lock()

        def _shouldListenForSTDOUT(self):
            return not isWindows()

        def initiate_send(self):
            with self._sendingData:
                asynchat.async_chat.initiate_send(self)

        def run(self):
            self._vlcready.clear()
            self.connect(('localhost', self.__playerController.vlcport))
            asyncore.loop()

        def handle_connect(self):
            asynchat.async_chat.handle_connect(self)
            self._vlcready.set()
            self.timeVLCLaunched = None

        def collect_incoming_data(self, data):
            self._ibuffer.append(data)

        def handle_close(self):
            if self.timeVLCLaunched and time.time() - self.timeVLCLaunched < constants.VLC_OPEN_MAX_WAIT_TIME:
                try:
                    self.__playerController._client.ui.showDebugMessage("Failed to connect to VLC, but reconnecting as within max wait time")
                except:
                    pass
                self.run()
            elif self.vlcHasResponded:
                asynchat.async_chat.handle_close(self)
                self.__playerController.drop()
            else:
                self.vlcHasResponded = True
                asynchat.async_chat.handle_close(self)
                self.__playerController.drop(getMessage("vlc-failed-connection").format(constants.VLC_MIN_VERSION))

        def handle_vlcoutput(self):
            out = self.__process.stderr
            for line in iter(out.readline, ''):
                line = line.decode('utf-8')
                if '[syncplay] core interface debug: removing module' in line:
                    self.__playerController.drop()
                    break
            out.close()

        def found_terminator(self):
            self.vlcHasResponded = True
            self.__playerController.lineReceived(b"".join(self._ibuffer))
            self._ibuffer = []

        def sendLine(self, line):
            if self.connected:
                if not self.requestedVLCVersion:
                    self.requestedVLCVersion = True
                    self.sendLine("get-vlc-version")
                # try:
                lineToSend = line + "\n"
                self.push(lineToSend.encode('utf-8'))
                if self.__playerController._client and self.__playerController._client.ui:
                    self.__playerController._client.ui.showDebugMessage("player >> {}".format(line))
                # except:
                    # pass
            if line == "close-vlc":
                self._vlcclosed.set()
                if not self.connected and not self.timeVLCLaunched:
                    # For circumstances where Syncplay is not connected to VLC and is not reconnecting
                    try:
                        self.__process.terminate()
                    except:  # When VLC is already closed
                        pass

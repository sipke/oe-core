# Copyright (C) 2013 Intel Corporation
#
# Released under the MIT license (see COPYING.MIT)

# This module provides a class for starting qemu images using runqemu.
# It's used by testimage.bbclass.

import subprocess
import os
import time
import signal
import re
import socket
import select
import bb

import importlib

class QemuRunner:

    def __init__(self, machine, rootfs, display, tmpdir, deploy_dir_image, logfile, boottime, nativesysroot):

        # Popen object for runqemu
        self.runqemu = None
        # pid of the qemu process that runqemu will start
        self.qemupid = None
        # target ip - from the command line
        self.ip = None
        # Port to be used for connection (ssh)
        self.port = None
        # host ip - where qemu is running
        self.server_ip = None
        # ssh user
        self.user = "root"

        self.machine = machine
        self.rootfs = rootfs
        self.display = display
        self.tmpdir = tmpdir
        self.deploy_dir_image = deploy_dir_image
        self.logfile = logfile
        self.boottime = boottime

        self.runqemutime = 60

        self.nativesysroot = nativesysroot

        self.create_socket()

    def ips_set(self):
        return self.ip != None and self.server != None

    def get_ips(self):
        # If we have no ips set yet, try to extract them from the command line
        if self.ip is None or self.server_ip is None:
            cmdline = ''
            with open('/proc/%s/cmdline' % self.qemupid) as p:
                cmdline = p.read()
            ips = re.findall("((?:[0-9]{1,3}\.){3}[0-9]{1,3})", cmdline.split("ip=")[1])
            if not ips or len(ips) != 3:
                bb.note("Couldn't get ip from qemu process arguments! Here is the qemu command line used: %s" % cmdline)
                return False
            else:
                self.ip = ips[0]
                self.server_ip = ips[1]
                return True
        # We already have our ips, no need to extract them from the command line
        else:
            return True

    def create_socket(self):

        self.bootlog = ''
        self.qemusock = None

        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setblocking(0)
            self.server_socket.bind(("127.0.0.1",0))
            self.server_socket.listen(2)
            self.serverport = self.server_socket.getsockname()[1]
            bb.note("Created listening socket for qemu serial console on: 127.0.0.1:%s" % self.serverport)
        except socket.error, msg:
            self.server_socket.close()
            bb.fatal("Failed to create listening socket: %s" %msg[1])


    def log(self, msg):
        if self.logfile:
            with open(self.logfile, "a") as f:
                f.write("%s" % msg)

    # Look for a Qemu starter class for the given machine
    # (likely in the given machine's BSP layer) and launch it
    # Then obtain the IPs and ports to use for host and device under test. This
    # will allow (for example) qemu to use port redirect for networking rather
    # than tap if it so choses
    # Allow the qemu starter to define a user for the ssh connection from host
    # to device under test. User should be set to None if ssh should be called
    # without user (i.e. current user will be used)
    def trylaunchstarter(self):
        starterexists = False
        try:
            startermodulename = "oeqa.utils.{0}starter".format(self.machine)
            startername = "{0}starter".format(self.machine).capitalize()
            startermodule = __import__(startermodulename, globals(), locals(), [startername])
            starterclass = getattr(startermodule, startername)
            starterinstance = starterclass()
            starterexists = True
            bb.note("Launching qemu using starter: %s" % startermodulename)
        except:
            # Ignore error as it is a valid condition that there is no starter
            bb.note("No qemu starter found, need to default to runqemu")
        if starterexists:
            try:
                # serial redirect is required by this test framework, so pass
                # this qemu parameter onto the qemu runner
                qemuextraparams = "-serial tcp:127.0.0.1:%s" % self.serverport
                self.runqemu = starterinstance.launch(self.nativesysroot, self.deploy_dir_image, qemuextraparams, self.machine, self.rootfs)
                self.qemupid = self.runqemu.pid
                bb.note("Qemu started qemupid %s" % self.qemupid)
                self.ip = starterinstance.ip
                self.port = starterinstance.port
                self.server_ip = starterinstance.server_ip
                self.user = starterinstance.user
            except:
                bb.note("Failed to launch qemu starter: %s" % startermodulename)

        return starterexists

    def launch_runqemu(self, qemuparams):
        self.qemuparams = 'bootparams="console=tty1 console=ttyS0,115200n8" qemuparams="-serial tcp:127.0.0.1:%s"' % self.serverport
        if qemuparams:
            self.qemuparams = self.qemuparams[:-1] + " " + qemuparams + " " + '\"'
        launch_cmd = 'runqemu %s %s %s' % (self.machine, self.rootfs, self.qemuparams)
        self.runqemu = subprocess.Popen(launch_cmd,shell=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,preexec_fn=os.setpgrp)
        bb.note("runqemu started, pid is %s" % self.runqemu.pid)
        bb.note("waiting at most %s seconds for qemu pid" % self.runqemutime)


    def start(self, qemuparams = None):

        if self.display:
            os.environ["DISPLAY"] = self.display
        else:
            bb.error("To start qemu I need a X desktop, please set DISPLAY correctly (e.g. DISPLAY=:1)")
            return False
        if not os.path.exists(self.rootfs):
            bb.error("Invalid rootfs %s" % self.rootfs)
            return False
        if not os.path.exists(self.tmpdir):
            bb.error("Invalid TMPDIR path %s" % self.tmpdir)
            return False
        else:
            os.environ["OE_TMPDIR"] = self.tmpdir
        if not os.path.exists(self.deploy_dir_image):
            bb.error("Invalid DEPLOY_DIR_IMAGE path %s" % self.deploy_dir_image)
            return False
        else:
            os.environ["DEPLOY_DIR_IMAGE"] = self.deploy_dir_image

        # Set this flag so that Qemu doesn't do any grabs as SDL grabs interact
        # badly with screensavers.
        os.environ["QEMU_DONT_GRAB"] = "1"

        # If a machine/bsp layer provides a qemu starter class named
        # <Machine>starter (e.g. Qemuarmstarter) then allow it to launch the
        # qemu process, otherwise launch runqemu as per normal.
        if not self.trylaunchstarter():
            self.launch_runqemu(qemuparams)

        if self.runqemu is None:
            bb.fatal("Failed to launch Qemu starter or runqemu for machine: " % self.machine)
            return False

        endtime = time.time() + self.runqemutime
        while not self.is_alive() and time.time() < endtime:
            time.sleep(1)

        if self.is_alive():
            bb.note("qemu started - qemu procces pid is %s" % self.qemupid)
            if not self.get_ips():
                self.stop()
            bb.note("Target IP: %s" % self.ip)
            bb.note("Server IP: %s" % self.server_ip)
            bb.note("Waiting at most %d seconds for login banner" % self.boottime )
            endtime = time.time() + self.boottime
            socklist = [self.server_socket]
            reachedlogin = False
            stopread = False
            while time.time() < endtime and not stopread:
                sread, swrite, serror = select.select(socklist, [], [], 5)
                for sock in sread:
                    if sock is self.server_socket:
                        self.qemusock, addr = self.server_socket.accept()
                        self.qemusock.setblocking(0)
                        socklist.append(self.qemusock)
                        socklist.remove(self.server_socket)
                        bb.note("Connection from %s:%s" % addr)
                    else:
                        data = sock.recv(1024)
                        if data:
                            self.log(data)
                            self.bootlog += data
                            lastlines = "\n".join(self.bootlog.splitlines()[-2:])
                            if re.search("login:", lastlines):
                                stopread = True
                                reachedlogin = True
                                bb.note("Reached login banner")
                        else:
                            socklist.remove(sock)
                            sock.close()
                            stopread = True

            if not reachedlogin:
                bb.note("Target didn't reached login boot in %d seconds" % self.boottime)
                lines = "\n".join(self.bootlog.splitlines()[-5:])
                bb.note("Last 5 lines of text:\n%s" % lines)
                bb.note("Check full boot log: %s" % self.logfile)
                self.stop()
                return False
        else:
            bb.note("Qemu pid didn't appeared in %s seconds" % self.runqemutime)
            output = self.runqemu.stdout
            self.stop()
            bb.note("Output from runqemu:\n%s" % output.read())
            return False

        return self.is_alive()

    def stop(self):

        if self.runqemu:
            bb.note("Sending SIGTERM to runqemu")
            os.kill(-self.runqemu.pid,signal.SIGTERM)
            endtime = time.time() + self.runqemutime
            while self.runqemu.poll() is None and time.time() < endtime:
                time.sleep(1)
            if self.runqemu.poll() is None:
                bb.note("Sending SIGKILL to runqemu")
                os.kill(-self.runqemu.pid,signal.SIGKILL)
            self.runqemu = None
        if self.server_socket:
            self.server_socket.close()
            self.server_socket = None
        self.qemupid = None
        self.ip = None

    def restart(self, qemuparams = None):
        bb.note("Restarting qemu process")
        if self.runqemu.poll() is None:
            self.stop()
        self.create_socket()
        if self.start(qemuparams):
            return True
        return False

    def is_alive(self):
        if self.qemupid is None:
            qemu_child = self.find_child(str(self.runqemu.pid))
            if qemu_child:
                self.qemupid = qemu_child[0]
                if os.path.exists("/proc/" + str(self.qemupid)):
                    return True
            return False
        else:
            return True


    def find_child(self,parent_pid):
        #
        # Walk the process tree from the process specified looking for a qemu-system. Return its [pid'cmd]
        #
        ps = subprocess.Popen(['ps', 'axww', '-o', 'pid,ppid,command'], stdout=subprocess.PIPE).communicate()[0]
        processes = ps.split('\n')
        nfields = len(processes[0].split()) - 1
        pids = {}
        commands = {}
        for row in processes[1:]:
            data = row.split(None, nfields)
            if len(data) != 3:
                continue
            if data[1] not in pids:
                pids[data[1]] = []

            pids[data[1]].append(data[0])
            commands[data[0]] = data[2]

        if parent_pid not in pids:
            return []

        parents = []
        newparents = pids[parent_pid]
        while newparents:
            next = []
            for p in newparents:
                if p in pids:
                    for n in pids[p]:
                        if n not in parents and n not in next:
                            next.append(n)
                if p not in parents:
                    parents.append(p)
                    newparents = next
        #print "Children matching %s:" % str(parents)
        for p in parents:
            # Need to be careful here since runqemu-internal runs "ldd qemu-system-xxxx"
            # Also, old versions of ldd (2.11) run "LD_XXXX qemu-system-xxxx"
            basecmd = commands[p].split()[0]
            basecmd = os.path.basename(basecmd)
            if "qemu-system" in basecmd and "-serial tcp" in commands[p]:
                return [int(p),commands[p]]

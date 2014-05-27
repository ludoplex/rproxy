#!/usr/bin/env python

# Remote Proxy for TiVo, v0.3
# Copyright 2014 William McBrine
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You didn't receive a copy of the license with this program because 
# you already have dozens of copies, don't you? If not, visit gnu.org.

""" Remote Proxy for TiVo

    This is a server that connects to the "Crestron" interface on a
    Series 3 or later TiVo, and reflects the port back out, allowing
    multiple simultaneous connections. (The TiVo allows only one.)
    Commands are queued from all sources, and sent to the TiVo no more
    often than once every tenth of a second, avoiding overload. Status
    responses are sent back to all connected clients. In other words, it
    works like the spec says the TiVo service is supposed to. :)

    Command-line options:

    -a, --address      Specify the address to serve from. The default is
                       '' (bind to all interfaces).

    -p, --port         Specify the port to serve from. The default is
                       31339, the standard TiVo "Crestron" remote port.

    -l, --list         List TiVos found on the network, and exit.

    -i, --interactive  List TiVos found, and prompt which to connect to.

    -z, --nozeroconf   Disable Zeroconf announcements.

    -v, --verbose      Echo messages to and from the TiVo to the console.
                       (In combination with -l, show extended details.)

    -h, --help         Print help and exit.

    <address>          Any other command-line option is treated as the IP
                       address (with optional port number) of the TiVo to
                       connect to. This is a required parameter, except
                       with -l or -h.

"""

__author__ = 'William McBrine <wmcbrine@gmail.com>'
__version__ = '0.3'
__license__ = 'GPL'

import getopt
import socket
import sys
import thread
import time

from Queue import Queue

have_zc = True
try:
    import Zeroconf
except:
    have_zc = False

DEFAULT_HOST = ('', 31339)
SERVICE = '_tivo-remote._tcp.local.'

class ZCListener:
    def __init__(self, names):
        self.names = names

    def removeService(self, server, type, name):
        self.names.remove(name)

    def addService(self, server, type, name):
        self.names.append(name)

class ZCBroadcast:
    def __init__(self):
        self.rz = Zeroconf.Zeroconf()

    def start(self, target, addr):
        host, port = addr
        host_ip = self.get_address(host)
        tivos = self.find_tivos()
        if target in tivos:
            name, prop = tivos[target]
        else:
            name = target[0]
            prop = {'TSN': '648000000000000', 'path': '/',
                    'protocol': 'tivo-remote', 'swversion': '0.0',
                    'platform': 'tcd/Series3'}
        name = 'Proxy(%s)' % name

        self.info = Zeroconf.ServiceInfo(SERVICE, '%s.%s' % (name, SERVICE),
                                         host_ip, port, 0, 0, prop)
        self.rz.registerService(self.info)

    def find_tivos(self, all=False):
        """ Get the records of TiVos offering remote control. """
        tivos = {}
        tivo_names = []

        try:
            browser = Zeroconf.ServiceBrowser(self.rz, SERVICE,
                                              ZCListener(tivo_names))
        except:
            return tivos

        time.sleep(1)    # Give them a second to respond

        if not all:
            # For proxied TiVos, remove the original
            for t in tivo_names[:]:
                if t.startswith('Proxy('):
                    try:
                        t = t.replace('.' + SERVICE, '')[6:-1] + '.' + SERVICE
                        tivo_names.remove(t)
                    except:
                        pass

        # Now get the addresses and properties -- this is the slow part
        for t in tivo_names:
            s = self.rz.getServiceInfo(SERVICE, t)
            if s:
                name = t.replace('.' + SERVICE, '')
                address = socket.inet_ntoa(s.getAddress())
                port = s.getPort()
                prop = s.getProperties()
                tivos[(address, port)] = (name, prop)

        if not all:
            # For proxies with numeric names, remove the original
            for t in tivo_names:
                if t.startswith('Proxy('):
                    address = t.replace('.' + SERVICE, '')[6:-1]
                    for key in tivos.keys()[:]:
                        if key[0] == address:
                            tivos.pop(key)
        return tivos

    def shutdown(self):
        self.rz.unregisterService(self.info)
        self.stop()

    def stop(self):
        self.rz.close()

    def get_address(self, host):
        if not host:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('4.2.2.1', 123))
            host = s.getsockname()[0]
        return socket.inet_aton(host)

def process_queue(tivo, queue, verbose):
    """ Pop commands from the queue and send them to the TiVo. Wait
        100ms between messages to avoid a bit jam.

    """
    while True:
        msg, address = queue.get()
        if verbose:
            sys.stderr.write('%s: %s\n' % (address, msg))
        try:
            tivo.sendall(msg)
        except:
            break
        time.sleep(0.1)

def read_client(queue, client, address):
    """ Read commands from a client remote control program, and put them
        in the queue. Run until the client disconnects.

    """
    while True:
        try:
            msg = client.recv(1024)
        except:
            break
        if not msg:
            break
        queue.put((msg, address))
    try:
        client.close()
    except:
        pass

def status_update(tivo, listeners, address, verbose):
    """ Read status response messages from the TiVo, and send them to
        each connected client.

    """
    while True:
        try:
            status = tivo.recv(1024)
        except:
            status = ''
        if not status:
            try:
                tivo.close()
            except:
                pass
            break
        if verbose:
            sys.stderr.write('%s: %s\n' % (address, status))
        for l in listeners[:]:
            try:
                l.sendall(status)
            except:
                listeners.remove(l)

def connect(target):
    """ Connect to the target TiVo within five seconds, or abort. """
    try:
        tivo = socket.socket()
        tivo.settimeout(5)
        tivo.connect(target)
        tivo.settimeout(None)
    except:
        raise
    return tivo

def serve(queue, listeners, host_port):
    """ Listen for connections from client remote control programs;
        start new read_client() threads and add listeners as needed.
        Serve until KeyboardInterrupt.

    """
    server = socket.socket()
    server.bind(host_port)
    server.listen(5)

    try:
        while True:
            client, address = server.accept()
            listeners.append(client)
            thread.start_new_thread(read_client, (queue, client, address))
    except KeyboardInterrupt:
        pass

def cleanup(tivo, queue, listeners):
    """ Close all sockets, and push one last message to make the
        process_queue() thread exit.

    """
    for l in [tivo] + listeners:
        try:
            l.close()
        except:
            pass

    queue.put(('', ''))

def parse_cmdline(params):
    """ Parse the command-line options, and return tuples for host and
        target addresses, plus the verbose flag.

    """
    host, port = DEFAULT_HOST
    use_zc = have_zc
    verbose = False
    tlist = False
    tselect = False

    try:
        opts, t_address = getopt.getopt(params, 'a:p:lizvh', ['address=',
                                        'port=', 'list', 'interactive',
                                        'nozeroconf', 'verbose', 'help'])
    except getopt.GetoptError, msg:
        sys.stderr.write('%s\n' % msg)

    for opt, value in opts:
        if opt in ('-a', '--address'):
            host = value
        elif opt in ('-p', '--port'):
            port = int(value)
        elif opt in ('-l', '--list'):
            tlist = True
        elif opt in ('-i', '--interactive'):
            tselect = True
        elif opt in ('-z', '--nozeroconf'):
            use_zc = False
        elif opt in ('-v', '--verbose'):
            verbose = True
        elif opt in ('-h', '--help'):
            print __doc__
            sys.exit()

    if tlist or tselect:
        return (), (host, port), True, verbose, tlist, tselect

    t_address = t_address[0]
    if ':' in t_address:
        t_address, t_port = address.split(':')
        t_port = int(t_port)
    else:
        t_port = DEFAULT_HOST[1]

    return (t_address, t_port), (host, port), use_zc, verbose, tlist, tselect

def proxy(target, host_port=DEFAULT_HOST, use_zc=True, verbose=False):
    queue = Queue()
    listeners = []
    tivo = connect(target)
    thread.start_new_thread(process_queue, (tivo, queue, verbose))
    thread.start_new_thread(status_update, (tivo, listeners, target, verbose))
    if use_zc:
        zc = ZCBroadcast()
        zc.start(target, host_port)
    serve(queue, listeners, host_port)
    if use_zc:
        zc.shutdown()
    cleanup(tivo, queue, listeners)

def scan(verbose):
    try:
        zc = ZCBroadcast()
    except:
        sys.stderr.write('-l requires Zeroconf\n')
        sys.exit(1)
    tivos = zc.find_tivos(True)
    zc.stop()
    for key, data in tivos.items():
        name, prop = data
        print '%s:%d -' % key, name
        if verbose:
            for pkey, pdata in prop.items():
                print ' %s: %s' % (pkey, pdata)
            print

def choose(host_port, verbose):
    try:
        zc = ZCBroadcast()
    except:
        sys.stderr.write('-i requires Zeroconf\n')
        sys.exit(1)
    tivos = zc.find_tivos()
    zc.stop()
    choices = {}
    i = 1
    for key, data in tivos.items():
        choices[str(i)] = key
        name, prop = data
        print '%d.' % i,
        print '%s:%d -' % key, name
        i += 1
    choice = raw_input('Connect to which? ')
    if choice in choices:
        proxy(choices[choice], host_port, True, verbose)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.stderr.write('Must specify an address\n')
        sys.exit(1)

    (target, host_port, use_zc,
     verbose, tlist, tselect) = parse_cmdline(sys.argv[1:])
    if tselect:
        choose(host_port, verbose)
    elif tlist:
        scan(verbose)
    else:
        proxy(target, host_port, use_zc, verbose)

#!/usr/bin/python

import os
import json
import re
import socket
from optparse import OptionParser
from functools import partial
from subprocess import call, check_output

from mininet.net import Mininet
from mininet.topo import Topo
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.util import run, quietRun
from mininet.log import setLogLevel, info, warn, error, debug
from mininet.cli import CLI

# Parse command line options and dump results
def parseOptions():
    "Parse command line options"
    parser = OptionParser()
    parser.add_option('--spine', dest='spine', type='int', default=2,
        help='number of spine switches, default=2')
    parser.add_option('--leaf', dest='leaf', type='int', default=2, 
        help='number of leaf switches, default=2')
    parser.add_option('--fanout', dest='fanout', type='int', default=2,
        help='number of hosts per leaf switch, default=2')
    parser.add_option('--collector', dest='collector', default='127.0.0.1',
        help='IP address of sFlow collector, default=127.0.0.1')
    parser.add_option('--controller', dest='controller', default='127.0.0.1',
        help='IP address of controller, default=127.0.0.1')
    parser.add_option('--topofile', dest='topofile', default='topology.txt',
        help='file used to write out topology, default topology.txt')
    (options, args) = parser.parse_args()
    return options, args

opts, args = parseOptions()

class LeafAndSpine(Topo):
    # spine = number of spine switches
    # leaf = number of leaf switches which are connected to all of the spines
    # fanout = number of hosts per leaf switch
    # Each host is given a /16 mask but the address is really a /24 with the
    # third octet being different between hosts of different leaves.
    # This means that the spines can route by /24.
    # because of the the way we allocate MAC and IP addresses
    # leaf and fanout must be < 255
    def __init__(self, spine=2, leaf=2, fanout=2, **opts):
        "Create custom topo."

        # Initialize topology
        Topo.__init__(self, **opts)
        # Initialize  spine switches
        spines = {}
        for s in range(spine):
            spines[s] = self.addSwitch('s%s' % (s + 1), protocols='OpenFlow13')
        # set link speeds to 10Mb/s
        linkopts = dict(bw=10)
        for ls in range(leaf):
            leafSwitch = self.addSwitch('s%s' % (spine+ls+1), protocols='OpenFlow13')
            # now connect the leaf to all the spines
            for s in range(spine):
                switch = spines[s]
                self.addLink(leafSwitch, switch, **linkopts)
            # Add hosts and leaf switches, fanout hosts per leaf switch 
            for f in range(fanout):
                host = self.addHost('h%s' % (ls*fanout+f+1), ip='10.0.%s.%s/16' % (ls, (f+1)))
                self.addLink(host, leafSwitch, **linkopts)

def configMulticast(spine, leaf, fanout):
    for ls in range(leaf):
        switch = 's%s' % (spine+ls+1)
        # disable flood on all but one of the uplinks
        for p in range(2, spine+1):
            # the uplinks are added first so numbered from 1
            call(['ovs-ofctl', 'mod-port', switch, str(p), 'noflood']) 
        # leaf switches flood multicasts (including ARP)
        call(['ovs-ofctl', 'add-flow', switch, 'dl_dst=01:00:00:00:00:00/01:00:00:00:00:00 priority=400 actions=flood'])
    for s in range(spine):
        # spine switches flood multicasts
        switch = 's%s' % (s+1)
        call(['ovs-ofctl', 'add-flow', switch, 'dl_dst=01:00:00:00:00:00/01:00:00:00:00:00 priority=400 actions=flood'])

#Configure OVS forwarding, multipathing for non-local hosts
def configUnicast(net, spine, leaf, fanout):
    for ls in range(leaf):
        lsname = 's%s' % (spine+ls+1)
        for f in range(fanout):
            # host.MAC() returns IP so we set the MAC and use in flow here!
            mac = '00:04:00:00:{:02x}:{:02x}'.format (ls, (f+1))
            host = net.get('h%s' % (ls*fanout+f+1))
            host.setMAC(mac)
            # rule for hosts connected to this leaf switch
            call(['ovs-ofctl', 'add-flow', lsname, 'dl_dst=%s priority=500 actions=output:%s' % (mac, (f+1+spine))])
            # now add the multipathing rules for hosts not connected to the leaf switch
            # note that symmetric_l4 hash uses ip and tcp field, but not udp
            # so udp flows between a pair of hosts will always use same link. 
            slaves = '1'
            if (spine > 1): 
                for x in range(2,spine+1): slaves=slaves+','+str(x)
            call(['ovs-ofctl', 'add-flow', lsname, 'priority=300 actions=bundle(symmetric_l4,%s,hrw,ofport,slaves:%s' % (ls+1, slaves)])
            # now tell the spines about the hosts
            for s in range(spine):
                sname = 's%s' % (s+1)
                call(['ovs-ofctl', 'add-flow', sname, 'dl_dst=%s priority=500 actions=output:%s' % (mac, str(ls+1))])
        
#Configure sFlow on OVS using the specified collector and ifname whose interface
#is to be used as agent address
def configSFlow(spine, leaf, collector, ifname):
    sflow = 'ovs-vsctl -- --id=@sflow create sflow agent=%s target=%s sampling=10 polling=20 --' % (ifname, collector)
    for s in range(1,spine+1):
        sflow += ' -- set bridge s%s sflow=@sflow' % s
    for ls in range(1,leaf+1):
        sflow += ' -- set bridge s%s sflow=@sflow' % (spine+ls)
    info('*** Configuring sFlow collector=%s \n' % collector)
    quietRun(sflow)

# Looks up the interface used to when sending to ip address and returns
# interface name and associated IP address.    
def getIfInfo(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((ip, 0))
    ip = s.getsockname()[0]
    ifconfig = check_output(['ifconfig'])
    ifs = re.findall(r'^(\S+).*?inet addr:(\S+).*?', ifconfig, re.S|re.M)
    for entry in ifs:
        if entry[1] == ip:
            return entry
    
#write out the topology to topofile    
def dumpTopology(net, agent, topofile):
    topoData = {'nodes': {}, 'links': {}}
    for s in net.switches:
        topoData['nodes'][s.name] = {'name': s.name, 'dpid': s.dpid, 'ports': {},'agent': agent}
    path = '/sys/devices/virtual/net/'
    for child in os.listdir(path):
        parts = re.match('(^s[0-9]+)-(.*)', child)
        if parts == None: continue
        ifindex = open(path+child+'/ifindex').read().split('\n',1)[0]
        topoData['nodes'][parts.group(1)]['ports'][child] = {'name': child, 'ifindex': ifindex}
    i = 0
    for s1 in net.switches:
        j = 0
        for s2 in net.switches:
            if j > i:
                intfs = s1.connectionsTo(s2)
                for intf in intfs:
                    s1ifIdx = topoData['nodes'][s1.name]['ports'][intf[0].name]['ifindex']
                    s2ifIdx = topoData['nodes'][s2.name]['ports'][intf[1].name]['ifindex']
                    linkName = '%s-%s' % (s1.name, s2.name)
                    info('topology link %s: %s %s %s %s %s %s\n' % (linkName, s1, intf[0].name, s1ifIdx, s2, intf[1].name, s2ifIdx))
                    topoData['links'][linkName] = {'node1': s1.name, 'port1': intf[0].name, 'ifindex1': s1ifIdx, 'node2': s2.name, 'port2': intf[1].name, 'ifindex2': s2ifIdx}
            j += 1
        i += 1
    #now identify the leaf/edge switches
    for h in net.hosts:
        for s in net.switches:
            intfs = h.connectionsTo(s)
            if intfs:
                topoData['nodes'][s.name]['tag'] = 'edge'
    f = open(topofile, 'w')
    f.write(json.dumps(topoData))
    f.flush()
    f.close  

def config(opts):
    spine = opts.spine
    leaf = opts.leaf
    fanout = opts.fanout
    controller = opts.controller
    collector = opts.collector
#(ifname, agent) = getIfInfo(collector)
    topo = LeafAndSpine(spine=spine, leaf=leaf, fanout=fanout)
    net = Mininet(topo=topo, link=TCLink, controller=lambda name: RemoteController(name, ip=controller))
    net.start()
#configMulticast(spine=spine, leaf=leaf, fanout=fanout)
#configUnicast(net=net, spine=spine, leaf=leaf, fanout=fanout)
#configSFlow(spine=spine, leaf=leaf, collector=collector, ifname=ifname)
#dumpTopology(net=net, agent=agent, topofile=opts.topofile)
    CLI(net)
    net.stop()
    
if __name__ == '__main__':
    setLogLevel('info')
    config(opts)
    os.system('sudo mn -c')


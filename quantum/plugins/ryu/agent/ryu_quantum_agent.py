#!/usr/bin/env python
# vim: tabstop=4 shiftwidth=4 softtabstop=4
#
# Copyright 2012 Isaku Yamahata <yamahata at private email ne jp>
# Based on openvswitch agent.
#
# Copyright 2011 Nicira Networks, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
# @author: Isaku Yamahata

import httplib
import socket
import sys

import netifaces
from ryu.app import client
from ryu.app import conf_switch_key
from ryu.app import rest_nw_id

from quantum.agent.linux import ovs_lib
from quantum.agent.linux.ovs_lib import VifPort
from quantum.agent import rpc as agent_rpc
from quantum.common import config as logging_config
from quantum.common import exceptions as q_exc
from quantum.common import topics
from quantum import context as q_context
from quantum.openstack.common import cfg
from quantum.openstack.common.cfg import NoSuchGroupError
from quantum.openstack.common.cfg import NoSuchOptError
from quantum.openstack.common import log
from quantum.plugins.ryu.common import config


LOG = log.getLogger(__name__)


# This is copied of nova.flags._get_my_ip()
# Agent shouldn't depend on nova module
def _get_my_ip():
    """
    Returns the actual ip of the local machine.

    This code figures out what source address would be used if some traffic
    were to be sent out to some well known address on the Internet. In this
    case, a Google DNS server is used, but the specific address does not
    matter much.  No traffic is actually sent.
    """
    csock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    csock.connect(('8.8.8.8', 80))
    (addr, _port) = csock.getsockname()
    csock.close()
    return addr


def _get_ip(cfg_ip_str, cfg_interface_str):
    ip = None
    try:
        ip = getattr(cfg.CONF.OVS, cfg_ip_str)
    except (NoSuchOptError, NoSuchGroupError):
        pass
    if ip:
        return ip

    iface = None
    try:
        iface = getattr(cfg.CONF.OVS, cfg_interface_str)
    except (NoSuchOptError, NoSuchGroupError):
        pass
    if iface:
        iface = netifaces.ifaddresses(iface)[netifaces.AF_INET][0]
        return iface['addr']

    return _get_my_ip()


def _get_tunnel_ip():
    return _get_ip('tunnel_ip', 'tunnel_interface')


def _get_ovsdb_ip():
    return _get_ip('ovsdb_ip', 'ovsdb_interface')


class OVSBridge(ovs_lib.OVSBridge):
    def __init__(self, br_name, root_helper):
        ovs_lib.OVSBridge.__init__(self, br_name, root_helper)
        self.datapath_id = None

    def find_datapath_id(self):
        self.datapath_id = self.get_datapath_id()

    def set_manager(self, target):
        self.run_vsctl(["set-manager", target])

    def get_ofport(self, name):
        return self.db_get_val("Interface", name, "ofport")

    def _get_ports(self, get_port):
        ports = []
        port_names = self.get_port_name_list()
        for name in port_names:
            if self.get_ofport(name) < 0:
                continue
            port = get_port(name)
            if port:
                ports.append(port)

        return ports

    def _get_external_port(self, name):
        # exclude vif ports
        external_ids = self.db_get_map("Interface", name, "external_ids")
        if external_ids:
            return

        # exclude tunnel ports
        options = self.db_get_map("Interface", name, "options")
        if "remote_ip" in options:
            return

        ofport = self.get_ofport(name)
        return VifPort(name, ofport, None, None, self)

    def get_external_ports(self):
        return self._get_ports(self._get_external_port)


class VifPortSet(object):
    def __init__(self, int_br, ryu_rest_client):
        super(VifPortSet, self).__init__()
        self.int_br = int_br
        self.api = ryu_rest_client

    def setup(self):
        for port in self.int_br.get_external_ports():
            LOG.debug(_('External port %s'), port)
            self.api.update_port(rest_nw_id.NW_ID_EXTERNAL,
                                 port.switch.datapath_id, port.ofport)


class RyuPluginApi(agent_rpc.PluginApi):
    def get_ofp_rest_api_addr(self, context):
        LOG.debug(_("Get Ryu rest API address"))
        return self.call(context,
                         self.make_msg('get_ofp_rest_api'),
                         topic=self.topic)


class OVSQuantumOFPRyuAgent(object):
    def __init__(self, integ_br, tunnel_ip, ovsdb_ip, ovsdb_port,
                 root_helper):
        super(OVSQuantumOFPRyuAgent, self).__init__()
        self._setup_rpc()
        self._setup_integration_br(root_helper, integ_br, tunnel_ip,
                                   ovsdb_port, ovsdb_ip)

    def _setup_rpc(self):
        self.plugin_rpc = RyuPluginApi(topics.PLUGIN)
        self.context = q_context.get_admin_context_without_session()

    def _setup_integration_br(self, root_helper, integ_br,
                              tunnel_ip, ovsdb_port, ovsdb_ip):
        self.int_br = OVSBridge(integ_br, root_helper)
        self.int_br.find_datapath_id()

        rest_api_addr = self.plugin_rpc.get_ofp_rest_api_addr(self.context)
        if not rest_api_addr:
            raise q_exc.Invalid(_("Ryu rest API port isn't specified"))
        LOG.debug(_("Going to ofp controller mode %s"), rest_api_addr)

        ryu_rest_client = client.OFPClient(rest_api_addr)

        self.vif_ports = VifPortSet(self.int_br, ryu_rest_client)
        self.vif_ports.setup()

        sc_client = client.SwitchConfClient(rest_api_addr)
        sc_client.set_key(self.int_br.datapath_id,
                          conf_switch_key.OVS_TUNNEL_ADDR, tunnel_ip)

        # Currently Ryu supports only tcp methods. (ssl isn't supported yet)
        self.int_br.set_manager('ptcp:%d' % ovsdb_port)
        sc_client.set_key(self.int_br.datapath_id, conf_switch_key.OVSDB_ADDR,
                          'tcp:%s:%d' % (ovsdb_ip, ovsdb_port))


def main():
    cfg.CONF(project='quantum')

    logging_config.setup_logging(cfg.CONF)

    integ_br = cfg.CONF.OVS.integration_bridge
    root_helper = cfg.CONF.AGENT.root_helper

    tunnel_ip = _get_tunnel_ip()
    LOG.debug(_('tunnel_ip %s'), tunnel_ip)
    ovsdb_port = cfg.CONF.OVS.ovsdb_port
    LOG.debug(_('ovsdb_port %s'), ovsdb_port)
    ovsdb_ip = _get_ovsdb_ip()
    LOG.debug(_('ovsdb_ip %s'), ovsdb_ip)
    try:
        OVSQuantumOFPRyuAgent(integ_br, tunnel_ip, ovsdb_ip, ovsdb_port,
                              root_helper)
    except httplib.HTTPException, e:
        LOG.error(_("Initialization failed: %s"), e)
        sys.exit(1)

    LOG.info(_("Ryu initialization on the node is done."
               " Now Ryu agent exits successfully."))
    sys.exit(0)


if __name__ == "__main__":
    main()

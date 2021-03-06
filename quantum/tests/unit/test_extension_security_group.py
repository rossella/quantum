# Copyright (c) 2012 OpenStack, LLC.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import contextlib
import os

import mock
import webob.exc

from quantum.api.v2 import attributes as attr
from quantum.common.test_lib import test_config
from quantum import context
from quantum.db import db_base_plugin_v2
from quantum.db import securitygroups_db
from quantum.extensions import securitygroup as ext_sg
from quantum.openstack.common import cfg
from quantum.tests.unit import test_db_plugin

DB_PLUGIN_KLASS = ('quantum.tests.unit.test_extension_security_group.'
                   'SecurityGroupTestPlugin')
ROOTDIR = os.path.dirname(os.path.dirname(__file__))
ETCDIR = os.path.join(ROOTDIR, 'etc')


def etcdir(*p):
    return os.path.join(ETCDIR, *p)


class SecurityGroupTestExtensionManager(object):

    def get_resources(self):
        return ext_sg.Securitygroup.get_resources()

    def get_actions(self):
        return []

    def get_request_extensions(self):
        return []


class SecurityGroupsTestCase(test_db_plugin.QuantumDbPluginV2TestCase):

    def _create_security_group(self, fmt, name, description, external_id=None,
                               **kwargs):

        data = {'security_group': {'name': name,
                                   'tenant_id': kwargs.get('tenant_id',
                                                           'test_tenant'),
                                   'description': description}}
        if external_id:
            data['security_group']['external_id'] = external_id
        security_group_req = self.new_create_request('security-groups', data,
                                                     fmt)
        if (kwargs.get('set_context') and 'tenant_id' in kwargs):
            # create a specific auth context for this request
            security_group_req.environ['quantum.context'] = (
                context.Context('', kwargs['tenant_id']))
        return security_group_req.get_response(self.ext_api)

    def _build_security_group_rule(self, security_group_id, direction,
                                   protocol, port_range_min, port_range_max,
                                   source_ip_prefix=None, source_group_id=None,
                                   external_id=None, tenant_id='test_tenant',
                                   ethertype='IPv4'):

        data = {'security_group_rule': {'security_group_id': security_group_id,
                                        'direction': direction,
                                        'protocol': protocol,
                                        'ethertype': ethertype,
                                        'port_range_min': port_range_min,
                                        'port_range_max': port_range_max,
                                        'tenant_id': tenant_id,
                                        'ethertype': ethertype}}
        if external_id:
            data['security_group_rule']['external_id'] = external_id

        if source_ip_prefix:
            data['security_group_rule']['source_ip_prefix'] = source_ip_prefix

        if source_group_id:
            data['security_group_rule']['source_group_id'] = source_group_id

        return data

    def _create_security_group_rule(self, fmt, rules, **kwargs):

        security_group_rule_req = self.new_create_request(
            'security-group-rules', rules, fmt)

        if (kwargs.get('set_context') and 'tenant_id' in kwargs):
            # create a specific auth context for this request
            security_group_rule_req.environ['quantum.context'] = (
                context.Context('', kwargs['tenant_id']))
        return security_group_rule_req.get_response(self.ext_api)

    def _make_security_group(self, fmt, name, description, external_id=None,
                             **kwargs):
        res = self._create_security_group(fmt, name, description,
                                          external_id, **kwargs)
        if res.status_int >= 400:
            raise webob.exc.HTTPClientError(code=res.status_int)
        return self.deserialize(fmt, res)

    def _make_security_group_rule(self, fmt, rules, **kwargs):
        res = self._create_security_group_rule(self.fmt, rules)
        if res.status_int >= 400:
            raise webob.exc.HTTPClientError(code=res.status_int)
        return self.deserialize(fmt, res)

    @contextlib.contextmanager
    def security_group(self, name='webservers', description='webservers',
                       external_id=None, fmt=None, no_delete=False):
        if not fmt:
            fmt = self.fmt
        security_group = self._make_security_group(fmt, name, description,
                                                   external_id)
        try:
            yield security_group
        finally:
            if not no_delete:
                self._delete('security-groups',
                             security_group['security_group']['id'])

    @contextlib.contextmanager
    def security_group_rule(self, security_group_id='4cd70774-cc67-4a87-9b39-7'
                                                    'd1db38eb087',
                            direction='ingress', protocol='tcp',
                            port_range_min='22', port_range_max='22',
                            source_ip_prefix=None, source_group_id=None,
                            external_id=None, fmt=None, no_delete=False,
                            ethertype='IPv4'):
        if not fmt:
            fmt = self.fmt
        rule = self._build_security_group_rule(security_group_id,
                                               direction,
                                               protocol, port_range_min,
                                               port_range_max,
                                               source_ip_prefix,
                                               source_group_id,
                                               external_id,
                                               ethertype=ethertype)
        security_group_rule = self._make_security_group_rule(self.fmt, rule)
        try:
            yield security_group_rule
        finally:
            if not no_delete:
                self._delete('security-group-rules',
                             security_group_rule['security_group_rule']['id'])


class SecurityGroupsTestCaseXML(SecurityGroupsTestCase):
    fmt = 'xml'


class SecurityGroupTestPlugin(db_base_plugin_v2.QuantumDbPluginV2,
                              securitygroups_db.SecurityGroupDbMixin):
    """ Test plugin that implements necessary calls on create/delete port for
    associating ports with security groups.
    """

    supported_extension_aliases = ["security-group"]

    def create_port(self, context, port):
        tenant_id = self._get_tenant_id_for_create(context, port['port'])
        default_sg = self._ensure_default_security_group(context, tenant_id)
        if not attr.is_attr_set(port['port'].get(ext_sg.SECURITYGROUPS)):
            port['port'][ext_sg.SECURITYGROUPS] = [default_sg]
        session = context.session
        with session.begin(subtransactions=True):
            sgids = self._get_security_groups_on_port(context, port)
            port = super(SecurityGroupTestPlugin, self).create_port(context,
                                                                    port)
            self._process_port_create_security_group(context, port['id'],
                                                     sgids)
            self._extend_port_dict_security_group(context, port)
        return port

    def update_port(self, context, id, port):
        session = context.session
        with session.begin(subtransactions=True):
            if ext_sg.SECURITYGROUPS in port['port']:
                port['port'][ext_sg.SECURITYGROUPS] = (
                    self._get_security_groups_on_port(context, port))
                # delete the port binding and read it with the new rules
                self._delete_port_security_group_bindings(context, id)
                self._process_port_create_security_group(
                    context, id, port['port'].get(ext_sg.SECURITYGROUPS))
            port = super(SecurityGroupTestPlugin, self).update_port(
                context, id, port)
            self._extend_port_dict_security_group(context, port)
        return port

    def create_network(self, context, network):
        tenant_id = self._get_tenant_id_for_create(context, network['network'])
        self._ensure_default_security_group(context, tenant_id)
        return super(SecurityGroupTestPlugin, self).create_network(context,
                                                                   network)

    def get_ports(self, context, filters=None, fields=None):
        quantum_lports = super(SecurityGroupTestPlugin, self).get_ports(
            context, filters)
        for quantum_lport in quantum_lports:
            self._extend_port_dict_security_group(context, quantum_lport)
        return quantum_lports


class SecurityGroupDBTestCase(SecurityGroupsTestCase):
    def setUp(self, plugin=None):
        test_config['plugin_name_v2'] = DB_PLUGIN_KLASS
        ext_mgr = SecurityGroupTestExtensionManager()
        test_config['extension_manager'] = ext_mgr
        super(SecurityGroupDBTestCase, self).setUp(plugin)

    def tearDown(self):
        del test_config['plugin_name_v2']
        super(SecurityGroupDBTestCase, self).tearDown()


class TestSecurityGroups(SecurityGroupDBTestCase):
    def test_create_security_group(self):
        name = 'webservers'
        description = 'my webservers'
        keys = [('name', name,), ('description', description)]
        with self.security_group(name, description) as security_group:
            for k, v, in keys:
                self.assertEqual(security_group['security_group'][k], v)

    def test_create_security_group_external_id(self):
        cfg.CONF.set_override('proxy_mode', True, 'SECURITYGROUP')
        name = 'webservers'
        description = 'my webservers'
        external_id = 10
        keys = [('name', name,), ('description', description),
                ('external_id', external_id)]
        with self.security_group(name, description, external_id) as sg:
            for k, v, in keys:
                self.assertEqual(sg['security_group'][k], v)

    def test_default_security_group(self):
        with self.network():
            res = self.new_list_request('security-groups')
            groups = self.deserialize(self.fmt, res.get_response(self.ext_api))
            self.assertEqual(len(groups['security_groups']), 1)

    def test_create_security_group_proxy_mode_not_admin(self):
        cfg.CONF.set_override('proxy_mode', True, 'SECURITYGROUP')
        res = self._create_security_group(self.fmt, 'webservers',
                                          'webservers', '1',
                                          tenant_id='bad_tenant',
                                          set_context=True)
        self.deserialize(self.fmt, res)
        self.assertEqual(res.status_int, 403)

    def test_create_security_group_no_external_id_proxy_mode(self):
        cfg.CONF.set_override('proxy_mode', True, 'SECURITYGROUP')
        res = self._create_security_group(self.fmt, 'webservers',
                                          'webservers')
        self.deserialize(self.fmt, res)
        self.assertEqual(res.status_int, 400)

    def test_create_security_group_no_external_id_not_proxy_mode(self):
        res = self._create_security_group(self.fmt, 'webservers',
                                          'webservers', '1')
        self.deserialize(self.fmt, res)
        self.assertEqual(res.status_int, 409)

    def test_create_default_security_group_fail(self):
        name = 'default'
        description = 'my webservers'
        res = self._create_security_group(self.fmt, name, description)
        self.deserialize(self.fmt, res)
        self.assertEqual(res.status_int, 409)

    def test_create_security_group_duplicate_external_id(self):
        cfg.CONF.set_override('proxy_mode', True, 'SECURITYGROUP')
        name = 'webservers'
        description = 'my webservers'
        external_id = 1
        with self.security_group(name, description, external_id):
            res = self._create_security_group(self.fmt, name, description,
                                              external_id)
            self.deserialize(self.fmt, res)
            self.assertEqual(res.status_int, 409)

    def test_list_security_groups(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description):
            res = self.new_list_request('security-groups')
            groups = self.deserialize(self.fmt, res.get_response(self.ext_api))
            self.assertEqual(len(groups['security_groups']), 2)
            for group in groups['security_groups']:
                if group['name'] == 'default':
                    self.assertEquals(len(group['security_group_rules']), 2)
                else:
                    self.assertEquals(len(group['security_group_rules']), 0)

    def test_create_security_group_rule_ethertype_invalid_as_number(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description) as sg:
            security_group_id = sg['security_group']['id']
            ethertype = 2
            rule = self._build_security_group_rule(
                security_group_id, 'ingress', 'tcp', '22', '22', None, None,
                ethertype=ethertype)
            res = self._create_security_group_rule('json', rule)
            self.deserialize('json', res)
            self.assertEqual(res.status_int, 400)

    def test_create_security_group_rule_protocol_invalid_as_number(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description) as sg:
            security_group_id = sg['security_group']['id']
            protocol = 2
            rule = self._build_security_group_rule(
                security_group_id, 'ingress', protocol, '22', '22',
                None, None)
            res = self._create_security_group_rule('json', rule)
            self.deserialize('json', res)
            self.assertEqual(res.status_int, 400)

    def test_create_security_group_rule_case_insensitive(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description) as sg:
            security_group_id = sg['security_group']['id']
            direction = "ingress"
            source_ip_prefix = "10.0.0.0/24"
            protocol = 'TCP'
            port_range_min = 22
            port_range_max = 22
            ethertype = 'ipV4'
            with self.security_group_rule(security_group_id, direction,
                                          protocol, port_range_min,
                                          port_range_max,
                                          source_ip_prefix,
                                          ethertype=ethertype) as rule:

                # the lower case value will be return
                self.assertEquals(rule['security_group_rule']['protocol'],
                                  protocol.lower())
                self.assertEquals(rule['security_group_rule']['ethertype'],
                                  'IPv4')

    def test_get_security_group(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description) as sg:
            source_group_id = sg['security_group']['id']
            res = self.new_show_request('security-groups', source_group_id)
            security_group_id = sg['security_group']['id']
            direction = "ingress"
            source_ip_prefix = "10.0.0.0/24"
            protocol = 'tcp'
            port_range_min = 22
            port_range_max = 22
            keys = [('source_ip_prefix', source_ip_prefix),
                    ('security_group_id', security_group_id),
                    ('direction', direction),
                    ('protocol', protocol),
                    ('port_range_min', port_range_min),
                    ('port_range_max', port_range_max)]
            with self.security_group_rule(security_group_id, direction,
                                          protocol, port_range_min,
                                          port_range_max,
                                          source_ip_prefix):

                group = self.deserialize(
                    self.fmt, res.get_response(self.ext_api))
                sg_rule = group['security_group']['security_group_rules']
                self.assertEqual(group['security_group']['id'],
                                 source_group_id)
                self.assertEqual(len(sg_rule), 1)
                for k, v, in keys:
                    self.assertEqual(sg_rule[0][k], v)

    def test_delete_security_group(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description, no_delete=True) as sg:
            source_group_id = sg['security_group']['id']
            self._delete('security-groups', source_group_id, 204)

    def test_delete_default_security_group_fail(self):
        with self.network():
            res = self.new_list_request('security-groups')
            sg = self.deserialize(self.fmt, res.get_response(self.ext_api))
            self._delete('security-groups', sg['security_groups'][0]['id'],
                         409)

    def test_default_security_group_rules(self):
        with self.network():
            res = self.new_list_request('security-groups')
            groups = self.deserialize(self.fmt, res.get_response(self.ext_api))
            self.assertEqual(len(groups['security_groups']), 1)
            res = self.new_list_request('security-group-rules')
            rules = self.deserialize(self.fmt, res.get_response(self.ext_api))
            self.assertEqual(len(rules['security_group_rules']), 2)
            # just generic rules to allow default egress and
            # intergroup communicartion
            for rule in rules['security_group_rules']:
                self.assertEqual(rule['port_range_max'], None)
                self.assertEqual(rule['port_range_min'], None)
                self.assertEqual(rule['protocol'], None)

    def test_create_security_group_rule_source_ip_prefix(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description) as sg:
            security_group_id = sg['security_group']['id']
            direction = "ingress"
            source_ip_prefix = "10.0.0.0/24"
            protocol = 'tcp'
            port_range_min = 22
            port_range_max = 22
            keys = [('source_ip_prefix', source_ip_prefix),
                    ('security_group_id', security_group_id),
                    ('direction', direction),
                    ('protocol', protocol),
                    ('port_range_min', port_range_min),
                    ('port_range_max', port_range_max)]
            with self.security_group_rule(security_group_id, direction,
                                          protocol, port_range_min,
                                          port_range_max,
                                          source_ip_prefix) as rule:
                for k, v, in keys:
                    self.assertEqual(rule['security_group_rule'][k], v)

    def test_create_security_group_rule_group_id(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description) as sg:
            with self.security_group(name, description) as sg2:
                security_group_id = sg['security_group']['id']
                direction = "ingress"
                source_group_id = sg2['security_group']['id']
                protocol = 'tcp'
                port_range_min = 22
                port_range_max = 22
                keys = [('source_group_id', source_group_id),
                        ('security_group_id', security_group_id),
                        ('direction', direction),
                        ('protocol', protocol),
                        ('port_range_min', port_range_min),
                        ('port_range_max', port_range_max)]
                with self.security_group_rule(security_group_id, direction,
                                              protocol, port_range_min,
                                              port_range_max,
                                              source_group_id=source_group_id
                                              ) as rule:
                    for k, v, in keys:
                        self.assertEqual(rule['security_group_rule'][k], v)

    def test_create_security_group_source_group_ip_and_ip_prefix(self):
        security_group_id = "4cd70774-cc67-4a87-9b39-7d1db38eb087"
        direction = "ingress"
        source_ip_prefix = "10.0.0.0/24"
        protocol = 'tcp'
        port_range_min = 22
        port_range_max = 22
        source_group_id = "9cd70774-cc67-4a87-9b39-7d1db38eb087"
        rule = self._build_security_group_rule(security_group_id, direction,
                                               protocol, port_range_min,
                                               port_range_max,
                                               source_ip_prefix,
                                               source_group_id)
        res = self._create_security_group_rule(self.fmt, rule)
        self.deserialize(self.fmt, res)
        self.assertEqual(res.status_int, 400)

    def test_create_security_group_rule_bad_security_group_id(self):
        security_group_id = "4cd70774-cc67-4a87-9b39-7d1db38eb087"
        direction = "ingress"
        source_ip_prefix = "10.0.0.0/24"
        protocol = 'tcp'
        port_range_min = 22
        port_range_max = 22
        rule = self._build_security_group_rule(security_group_id, direction,
                                               protocol, port_range_min,
                                               port_range_max,
                                               source_ip_prefix)
        res = self._create_security_group_rule(self.fmt, rule)
        self.deserialize(self.fmt, res)
        self.assertEqual(res.status_int, 404)

    def test_create_security_group_rule_bad_tenant(self):
        with self.security_group() as sg:
            rule = {'security_group_rule':
                    {'security_group_id': sg['security_group']['id'],
                     'direction': 'ingress',
                     'protocol': 'tcp',
                     'port_range_min': '22',
                     'port_range_max': '22',
                     'tenant_id': "bad_tenant"}}

        res = self._create_security_group_rule(self.fmt, rule)
        self.deserialize(self.fmt, res)
        self.assertEqual(res.status_int, 404)

    def test_create_security_group_rule_exteral_id_proxy_mode(self):
        cfg.CONF.set_override('proxy_mode', True, 'SECURITYGROUP')
        with self.security_group(external_id=1) as sg:
            rule = {'security_group_rule':
                    {'security_group_id': sg['security_group']['id'],
                     'direction': 'ingress',
                     'protocol': 'tcp',
                     'port_range_min': '22',
                     'port_range_max': '22',
                     'external_id': '1',
                     'tenant_id': 'test_tenant',
                     'source_group_id': sg['security_group']['id']}}

            res = self._create_security_group_rule(self.fmt, rule)
            self.deserialize(self.fmt, res)
            self.assertEqual(res.status_int, 201)

    def test_create_security_group_rule_exteral_id_not_proxy_mode(self):
        with self.security_group() as sg:
            rule = {'security_group_rule':
                    {'security_group_id': sg['security_group']['id'],
                     'direction': 'ingress',
                     'protocol': 'tcp',
                     'port_range_min': '22',
                     'port_range_max': '22',
                     'external_id': 1,
                     'tenant_id': 'test_tenant',
                     'source_group_id': sg['security_group']['id']}}

            res = self._create_security_group_rule(self.fmt, rule)
            self.deserialize(self.fmt, res)
            self.assertEqual(res.status_int, 409)

    def test_create_security_group_rule_not_admin(self):
        cfg.CONF.set_override('proxy_mode', True, 'SECURITYGROUP')
        with self.security_group(external_id='1') as sg:
            rule = {'security_group_rule':
                    {'security_group_id': sg['security_group']['id'],
                     'direction': 'ingress',
                     'protocol': 'tcp',
                     'port_range_min': '22',
                     'port_range_max': '22',
                     'tenant_id': 'bad_tenant',
                     'external_id': 1,
                     'source_group_id': sg['security_group']['id']}}

            res = self._create_security_group_rule(self.fmt, rule,
                                                   tenant_id='bad_tenant',
                                                   set_context=True)
            self.deserialize(self.fmt, res)
            self.assertEqual(res.status_int, 403)

    def test_create_security_group_rule_bad_tenant_source_group_id(self):
        with self.security_group() as sg:
            res = self._create_security_group(self.fmt, 'webservers',
                                              'webservers',
                                              tenant_id='bad_tenant')
            sg2 = self.deserialize(self.fmt, res)
            rule = {'security_group_rule':
                    {'security_group_id': sg2['security_group']['id'],
                     'direction': 'ingress',
                     'protocol': 'tcp',
                     'port_range_min': '22',
                     'port_range_max': '22',
                     'tenant_id': 'bad_tenant',
                     'source_group_id': sg['security_group']['id']}}

            res = self._create_security_group_rule(self.fmt, rule,
                                                   tenant_id='bad_tenant',
                                                   set_context=True)
            self.deserialize(self.fmt, res)
            self.assertEqual(res.status_int, 404)

    def test_create_security_group_rule_bad_tenant_security_group_rule(self):
        with self.security_group() as sg:
            res = self._create_security_group(self.fmt, 'webservers',
                                              'webservers',
                                              tenant_id='bad_tenant')
            self.deserialize(self.fmt, res)
            rule = {'security_group_rule':
                    {'security_group_id': sg['security_group']['id'],
                     'direction': 'ingress',
                     'protocol': 'tcp',
                     'port_range_min': '22',
                     'port_range_max': '22',
                     'tenant_id': 'bad_tenant'}}

            res = self._create_security_group_rule(self.fmt, rule,
                                                   tenant_id='bad_tenant',
                                                   set_context=True)
            self.deserialize(self.fmt, res)
            self.assertEqual(res.status_int, 404)

    def test_create_security_group_rule_bad_source_group_id(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description) as sg:
            security_group_id = sg['security_group']['id']
            source_group_id = "4cd70774-cc67-4a87-9b39-7d1db38eb087"
            direction = "ingress"
            protocol = 'tcp'
            port_range_min = 22
            port_range_max = 22
        rule = self._build_security_group_rule(security_group_id, direction,
                                               protocol, port_range_min,
                                               port_range_max,
                                               source_group_id=source_group_id)
        res = self._create_security_group_rule(self.fmt, rule)
        self.deserialize(self.fmt, res)
        self.assertEqual(res.status_int, 404)

    def test_create_security_group_rule_duplicate_rules(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description) as sg:
            security_group_id = sg['security_group']['id']
            with self.security_group_rule(security_group_id):
                rule = self._build_security_group_rule(
                    sg['security_group']['id'], 'ingress', 'tcp', '22', '22')
                self._create_security_group_rule(self.fmt, rule)
                res = self._create_security_group_rule(self.fmt, rule)
                self.deserialize(self.fmt, res)
                self.assertEqual(res.status_int, 409)

    def test_create_security_group_rule_min_port_greater_max(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description) as sg:
            security_group_id = sg['security_group']['id']
            with self.security_group_rule(security_group_id):
                rule = self._build_security_group_rule(
                    sg['security_group']['id'], 'ingress', 'tcp', '50', '22')
                self._create_security_group_rule(self.fmt, rule)
                res = self._create_security_group_rule(self.fmt, rule)
                self.deserialize(self.fmt, res)
                self.assertEqual(res.status_int, 400)

    def test_create_security_group_rule_ports_but_no_protocol(self):
        name = 'webservers'
        description = 'my webservers'
        with self.security_group(name, description) as sg:
            security_group_id = sg['security_group']['id']
            with self.security_group_rule(security_group_id):
                rule = self._build_security_group_rule(
                    sg['security_group']['id'], 'ingress', None, '22', '22')
                self._create_security_group_rule(self.fmt, rule)
                res = self._create_security_group_rule(self.fmt, rule)
                self.deserialize(self.fmt, res)
                self.assertEqual(res.status_int, 400)

    def test_list_ports_security_group(self):
        with self.network() as n:
            with self.subnet(n):
                res = self._create_port(self.fmt, n['network']['id'])
                self.deserialize(self.fmt, res)
                res = self.new_list_request('ports')
                ports = self.deserialize(self.fmt,
                                         res.get_response(self.api))
                port = ports['ports'][0]
                self.assertEquals(len(port[ext_sg.SECURITYGROUPS]), 1)
                self._delete('ports', port['id'])

    def test_update_port_with_security_group(self):
        with self.network() as n:
            with self.subnet(n):
                with self.security_group() as sg:
                    res = self._create_port(self.fmt, n['network']['id'])
                    port = self.deserialize(self.fmt, res)

                    data = {'port': {'fixed_ips': port['port']['fixed_ips'],
                                     'name': port['port']['name'],
                                     ext_sg.SECURITYGROUPS:
                                     [sg['security_group']['id']]}}

                    req = self.new_update_request('ports', data,
                                                  port['port']['id'])
                    res = self.deserialize(self.fmt,
                                           req.get_response(self.api))
                    self.assertEqual(res['port'][ext_sg.SECURITYGROUPS][0],
                                     sg['security_group']['id'])

                    # Test update port without security group
                    data = {'port': {'fixed_ips': port['port']['fixed_ips'],
                                     'name': port['port']['name']}}

                    req = self.new_update_request('ports', data,
                                                  port['port']['id'])
                    res = self.deserialize(self.fmt,
                                           req.get_response(self.api))
                    self.assertEqual(res['port'][ext_sg.SECURITYGROUPS][0],
                                     sg['security_group']['id'])

                    self._delete('ports', port['port']['id'])

    def test_update_port_with_multiple_security_groups(self):
        with self.network() as n:
            with self.subnet(n):
                with self.security_group() as sg1:
                    with self.security_group() as sg2:
                        res = self._create_port(
                            self.fmt, n['network']['id'],
                            security_groups=[sg1['security_group']['id'],
                                             sg2['security_group']['id']])
                        port = self.deserialize(self.fmt, res)
                        self.assertEqual(len(
                            port['port'][ext_sg.SECURITYGROUPS]), 2)
                        self._delete('ports', port['port']['id'])

    def test_update_port_remove_security_group_empty_list(self):
        with self.network() as n:
            with self.subnet(n):
                with self.security_group() as sg:
                    res = self._create_port(self.fmt, n['network']['id'],
                                            security_groups=(
                                            [sg['security_group']['id']]))
                    port = self.deserialize(self.fmt, res)

                    data = {'port': {'fixed_ips': port['port']['fixed_ips'],
                                     'name': port['port']['name'],
                                     'security_groups': []}}

                    req = self.new_update_request('ports', data,
                                                  port['port']['id'])
                    res = self.deserialize(self.fmt,
                                           req.get_response(self.api))
                    self.assertEqual(res['port'].get(ext_sg.SECURITYGROUPS),
                                     [])
                    self._delete('ports', port['port']['id'])

    def test_update_port_remove_security_group_none(self):
        with self.network() as n:
            with self.subnet(n):
                with self.security_group() as sg:
                    res = self._create_port(self.fmt, n['network']['id'],
                                            security_groups=(
                                            [sg['security_group']['id']]))
                    port = self.deserialize(self.fmt, res)

                    data = {'port': {'fixed_ips': port['port']['fixed_ips'],
                                     'name': port['port']['name'],
                                     'security_groups': None}}

                    req = self.new_update_request('ports', data,
                                                  port['port']['id'])
                    res = self.deserialize(self.fmt,
                                           req.get_response(self.api))
                    self.assertEqual(res['port'].get(ext_sg.SECURITYGROUPS),
                                     [])
                    self._delete('ports', port['port']['id'])

    def test_create_port_with_bad_security_group(self):
        with self.network() as n:
            with self.subnet(n):
                res = self._create_port(self.fmt, n['network']['id'],
                                        security_groups=['bad_id'])

                self.deserialize(self.fmt, res)
                self.assertEqual(res.status_int, 400)

    def test_create_delete_security_group_port_in_use(self):
        with self.network() as n:
            with self.subnet(n):
                with self.security_group() as sg:
                    res = self._create_port(self.fmt, n['network']['id'],
                                            security_groups=(
                                            [sg['security_group']['id']]))
                    port = self.deserialize(self.fmt, res)
                    self.assertEqual(port['port'][ext_sg.SECURITYGROUPS][0],
                                     sg['security_group']['id'])
                    # try to delete security group that's in use
                    res = self._delete('security-groups',
                                       sg['security_group']['id'], 409)
                    # delete the blocking port
                    self._delete('ports', port['port']['id'])

    def test_create_security_group_rule_bulk_native(self):
        if self._skip_native_bulk:
            self.skipTest("Plugin does not support native bulk "
                          "security_group_rule create")
        with self.security_group() as sg:
            rule1 = self._build_security_group_rule(sg['security_group']['id'],
                                                    'ingress', 'tcp', '22',
                                                    '22', '10.0.0.1/24')
            rule2 = self._build_security_group_rule(sg['security_group']['id'],
                                                    'ingress', 'tcp', '23',
                                                    '23', '10.0.0.1/24')
            rules = {'security_group_rules': [rule1['security_group_rule'],
                                              rule2['security_group_rule']]}
            res = self._create_security_group_rule(self.fmt, rules)
            self.deserialize(self.fmt, res)
            self.assertEqual(res.status_int, 201)

    def test_create_security_group_rule_bulk_emulated(self):
        real_has_attr = hasattr

        #ensures the API choose the emulation code path
        def fakehasattr(item, attr):
            if attr.endswith('__native_bulk_support'):
                return False
            return real_has_attr(item, attr)

        with mock.patch('__builtin__.hasattr',
                        new=fakehasattr):
            with self.security_group() as sg:
                rule1 = self._build_security_group_rule(
                    sg['security_group']['id'], 'ingress', 'tcp', '22', '22',
                    '10.0.0.1/24')
                rule2 = self._build_security_group_rule(
                    sg['security_group']['id'], 'ingress', 'tcp', '23', '23',
                    '10.0.0.1/24')
                rules = {'security_group_rules': [rule1['security_group_rule'],
                                                  rule2['security_group_rule']]
                         }
                res = self._create_security_group_rule(self.fmt, rules)
                self.deserialize(self.fmt, res)
                self.assertEqual(res.status_int, 201)

    def test_create_security_group_rule_duplicate_rule_in_post(self):
        if self._skip_native_bulk:
            self.skipTest("Plugin does not support native bulk "
                          "security_group_rule create")
        with self.security_group() as sg:
            rule = self._build_security_group_rule(sg['security_group']['id'],
                                                   'ingress', 'tcp', '22',
                                                   '22', '10.0.0.1/24')
            rules = {'security_group_rules': [rule['security_group_rule'],
                                              rule['security_group_rule']]}
            res = self._create_security_group_rule(self.fmt, rules)
            rule = self.deserialize(self.fmt, res)
            self.assertEqual(res.status_int, 409)

    def test_create_security_group_rule_duplicate_rule_in_post_emulated(self):
        real_has_attr = hasattr

        #ensures the API choose the emulation code path
        def fakehasattr(item, attr):
            if attr.endswith('__native_bulk_support'):
                return False
            return real_has_attr(item, attr)

        with mock.patch('__builtin__.hasattr',
                        new=fakehasattr):

            with self.security_group() as sg:
                rule = self._build_security_group_rule(
                    sg['security_group']['id'], 'ingress', 'tcp', '22', '22',
                    '10.0.0.1/24')
                rules = {'security_group_rules': [rule['security_group_rule'],
                                                  rule['security_group_rule']]}
                res = self._create_security_group_rule(self.fmt, rules)
                rule = self.deserialize(self.fmt, res)
                self.assertEqual(res.status_int, 409)

    def test_create_security_group_rule_duplicate_rule_db(self):
        if self._skip_native_bulk:
            self.skipTest("Plugin does not support native bulk "
                          "security_group_rule create")
        with self.security_group() as sg:
            rule = self._build_security_group_rule(sg['security_group']['id'],
                                                   'ingress', 'tcp', '22',
                                                   '22', '10.0.0.1/24')
            rules = {'security_group_rules': [rule]}
            self._create_security_group_rule(self.fmt, rules)
            res = self._create_security_group_rule(self.fmt, rules)
            rule = self.deserialize(self.fmt, res)
            self.assertEqual(res.status_int, 409)

    def test_create_security_group_rule_duplicate_rule_db_emulated(self):
        real_has_attr = hasattr

        #ensures the API choose the emulation code path
        def fakehasattr(item, attr):
            if attr.endswith('__native_bulk_support'):
                return False
            return real_has_attr(item, attr)

        with mock.patch('__builtin__.hasattr',
                        new=fakehasattr):
            with self.security_group() as sg:
                rule = self._build_security_group_rule(
                    sg['security_group']['id'], 'ingress', 'tcp', '22', '22',
                    '10.0.0.1/24')
                rules = {'security_group_rules': [rule]}
                self._create_security_group_rule(self.fmt, rules)
                res = self._create_security_group_rule(self.fmt, rule)
                self.deserialize(self.fmt, res)
                self.assertEqual(res.status_int, 409)

    def test_create_security_group_rule_differnt_security_group_ids(self):
        if self._skip_native_bulk:
            self.skipTest("Plugin does not support native bulk "
                          "security_group_rule create")
        with self.security_group() as sg1:
            with self.security_group() as sg2:
                rule1 = self._build_security_group_rule(
                    sg1['security_group']['id'], 'ingress', 'tcp', '22', '22',
                    '10.0.0.1/24')
                rule2 = self._build_security_group_rule(
                    sg2['security_group']['id'], 'ingress', 'tcp', '23', '23',
                    '10.0.0.1/24')

                rules = {'security_group_rules': [rule1['security_group_rule'],
                                                  rule2['security_group_rule']]
                         }
                res = self._create_security_group_rule(self.fmt, rules)
                self.deserialize(self.fmt, res)
                self.assertEqual(res.status_int, 400)

    def test_create_security_group_rule_with_invalid_ethertype(self):
        security_group_id = "4cd70774-cc67-4a87-9b39-7d1db38eb087"
        direction = "ingress"
        source_ip_prefix = "10.0.0.0/24"
        protocol = 'tcp'
        port_range_min = 22
        port_range_max = 22
        source_group_id = "9cd70774-cc67-4a87-9b39-7d1db38eb087"
        rule = self._build_security_group_rule(security_group_id, direction,
                                               protocol, port_range_min,
                                               port_range_max,
                                               source_ip_prefix,
                                               source_group_id,
                                               ethertype='IPv5')
        res = self._create_security_group_rule(self.fmt, rule)
        self.deserialize(self.fmt, res)
        self.assertEqual(res.status_int, 400)

    def test_create_security_group_rule_with_invalid_protocol(self):
        security_group_id = "4cd70774-cc67-4a87-9b39-7d1db38eb087"
        direction = "ingress"
        source_ip_prefix = "10.0.0.0/24"
        protocol = 'tcp/ip'
        port_range_min = 22
        port_range_max = 22
        source_group_id = "9cd70774-cc67-4a87-9b39-7d1db38eb087"
        rule = self._build_security_group_rule(security_group_id, direction,
                                               protocol, port_range_min,
                                               port_range_max,
                                               source_ip_prefix,
                                               source_group_id)
        res = self._create_security_group_rule(self.fmt, rule)
        self.deserialize(self.fmt, res)
        self.assertEqual(res.status_int, 400)

    def test_validate_port_external_id_quantum_id(self):
        cfg.CONF.set_override('proxy_mode', True, 'SECURITYGROUP')
        with self.network() as n:
            with self.subnet(n):
                sg1 = (self.deserialize(self.fmt,
                       self._create_security_group(self.fmt,
                                                   'foo', 'bar', '1')))
                sg2 = (self.deserialize(self.fmt,
                       self._create_security_group(self.fmt,
                                                   'foo', 'bar', '2')))
                res = self._create_port(
                    self.fmt, n['network']['id'],
                    security_groups=[sg1['security_group']['id']])

                port = self.deserialize(self.fmt, res)
                # This request updates the port sending the quantum security
                # group id in and a nova security group id.
                data = {'port': {'fixed_ips': port['port']['fixed_ips'],
                                 'name': port['port']['name'],
                                 ext_sg.SECURITYGROUPS:
                                 [sg1['security_group']['external_id'],
                                  sg2['security_group']['id']]}}
                req = self.new_update_request('ports', data,
                                              port['port']['id'])
                res = self.deserialize(self.fmt, req.get_response(self.api))
                self.assertEquals(len(res['port'][ext_sg.SECURITYGROUPS]), 2)
                for sg_id in res['port'][ext_sg.SECURITYGROUPS]:
                    # only security group id's should be
                    # returned and not external_ids
                    self.assertEquals(len(sg_id), 36)
                self._delete('ports', port['port']['id'])

    def test_validate_port_external_id_string_or_int(self):
        cfg.CONF.set_override('proxy_mode', True, 'SECURITYGROUP')
        with self.network() as n:
            with self.subnet(n):
                string_id = '1'
                int_id = 2
                self.deserialize(
                    self.fmt, self._create_security_group(self.fmt,
                                                          'foo', 'bar',
                                                          string_id))
                self.deserialize(
                    self.fmt, self._create_security_group(self.fmt,
                                                          'foo', 'bar',
                                                          int_id))
                res = self._create_port(
                    self.fmt, n['network']['id'],
                    security_groups=[string_id, int_id])

                port = self.deserialize(self.fmt, res)
                self._delete('ports', port['port']['id'])

    def test_create_port_with_non_uuid_or_int(self):
        with self.network() as n:
            with self.subnet(n):
                res = self._create_port(self.fmt, n['network']['id'],
                                        security_groups=['not_valid'])

                self.deserialize(self.fmt, res)
                self.assertEqual(res.status_int, 400)

    def test_validate_port_external_id_fail(self):
        cfg.CONF.set_override('proxy_mode', True, 'SECURITYGROUP')
        with self.network() as n:
            with self.subnet(n):
                bad_id = 1
                res = self._create_port(
                    self.fmt, n['network']['id'],
                    security_groups=[bad_id])

                self.deserialize(self.fmt, res)
                self.assertEqual(res.status_int, 404)


class TestSecurityGroupsXML(TestSecurityGroups):
    fmt = 'xml'

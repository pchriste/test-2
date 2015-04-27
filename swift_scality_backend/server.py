# Copyright (c) 2010-2013 OpenStack, LLC.
# Copyright (c) 2014 Scality
#
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

""" Scality Sproxyd Object Server for Swift """

import errno
import itertools
import os

import eventlet

import swift.common.bufferedhttp
import swift.common.exceptions
import swift.common.http
from swift import gettext_ as _
import swift.obj.server

import scality_sproxyd_client.sproxyd_client

import swift_scality_backend.diskfile
import swift_scality_backend.policy_configuration
import swift_scality_backend.utils

POLICY_STUB = object()


class ObjectController(swift.obj.server.ObjectController):
    """Implements the WSGI application for the Scality Object Server."""

    def __init__(self, *args, **kwargs):
        self._clients = {}
        self._conn_timeout = None
        self._read_timeout = None
        self._diskfile_mgr = None
        self._policy_configuration = None

        super(ObjectController, self).__init__(*args, **kwargs)

    def setup(self, conf):
        """Class setup

        :param conf: WSGI configuration parameter
        """
        # TODO(jordanP) to be changed when we make clear in the Readme we expect
        # a comma separated list of full sproxyd endpoints.
        sproxyd_path = conf.get('sproxyd_path', '/proxy/chord').strip('/')
        sproxyd_urls = ['http://%s/%s/' % (h, sproxyd_path) for h in
                        swift_scality_backend.utils.split_list(conf['sproxyd_host'])]

        # We can't pass `None` as value for sproxyd_*_timeout because it will
        # override the defaults set in SproxydClient
        kwargs = {}

        sproxyd_conn_timeout = conf.get('sproxyd_conn_timeout')
        if sproxyd_conn_timeout is not None:
            kwargs['conn_timeout'] = float(sproxyd_conn_timeout)

        sproxyd_read_timeout = conf.get('sproxyd_proxy_timeout')
        if sproxyd_read_timeout is not None:
            kwargs['read_timeout'] = float(sproxyd_read_timeout)

        self._clients[0] = scality_sproxyd_client.sproxyd_client.SproxydClient(
            sproxyd_urls, logger=self.logger, **kwargs)
        self._diskfile_mgr = swift_scality_backend.diskfile.DiskFileManager(conf, self.logger)

        self._conn_timeout = float(sproxyd_conn_timeout) \
            if sproxyd_conn_timeout is not None else None
        self._read_timeout = float(sproxyd_read_timeout) \
            if sproxyd_read_timeout is not None else None

        sp_path = \
            swift_scality_backend.policy_configuration.DEFAULT_CONFIGURATION_PATH
        self.logger.info('Reading storage policy configuration from %r', sp_path)
        try:
            with open(sp_path, 'r') as fd:
                self.logger.info('Parsing storage policy configuration')
                self._policy_configuration = \
                    swift_scality_backend.policy_configuration.Configuration.from_stream(fd)
        except IOError as exc:
            if exc.errno == errno.ENOENT:
                self.logger.info(
                    'No storage policy configuration found at %r', sp_path)
                self._policy_configuration = None
            else:
                self.logger.exception(
                    'Failure while reading storage policy configuration '
                    'from %r', sp_path)
                raise

    def _get_client_for_policy(self, policy_idx):
        '''Retrieve or create an Sproxyd client for a given storage policy

        :param policy_idx: Policy identifier
        :type policy_idx: `int`
        :return: Sproxyd client which can be used for requests in the given
                 policy
        :rtype: `scality_sproxyd_client.sproxyd_client.SproxydClient`

        :raise RuntimeError: No policies configured
        '''

        if policy_idx not in self._clients:
            if not self._policy_configuration:
                raise RuntimeError(
                    'No storage policy configuration found, but request for '
                    'policy %r' % policy_idx)

            policy = self._policy_configuration.get_policy(policy_idx)

            # TODO: Separate read- and write-endpoints
            # TODO: Location hints
            endpoints = policy.lookup(policy.WRITE, location_hints=[])

            client = scality_sproxyd_client.sproxyd_client.SproxydClient(
                itertools.chain(*endpoints),
                self._conn_timeout, self._read_timeout, self.logger)

            self._clients[policy_idx] = client

        return self._clients[policy_idx]

    def get_diskfile(self, device, partition, account, container, obj,
                     policy=POLICY_STUB, **kwargs):
        """
        Utility method for instantiating a DiskFile object supporting a
        given REST API.
        """

        # When `policy_idx` is not set (e.g. running Swift 1.13), the fallback
        # policy 0 should be used.
        if policy_idx is POLICY_IDX_STUB:
            policy_idx = 0

        client = self._get_client_for_policy(policy_idx)

        return self._diskfile_mgr.get_diskfile(
            client, account, container, obj)

    def async_update(self, op, account, container, obj, host, partition,
                     contdevice, headers_out, objdevice,
                     policy=POLICY_STUB):
        """Sends or saves an async update.

        :param op: operation performed (ex: 'PUT', or 'DELETE')
        :param account: account name for the object
        :param container: container name for the object
        :param obj: object name
        :param host: host that the container is on
        :param partition: partition that the container is on
        :param contdevice: device name that the container is on
        :param headers_out: dictionary of headers to send in the container
                            request
        :param objdevice: device name that the object is in
        :param policy: the associated BaseStoragePolicy instance OR the
                       associated storage policy index (depends on the Swift
                       version)
        """
        headers_out['user-agent'] = 'obj-server %s' % os.getpid()
        full_path = '/%s/%s/%s' % (account, container, obj)
        if all([host, partition, contdevice]):
            try:
                with swift.common.exceptions.ConnectionTimeout(self.conn_timeout):
                    ip, port = host.rsplit(':', 1)
                    conn = swift.common.bufferedhttp.http_connect(ip, port,
                                                                  contdevice, partition, op,
                                                                  full_path, headers_out)
                with eventlet.Timeout(self.node_timeout):
                    response = conn.getresponse()
                    response.read()
                    if swift.common.http.is_success(response.status):
                        return
                    else:
                        self.logger.error(_(
                            'ERROR Container update failed: %(status)d '
                            'response from %(ip)s:%(port)s/%(dev)s'),
                            {'status': response.status, 'ip': ip, 'port': port,
                             'dev': contdevice})
            except Exception:
                self.logger.exception(_(
                    'ERROR container update failed with '
                    '%(ip)s:%(port)s/%(dev)s'),
                    {'ip': ip, 'port': port, 'dev': contdevice})
        # FIXME: For now don't handle async updates

    def REPLICATE(*_args, **_kwargs):
        """Handle REPLICATE requests for the Swift Object Server.

        This is used by the object replicator to get hashes for directories.
        """
        pass


def app_factory(global_conf, **local_conf):
    """paste.deploy app factory for creating WSGI object server apps."""
    conf = global_conf.copy()
    conf.update(local_conf)
    return ObjectController(conf)

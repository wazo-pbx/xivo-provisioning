# -*- coding: utf-8 -*-
# Copyright 2010-2019 The Wazo Authors  (see the AUTHORS file)
# SPDX-License-Identifier: GPL-3.0-or-later

import copy
import logging
import functools
import os.path
import urlparse
from provd.devices.config import RawConfigError, DefaultConfigFactory
from provd.devices.device import needs_reconfiguration
from provd.localization import get_localization_service
from provd.operation import OIP_PROGRESS, OIP_FAIL, OIP_SUCCESS
from provd.persist.common import (
    ID_KEY,
    InvalidIdError as PersistInvalidIdError,
    NonDeletableError as PersistNonDeletableError,
)
from provd.plugins import PluginManager, PluginNotLoadedError
from provd.services import InvalidParameterError, JsonConfigPersister, \
    PersistentConfigureServiceDecorator
from provd.synchro import DeferredRWLock
from twisted.internet import defer
from provd.rest.server import auth
from provd.rest.server.helpers.tenants import Tenant, Tokens

logger = logging.getLogger(__name__)


class InvalidIdError(Exception):
    """Raised when a passed ID is invalid, not necessary because of its type,
    but because of its semantic.

    """
    pass


class DeviceNotInProvdTenantError(Exception):

    def __init__(self, tenant_uuid):
        super(DeviceNotInProvdTenantError, self).__init__('Device not in provd tenant')
        self.tenant_uuid = tenant_uuid


class TenantInvalidForDeviceError(Exception):

    def __init__(self, tenant_uuid):
        super(TenantInvalidForDeviceError, self).__init__('Tenant invalid for device')
        self.tenant_uuid = tenant_uuid


class NonDeletableError(Exception):
    """Raised when a document is non deletable"""
    pass


def _rlock_arg(rw_lock):
    def decorator(fun):
        @functools.wraps(fun)
        def aux(*args, **kwargs):
            d = rw_lock.read_lock.run(fun, *args, **kwargs)
            return d
        return aux
    return decorator


def _wlock_arg(rw_lock):
    def decorator(fun):
        @functools.wraps(fun)
        def aux(*args, **kwargs):
            d = rw_lock.write_lock.run(fun, *args, **kwargs)
            return d
        return aux
    return decorator


def _rlock(fun):
    # Decorator for instance method of ProvisioningApplication that need to
    # acquire the read lock
    @functools.wraps(fun)
    def aux(self, *args, **kwargs):
        d = self._rw_lock.read_lock.run(fun, self, *args, **kwargs)
        return d
    return aux


def _wlock(fun):
    # Decorator for instance method of ProvisioningApplication that need to
    # acquire the write lock
    @functools.wraps(fun)
    def aux(self, *args, **kwargs):
        d = self._rw_lock.write_lock.run(fun, self, *args, **kwargs)
        return d
    return aux


def _check_common_raw_config_validity(raw_config):
    for param in [u'ip', u'http_port', u'tftp_port']:
        if param not in raw_config:
            raise RawConfigError('missing %s parameter' % param)


def _check_raw_config_validity(raw_config):
    # XXX this is bit repetitive...
    _check_common_raw_config_validity(raw_config)
    if raw_config.get(u'ntp_enabled'):
        if u'ntp_ip' not in raw_config:
            raise RawConfigError('missing ntp_ip parameter')
    if raw_config.get(u'vlan_enabled'):
        if u'vlan_id' not in raw_config:
            raise RawConfigError('missing vlan_id parameter')
    if raw_config.get(u'syslog_enabled'):
        if u'syslog_ip' not in raw_config:
            raise RawConfigError('missing syslog_ip parameter')
    if u'sip_lines' in raw_config:
        for line_no, line in raw_config[u'sip_lines'].iteritems():
            if u'proxy_ip' not in line and u'sip_proxy_ip' not in raw_config:
                raise RawConfigError('missing proxy_ip parameter for line %s' %
                                     line_no)
            if u'protocol' in raw_config and raw_config[u'protocol'] == 'SIP':
                for param in [u'username', u'password', u'display_name']:
                    if param not in line:
                        raise RawConfigError('missing %s parameter for line %s' %
                                             (param, line_no))
    if u'sccp_call_managers' in raw_config:
        for priority, call_manager in raw_config[u'sccp_call_managers'].iteritems():
            if u'ip' not in call_manager:
                raise RawConfigError('missing ip parameter for call manager %s' %
                                     priority)
    if u'funckeys' in raw_config:
        funckeys = raw_config[u'funckeys']
        for funckey_no, funckey in funckeys.iteritems():
            try:
                type_ = funckey[u'type']
            except KeyError:
                raise RawConfigError('missing type parameter for funckey %s' %
                                     funckey_no)
            else:
                if (type_ == u'speeddial' or type_ == u'blf') and u'value' not in funckey:
                    raise RawConfigError('missing value parameter for funckey %s' %
                                         funckey_no)


def _set_defaults_raw_config(raw_config):
    if raw_config.get(u'syslog_enabled'):
        raw_config.setdefault(u'syslog_port', 514)
        raw_config.setdefault(u'level', u'warning')
    if u'sip_proxy_ip' in raw_config:
        raw_config.setdefault(u'sip_registrar_ip', raw_config[u'sip_proxy_ip'])
    raw_config.setdefault(u'sip_srtp_mode', u'disabled')
    raw_config.setdefault(u'sip_transport', u'udp')
    if u'sip_lines' not in raw_config:
        raw_config[u'sip_lines'] = {}
    else:
        for line in raw_config[u'sip_lines'].itervalues():
            if u'proxy_ip' in line:
                line.setdefault(u'registrar_ip', line[u'proxy_ip'])
            if u'username' in line:
                line.setdefault(u'auth_username', line[u'username'])
    raw_config.setdefault(u'sccp_call_managers', {})
    raw_config.setdefault(u'funckeys', {})


class ProvisioningApplication(object):
    """Main logic used to provision devices.

    Here's the restrictions on the devices/configs/plugins stored by instances
    of this class:
    - device can make references to unknown configs or plugins
    - configs can make references to unknown configs
    - a plugin can be uninstalled even if some devices make references to it
    - a config can be removed even if some devices or other configs make
      reference to it

    This class enforce the plugin contract.

    """
    # Note that, seen from the outside, all method acquiring a lock return a
    # deferred.

    def __init__(self, cfg_collection, dev_collection, config):
        self._cfg_collection = cfg_collection
        self._dev_collection = dev_collection
        self._splitted_config = config
        self._token = None
        self._tenant_uuid = None

        base_storage_dir = config['general']['base_storage_dir']
        plugins_dir = os.path.join(base_storage_dir, 'plugins')

        self.proxies = self._splitted_config.get('proxy', {})
        self.nat = 0

        self.pg_mgr = PluginManager(self,
                                    plugins_dir,
                                    config['general']['cache_dir'],
                                    config['general']['cache_plugin'],
                                    config['general']['check_compat_min'],
                                    config['general']['check_compat_max'])
        if 'plugin_server' in config['general']:
            self.pg_mgr.server = config['general']['plugin_server']

        # Do not move this line up unless you know what you are doing...
        cfg_service = ApplicationConfigureService(self.pg_mgr, self.proxies, self)
        persister = JsonConfigPersister(os.path.join(base_storage_dir,
                                                     'app.json'))
        self.configure_service = PersistentConfigureServiceDecorator(cfg_service, persister)

        self._base_raw_config = config['general']['base_raw_config']
        logger.info('Using base raw config %s', self._base_raw_config)
        _check_common_raw_config_validity(self._base_raw_config)
        self._rw_lock = DeferredRWLock()
        self._cfg_factory = DefaultConfigFactory()
        self._pg_load_all(True)

    @_wlock
    def close(self):
        logger.info('Closing provisioning application...')
        self.pg_mgr.close()
        logger.info('Provisioning application closed')

    def token(self):
        return self._token

    def set_token(self, token_id):
        logger.debug('Setting token for provd app: %s', token_id)
        self._token = token_id
        auth_client = auth.get_auth_client()
        token = Tokens(auth_client).get(self._token)
        self._tenant_uuid = Tenant.from_token(token).uuid

    def tenant_uuid(self):
        return self._tenant_uuid

    # device methods

    def _dev_get_plugin(self, device):
        if u'plugin' in device:
            return self.pg_mgr.get(device[u'plugin'])
        else:
            return None

    def _dev_get_raw_config(self, device):
        # Return a deferred that will fire with a raw config associated
        # with the device, or fire with None if there's no such raw config
        if u'config' in device:
            cfg_id = device[u'config']
            return self._cfg_collection.get_raw_config(cfg_id, self._base_raw_config)
        else:
            return defer.succeed(None)

    @defer.inlineCallbacks
    def _dev_get_plugin_and_raw_config(self, device):
        # Return a deferred that will fire with a tuple (plugin, raw_config)
        # associated with the device, or fire with the tuple (None, None) if
        # there's at least one without etc etc
        plugin = self._dev_get_plugin(device)
        if plugin is not None:
            raw_config = yield self._dev_get_raw_config(device)
            if raw_config is not None:
                defer.returnValue((plugin, raw_config))
        defer.returnValue((None, None))

    def _dev_configure(self, device, plugin, raw_config):
        # Return true if the device has been successfully configured (i.e.
        # no exception were raised), else false.
        logger.info('Configuring device %s with plugin %s', device[ID_KEY], plugin.id)
        try:
            _check_raw_config_validity(raw_config)
        except Exception:
            logger.error('Error while configuring device %s', device[ID_KEY],
                         exc_info=True)
        else:
            _set_defaults_raw_config(raw_config)
            try:
                plugin.configure(device, raw_config)
            except Exception:
                logger.error('Error while configuring device %s', device[ID_KEY],
                             exc_info=True)
            else:
                return True
        return False

    @defer.inlineCallbacks
    def _dev_configure_if_possible(self, device):
        # Return a deferred that fire with true if the device has been
        # successfully configured (i.e. no exception were raised), else false.
        plugin, raw_config = yield self._dev_get_plugin_and_raw_config(device)
        if plugin is None:
            defer.returnValue(False)
        else:
            defer.returnValue(self._dev_configure(device, plugin, raw_config))

    def _dev_deconfigure(self, device, plugin):
        # Return true if the device has been successfully deconfigured (i.e.
        # no exception were raised), else false.
        logger.info('Deconfiguring device %s with plugin %s', device[ID_KEY], plugin.id)
        try:
            plugin.deconfigure(device)
        except Exception:
            logger.error('Error while deconfiguring device %s', device[ID_KEY],
                         exc_info=True)
            return False
        else:
            return True

    def _dev_deconfigure_if_possible(self, device):
        # Return true if the device has been successfully configured (i.e.
        # no exception were raised), else false.
        plugin = self._dev_get_plugin(device)
        if plugin is None:
            return False
        else:
            return self._dev_deconfigure(device, plugin)

    def _dev_synchronize(self, device, plugin, raw_config):
        # Return a deferred that will fire with None once the device
        # synchronization is completed.
        logger.info('Synchronizing device %s with plugin %s', device[ID_KEY], plugin.id)
        _set_defaults_raw_config(raw_config)
        return plugin.synchronize(device, raw_config)

    @defer.inlineCallbacks
    def _dev_synchronize_if_possible(self, device):
        # Return a deferred that will fire with None once the device
        # synchronization is completed.
        plugin, raw_config = yield self._dev_get_plugin_and_raw_config(device)
        if plugin is None:
            # somewhat rare case were the device is marked as configured but
            # the plugin used by the device is not installed/loaded. This
            # is often caused by a manual plugin uninstallation
            raise Exception('Plugin %s is not installed/loaded' % device.get(u'plugin'))
        else:
            yield self._dev_synchronize(device, plugin, raw_config)

    @defer.inlineCallbacks
    def _dev_get_or_raise(self, id):
        device = yield self._dev_collection.retrieve(id)
        if device is None:
            raise InvalidIdError('invalid device ID "%s"' % id)
        else:
            defer.returnValue(device)

    @_wlock
    @defer.inlineCallbacks
    def dev_insert(self, device):
        """Insert a new device into the provisioning application.

        Return a deferred that will fire with the ID of the device.

        The deferred will fire it's errback with a ValueError if device
        is not a valid device object, i.e. invalid key value, invalid
        type, etc.

        The deferred will fire it's errback with an Exception if an 'id'
        key is specified but there's already one device with the same ID.

        If device has no 'id' key, one will be added after the device is
        successfully inserted.

        Device will be automatically configured if there's enough information
        to do so.

        Note that:
        - the value of 'configured' is ignored if given.
        - the passed in device object might be modified so that if the device
          has been inserted successfully, the device object has the same value
          as the one which has been inserted.

        """
        logger.info('Inserting new device')
        try:
            # new device are never configured
            device[u'configured'] = False

            if not device.get('tenant_uuid'):
                device['tenant_uuid'] = self._tenant_uuid

            device['is_new'] = device['tenant_uuid'] == self._tenant_uuid

            try:
                id = yield self._dev_collection.insert(device)
            except PersistInvalidIdError, e:
                raise InvalidIdError(e)
            else:
                configured = yield self._dev_configure_if_possible(device)
                if configured:
                    device[u'configured'] = True
                    yield self._dev_collection.update(device)
                defer.returnValue(id)
        except Exception:
            logger.error('Error while inserting device', exc_info=True)
            raise

    @_wlock
    @defer.inlineCallbacks
    def dev_update(self, device, pre_update_hook=None):
        """Update the device.

        The pre_update_hook function is called with the device and
        its config just before the device is persisted.

        Return a deferred that fire with None once the update is completed.

        The deferred will fire its errback with an exception if device has
        no 'id' key.

        The deferred will fire its errback with an InvalidIdError if device
        has unknown id.

        The device is automatically deconfigured/configured if needed.

        Note that the value of 'configured' is ignored if given.

        """
        try:
            try:
                id = device[ID_KEY]
            except KeyError:
                raise InvalidIdError('no id key for device %s' % device)
            else:
                logger.info('Updating device %s', id)
                old_device = yield self._dev_get_or_raise(id)
                if needs_reconfiguration(old_device, device):
                    # Deconfigure old device it was configured
                    if old_device[u'configured']:
                        self._dev_deconfigure_if_possible(old_device)
                    # Configure new device if possible
                    configured = yield self._dev_configure_if_possible(device)
                    device[u'configured'] = configured
                else:
                    device[u'configured'] = old_device[u'configured']
                if pre_update_hook is not None:
                    config = yield self._cfg_collection.retrieve(device.get(u'config'))
                    pre_update_hook(device, config)
                # Update device collection if the device is different from
                # the old device
                if device != old_device:
                    device['is_new'] = device['tenant_uuid'] == self._tenant_uuid
                    yield self._dev_collection.update(device)
                    # check if old device was using a transient config that is
                    # no more in use
                    if u'config' in old_device and old_device[u'config'] != device.get(u'config'):
                        old_device_cfg_id = old_device[u'config']
                        old_device_cfg = yield self._cfg_collection.retrieve(old_device_cfg_id)
                        if old_device_cfg and old_device_cfg.get(u'transient'):
                            # if no devices are using this transient config, delete it
                            if not (yield self._dev_collection.find_one({u'config': old_device_cfg_id})):
                                self._cfg_collection.delete(old_device_cfg_id)
                else:
                    logger.info('Not updating device %s: not changed', id)
        except Exception:
            logger.error('Error while updating device', exc_info=True)
            raise

    @_wlock
    @defer.inlineCallbacks
    def dev_delete(self, id):
        """Delete the device with the given ID.

        Return a deferred that will fire with None once the device is
        deleted.

        The deferred will fire its errback with an InvalidIdError if device
        has unknown id.

        The device is automatically deconfigured if needed.

        """
        logger.info('Deleting device %s', id)
        try:
            device = yield self._dev_get_or_raise(id)
            # Next line should never raise an exception since we successfully
            # retrieve the device with the same id just before and we are
            # using the write lock
            yield self._dev_collection.delete(id)
            # check if device was using a transient config that is no more in use
            if u'config' in device:
                device_cfg_id = device[u'config']
                device_cfg = yield self._cfg_collection.retrieve(device_cfg_id)
                if device_cfg and device_cfg.get(u'transient'):
                    # if no devices are using this transient config, delete it
                    if not (yield self._dev_collection.find_one({u'config': device_cfg_id})):
                        self._cfg_collection.delete(device_cfg_id)
            if device[u'configured']:
                self._dev_deconfigure_if_possible(device)
        except Exception:
            logger.error('Error while deleting device', exc_info=True)
            raise

    def dev_retrieve(self, id):
        """Return a deferred that fire with the device with the given ID, or
        fire with None if there's no such document.

        """
        return self._dev_collection.retrieve(id)

    def dev_find(self, selector, *args, **kwargs):
        return self._dev_collection.find(selector, *args, **kwargs)

    def dev_find_one(self, selector, *args, **kwargs):
        return self._dev_collection.find_one(selector, *args, **kwargs)

    @_wlock
    @defer.inlineCallbacks
    def dev_reconfigure(self, id):
        """Force the reconfiguration of the device. This is usually not
        necessary since configuration is usually done automatically.

        Return a deferred that will fire once the device reconfiguration is
        completed, with either True if the device has been successfully
        reconfigured or else False.

        The deferred will fire its errback with an exception if id is not a
        valid device ID.

        """
        logger.info('Reconfiguring device %s', id)
        try:
            device = yield self._dev_get_or_raise(id)
            if device[u'configured']:
                self._dev_deconfigure_if_possible(device)
            configured = yield self._dev_configure_if_possible(device)
            if device[u'configured'] != configured:
                device[u'configured'] = configured
                yield self._dev_collection.update(device)
            defer.returnValue(configured)
        except Exception:
            logger.error('Error while reconfiguring device', exc_info=True)
            raise

    @_rlock
    @defer.inlineCallbacks
    def dev_synchronize(self, id):
        """Synchronize the physical device with its config.

        Return a deferred that will fire with None once the device is
        synchronized.

        The deferred will fire its errback with an exception if id is not a
        valid device ID.

        The deferred will fire its errback with an exception if the device
        can't be synchronized, either because it has not been configured yet,
        does not support synchronization or if the operation just seem to
        have failed.

        """
        logger.info('Synchronizing device %s', id)
        try:
            device = yield self._dev_get_or_raise(id)
            if not device[u'configured']:
                raise Exception('can\'t synchronize not configured device %s' % id)
            else:
                yield self._dev_synchronize_if_possible(device)
        except Exception:
            logger.error('Error while synchronizing device', exc_info=True)
            raise

    # config methods

    @defer.inlineCallbacks
    def _cfg_get_or_raise(self, id):
        config = yield self._cfg_collection.retrieve(id)
        if config is None:
            raise InvalidIdError('invalid config ID "%s"' % id)
        else:
            defer.returnValue(config)

    @_wlock
    @defer.inlineCallbacks
    def cfg_insert(self, config):
        """Insert a new config into the provisioning application.

        Return a deferred that will fire with the ID of the config.

        The deferred will fire it's errback with a ValueError if config
        is not a valid config object, i.e. invalid key value, invalid
        type, etc.

        The deferred will fire it's errback with an Exception if an 'id'
        key is specified but there's already one config with the same ID.

        If config has no 'id' key, one will be added after the config is
        successfully inserted.

        """
        logger.info('Inserting config %s', config.get(ID_KEY))
        try:
            try:
                id = yield self._cfg_collection.insert(config)
            except PersistInvalidIdError, e:
                raise InvalidIdError(e)
            else:
                # configure each device that depend on the newly inserted config
                # 1. get the set of affected configs
                affected_cfg_ids = yield self._cfg_collection.get_descendants(id)
                affected_cfg_ids.add(id)
                # 2. get the raw_config of every affected config
                raw_configs = {}
                for affected_cfg_id in affected_cfg_ids:
                    raw_configs[affected_cfg_id] = yield self._cfg_collection.get_raw_config(
                                                             affected_cfg_id, self._base_raw_config)
                # 3. reconfigure/deconfigure each affected devices
                affected_devices = yield self._dev_collection.find({u'config': {u'$in': list(affected_cfg_ids)}})
                for device in affected_devices:
                    plugin = self._dev_get_plugin(device)
                    if plugin is not None:
                        raw_config = raw_configs[device[u'config']]
                        assert raw_config is not None
                        # deconfigure
                        if device[u'configured']:
                            self._dev_deconfigure(device, plugin)
                        # configure
                        configured = self._dev_configure(device, plugin, raw_config)
                        # update device if it has changed
                        if device[u'configured'] != configured:
                            device[u'configured'] = configured
                            yield self._dev_collection.update(device)
                # 4. return the device id
                defer.returnValue(id)
        except Exception:
            logger.error('Error while inserting config', exc_info=True)
            raise

    @_wlock
    @defer.inlineCallbacks
    def cfg_update(self, config):
        """Update the config.

        Return a deferred that fire with None once the update is completed.

        The deferred will fire its errback with an exception if config has
        no 'id' key.

        The deferred will fire its errback with an InvalidIdError if config
        has unknown id.

        Note that device might be reconfigured.

        """
        try:
            try:
                id = config[ID_KEY]
            except KeyError:
                raise InvalidIdError('no id key for config %s' % config)
            else:
                logger.info('Updating config %s', id)
                old_config = yield self._cfg_get_or_raise(id)
                if old_config == config:
                    logger.info('config has not changed, ignoring update')
                else:
                    yield self._cfg_collection.update(config)
                    affected_cfg_ids = yield self._cfg_collection.get_descendants(id)
                    affected_cfg_ids.add(id)
                    # 2. get the raw_config of every affected config
                    raw_configs = {}
                    for affected_cfg_id in affected_cfg_ids:
                        raw_configs[affected_cfg_id] = yield self._cfg_collection.get_raw_config(
                                                                 affected_cfg_id, self._base_raw_config)
                    # 3. reconfigure each device having a direct dependency on
                    #    one of the affected cfg id
                    affected_devices = yield self._dev_collection.find({u'config': {u'$in': list(affected_cfg_ids)}})
                    for device in affected_devices:
                        plugin = self._dev_get_plugin(device)
                        if plugin is not None:
                            raw_config = raw_configs[device[u'config']]
                            assert raw_config is not None
                            # deconfigure
                            if device[u'configured']:
                                self._dev_deconfigure(device, plugin)
                            # configure
                            configured = self._dev_configure(device, plugin, raw_config)
                            # update device if it has changed
                            if device[u'configured'] != configured:
                                device[u'configured'] = configured
                                yield self._dev_collection.update(device)
        except Exception:
            logger.error('Error while updating config', exc_info=True)
            raise

    @_wlock
    @defer.inlineCallbacks
    def cfg_delete(self, id):
        """Delete the config with the given ID. Does not delete any reference
        to it from other configs.

        Return a deferred that will fire with None once the config is
        deleted.

        The deferred will fire its errback with an InvalidIdError if config
        has unknown id.

        The devices depending directly or indirectly over this config are
        automatically reconfigured if needed.

        """
        logger.info('Deleting config %s', id)
        try:
            try:
                yield self._cfg_collection.delete(id)
            except PersistInvalidIdError, e:
                raise InvalidIdError(e)
            except PersistNonDeletableError, e:
                raise NonDeletableError(e)
            else:
                # 1. get the set of affected configs
                affected_cfg_ids = yield self._cfg_collection.get_descendants(id)
                affected_cfg_ids.add(id)
                # 2. get the raw_config of every affected config
                raw_configs = {}
                for affected_cfg_id in affected_cfg_ids:
                    raw_configs[affected_cfg_id] = yield self._cfg_collection.get_raw_config(
                                                             affected_cfg_id, self._base_raw_config)
                # 3. reconfigure/deconfigure each affected devices
                affected_devices = yield self._dev_collection.find({u'config': {u'$in': list(affected_cfg_ids)}})
                for device in affected_devices:
                    plugin = self._dev_get_plugin(device)
                    if plugin is not None:
                        raw_config = raw_configs[device[u'config']]
                        # deconfigure
                        if device[u'configured']:
                            self._dev_deconfigure(device, plugin)
                        # configure if device config is not the deleted config
                        if device[u'config'] == id:
                            assert raw_config is None
                            # update device if it has changed
                            if device[u'configured']:
                                device[u'configured'] = False
                                yield self._dev_collection.update(device)
                        else:
                            assert raw_config is not None
                            configured = yield self._dev_configure(device, plugin, raw_config)
                            # update device if it has changed
                            if device[u'configured'] != configured:
                                yield self._dev_collection.update(device)
        except Exception:
            logger.error('Error while deleting config', exc_info=True)
            raise

    def cfg_retrieve(self, id):
        """Return a deferred that fire with the config with the given ID, or
        fire with None if there's no such document.

        """
        return self._cfg_collection.retrieve(id)

    def cfg_retrieve_raw_config(self, id):
        return self._cfg_collection.get_raw_config(id, self._base_raw_config)

    def cfg_find(self, selector, *args, **kwargs):
        return self._cfg_collection.find(selector, *args, **kwargs)

    def cfg_find_one(self, selector, *args, **kwargs):
        return self._cfg_collection.find_one(selector, *args, **kwargs)

    @_wlock
    @defer.inlineCallbacks
    def cfg_create_new(self):
        """Create a new config from the config with the autocreate role.

        Return a deferred that will fire with the ID of the newly created
        config, or fire with None if there's no config with the autocreate
        role or if the config factory returned None.

        """
        logger.info('Creating new config')
        try:
            new_config_id = None
            config = yield self._cfg_collection.find_one({u'role': u'autocreate'})
            if config:
                # remove the role of the config so we don't create new config
                # with the autocreate role
                del config[u'role']
                new_config = self._cfg_factory(config)
                if new_config:
                    new_config_id = yield self._cfg_collection.insert(new_config)
                else:
                    logger.debug('Autocreate config factory returned null config')
            else:
                logger.debug('No config with the autocreate role found')

            defer.returnValue(new_config_id)
        except Exception:
            logger.error('Error while autocreating config', exc_info=True)
            raise

    # plugin methods

    def _pg_load_all(self, catch_error=False):
        logger.info('Loading all plugins')
        loaded_plugins = 0
        for pg_id in self.pg_mgr.list_installed():
            try:
                self._pg_load(pg_id)
                loaded_plugins += 1
            except Exception:
                if catch_error:
                    logger.error('Could not load plugin %s', pg_id)
                else:
                    raise
        logger.info('Loaded %d plugins.', loaded_plugins)

    def _pg_configure_pg(self, id):
        # Raise an exception if configure_common fail
        plugin = self.pg_mgr[id]
        common_config = copy.deepcopy(self._base_raw_config)
        logger.info('Configuring plugin %s with config %s', id, common_config)
        try:
            plugin.configure_common(common_config)
        except Exception:
            logger.error('Error while configuring plugin %s', id, exc_info=True)
            raise

    def _pg_load(self, id):
        # Raise an exception if plugin loading or common configuration fail
        gen_cfg = dict(self._splitted_config['general'])
        gen_cfg['proxies'] = self.proxies
        spec_cfg = dict(self._splitted_config.get('plugin_config', {}).get(id, {}))
        try:
            self.pg_mgr.load(id, gen_cfg, spec_cfg)
        except Exception:
            logger.error('Error while loading plugin %s', id, exc_info=True)
            raise
        else:
            self._pg_configure_pg(id)

    def _pg_unload(self, id):
        # This method should never raise an exception
        try:
            self.pg_mgr.unload(id)
        except PluginNotLoadedError:
            # this is the case were an incompatible/bogus plugin has been
            # installed succesfully but the plugin was not loadable
            logger.info('Plugin %s was not loaded ', id)

    @defer.inlineCallbacks
    def _pg_configure_all_devices(self, plugin_id):
        logger.info('Reconfiguring all devices using plugin %s', plugin_id)
        devices = yield self._dev_collection.find({u'plugin': plugin_id})
        for device in devices:
            # deconfigure
            if device[u'configured']:
                self._dev_deconfigure_if_possible(device)
            # configure
            configured = yield self._dev_configure_if_possible(device)
            if device[u'configured'] != configured:
                device[u'configured'] = configured
                yield self._dev_collection.update(device)

    def pg_install(self, id):
        """Install the plugin with the given id.

        Return a tuple (deferred, operation in progress).

        This method raise the following exception:
          - an Exception if the plugin is already installed.
          - an Exception if there's no installable plugin with the specified
            name.
          - an Exception if there's already an install/upgrade operation
            in progress for the plugin.
          - an InvalidParameterError if the plugin package is not in cache
            and no 'server' param has been set.

        Affected devices are automatically configured if needed.

        """
        logger.info('Installing and loading plugin %s', id)
        if self.pg_mgr.is_installed(id):
            logger.error('Error: plugin %s is already installed', id)
            raise Exception('plugin %s is already installed' % id)

        def callback1(_):
            # reset the state to in progress
            oip.state = OIP_PROGRESS
        @_wlock_arg(self._rw_lock)
        def callback2(_):
            # The lock apply only to the deferred return by this function
            # and not on the function itself
            # next line might raise an exception, which is ok
            self._pg_load(id)
            return self._pg_configure_all_devices(id)
        def callback3(_):
            oip.state = OIP_SUCCESS
        def errback3(err):
            oip.state = OIP_FAIL
            return err
        deferred, oip = self.pg_mgr.install(id)
        deferred.addCallback(callback1)
        deferred.addCallback(callback2)
        deferred.addCallbacks(callback3, errback3)
        return deferred, oip

    def pg_upgrade(self, id):
        """Upgrade the plugin with the given id.

        Same contract as pg_install, except that the plugin must already be
        installed.

        Affected devices are automatically reconfigured if needed.

        """
        logger.info('Upgrading and reloading plugin %s', id)
        if not self.pg_mgr.is_installed(id):
            logger.error('Error: plugin %s is not already installed', id)
            raise Exception('plugin %s is not already installed' % id)

        def callback1(_):
            # reset the state to in progress
            oip.state = OIP_PROGRESS
        @_wlock_arg(self._rw_lock)
        def callback2(_):
            # The lock apply only to the deferred return by this function
            # and not on the function itself
            if id in self.pg_mgr:
                self._pg_unload(id)
            # next line might raise an exception, which is ok
            self._pg_load(id)
            return self._pg_configure_all_devices(id)
        def callback3(_):
            oip.state = OIP_SUCCESS
        def errback3(err):
            oip.state = OIP_FAIL
            return err
        # XXX we probably want to check that the plugin is 'really' upgradeable
        deferred, oip = self.pg_mgr.upgrade(id)
        deferred.addCallback(callback1)
        deferred.addCallback(callback2)
        deferred.addCallbacks(callback3, errback3)
        return deferred, oip

    @_wlock
    @defer.inlineCallbacks
    def pg_uninstall(self, id):
        """Uninstall the plugin with the given id.

        Return a deferred that will fire with None once the operation is
        completed.

        The deferred will fire its errback with an Exception if the plugin
        is not already installed.

        Affected devices are automatically deconfigured if needed.

        """
        logger.info('Uninstalling and unloading plugin %s', id)
        self.pg_mgr.uninstall(id)
        self._pg_unload(id)
        # soft deconfigure all the device that were configured by this device
        # note that there is no point in calling plugin.deconfigure for every
        # of these devices since the plugin is removed anyway
        affected_devices = yield self._dev_collection.find({u'plugin': id,
                                                            u'configured': True})
        for device in affected_devices:
            device[u'configured'] = False
            yield self._dev_collection.update(device)

    @_wlock
    @defer.inlineCallbacks
    def pg_reload(self, id):
        """Reload the plugin with the given id.

        If the plugin is not loaded yet, load it.

        Return a deferred that will fire with None once the operation is
        completed.

        The deferred will fire its errback with an exception if the plugin
        is not already installed or if there's an error at loading.

        """
        logger.info('Reloading plugin %s', id)
        if not self.pg_mgr.is_installed(id):
            logger.error('Can\'t reload plugin %s: not installed', id)
            raise Exception('plugin %s is not installed' % id)

        devices = yield self._dev_collection.find({u'plugin': id})
        devices = list(devices)
        # unload plugin
        if id in self.pg_mgr:
            plugin = self.pg_mgr[id]
            for device in devices:
                if device[u'configured']:
                    self._dev_deconfigure(device, plugin)
            self._pg_unload(id)
        # load plugin
        try:
            self._pg_load(id)
        except Exception:
            # mark all the devices as not configured and reraise
            # the exception
            for device in devices:
                if device[u'configured']:
                    device[u'configured'] = False
                    yield self._dev_collection.update(device)
            raise
        else:
            # reconfigure every device
            for device in devices:
                configured = yield self._dev_configure_if_possible(device)
                if device[u'configured'] != configured:
                    device[u'configured'] = configured
                    yield self._dev_collection.update(device)

    def pg_retrieve(self, id):
        return self.pg_mgr[id]


def _check_is_server_url(value):
    if value is None:
        return

    try:
        parse_result = urlparse.urlparse(value)
    except Exception, e:
        raise InvalidParameterError(e)
    else:
        if not parse_result.scheme:
            raise InvalidParameterError('no scheme: %s' % value)
        if not parse_result.hostname:
            raise InvalidParameterError('no hostname: %s' % value)


def _check_is_proxy(value):
    if value is None:
        return

    try:
        parse_result = urlparse.urlparse(value)
    except Exception, e:
        raise InvalidParameterError(e)
    else:
        if not parse_result.scheme:
            raise InvalidParameterError('no scheme: %s' % value)
        if not parse_result.hostname:
            raise InvalidParameterError('no hostname: %s' % value)
        if parse_result.path:
            raise InvalidParameterError('path: %s' % value)


def _check_is_https_proxy(value):
    if value is None:
        return

    if not value:
        raise InvalidParameterError('zero-length value')
    try:
        parse_result = urlparse.urlparse(value)
    except Exception, e:
        raise InvalidParameterError(e)
    else:
        if parse_result.scheme and parse_result.hostname:
            raise InvalidParameterError('scheme and hostname: %s' % value)


class ApplicationConfigureService(object):
    def __init__(self, pg_mgr, proxies, app):
        self._pg_mgr = pg_mgr
        self._proxies = proxies
        self._app = app

    def _get_param_locale(self):
        l10n_service = get_localization_service()
        if l10n_service is None:
            logger.info('No localization service registered')
            return None
        else:
            value = l10n_service.get_locale()
            if value is None:
                return None
            else:
                return value.decode('ascii')

    def _set_param_locale(self, value):
        l10n_service = get_localization_service()
        if l10n_service is None:
            logger.info('No localization service registered')
        else:
            if not value:
                l10n_service.set_locale(None)
            else:
                try:
                    l10n_service.set_locale(value.encode('ascii'))
                except (UnicodeError, ValueError), e:
                    raise InvalidParameterError(e)

    def _generic_set_proxy(self, key, value):
        if not value:
            if key in self._proxies:
                del self._proxies[key]
        else:
            self._proxies[key] = value

    def _get_param_http_proxy(self):
        return self._proxies.get('http')

    def _set_param_http_proxy(self, value):
        _check_is_proxy(value)
        self._generic_set_proxy('http', value)

    def _get_param_ftp_proxy(self):
        return self._proxies.get('ftp')

    def _set_param_ftp_proxy(self, value):
        _check_is_proxy(value)
        self._generic_set_proxy('ftp', value)

    def _get_param_https_proxy(self):
        return self._proxies.get('https')

    def _set_param_https_proxy(self, value):
        _check_is_https_proxy(value)
        self._generic_set_proxy('https', value)

    def _get_param_plugin_server(self):
        return self._pg_mgr.server

    def _set_param_plugin_server(self, value):
        _check_is_server_url(value)
        self._pg_mgr.server = value

    def _get_param_NAT(self):
        return self._app.nat

    def _set_param_NAT(self, value):
        if value is None or value == '0':
            value = 0
        elif value == '1':
            value = 1
        else:
            raise InvalidParameterError(value)
        self._app.nat = value

    def get(self, name):
        get_fun_name = '_get_param_%s' % name
        try:
            get_fun = getattr(self, get_fun_name)
        except AttributeError:
            raise KeyError(name)
        else:
            return get_fun()

    def set(self, name, value):
        set_fun_name = '_set_param_%s' % name
        try:
            set_fun = getattr(self, set_fun_name)
        except AttributeError:
            raise KeyError(name)
        else:
            set_fun(value)

    description = [
        (u'plugin_server', u'The plugins repository URL'),
        (u'http_proxy', u'The proxy for HTTP requests. Format is "http://[user:password@]host:port"'),
        (u'ftp_proxy', u'The proxy for FTP requests. Format is "http://[user:password@]host:port"'),
        (u'https_proxy', u'The proxy for HTTPS requests. Format is "host:port"'),
        (u'locale', u'The current locale. Example: fr_FR'),
        (u'NAT', u'Set to 1 if all the devices are behind a NAT.')
    ]

    description_fr = [
        (u'plugin_server', u"L'addresse (URL) du dépôt de plugins"),
        (u'http_proxy', u'Le proxy pour les requêtes HTTP. Le format est "http://[user:password@]host:port"'),
        (u'ftp_proxy', u'Le proxy pour les requêtes FTP. Le format est "http://[user:password@]host:port"'),
        (u'https_proxy', u'Le proxy pour les requêtes HTTPS. Le format est "host:port"'),
        (u'locale', u'La locale courante. Exemple: en_CA'),
        (u'NAT', u'Mettre à 1 si toutes les terminaisons sont derrière un NAT.')
    ]

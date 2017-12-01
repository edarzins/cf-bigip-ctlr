#!/usr/bin/env python

# Copyright 2016, 2017 F5 Networks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import absolute_import

import argparse
import fcntl
import hashlib
import json
import logging
import os
import os.path
import sys
import time
import threading
import signal

import pyinotify

from urlparse import urlparse
from f5_cccl.api import F5CloudServiceManager
from f5_cccl.exceptions import F5CcclError
from f5_cccl.utils.mgmt import mgmt_root
from f5_cccl.utils.network import apply_network_fdb_config
from f5_cccl.utils.profile import (delete_unused_ssl_profiles,
                                   create_client_ssl_profile,
                                   create_server_ssl_profile)

log = logging.getLogger(__name__)
console = logging.StreamHandler()
console.setFormatter(
    logging.Formatter("[%(asctime)s %(name)s %(levelname)s] %(message)s"))
root_logger = logging.getLogger()
root_logger.addHandler(console)


class ResponseStatusFilter(logging.Filter):
    def filter(self, record):
        return not record.getMessage().startswith("RESPONSE::STATUS")


class CertFilter(logging.Filter):
    def filter(self, record):
        return "CERTIFICATE" not in record.getMessage()


class KeyFilter(logging.Filter):
    def filter(self, record):
        return "PRIVATE KEY" not in record.getMessage()


root_logger.addFilter(ResponseStatusFilter())
root_logger.addFilter(CertFilter())
root_logger.addFilter(KeyFilter())


DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_VERIFY_INTERVAL = 30.0


class CloudServiceManager():
    """CloudServiceManager class.

    Applies a configuration to a BigIP

    Args:
        bigip: ManagementRoot object
        partition: BIG-IP partition to manage
    """

    def __init__(self, bigip, partition):
        """Initialize the CloudServiceManager object."""
        self._mgmt_root = bigip
        self._cccl = F5CloudServiceManager(
            bigip,
            partition,
            prefix="")

    def mgmt_root(self):
        """ Return the BIG-IP ManagementRoot object"""
        return self._mgmt_root

    def get_partition(self):
        """ Return the managed partition."""
        return self._cccl.get_partition()

    def _apply_ltm_config(self, config):
        """Apply the configuration to the BIG-IP.

        Args:
            config: BIG-IP config dict
        """
        return self._cccl.apply_config(config)


class IntervalTimerError(Exception):
    def __init__(self, msg):
        Exception.__init__(self, msg)


class IntervalTimer(object):
    def __init__(self, interval, cb):
        float(interval)
        if 0 >= interval:
            raise IntervalTimerError("interval must be greater than 0")

        if not cb or not callable(cb):
            raise IntervalTimerError("cb must be callable object")

        self._cb = cb
        self._interval = interval
        self._execution_time = 0.0
        self._running = False
        self._timer = None
        self._lock = threading.RLock()

    def _set_execution_time(self, start_time, stop_time):
        if stop_time >= start_time:
            self._execution_time = stop_time - start_time
        else:
            self._execution_time = 0.0

    def _adjust_interval(self):
        adjusted_interval = self._interval - self._execution_time
        if adjusted_interval < 0.0:
            adjusted_interval = 0.0
        self._execution_time = 0.0
        return adjusted_interval

    def _run(self):
        start_time = time.clock()
        try:
            self._cb()
        except Exception:
            log.exception('Unexpected error')
        finally:
            with self._lock:
                stop_time = time.clock()
                self._set_execution_time(start_time, stop_time)
                if self._running:
                    self.start()

    def is_running(self):
        return self._running

    def start(self):
        with self._lock:
            if self._running:
                # restart timer, possibly with a new interval
                self.stop()
            self._timer = threading.Timer(self._adjust_interval(), self._run)
            # timers can't be stopped, cancel just prevents the callback from
            # occuring when the timer finally expires.  Make it a daemon allows
            # cancelled timers to exit eventually without a need for join.
            self._timer.daemon = True
            self._timer.start()
            self._running = True

    def stop(self):
        with self._lock:
            if self._running:
                self._timer.cancel()
                self._timer = None
                self._running = False


class ConfigError(Exception):
    def __init__(self, msg):
        Exception.__init__(self, msg)


def create_ltm_config(partition, config):
    """Extract a BIG-IP configuration from the LTM configuration.

    Args:
        config: BigIP config
    """
    ltm = {}
    if 'resources' in config and partition in config['resources']:
        ltm = config['resources'][partition]

    log.debug("Service Config: %s", json.dumps(ltm))
    return ltm


def create_network_config(config):
    """Extract a BIG-IP Network configuration from the network config.

    Args:
        config: BigIP config which contains openshift-sdn defs
    """
    f5_network = {}
    if 'openshift-sdn' in config:
        f5_network['fdb'] = config['openshift-sdn']

    return f5_network


def _create_custom_profiles(mgmt, partition, custom_profiles):
    incomplete = 0

    customProfiles = False
    for profile in custom_profiles:
        if profile['context'] == 'clientside':
            incomplete += create_client_ssl_profile(mgmt, partition, profile)
            customProfiles = True
        elif profile['context'] == 'serverside':
            incomplete += create_server_ssl_profile(mgmt, partition, profile)
            customProfiles = True
        else:
            log.error(
                "Only client or server custom profiles are supported.")

    return customProfiles, incomplete


def _delete_unused_ssl_profiles(mgmt, partition, config):
    return delete_unused_ssl_profiles(mgmt, partition, config)


class ConfigHandler():
    def __init__(self, config_file, managers, verify_interval):
        self._config_file = config_file
        self._managers = managers

        self._condition = threading.Condition()
        self._thread = threading.Thread(target=self._do_reset)
        self._pending_reset = False
        self._stop = False
        self._backoff_time = 1
        self._backoff_timer = None
        self._max_backoff_time = 128

        self._interval = None
        self._verify_interval = 0
        self.set_interval_timer(verify_interval)

        self._thread.start()

    def set_interval_timer(self, verify_interval):
        if verify_interval != self._verify_interval:
            if self._interval is not None:
                self._interval.stop()
                self._interval = None

            self._verify_interval = verify_interval
            if self._verify_interval > 0:
                self._interval = IntervalTimer(self._verify_interval,
                                               self.notify_reset)

    def stop(self):
        self._condition.acquire()
        self._stop = True
        self._condition.notify()
        self._condition.release()
        if self._backoff_timer is not None:
            self.cleanup_backoff()

    def notify_reset(self):
        self._condition.acquire()
        self._pending_reset = True
        self._condition.notify()
        self._condition.release()

    def _do_reset(self):
        log.debug('config handler thread start')

        with self._condition:
            # customProfiles is true when we've written out a custom profile.
            # Once we know we've written out a profile, we can call delete
            # if needed.
            customProfiles = False
            while True:
                self._condition.acquire()
                if not self._pending_reset and not self._stop:
                    self._condition.wait()
                log.debug('config handler woken for reset')

                self._pending_reset = False
                self._condition.release()

                if self._stop:
                    log.info('stopping config handler')
                    if self._backoff_timer is not None:
                        self.cleanup_backoff()
                    break

                start_time = time.time()

                config = _parse_config(self._config_file)
                # No 'resources' indicates that the controller is not
                # yet ready -- it does not mean to apply an empty config
                if 'resources' not in config:
                    continue
                verify_interval, _ = _handle_global_config(config)
                _handle_openshift_sdn_config(config)
                self.set_interval_timer(verify_interval)

                cfg_network = create_network_config(config)
                incomplete = 0

                for mgr in self._managers:
                    partition = mgr.get_partition()
                    cfg_ltm = create_ltm_config(partition, config)
                    try:
                        # Manually create custom profiles;
                        # CCCL doesn't yet do this
                        if 'customProfiles' in cfg_ltm:
                            tmp = 0
                            customProfiles, tmp = _create_custom_profiles(
                                mgr.mgmt_root(),
                                partition,
                                cfg_ltm['customProfiles'])
                            incomplete += tmp

                        # Apply the BIG-IP config after creating profiles
                        # and before deleting profiles
                        incomplete += mgr._apply_ltm_config(cfg_ltm)

                        # Manually delete custom profiles (if needed)
                        if customProfiles:
                            _delete_unused_ssl_profiles(
                                mgr.mgmt_root(),
                                partition,
                                cfg_ltm)

                    except F5CcclError as e:
                        # We created an invalid configuration, raise the
                        # exception and fail
                        log.error("CCCL Error: %s", e.msg)
                        raise e

                if 'fdb' in cfg_network:
                    incomplete += apply_network_fdb_config(
                        self._managers[0].mgmt_root(),
                        cfg_network['fdb'])

                if incomplete:
                    # Error occurred, perform retries
                    self.handle_backoff()
                else:
                    if (self._interval and self._interval.is_running()
                            is False):
                        self._interval.start()
                    self._backoff_time = 1
                    if self._backoff_timer is not None:
                        self.cleanup_backoff()

                perf_enable = os.environ.get('SCALE_PERF_ENABLE')
                if perf_enable:  # pragma: no cover
                    test_data = {}
                    app_count = 0
                    backend_count = 0
                    for service in config['resources']['test'][
                            'virtualServers']:
                        app_count += 1
                        backends = 0
                        for pool in config['resources']['test']['pools']:
                            if pool['name'] == service['name']:
                                backends = len(pool['members'])
                                break
                        test_data[service['name']] = backends
                        backend_count += backends
                    test_data['Total_Services'] = app_count
                    test_data['Total_Backends'] = backend_count
                    test_data['Time'] = time.time()
                    json_data = json.dumps(test_data)
                    log.info('SCALE_PERF: Test data: %s',
                             json_data)

                log.debug('updating tasks finished, took %s seconds',
                          time.time() - start_time)

        if self._interval:
            self._interval.stop()

    def cleanup_backoff(self):
        """Cleans up canceled backoff timers."""
        self._backoff_timer.cancel()
        self._backoff_timer.join()
        self._backoff_timer = None

    def handle_backoff(self):
        """Wrapper for calls to retry_backoff."""
        if (self._interval and self._interval.is_running() is
                True):
            self._interval.stop()
        if self._backoff_timer is None:
            self.retry_backoff()

    def retry_backoff(self):
        """Add a backoff timer to retry in case of failure."""
        def timer_cb():
            self._backoff_timer = None
            self.notify_reset()

        self._backoff_timer = threading.Timer(
            self._backoff_time, timer_cb
        )
        log.error("Error applying config, will try again in %s seconds",
                  self._backoff_time)
        self._backoff_timer.start()
        if self._backoff_time < self._max_backoff_time:
            self._backoff_time *= 2


class ConfigWatcher(pyinotify.ProcessEvent):
    def __init__(self, config_file, on_change):
        basename = os.path.basename(config_file)
        if not basename or 0 == len(basename):
            raise ConfigError('config_file must be a file path')

        self._config_file = config_file
        self._on_change = on_change

        self._config_dir = os.path.dirname(self._config_file)
        self._config_stats = None
        if os.path.exists(self._config_file):
            try:
                self._config_stats = self._md5()
            except IOError as ioe:
                log.warning('ioerror during md5 sum calculation: {}'.
                            format(ioe))

        self._running = False
        self._polling = False
        self._user_abort = False
        signal.signal(signal.SIGINT, self._exit_gracefully)
        signal.signal(signal.SIGTERM, self._exit_gracefully)

    def _exit_gracefully(self, signum, frame):
        self._user_abort = True
        self._running = False

    def _loop_check(self, notifier):
        if self._polling:
            log.debug('inotify loop ended - returning to polling mode')
            return True
        else:
            return False

    def loop(self):
        self._running = True
        if not os.path.exists(self._config_dir):
            log.info(
                'configured directory doesn\'t exist {}, entering poll loop'.
                format(self._config_dir))
            self._polling = True

        while self._running:
            try:
                while self._polling:
                    if self._polling:
                        if os.path.exists(self._config_dir):
                            log.debug('found watchable directory - {}'.format(
                                self._config_dir))
                            self._polling = False
                            break
                        else:
                            log.debug('waiting for watchable directory - {}'.
                                      format(self._config_dir))
                            time.sleep(1)

                _wm = pyinotify.WatchManager()
                _notifier = pyinotify.Notifier(_wm, default_proc_fun=self)
                _notifier.coalesce_events(True)
                mask = (pyinotify.IN_CREATE | pyinotify.IN_DELETE |
                        pyinotify.IN_MOVED_FROM | pyinotify.IN_MOVED_TO |
                        pyinotify.IN_CLOSE_WRITE | pyinotify.IN_MOVE_SELF |
                        pyinotify.IN_DELETE_SELF)
                _wm.add_watch(
                    path=self._config_dir,
                    mask=mask,
                    quiet=False,
                    exclude_filter=lambda path: False)

                log.info('entering inotify loop to watch {}'.format(
                    self._config_file))
                _notifier.loop(callback=self._loop_check)

                if (not self._polling and _notifier._fd is None):
                    log.info('terminating')
                    self._running = False
            except Exception as e:
                log.warning(e)

        if self._user_abort:
            log.info('Received user kill signal, terminating.')

    def _md5(self):
        md5 = hashlib.md5()

        with open(self._config_file, 'rb') as f:
            fcntl.lockf(f.fileno(), fcntl.LOCK_SH, 0, 0, 0)
            while True:
                buf = f.read(4096)
                if not buf:
                    break
                md5.update(buf)
            fcntl.lockf(f.fileno(), fcntl.LOCK_UN, 0, 0, 0)
        return md5.digest()

    def _should_watch(self, pathname):
        if pathname == self._config_file:
            return True
        return False

    def _is_changed(self):
        changed = False
        cur_hash = None
        if not os.path.exists(self._config_file):
            if cur_hash != self._config_stats:
                changed = True
            else:
                changed = False
        else:
            try:
                cur_hash = self._md5()
                if cur_hash != self._config_stats:
                    changed = True
                else:
                    changed = False
            except IOError as ioe:
                log.warning('ioerror during md5 sum calculation: {}'.
                            format(ioe))

        return (changed, cur_hash)

    def process_default(self, event):
        if (pyinotify.IN_DELETE_SELF == event.mask or
                pyinotify.IN_MOVE_SELF == event.mask):
            log.warn(
                'watchpoint {} has been moved or destroyed, using poll loop'.
                format(self._config_dir))
            self._polling = True

            if self._config_stats is not None:
                log.debug('config file {} changed, parent gone'.format(
                    self._config_file))
                self._config_stats = None
                self._on_change()

        if self._should_watch(event.pathname):
            (changed, md5) = self._is_changed()

            if changed:
                log.debug('config file {0} changed - signalling bigip'.format(
                    self._config_file, self._config_stats, md5))
                self._config_stats = md5
                self._on_change()


def _parse_config(config_file):
    if os.path.exists(config_file):
        with open(config_file, 'r') as config:
            fcntl.lockf(config.fileno(), fcntl.LOCK_SH, 0, 0, 0)
            config_json = json.load(config)
            fcntl.lockf(config.fileno(), fcntl.LOCK_UN, 0, 0, 0)
            log.debug('loaded configuration file successfully')
            return config_json
    else:
        return None


def _handle_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
            '--config-file',
            type=str,
            required=True,
            help='BigIp configuration file')
    args = parser.parse_args()

    basename = os.path.basename(args.config_file)
    if not basename or 0 == len(basename):
        raise ConfigError('must provide a file path')

    args.config_file = os.path.realpath(args.config_file)

    return args


def _handle_global_config(config):
    level = DEFAULT_LOG_LEVEL
    verify_interval = DEFAULT_VERIFY_INTERVAL

    if config and 'global' in config:
        global_cfg = config['global']

        if 'log-level' in global_cfg:
            log_level = global_cfg['log-level']
            try:
                level = logging.getLevelName(log_level.upper())
            except (AttributeError):
                log.warn('The "global:log-level" field in the configuration '
                         'file should be a string')

        if 'verify-interval' in global_cfg:
            try:
                verify_interval = float(global_cfg['verify-interval'])
                if verify_interval < 0:
                    verify_interval = DEFAULT_VERIFY_INTERVAL
                    log.warn('The "global:verify-interval" field in the '
                             'configuration file should be a non-negative '
                             'number')
            except (ValueError):
                log.warn('The "global:verify-interval" field in the '
                         'configuration file should be a number')

    try:
        root_logger.setLevel(level)
        if level > logging.DEBUG:
            logging.getLogger('requests.packages.urllib3.'
                              'connectionpool').setLevel(logging.WARNING)
    except:
        level = DEFAULT_LOG_LEVEL
        root_logger.setLevel(level)
        if level > logging.DEBUG:
            logging.getLogger('requests.packages.urllib3.'
                              'connectionpool').setLevel(logging.WARNING)
        log.warn('Undefined value specified for the '
                 '"global:log-level" field in the configuration file')

    # level only is needed for unit tests
    return verify_interval, level


def _handle_bigip_config(config):
    if (not config) or ('bigip' not in config):
        raise ConfigError('Configuration file missing "bigip" section')
    bigip = config['bigip']
    if 'username' not in bigip:
        raise ConfigError('Configuration file missing '
                          '"bigip:username" section')
    if 'password' not in bigip:
        raise ConfigError('Configuration file missing '
                          '"bigip:password" section')
    if 'url' not in bigip:
        raise ConfigError('Configuration file missing "bigip:url" section')
    if ('partitions' not in bigip) or (len(bigip['partitions']) == 0):
        raise ConfigError('Configuration file must specify at least one '
                          'partition in the "bigip:partitions" section')

    url = urlparse(bigip['url'])
    host = url.hostname
    port = url.port
    if not port:
        port = 443

    return host, port


def _handle_openshift_sdn_config(config):
    if config and 'openshift-sdn' in config:
        sdn = config['openshift-sdn']
        if 'vxlan-name' not in sdn:
            raise ConfigError('Configuration file missing '
                              '"openshift-sdn:vxlan-name" section')
        if 'vxlan-node-ips' not in sdn:
            raise ConfigError('Configuration file missing '
                              '"openshift-sdn:vxlan-node-ips" section')


def _set_user_agent(bigip):
    try:
        with open('VERSION_BUILD.json', 'r') as version_file:
            data = json.load(version_file)
            bigip.icrs.append_user_agent(
                "k8s-bigip-ctlr-" + data['version'] + '-' + data['build'])
    except Exception as e:
        bigip.icrs.append_user_agent("k8s-bigip-ctlr-VERSION-UNKNOWN")
        log.error("Could not set iControl REST User-Agent: %s", e)


def main():
    try:
        args = _handle_args()

        config = _parse_config(args.config_file)
        verify_interval, _ = _handle_global_config(config)
        host, port = _handle_bigip_config(config)

        # FIXME (kenr): Big-IP settings are currently static (we ignore any
        #               changes to these fields in subsequent updates). We
        #               may want to make the changes dynamic in the future.

        # BIG-IP to manage
        bigip = mgmt_root(
            host,
            config['bigip']['username'],
            config['bigip']['password'],
            port,
            "tmos")

        # Read version and build info, set user-agent for ICR session
        _set_user_agent(bigip)

        managers = []
        for partition in config['bigip']['partitions']:
            # Management for the BIG-IP partitions
            manager = CloudServiceManager(
                bigip,
                partition)
            managers.append(manager)

        handler = ConfigHandler(args.config_file,
                                managers,
                                verify_interval)

        if os.path.exists(args.config_file):
            handler.notify_reset()

        watcher = ConfigWatcher(args.config_file, handler.notify_reset)
        watcher.loop()
        handler.stop()
    except (IOError, ValueError, ConfigError) as e:
        log.error(e)
        sys.exit(1)
    except Exception:
        log.exception('Unexpected error')
        sys.exit(1)

    return 0


if __name__ == "__main__":
    main()

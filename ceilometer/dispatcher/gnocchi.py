#
# Copyright 2014 eNovance
#
# Authors: Julien Danjou <julien@danjou.info>
#          Mehdi Abaakouk <mehdi.abaakouk@enovance.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import fnmatch
import functools
import itertools
import operator
import os
import threading

from jsonpath_rw_ext import parser
from oslo_config import cfg
from oslo_log import log
import six
import yaml

from ceilometer import dispatcher
from ceilometer.dispatcher import gnocchi_client
from ceilometer.i18n import _, _LE
from ceilometer import keystone_client

LOG = log.getLogger(__name__)

dispatcher_opts = [
    cfg.BoolOpt('filter_service_activity',
                default=True,
                help='Filter out samples generated by Gnocchi '
                'service activity'),
    cfg.StrOpt('filter_project',
               default='gnocchi',
               help='Gnocchi project used to filter out samples '
               'generated by Gnocchi service activity'),
    cfg.StrOpt('url',
               default="http://localhost:8041",
               help='URL to Gnocchi.'),
    cfg.StrOpt('archive_policy',
               default="low",
               help='The archive policy to use when the dispatcher '
               'create a new metric.'),
    cfg.StrOpt('archive_policy_file',
               default='gnocchi_archive_policy_map.yaml',
               deprecated_for_removal=True,
               help=_('The Yaml file that defines per metric archive '
                      'policies.')),
    cfg.StrOpt('resources_definition_file',
               default='gnocchi_resources.yaml',
               help=_('The Yaml file that defines mapping between samples '
                      'and gnocchi resources/metrics')),
]

cfg.CONF.register_opts(dispatcher_opts, group="dispatcher_gnocchi")


def log_and_ignore_unexpected_workflow_error(func):
    def log_and_ignore(self, *args, **kwargs):
        try:
            func(self, *args, **kwargs)
        except gnocchi_client.UnexpectedError as e:
            LOG.error(six.text_type(e))
    return log_and_ignore


class LegacyArchivePolicyDefinition(object):
    def __init__(self, definition_cfg):
        self.cfg = definition_cfg
        if self.cfg is None:
            LOG.debug("No archive policy file found! Using default config.")

    def get(self, metric_name):
        if self.cfg is not None:
            for metric, policy in self.cfg.items():
                # Support wild cards such as disk.*
                if fnmatch.fnmatch(metric_name, metric):
                    return policy


class ResourcesDefinitionException(Exception):
    def __init__(self, message, definition_cfg):
        super(ResourcesDefinitionException, self).__init__(message)
        self.definition_cfg = definition_cfg

    def __str__(self):
        return '%s %s: %s' % (self.__class__.__name__,
                              self.definition_cfg, self.message)


class ResourcesDefinition(object):

    MANDATORY_FIELDS = {'resource_type': six.string_types,
                        'metrics': list}

    JSONPATH_RW_PARSER = parser.ExtentedJsonPathParser()

    def __init__(self, definition_cfg, default_archive_policy,
                 legacy_archive_policy_definition):
        self._default_archive_policy = default_archive_policy
        self._legacy_archive_policy_definition =\
            legacy_archive_policy_definition
        self.cfg = definition_cfg

        for field, field_type in self.MANDATORY_FIELDS.items():
            if field not in self.cfg:
                raise ResourcesDefinitionException(
                    _LE("Required field %s not specified") % field, self.cfg)
            if not isinstance(self.cfg[field], field_type):
                raise ResourcesDefinitionException(
                    _LE("Required field %(field)s should be a %(type)s") %
                    {'field': field, 'type': field_type}, self.cfg)

        self._field_getter = {}
        for name, fval in self.cfg.get('attributes', {}).items():
            if isinstance(fval, six.integer_types):
                self._field_getter[name] = fval
            else:
                try:
                    parts = self.JSONPATH_RW_PARSER.parse(fval)
                except Exception as e:
                    raise ResourcesDefinitionException(
                        _LE("Parse error in JSONPath specification "
                            "'%(jsonpath)s': %(err)s")
                        % dict(jsonpath=fval, err=e), self.cfg)
                self._field_getter[name] = functools.partial(
                    self._parse_jsonpath_field, parts)

    @staticmethod
    def _parse_jsonpath_field(parts, sample):
        values = [match.value for match in parts.find(sample)
                  if match.value is not None]
        if values:
            return values[0]

    def match(self, metric_name):
        for t in self.cfg['metrics']:
            if fnmatch.fnmatch(metric_name, t):
                return True
        return False

    def attributes(self, sample):
        attrs = {}
        for attr, getter in self._field_getter.items():
            if callable(getter):
                value = getter(sample)
            else:
                value = getter
            if value is not None:
                attrs[attr] = value
        return attrs

    def metrics(self):
        metrics = {}
        for t in self.cfg['metrics']:
            archive_policy = self.cfg.get(
                'archive_policy',
                self._legacy_archive_policy_definition.get(t))
            metrics[t] = dict(archive_policy_name=archive_policy or
                              self._default_archive_policy)
        return metrics


class GnocchiDispatcher(dispatcher.MeterDispatcherBase):
    """Dispatcher class for recording metering data into database.

    The dispatcher class records each meter into the gnocchi service
    configured in ceilometer configuration file. An example configuration may
    look like the following:

    [dispatcher_gnocchi]
    url = http://localhost:8041
    archive_policy = low

    To enable this dispatcher, the following section needs to be present in
    ceilometer.conf file

    [DEFAULT]
    meter_dispatchers = gnocchi
    """
    def __init__(self, conf):
        super(GnocchiDispatcher, self).__init__(conf)
        self.conf = conf
        self.filter_service_activity = (
            conf.dispatcher_gnocchi.filter_service_activity)
        self._ks_client = keystone_client.get_client()
        self.gnocchi_archive_policy_data = self._load_archive_policy(conf)
        self.resources_definition = self._load_resources_definitions(conf)

        self._gnocchi_project_id = None
        self._gnocchi_project_id_lock = threading.Lock()

        self._gnocchi = gnocchi_client.Client(conf.dispatcher_gnocchi.url)

    # TODO(sileht): Share yaml loading with
    # event converter and declarative notification

    @staticmethod
    def _get_config_file(conf, config_file):
        if not os.path.exists(config_file):
            config_file = cfg.CONF.find_file(config_file)
        return config_file

    @classmethod
    def _load_resources_definitions(cls, conf):
        res_def_file = cls._get_config_file(
            conf, conf.dispatcher_gnocchi.resources_definition_file)
        data = {}
        if res_def_file is not None:
            with open(res_def_file) as data_file:
                try:
                    data = yaml.safe_load(data_file)
                except ValueError:
                    data = {}

        legacy_archive_policies = cls._load_archive_policy(conf)
        return [ResourcesDefinition(r, conf.dispatcher_gnocchi.archive_policy,
                                    legacy_archive_policies)
                for r in data.get('resources', [])]

    @classmethod
    def _load_archive_policy(cls, conf):
        policy_config_file = cls._get_config_file(
            conf, conf.dispatcher_gnocchi.archive_policy_file)
        data = {}
        if policy_config_file is not None:
            with open(policy_config_file) as data_file:
                try:
                    data = yaml.safe_load(data_file)
                except ValueError:
                    data = {}
        return LegacyArchivePolicyDefinition(data)

    @property
    def gnocchi_project_id(self):
        if self._gnocchi_project_id is not None:
            return self._gnocchi_project_id
        with self._gnocchi_project_id_lock:
            if self._gnocchi_project_id is None:
                try:
                    project = self._ks_client.tenants.find(
                        name=self.conf.dispatcher_gnocchi.filter_project)
                except Exception:
                    LOG.exception('fail to retrieve user of Gnocchi service')
                    raise
                self._gnocchi_project_id = project.id
                LOG.debug("gnocchi project found: %s", self.gnocchi_project_id)
            return self._gnocchi_project_id

    def _is_swift_account_sample(self, sample):
        return bool([rd for rd in self.resources_definition
                     if rd.cfg['resource_type'] == 'swift_account'
                     and rd.match(sample['counter_name'])])

    def _is_gnocchi_activity(self, sample):
        return (self.filter_service_activity and (
            # avoid anything from the user used by gnocchi
            sample['project_id'] == self.gnocchi_project_id or
            # avoid anything in the swift account used by gnocchi
            (sample['resource_id'] == self.gnocchi_project_id and
             self._is_swift_account_sample(sample))
        ))

    def _get_resource_definition(self, metric_name):
        for rd in self.resources_definition:
            if rd.match(metric_name):
                return rd

    def record_metering_data(self, data):
        # NOTE(sileht): skip sample generated by gnocchi itself
        data = [s for s in data if not self._is_gnocchi_activity(s)]

        # FIXME(sileht): This method bulk the processing of samples
        # grouped by resource_id and metric_name but this is not
        # efficient yet because the data received here doesn't often
        # contains a lot of different kind of samples
        # So perhaps the next step will be to pool the received data from
        # message bus.
        data.sort(key=lambda s: (s['resource_id'], s['counter_name']))

        resource_grouped_samples = itertools.groupby(
            data, key=operator.itemgetter('resource_id'))

        for resource_id, samples_of_resource in resource_grouped_samples:
            metric_grouped_samples = itertools.groupby(
                list(samples_of_resource),
                key=operator.itemgetter('counter_name'))

            self._process_resource(resource_id, metric_grouped_samples)

    @log_and_ignore_unexpected_workflow_error
    def _process_resource(self, resource_id, metric_grouped_samples):
        resource_extra = {}
        for metric_name, samples in metric_grouped_samples:
            samples = list(samples)
            rd = self._get_resource_definition(metric_name)
            if rd is None:
                LOG.warn("metric %s is not handled by gnocchi" %
                         metric_name)
                continue
            if rd.cfg.get("ignore"):
                continue

            resource_type = rd.cfg['resource_type']
            resource = {
                "id": resource_id,
                "user_id": samples[0]['user_id'],
                "project_id": samples[0]['project_id'],
                "metrics": rd.metrics(),
            }
            measures = []

            for sample in samples:
                resource_extra.update(rd.attributes(sample))
                measures.append({'timestamp': sample['timestamp'],
                                 'value': sample['counter_volume']})

            resource.update(resource_extra)

            try:
                self._gnocchi.post_measure(resource_type, resource_id,
                                           metric_name, measures)
            except gnocchi_client.NoSuchMetric:
                # TODO(sileht): Make gnocchi smarter to be able to detect 404
                # for 'resource doesn't exist' and for 'metric doesn't exist'
                # https://bugs.launchpad.net/gnocchi/+bug/1476186
                self._ensure_resource_and_metric(resource_type, resource,
                                                 metric_name)

                try:
                    self._gnocchi.post_measure(resource_type, resource_id,
                                               metric_name, measures)
                except gnocchi_client.NoSuchMetric:
                    LOG.error(_LE("Fail to post measures for "
                                  "%(resource_id)s/%(metric_name)s") %
                              dict(resource_id=resource_id,
                                   metric_name=metric_name))

        if resource_extra:
            self._gnocchi.update_resource(resource_type, resource_id,
                                          resource_extra)

    def _ensure_resource_and_metric(self, resource_type, resource,
                                    metric_name):
        try:
            self._gnocchi.create_resource(resource_type, resource)
        except gnocchi_client.ResourceAlreadyExists:
            try:
                archive_policy = resource['metrics'][metric_name]
                self._gnocchi.create_metric(resource_type, resource['id'],
                                            metric_name, archive_policy)
            except gnocchi_client.MetricAlreadyExists:
                # NOTE(sileht): Just ignore the metric have been
                # created in the meantime.
                pass

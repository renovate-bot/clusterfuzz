# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""deploy.py handles the deploy command"""

from collections import namedtuple
import contextlib
import datetime
import json
import os
import re
import sys
import tempfile
import time
from typing import Any

import pytz

from local.butler import appengine
from local.butler import common
from local.butler import constants
from local.butler import package
from src.clusterfuzz._internal.base import utils
from src.clusterfuzz._internal.config import local_config
from src.clusterfuzz._internal.metrics import monitoring_metrics
from src.clusterfuzz._internal.system import environment

EXPECTED_BOT_COUNT_PERCENT = 0.8

APPENGINE_FILESIZE_LIMIT = 30 * 1000 * 1000  # ~32 MB
DEPLOY_RETRIES = 3
MATCH_ALL = '*'
RETRY_WAIT_SECONDS = 10

# Give 12 hours for cron jobs to complete before deleting a version.
VERSION_DELETE_WINDOW_MINUTES = 12 * 60

INDEX_YAML_PATH = os.path.join(appengine.SRC_DIR_PY, 'index.yaml')
SERVICE_REGEX = re.compile(r'service\s*:\s*(.*)')

Version = namedtuple('Version', ['id', 'deploy_time', 'traffic_split'])


def now(tz=None):
  """Used for mocks."""
  return datetime.datetime.now(tz)


def _get_services(paths):
  """Get list of services from deployment yamls."""
  services = []
  for path in paths:
    for line in open(path):
      match = SERVICE_REGEX.search(line)
      if match:
        matched_service = match.group(1)
        if matched_service not in services:
          services.append(matched_service)
        break

  return services


def _get_redis_ip(project):
  """Get the redis IP address."""
  region = appengine.region(project)
  return_code, ip = common.execute(
      'gcloud redis instances describe redis-instance '
      '--project={project} --region={region} '
      '--format="value(host)"'.format(project=project, region=region))

  if return_code:
    raise RuntimeError('Failed to get redis IP.')

  return ip.decode('utf-8').strip()


def _additional_app_env_vars(project):
  """Additional environment variables to include for App Engine."""
  return {
      'REDIS_HOST': _get_redis_ip(project),
  }


# TODO: Add structured log
def _deploy_app_prod(project,
                     deployment_bucket,
                     yaml_paths,
                     package_zip_paths,
                     deploy_appengine=True,
                     test_deployment=False,
                     release='prod'):
  """Deploy app in production."""
  if deploy_appengine:
    services = _get_services(yaml_paths)
    rebased_yaml_paths = appengine.copy_yamls_and_preprocess(
        yaml_paths, _additional_app_env_vars(project))

    _deploy_appengine(
        project, [INDEX_YAML_PATH] + rebased_yaml_paths,
        stop_previous_version=False)
    for path in rebased_yaml_paths:
      os.remove(path)

    for service in services:
      _delete_old_versions(project, service, VERSION_DELETE_WINDOW_MINUTES)

  if package_zip_paths:
    for package_zip_path in package_zip_paths:
      _deploy_zip(
          deployment_bucket, package_zip_path, test_deployment=test_deployment)

    releases = [release]
    releases += constants.ADDITIONAL_RELEASES if release == 'prod' else []
    for rel in releases:
      _deploy_manifest(
          deployment_bucket,
          constants.PACKAGE_TARGET_MANIFEST_PATH,
          test_deployment=test_deployment,
          release=rel)


def _deploy_app_staging(project, yaml_paths):
  """Deploy app in staging."""
  services = _get_services(yaml_paths)

  rebased_yaml_paths = appengine.copy_yamls_and_preprocess(
      yaml_paths, _additional_app_env_vars(project))
  _deploy_appengine(project, rebased_yaml_paths, stop_previous_version=True)
  for path in rebased_yaml_paths:
    os.remove(path)

  for service in services:
    _delete_old_versions(project, service, 0)


def _versions_to_delete(versions, window):
  """Return the versions that should be deleted."""
  # gcloud app versions list returns local time.
  cutoff = now() - datetime.timedelta(minutes=window)

  # Don't delete any versions that stopped serving within
  # |window| minutes before now (or the latest one, since that's definitely
  # still serving).
  # This is so that cron jobs have a chance to finish.

  # Find the first version for which the deploy time of the next version is
  # after the cutoff. This is the first version that we do not delete, because
  # it was still serving after the cutoff.
  delete_end = 0
  while (delete_end < len(versions) - 1 and
         versions[delete_end + 1].deploy_time <= cutoff):
    delete_end += 1

  return versions[:delete_end]


def _delete_old_versions(project, service, delete_window):
  """Delete old versions."""

  def _to_datetime(entry):
    """Parse datetime entry."""
    return datetime.datetime(entry['year'], entry['month'], entry['day'],
                             entry['hour'], entry['minute'], entry['second'])

  _, versions = common.execute('gcloud app versions list --format=json '
                               '--project=%s --service=%s' % (project, service))
  versions = [
      Version(version['id'], _to_datetime(version['last_deployed_time']),
              version['traffic_split']) for version in json.loads(versions)
  ]

  versions.sort(key=lambda v: v.deploy_time)
  assert versions[-1].traffic_split == 1.0

  to_delete = _versions_to_delete(versions, delete_window)
  if not to_delete:
    return

  versions = ' '.join(version.id for version in to_delete)
  common.execute('gcloud app versions delete --quiet '
                 '--project=%s --service=%s %s' % (project, service, versions))


def _deploy_appengine(project, yamls, stop_previous_version, version=None):
  """Deploy to appengine using `yamls`."""
  stop_previous_version_arg = ('--stop-previous-version'
                               if stop_previous_version else
                               '--no-stop-previous-version')

  version_arg = '--version=' + version if version else ''

  for retry_num in range(DEPLOY_RETRIES + 1):
    return_code, _ = common.execute(
        'gcloud app deploy %s --quiet '
        '--project=%s %s %s' % (stop_previous_version_arg, project, version_arg,
                                ' '.join(yamls)),
        exit_on_error=False)

    if return_code == 0:
      break

    if retry_num == DEPLOY_RETRIES:
      print('Failed to deploy after %d retries.' % DEPLOY_RETRIES)
      sys.exit(return_code)

    print('gcloud deployment failed, retrying...')
    time.sleep(RETRY_WAIT_SECONDS)


def find_file_exceeding_limit(path, limit):
  """Find one individual file that exceeds limit within path (recursively)."""
  for root, _, filenames in os.walk(path):
    for filename in filenames:
      full_path = os.path.join(root, filename)
      if os.path.getsize(full_path) >= limit:
        return full_path
  return None


def _deploy_zip(bucket_name, zip_path, test_deployment=False):
  """Deploy zip to GCS."""
  if test_deployment:
    common.execute(f'gsutil cp {zip_path} gs://{bucket_name}/test-deployment/'
                   f'{os.path.basename(zip_path)}')
  else:
    common.execute('gsutil cp %s gs://%s/%s' % (zip_path, bucket_name,
                                                os.path.basename(zip_path)))


def _deploy_manifest(bucket_name,
                     manifest_path,
                     test_deployment=False,
                     release='prod'):
  """Deploy source manifest to GCS."""
  remote_manifest_path = utils.get_remote_manifest_filename(release)

  if test_deployment:
    common.execute(f'gsutil cp {manifest_path} '
                   f'gs://{bucket_name}/test-deployment/'
                   f'{remote_manifest_path}')
  else:
    common.execute(f'gsutil cp {manifest_path} '
                   f'gs://{bucket_name}/'
                   f'{remote_manifest_path}')


def _update_deployment_manager(project, name, config_path):
  """Update deployment manager settings."""
  if not os.path.exists(config_path):
    return

  gcloud = common.Gcloud(project)
  operation = 'update'
  try:
    gcloud.run('deployment-manager', 'deployments', 'describe', name)
  except common.GcloudError:
    # Does not exist.
    operation = 'create'

  for _ in range(DEPLOY_RETRIES + 1):
    try:
      gcloud.run('deployment-manager', 'deployments', operation, name,
                 '--config=' + config_path)
      break
    except common.GcloudError:
      time.sleep(RETRY_WAIT_SECONDS)


def _update_pubsub_queues(project):
  """Update pubsub queues."""
  _update_deployment_manager(
      project, 'pubsub',
      os.path.join(environment.get_config_directory(), 'pubsub', 'queues.yaml'))


def _get_region_counts():
  """Get region instance counts."""
  counts = {}
  regions = local_config.MonitoringRegionsConfig()
  clusters = local_config.Config(local_config.GCE_CLUSTERS_PATH).get()

  def get_region(name):
    """Get the region."""
    for pattern in regions.get('patterns'):
      if re.match(pattern['pattern'], name + '-0000'):
        return pattern['name']

    return None

  # Compute expected bot counts per region.
  for config in clusters.values():
    for name, cluster in config['clusters'].items():
      region = get_region(name)
      if not region:
        continue

      counts.setdefault(region, 0)
      counts[region] += cluster['instance_count']

  return counts


@contextlib.contextmanager
def _preprocess_alerts(alerts_path):
  """Preprocess alerts."""
  with open(alerts_path) as f:
    alerts_data = f.read()

  counts = _get_region_counts()
  for region, count in counts.items():
    alerts_data = re.sub('BOT_COUNT:' + region + r'(?=\s|$)',
                         str(int(count * EXPECTED_BOT_COUNT_PERCENT)),
                         alerts_data)

  with tempfile.NamedTemporaryFile(mode='w') as f:
    f.write(alerts_data)
    f.flush()
    yield f.name


def _update_alerts(project):
  """Update pubsub topics."""
  if not local_config.ProjectConfig().get('monitoring.enabled'):
    return

  alerts_path = os.path.join(environment.get_config_directory(), 'monitoring',
                             'alerts.yaml')
  with _preprocess_alerts(alerts_path) as processed_alerts_path:
    _update_deployment_manager(project, 'alerts', processed_alerts_path)


def _update_bigquery(project):
  """Update bigquery datasets and tables."""
  _update_deployment_manager(
      project, 'bigquery',
      os.path.join(environment.get_config_directory(), 'bigquery',
                   'datasets.yaml'))


def get_remote_sha(git_dir: str = '.'):
  """Get remote sha of origin/master."""
  _, remote_sha_line = common.execute(
      f'git -C {git_dir} ls-remote origin refs/heads/master')

  return re.split(br'\s+', remote_sha_line)[0]


def is_diff_origin_master(git_dir: str = '.'):
  """Check if the current state is different from origin/master."""
  common.execute(f'git -C {git_dir} fetch')
  remote_sha = get_remote_sha(git_dir)
  _, local_sha = common.execute(f'git -C {git_dir} rev-parse HEAD')
  _, diff_output = common.execute(f'git -C {git_dir} diff origin/master --stat')

  return diff_output.strip() or remote_sha.strip() != local_sha.strip()


def _staging_deployment_helper():
  """Helper for staging deployment."""
  config = local_config.Config(local_config.GAE_CONFIG_PATH)
  project = config.get('application_id')

  print('Deploying %s to staging.' % project)
  deployment_config = config.sub_config('deployment')

  path = 'staging3'
  yaml_paths = deployment_config.get_absolute_path(path)

  _deploy_app_staging(project, yaml_paths)
  print('Staging deployment finished.')


# We need to import the wrap_with_monitoring through monitoring_metrics
# monitor's import because we need to point to the same module instance
# for assuring the same metric store we increment the metric will have
# the metrics flushed by the monitoring thread.
@monitoring_metrics.monitor.wrap_with_monitoring()
def _prod_deployment_helper(config_dir,
                            package_zip_paths,
                            deploy_appengine=True,
                            deploy_terraform=True,
                            test_deployment=False,
                            release='prod'):
  """Helper for production deployment."""
  config = local_config.Config()
  deployment_bucket = config.get('project.deployment.bucket')

  gae_config = config.sub_config(local_config.GAE_CONFIG_PATH)
  gae_deployment = gae_config.sub_config('deployment')
  project = gae_config.get('application_id')

  print('Deploying %s to prod.' % project)
  path = 'prod3'

  yaml_paths = gae_deployment.get_absolute_path(path, default=[])
  if not yaml_paths:
    deploy_appengine = False

  if deploy_appengine:
    _update_pubsub_queues(project)
    _update_alerts(project)
    _update_bigquery(project)

  labels: dict[str, Any] = {
      'deploy_zip': bool(package_zip_paths),
      'deploy_app_engine': deploy_appengine,
      'deploy_kubernetes': False,
      'deploy_terraform': deploy_terraform,
      'success': True,
      'release': release,
      'clusterfuzz_version': utils.current_source_version()
  }

  try:
    # Appengine depends on Redis, which is managed by Terraform
    # Therefore, we need to deploy Terraform first
    if deploy_terraform:
      _deploy_terraform(config_dir)
    _deploy_app_prod(
        project,
        deployment_bucket,
        yaml_paths,
        package_zip_paths,
        deploy_appengine=deploy_appengine,
        test_deployment=test_deployment,
        release=release)

    if deploy_appengine:
      common.execute(
          f'python butler.py run setup --config-dir {config_dir} --non-dry-run')

    print(f'Production deployment finished. {labels}')
    monitoring_metrics.PRODUCTION_DEPLOYMENT.increment(labels)
  except Exception as ex:
    labels.update({'success': False})
    monitoring_metrics.PRODUCTION_DEPLOYMENT.increment(labels)
    raise ex


def _deploy_terraform(config_dir):
  """Deploys GKE cluster via terraform."""
  terraform_dir = os.path.join(config_dir, 'terraform')
  terraform = f'terraform -chdir={terraform_dir}'
  common.execute(f'{terraform} init')
  common.execute(f'{terraform} apply -target=module.clusterfuzz -auto-approve')
  common.execute(f'rm -rf {terraform_dir}/.terraform*')


def _is_safe_deploy_day():
  time_now_in_ny = now(pytz.timezone('America/New_York'))
  day_now_in_ny = time_now_in_ny.weekday()
  return day_now_in_ny not in {4, 5, 6}  # The days of the week are 0-indexed.


def _enforce_safe_day_to_deploy():
  """Checks that is not an unsafe day (Friday, Saturday, or Sunday) to
  deploy for chrome ClusterFuzz."""

  config = local_config.Config()
  if config.get('weekend_deploy_allowed', True):
    return

  if not _is_safe_deploy_day():
    raise RuntimeError('Cannot deploy Fri-Sun to this CF instance except for '
                       'urgent fixes. See b/384493595. If needed, temporarily '
                       'delete+commit this. You are not too l33t for this '
                       'rule. Do not break it!')


def execute(args):
  """Deploy Clusterfuzz to Appengine."""
  if sys.version_info.major != 3 or sys.version_info.minor != 11:
    print('You can only deploy from Python 3.11. Install Python 3.11 and '
          'run: `PYTHON=python3.11 local/install_deps.bash`')
    sys.exit(1)

  os.environ['ROOT_DIR'] = '.'

  if not os.path.exists(args.config_dir):
    print('Please provide a valid configuration directory.')
    sys.exit(1)

  os.environ['CONFIG_DIR_OVERRIDE'] = args.config_dir

  if not common.has_file_in_path('gcloud'):
    print('Please install gcloud.')
    sys.exit(1)

  is_ci = os.getenv('TEST_BOT_ENVIRONMENT')
  if not is_ci and common.is_git_dirty():
    print('Your branch is dirty. Please fix before deploying.')
    sys.exit(1)

  if not common.has_file_in_path('gsutil'):
    print('gsutil not found in PATH.')
    sys.exit(1)

  _enforce_safe_day_to_deploy()

  # Build templates before deployment.
  appengine.build_templates()

  if not is_ci and not args.staging:
    if is_diff_origin_master() or is_diff_origin_master(
        environment.get_config_directory()):
      if args.force:
        print('You are not on origin/master for clusterfuzz '
              'or clusterfuzz-config. --force is used. Continue.')
        for _ in range(3):
          print('.')
          time.sleep(1)
        print()
      else:
        print('You are not on origin/master for clusterfuzz '
              'or clusterfuzz-config. Please fix or use --force.')
        sys.exit(1)

  if args.staging:
    revision = common.compute_staging_revision()
    platforms = ['linux']  # No other platforms required.
  elif args.prod:
    revision = common.compute_prod_revision()
    platforms = list(constants.PLATFORMS.keys())
  else:
    print('Please specify either --prod or --staging. For production '
          'deployments, you probably want to use deploy.sh from your '
          'configs directory instead.')
    sys.exit(1)

  deploy_zips = 'zips' in args.targets
  deploy_appengine = 'appengine' in args.targets
  deploy_terraform = 'terraform' in args.targets
  test_deployment = 'test_deployment' in args.targets

  if test_deployment:
    deploy_appengine = False
    deploy_terraform = False
    deploy_zips = True

  package_zip_paths = []
  if deploy_zips:
    for platform_name in platforms:
      package_zip_paths += package.package(
          revision, platform_name=platform_name, release=args.release)
  else:
    # package.package calls these, so only set these up if we're not packaging,
    # since they can be fairly slow.
    appengine.symlink_dirs()
    common.install_dependencies('linux')
    with open(constants.PACKAGE_TARGET_MANIFEST_PATH, 'w') as f:
      f.write('%s\n' % revision)

  too_large_file_path = find_file_exceeding_limit('src/appengine',
                                                  APPENGINE_FILESIZE_LIMIT)
  if too_large_file_path:
    print(("%s is larger than %d bytes. It wouldn't be deployed to appengine."
           ' Please fix.') % (too_large_file_path, APPENGINE_FILESIZE_LIMIT))
    sys.exit(1)

  if args.staging:
    _staging_deployment_helper()
  else:
    # This workaround is needed to set the env vars APPLICATION_ID and BOT_NAME
    # for local environment, and it's needed for loading the monitoring module
    config = local_config.ProjectConfig().get('env')
    environment.set_value("APPLICATION_ID", config["APPLICATION_ID"])
    environment.set_value("BOT_NAME", os.uname().nodename)
    _prod_deployment_helper(
        args.config_dir,
        package_zip_paths,
        deploy_appengine,
        deploy_terraform,
        test_deployment=test_deployment,
        release=args.release)

  with open(constants.PACKAGE_TARGET_MANIFEST_PATH) as f:
    print('Source updated to %s' % f.read())

  if platforms[-1] != common.get_platform():
    # Make sure the installed dependencies are for the current platform.
    common.install_dependencies()

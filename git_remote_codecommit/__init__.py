# Copyright 2018 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

import collections
import datetime
import os
import subprocess
import sys
import re

import botocore.auth
import botocore.awsrequest
import botocore.compat
import botocore.hooks
import botocore.session

try:
  from urlparse import urlparse  # python 2.x
except ImportError:
  from urllib.parse import urlparse  # python 3.x


class FormatError(Exception):
  pass


class ProfileNotFound(Exception):
  pass


class RegionNotFound(Exception):
  pass


class RegionNotAvailable(Exception):
  pass


class CredentialsNotFound(Exception):
  pass


class Context(collections.namedtuple('Context', ['session', 'repository', 'version', 'region', 'credentials'])):
  """
  Repository information the hook concerns, derived from git's remote url and
  the user's AWS profile.

  :var botocore.session.Session session: aws session context
  :var str repository: repository name
  :var str version: protocol version for this hook
  :var str region: region the repository resides within
  :var botocore.credentials credentials: session credentials
  """

  @staticmethod
  def from_url(remote_url):
    """
    Parses repository information from a git url, filling in additional
    attributes we need from our AWS profile.

    Our remote helper accepts two distinct types of urls...

    * codecommit://<profile>@<repository>
    * codecommit::<region>://<profile>@<repository>

    If provided the former we get the whole url, but if the later git will
    truncate the proceeding 'codecommit::' prefix for us.

    The '<profile>@' url is optional, using the aws sessions present profile
    if not provided.

    :param str remote_url: git remote url to parse

    :returns: **Context** with our CodeCommit repository information

    :raises:
      * **FormatError** if the url is malformed
      * **ProfileNotFound** if the url references a profile that doesn't exist
      * **RegionNotFound** if the url references a region that doesn't exist
      * **RegionNotAvailable** if the url references a region that is not available
    """
    if remote_url is None:
      raise FormatError('url required')

    url = urlparse(remote_url)
    event_handler = botocore.hooks.HierarchicalEmitter()
    profile = 'default'
    repository = url.netloc
    if not url.scheme or not url.netloc:
      raise FormatError(f'{remote_url} is a malformed url')

    if '@' in url.netloc:
      profile, repository = url.netloc.split('@', 1)
      session = botocore.session.Session(profile = profile, event_hooks = event_handler)

      if profile not in session.available_profiles:
        raise ProfileNotFound(f"Profile {profile} not found, available profiles are: {', '.join(session.available_profiles)}")

    else:
      session = botocore.session.Session(event_hooks = event_handler)

    try:
      # when the aws cli is available support plugin authentication

      import awscli.plugin

      awscli.plugin.load_plugins(
          session.full_config.get('plugins', {}),
          event_hooks = event_handler,
          include_builtins = False,
      )

      session.emit_first_non_none_response('session-initialized', session = session)
    except ImportError:
      pass

    available_regions = [region for partition in session.get_available_partitions() for region in session.get_available_regions('codecommit', partition)]

    if url.scheme == 'codecommit':
      region = session.get_config_variable('region')

      if not region:
        raise RegionNotFound(f"Profile {profile} doesn't have a region available. Please set it.")

      if region not in available_regions:
        raise RegionNotAvailable(f'Region {region} is currently not available for use with AWS CodeCommit. Please try again with a valid region. If you believe this is an error then please update your version of botocore.')

    elif re.match(r"^[a-z]{2}-\w*.*-\d{1}", url.scheme):
      if url.scheme in available_regions:
        region = url.scheme

      else:
        raise RegionNotAvailable(f'Region {url.scheme} is currently not available for use with AWS CodeCommit. Please try again with a valid region. If you believe this is an error then please update your version of botocore.')

    else:
      raise FormatError(f'{remote_url} is a malformed url')
    credentials = session.get_credentials()

    if not credentials:
      raise CredentialsNotFound(f"Profile {profile} doesn't have credentials available.")

    return Context(session, repository, 'v1', region, credentials)


def error(msg):
  sys.stderr.write('%s\n' % msg)
  sys.exit(1)


def main():
  """
  Hook that can be invoked by git, providing simplified push/pull access for a
  CodeCommit repository.
  """

  if len(sys.argv) < 3:
    error('Too few arguments. This hook requires the git command and remote.')

  elif len(sys.argv) > 3:
    error("Too many arguments. Hook only accepts the git command and remote, but argv was: '%s'" % "', '".join(sys.argv))

  git_cmd, remote_url = sys.argv[1:3]

  try:
    context = Context.from_url(remote_url)
    authenticated_url = git_url(context.repository, context.version, context.region, context.credentials)
    sys.exit(subprocess.call(['git', 'remote-http', git_cmd, authenticated_url]))

  except (FormatError, ProfileNotFound, RegionNotFound, CredentialsNotFound) as exc:
    error(str(exc))


def git_url(repository, version, region, credentials):
  """
  Provides the signed url we can use for pushing and pulling from CodeCommit...

  ::

    https://(username):(password)@git-codecommit.(region).amazonaws.com/v1/repos/(repository)

  :param str repository: repository name
  :param str version: protocol version for this hook
  :param str region: region the repository resides within
  :param botocore.credentials credentials: session credentials

  :return: url we can push/pull from
  """

  hostname = os.environ.get('CODE_COMMIT_ENDPOINT', f'git-codecommit.{region}.amazonaws.com')
  path = f'/{version}/repos/{repository}'

  token = '%' + credentials.token if credentials.token else ''
  username = botocore.compat.quote(credentials.access_key + token, safe='')
  signature = sign(hostname, path, region, credentials)

  return f'https://{username}:{signature}@{hostname}{path}'


def sign(hostname, path, region, credentials):
  """
  Provides a SigV4 signature for a CodeCommit url.

  :param str hostname: aws hostname request is for
  :param str path: resource the request is for
  :param str region: region the repository resides within
  :param botocore.credentials credentials: session credentials

  :return: signature for the url
  """

  request = botocore.awsrequest.AWSRequest(method = 'GIT', url = f'https://{hostname}{path}')
  request.context['timestamp'] = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')

  signer = botocore.auth.SigV4Auth(credentials, 'codecommit', region)
  canonical_request = f'GIT\n{path}\n\nhost:{hostname}\n\nhost\n'
  string_to_sign = signer.string_to_sign(request, canonical_request)
  signature = signer.signature(string_to_sign, request)
  return f"{request.context['timestamp']}Z{signature}"

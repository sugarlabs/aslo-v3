import os
import shutil
import configparser
import requests
import datetime
import zipfile
import uuid
import re
import subprocess as sp

from flask import current_app as app
from aslo.service import activity as activity_service
from aslo.celery_app import logger
from .exceptions import ReleaseError, BuildProcessError, ScreenshotDoesNotExist
from . import gh
from . import i18n
from . import img


def get_bundle_path(bundle_name):
    return os.path.join(app.config['BUILD_BUNDLE_DIR'], bundle_name)


def xo_file_exists(assets):
    for asset in assets:
        if '.xo' in asset['name']:
            logger.info('Attached xo file has been found.')
            return asset

    logger.info('No attached xo file has been found.')
    return None


def download_attached_xo(xo):
    # save on blocks of 1024
    logger.info("Downloading {} file...".format(xo['name']))
    response = requests.get(xo['browser_download_url'], stream=True)
    tmp_bundle_path = os.path.join(app.config['TEMP_BUNDLE_DIR'], xo['name'])
    with open(tmp_bundle_path, "wb") as fh:
        for block in response.iter_content(chunk_size=1024):
            fh.write(block)

    return tmp_bundle_path


def verify_and_extract_xo(tmp_bundle_path):
    def verify_xo(xo_archive):
        logger.info('Searching for activity.info inside xo file.')
        valid = any(
            'activity.info' in filename for filename in xo_archive.namelist()
        )
        if not valid:
            raise ReleaseError('activity.info not found in xo file.')
        else:
            logger.info('activity.info file has been found in xo file.')

        # TODO: are we going to store this locally and/or in remote server?
        bundle_name = os.path.basename(tmp_bundle_path)
        bundle_path = get_bundle_path(bundle_name)
        if os.path.exists(bundle_path) and os.path.isfile(bundle_path):
            raise ReleaseError(
                'Bundle {} already exist.'.format(bundle_name)
            )

    def extract_xo(xo_archive):
        random_uuid = uuid.uuid4().hex
        extract_dir = os.path.join(app.config['TEMP_BUNDLE_DIR'], random_uuid)
        try:
            os.mkdir(extract_dir)
        except (IOError, FileExistsError) as e:
            raise ReleaseError(
                'Failed to created {} directory. Error: {}'
                .format(extract_dir, e)
            )

        # Find root_prefix for the xo archive. Usually it's Name.Activity
        archive_root_prefix = os.path.commonpath(xo_archive.namelist())
        try:
            xo_archive.extractall(path=extract_dir)
            extraction_path = os.path.join(extract_dir, archive_root_prefix)
        except Exception as e:
            logger.exception(e)

        return extraction_path

    xo_archive = zipfile.ZipFile(tmp_bundle_path)
    verify_xo(xo_archive)
    extraction_path = extract_xo(xo_archive)

    return extraction_path


def clone_repo(url, tag, repo_path):
    target_dir = app.config['BUILD_CLONE_REPO']
    if not os.path.isdir(target_dir):
        raise BuildProcessError('Directory %s does not exist' % target_dir)

    if os.path.isdir(repo_path):
        logger.info('Removing existing cloned repo %s', repo_path)
        try:
            shutil.rmtree(repo_path)
        except IOError as e:
            raise BuildProcessError(
                'Can\'t remove existing repo {}. Exception: {}'
                .format(repo_path, e)
            )

    cmd = ['git', '-c', 'advice.detachedHead=false', '-C', target_dir,
           'clone', '-b', tag, '--depth', '1', url]

    logger.info('Cloning repo %s', url)
    if sp.call(cmd) != 0:
        raise BuildProcessError('[%s] command has failed' % ' '.join(cmd))


def get_activity_metadata(repo_path):
    def metadata_file_exists():
        activity_file = os.path.join(repo_path, 'activity/activity.info')
        if not os.path.isfile(activity_file):
            raise ReleaseError(
                'Activity file %s does not exist' % activity_file
            )

        return activity_file

    def parse_metadata_file(activity_file):
        parser = configparser.ConfigParser()
        if len(parser.read(activity_file)) == 0:
            raise ReleaseError('Error parsing metadata file')

        try:
            attributes = dict(parser.items('Activity'))
        except configparser.NoSectionError as e:
            raise ReleaseError(
                'Error parsing metadata file. Exception message: %s' % e
            )

        return attributes

    def validate_mandatory_attributes(attributes):
        MANDATORY_ATTRIBUTES = ['name', 'bundle_id', 'license',
                                'icon', 'activity_version']
        for attr in MANDATORY_ATTRIBUTES:
            if attr not in attributes:
                raise ReleaseError(
                    '%s field missing in activity metadata' % attr
                )

        return True

    logger.info('Getting activity metadata from activity.info file.')
    activity_file = metadata_file_exists()
    attributes = parse_metadata_file(activity_file)
    validate_mandatory_attributes(attributes)

    return attributes


def invoke_bundle_build(repo_path):
    def check_bundle():
        dist_dir = os.path.join(repo_path, 'dist')
        if os.path.isdir(dist_dir) and len(os.listdir(dist_dir)) == 1:
            logger.info('Bundle has been built successfully')
            return os.path.join(dist_dir, os.listdir(dist_dir)[0])
        else:
            raise BuildProcessError('Bundle file was not generated correctly')

    logger.info('Building bundle.')
    volume = repo_path + ':/activity'
    docker_image = app.config['BUILD_DOCKER_IMAGE']
    docker_cmd = ['docker', 'run', '--rm', '-v', volume, docker_image]
    logger.info('Running docker command: "%s"', ' '.join(docker_cmd))
    if sp.call(docker_cmd) != 0:
        raise BuildProcessError('Docker building process has failed')

    bundle_path = check_bundle()
    return bundle_path


def compare_version_in_bundlename_and_metadata(tmp_bundle_path, metadata):
    bundle_name = os.path.basename(tmp_bundle_path)
    match = re.search(r'^\w+-(\d+.?\d*).xo$', bundle_name)
    bundle_version = match.group(1) if match else None
    if metadata['activity_version'] != bundle_version:
        raise ReleaseError(
            'Bundle filename version and activity metadata version '
            'does not match.'
        )


def get_sugar_details(activity, repo_path):
    logger.info('Applying heuristic to determine min sugar supported version.')

    def is_gtk3():
        GTK3_IMPORT_TYPES = {'sugar3': 3, 'from gi.repository import Gtk': 3,
                             'sugar.': 2, 'import pygtk': 2,
                             'pygtk.require': 2}

        setup_py_path = os.path.join(repo_path, 'setup.py')
        all_files = os.listdir(repo_path)
        try_paths = [setup_py_path] + all_files

        for path in try_paths:
            if os.path.isfile(path):
                with open(path) as f:
                    text = f.read()
                    for sign in GTK3_IMPORT_TYPES:
                        if sign in text:
                            version = GTK3_IMPORT_TYPES[sign]
                            return version == 3

        # Fallback to assuming GTK3
        return True

    def is_web():
        if 'exec' in activity:
            return activity['exec'] == 'sugar-activity-web'
        return False  # Fallback

    def has_old_toolbars():
        OLD_TOOLBAR_SIGNS = ['activity.ActivityToolbox', 'gtk.Toolbar']
        for path in os.listdir(repo_path):
            if os.path.isfile(path):
                with open(path) as f:
                    text = f.read()
                for sign in OLD_TOOLBAR_SIGNS:
                    if sign in text:
                        return True
        return False

    def determine_min_sugar_version(is_gtk3, is_web, has_old_toolbars):
        min_sugar_version = '0.100' if is_web else (
            '0.96' if is_gtk3 else (
                '0.86' if not has_old_toolbars else '0.82'
            ))
        return min_sugar_version

    sugar = {}
    sugar['is_gtk3'] = is_gtk3()
    sugar['is_web'] = is_web()
    sugar['has_old_toolbars'] = has_old_toolbars()
    sugar['min_sugar_version'] = determine_min_sugar_version(
        sugar['is_gtk3'], sugar['is_web'], sugar['has_old_toolbars']
    )

    return sugar


def store_bundle(tmp_bundle_path, bundle_id):
    # Create bundle_id folder if it doesn't exist
    bundle_id_path = os.path.join(app.config['BUILD_BUNDLE_DIR'], bundle_id)
    if not os.path.exists(bundle_id_path):
        os.makedirs(bundle_id_path, exist_ok=True)
    try:
        shutil.copy2(tmp_bundle_path, bundle_id_path)
        stored_bundle = os.path.join(
            bundle_id_path,
            os.path.basename(tmp_bundle_path)
        )
        os.chmod(stored_bundle, 0o644)
    except IOError as e:
        raise ReleaseError(
            'Bundle copying has failed: %s', e
        )

    logger.info('Bundle succesfully stored at %s', stored_bundle)


def clean_up(tmp_bundle_path, repo_path):
    try:
        os.remove(tmp_bundle_path)
        shutil.rmtree(repo_path)
    except IOError as e:
        raise ReleaseError('Error removing file: %s' % e)


def handle_release(gh_json):
    repo_url = gh_json['repository']['clone_url']
    repo_name = gh_json['repository']['name']
    release = gh_json['release']
    tag = release['tag_name']
    tag_commit = gh.find_tag_commit(gh_json['repository']['full_name'], tag)
    xo_asset = None
    bundle_name = None
    # TODO: Extract message to constants file
    gh.comment_on_commit(
        tag_commit, "Build has started :hourglass_flowing_sand:"
    )

    if 'assets' in release and len(release['assets']) != 0:
        xo_asset = xo_file_exists(release['assets'])

    if xo_asset:
        logger.info('[bundle-release] No bundle building process needed.')
        tmp_bundle_path = download_attached_xo(xo_asset)
        repo_path = verify_and_extract_xo(tmp_bundle_path)
        bundle_name = xo_asset['name']
    else:
        logger.info('[sourcecode-release] Building bundle from source code.')
        repo_path = os.path.join(app.config['BUILD_CLONE_REPO'], repo_name)
        clone_repo(repo_url, tag, repo_path)
        tmp_bundle_path = invoke_bundle_build(repo_path)
        bundle_name = os.path.basename(tmp_bundle_path)

    metadata = get_activity_metadata(repo_path)
    compare_version_in_bundlename_and_metadata(tmp_bundle_path, metadata)

    translations = i18n.get_translations(repo_path)

    if translations:
        metadata['i18n_name'] = i18n.translate_field(
            metadata['name'], translations
        )
        metadata['i18n_summary'] = i18n.translate_field(
            metadata.get('summary', ''), translations
        )

        # name and summary fields might have empty values or missing transl.
        if not metadata['i18n_name']:
            metadata['i18n_name'] = {'en': metadata['name']}
        if not metadata['i18n_summary']:
            metadata['i18n_summary'] = {'en': metadata.get('summary', '')}
    else:
        metadata['i18n_name'] = {'en': metadata['name']}
        metadata['i18n_summary'] = {'en': metadata.get('summary', '')}

    metadata['repository'] = repo_url
    metadata['developers'] = gh.get_developers(
        gh_json['repository']['full_name']
    )
    metadata['icon_bin'] = img.get_icon(repo_path, metadata['icon'])

    try:
        screenshots = img.get_screenshots(repo_path, metadata['bundle_id'])
    except ScreenshotDoesNotExist as e:
        screenshots = {}
        logger.info(e)
    finally:
        metadata['screenshots'] = screenshots

    metadata['sugar'] = get_sugar_details(metadata, repo_path)
    metadata['bundle_name'] = bundle_name
    metadata['release'] = {}
    metadata['release']['notes'] = gh.render_markdown(
        gh_json['release']['body']
    )
    metadata['release']['time'] = datetime.datetime.strptime(
        gh_json['release']['published_at'], '%Y-%m-%dT%H:%M:%SZ'
    )

    logger.info('Inserting activity into db.')
    activity_service.insert_activity(metadata)
    logger.info('Saving bundle.')
    store_bundle(tmp_bundle_path, metadata['bundle_id'])
    logger.info('Cleaning up.')
    clean_up(tmp_bundle_path, repo_path)

import mongoengine as me
import hashlib

from aslo.api.exceptions import ReleaseError
from aslo.models.activity import ActivityModel, DeveloperModel
from aslo.models.release import ReleaseModel
from aslo.persistence.release import Release
from aslo.persistence.activity import Activity


def add_release(activity, release):
    # first release
    if not activity.latest_release and len(activity.previous_releases) == 0:
        activity.latest_release = release
        return

    if activity.latest_release.activity_version >= release.activity_version:
        Release.delete(release)
        raise me.ValidationError(
            'New activity release version {} is less or equal than the '
            'current version {}'.format(
                release.activity_version,
                activity.latest_release.activity_version
            )
        )

    activity.previous_releases.append(activity.latest_release)
    activity.latest_release = release


def set_developers(activity, developers):
    devs = []
    for developer in developers:
        dev = DeveloperModel()
        dev.name = developer['name']
        dev.email = developer['email']
        dev.page = developer['page']
        dev.avatar = developer['avatar']
        devs.append(dev)
    activity.developers = devs


def get_all_screenshots(bundle_id):
    screenshots = {}
    activity = Activity.get_by_bundle_id(bundle_id)
    if activity:
        release = activity.latest_release
        screenshots = release.screenshots

    return screenshots


def insert_activity(data):
    activity = Activity.get_by_bundle_id(data['bundle_id'])
    if activity is None:
        activity = ActivityModel()
        activity.bundle_id = data['bundle_id']

    activity.license = data['license']
    activity.repository = data['repository']
    if 'categories' in data:
        activity.categories = data['categories'].split()
    activity.name = data['i18n_name']
    activity.summary = data['i18n_summary']
    set_developers(activity, data['developers'])
    icon_hash = hashlib.sha1(data['icon_bin']).hexdigest()
    if icon_hash != activity.icon_hash:
        activity.icon = data['icon_bin']
        activity.icon_hash = icon_hash
        activity.icon_type = data.get('icon_type', 'image/svg+xml')

    release = ReleaseModel()
    release.activity_version = float(data['activity_version'])
    release.min_sugar_version = float(data['sugar']['min_sugar_version'])
    release.is_web = data['sugar']['is_web']
    release.has_old_toolbars = data['sugar']['has_old_toolbars']
    release.bundle_name = data['bundle_name']
    release.release_notes = data['release']['notes']
    release.timestamp = data['release']['time']
    release.screenshots = data['screenshots']

    try:
        Release.add_or_update(release)
        add_release(activity, release)
    except me.ValidationError as e:
        raise ReleaseError('Failed saving release into db: %s' % e)
    else:
        try:
            Activity.add_or_update(activity)
        except me.ValidationError as e:
            Release.delete(release)
            raise ReleaseError('Failed saving activity into db: %s' % e)


def find_release(activity, activity_version):
    if activity.latest_release.activity_version == activity_version:
        return activity.latest_release

    if len(activity.previous_releases) != 0:
        for release in activity.previous_releases:
            if release.activity_version == activity_version:
                return release

    return None


def get_all(page=None, pagesize=None):
    if page:
        return Activity.paginate(page=page, pagesize=pagesize)
    else:
        return Activity.get_all()


def filter_by_lang_code(lang_code, page=None, pagesize=None):
    if lang_code in ['en_US', 'en']:
        Qcomb = (me.Q(**{'name__en__exists': True}) |
                 me.Q(**{'name__en_US__exists': True}))
    else:
        Qcomb = me.Q(**{'name__' + lang_code + '__exists': True})

    if page:
        return Activity.paginate(page=page, pagesize=pagesize, Qcomb=Qcomb)
    else:
        return Activity.query(Qcombination=Qcomb)


def search_by_activity_name(activity_name, lang_code, page=None,
                            pagesize=None):
    if lang_code in ['en', 'en_US']:
        Qcomb = (me.Q(**{'name__en__icontains': activity_name}) |
                 me.Q(**{'name__en_US__icontains': activity_name}))
    else:
        Qcomb = me.Q(**{'name__' + lang_code + '__icontains': activity_name})

    if page:
        return Activity.paginate(page=page, pagesize=pagesize, Qcomb=Qcomb)
    else:
        return Activity.query(Qcombination=Qcomb)


def search_by_category(category_name, page=None, pagesize=None):
    Qcomb = me.Q(**{'categories__iexact': category_name})
    if page:
        return Activity.paginate(page=page, pagesize=pagesize, Qcomb=Qcomb)
    else:
        return Activity.query(Qcombination=Qcomb)

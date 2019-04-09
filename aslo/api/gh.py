import hmac
import hashlib
from flask import current_app as app
from github import Github


def verify_signature(gh_signature, body, secret):
    sha1 = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest('sha1=' + sha1, gh_signature)


def auth():
    g = Github(app.config['GITHUB_OAUTH_TOKEN'])
    return g


def get_developers(repo_url):
    g = auth()
    repository = g.get_repo(repo_url)
    contributors = repository.get_contributors()
    developers = []
    for c in contributors:
        dev = {'email': c.email, 'page': c.html_url, 'avatar': c.avatar_url}
        dev['name'] = c.name if c.name else c.login
        developers.append(dev)

    return developers


def find_tag_commit(repo_name, tag_name):
    g = auth()
    tags = g.get_repo(repo_name).get_tags()
    for tag in tags:
        if tag.name == tag_name:
            return tag.commit

    return None


def comment_on_commit(commit, message):
    commit.create_comment(message)


def render_markdown(message):
    # https://developer.github.com/v3/markdown/
    g = auth()
    return g.render_markdown(message)

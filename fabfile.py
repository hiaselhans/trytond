# -*- coding: utf-8 -*-
"""
    fabfile

    A script which updates the latest modules looking at the tryton repo

    :copyright: © 2013 by Openlabs Technologies & Consulting (P) Limited
    :license: BSD, see LICENSE for more details.
"""
import requests
from fabric.api import local, lcd


def git_clone(repository, branch):
    """
    Git clone a repository
    """
    local('git clone %s -b %s' % (repository, branch))


def setup(branch='develop'):
    """
    Setup a new environment completely with all submodules
    """
    git_clone('git@github.com:tryton/trytond.git', branch)
    all_repos = requests.get(
        'https://api.github.com/orgs/tryton/repos?per_page=1000'
    ).json()
    for repo in all_repos:
        if repo['name'] in ['proteus', 'tryton', 'sao']:
            continue
        with lcd('trytond/trytond/modules'):
            git_clone(repo['git_url'], branch)

    with lcd('trytond'):
        local('python setup.py install')


def runtests():
    """
    Run the tests finally
    """
    with lcd('trytond'):
        local('python setup.py test')

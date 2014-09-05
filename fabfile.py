# -*- coding: utf-8 -*-
"""
    fabfile

    A script which updates the latest modules looking at the tryton repo

    :copyright: © 2013 by Openlabs Technologies & Consulting (P) Limited
    :license: BSD, see LICENSE for more details.
"""
import requests
import re

from fabric.api import local, lcd, settings


def git_clone(repository, branch):
    """
    Git clone a repository
    """
    local('git clone %s -b %s' % (repository, branch))

def install(develop=True):
    local('python2 setup.py {}'.format('develop' if develop else ''))


def setup(branch='develop'):
    """
    Setup a new environment completely with all submodules
    """
    #git_clone('git@github.com:tryton/trytond.git', branch)
    all_repos = requests.get(
        'https://api.github.com/orgs/tryton/repos?per_page=1000'
    ).json()
    for repo in all_repos:
        if repo['name'] == 'proteus':
            git_clone(repo['git_url'], branch)
            with lcd('proteus'):
                install()

        elif repo['name'] not in ['tryton', 'sao', 'trytond',
                                'google_translate', 'ci', 'neso']:
            with lcd('trytond/modules'), settings(warn_only=True):
                git_clone(repo['git_url'], branch)

    install()



def runtests():
    """
    Run the tests finally
    """
    #local('python setup.py test')
    #local('ls trytond/modules')
    local('python trytond/tests/run-tests.py -v -v -m')

#This file is part of Tryton.  The COPYRIGHT file at the top level of
#this repository contains the full copyright notices and license terms.
import sys
import os
import subprocess
import logging
from threading import Lock
from trytond.modules import Index

_LOCK = Lock()
_TIMES = {}
_MODULES = None

logger = logging.getLogger('monitor')

def _modified(path):
    _LOCK.acquire()
    try:
        try:
            if not os.path.isfile(path):
                # Deletion
                return path in _TIMES
            mtime = os.stat(path).st_mtime
            if path not in _TIMES:
                _TIMES[path] = mtime

            if mtime != _TIMES[path]:
                _TIMES[path] = mtime
                return True
        except Exception:
            return True
    finally:
        _LOCK.release()
    return False


def _importable(package):
    """
    check if a package can be imported or not
    :param package:
    :return: True on success
    """
    #exit_code = subprocess.call((sys.executable, '-c', 'import %s' %
    #                             package.name),
    #                             cwd=os.path.dirname(package.path))
    #
    #return exit_code == 0

    try:
        package.import_module()
    except Exception, e:
        logger.info(str(e))
        raise
        #return False
    return True


def monitor(files):
    '''
    Monitor module files for change

    :return: True if at least one file has changed
    '''
    modified = False
    last_keys = set(Index().keys())
    try:
        Index.create_index()
    except Exception:
        return False

    for fle in files:
        if _modified(fle):
            modified = True

    # check view xml
    for package in Index().itervalues():
        view_dir = os.path.join(package.path, 'view')
        if os.path.isdir(view_dir):
            for filename in os.listdir(view_dir):
                if _modified(os.path.join(view_dir, filename)):
                    modified = True
        for root, dirs, files in os.walk(package.path):
            for fle in files:
                if fle.endswith(".py"):
                    path = os.path.join(root, fle)
                    if _modified(path):
                        modified = True

    if last_keys and last_keys.symmetric_difference(Index().keys()):
        modified = True

    # Do not restart on module-errors
    if modified:
        for package in Index().itervalues():
            if package._imported and not _importable(package):
                logger.info('Module import failed on %s. not reloading' %
                            package.name)
                return False

    return modified
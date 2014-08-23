# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import os
import sys
import logging
import imp
import ConfigParser
from glob import iglob

from sql import Table
from sql.functions import Now

import trytond.tools as tools  # fix because of circular import
from trytond.config import CONFIG
from trytond.transaction import Transaction
from trytond.cache import Cache
import trytond.convert as convert

ir_module = Table('ir_module_module')
ir_model_data = Table('ir_model_data')

MODULES_PATH = [os.path.abspath(os.path.dirname(__file__))]
TRYTON_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))


class Module(object):
    """
    A class to represent a tryton module, defined by name and module_path
    """

    def __init__(self, name, path):
        self.name = name
        self.path = path
        self._module = None
        # parse config file
        config = ConfigParser.ConfigParser()
        try:
            with open(os.path.join(self.path, 'tryton.cfg'), 'r') as fp:
                config.readfp(fp)
            self.info = dict(config.items('tryton'))
            self.info['directory'] = self.path
            for key in ('depends', 'extras_depend', 'xml'):
                if key in self.info:
                    self.info[key] = self.info[key].strip().splitlines()
                else:
                    self.info[key] = []
        except IOError:
            if not name == 'all':
                raise Exception('Module %s not found' % self.name)

    def import_module(self):
        if self._module is None:
            search_path = [os.path.dirname(self.path)]
            mod_file, pathname, description = imp.find_module(self.name,
                                                              search_path)
            self._module = imp.load_module('trytond.modules.' + self.name,
                                     mod_file, pathname, description)
            if mod_file is not None:
                mod_file.close()

        return self._module

    def import_tests(self):
        self.import_module()
        test_module = 'trytond.modules.%s.tests' % self.name
        try:
            return __import__(test_module, fromlist=[''])
        except ImportError:
            return None

    def is_to_install(self):
        for kind in ('init', 'update'):
            if 'all' in CONFIG[kind] and self.name != 'tests':
                return True
            elif self.name in CONFIG[kind]:
                return True
        return False

    def __str__(self):
        return "Trytond-module: %s" % self.name

    def __repr__(self):
        return self.__str__()

    depends = property(lambda self: self.info['depends'])
    extras_depend = property(lambda self: self.info['extras_depend'])


class Index(dict):
    """
    Singleton Index instance to know of all available modules and return a
    sorted list on request
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Index, cls).__new__(
                cls, *args, **kwargs)
        return cls._instance

    def __getitem__(self, item):
        try:
            return super(Index, self).__getitem__(item)
        except KeyError:
            raise Exception('Module %s not found!' % item)

    def add_module(self, name, path):
        if name not in self:
            self[name] = Module(name, path)
        return self[name]

    def create_index(self):
        """
        Check all module sources and fill Index-dict {module_name: module}
        """

        # INSERT MODULES FROM Tryton-path
        for modules_path in MODULES_PATH:
            for folder in os.listdir(modules_path):
                if (os.path.isdir(os.path.join(modules_path, folder))
                        and not folder.startswith('.')):
                    self.add_module(folder, os.path.join(modules_path, folder))

        # INSERT EGGS
        try:
            import pkg_resources

            for egg in pkg_resources.iter_entry_points('trytond.modules'):
                mod_name = egg.module_name.split('.')[-1]
                mod_path = os.path.join(egg.dist.location,
                                        *egg.module_name.split('.'))
                if not os.path.isdir(mod_path):
                    for path in sys.path:
                        mod_path = os.path.join(path,
                                                *egg.module_name.split('.'))
                        if os.path.isdir(mod_path):
                            break
                if os.path.isdir(mod_path):
                    self.add_module(mod_name, mod_path)
                else:
                    self.add_module(mod_name, egg.dist.location)
        except ImportError:
            pass

        # INSERT BUILTIN
        for name in ('ir', 'res', 'webdav', 'tests'):
            self.add_module(name, os.path.join(TRYTON_ROOT, name))

    def create_graph(self, module_list=None):
        """
        Create a sorted list (=graph) with lowest dependencies first for
        'module_list' or for all modules in Index

        :param module_list: list of module names
        :return: sorted list of module-nodes
        """
        if module_list is None:
            module_list = self.keys()
        sorted_list = []

        def _add_deps(module_name):
            """
            internal function to first add dependencies recursively, as soon as
            all dependencies are fulfilled, add the module itself
            """
            module_node = self[module_name]

            if module_node in sorted_list:
                return
            for dep in module_node.depends:
                _add_deps(dep)
            for xdep in module_node.extras_depend:
                if xdep in module_list:
                    _add_deps(xdep)
            sorted_list.append(module_node)

        for module in module_list:
            try:
                _add_deps(module)
            except RuntimeError:  # endless recursion on circular depends
                raise ImportError("Circular dependencies for Module %s"
                                  % module)

        return sorted_list

    def get_parents(self, module_name):
        """
        Get all modules which depend on 'module_name'
        """
        return filter(lambda module: module_name in self.recursive_deps(module),
                      self.keys())

    def recursive_deps(self, module_name):
        """
        get recursive depends for 'module_name'
        """
        depends = set(self[module_name].depends)
        for dep in depends:
            depends.union(self.recursive_deps(dep))
        return depends

    def recursive_all_deps(self, module_name):
        """
        get recursive depends for 'module_name' including extra-depends
        """
        depends = set(self[module_name].depends +
                      self[module_name].extras_depend)
        for dep in depends:
            depends.update(self.recursive_all_deps(dep))
        return depends

    def __str__(self):
        return "Module-index with %s Modules: %s" % (len(self), self.keys())

    def module_file(self, path):
        """
        Return the path of a module-file (e.g. module_file(sale/sale.odt) gives
        the full path for the file sale.odt in sale module folder)
        """
        path = path.replace('/', os.sep)
        path = path.split(os.sep)
        module = path[0]
        return os.path.join(self[module].path, *path[1:])


# ###LEGACY STUFF#########
def create_graph(module_list=None):
    return Index().create_graph(module_list), [], set()


def get_module_list():
    return Index().keys()


def load_module_graph(graph, pool, lang=None):
    """
    Load all the modules from a given graph
    """
    if lang is None:
        lang = [CONFIG['language']]
    modules_todo = []
    models_to_update_history = set()
    logger = logging.getLogger('modules')
    cursor = Transaction().cursor

    modules = [m.name for m in graph]
    # get all modules in database -> module_states {name: state}
    cursor.execute(*ir_module.select(ir_module.name, ir_module.state,
                                     where=ir_module.name.in_(modules)))

    module_states = dict(cursor.fetchall())

    for module in graph:
        logger.info(module.name)
        classes = pool.setup(module.name)
        package_state = module_states.get(module.name, 'uninstalled')
        if module.is_to_install():
            if package_state == 'installed':
                package_state = 'to upgrade'
            else:
                package_state = 'to install'
        if package_state in ('to install', 'to upgrade'):
            # actually this has no effect sometimes..
            for child in Index().recursive_deps(module.name):
                module_states[child] = package_state

            for cls_type in classes.keys():
                for cls in classes[cls_type]:
                    logger.info('%s:register %s' % (module.name, cls.__name__))
                    cls.__register__(module.name)
            models_to_update_history = set([model.__name__
                                            for model in classes['model']
                                            if hasattr(model, '_history')])
            for model in classes['model']:
                if hasattr(model, '_history'):
                    models_to_update_history.add(model.__name__)

            # Instanciate a new parser for the package:
            tryton_parser = convert.TrytondXmlHandler(
                pool=pool, module=module.name, module_state=package_state
            )

            # load the xml files
            for filename in module.info.get('xml', []):
                filename = filename.replace('/', os.sep)
                logger.info('%s:loading %s' % (module.name, filename))
                # Feed the parser with xml content:
                with open(os.path.join(module.path, filename), 'r') as fp:
                    tryton_parser.parse_xmlstream(fp)

            modules_todo.append((module.name, list(tryton_parser.to_delete)))

            # load the locale files
            for filename in iglob('%s/%s/*.po' % (module.path, 'locale')):
                filename = filename.replace('/', os.sep)
                # /bla/bli/de_DE.po -> de_DE
                lang2 = os.path.splitext(os.path.basename(filename))[0]
                if lang2 not in lang:
                    continue
                logger.info('%s:loading %s' % (
                    module.name, filename[len(module.info['directory']) + 1:]
                ))
                Translation = pool.get('ir.translation')
                # register translation
                Translation.translation_import(lang2, module.name, filename)

            cursor.execute(*ir_module.select(
                ir_module.id, where=(ir_module.name == module.name)
            ))
            try:
                module_id, = cursor.fetchone()
                cursor.execute(*ir_module.update(
                    [ir_module.state], ['installed'],
                    where=(ir_module.id == module_id)
                ))
            except TypeError:
                cursor.execute(*ir_module.insert(
                    [ir_module.create_uid, ir_module.create_date,
                     ir_module.name, ir_module.state],
                    [[0, Now(), module.name, 'installed']]))
            module_states[module.name] = 'installed'

        cursor.commit()

    for model_name in models_to_update_history:
        model = pool.get(model_name)
        if model._history:
            logger.info('history:update %s' % model.__name__)
            model._update_history_table()

    # Vacuum :
    while modules_todo:
        (module_name, to_delete) = modules_todo.pop()
        convert.post_import(pool, module_name, to_delete)

    cursor.commit()


def register_classes():
    '''
    Import all modules to register the classes in the Pool
    '''
    Index().create_index()
    sorted_list = Index().create_graph()

    import trytond.ir
    trytond.ir.register()

    import trytond.res
    trytond.res.register()

    import trytond.webdav
    trytond.webdav.register()

    import trytond.tests
    trytond.tests.register()

    # todo: add to list+dependency: tests>webdav>res>ir

    logger = logging.getLogger('modules')

    for package in sorted_list:

        if package.name in ('ir', 'res', 'webdav', 'tests'):
            continue

        logger.info('%s:registering classes' % package.name)
        if not os.path.isdir(package.path):
            raise Exception('Couldn\'t find module %s' % package.name)

        # Some modules register nothing in the Pool
        module = package.import_module()
        if hasattr(module, 'register'):
            module.register()


def load_modules(database_name, pool, update=False, lang=None):
    """
    Load all modules for a database into a pool
    (also include modules to be installed)
    """

    def _load_modules():
        res = True
        cursor = Transaction().cursor
        if update:
            # Migration from 2.2: workflow module removed
            cursor.execute(*ir_module.delete(
                where=(ir_module.name == 'workflow')))
            if 'all' in CONFIG['init']:
                cursor.execute(*ir_module.select(
                    ir_module.name, where=(ir_module.name != 'tests')
                ))
            else:
                cursor.execute(*ir_module.select(
                    ir_module.name, where=ir_module.state.in_(
                        ('installed', 'to install', 'to upgrade', 'to remove')
                    )
                ))
        else:
            cursor.execute(*ir_module.select(
                ir_module.name, where=ir_module.state.in_(
                    ('installed', 'to upgrade', 'to remove')
                )
            ))
        module_list = [name for (name,) in cursor.fetchall()]
        if update:
            for module in CONFIG['init'].keys():
                if CONFIG['init'][module]:
                    module_list.append(module)
            for module in CONFIG['update'].keys():
                if CONFIG['update'][module] and module != 'all':
                    module_list.append(module)

        sorted_list = Index().create_graph(module_list)
        try:
            load_module_graph(sorted_list, pool, lang)
        except Exception:
            cursor.rollback()
            raise

        if update:
            cursor.execute(*ir_module.select(
                ir_module.name, where=(ir_module.state == 'to remove')
            ))
            fetchall = cursor.fetchall()
            if fetchall:
                for (mod_name,) in fetchall:
                    # TODO check if ressource not updated by the user
                    cursor.execute(*ir_model_data.select(
                        ir_model_data.model, ir_model_data.db_id,
                        where=(ir_model_data.module == mod_name),
                        order_by=ir_model_data.id.desc
                    ))
                    for rmod, rid in cursor.fetchall():
                        Model = pool.get(rmod)
                        Model.delete([Model(rid)])
                    cursor.commit()
                cursor.execute(*ir_module.update(
                    [ir_module.state], ['uninstalled'],
                    where=(ir_module.state == 'to remove')
                ))
                cursor.commit()
                res = False

            Module = pool.get('ir.module.module')
            Module.update_list()
        cursor.commit()
        return res

    if not Transaction().cursor:
        with Transaction().start(database_name, 0):
            res = _load_modules()
    else:
        with Transaction().new_cursor(), \
             Transaction().set_user(0), \
             Transaction().reset_context():
            res = _load_modules()

    Cache.resets(database_name)
    return res

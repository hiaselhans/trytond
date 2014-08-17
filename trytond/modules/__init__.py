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

from trytond.config import CONFIG
from trytond.transaction import Transaction
from trytond.cache import Cache
import trytond.convert as convert

ir_module = Table('ir_module_module')
ir_model_data = Table('ir_model_data')

MODULES_PATH = [os.path.abspath(os.path.dirname(__file__))]
TRYTON_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

EGG_MODULES = {}


class Module(object):
    """
    A class to represent a tryton module (instantiated by its name and module-path)
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

    @property
    def module(self):
        if self._module is None:
            mod_file, pathname, description = imp.find_module(self.name,
                                                              [os.path.dirname(self.path)])
            self._module = imp.load_module('trytond.modules.' + self.name,
                                           mod_file, pathname, description)
            if mod_file is not None:
                mod_file.close()

        return self._module

    def is_to_install(self):
        for kind in ('init', 'update'):
            if 'all' in CONFIG[kind] and self.name != 'tests':
                return True
            elif self.name in CONFIG[kind]:
                return True
        return False

    def __str__(self):
        return "Trytond-module: " % self.name

    def __repr__(self):
        return self.__str__()

    depends = property(lambda self: self.info['depends'])
    extra_depend = property(lambda self: self.info['extra_depend'])


class Index(object):
    """
    Singleton Index instance to know of all available modules and return a sorted list on request
    """
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(Index, cls).__new__(
                cls, *args, **kwargs)
        return cls._instance

    def __init__(self):
        if not hasattr(self, 'module_nodes'):
            self.module_nodes = {}
        self.create_index()

    def __getitem__(self, item):
        try:
            return self.module_nodes[item]
        except KeyError:
            raise Exception('Module %s not found!' % item)

    def create_node(self, name, path):
        if name not in self.module_nodes:
            self.module_nodes[name] = Module(name, path)
        return self.module_nodes[name]

    def create_index(self):
        """
        Check all module sources and fill module_nodes dict {module_name: module}
        """
        # INSERT MODULES FROM Tryton-path
        for modules_path in MODULES_PATH:
            if os.path.isdir(modules_path):
                for file in os.listdir(modules_path):
                    if os.path.isdir(os.path.join(modules_path, file)) and not file.startswith('.'):
                        self.create_node(file, os.path.join(modules_path, file))
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
                    self.create_node(mod_name, mod_path)
                else:
                    self.create_node(mod_name, egg.dist.location)
        except ImportError:
            pass
        # MODULE_PATH_MAPPER.update(EGG_MODULES.keys())
        for name in ('ir', 'res', 'webdav', 'tests'):
            self.create_node(name, os.path.join(TRYTON_ROOT, name))

    def create_graph(self, module_list=None):
        if module_list is None:
            module_list = self.module_nodes.keys()
        sorted_list = [self.module_nodes[key] for key in ('ir', 'res', 'webdav')]
        requires = []

        def _add_deps(module):
            """
            function to first add dependencies recursively, as soon as all
            dependencies are fulfilled, add the module itself
            """
            try:
                node = self.module_nodes[module]
            except KeyError:
                raise ImportError('Module %s not installed' % module)

            if node in sorted_list:
                return
            for dep in node.depends:
                _add_deps(dep)
            if not node in module_list:
                requires.append(node)
            sorted_list.append(node)

        for module in module_list:
            try:
                _add_deps(module)
            except RuntimeError:  # endless recursion on circular depends
                raise ImportError("Invalid dependencies for Module %s" % module)

        return sorted_list

    def get_parents(self, module_name):
        """
        Get all modules which depend on 'module_name'
        """
        return filter(lambda module: module_name in self.get_all_deps(module), self.module_nodes.keys())

    def get_all_deps(self, module_name):
        depends = set(self[module_name].depends)
        for dep in depends:
            depends.union(self.get_all_deps(dep))
        return depends
        # return depends + list(itertools.chain([self.get_all_deps(dep) for dep in depends]))

    def __str__(self):
        return "Module-index with %s Modules: %s" % (len(self.module_nodes), self.module_nodes.keys())

    def module_file(self, path):
        """
        Return the path of a module-file (p.e. module_file(sale/sale.odt) gives the full path
        for the file sale.odt in sale module folder)
        """
        path = path.split('/')
        module = path[0]
        return os.path.join(self[module].path, *path[1:])


# ###LEGACY STUFF#########
def create_graph(module_list):
    return Index().create_graph(module_list), [], set()


def get_module_list():
    return Index().module_nodes.keys()


def load_module_graph(graph, pool, lang=None):
    """
    Load all the module from a given graph (=list of modules sorted from lowest dependencies)
    """
    if lang is None:
        lang = [CONFIG['language']]
    modules_todo = []
    models_to_update_history = set()
    logger = logging.getLogger('modules')
    cursor = Transaction().cursor

    modules = [m.name for m in graph]
    # get all modules in database -> module2state
    cursor.execute(*ir_module.select(ir_module.name, ir_module.state,
                                     where=ir_module.name.in_(modules)))

    module_states = dict(cursor.fetchall())

    for module in graph:
        logger.info(module.name)
        classes = pool.setup(module.name)
        package_state = module_states.get(module.name, 'uninstalled')
        if module.is_to_install():
            # this used to check only for top module if it was installed or not,
            # should be checked for parents also? maybe... what??
            if package_state == 'installed':
                package_state = 'to upgrade'
            else:
                package_state = 'to install'
        if package_state in ('to install', 'to upgrade'):
            for child in Index().get_all_deps(module.name):
                module_states[child] = package_state


            # this used to check only for top module if it was installed or not, should be checked for each maybe...
            for type in classes.keys():
                for cls in classes[type]:
                    logger.info('%s:register %s' % (module.name, cls.__name__))
                    cls.__register__(module.name)
            for model in classes['model']:
                if hasattr(model, '_history'):
                    models_to_update_history.add(model.__name__)

            # Instanciate a new parser for the package:
            tryton_parser = convert.TrytondXmlHandler(pool=pool, module=module.name,
                                                      module_state=package_state)


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
                logger.info('%s:loading %s' % (module.name,
                                               filename[len(module.info['directory']) + 1:]))
                Translation = pool.get('ir.translation')
                #register translation
                Translation.translation_import(lang2, module.name, filename)

            cursor.execute(*ir_module.select(ir_module.id,
                                             where=(ir_module.name == module.name)))
            try:
                module_id, = cursor.fetchone()
                cursor.execute(*ir_module.update([ir_module.state],
                                                 ['installed'], where=(ir_module.id == module_id)))
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
    Import modules to register the classes in the Pool
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
        if hasattr(package.module, 'register'):
            package.module.register()


def load_modules(database_name, pool, update=False, lang=None):
    graph = Index()

    res = True

    def _load_modules():
        global res
        cursor = Transaction().cursor
        if update:
            # Migration from 2.2: workflow module removed
            cursor.execute(*ir_module.delete(
                where=(ir_module.name == 'workflow')))
            if 'all' in CONFIG['init']:
                cursor.execute(*ir_module.select(ir_module.name,
                                                 where=(ir_module.name != 'tests')))
            else:
                cursor.execute(*ir_module.select(ir_module.name,
                                                 where=ir_module.state.in_(('installed', 'to install',
                                                                            'to upgrade', 'to remove'))))
        else:
            cursor.execute(*ir_module.select(ir_module.name,
                                             where=ir_module.state.in_(('installed', 'to upgrade',
                                                                        'to remove'))))
        module_list = [name for (name,) in cursor.fetchall()]
        if update:
            for module in CONFIG['init'].keys():
                if CONFIG['init'][module]:
                    module_list.append(module)
            for module in CONFIG['update'].keys():
                if CONFIG['update'][module]:
                    module_list.append(module)

        sorted_list = graph.create_graph(module_list)

        try:
            load_module_graph(sorted_list, pool, lang)
        except Exception:
            cursor.rollback()
            raise

        if update:
            cursor.execute(*ir_module.select(ir_module.name,
                                             where=(ir_module.state == 'to remove')))
            fetchall = cursor.fetchall()
            if fetchall:
                for (mod_name,) in fetchall:
                    # TODO check if ressource not updated by the user
                    cursor.execute(*ir_model_data.select(ir_model_data.model,
                                                         ir_model_data.db_id,
                                                         where=(ir_model_data.module == mod_name),
                                                         order_by=ir_model_data.id.desc))
                    for rmod, rid in cursor.fetchall():
                        Model = pool.get(rmod)
                        Model.delete([Model(rid)])
                    cursor.commit()
                cursor.execute(*ir_module.update([ir_module.state],
                                                 ['uninstalled'],
                                                 where=(ir_module.state == 'to remove')))
                cursor.commit()
                res = False

            Module = pool.get('ir.module.module')
            Module.update_list()
        cursor.commit()

    if not Transaction().cursor:
        with Transaction().start(database_name, 0):
            _load_modules()
    else:
        with Transaction().new_cursor(), \
             Transaction().set_user(0), \
             Transaction().reset_context():
            _load_modules()

    Cache.resets(database_name)
    return res

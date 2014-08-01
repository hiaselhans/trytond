# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import os
import sys
import itertools
import logging
from functools import reduce
import imp
import operator
import ConfigParser
from glob import iglob

from sql import Table
from sql.functions import Now

import trytond.tools as tools
from trytond.config import CONFIG
from trytond.transaction import Transaction
from trytond.cache import Cache
import trytond.convert as convert

ir_module = Table('ir_module_module')
ir_model_data = Table('ir_model_data')

OPJ = os.path.join
MODULES_PATH = [os.path.abspath(os.path.dirname(__file__))]
TRYTON_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))

MODULES = []

EGG_MODULES = {}

MODULE_PATH_MAPPER = {}


def update_path_mapper():
    """
    Check all module sources and fill MODULE_PATH_MAPPER dict {module_name: path}
    """
    global MODULE_PATH_MAPPER
    # INSERT MODULES FROM Tryton-path
    for modules_path in MODULES_PATH:
        if os.path.isdir(modules_path):
            for file in os.listdir(modules_path):
                if os.path.isdir(OPJ(modules_path, file)) and not file.startswith('.'):
                    MODULE_PATH_MAPPER[file] = os.path.join(modules_path, file)
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
                MODULE_PATH_MAPPER[mod_name] = mod_path
            else:
                MODULE_PATH_MAPPER[mod_name] = egg.dist.location
    except ImportError:
        pass
    # MODULE_PATH_MAPPER.update(EGG_MODULES.keys())
    MODULE_PATH_MAPPER.update({'ir': os.path.join(TRYTON_ROOT, 'ir'),
                               'res': os.path.join(TRYTON_ROOT, 'res'),
                               'webdav': os.path.join(TRYTON_ROOT, 'webdav'),
                               'tests': os.path.join(TRYTON_ROOT, 'tests')})


update_path_mapper()


class Graph():
    def __init__(self, additional_paths=None):
        self._modules_path = [os.path.abspath(os.path.dirname(__file__))]
        if additional_paths is not None:
            self._modules_path += additional_paths
        self._module_path_mapper = {}

    def add_node(self, name, deps):
        for i in [Node(x, self) for x in deps]:
            i.add_child(name)
        if not deps:
            Node(name, self)

    @classmethod
    def create_graph(cls, module_lst):
        graph = cls()
        packages = []

        for module in module_lst:
            new_node = Node(module, graph)
            try:
                info = new_node.get_module_info(MODULE_PATH_MAPPER[module])
            except IOError:
                if module != 'all':
                    raise Exception('Module %s not found' % module)
            packages.append((module, info.get('depends', []),
                             info.get('extras_depend', []), info))

        current, later = set([x[0] for x in packages]), set()
        all_packages = set(current)
        while packages and current > later:
            package, deps, xdep, info = packages[0]

            # if all dependencies of 'package' are already in the graph,
            # add 'package' in the graph
            all_deps = deps + [x for x in xdep if x in all_packages]
            if reduce(lambda x, y: x and y in graph, all_deps, True):
                if not package in current:
                    packages.pop(0)
                    continue
                later.clear()
                current.remove(package)
                graph.add_node(package, all_deps)
                node = Node(package, graph)
                node.info = info
            else:
                later.add(package)
                packages.append((package, deps, xdep, info))
            packages.pop(0)

        missings = set()
        for package, deps, _, _ in packages:
            if package not in later:
                continue
            missings |= set((x for x in deps if x not in graph))
        if missings:
            raise Exception('Missing dependencies: %s' % list(missings
                                                              - set((p[0] for p in packages))))
        return graph, packages, later

    def __iter__(self):
        level = 0
        done = set(self.keys())
        while done:
            level_modules = [(name, module) for name, module in self.items()
                             if module.depth == level]
            for name, module in level_modules:
                done.remove(name)
                yield module
            level += 1

    def __str__(self):
        res = ''
        for i in self:
            res += str(i)
            res += '\n'
        return res


class Node(object):
    def __new__(cls, name, graph):
        """ make this object a "singleton": per graph per name only one instance
        """
        if name in graph:
            inst = graph[name]
        else:
            inst = object.__new__(cls)
            graph[name] = inst
        return inst

    def __init__(self, name, graph):
        super(Node, self).__init__()
        self.name = name
        self.graph = graph

        # __init__ is called even if Node already exists
        if not hasattr(self, 'info'):
            self.info = None
        if not hasattr(self, 'childs'):
            self.childs = []
        if not hasattr(self, 'depth'):
            self.depth = 0

    def add_child(self, name):
        node = Node(name, self.graph)
        node.depth = max(self.depth + 1, node.depth)
        if node not in self.all_children():
            self.childs.append(node)
        self.childs.sort(key=operator.attrgetter('name'))

    def all_children(self):
        res = []
        for child in self.childs:
            res.append(child)
            res += child.all_children()
        return res

    def has_child(self, name):
        """checks if a child exists with the name; faster version of Node(name, self.graph) in self.all_children
        """
        return Node(name, self.graph) in self.childs or \
               any([c.has_child(name) for c in self.childs])

    def __setattr__(self, name, value):
        super(Node, self).__setattr__(name, value)
        if name == 'depth':
            for child in self.childs:
                setattr(child, name, value + 1)

    def __iter__(self):
        """
        faster version of for c in self.all_children: yield c
        """
        return itertools.chain(iter(self.childs),
                               *[iter(x) for x in self.childs])

    def __str__(self):
        return self.pprint()

    def pprint(self, depth=0):
        res = '%s\n' % self.name
        for child in self.childs:
            res += '%s`-> %s' % ('    ' * depth, child.pprint(depth + 1))
        return res

    def get_module_info(self):
        """Return the content of the tryton.cfg"""
        config = ConfigParser.ConfigParser()
        with open(os.path.join(self.name, 'tryton.cfg'), 'r') as fp:
            config.readfp(fp)
        info = dict(config.items('tryton'))
        info['directory'] = self.name
        for key in ('depends', 'extras_depend', 'xml'):
            if key in info:
                info[key] = info[key].strip().splitlines()
        return info


def is_module_to_install(module):
    for kind in ('init', 'update'):
        if 'all' in CONFIG[kind] and module != 'tests':
            return True
        elif module in CONFIG[kind]:
            return True
    return False


def load_module_graph(graph, pool, lang=None):
    if lang is None:
        lang = [CONFIG['language']]
    modules_todo = []
    models_to_update_history = set()
    logger = logging.getLogger('modules')
    cursor = Transaction().cursor

    modules = [x.name for x in graph]
    cursor.execute(*ir_module.select(ir_module.name, ir_module.state,
                                     where=ir_module.name.in_(modules)))
    module2state = dict(cursor.fetchall())

    for package in graph:
        module = package.name
        if module not in MODULES:
            continue
        logger.info(module)
        classes = pool.setup(module)
        package_state = module2state.get(module, 'uninstalled')
        if (is_module_to_install(module)
            or package_state in ('to install', 'to upgrade')):
            if package_state not in ('to install', 'to upgrade'):
                if package_state == 'installed':
                    package_state = 'to upgrade'
                else:
                    package_state = 'to install'
            for child in package.childs:
                module2state[child.name] = package_state
            for type in classes.keys():
                for cls in classes[type]:
                    logger.info('%s:register %s' % (module, cls.__name__))
                    cls.__register__(module)
            for model in classes['model']:
                if hasattr(model, '_history'):
                    models_to_update_history.add(model.__name__)

            # Instanciate a new parser for the package:
            tryton_parser = convert.TrytondXmlHandler(pool=pool, module=module,
                                                      module_state=package_state)

            for filename in package.info.get('xml', []):
                filename = filename.replace('/', os.sep)
                logger.info('%s:loading %s' % (module, filename))
                # Feed the parser with xml content:
                with tools.file_open(OPJ(module, filename)) as fp:
                    tryton_parser.parse_xmlstream(fp)

            modules_todo.append((module, list(tryton_parser.to_delete)))

            for filename in iglob('%s/%s/*.po'
                    % (package.info['directory'], 'locale')):
                filename = filename.replace('/', os.sep)
                lang2 = os.path.splitext(os.path.basename(filename))[0]
                if lang2 not in lang:
                    continue
                logger.info('%s:loading %s' % (module,
                                               filename[len(package.info['directory']) + 1:]))
                Translation = pool.get('ir.translation')
                Translation.translation_import(lang2, module, filename)

            cursor.execute(*ir_module.select(ir_module.id,
                                             where=(ir_module.name == package.name)))
            try:
                module_id, = cursor.fetchone()
                cursor.execute(*ir_module.update([ir_module.state],
                                                 ['installed'], where=(ir_module.id == module_id)))
            except TypeError:
                cursor.execute(*ir_module.insert(
                    [ir_module.create_uid, ir_module.create_date,
                     ir_module.name, ir_module.state],
                    [[0, Now(), package.name, 'installed']]))
            module2state[package.name] = 'installed'

        cursor.commit()

    for model_name in models_to_update_history:
        model = pool.get(model_name)
        if model._history:
            logger.info('history:update %s' % model.__name__)
            model._update_history_table()

    # Vacuum :
    while modules_todo:
        (module, to_delete) = modules_todo.pop()
        convert.post_import(pool, module, to_delete)

    cursor.commit()


# def get_module_list():
# module_list = set()
# if os.path.exists(MODULES_PATH) and os.path.isdir(MODULES_PATH):
# for file in os.listdir(MODULES_PATH):
#             if file.startswith('.'):
#                 continue
#             if os.path.isdir(OPJ(MODULES_PATH, file)):
#                 module_list.add(file)
#     update_egg_modules()
#     module_list.update(EGG_MODULES.keys())
#     module_list.add('ir')
#     module_list.add('res')
#     module_list.add('webdav')
#     module_list.add('tests')
#     return list(module_list)
def get_module_list():
    update_path_mapper()
    return MODULE_PATH_MAPPER.keys()


def register_classes():
    '''
    Import modules to register the classes in the Pool
    '''
    import trytond.ir

    trytond.ir.register()
    import trytond.res

    trytond.res.register()
    import trytond.webdav

    trytond.webdav.register()
    import trytond.tests

    trytond.tests.register()
    logger = logging.getLogger('modules')

    modules = [name for name in MODULE_PATH_MAPPER]
    for package in create_graph(modules)[0]:
        module = package.name
        logger.info('%s:registering classes' % module)

        if module in ('ir', 'res', 'webdav', 'tests'):
            MODULES.append(module)
            continue

        mod_path = MODULE_PATH_MAPPER[module]
        if not os.path.isdir(mod_path):
            raise Exception('Couldn\'t find module %s' % module)
        print(mod_path)
        mod_file, pathname, description = imp.find_module(module,
                                                          [os.path.dirname(mod_path)])
        the_module = imp.load_module('trytond.modules.' + module,
                                     mod_file, pathname, description)
        # Some modules register nothing in the Pool
        if hasattr(the_module, 'register'):
            the_module.register()
        if mod_file is not None:
            mod_file.close()
        MODULES.append(module)


def load_modules(database_name, pool, update=False, lang=None):
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
        graph = create_graph(module_list)[0]

        try:
            load_module_graph(graph, pool, lang)
        except Exception:
            cursor.rollback()
            raise

        if update:
            cursor.execute(*ir_module.select(ir_module.name,
                                             where=(ir_module.state == 'to remove')))
            fetchall = cursor.fetchall()
            if fetchall:
                for (mod_name,) in fetchall:
                    #TODO check if ressource not updated by the user
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

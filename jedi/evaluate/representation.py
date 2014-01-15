"""
Like described in the :mod:`parsing_representation` module, there's a need for
an ast like module to represent the states of parsed modules.

But now there are also structures in Python that need a little bit more than
that. An ``Instance`` for example is only a ``Class`` before it is
instantiated. This class represents these cases.

So, why is there also a ``Class`` class here? Well, there are decorators and
they change classes in Python 3.
"""
import copy

from jedi._compatibility import use_metaclass, unicode
from jedi.parser import representation as pr
from jedi import debug
from jedi import common
from jedi.evaluate.cache import memoize_default, CachedMetaClass
from jedi.evaluate import compiled
from jedi.evaluate import recursion
from jedi.evaluate import iterable
from jedi.evaluate import docstrings
from jedi.evaluate import helpers
from jedi.evaluate import param


class Executable(pr.IsScope):
    """
    An instance is also an executable - because __init__ is called
    :param var_args: The param input array, consist of `pr.Array` or list.
    """
    def __init__(self, evaluator, base, var_args=()):
        self._evaluator = evaluator
        self.base = base
        self.var_args = var_args

    def get_parent_until(self, *args, **kwargs):
        return self.base.get_parent_until(*args, **kwargs)

    @common.safe_property
    def parent(self):
        return self.base.parent


class Instance(use_metaclass(CachedMetaClass, Executable)):
    """
    This class is used to evaluate instances.
    """
    def __init__(self, evaluator, base, var_args=()):
        super(Instance, self).__init__(evaluator, base, var_args)
        if str(base.name) in ['list', 'set'] \
                and compiled.builtin == base.get_parent_until():
            # compare the module path with the builtin name.
            self.var_args = iterable.check_array_instances(evaluator, self)
        else:
            # need to execute the __init__ function, because the dynamic param
            # searching needs it.
            with common.ignored(KeyError):
                self.execute_subscope_by_name('__init__', self.var_args)
        # Generated instances are classes that are just generated by self
        # (No var_args) used.
        self.is_generated = False

    @memoize_default(None)
    def _get_method_execution(self, func):
        func = InstanceElement(self._evaluator, self, func, True)
        return FunctionExecution(self._evaluator, func, self.var_args)

    def _get_func_self_name(self, func):
        """
        Returns the name of the first param in a class method (which is
        normally self.
        """
        try:
            return str(func.params[0].get_name())
        except IndexError:
            return None

    @memoize_default([])
    def get_self_attributes(self):
        def add_self_dot_name(name):
            """
            Need to copy and rewrite the name, because names are now
            ``instance_usage.variable`` instead of ``self.variable``.
            """
            n = copy.copy(name)
            n.names = n.names[1:]
            names.append(InstanceElement(self._evaluator, self, n))

        names = []
        # This loop adds the names of the self object, copies them and removes
        # the self.
        for sub in self.base.subscopes:
            if isinstance(sub, pr.Class):
                continue
            # Get the self name, if there's one.
            self_name = self._get_func_self_name(sub)
            if not self_name:
                continue

            if sub.name.get_code() == '__init__':
                # ``__init__`` is special because the params need are injected
                # this way. Therefore an execution is necessary.
                if not sub.decorators:
                    # __init__ decorators should generally just be ignored,
                    # because to follow them and their self variables is too
                    # complicated.
                    sub = self._get_method_execution(sub)
            for n in sub.get_set_vars():
                # Only names with the selfname are being added.
                # It is also important, that they have a len() of 2,
                # because otherwise, they are just something else
                if n.names[0] == self_name and len(n.names) == 2:
                    add_self_dot_name(n)

        if not isinstance(self.base, compiled.CompiledObject):
            for s in self.base.get_super_classes():
                for inst in self._evaluator.execute(s):
                    names += inst.get_self_attributes()
        return names

    def get_subscope_by_name(self, name):
        sub = self.base.get_subscope_by_name(name)
        return InstanceElement(self._evaluator, self, sub, True)

    def execute_subscope_by_name(self, name, args=()):
        method = self.get_subscope_by_name(name)
        return self._evaluator.execute(method, args)

    def get_descriptor_return(self, obj):
        """ Throws a KeyError if there's no method. """
        # Arguments in __get__ descriptors are obj, class.
        # `method` is the new parent of the array, don't know if that's good.
        args = [obj, obj.base] if isinstance(obj, Instance) else [None, obj]
        return self.execute_subscope_by_name('__get__', args)

    @memoize_default([])
    def get_defined_names(self):
        """
        Get the instance vars of a class. This includes the vars of all
        classes
        """
        names = self.get_self_attributes()

        for var in self.base.instance_names():
            names.append(InstanceElement(self._evaluator, self, var, True))
        return names

    def scope_generator(self):
        """
        An Instance has two scopes: The scope with self names and the class
        scope. Instance variables have priority over the class scope.
        """
        yield self, self.get_self_attributes()

        names = []
        for var in self.base.instance_names():
            names.append(InstanceElement(self._evaluator, self, var, True))
        yield self, names

    def get_index_types(self, index=None):
        args = [] if index is None else [index]
        try:
            return self.execute_subscope_by_name('__getitem__', args)
        except KeyError:
            debug.warning('No __getitem__, cannot access the array.')
            return []

    def __getattr__(self, name):
        if name not in ['start_pos', 'end_pos', 'name', 'get_imports',
                        'doc', 'docstr', 'asserts']:
            raise AttributeError("Instance %s: Don't touch this (%s)!"
                                 % (self, name))
        return getattr(self.base, name)

    def __repr__(self):
        return "<e%s of %s (var_args: %s)>" % \
            (type(self).__name__, self.base, len(self.var_args or []))


class InstanceElement(use_metaclass(CachedMetaClass, pr.Base)):
    """
    InstanceElement is a wrapper for any object, that is used as an instance
    variable (e.g. self.variable or class methods).
    """
    def __init__(self, evaluator, instance, var, is_class_var=False):
        self._evaluator = evaluator
        if isinstance(var, pr.Function):
            var = Function(evaluator, var)
        elif isinstance(var, pr.Class):
            var = Class(evaluator, var)
        self.instance = instance
        self.var = var
        self.is_class_var = is_class_var

    @common.safe_property
    @memoize_default(None)
    def parent(self):
        par = self.var.parent
        if isinstance(par, Class) and par == self.instance.base \
                or isinstance(par, pr.Class) \
                and par == self.instance.base.base:
            par = self.instance
        elif not isinstance(par, (pr.Module, compiled.CompiledObject)):
            par = InstanceElement(self.instance._evaluator, self.instance, par, self.is_class_var)
        return par

    def get_parent_until(self, *args, **kwargs):
        return pr.Simple.get_parent_until(self, *args, **kwargs)

    def get_decorated_func(self):
        """ Needed because the InstanceElement should not be stripped """
        func = self.var.get_decorated_func(self.instance)
        if func == self.var:
            return self
        return func

    def expression_list(self):
        # Copy and modify the array.
        return [InstanceElement(self.instance._evaluator, self.instance, command, self.is_class_var)
                if not isinstance(command, unicode) else command
                for command in self.var.expression_list()]

    def __iter__(self):
        for el in self.var.__iter__():
            yield InstanceElement(self.instance._evaluator, self.instance, el, self.is_class_var)

    def __getattr__(self, name):
        return getattr(self.var, name)

    def isinstance(self, *cls):
        return isinstance(self.var, cls)

    def __repr__(self):
        return "<%s of %s>" % (type(self).__name__, self.var)


class Class(use_metaclass(CachedMetaClass, pr.IsScope)):
    """
    This class is not only important to extend `pr.Class`, it is also a
    important for descriptors (if the descriptor methods are evaluated or not).
    """
    def __init__(self, evaluator, base):
        self._evaluator = evaluator
        self.base = base

    @memoize_default(default=())
    def get_super_classes(self):
        supers = []
        # TODO care for mro stuff (multiple super classes).
        for s in self.base.supers:
            # Super classes are statements.
            for cls in self._evaluator.eval_statement(s):
                if not isinstance(cls, Class):
                    debug.warning('Received non class, as a super class')
                    continue  # Just ignore other stuff (user input error).
                supers.append(cls)
        if not supers and self.base.parent != compiled.builtin:
            # add `object` to classes
            supers += self._evaluator.find_types(compiled.builtin, 'object')
        return supers

    @memoize_default(default=())
    def instance_names(self):
        def in_iterable(name, iterable):
            """ checks if the name is in the variable 'iterable'. """
            for i in iterable:
                # Only the last name is important, because these names have a
                # maximal length of 2, with the first one being `self`.
                if i.names[-1] == name.names[-1]:
                    return True
            return False

        result = self.base.get_defined_names()
        super_result = []
        # TODO mro!
        for cls in self.get_super_classes():
            # Get the inherited names.
            if isinstance(cls, compiled.CompiledObject):
                super_result += cls.get_defined_names()
            else:
                for i in cls.instance_names():
                    if not in_iterable(i, result):
                        super_result.append(i)
        result += super_result
        return result

    @memoize_default(default=())
    def get_defined_names(self):
        result = self.instance_names()
        type_cls = self._evaluator.find_types(compiled.builtin, 'type')[0]
        return result + list(type_cls.get_defined_names())

    def get_subscope_by_name(self, name):
        for sub in reversed(self.subscopes):
            if sub.name.get_code() == name:
                return sub
        raise KeyError("Couldn't find subscope.")

    @common.safe_property
    def name(self):
        return self.base.name

    def __getattr__(self, name):
        if name not in ['start_pos', 'end_pos', 'parent', 'asserts', 'docstr',
                        'doc', 'get_imports', 'get_parent_until', 'get_code',
                        'subscopes']:
            raise AttributeError("Don't touch this: %s of %s !" % (name, self))
        return getattr(self.base, name)

    def __repr__(self):
        return "<e%s of %s>" % (type(self).__name__, self.base)


class Function(use_metaclass(CachedMetaClass, pr.IsScope)):
    """
    Needed because of decorators. Decorators are evaluated here.
    """
    def __init__(self, evaluator, func, is_decorated=False):
        """ This should not be called directly """
        self._evaluator = evaluator
        self.base_func = func
        self.is_decorated = is_decorated

    @memoize_default(None)
    def _decorated_func(self, instance=None):
        """
        Returns the function, that is to be executed in the end.
        This is also the places where the decorators are processed.
        """
        f = self.base_func

        # Only enter it, if has not already been processed.
        if not self.is_decorated:
            for dec in reversed(self.base_func.decorators):
                debug.dbg('decorator: %s %s', dec, f)
                dec_results = set(self._evaluator.eval_statement(dec))
                if not len(dec_results):
                    debug.warning('decorator not found: %s on %s', dec, self.base_func)
                    return None
                decorator = dec_results.pop()
                if dec_results:
                    debug.warning('multiple decorators found %s %s',
                                  self.base_func, dec_results)
                # Create param array.
                old_func = Function(self._evaluator, f, is_decorated=True)
                if instance is not None and decorator.isinstance(Function):
                    old_func = InstanceElement(self._evaluator, instance, old_func)
                    instance = None

                wrappers = self._evaluator.execute(decorator, (old_func,))
                if not len(wrappers):
                    debug.warning('no wrappers found %s', self.base_func)
                    return None
                if len(wrappers) > 1:
                    # TODO resolve issue with multiple wrappers -> multiple types
                    debug.warning('multiple wrappers found %s %s',
                                  self.base_func, wrappers)
                f = wrappers[0]

                debug.dbg('decorator end %s', f)
        if f != self.base_func and isinstance(f, pr.Function):
            f = Function(self._evaluator, f)
        return f

    def get_decorated_func(self, instance=None):
        decorated_func = self._decorated_func(instance)
        if decorated_func == self.base_func:
            return self
        if decorated_func is None:
            # If the decorator func is not found, just ignore the decorator
            # function, because sometimes decorators are just really
            # complicated.
            return Function(self._evaluator, self.base_func, True)
        return decorated_func

    def get_magic_function_names(self):
        return compiled.magic_function_class.get_defined_names()

    def get_magic_function_scope(self):
        return compiled.magic_function_class

    def __getattr__(self, name):
        return getattr(self.base_func, name)

    def __repr__(self):
        dec = ''
        if self._decorated_func() != self.base_func:
            dec = " is " + repr(self._decorated_func())
        return "<e%s of %s%s>" % (type(self).__name__, self.base_func, dec)


class FunctionExecution(Executable):
    """
    This class is used to evaluate functions and their returns.

    This is the most complicated class, because it contains the logic to
    transfer parameters. It is even more complicated, because there may be
    multiple calls to functions and recursion has to be avoided. But this is
    responsibility of the decorators.
    """
    @memoize_default(default=())
    @recursion.execution_recursion_decorator
    def get_return_types(self, evaluate_generator=False):
        func = self.base
        # Feed the listeners, with the params.
        for listener in func.listeners:
            listener.execute(self._get_params())
        if func.is_generator and not evaluate_generator:
            return [iterable.Generator(self._evaluator, func, self.var_args)]
        else:
            stmts = docstrings.find_return_types(self._evaluator, func)
            for r in self.returns:
                if r is not None:
                    stmts += self._evaluator.eval_statement(r)
            return stmts

    @memoize_default(default=())
    def _get_params(self):
        """
        This returns the params for an TODO and is injected as a
        'hack' into the pr.Function class.
        This needs to be here, because Instance can have __init__ functions,
        which act the same way as normal functions.
        """
        return param.get_params(self._evaluator, self.base, self.var_args)

    def get_defined_names(self):
        """
        Call the default method with the own instance (self implements all
        the necessary functions). Add also the params.
        """
        return self._get_params() + pr.Scope.get_set_vars(self)

    get_set_vars = get_defined_names

    def _copy_properties(self, prop):
        """
        Literally copies a property of a Function. Copying is very expensive,
        because it is something like `copy.deepcopy`. However, these copied
        objects can be used for the executions, as if they were in the
        execution.
        """
        # Copy all these lists into this local function.
        attr = getattr(self.base, prop)
        objects = []
        for element in attr:
            if element is None:
                copied = element
            else:
                copied = helpers.fast_parent_copy(element)
                copied.parent = self._scope_copy(copied.parent)
                if isinstance(copied, pr.Function):
                    copied = Function(self._evaluator, copied)
            objects.append(copied)
        return objects

    def __getattr__(self, name):
        if name not in ['start_pos', 'end_pos', 'imports', '_sub_module']:
            raise AttributeError('Tried to access %s: %s. Why?' % (name, self))
        return getattr(self.base, name)

    @memoize_default(None)
    def _scope_copy(self, scope):
        """ Copies a scope (e.g. if) in an execution """
        # TODO method uses different scopes than the subscopes property.

        # just check the start_pos, sometimes it's difficult with closures
        # to compare the scopes directly.
        if scope.start_pos == self.start_pos:
            return self
        else:
            copied = helpers.fast_parent_copy(scope)
            copied.parent = self._scope_copy(copied.parent)
            return copied

    @common.safe_property
    @memoize_default([])
    def returns(self):
        return self._copy_properties('returns')

    @common.safe_property
    @memoize_default([])
    def asserts(self):
        return self._copy_properties('asserts')

    @common.safe_property
    @memoize_default([])
    def statements(self):
        return self._copy_properties('statements')

    @common.safe_property
    @memoize_default([])
    def subscopes(self):
        return self._copy_properties('subscopes')

    def get_statement_for_position(self, pos):
        return pr.Scope.get_statement_for_position(self, pos)

    def __repr__(self):
        return "<%s of %s>" % \
            (type(self).__name__, self.base)

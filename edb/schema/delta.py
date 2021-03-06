#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


from __future__ import annotations
from typing import *

import collections
import collections.abc
import contextlib
import itertools
import uuid

from edb import errors

from edb.common import adapter
from edb.common import checked
from edb.common import markup
from edb.common import ordered
from edb.common import parsing
from edb.common import struct
from edb.common import topological
from edb.common import verutils

from edb.edgeql import ast as qlast
from edb.edgeql import compiler as qlcompiler
from edb.edgeql import qltypes
from edb.edgeql import quote as qlquote

from . import expr as s_expr
from . import name as sn
from . import objects as so
from . import schema as s_schema
from . import utils


def delta_objects(
    old: Iterable[so.Object_T],
    new: Iterable[so.Object_T],
    sclass: Type[so.Object_T],
    *,
    context: so.ComparisonContext,
    old_schema: s_schema.Schema,
    new_schema: s_schema.Schema,
) -> DeltaRoot:

    delta = DeltaRoot()

    oldkeys = {o.id: o.hash_criteria(old_schema) for o in old}
    newkeys = {o.id: o.hash_criteria(new_schema) for o in new}

    unchanged = set(oldkeys.values()) & set(newkeys.values())

    old = ordered.OrderedSet[so.Object_T](
        o for o in old
        if oldkeys[o.id] not in unchanged
    )
    new = ordered.OrderedSet[so.Object_T](
        o for o in new
        if newkeys[o.id] not in unchanged
    )

    oldnames = {o.get_name(old_schema) for o in old}
    newnames = {o.get_name(new_schema) for o in new}
    common_names = oldnames & newnames

    pairs = sorted(
        itertools.product(new, old),
        key=lambda pair: pair[0].get_name(new_schema) not in common_names,
    )

    full_matrix: List[Tuple[so.Object_T, so.Object_T, float]] = []

    for x, y in pairs:
        x_name = x.get_name(new_schema)
        y_name = y.get_name(old_schema)

        if (
            context.guidance is not None
            and (sclass, (y_name, x_name)) in context.guidance.banned_alters
        ):
            similarity = 0.0
        else:
            similarity = y.compare(
                x,
                our_schema=old_schema,
                their_schema=new_schema,
                context=context,
            )

        full_matrix.append((x, y, similarity))

    full_matrix.sort(
        key=lambda v: (
            1.0 - v[2],
            v[0].get_name(new_schema),
            v[1].get_name(old_schema),
        ),
    )

    seen_x = set()
    seen_y = set()
    comparison_map: Dict[so.Object_T, Tuple[float, so.Object_T]] = {}
    for x, y, similarity in full_matrix:
        if x not in seen_x and y not in seen_y:
            comparison_map[x] = (similarity, y)
            seen_x.add(x)
            seen_y.add(y)

    alters = []

    if comparison_map:
        if issubclass(sclass, so.InheritingObject):
            # Generate the diff from the top of the inheritance
            # hierarchy, since changes to parent objects may inform
            # how the delta in child objects is treated.
            order_x = cast(
                Iterable[so.Object_T],
                _sort_by_inheritance(
                    new_schema,
                    cast(Iterable[so.InheritingObject], comparison_map),
                ),
            )
        else:
            order_x = comparison_map

        for x in order_x:
            s, y = comparison_map[x]
            if 0.6 < s < 1.0:
                alter = y.as_alter_delta(
                    other=x,
                    context=context,
                    self_schema=old_schema,
                    other_schema=new_schema,
                )

                alters.append(alter)

    created = new - {x for x, (s, _) in comparison_map.items() if s > 0.6}

    for x in created:
        if (
            context.guidance is None
            or (
                (sclass, x.get_name(new_schema))
                not in context.guidance.banned_creations
            )
        ):
            delta.add(
                x.as_create_delta(
                    schema=new_schema,
                    context=context,
                ),
            )

    delta.update(alters)

    deleted_order: Iterable[so.Object]
    deleted = old - {y for _, (s, y) in comparison_map.items() if s > 0.6}

    if issubclass(sclass, so.InheritingObject):
        deleted_order = _sort_by_inheritance(
            old_schema,
            cast(Iterable[so.InheritingObject], deleted),
        )
    else:
        deleted_order = deleted

    for obj in deleted_order:
        if (
            context.guidance is None
            or (
                (sclass, obj.get_name(old_schema))
                not in context.guidance.banned_deletions
            )
        ):
            delta.add(
                obj.as_delete_delta(
                    schema=old_schema,
                    context=context,
                ),
            )

    return delta


def _sort_by_inheritance(
    schema: s_schema.Schema,
    objs: Iterable[so.InheritingObjectT],
) -> Iterable[so.InheritingObjectT]:
    graph = {}
    for x in objs:
        graph[x] = {
            'item': x,
            'deps': x.get_bases(schema).objects(schema),
        }

    return cast(
        Iterable[so.InheritingObjectT],
        topological.sort(graph, allow_unresolved=True),
    )


CommandMeta_T = TypeVar("CommandMeta_T", bound="CommandMeta")


class CommandMeta(
    adapter.Adapter,
    struct.MixedStructMeta,
    markup.MarkupCapableMeta,
):

    _astnode_map: Dict[Type[qlast.DDLOperation], Type[Command]] = {}

    def __new__(
        mcls: Type[CommandMeta_T],
        name: str,
        bases: Tuple[type, ...],
        dct: Dict[str, Any],
        *,
        context_class: Optional[Type[CommandContextToken[Command]]] = None,
        **kwargs: Any,
    ) -> CommandMeta_T:
        cls = super().__new__(mcls, name, bases, dct, **kwargs)

        if context_class is not None:
            cast(Command, cls)._context_class = context_class

        return cls

    def __init__(
        cls,
        name: str,
        bases: Tuple[type, ...],
        clsdict: Dict[str, Any],
        *,
        adapts: Optional[type] = None,
        **kwargs: Any,
    ) -> None:
        adapter.Adapter.__init__(cls, name, bases, clsdict, adapts=adapts)
        struct.MixedStructMeta.__init__(cls, name, bases, clsdict)
        astnodes = clsdict.get('astnode')
        if astnodes and not isinstance(astnodes, (list, tuple)):
            astnodes = [astnodes]
        if astnodes:
            cls.register_astnodes(astnodes)

    def register_astnodes(
        cls,
        astnodes: Iterable[Type[qlast.DDLCommand]],
    ) -> None:
        mapping = type(cls)._astnode_map

        for astnode in astnodes:
            existing = mapping.get(astnode)
            if existing:
                msg = ('duplicate EdgeQL AST node to command mapping: ' +
                       '{!r} is already declared for {!r}')
                raise TypeError(msg.format(astnode, existing))

            mapping[astnode] = cast(Type["Command"], cls)


_void = object()


# We use _DummyObject for contexts where an instance of an object is
# required by type signatures, and the actual reference will be quickly
# replaced by a real object.
_dummy_object = so.Object(_private_init=True)


Command_T = TypeVar("Command_T", bound="Command")


class Command(struct.MixedStruct, metaclass=CommandMeta):
    source_context = struct.Field(parsing.ParserContext, default=None)
    canonical = struct.Field(bool, default=False)

    _context_class: Optional[Type[CommandContextToken[Command]]] = None

    ops: ordered.OrderedSet[Command]
    before_ops: ordered.OrderedSet[Command]

    #: AlterObjectProperty lookup table for get|set_attribute_value
    _attrs: Dict[str, AlterObjectProperty]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.ops = ordered.OrderedSet()
        self.before_ops = ordered.OrderedSet()
        self.qlast: qlast.DDLOperation
        self._attrs = {}

    def copy(self: Command_T) -> Command_T:
        result = super().copy()
        result.ops = ordered.OrderedSet(
            op.copy() for op in self.ops)
        result.before_ops = ordered.OrderedSet(
            op.copy() for op in self.before_ops)
        return result

    @classmethod
    def adapt(cls: Type[Command_T], obj: Command) -> Command_T:
        result = obj.copy_with_class(cls)
        mcls = cast(CommandMeta, type(cls))
        for op in obj.get_prerequisites():
            result.add_prerequisite(mcls.adapt(op))
        for op in obj.get_subcommands(include_prerequisites=False):
            result.add(mcls.adapt(op))
        return result

    def _resolve_attr_value(
        self,
        value: Any,
        fname: str,
        field: so.Field[Any],
        schema: s_schema.Schema,
    ) -> Any:
        ftype = field.type

        if isinstance(value, so.Shell):
            value = value.resolve(schema)
        else:
            if issubclass(ftype, so.ObjectDict):
                if isinstance(value, so.ObjectDict):
                    items = dict(value.items(schema))
                elif isinstance(value, collections.abc.Mapping):
                    items = {}
                    for k, v in value.items():
                        if isinstance(v, so.Shell):
                            val = v.resolve(schema)
                        else:
                            val = v
                        items[k] = val

                value = ftype.create(schema, items)

            elif issubclass(ftype, so.ObjectCollection):
                sequence: Sequence[so.Object]
                if isinstance(value, so.ObjectCollection):
                    sequence = value.objects(schema)
                else:
                    sequence = []
                    for v in value:
                        if isinstance(v, so.Shell):
                            val = v.resolve(schema)
                        else:
                            val = v
                        sequence.append(val)
                value = ftype.create(schema, sequence)

            elif issubclass(ftype, s_expr.Expression):
                if value is not None:
                    value = ftype.from_expr(value, schema)

            else:
                value = field.coerce_value(schema, value)

        return value

    def enumerate_attributes(self) -> Tuple[str, ...]:
        return tuple(self._attrs)

    def has_attribute_value(self, attr_name: str) -> bool:
        return attr_name in self._attrs

    def get_attribute_set_cmd(
        self,
        attr_name: str,
    ) -> Optional[AlterObjectProperty]:
        return self._attrs.get(attr_name)

    def get_attribute_value(
        self,
        attr_name: str,
    ) -> Any:
        op = self.get_attribute_set_cmd(attr_name)
        if op is not None:
            return op.new_value
        else:
            return None

    def get_local_attribute_value(
        self,
        attr_name: str,
    ) -> Any:
        """Return the new value of field, if not inherited."""
        op = self.get_attribute_set_cmd(attr_name)
        if op is not None and op.source != 'inheritance':
            return op.new_value
        else:
            return None

    def get_orig_attribute_value(
        self,
        attr_name: str,
    ) -> Any:
        op = self.get_attribute_set_cmd(attr_name)
        if op is not None:
            return op.old_value
        else:
            return None

    def get_attribute_source_context(
        self,
        attr_name: str,
    ) -> Optional[parsing.ParserContext]:
        op = self.get_attribute_set_cmd(attr_name)
        if op is not None:
            return op.source_context
        else:
            return None

    def set_attribute_value(
        self,
        attr_name: str,
        value: Any,
        *,
        orig_value: Any = None,
        inherited: bool = False,
        source_context: Optional[parsing.ParserContext] = None,
    ) -> None:
        orig_op = op = self.get_attribute_set_cmd(attr_name)
        if op is None:
            op = AlterObjectProperty(property=attr_name, new_value=value)
        else:
            op.new_value = value

        if inherited:
            op.source = 'inheritance'
        if source_context is not None:
            op.source_context = source_context
        if orig_value is not None:
            op.old_value = orig_value

        if orig_op is None:
            self.add(op)

    def discard_attribute(self, attr_name: str) -> None:
        op = self.get_attribute_set_cmd(attr_name)
        if op is not None:
            self.discard(op)

    def __iter__(self) -> NoReturn:
        raise TypeError(f'{type(self)} object is not iterable')

    @overload
    def get_subcommands(
        self,
        *,
        type: Type[Command_T],
        metaclass: Optional[Type[so.Object]] = None,
        include_prerequisites: bool = True,
    ) -> Tuple[Command_T, ...]:
        ...

    @overload
    def get_subcommands(  # NoQA: F811
        self,
        *,
        type: None = None,
        metaclass: Optional[Type[so.Object]] = None,
        include_prerequisites: bool = True,
    ) -> Tuple[Command, ...]:
        ...

    def get_subcommands(  # NoQA: F811
        self,
        *,
        type: Union[Type[Command_T], None] = None,
        metaclass: Optional[Type[so.Object]] = None,
        include_prerequisites: bool = True,
    ) -> Tuple[Command, ...]:
        ops: Iterable[Command]
        if include_prerequisites:
            ops = itertools.chain(self.before_ops, self.ops)
        else:
            ops = self.ops

        filters = []

        if type is not None:
            t = type
            filters.append(lambda i: isinstance(i, t))

        if metaclass is not None:
            mcls = metaclass
            filters.append(
                lambda i: (
                    isinstance(i, ObjectCommand)
                    and issubclass(i.get_schema_metaclass(), mcls)
                )
            )

        if filters:
            return tuple(filter(lambda i: all(f(i) for f in filters), ops))
        else:
            return tuple(ops)

    @overload
    def get_prerequisites(
        self,
        *,
        type: Type[Command_T],
        include_prerequisites: bool = True,
    ) -> Tuple[Command_T, ...]:
        ...

    @overload
    def get_prerequisites(  # NoQA: F811
        self,
        *,
        type: None = None,
    ) -> Tuple[Command, ...]:
        ...

    def get_prerequisites(  # NoQA: F811
        self,
        *,
        type: Union[Type[Command_T], None] = None,
        include_prerequisites: bool = True,
    ) -> Tuple[Command, ...]:
        if type is not None:
            t = type
            return tuple(filter(lambda i: isinstance(i, t), self.before_ops))
        else:
            return tuple(self.before_ops)

    def has_subcommands(self) -> bool:
        return bool(self.ops) or bool(self.before_ops)

    def get_nonattr_subcommand_count(self) -> int:
        count = 0
        for op in self.ops:
            if not isinstance(op, AlterObjectProperty):
                count += 1
        for op in self.before_ops:
            if not isinstance(op, AlterObjectProperty):
                count += 1
        return count

    def prepend_prerequisite(self, command: Command) -> None:
        if isinstance(command, CommandGroup):
            for op in reversed(command.get_subcommands()):
                self.prepend_prerequisite(op)
        else:
            self.before_ops.add(command, last=False)

    def add_prerequisite(self, command: Command) -> None:
        if isinstance(command, CommandGroup):
            self.before_ops.update(command.get_subcommands())  # type: ignore
        else:
            self.before_ops.add(command)

    def prepend(self, command: Command) -> None:
        if isinstance(command, CommandGroup):
            for op in reversed(command.get_subcommands()):
                self.prepend(op)
        else:
            if isinstance(command, AlterObjectProperty):
                self._attrs[command.property] = command
            self.ops.add(command, last=False)

    def add(self, command: Command) -> None:
        if isinstance(command, CommandGroup):
            self.update(command.get_subcommands())
        else:
            if isinstance(command, AlterObjectProperty):
                self._attrs[command.property] = command
            self.ops.add(command)

    def update(self, commands: Iterable[Command]) -> None:  # type: ignore
        for command in commands:
            self.add(command)

    def replace(self, existing: Command, new: Command) -> None:  # type: ignore
        self.ops.replace(existing, new)

    def replace_all(self, commands: Iterable[Command]) -> None:
        self.ops.clear()
        self._attrs.clear()
        self.update(commands)

    def discard(self, command: Command) -> None:
        self.ops.discard(command)
        self.before_ops.discard(command)
        if isinstance(command, AlterObjectProperty):
            self._attrs.pop(command.property)

    def apply(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        return schema

    def get_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        context_class = type(self).get_context_class()
        assert context_class is not None
        with context(context_class(schema=schema, op=self)):
            return self._get_ast(schema, context, parent_node=parent_node)

    def _get_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        raise NotImplementedError

    @classmethod
    def get_orig_expr_text(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        name: str,
    ) -> Optional[str]:
        orig_text_expr = qlast.get_ddl_field_value(astnode, f'orig_{name}')
        if orig_text_expr:
            orig_text = qlcompiler.evaluate_ast_to_python_val(
                orig_text_expr, schema=schema)
        else:
            orig_text = None

        return orig_text  # type: ignore

    @classmethod
    def command_for_ast_node(
        cls,
        astnode: qlast.DDLOperation,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> Type[Command]:
        return cls

    @classmethod
    def _modaliases_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: CommandContext,
    ) -> Dict[Optional[str], str]:
        modaliases = {}
        if isinstance(astnode, qlast.DDLCommand):
            for alias in astnode.aliases:
                if isinstance(alias, qlast.ModuleAliasDecl):
                    modaliases[alias.alias] = alias.module

        return modaliases

    @classmethod
    def localnames_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: CommandContext,
    ) -> Set[str]:
        localnames: Set[str] = set()
        if isinstance(astnode, qlast.DDLCommand):
            for alias in astnode.aliases:
                if isinstance(alias, qlast.AliasedExpr):
                    localnames.add(alias.alias)

        return localnames

    @classmethod
    def _cmd_tree_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: CommandContext,
    ) -> Command:
        cmd = cls._cmd_from_ast(schema, astnode, context)
        cmd.source_context = astnode.context
        cmd.qlast = astnode
        ctx = context.current()
        if ctx is not None and type(ctx) is cls.get_context_class():
            ctx.op = cmd

        if astnode.commands:
            for subastnode in astnode.commands:
                subcmd = compile_ddl(schema, subastnode, context=context)
                if subcmd is not None:
                    cmd.add(subcmd)

        return cmd

    @classmethod
    def _cmd_from_ast(
        cls: Type[Command_T],
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: CommandContext,
    ) -> Command:
        return cls()

    @classmethod
    def as_markup(cls, self: Command, *, ctx: markup.Context) -> markup.Markup:
        node = markup.elements.lang.TreeNode(name=str(self))

        for dd in self.get_subcommands():
            if isinstance(dd, AlterObjectProperty):
                diff = markup.elements.doc.ValueDiff(
                    before=repr(dd.old_value), after=repr(dd.new_value))

                if dd.source == 'inheritance':
                    diff.comment = 'inherited'

                node.add_child(label=dd.property, node=diff)
            else:
                node.add_child(node=markup.serialize(dd, ctx=ctx))

        return node

    @classmethod
    def get_context_class(
        cls: Type[Command_T],
    ) -> Optional[Type[CommandContextToken[Command_T]]]:
        return cast(
            Optional[Type[CommandContextToken[Command_T]]],
            cls._context_class,
        )

    @classmethod
    def get_context_class_or_die(
        cls: Type[Command_T],
    ) -> Type[CommandContextToken[Command_T]]:
        ctxcls = cls.get_context_class()
        if ctxcls is None:
            raise RuntimeError(f'context class not defined for {cls}')
        return ctxcls


# Similarly to _dummy_object, we use _dummy_command for places where
# the typing requires an object, but we don't have it just yet.
_dummy_command = Command()


CommandList = checked.CheckedList[Command]


class CommandGroup(Command):
    def apply(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        for op in self.get_subcommands():
            schema = op.apply(schema, context)
        return schema


class CommandContextToken(Generic[Command_T]):
    original_schema: s_schema.Schema
    op: Command_T
    modaliases: Mapping[Optional[str], str]
    localnames: AbstractSet[str]
    inheritance_merge: Optional[bool]
    inheritance_refdicts: Optional[AbstractSet[str]]
    mark_derived: Optional[bool]
    preserve_path_id: Optional[bool]
    enable_recursion: Optional[bool]
    transient_derivation: Optional[bool]

    def __init__(
        self,
        schema: s_schema.Schema,
        op: Command_T,
        *,
        modaliases: Optional[Mapping[Optional[str], str]] = None,
        # localnames are the names defined locally via with block or
        # as function parameters and should not be fully-qualified
        localnames: AbstractSet[str] = frozenset(),
    ) -> None:
        self.original_schema = schema
        self.op = op
        self.modaliases = modaliases if modaliases is not None else {}
        self.localnames = localnames
        self.inheritance_merge = None
        self.inheritance_refdicts = None
        self.mark_derived = None
        self.preserve_path_id = None
        self.enable_recursion = None
        self.transient_derivation = None


class CommandContextWrapper(Generic[Command_T]):
    def __init__(
        self,
        context: CommandContext,
        token: CommandContextToken[Command_T],
    ) -> None:
        self.context = context
        self.token = token

    def __enter__(self) -> CommandContextToken[Command_T]:
        self.context.push(self.token)  # type: ignore
        return self.token

    def __exit__(
        self,
        exc_type: Type[Exception],
        exc_value: Exception,
        traceback: Any,
    ) -> None:
        self.context.pop()


class CommandContext:
    def __init__(
        self,
        *,
        schema: Optional[s_schema.Schema] = None,
        modaliases: Optional[Mapping[Optional[str], str]] = None,
        localnames: AbstractSet[str] = frozenset(),
        declarative: bool = False,
        stdmode: bool = False,
        testmode: bool = False,
        disable_dep_verification: bool = False,
        descriptive_mode: bool = False,
        schema_object_ids: Optional[
            Mapping[Tuple[str, Optional[str]], uuid.UUID]
        ] = None,
        backend_superuser_role: Optional[str] = None,
        compat_ver: Optional[verutils.Version] = None,
    ) -> None:
        self.stack: List[CommandContextToken[Command]] = []
        self._cache: Dict[Hashable, Any] = {}
        self._values: Dict[Hashable, Any] = {}
        self.declarative = declarative
        self.schema = schema
        self._modaliases = modaliases if modaliases is not None else {}
        self._localnames = localnames
        self.stdmode = stdmode
        self.testmode = testmode
        self.descriptive_mode = descriptive_mode
        self.disable_dep_verification = disable_dep_verification
        self.renames: Dict[str, str] = {}
        self.renamed_objs: Set[so.Object] = set()
        self.altered_targets: Set[so.Object] = set()
        self.schema_object_ids = schema_object_ids
        self.backend_superuser_role = backend_superuser_role
        self.affected_finalization: \
            Dict[Command, Tuple[DeltaRoot, Command]] = dict()
        self.compat_ver = compat_ver

    @property
    def modaliases(self) -> Mapping[Optional[str], str]:
        maps = [t.modaliases for t in reversed(self.stack)]
        maps.append(self._modaliases)
        return collections.ChainMap(*maps)

    @property
    def localnames(self) -> Set[str]:
        ign: Set[str] = set()
        for ctx in reversed(self.stack):
            ign.update(ctx.localnames)
        ign.update(self._localnames)
        return ign

    @property
    def inheritance_merge(self) -> Optional[bool]:
        for ctx in reversed(self.stack):
            if ctx.inheritance_merge is not None:
                return ctx.inheritance_merge
        return None

    @property
    def mark_derived(self) -> Optional[bool]:
        for ctx in reversed(self.stack):
            if ctx.mark_derived is not None:
                return ctx.mark_derived
        return None

    @property
    def preserve_path_id(self) -> Optional[bool]:
        for ctx in reversed(self.stack):
            if ctx.preserve_path_id is not None:
                return ctx.preserve_path_id
        return None

    @property
    def inheritance_refdicts(self) -> Optional[AbstractSet[str]]:
        for ctx in reversed(self.stack):
            if ctx.inheritance_refdicts is not None:
                return ctx.inheritance_refdicts
        return None

    @property
    def enable_recursion(self) -> bool:
        for ctx in reversed(self.stack):
            if ctx.enable_recursion is not None:
                return ctx.enable_recursion

        return True

    @property
    def transient_derivation(self) -> bool:
        for ctx in reversed(self.stack):
            if ctx.transient_derivation is not None:
                return ctx.transient_derivation

        return False

    @property
    def canonical(self) -> bool:
        return any(ctx.op.canonical for ctx in self.stack)

    def in_deletion(self, offset: int = 0) -> bool:
        """Return True if any object is being deleted in this context.

        :param offset:
            The offset in the context stack to start looking at.

        :returns:
            True if any object is being deleted in this context starting
            from *offset* in the stack.
        """
        return any(isinstance(ctx.op, DeleteObject)
                   for ctx in self.stack[:-offset])

    def is_deleting(self, obj: so.Object) -> bool:
        """Return True if *obj* is being deleted in this context.

        :param obj:
            The object in question.

        :returns:
            True if *obj* is being deleted in this context.
        """
        return any(isinstance(ctx.op, DeleteObject)
                   and ctx.op.scls is obj for ctx in self.stack)

    def push(self, token: CommandContextToken[Command]) -> None:
        self.stack.append(token)

    def pop(self) -> CommandContextToken[Command]:
        return self.stack.pop()

    def get(
        self,
        cls: Union[Type[Command], Type[CommandContextToken[Command]]],
    ) -> Optional[CommandContextToken[Command]]:
        if issubclass(cls, Command):
            ctxcls = cls.get_context_class()
            assert ctxcls is not None
        else:
            ctxcls = cls

        for item in reversed(self.stack):
            if isinstance(item, ctxcls):
                return item

        return None

    def get_ancestor(
        self,
        cls: Union[Type[Command], Type[CommandContextToken[Command]]],
        op: Optional[Command] = None,
    ) -> Optional[CommandContextToken[Command]]:
        if issubclass(cls, Command):
            ctxcls = cls.get_context_class()
            assert ctxcls is not None
        else:
            ctxcls = cls

        if op is not None:
            for item in list(reversed(self.stack)):
                if isinstance(item, ctxcls) and item.op is not op:
                    return item
        else:
            for item in list(reversed(self.stack))[1:]:
                if isinstance(item, ctxcls):
                    return item

        return None

    def top(self) -> CommandContextToken[Command]:
        if self.stack:
            return self.stack[0]
        else:
            raise KeyError('command context stack is empty')

    def current(self) -> CommandContextToken[Command]:
        if self.stack:
            return self.stack[-1]
        else:
            raise KeyError('command context stack is empty')

    def parent(self) -> Optional[CommandContextToken[Command]]:
        if len(self.stack) > 1:
            return self.stack[-2]
        else:
            return None

    def copy(self) -> CommandContext:
        ctx = CommandContext()
        ctx.stack = self.stack[:]
        return ctx

    def at_top(self) -> CommandContext:
        ctx = CommandContext()
        ctx.stack = ctx.stack[:1]
        return ctx

    def cache_value(self, key: Hashable, value: Any) -> None:
        self._cache[key] = value

    def get_cached(self, key: Hashable) -> Any:
        return self._cache.get(key)

    def drop_cache(self, key: Hashable) -> None:
        self._cache.pop(key, None)

    def store_value(self, key: Hashable, value: Any) -> None:
        self._values[key] = value

    def get_value(self, key: Hashable) -> Any:
        return self._values.get(key)

    @contextlib.contextmanager
    def suspend_dep_verification(self) -> Iterator[CommandContext]:
        dep_ver = self.disable_dep_verification
        self.disable_dep_verification = True
        try:
            yield self
        finally:
            self.disable_dep_verification = dep_ver

    def __call__(
        self,
        token: CommandContextToken[Command_T],
    ) -> CommandContextWrapper[Command_T]:
        return CommandContextWrapper(self, token)


class DeltaRootContext(CommandContextToken["DeltaRoot"]):
    pass


class DeltaRoot(CommandGroup, context_class=DeltaRootContext):

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.new_types: Set[uuid.UUID] = set()

    def apply(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        from . import modules
        from . import types as s_types

        context = context or CommandContext()

        with context(DeltaRootContext(schema=schema, op=self)):
            mods = []

            for cmop in self.get_subcommands(type=modules.CreateModule):
                schema = cmop.apply(schema, context)
                mods.append(cmop.scls)

            for amop in self.get_subcommands(type=modules.AlterModule):
                schema = amop.apply(schema, context)
                mods.append(amop.scls)

            for objop in self.get_subcommands():
                if not isinstance(objop, (modules.CreateModule,
                                          modules.AlterModule,
                                          s_types.DeleteCollectionType)):
                    schema = objop.apply(schema, context)

            for cop in self.get_subcommands(type=s_types.DeleteCollectionType):
                schema = cop.apply(schema, context)

        return schema


class ObjectCommandMeta(CommandMeta):
    _transparent_adapter_subclass: ClassVar[bool] = True
    _schema_metaclasses: ClassVar[
        Dict[Tuple[str, Type[so.Object]], Type[ObjectCommand[so.Object]]]
    ] = {}

    def __new__(
        mcls,
        name: str,
        bases: Tuple[type, ...],
        dct: Dict[str, Any],
        *,
        schema_metaclass: Optional[Type[so.Object]] = None,
        **kwargs: Any,
    ) -> ObjectCommandMeta:
        cls = cast(
            Type["ObjectCommand[so.Object]"],
            super().__new__(mcls, name, bases, dct, **kwargs),
        )
        if cls.has_adaptee():
            # This is a command adapter rather than the actual
            # command, so skip the registrations.
            return cls

        if (schema_metaclass is not None or
                not hasattr(cls, '_schema_metaclass')):
            cls._schema_metaclass = schema_metaclass

        delta_action = getattr(cls, '_delta_action', None)
        if cls._schema_metaclass is not None and delta_action is not None:
            key = delta_action, cls._schema_metaclass
            cmdcls = mcls._schema_metaclasses.get(key)
            if cmdcls is not None:
                raise TypeError(
                    f'Action {cls._delta_action!r} for '
                    f'{cls._schema_metaclass} is already claimed by {cmdcls}'
                )
            mcls._schema_metaclasses[key] = cls

        return cls

    @classmethod
    def get_command_class(
        mcls,
        cmdtype: Type[Command_T],
        schema_metaclass: Type[so.Object],
    ) -> Optional[Type[Command_T]]:
        assert issubclass(cmdtype, ObjectCommand)
        return cast(
            Optional[Type[Command_T]],
            mcls._schema_metaclasses.get(
                (cmdtype._delta_action, schema_metaclass)),
        )

    @classmethod
    def get_command_class_or_die(
        mcls,
        cmdtype: Type[Command_T],
        schema_metaclass: Type[so.Object],
    ) -> Type[Command_T]:
        cmdcls = mcls.get_command_class(cmdtype, schema_metaclass)
        if cmdcls is None:
            raise TypeError(f'missing {cmdtype.__name__} implementation '
                            f'for {schema_metaclass.__name__}')
        return cmdcls


ObjectCommand_T = TypeVar("ObjectCommand_T", bound='ObjectCommand[so.Object]')


class ObjectCommand(
    Command,
    Generic[so.Object_T],
    metaclass=ObjectCommandMeta,
):
    """Base class for all Object-related commands."""

    #: Full name of the object this command operates on.
    classname = struct.Field(str)
    #: An optional set of values neceessary to render the command in DDL.
    ddl_identity = struct.Field(
        dict,  # type: ignore
        default=None,
    )
    #: An optional dict of metadata annotations for this command.
    annotations = struct.Field(
        dict,  # type: ignore
        default=None,
    )

    scls: so.Object_T
    _delta_action: ClassVar[str]
    _schema_metaclass: ClassVar[Optional[Type[so.Object_T]]]
    astnode: ClassVar[Union[Type[qlast.DDLOperation],
                            List[Type[qlast.DDLOperation]]]]

    @classmethod
    def _classname_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.NamedDDL,
        context: CommandContext,
    ) -> str:
        return astnode.name.name

    @classmethod
    def _cmd_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: CommandContext,
    ) -> ObjectCommand[so.Object_T]:
        assert isinstance(astnode, qlast.ObjectDDL), 'expected ObjectDDL'
        classname = cls._classname_from_ast(schema, astnode, context)
        return cls(classname=classname)

    def get_parent_op(
        self,
        context: CommandContext,
    ) -> ObjectCommand[so.Object]:
        parent = context.parent()
        if parent is None:
            raise AssertionError(f'{self!r} has no parent context')
        op = parent.op
        assert isinstance(op, ObjectCommand)
        return op

    def _propagate_if_expr_refs(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        action: str,
    ) -> Tuple[s_schema.Schema, List[qlast.DDLCommand]]:
        scls = self.scls
        expr_refs = s_expr.get_expr_referrers(schema, scls)
        # Commands to be executed after the original change is
        # complete
        finalize_ast: List[qlast.DDLCommand] = []

        if expr_refs:
            ref_desc = []
            for ref, fn in expr_refs.items():
                from . import functions as s_func
                from . import indexes as s_indexes
                from . import pointers as s_pointers

                cmd_drop: Command
                cmd_create: Command

                if isinstance(ref, s_indexes.Index):
                    # If the affected entity is an index, just drop it and
                    # schedule it to be re-created.
                    create_root, create_parent = ref.init_parent_delta_branch(
                        schema)
                    create_cmd = ref.init_delta_command(schema, CreateObject)
                    for fname in type(ref).get_fields():
                        value = ref.get_explicit_field_value(
                            schema, fname, None)
                        if value is not None:
                            create_cmd.set_attribute_value(fname, value)
                    create_parent.add(create_cmd)
                    context.affected_finalization[self] = (
                        create_root, create_cmd)
                    schema = ref.delete(schema)
                    continue

                elif isinstance(ref, s_pointers.Pointer):
                    # If the affected entity is a pointer, drop/create
                    # the default value or computable.
                    delta_drop, cmd_drop = ref.init_delta_branch(
                        schema, cmdtype=AlterObject)
                    delta_create, cmd_create = ref.init_delta_branch(
                        schema, cmdtype=AlterObject)

                    # Copy own fields into the create command.
                    value = ref.get_explicit_field_value(schema, fn, None)
                    cmd_drop.set_attribute_value(fn, None)
                    cmd_create.set_attribute_value(fn, value)

                    context.affected_finalization[self] = (
                        delta_create, cmd_create
                    )
                    schema = delta_drop.apply(schema, context)
                    continue

                elif isinstance(ref, s_func.Function):
                    # If the affected entity is a function, alter it
                    # to change the body to a dummy version (removing
                    # the dependency) and then reset the body to
                    # original expression.
                    delta_drop, cmd_drop = ref.init_delta_branch(
                        schema, cmdtype=AlterObject)
                    delta_create, cmd_create = ref.init_delta_branch(
                        schema, cmdtype=AlterObject)

                    # Copy own fields into the create command.
                    value = ref.get_explicit_field_value(schema, fn, None)
                    cmd_drop.set_attribute_value(
                        fn, ref.get_dummy_body(schema))
                    cmd_create.set_attribute_value(fn, value)

                    context.affected_finalization[self] = (
                        delta_create, cmd_create
                    )
                    schema = delta_drop.apply(schema, context)
                    continue

                if fn == 'expr':
                    fdesc = 'expression'
                else:
                    fdesc = f"{fn.replace('_', ' ')} expression"

                vn = ref.get_verbosename(schema, with_parent=True)

                ref_desc.append(f'{fdesc} of {vn}')

            if ref_desc:
                expr_s = (
                    'an expression' if len(ref_desc) == 1 else 'expressions')
                ref_desc_s = "\n - " + "\n - ".join(ref_desc)

                raise errors.SchemaDefinitionError(
                    f'cannot {action} because it is used in {expr_s}',
                    details=(
                        f'{scls.get_verbosename(schema)} is used in:'
                        f'{ref_desc_s}'
                    )
                )

        return schema, finalize_ast

    def _finalize_affected_refs(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        delta, cmd = context.affected_finalization.get(self, (None, None))
        if delta is not None:
            from . import lproperties as s_props
            from . import links as s_links
            from . import pointers as s_pointers
            from . import types as s_types
            from edb.ir import ast as irast

            # if the delta involves re-setting a computable
            # expression, then we also need to change the type to the
            # new expression type
            if (isinstance(cmd, (s_props.AlterProperty, s_links.AlterLink))):
                for cm in cmd.get_subcommands(type=AlterObjectProperty):
                    if cm.property == 'expr':
                        assert isinstance(cm.new_value, s_expr.Expression)
                        pointer = cast(
                            s_pointers.Pointer, schema.get(cmd.classname))
                        source = cast(s_types.Type, pointer.get_source(schema))
                        expression = s_expr.Expression.compiled(
                            cm.new_value,
                            schema=schema,
                            options=qlcompiler.CompilerOptions(
                                modaliases=context.modaliases,
                                anchors={qlast.Source().name: source},
                                path_prefix_anchor=qlast.Source().name,
                                singletons=frozenset([source]),
                            ),
                        )

                        assert isinstance(expression.irast, irast.Statement)
                        target = expression.irast.stype
                        cmd.set_attribute_value('target', target)
                        break

            schema = delta.apply(schema, context)

        return schema

    def _append_subcmd_ast(
        self,
        schema: s_schema.Schema,
        node: qlast.DDLOperation,
        subcmd: Command,
        context: CommandContext,
    ) -> None:
        subnode = subcmd.get_ast(schema, context, parent_node=node)
        if subnode is not None:
            node.commands.append(subnode)

    def _get_ast_node(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> Type[qlast.DDLOperation]:
        # TODO: how to handle the following type: ignore?
        # in this class, astnode is always a Type[DDLOperation],
        # but the current design of constraints handles it as
        # a List[Type[DDLOperation]]
        return type(self).astnode  # type: ignore

    def _deparse_name(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        name: str,
    ) -> qlast.ObjectRef:
        qlclass = self.get_schema_metaclass().get_ql_class()

        if isinstance(name, sn.Name):
            nname = sn.shortname_from_fullname(name)
            ref = qlast.ObjectRef(
                module=nname.module, name=nname.name, itemclass=qlclass)
        else:
            ref = qlast.ObjectRef(module='', name=name, itemclass=qlclass)

        return ref

    def _get_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        astnode = self._get_ast_node(schema, context)

        if astnode.get_field('name'):
            op = astnode(
                name=self._deparse_name(schema, context, self.classname),
            )
        else:
            op = astnode()

        self._apply_fields_ast(schema, context, op)

        return op

    def _apply_fields_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        node: qlast.DDLOperation,
    ) -> None:
        for op in self.get_subcommands(type=AlterObjectFragment):
            self._append_subcmd_ast(schema, node, op, context)

        mcls = self.get_schema_metaclass()

        if not isinstance(self, DeleteObject):
            fops = self.get_subcommands(type=AlterObjectProperty)
            for fop in sorted(fops, key=lambda f: f.property):
                field = mcls.get_field(fop.property)
                if fop.new_value is not None:
                    new_value = fop.new_value
                else:
                    new_value = field.get_default()

                if (
                    (fop.source != 'inheritance' or context.descriptive_mode)
                    and fop.old_value != new_value
                ):
                    self._apply_field_ast(schema, context, node, fop)

        if not isinstance(self, AlterObjectFragment):
            for field in self.get_ddl_identity_fields(context):
                if (
                    issubclass(field.type, s_expr.Expression)
                    and mcls.has_field(f'orig_{field.name}')
                    and not qlast.get_ddl_field_value(
                        node, f'orig_{field.name}'
                    )
                ):
                    expr = self.get_ddl_identity(field.name)
                    if expr.origtext != expr.text:
                        node.commands.append(
                            qlast.SetField(
                                name=f'orig_{field.name}',
                                value=qlast.StringConstant.from_python(
                                    expr.origtext,
                                ),
                            ),
                        )

                ast_attr = self.get_ast_attr_for_field(field.name)
                if (
                    ast_attr is not None
                    and not getattr(node, ast_attr, None)
                    and (
                        field.required
                        or self.has_ddl_identity(field.name)
                    )
                ):
                    ddl_id = self.get_ddl_identity(field.name)
                    if issubclass(field.type, s_expr.Expression):
                        attr_val = ddl_id.qlast
                    elif issubclass(field.type, s_expr.ExpressionList):
                        attr_val = [e.qlast for e in ddl_id]
                    else:
                        raise AssertionError(
                            f'unexpected type of ddl_identity'
                            f' field: {field.type!r}'
                        )

                    setattr(node, ast_attr, attr_val)

            for refdict in mcls.get_refdicts():
                self._apply_refs_fields_ast(schema, context, node, refdict)

    def _apply_refs_fields_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        node: qlast.DDLOperation,
        refdict: so.RefDict,
    ) -> None:
        for op in self.get_subcommands(metaclass=refdict.ref_cls):
            self._append_subcmd_ast(schema, node, op, context)

    def _apply_field_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        node: qlast.DDLOperation,
        op: AlterObjectProperty,
    ) -> None:
        if op.property != 'name':
            subnode = op._get_ast(schema, context, parent_node=node)
            if subnode is not None:
                node.commands.append(subnode)

    def get_ast_attr_for_field(self, field: str) -> Optional[str]:
        return None

    def get_ddl_identity_fields(
        self,
        context: CommandContext,
    ) -> Tuple[so.Field[Any], ...]:
        mcls = self.get_schema_metaclass()
        return tuple(f for f in mcls.get_fields().values() if f.ddl_identity)

    @classmethod
    def maybe_get_schema_metaclass(cls) -> Optional[Type[so.Object_T]]:
        return cls._schema_metaclass

    @classmethod
    def get_schema_metaclass(cls) -> Type[so.Object_T]:
        if cls._schema_metaclass is None:
            raise TypeError(f'schema metaclass not set for {cls}')
        return cls._schema_metaclass

    @classmethod
    def get_other_command_class(
        cls,
        cmdtype: Type[ObjectCommand_T],
    ) -> Type[ObjectCommand_T]:
        mcls = cls.get_schema_metaclass()
        return ObjectCommandMeta.get_command_class_or_die(cmdtype, mcls)

    def _validate_legal_command(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> None:
        from . import functions as s_func
        from . import modules as s_mod

        if (not context.stdmode and not context.testmode and
                not isinstance(self, s_func.ParameterCommand)):

            shortname: str
            modname: Optional[str]
            if isinstance(self.classname, sn.Name):
                shortname = sn.shortname_from_fullname(self.classname)
                modname = self.classname.module
            elif issubclass(self.get_schema_metaclass(), s_mod.Module):
                # modules have classname as simple strings
                shortname = modname = self.classname
            else:
                modname = None

            if modname is not None and modname in s_schema.STD_MODULES:
                raise errors.SchemaDefinitionError(
                    f'cannot {self._delta_action} `{shortname}`: '
                    f'module {modname} is read-only',
                    context=self.source_context)

    def get_verbosename(self) -> str:
        mcls = self.get_schema_metaclass()
        return mcls.get_verbosename_static(self.classname)

    def get_displayname(self) -> str:
        mcls = self.get_schema_metaclass()
        return mcls.get_displayname_static(self.classname)

    @overload
    def get_object(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        name: Optional[str] = None,
        default: Union[so.Object_T, so.NoDefaultT] = so.NoDefault,
    ) -> so.Object_T:
        ...

    @overload
    def get_object(  # NoQA: F811
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        name: Optional[str] = None,
        default: None = None,
    ) -> Optional[so.Object_T]:
        ...

    def get_object(  # NoQA: F811
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        name: Optional[str] = None,
        default: Union[so.Object_T, so.NoDefaultT, None] = so.NoDefault,
    ) -> Optional[so.Object_T]:
        metaclass = self.get_schema_metaclass()
        if name is None:
            name = self.classname
            rename = context.renames.get(name)
            if rename is not None:
                name = rename
        return schema.get_global(metaclass, name, default=default)

    def canonicalize_attributes(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        """Resolve, canonicalize and amend field mutations in this command.

        This is called just before the object described by this command
        is created or updated but after all prerequisite command have
        been applied, so it is safe to resolve object shells and do
        other schema inquiries here.
        """
        return schema

    def populate_ddl_identity(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        return schema

    def get_resolved_attribute_value(
        self,
        attr_name: str,
        *,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> Any:
        raw_value = self.get_attribute_value(attr_name)
        if raw_value is None:
            return None

        value = context.get_cached((self, 'attribute', attr_name))
        if value is None:
            metaclass = self.get_schema_metaclass()
            field = metaclass.get_field(attr_name)
            if field is None:
                raise errors.SchemaDefinitionError(
                    f'got AlterObjectProperty command for '
                    f'invalid field: {metaclass.__name__}.{attr_name}')

            value = self._resolve_attr_value(
                raw_value, attr_name, field, schema)

            if (isinstance(value, s_expr.Expression)
                    and not value.is_compiled()):
                value = self.compile_expr_field(schema, context, field, value)

            context.cache_value((self, 'attribute', attr_name), value)

        return value

    def get_attributes(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> Dict[str, Any]:
        result = {}

        for attr in self.enumerate_attributes():
            result[attr] = self.get_attribute_value(attr)

        return result

    def get_resolved_attributes(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> Dict[str, Any]:
        result = {}

        for attr in self.enumerate_attributes():
            result[attr] = self.get_resolved_attribute_value(
                attr, schema=schema, context=context)

        return result

    def get_orig_attributes(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> Dict[str, Any]:
        result = {}

        for attr in self.enumerate_attributes():
            result[attr] = self.get_orig_attribute_value(attr)

        return result

    def compile_expr_field(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        field: so.Field[Any],
        value: Any,
    ) -> s_expr.Expression:
        cdn = self.get_schema_metaclass().get_schema_class_displayname()
        raise errors.InternalServerError(
            f'uncompiled expression in the field {field.name!r} of '
            f'{cdn} {self.classname!r}'
        )

    def _create_begin(
        self, schema: s_schema.Schema, context: CommandContext
    ) -> s_schema.Schema:
        raise NotImplementedError

    def new_context(
        self: ObjectCommand[so.Object_T],
        schema: s_schema.Schema,
        context: CommandContext,
        scls: so.Object_T,
    ) -> CommandContextWrapper[ObjectCommand[so.Object_T]]:
        ctxcls = type(self).get_context_class()
        assert ctxcls is not None
        obj_ctxcls = cast(
            Type[ObjectCommandContext[so.Object_T]],
            ctxcls,
        )
        return context(obj_ctxcls(schema=schema, op=self, scls=scls))

    def get_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        dummy = cast(so.Object_T, _dummy_object)

        context_class = type(self).get_context_class()
        if context_class is not None:
            with self.new_context(schema, context, dummy):
                return self._get_ast(schema, context, parent_node=parent_node)
        else:
            return self._get_ast(schema, context, parent_node=parent_node)

    def get_ddl_identity(self, aspect: str) -> Any:
        if self.ddl_identity is None:
            raise LookupError(f'{self!r} has no DDL identity information')
        value = self.ddl_identity.get(aspect)
        if value is None:
            raise LookupError(f'{self!r} has no {aspect!r} in DDL identity')
        return value

    def has_ddl_identity(self, aspect: str) -> bool:
        return (
            self.ddl_identity is not None
            and self.ddl_identity.get(aspect) is not None
        )

    def set_ddl_identity(self, aspect: str, value: Any) -> None:
        if self.ddl_identity is None:
            self.ddl_identity = {}

        self.ddl_identity[aspect] = value

    def get_annotation(self, name: str) -> Any:
        if self.annotations is None:
            return None
        else:
            return self.annotations.get(name)

    def set_annotation(self, name: str, value: Any) -> None:
        if self.annotations is None:
            self.annotations = {}
        self.annotations[name] = value


class ObjectCommandContext(CommandContextToken[ObjectCommand[so.Object_T]]):

    def __init__(
        self,
        schema: s_schema.Schema,
        op: ObjectCommand[so.Object_T],
        scls: so.Object_T,
        *,
        modaliases: Optional[Mapping[Optional[str], str]] = None,
        localnames: AbstractSet[str] = frozenset(),
    ) -> None:
        super().__init__(
            schema, op, modaliases=modaliases, localnames=localnames)
        self.scls = scls


class QualifiedObjectCommand(ObjectCommand[so.QualifiedObject_T]):

    classname = struct.Field(sn.Name)

    @classmethod
    def _classname_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.NamedDDL,
        context: CommandContext,
    ) -> sn.Name:
        objref = astnode.name
        module = context.modaliases.get(objref.module, objref.module)
        if module is None:
            raise errors.SchemaDefinitionError(
                f'unqualified name and no default module set',
                context=objref.context,
            )

        return sn.Name(module=module, name=objref.name)

    @overload
    def get_object(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        name: Optional[str] = None,
        default: Union[so.QualifiedObject_T, so.NoDefaultT] = so.NoDefault,
    ) -> so.QualifiedObject_T:
        ...

    @overload
    def get_object(  # NoQA: F811
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        name: Optional[str] = None,
        default: None = None,
    ) -> Optional[so.QualifiedObject_T]:
        ...

    def get_object(  # NoQA: F811
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        name: Optional[str] = None,
        default: Union[
            so.QualifiedObject_T, so.NoDefaultT, None] = so.NoDefault,
    ) -> Optional[so.QualifiedObject_T]:
        if name is None:
            name = self.classname
            rename = context.renames.get(name)
            if rename is not None:
                name = rename
        metaclass = self.get_schema_metaclass()
        return cast(
            Optional[so.QualifiedObject_T],
            schema.get(name, type=metaclass, default=default,
                       sourcectx=self.source_context),
        )


class GlobalObjectCommand(ObjectCommand[so.GlobalObject]):
    pass


class CreateObject(ObjectCommand[so.Object_T], Generic[so.Object_T]):
    _delta_action = 'create'

    # If the command is conditioned with IF NOT EXISTS
    if_not_exists = struct.Field(bool, default=False)

    @classmethod
    def command_for_ast_node(
        cls,
        astnode: qlast.DDLOperation,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> Type[ObjectCommand[so.Object_T]]:
        assert isinstance(astnode, qlast.CreateObject), "expected CreateObject"

        if astnode.sdl_alter_if_exists:
            modaliases = cls._modaliases_from_ast(schema, astnode, context)
            dummy_op = cls(classname=sn.Name('placeholder::placeholder'))
            ctxcls = cast(
                Type[ObjectCommandContext[so.Object_T]],
                cls.get_context_class_or_die(),
            )
            ctx = ctxcls(
                schema,
                op=dummy_op,
                scls=cast(so.Object_T, _dummy_object),
                modaliases=modaliases,
            )
            with context(ctx):
                classname = cls._classname_from_ast(schema, astnode, context)
            mcls = cls.get_schema_metaclass()
            if schema.get(classname, default=None) is not None:
                return ObjectCommandMeta.get_command_class_or_die(
                    AlterObject, mcls)

        return cls

    @classmethod
    def _cmd_tree_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: CommandContext,
    ) -> Command:
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)
        assert isinstance(astnode, qlast.CreateObject)
        assert isinstance(cmd, CreateObject)

        cmd.if_not_exists = astnode.create_if_not_exists

        cmd.set_attribute_value('name', cmd.classname)

        if getattr(astnode, 'is_abstract', False):
            cmd.set_attribute_value('is_abstract', True)

        return cmd

    def validate_create(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> None:
        pass

    def _create_begin(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        self._validate_legal_command(schema, context)

        for op in self.get_prerequisites():
            schema = op.apply(schema, context)

        if context.schema_object_ids is not None:
            mcls = self.get_schema_metaclass()
            qlclass: Optional[qltypes.SchemaObjectClass]
            if issubclass(mcls, so.QualifiedObject):
                qlclass = None
            else:
                qlclass = mcls.get_ql_class_or_die()

            objname = self.classname
            if (
                context.compat_ver is not None
                and (
                    context.compat_ver
                    < (1, 0, verutils.VersionStage.ALPHA, 5)
                )
            ):
                # Pre alpha.5 used to have a different name mangling scheme.
                objname = sn.compat_name_remangle(objname)

            key = (objname, qlclass)
            specified_id = context.schema_object_ids.get(key)
            if specified_id is not None:
                self.set_attribute_value('id', specified_id)

        if not context.canonical:
            schema = self.populate_ddl_identity(schema, context)
            schema = self.canonicalize_attributes(schema, context)
            self.validate_create(schema, context)

        props = self.get_resolved_attributes(schema, context)
        metaclass = self.get_schema_metaclass()
        schema, self.scls = metaclass.create_in_schema(schema, **props)

        if not props.get('id'):
            # Record the generated ID.
            self.set_attribute_value('id', self.scls.id)

        return schema

    def canonicalize_attributes(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        schema = super().canonicalize_attributes(schema, context)
        self.set_attribute_value('builtin', context.stdmode)
        return schema

    def _get_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        node = super()._get_ast(schema, context, parent_node=parent_node)
        if node is not None and self.if_not_exists:
            node.create_if_not_exists = True
        return node

    def _create_innards(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        for op in self.get_subcommands(include_prerequisites=False):
            schema = op.apply(schema, context=context)
        return schema

    def _create_finalize(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        return schema

    def apply(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        dummy = cast(so.Object_T, _dummy_object)
        with self.new_context(schema, context, dummy):
            if self.if_not_exists:
                scls = self.get_object(schema, context, default=None)

                if scls is not None:
                    parent_ctx = context.parent()
                    if parent_ctx is not None and not self.canonical:
                        parent_ctx.op.discard(self)

                    self.scls = scls
                    return schema

            schema = self._create_begin(schema, context)
            ctx = context.current()
            objctx = cast(ObjectCommandContext[so.Object_T], ctx)
            objctx.scls = self.scls
            schema = self._create_innards(schema, context)
            schema = self._create_finalize(schema, context)
        return schema


class AlterObjectFragment(ObjectCommand[so.Object_T]):

    def apply(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        # AlterObjectFragment must be executed in the context
        # of a parent AlterObject command.
        scls = self.get_parent_op(context).scls
        self.scls = cast(so.Object_T, scls)

        schema = self._alter_begin(schema, context)
        schema = self._alter_innards(schema, context)
        schema = self._alter_finalize(schema, context)

        return schema

    def get_parent_op(
        self,
        context: CommandContext,
    ) -> ObjectCommand[so.Object]:
        op = context.current().op
        assert isinstance(op, ObjectCommand)
        return op

    def _alter_begin(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        for op in self.get_prerequisites():
            schema = op.apply(schema, context)

        if not context.canonical:
            schema = self.populate_ddl_identity(schema, context)
            schema = self.canonicalize_attributes(schema, context)

        props = self.get_resolved_attributes(schema, context)
        return self.scls.update(schema, props)

    def _alter_innards(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        for op in self.get_subcommands(include_prerequisites=False):
            if not isinstance(op, AlterObjectProperty):
                schema = op.apply(schema, context=context)
        return schema

    def _alter_finalize(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        schema = self._finalize_affected_refs(schema, context)
        return schema


class RenameObject(AlterObjectFragment[so.Object_T]):
    _delta_action = 'rename'

    astnode = qlast.Rename

    new_name = struct.Field(str)

    def _rename_begin(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        self._validate_legal_command(schema, context)
        scls = self.scls

        # Renames of schema objects used in expressions is
        # not supported yet.  Eventually we'll add support
        # for transparent recompilation.
        vn = scls.get_verbosename(schema)
        schema, finalize_ast = self._propagate_if_expr_refs(
            schema, context, action=f'rename {vn}')

        if not context.canonical:
            self.set_attribute_value(
                'name',
                value=self.new_name,
                orig_value=self.classname,
            )

            if not context.get_value(('renamecanon', self)):
                commands = self._canonicalize(schema, context, self.scls)
                self.update(commands)

        for op in self.get_prerequisites():
            schema = op.apply(schema, context)

        self.old_name = self.classname
        schema = scls.set_field_value(schema, 'name', self.new_name)

        return schema

    def _rename_innards(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        for op in self.get_subcommands(include_prerequisites=False):
            if not isinstance(op, (AlterObjectFragment, AlterObjectProperty)):
                schema = op.apply(schema, context=context)
        return schema

    def _rename_finalize(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        schema = self._finalize_affected_refs(schema, context)
        return schema

    def apply(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        parent_op = self.get_parent_op(context)
        scls = self.scls = cast(so.Object_T, parent_op.scls)

        context.renames[self.classname] = self.new_name
        context.renamed_objs.add(scls)

        schema = self._rename_begin(schema, context)
        schema = self._rename_innards(schema, context)
        schema = self._rename_finalize(schema, context)

        return schema

    def _canonicalize(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        scls: so.Object,
    ) -> Sequence[Command]:
        mcls = self.get_schema_metaclass()
        commands = []

        for refdict in mcls.get_refdicts():
            all_refs = set(
                scls.get_field_value(schema, refdict.attr).objects(schema)
            )

            for ref in all_refs:
                alter = ref.init_delta_command(schema, AlterObject)
                ref_name = ref.get_name(schema)
                quals = list(sn.quals_from_fullname(ref_name))
                quals[0] = self.new_name
                shortname = sn.shortname_from_fullname(ref_name)
                new_ref_name = sn.Name(
                    name=sn.get_specialized_name(shortname, *quals),
                    module=ref_name.module,
                )
                rename = ref.init_delta_command(
                    schema,
                    RenameObject,
                    new_name=new_ref_name,
                )
                rename.set_attribute_value(
                    'name',
                    value=new_ref_name,
                    orig_value=ref_name,
                )
                with alter.new_context(schema, context, ref):
                    rename.update(rename._canonicalize(schema, context, ref))
                alter.canonical = True
                alter.add(rename)
                commands.append(alter)

        # Record the fact that RenameObject._canonicalize
        # was called on this object to guard against possible
        # duplicate calls.
        context.store_value(('renamecanon', self), True)

        return commands

    def _get_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        astnode = self._get_ast_node(schema, context)
        ref = self._deparse_name(schema, context, self.new_name)
        ref.itemclass = None
        orig_ref = self._deparse_name(schema, context, self.classname)
        if (orig_ref.module, orig_ref.name) != (ref.module, ref.name):
            return astnode(new_name=ref)
        else:
            return None

    @classmethod
    def _cmd_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: CommandContext,
    ) -> RenameObject[so.Object_T]:
        parent_ctx = context.current()
        parent_op = parent_ctx.op
        assert isinstance(parent_op, ObjectCommand)
        parent_class = parent_op.get_schema_metaclass()
        rename_class = ObjectCommandMeta.get_command_class_or_die(
            RenameObject, parent_class)
        return rename_class._rename_cmd_from_ast(schema, astnode, context)

    @classmethod
    def _rename_cmd_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: CommandContext,
    ) -> RenameObject[so.Object_T]:
        assert isinstance(astnode, qlast.Rename)

        parent_ctx = context.current()
        parent_op = parent_ctx.op
        assert isinstance(parent_op, ObjectCommand)
        parent_class = parent_op.get_schema_metaclass()
        rename_class = ObjectCommandMeta.get_command_class_or_die(
            RenameObject, parent_class)

        new_name = cls._classname_from_ast(schema, astnode, context)
        return rename_class(
            metaclass=parent_class,
            classname=parent_op.classname,
            new_name=new_name,
        )


class RenameQualifiedObject(AlterObjectFragment[so.Object_T]):

    new_name = struct.Field(sn.Name)

    def _get_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        astnode = self._get_ast_node(schema, context)
        new_name = self.new_name
        ref = qlast.ObjectRef(name=new_name.name, module=new_name.module)
        return astnode(new_name=ref)


class AlterObject(ObjectCommand[so.Object_T], Generic[so.Object_T]):
    _delta_action = 'alter'

    #: If True, apply the command only if the object exists.
    if_exists = struct.Field(bool, default=False)

    @classmethod
    def _cmd_tree_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: CommandContext,
    ) -> Command:
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)
        assert isinstance(cmd, AlterObject)

        if getattr(astnode, 'is_abstract', False):
            cmd.set_attribute_value('is_abstract', True)

        added_bases = []
        dropped_bases: List[so.ObjectShell] = []

        if getattr(astnode, 'commands', None):
            for astcmd in astnode.commands:
                if isinstance(astcmd, qlast.AlterDropInherit):
                    dropped_bases.extend(
                        utils.ast_to_object_shell(
                            b,
                            metaclass=cls.get_schema_metaclass(),
                            modaliases=context.modaliases,
                            schema=schema,
                        )
                        for b in astcmd.bases
                    )

                elif isinstance(astcmd, qlast.AlterAddInherit):
                    bases = [
                        utils.ast_to_object_shell(
                            b,
                            metaclass=cls.get_schema_metaclass(),
                            modaliases=context.modaliases,
                            schema=schema,
                        )
                        for b in astcmd.bases
                    ]

                    pos_node = astcmd.position
                    pos: Optional[Union[str, Tuple[str, so.ObjectShell]]]
                    if pos_node is not None:
                        if pos_node.ref is not None:
                            ref = so.ObjectShell(
                                name=(
                                    f'{pos_node.ref.module}::'
                                    f'{pos_node.ref.name}'
                                ),
                                schemaclass=cls.get_schema_metaclass(),
                            )
                            pos = (pos_node.position, ref)
                        else:
                            pos = pos_node.position
                    else:
                        pos = None

                    added_bases.append((bases, pos))

        if added_bases or dropped_bases:
            from . import inheriting

            parent_class = cmd.get_schema_metaclass()
            rebase_class = ObjectCommandMeta.get_command_class_or_die(
                inheriting.RebaseInheritingObject, parent_class)

            cmd.add(
                rebase_class(
                    metaclass=parent_class,
                    classname=cmd.classname,
                    removed_bases=tuple(dropped_bases),
                    added_bases=tuple(added_bases)
                )
            )

        return cmd

    def _get_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        node = super()._get_ast(schema, context, parent_node=parent_node)
        if (node is not None and hasattr(node, 'commands') and
                not node.commands):
            # Alter node without subcommands.  Occurs when all
            # subcommands have been filtered out of DDL stream,
            # so filter it out as well.
            node = None
        return node

    def _alter_begin(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        self._validate_legal_command(schema, context)

        for op in self.get_prerequisites():
            schema = op.apply(schema, context)

        for op in self.get_subcommands(type=AlterObjectFragment):
            schema = op.apply(schema, context)

        if not context.canonical:
            schema = self.populate_ddl_identity(schema, context)
            schema = self.canonicalize_attributes(schema, context)
            self.validate_alter(schema, context)

        props = self.get_resolved_attributes(schema, context)
        schema = self.scls.update(schema, props)
        return schema

    def _alter_innards(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        for op in self.get_subcommands(include_prerequisites=False):
            if not isinstance(op, (AlterObjectFragment, AlterObjectProperty)):
                schema = op.apply(schema, context=context)

        return schema

    def _alter_finalize(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        schema = self._finalize_affected_refs(schema, context)
        return schema

    def validate_alter(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> None:
        pass

    def apply(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:

        if not context.canonical and self.if_exists:
            scls = self.get_object(schema, context, default=None)
            if scls is None:
                context.current().op.discard(self)
                return schema
        else:
            scls = self.get_object(schema, context)

        self.scls = scls

        with self.new_context(schema, context, scls):
            schema = self._alter_begin(schema, context)
            schema = self._alter_innards(schema, context)
            schema = self._alter_finalize(schema, context)

        return schema


class DeleteObject(ObjectCommand[so.Object_T], Generic[so.Object_T]):
    _delta_action = 'delete'

    #: If True, apply the command only if the object has no referrers
    #: in the schema.
    if_unused = struct.Field(bool, default=False)

    def _delete_begin(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        from . import ordering

        self._validate_legal_command(schema, context)

        if not context.canonical:
            schema = self.populate_ddl_identity(schema, context)
            schema = self.canonicalize_attributes(schema, context)

            if not context.get_value(('delcanon', self)):
                commands = self._canonicalize(schema, context, self.scls)
                root = DeltaRoot()
                root.update(commands)
                root = ordering.linearize_delta(root, schema, schema)
                self.update(root.get_subcommands())

        return schema

    def _canonicalize(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        scls: so.Object,
    ) -> Sequence[Command]:
        mcls = self.get_schema_metaclass()
        commands = []

        for refdict in mcls.get_refdicts():
            deleted_refs = set()

            all_refs = set(
                scls.get_field_value(schema, refdict.attr).objects(schema)
            )

            refcmds = cast(
                Tuple[ObjectCommand[so.Object], ...],
                self.get_subcommands(metaclass=refdict.ref_cls),
            )

            for op in refcmds:
                deleted_ref: so.Object = schema.get(op.classname)
                deleted_refs.add(deleted_ref)

            # Add implicit Delete commands for any local refs not
            # deleted explicitly.
            for ref in all_refs - deleted_refs:
                op = ref.init_delta_command(schema, DeleteObject)
                assert isinstance(op, DeleteObject)
                subcmds = op._canonicalize(schema, context, ref)
                op.update(subcmds)
                commands.append(op)

        # Record the fact that DeleteObject._canonicalize
        # was called on this object to guard against possible
        # duplicate calls.
        context.store_value(('delcanon', self), True)

        return commands

    def _delete_innards(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        for op in self.get_subcommands(metaclass=so.Object):
            schema = op.apply(schema, context=context)

        return schema

    def _delete_finalize(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        ref_strs = []

        if not context.canonical and not context.disable_dep_verification:
            refs = schema.get_referrers(self.scls)
            ctx = context.current()
            assert ctx is not None
            orig_schema = ctx.original_schema
            if refs:
                for ref in refs:
                    if (not context.is_deleting(ref)
                            and ref.is_blocking_ref(orig_schema, self.scls)):
                        ref_strs.append(
                            ref.get_verbosename(orig_schema, with_parent=True))

            if ref_strs:
                vn = self.scls.get_verbosename(orig_schema, with_parent=True)
                dn = self.scls.get_displayname(orig_schema)
                detail = '; '.join(f'{ref_str} depends on {dn}'
                                   for ref_str in ref_strs)
                raise errors.SchemaError(
                    f'cannot drop {vn} because '
                    f'other objects in the schema depend on it',
                    details=detail,
                )

        schema = schema.delete(self.scls)
        return schema

    def apply(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
    ) -> s_schema.Schema:
        scls = self.get_object(schema, context)
        self.scls = scls

        with self.new_context(schema, context, scls):
            if (
                not self.canonical
                and self.if_unused
                and schema.get_referrers(scls)
            ):
                parent_ctx = context.parent()
                if parent_ctx is not None:
                    parent_ctx.op.discard(self)

                return schema

            schema = self._delete_begin(schema, context)
            schema = self._delete_innards(schema, context)
            schema = self._delete_finalize(schema, context)

        return schema


class AlterSpecialObjectProperty(Command):
    astnode = qlast.SetSpecialField

    @classmethod
    def _cmd_tree_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: CommandContext,
    ) -> AlterObjectProperty:
        assert isinstance(astnode, qlast.BaseSetField)

        propname = astnode.name
        parent_ctx = context.current()
        parent_op = parent_ctx.op
        assert isinstance(parent_op, ObjectCommand)
        parent_cls = parent_op.get_schema_metaclass()
        field = parent_cls.get_field(propname)

        new_value: Any = astnode.value

        if field.type is s_expr.Expression:
            if parent_cls.has_field(f'orig_{field.name}'):
                orig_text = cls.get_orig_expr_text(
                    schema, parent_op.qlast, field.name)
            else:
                orig_text = None
            new_value = s_expr.Expression.from_ast(
                astnode.value,
                schema,
                context.modaliases,
                context.localnames,
                orig_text=orig_text,
            )

        return AlterObjectProperty(
            property=astnode.name,
            new_value=new_value,
            source_context=astnode.context,
        )


class AlterObjectProperty(Command):
    astnode = qlast.SetField

    property = struct.Field(str)
    old_value = struct.Field[Any](object, None)
    new_value = struct.Field[Any](object, None)
    source = struct.Field(str, None)

    @classmethod
    def _cmd_tree_from_ast(
        cls,
        schema: s_schema.Schema,
        astnode: qlast.DDLOperation,
        context: CommandContext,
    ) -> AlterObjectProperty:
        assert isinstance(astnode, qlast.BaseSetField)

        propname = astnode.name

        parent_ctx = context.current()
        parent_op = parent_ctx.op
        assert isinstance(parent_op, ObjectCommand)
        parent_cls = parent_op.get_schema_metaclass()
        field = parent_cls.get_field(propname)
        if field is None:
            raise errors.SchemaDefinitionError(
                f'{propname!r} is not a valid field',
                context=astnode.context)

        if not (field.allow_ddl_set
                or context.stdmode
                or context.testmode):
            raise errors.SchemaDefinitionError(
                f'{propname!r} is not a valid field',
                context=astnode.context)

        if field.name == 'id' and not isinstance(parent_op, CreateObject):
            raise errors.SchemaDefinitionError(
                f'cannot alter object id',
                context=astnode.context)

        new_value: Any

        if field.type is s_expr.Expression:
            if parent_cls.has_field(f'orig_{field.name}'):
                orig_text = cls.get_orig_expr_text(
                    schema, parent_op.qlast, field.name)
            else:
                orig_text = None
            new_value = s_expr.Expression.from_ast(
                astnode.value,
                schema,
                context.modaliases,
                orig_text=orig_text,
            )
        else:
            if isinstance(astnode.value, qlast.Tuple):
                new_value = tuple(
                    qlcompiler.evaluate_ast_to_python_val(
                        el, schema=schema)
                    for el in astnode.value.elements
                )

            elif isinstance(astnode.value, qlast.ObjectRef):

                new_value = utils.ast_to_object_shell(
                    astnode.value,
                    modaliases=context.modaliases,
                    schema=schema,
                )

            elif (isinstance(astnode.value, qlast.Set)
                    and not astnode.value.elements):
                # empty set
                new_value = None

            else:
                new_value = qlcompiler.evaluate_ast_to_python_val(
                    astnode.value, schema=schema)

        return cls(property=propname, new_value=new_value,
                   source_context=astnode.context)

    def _get_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        parent_node: Optional[qlast.DDLOperation] = None,
    ) -> Optional[qlast.DDLOperation]:
        value = self.new_value
        astcls = qlast.SetField

        new_value_empty = \
            (value is None or
                (isinstance(value, collections.abc.Container) and not value))

        parent_ctx = context.current()
        parent_op = parent_ctx.op
        assert isinstance(parent_op, ObjectCommand)
        assert parent_node is not None
        parent_cls = parent_op.get_schema_metaclass()
        field = parent_cls.get_field(self.property)
        parent_node_attr = parent_op.get_ast_attr_for_field(field.name)
        if field is None:
            raise errors.SchemaDefinitionError(
                f'{self.property!r} is not a valid field',
                context=self.source_context)

        if self.property == 'id':
            return None

        if (not field.allow_ddl_set
                and self.property != 'expr'
                and parent_node_attr is None):
            # Don't produce any AST if:
            #
            # * a field does not have the "allow_ddl_set" option, unless
            #   it's an 'expr' field.
            #
            #   'expr' fields come from the "USING" clause and are specially
            #   treated in parser and codegen.
            return None

        if self.source == 'inheritance':
            # We don't want to show inherited properties unless
            # we are in "descriptive_mode" and ...

            if ((not context.descriptive_mode
                    or self.property not in {'default', 'readonly'})
                    and parent_node_attr is None):
                # If property isn't 'default' or 'readonly' --
                # skip the AST for it.
                return None

            parentop_sn = sn.shortname_from_fullname(parent_op.classname).name
            if self.property == 'default' and parentop_sn == 'id':
                # If it's 'default' for the 'id' property --
                # skip the AST for it.
                return None

        if new_value_empty:
            return None

        if issubclass(field.type, s_expr.Expression):
            return self._get_expr_field_ast(
                schema,
                context,
                parent_op=parent_op,
                field=field,
                parent_node=parent_node,
                parent_node_attr=parent_node_attr,
            )
        elif (v := utils.is_nontrivial_container(value)) and v is not None:
            value = qlast.Tuple(elements=[
                utils.const_ast_from_python(el) for el in v
            ])
        elif isinstance(value, uuid.UUID):
            value = qlast.TypeCast(
                expr=qlast.StringConstant.from_python(str(value)),
                type=qlast.TypeName(
                    maintype=qlast.ObjectRef(
                        name='uuid',
                        module='std',
                    )
                )
            )
        else:
            value = utils.const_ast_from_python(value)

        return astcls(name=self.property, value=value)

    def _get_expr_field_ast(
        self,
        schema: s_schema.Schema,
        context: CommandContext,
        *,
        parent_op: ObjectCommand[so.Object],
        field: so.Field[Any],
        parent_node: qlast.DDLOperation,
        parent_node_attr: Optional[str],
    ) -> Optional[qlast.DDLOperation]:
        from edb import edgeql

        astcls: Type[qlast.BaseSetField]

        assert isinstance(
            self.new_value,
            (s_expr.Expression, s_expr.ExpressionShell),
        )

        if self.property == 'expr':
            astcls = qlast.SetSpecialField
        else:
            astcls = qlast.SetField

        parent_cls = parent_op.get_schema_metaclass()
        has_shadow = parent_cls.has_field(f'orig_{field.name}')

        if context.descriptive_mode:
            # When generating AST for DESCRIBE AS TEXT, we want
            # to use the original user-specified and unmangled
            # expression to render the object definition.
            expr_ql = edgeql.parse_fragment(self.new_value.origtext)
        else:
            # In all other DESCRIBE modes we want the original expression
            # to be there as a 'SET orig_<expr> := ...' command.
            # The mangled expression should be the main expression that
            # the object is defined with.
            expr_ql = self.new_value.qlast
            orig_fname = f'orig_{field.name}'
            assert self.new_value.origtext is not None
            if (
                has_shadow
                and not qlast.get_ddl_field_value(parent_node, orig_fname)
                and self.new_value.text != self.new_value.origtext
            ):
                parent_node.commands.append(
                    qlast.SetField(
                        name=orig_fname,
                        value=qlast.StringConstant.from_python(
                            self.new_value.origtext),
                    )
                )

        if parent_node is not None and parent_node_attr is not None:
            setattr(parent_node, parent_node_attr, expr_ql)
            return None
        else:
            return astcls(name=self.property, value=expr_ql)

    def __repr__(self) -> str:
        return '<%s.%s "%s":"%s"->"%s">' % (
            self.__class__.__module__, self.__class__.__name__,
            self.property, self.old_value, self.new_value)


def compile_ddl(
    schema: s_schema.Schema,
    astnode: qlast.DDLOperation,
    *,
    context: Optional[CommandContext]=None,
) -> Command:

    if context is None:
        context = CommandContext()

    primary_cmdcls = CommandMeta._astnode_map.get(type(astnode))
    if primary_cmdcls is None:
        raise LookupError(f'no delta command class for AST node {astnode!r}')

    cmdcls = primary_cmdcls.command_for_ast_node(astnode, schema, context)

    context_class = cmdcls.get_context_class()
    if context_class is not None:
        modaliases = cmdcls._modaliases_from_ast(schema, astnode, context)
        localnames = cmdcls.localnames_from_ast(schema, astnode, context)
        ctxcls = cast(
            Type[ObjectCommandContext[so.Object]],
            context_class,
        )
        ctx = ctxcls(
            schema,
            op=cast(ObjectCommand[so.Object], _dummy_command),
            scls=_dummy_object,
            modaliases=modaliases,
            localnames=localnames,
        )
        with context(ctx):
            cmd = cmdcls._cmd_tree_from_ast(schema, astnode, context)
    else:
        cmd = cmdcls._cmd_tree_from_ast(schema, astnode, context)

    return cmd


# See _dummy_command
_dummy_object_command: ObjectCommand[Any] = ObjectCommand(classname="dummy")


def get_object_delta_command(
    *,
    objtype: Type[so.Object_T],
    cmdtype: Type[ObjectCommand_T],
    schema: s_schema.Schema,
    name: str,
    ddl_identity: Optional[Mapping[str, Any]] = None,
    **kwargs: Any,
) -> ObjectCommand_T:

    cmdcls = cast(
        Type[ObjectCommand_T],
        ObjectCommandMeta.get_command_class_or_die(cmdtype, objtype),
    )

    return cmdcls(
        classname=name,
        ddl_identity=ddl_identity,
        **kwargs,
    )


def get_object_command_id(delta: ObjectCommand[so.Object]) -> str:
    quoted_name: str

    if isinstance(delta.classname, sn.Name):
        quoted_module = qlquote.quote_ident(delta.classname.module)
        quoted_nqname = qlquote.quote_ident(delta.classname.name)
        quoted_name = sn.Name(module=quoted_module, name=quoted_nqname)
    else:
        quoted_name = qlquote.quote_ident(delta.classname)

    if isinstance(delta, CreateObject):
        qlop = 'CREATE'
    elif isinstance(delta, AlterObject):
        qlop = 'ALTER'
    elif isinstance(delta, DeleteObject):
        qlop = 'DROP'
    else:
        raise AssertionError(f'unexpected command type: {type(delta)}')

    qlcls = delta.get_schema_metaclass().get_ql_class_or_die()

    return f'{qlop} {qlcls} {quoted_name}'

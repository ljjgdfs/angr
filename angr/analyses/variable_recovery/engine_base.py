from typing import Optional, Set, List, Tuple, TYPE_CHECKING
import logging

import claripy

from ...storage.memory_mixins.paged_memory.pages.multi_values import MultiValues
from ...engines.light import SimEngineLight, ArithmeticExpression
from ...errors import SimEngineError, SimMemoryMissingError
from ...sim_variable import SimVariable, SimStackVariable, SimRegisterVariable, SimMemoryVariable
from ...code_location import CodeLocation
from ..typehoon import typevars, typeconsts

if TYPE_CHECKING:
    from .variable_recovery_base import VariableRecoveryStateBase
    from angr.knowledge_plugins.variables.variable_manager import VariableManager

#
# The base engine used in VariableRecoveryFast
#

l = logging.getLogger(name=__name__)


class RichR:
    """
    A rich representation of calculation results.
    """

    __slots__ = ('data', 'variable', 'typevar', 'type_constraints', )

    def __init__(self, data: claripy.ast.Base, variable=None, typevar: Optional[typevars.TypeVariable]=None,
                 type_constraints=None):
        self.data: claripy.ast.Base = data
        self.variable = variable
        self.typevar = typevar
        self.type_constraints = type_constraints

    @property
    def bits(self):
        if self.data is not None and not isinstance(self.data, (int, float)):
            return self.data.bits
        if self.variable is not None:
            return self.variable.bits
        return None

    def __repr__(self):
        return "R{%r}" % self.data


class SimEngineVRBase(SimEngineLight):

    state: 'VariableRecoveryStateBase'

    def __init__(self, project, kb):
        super().__init__()

        self.project = project
        self.kb = kb
        self.variable_manager: Optional['VariableManager'] = None

    @property
    def func_addr(self):
        if self.state is None:
            return None
        return self.state.function.addr

    def process(self, state, *args, **kwargs):  # pylint:disable=unused-argument

        self.variable_manager = state.variable_manager

        try:
            self._process(state, None, block=kwargs.pop('block', None))
        except SimEngineError as e:
            if kwargs.pop('fail_fast', False) is True:
                raise e

    def _process(self, state, successors, block=None, func_addr=None):  # pylint:disable=unused-argument,arguments-differ
        super()._process(state, successors, block=block)

    #
    # Logic
    #

    def _reference(self, richr: RichR, codeloc: CodeLocation, src=None):
        data: claripy.ast.Base = richr.data
        # extract stack offset
        if self.state.is_stack_address(data):
            # this is a stack address
            stack_offset: Optional[int] = self.state.get_stack_offset(data)
        else:
            return

        existing_vars: List[Tuple[SimVariable,int]] = self.variable_manager[self.func_addr].find_variables_by_stmt(
            self.block.addr,
            self.stmt_idx,
            'memory')

        # find the correct variable
        variable = None
        if existing_vars:
            variable, _ = existing_vars[0]

        vs = None
        if stack_offset is not None:
            stack_addr = self.state.stack_addr_from_offset(stack_offset)
            if variable is None:
                # TODO: how to determine the size for a lea?
                try:
                    vs: Optional[MultiValues] = self.state.stack_region.load(stack_addr, size=1)
                except SimMemoryMissingError:
                    vs = None

                if vs is not None:
                    # extract variables
                    for values in vs.values.values():
                        for v in values:
                            for var_stack_offset, var in self.state.extract_variables(v):
                                existing_vars.append((var, var_stack_offset))

                if not existing_vars:
                    # no variables exist
                    lea_size = 1
                    variable = SimStackVariable(stack_offset, lea_size, base='bp',
                                                ident=self.variable_manager[self.func_addr].next_variable_ident(
                                                    'stack'),
                                                region=self.func_addr,
                                                )
                    self.variable_manager[self.func_addr].add_variable('stack', stack_offset, variable)
                    l.debug('Identified a new stack variable %s at %#x.', variable, self.ins_addr)
                    existing_vars.append((variable, 0))

                else:
                    # FIXME: Why is it only taking the first variable?
                    variable = next(iter(existing_vars))[0]

            # write the variable back to stack
            if vs is None:
                top = self.state.top(self.arch.byte_width)
                top = self.state.annotate_with_variables(top, [(0, variable)])
                vs = MultiValues(offset_to_values={0: {top}})
            self.state.stack_region.store(stack_addr, vs)

        typevar = typevars.TypeVariable() if richr.typevar is None else richr.typevar
        self.state.typevars.add_type_variable(variable, codeloc, typevar)

        # find all variables
        for var, offset in existing_vars:
            if offset is None: offset = 0
            offset_into_var = (stack_offset - offset) if stack_offset is not None else None  # TODO: Is this correct?
            if offset_into_var == 0: offset_into_var = None
            self.variable_manager[self.func_addr].reference_at(var, offset_into_var, codeloc,
                                                               atom=src)

    def _assign_to_register(self, offset, richr, size, src=None, dst=None):
        """

        :param int offset:
        :param RichR data:
        :param int size:
        :return:
        """

        codeloc: CodeLocation = self._codeloc()
        data: claripy.ast.Base = richr.data

        # lea
        self._reference(richr, codeloc, src=src)

        # handle register writes
        existing_vars = self.variable_manager[self.func_addr].find_variables_by_atom(self.block.addr, self.stmt_idx,
                                                                                     dst)
        existing_vars: Set[Tuple[SimVariable,int]]
        if not existing_vars:
            variable = SimRegisterVariable(offset, size,
                                           ident=self.variable_manager[self.func_addr].next_variable_ident(
                                               'register'),
                                           region=self.func_addr
                                           )
            self.variable_manager[self.func_addr].set_variable('register', offset, variable)
        else:
            variable, _ = next(iter(existing_vars))

        annotated_data = self.state.annotate_with_variables(data, [(0, variable)])  # FIXME: The offset does not have to be 0
        v = MultiValues(offset_to_values={0: {annotated_data}})
        self.state.register_region.store(offset, v)
        # register with the variable manager
        self.variable_manager[self.func_addr].write_to(variable, None, codeloc, atom=dst)

        if not self.arch.is_artificial_register(offset, size) and richr.typevar is not None:
            if not self.state.typevars.has_type_variable_for(variable, codeloc):
                # assign a new type variable to it
                typevar = typevars.TypeVariable()
                self.state.typevars.add_type_variable(variable, codeloc, typevar)
                # create constraints
                self.state.add_type_constraint(typevars.Subtype(richr.typevar, typevar))
                self.state.add_type_constraint(typevars.Subtype(typevar, typeconsts.int_type(variable.size * 8)))

    def _store(self, richr_addr: RichR, data: RichR, size, stmt=None):  # pylint:disable=unused-argument
        """

        :param RichR addr:
        :param RichR data:
        :param int size:
        :return:
        """

        addr: claripy.ast.Base = richr_addr.data
        stored = False

        if addr.concrete:
            self._store_to_global(addr._model_concrete.value, data, size, stmt=stmt)
            stored = True
        else:
            if self.state.is_stack_address(addr):
                stack_offset = self.state.get_stack_offset(addr)
                if stack_offset is not None:
                    # Storing data to stack
                    self._store_to_stack(stack_offset, data, size, stmt=stmt)
                    stored = True

        if not stored:
            # storing to a location specified by a pointer whose value cannot be determined at this point
            self._store_to_variable(richr_addr, size, stmt=stmt)

    def _store_to_stack(self, stack_offset, data: RichR, size, stmt=None, endness=None):
        if stmt is None:
            existing_vars = self.variable_manager[self.func_addr].find_variables_by_stmt(self.block.addr,
                                                                                         self.stmt_idx,
                                                                                         'memory'
                                                                                         )
        else:
            existing_vars = self.variable_manager[self.func_addr].find_variables_by_atom(self.block.addr,
                                                                                         self.stmt_idx,
                                                                                         stmt
                                                                                         )
        if not existing_vars:
            variable = SimStackVariable(stack_offset, size, base='bp',
                                        ident=self.variable_manager[self.func_addr].next_variable_ident(
                                            'stack'),
                                        region=self.func_addr,
                                        )
            variable_offset = 0
            if isinstance(stack_offset, int):
                self.variable_manager[self.func_addr].set_variable('stack', stack_offset, variable)
                l.debug('Identified a new stack variable %s at %#x.', variable, self.ins_addr)

        else:
            variable, variable_offset = next(iter(existing_vars))

        if isinstance(stack_offset, int):
            expr = self.state.annotate_with_variables(data.data, [(variable_offset, variable)])
            stack_addr = self.state.stack_addr_from_offset(stack_offset)
            self.state.stack_region.store(stack_addr, expr, endness=endness)

            codeloc = CodeLocation(self.block.addr, self.stmt_idx, ins_addr=self.ins_addr)

            addr_and_variables = set()
            try:
                vs: MultiValues = self.state.stack_region.load(stack_addr, size, endness=endness)
                for values in vs.values.values():
                    for value in values:
                        addr_and_variables.update(self.state.extract_variables(value))
            except SimMemoryMissingError:
                pass

            for var_offset, var in addr_and_variables:
                offset_into_var = var_offset
                if offset_into_var == 0:
                    offset_into_var = None
                self.variable_manager[self.func_addr].write_to(var,
                                                               offset_into_var,
                                                               codeloc,
                                                               atom=stmt,
                                                               )

            # create type constraints
            if data.typevar is not None:
                if not self.state.typevars.has_type_variable_for(variable, codeloc):
                    typevar = typevars.TypeVariable()
                    self.state.typevars.add_type_variable(variable, codeloc, typevar)
                else:
                    typevar = self.state.typevars.get_type_variable(variable, codeloc)
                if typevar is not None:
                    self.state.add_type_constraint(
                        typevars.Subtype(data.typevar, typevar)
                    )
        # TODO: Create a tv_sp.store.<bits>@N <: typevar type constraint for the stack pointer

    def _store_to_global(self, addr: int, data: RichR, size: int, stmt=None):
        variable_manager = self.variable_manager['global']
        if stmt is None:
            existing_vars = variable_manager.find_variables_by_stmt(self.block.addr, self.stmt_idx, 'memory')
        else:
            existing_vars = variable_manager.find_variables_by_atom(self.block.addr, self.stmt_idx, stmt)
        if not existing_vars:
            variable = SimMemoryVariable(addr, size,
                                        ident=variable_manager.next_variable_ident('global'),
                                        )
            variable_manager.set_variable('global', addr, variable)
            l.debug('Identified a new global variable %s at %#x.', variable, self.ins_addr)

        else:
            variable, _ = next(iter(existing_vars))

        data_expr: claripy.ast.Base = data.data
        data_expr = self.state.annotate_with_variables(data_expr, [(0, variable)])

        self.state.global_region.store(addr,
                                       data_expr,
                                       endness=self.state.arch.memory_endness if stmt is None else stmt.endness)

        codeloc = CodeLocation(self.block.addr, self.stmt_idx, ins_addr=self.ins_addr)
        values: MultiValues = self.state.global_region.load(addr,
                                                            size=size,
                                                            endness=self.state.arch.memory_endness if stmt is None else stmt.endness)
        for vs in values.values.values():
            for v in vs:
                for var_offset, var in self.state.extract_variables(v):
                    variable_manager.write_to(var, var_offset, codeloc, atom=stmt)

        # create type constraints
        if data.typevar is not None:
            if not self.state.typevars.has_type_variable_for(variable, codeloc):
                typevar = typevars.TypeVariable()
                self.state.typevars.add_type_variable(variable, codeloc, typevar)
            else:
                typevar = self.state.typevars.get_type_variable(variable, codeloc)
            if typevar is not None:
                self.state.add_type_constraint(
                    typevars.Subtype(data.typevar, typevar)
                )

    def _store_to_variable(self, richr_addr: RichR, size: int, stmt=None):  # pylint:disable=unused-argument

        addr_variable = richr_addr.variable
        codeloc = self._codeloc()

        # Storing data into a pointer
        if richr_addr.type_constraints:
            for tc in richr_addr.type_constraints:
                self.state.add_type_constraint(tc)

        if richr_addr.typevar is None:
            typevar = typevars.TypeVariable()
        else:
            typevar = richr_addr.typevar

        if typevar is not None:
            if isinstance(typevar, typevars.DerivedTypeVariable) and isinstance(typevar.label, typevars.AddN):
                base_typevar = typevar.type_var
                field_offset = typevar.label.n
            else:
                base_typevar = typevar
                field_offset = 0

            # if addr_variable is not None:
            #     self.variable_manager[self.func_addr].reference_at(addr_variable, field_offset, codeloc, atom=stmt)

            store_typevar = typevars.DerivedTypeVariable(
                typevars.DerivedTypeVariable(base_typevar, typevars.Store()),
                typevars.HasField(size * self.state.arch.byte_width, field_offset)
            )
            if addr_variable is not None:
                self.state.typevars.add_type_variable(addr_variable, codeloc, typevar)
            self.state.add_type_constraint(typevars.Existence(store_typevar))

    def _load(self, richr_addr: RichR, size: int, expr=None):
        """

        :param RichR richr_addr:
        :param size:
        :return:
        """

        addr: claripy.ast.Base = richr_addr.data
        codeloc = CodeLocation(self.block.addr, self.stmt_idx, ins_addr=self.ins_addr)

        if self.state.is_stack_address(addr):
            potential_offset = self.state.get_stack_offset(addr)
            custom_mask = None
            if potential_offset is not None:
                # Loading data from stack

                # split the offset into a concrete offset and a dynamic offset
                # the stack offset may not be a concrete offset
                # for example, SP-0xe0+var_1
                if isinstance(potential_offset, tuple):
                    stack_offset = potential_offset[0]
                    custom_mask = potential_offset[1]
                    print(custom_mask)
                else:
                    stack_offset = potential_offset

                if type(stack_offset) is ArithmeticExpression:
                    if type(stack_offset.operands[0]) is int:
                        concrete_offset = stack_offset.operands[0]
                        dynamic_offset = stack_offset.operands[1]
                    elif type(stack_offset.operands[1]) is int:
                        concrete_offset = stack_offset.operands[1]
                        dynamic_offset = stack_offset.operands[0]
                    else:
                        # cannot determine the concrete offset. give up
                        concrete_offset = None
                        dynamic_offset = stack_offset
                else:
                    # type(stack_offset) is int
                    concrete_offset = stack_offset
                    dynamic_offset = None

                try:
                    values: Optional[MultiValues] = self.state.stack_region.load(
                        self.state.stack_addr_from_offset(concrete_offset, custom_mask=custom_mask),
                        size=size,
                        endness=self.state.arch.memory_endness)

                except SimMemoryMissingError:
                    values = None

                all_vars: Set[Tuple[int,SimVariable]] = set()
                if values:
                    for vs in values.values.values():
                        for v in vs:
                            for var_offset, var_ in self.state.extract_variables(v):
                                all_vars.add((var_offset, var_))

                if not all_vars:
                    variable = SimStackVariable(concrete_offset, size, base='bp',
                                                ident=self.variable_manager[self.func_addr].next_variable_ident(
                                                    'stack'),
                                                region=self.func_addr,
                                                )
                    v = self.state.top(size * self.state.arch.byte_width)
                    v = self.state.annotate_with_variables(v, [(0, variable)])
                    self.state.stack_region.store(concrete_offset, v, endness=self.state.arch.memory_endness)

                    self.variable_manager[self.func_addr].add_variable('stack', concrete_offset, variable)

                    l.debug('Identified a new stack variable %s at %#x.', variable, self.ins_addr)

                    all_vars = { (0, variable) }

                if len(all_vars) > 1:
                    # overlapping variables
                    l.warning("Reading memory with overlapping variables: %s. Ignoring all but the first one.",
                              all_vars)

                for var_offset, var in all_vars:
                    # calculate variable_offset
                    if dynamic_offset is None:
                        offset_into_variable = None
                    else:
                        if var_offset == 0:
                            offset_into_variable = dynamic_offset
                        else:
                            offset_into_variable = ArithmeticExpression(ArithmeticExpression.Add,
                                                                        (dynamic_offset, var_offset,)
                                                                        )
                    self.variable_manager[self.func_addr].read_from(var,
                                                                    offset_into_variable,
                                                                    codeloc,
                                                                    atom=expr,
                                                                    # overwrite=True
                                                                    )
                    break

                # add delayed type constraints
                if var in self.state.delayed_type_constraints:
                    for constraint in self.state.delayed_type_constraints[var]:
                        self.state.add_type_constraint(constraint)
                    self.state.delayed_type_constraints.pop(var)
                # create type constraints
                if not self.state.typevars.has_type_variable_for(var, codeloc):
                    typevar = typevars.TypeVariable()
                    self.state.typevars.add_type_variable(var, codeloc, typevar)
                else:
                    typevar = self.state.typevars.get_type_variable(var, codeloc)
                # TODO: Create a tv_sp.load.<bits>@N type variable for the stack variable
                #typevar = typevars.DerivedTypeVariable(
                #    typevars.DerivedTypeVariable(typevar, typevars.Load()),
                #    typevars.HasField(size * 8, 0)
                #)

                r = self.state.top(size * self.state.arch.byte_width)
                r = self.state.annotate_with_variables(r, list(all_vars))
                return RichR(r, variable=var, typevar=typevar)

        elif addr.concrete:
            # Loading data from memory
            global_variables = self.variable_manager['global']
            addr_v: int = addr._model_concrete.value
            variables = global_variables.get_global_variables(addr_v)
            if not variables:
                var = SimMemoryVariable(addr_v, size)
                global_variables.add_variable('global', addr_v, var)
                variables = [var]
            for var in variables:
                global_variables.read_from(var, 0, codeloc, atom=expr)

        # Loading data from a pointer
        if richr_addr.type_constraints:
            for tc in richr_addr.type_constraints:
                self.state.add_type_constraint(tc)

        # parse the loading offset
        offset = 0
        if (isinstance(richr_addr.typevar, typevars.DerivedTypeVariable) and
                isinstance(richr_addr.typevar.label, typevars.AddN)):
            offset = richr_addr.typevar.label.n
            richr_addr_typevar = richr_addr.typevar.type_var  # unpack
        else:
            richr_addr_typevar = richr_addr.typevar

        if richr_addr_typevar is not None:
            # create a type constraint
            typevar = typevars.DerivedTypeVariable(
                typevars.DerivedTypeVariable(richr_addr_typevar, typevars.Load()),
                typevars.HasField(size * self.state.arch.byte_width, offset)
            )
            self.state.add_type_constraint(typevars.Existence(typevar))
            return RichR(self.state.top(size * self.state.arch.byte_width), typevar=typevar)
        else:
            return RichR(self.state.top(size * self.state.arch.byte_width))

    def _read_from_register(self, offset, size, expr=None):
        """

        :param offset:
        :param size:
        :return:
        """

        codeloc = self._codeloc()

        try:
            values: Optional[MultiValues] = self.state.register_region.load(offset, size=size)
        except SimMemoryMissingError:
            values = None

        if not values:
            # the value does not exist. create a new variable
            variable = SimRegisterVariable(offset, size,
                                           ident=self.variable_manager[self.func_addr].next_variable_ident(
                                               'register'),
                                           region=self.func_addr,
                                           )
            value = self.state.top(size * self.state.arch.byte_width)
            value = self.state.annotate_with_variables(value, [(0, variable)])
            self.state.register_region.store(offset, value)
            self.variable_manager[self.func_addr].add_variable('register', offset, variable)

            value_list = [{ value }]
        else:
            value_list = list(values.values.values())

        variable_set = set()
        for value_set in value_list:
            for value in value_set:
                for var_offset, var in self.state.extract_variables(value):
                    self.variable_manager[self.func_addr].read_from(var, None, codeloc, atom=expr)
                    variable_set.add(var)

        if self.arch.is_artificial_register(offset, size) or offset in (self.arch.sp_offset, self.arch.bp_offset):
            typevar = None
            var = None
        else:
            # we accept the precision loss here by only returning the first variable
            # FIXME: Multiple variables
            var = next(iter(variable_set))

            # add delayed type constraints
            if var in self.state.delayed_type_constraints:
                for constraint in self.state.delayed_type_constraints[var]:
                    self.state.add_type_constraint(constraint)
                self.state.delayed_type_constraints.pop(var)

            if var not in self.state.typevars:
                typevar = typevars.TypeVariable()
                self.state.typevars.add_type_variable(var, codeloc, typevar)
            else:
                # FIXME: This is an extremely stupid hack. Fix it later.
                # typevar = next(reversed(list(self.state.typevars[var].values())))
                typevar = self.state.typevars[var]

        return RichR(next(iter(value_list[0])), variable=var, typevar=typevar)

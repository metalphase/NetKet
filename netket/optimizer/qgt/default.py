# Copyright 2021 The NetKet Authors - All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Any, TYPE_CHECKING
from functools import partial

import jax
from plum import Callable

import netket.jax as nkjax
from netket.optimizer.linear_operator import LinearOperator

from .qgt_jacobian import QGTJacobianDense, QGTJacobianPyTree
from .qgt_onthefly import QGTOnTheFly

from .. import solver as nk_solver_module

if TYPE_CHECKING:
    from netket.vqs import VariationalState

QGTConstructor = Callable[["VariationalState"], LinearOperator]

solvers: list[Callable] = []

for solver in dir(nk_solver_module):
    # only add solvers, not random
    # useless things
    if solver[:2] == "__":
        continue
    else:
        solvers.append(getattr(nk_solver_module, solver))


def _is_dense_solver(solver: Any) -> bool:
    """
    Returns true if the solver is one of our known dense solvers
    """
    if isinstance(solver, partial):
        solver = solver.func

    if solver in solvers:
        return True

    return False


def default_qgt_matrix(
    variational_state, solver: Any = False, **kwargs
) -> QGTConstructor:
    """
    Determines default metric tensor depending on variational_state and solver
    """
    from netket.vqs import FullSumState

    if isinstance(variational_state, FullSumState):
        return partial(QGTJacobianPyTree, **kwargs)

    n_param_leaves = len(jax.tree_util.tree_leaves(variational_state.parameters))
    n_params = variational_state.n_parameters

    # those require dense matrix that is known to be faster for this qgt
    if _is_dense_solver(solver):
        return partial(QGTJacobianDense, **kwargs)

    # TODO: Remove this once all QGT support diag_scale.
    has_diag_rescale = kwargs.get("diag_scale") is not None

    # arbitrary heuristic: if the network's parameters has many leaves
    # (an rbm has 3) then JacobianDense might be faster
    # the numbers chosen below are rather arbitrary and should be tuned.
    if (n_param_leaves > 6 and n_params > 800) or has_diag_rescale:
        if nkjax.tree_ishomogeneous(variational_state.parameters):
            return partial(QGTJacobianDense, **kwargs)
        else:
            return partial(QGTJacobianPyTree, **kwargs)
    else:
        return partial(QGTOnTheFly, **kwargs)  # type: ignore


class QGTAuto:
    """
    Automatically select the 'best' Quantum Geometric Tensor
    computing format according to some rather untested heuristic.

    Args:
        variational_state: The variational State
        kwargs: are passed on to the QGT constructor.
    """

    _last_vstate = None
    """Cached last variational state to skip logic to decide what type of
    QGT to chose.
    """

    _last_matrix: QGTConstructor | None = None
    """
    Cached last QGT. Used when vstate == _last_vstate
    """

    _kwargs: dict = {}
    """
    Kwargs passed at construction. Used when constructing a QGT.
    """

    _solver: Callable | None

    def __init__(self, solver: Callable | None = None, **kwargs):
        self._solver = solver

        self._kwargs = kwargs

    def __call__(
        self, variational_state: "VariationalState", *args, **kwargs
    ) -> LinearOperator:
        if self._last_vstate != variational_state:
            self._last_vstate = variational_state

            self._last_matrix = default_qgt_matrix(
                variational_state, solver=self._solver, **self._kwargs, **kwargs
            )

        return self._last_matrix(variational_state, *args, **kwargs)  # type: ignore

    def __repr__(self):
        return "QGTAuto()"

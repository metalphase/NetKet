# Copyright 2021-2024 The NetKet Authors - All rights reserved.
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

from . import tensor_networks

from .rbm import RBM, RBMModPhase, RBMMultiVal, RBMSymm
from .equivariant import GCNN
from .residual import ResGCNN
from .full_space import LogStateVector
from .jastrow import Jastrow
from .gaussian import Gaussian
from .deepset import DeepSetRelDistance, DeepSetMLP
from .ndm import NDM
from .autoreg import (
    AbstractARNN,
    ARNNSequential,
    ARNNDense,
    ARNNConv1D,
    ARNNConv2D,
)
from .fast_autoreg import (
    FastARNNSequential,
    FastARNNDense,
    FastARNNConv1D,
    FastARNNConv2D,
)
from .mlp import MLP

from .slater import Slater2nd, MultiSlater2nd

from .utils import update_GCNN_parity


from netket.utils import _hide_submodules

_hide_submodules(__name__)

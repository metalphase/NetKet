# Ammendment to equivariant.py to include residual group convolutional neural network layers
# (ResGCNN)
#
# The ResGCNN is described described in C. Roth et. al (2023) "High-accuracy variational 
# Monte Carlo for frustrated magnets with deep neural networks" with hidden layers given by:
# 
#  h^{(i+1)} = h^{(i)} + t_{i}W_{2}^{(i)} \circ \text{relu}[W_1^{(i)}\circ h^{(i)}]
#
# where h^{(i)} is the ith layer of the network. 
#
# The embedding and output layers are untouched from the equivariant.py construction.

from typing import Any

import numpy as np

from jax import numpy as jnp
from flax import linen as nn
from jax.nn.initializers import zeros, lecun_normal
from jax.scipy.special import logsumexp

# HashableArray is a class that wraps a numpy or jax array in order to make it hashable
# and equality comparable.
from netket.utils import HashableArray
from netket.utils.types import NNInitFunc, Array
from netket.utils.group import PermutationGroup
from netket.graph import Graph, Lattice
from netket.jax import logsumexp_cplx, is_complex_dtype
from netket.nn.activation import reim_selu, reim_relu
from netket.nn.symmetric_linear import (
    DenseSymmMatrix,
    DenseSymmFFT,
    DenseEquivariantFFT,
    DenseEquivariantIrrep,
)

# Same as netket.nn.symmetric_linear.default_equivariant_initializer
# All GCNN layers have kernels of shape [out_features, in_features, n_symm]
default_resgcnn_initializer = lecun_normal(in_axis=1, out_axis=0)


def identity(x):
    return x


class ResGCNN_FFT(nn.Module):
    r"""Implements a GCNN using a fast fourier transform over the translation group.

    The group convolution can be written in terms of translational convolutions with
    symmetry transformed filters as described in ` Cohen et. *al* <http://proceedings.mlr.press/v48/cohenc16.pdf>`_
    The translational convolutions are then implemented with Fast Fourier Transforms.
    """

    symmetries: HashableArray
    """A group of symmetry operations (or array of permutation indices) over which the network should be equivariant.
    Numpy/Jax arrays must be wrapped into an :class:`netket.utils.HashableArray`.
    """
    product_table: HashableArray
    """Product table describing the algebra of the symmetry group
    Numpy/Jax arrays must be wrapped into an :class:`netket.utils.HashableArray`.
    """
    shape: tuple
    """Shape of the translation group"""
    layers: int
    """Number of layers (not including sum layer over output)."""
    features: tuple
    """Number of features in each layer starting from the input. If a single number is given,
    all layers will have the same number of features."""
    characters: HashableArray
    """Array specifying the characters of the desired symmetry representation"""
    param_dtype: Any = float
    """The dtype of the weights."""
    activation: Any = reim_relu
    """The nonlinear activation function between hidden layers."""
    output_activation: Any = identity
    """The nonlinear activation before the output. Defaults to the identity."""
    input_mask: Array = None
    """Optional array of shape `(n_sites,)` used to restrict the convolutional
        kernel. Only parameters with mask :math:'\ne 0' are used. For best performance a
        boolean mask should be used."""
    hidden_mask: Array = None
    """Optional array of shape `(n_symm,)` where `(n_symm,)` = `len(graph.automorphisms())`
        used to restrict the convolutional kernel. Only parameters with mask :math:'\ne 0' are used.
        For best performance a boolean mask should be used"""
    equal_amplitudes: bool = False
    """If true forces all basis states to have the same amplitude by setting `Re[logψ] = 0`"""
    use_bias: bool = True
    """if True uses a bias in all layers."""
    precision: Any = None
    """numerical precision of the computation see :class:`jax.lax.Precision` for details."""
    kernel_init: NNInitFunc = default_resgcnn_initializer
    """Initializer for the kernels of all layers."""
    bias_init: NNInitFunc = zeros
    """Initializer for the biases of all layers."""
    complex_output: bool = True
    """Use complex-valued `logsumexp`. Necessary when parameters are real but some
    `characters` are negative."""

    def setup(self):
        self.n_symm = np.asarray(self.symmetries).shape[0]

        # Embedding layer that implements a projection onto a symmetry group. The output is
        # equivariant with respect to the symmetry operations of the group
        self.dense_symm = DenseSymmFFT(
            space_group=self.symmetries,
            shape=self.shape,
            features=self.features[0],
            param_dtype=self.param_dtype,
            use_bias=self.use_bias,
            kernel_init=self.kernel_init,
            bias_init=self.bias_init,
            precision=self.precision,
            mask=self.input_mask,
        )

        # A stack of equivariant layers that are drawn from the DenseEquivariantFFT layers defined
        # in netket.nn.symmetric_linear. If I were to hijack this class to add skip connections, it should
        # be done here when adding layers together.
        self.equivariant_layers = [
            DenseEquivariantFFT(
                product_table=self.product_table,
                shape=self.shape,
                features=self.features[layer + 1],
                use_bias=self.use_bias,
                param_dtype=self.param_dtype,
                precision=self.precision,
                kernel_init=self.kernel_init,
                bias_init=self.bias_init,
                mask=self.hidden_mask,
            )

            # Store the self.layers-1 blocks of GCNN hidden layers
            for layer in range(2*(self.layers - 1))
        ]

        

    # When we call on the ResGCNN model, we are invoking this __call__() function
    @nn.compact
    def __call__(self, x):
        if x.ndim < 3:
            x = jnp.expand_dims(x, -2)  # add a feature dimension

        # Apply our embedding layer to the input
        x = self.dense_symm(x)

        # For each layer index we are on, we first apply our specified activation function on the input x
        # then apply the layer'th layer in the equivariant layers stored in equivariant_layers.
        for layer in range(self.layers - 1):
            # This is where I should be able to implement the ResGCNN layers. Each layer should have the 
            # following structure with skip connection from beginning of each block to the end:
            # G-Conv layer 1, ReLU activation, G-Conv layer 2  
            z = self.equivariant_layers[layer](x)
            z = self.activation(z)
            z = self.equivariant_layers[layer+1](z)

            t = self.param("t_{}".format(layer), zeros, [1], self.param_dtype)
            x = x + (t * z)

        # Apply our output activation (identity function by default)
        x = self.output_activation(x)

        # Output layer
        if self.complex_output:
            x = logsumexp_cplx(x, axis=(-2, -1), b=jnp.asarray(self.characters))
        else:
            x = logsumexp(x, axis=(-2, -1), b=jnp.asarray(self.characters))

        if self.equal_amplitudes:
            return 1j * jnp.imag(x)
        else:
            return x


class ResGCNN_Irrep(nn.Module):
    r"""Implements a GCNN by projecting onto irreducible
    representations of the group. The projection onto
    the group is implemented with matrix multiplication

    Layers act on a feature maps of shape [batch_size, in_features, n_symm] and
    returns a feature map of shape [batch_size, features, n_symm].
    The input and the output are related by

    .. math ::

        y^{(i)}_g = \sum_{h,j} f^{(j)}_h W^{(ij)}_{h^{-1}g}.

    Note that this switches the convention of Cohen et al. to use an actual group
    convolution, but this doesn't affect equivariance.
    The convolution is implemented in terms of a group Fourier transform.
    Therefore, the group structure is represented internally as the set of its
    irrep matrices. After Fourier transforming, the convolution translates to

    .. math ::

        y^{(i)}_\rho = \sum_j f^{(j)}_\rho W^{(ij)}_\rho,

    where all terms are d x d matrices rather than numbers, and the juxtaposition
    stands for matrix multiplication.
    """

    symmetries: HashableArray
    """A group of symmetry operations (or array of permutation indices) over which the network should be equivariant.
    Numpy/Jax arrays must be wrapped into an :class:`netket.utils.HashableArray`.
    """
    irreps: tuple[HashableArray]
    """List of irreducible representation matrices"""
    layers: int
    """Number of layers (not including sum layer over output)."""
    features: tuple
    """Number of features in each layer starting from the input. If a single number is given,
    all layers will have the same number of features."""
    characters: HashableArray
    """Array specifying the characters of the desired symmetry representation"""
    param_dtype: Any = np.float64
    """The dtype of the weights."""
    activation: Any = reim_relu
    """The nonlinear activation function between hidden layers."""
    output_activation: Any = identity
    """The nonlinear activation before the output."""
    input_mask: Array = None
    """Optional array of shape `(n_sites,)` used to restrict the convolutional
        kernel. Only parameters with mask :math:'\ne 0' are used. For best performance a
        boolean mask should be used."""
    hidden_mask: Array = None
    """Optional array of shape `(n_symm,)` where `(n_symm,)` = `len(graph.automorphisms())`
        used to restrict the convolutional kernel. Only parameters with mask :math:'\ne 0' are used.
        For best performance a boolean mask should be used"""
    equal_amplitudes: bool = False
    """If true forces all basis states to have the same amplitude by setting `Re[logψ] = 0`"""
    use_bias: bool = True
    """if True uses a bias in all layers."""
    precision: Any = None
    """numerical precision of the computation see :class:`jax.lax.Precision` for details."""
    kernel_init: NNInitFunc = default_resgcnn_initializer
    """Initializer for the kernels of all layers."""
    bias_init: NNInitFunc = zeros
    """Initializer for the biases of all layers."""
    complex_output: bool = True
    """Use complex-valued `logsumexp`. Necessary when parameters are real but some
    `characters` are negative."""

    def setup(self):
        self.n_symm = np.asarray(self.symmetries).shape[0]

        self.dense_symm = DenseSymmMatrix(
            symmetries=self.symmetries,
            features=self.features[0],
            param_dtype=self.param_dtype,
            use_bias=self.use_bias,
            kernel_init=self.kernel_init,
            bias_init=self.bias_init,
            precision=self.precision,
            mask=self.input_mask,
        )

        self.equivariant_layers = [
            DenseEquivariantIrrep(
                irreps=self.irreps,
                features=self.features[layer + 1],
                use_bias=self.use_bias,
                param_dtype=self.param_dtype,
                precision=self.precision,
                kernel_init=self.kernel_init,
                bias_init=self.bias_init,
                mask=self.hidden_mask,
            )
            for layer in range(2*(self.layers - 1))
        ]

    @nn.compact
    def __call__(self, x):
        if x.ndim < 3:
            x = jnp.expand_dims(x, -2)  # add a feature dimension
        x = self.dense_symm(x)

        for layer in range(self.layers - 1):

            # We want to replicate the structure of:
            # h^{(i+1)} = h^{(i)} + t_{i}W_{2}^{(i)} \circ \text{relu}[W_1^{(i)}\circ h^{(i)}]

            z = self.equivariant_layers[layer](x)
            z = self.activation(z)
            z = self.equivariant_layers[layer+1](z)

            t = self.param("t_{}".format(layer), zeros, [1], self.param_dtype)
            x = x + (t * z)

        x = self.output_activation(x)

        if self.complex_output:
            x = logsumexp_cplx(x, axis=(-2, -1), b=jnp.asarray(self.characters))
        else:
            x = logsumexp(x, axis=(-2, -1), b=jnp.asarray(self.characters))

        if self.equal_amplitudes:
            return 1j * jnp.imag(x)
        else:
            return x


class GCNN_Parity_FFT(nn.Module):
    r"""Implements a GCNN using a fast fourier transform over the translation group.
    The group convolution can be written in terms of translational convolutions with
    symmetry transformed filters as described in ` Cohen et. *al* <http://proceedings.mlr.press/v48/cohenc16.pdf>`_
    The translational convolutions are then implemented with Fast Fourier Transforms.
    This model adds parity symmetry under the transformation x->-x
    """

    symmetries: HashableArray
    """A group of symmetry operations (or array of permutation indices) over which the network should be equivariant.
    Numpy/Jax arrays must be wrapped into an :class:`netket.utils.HashableArray`.
    """
    product_table: HashableArray
    """Product table describing the algebra of the symmetry group
    Numpy/Jax arrays must be wrapped into an :class:`netket.utils.HashableArray`.
    """
    shape: tuple
    """Shape of the translation group"""
    layers: int
    """Number of layers (not including sum layer over output)."""
    features: tuple
    """Number of features in each layer starting from the input. If a single number is given,
    all layers will have the same number of features."""
    characters: HashableArray
    """Array specifying the characters of the desired symmetry representation"""
    parity: int
    """Integer specifying the eigenvalue with respect to parity"""
    param_dtype: Any = np.float64
    """The dtype of the weights."""
    activation: Any = reim_selu
    """The nonlinear activation function between hidden layers."""
    output_activation: Any = identity
    """The nonlinear activation before the output."""
    input_mask: Array = None
    """Optional array of shape `(n_sites,)` used to restrict the convolutional
        kernel. Only parameters with mask :math:'\ne 0' are used. For best performance a
        boolean mask should be used."""
    hidden_mask: Array = None
    """Optional array of shape `(n_symm,)` where `(n_symm,)` = `len(graph.automorphisms())`
        used to restrict the convolutional kernel. Only parameters with mask :math:'\ne 0' are used.
        For best performance a boolean mask should be used"""
    equal_amplitudes: bool = False
    """If true forces all basis states to have the same amplitude by setting Re[psi] = 0"""
    use_bias: bool = True
    """if True uses a bias in all layers."""
    precision: Any = None
    """numerical precision of the computation see :class:`jax.lax.Precision` for details."""
    kernel_init: NNInitFunc = default_resgcnn_initializer
    """Initializer for the kernels of all layers."""
    bias_init: NNInitFunc = zeros
    """Initializer for the biases of all layers."""
    complex_output: bool = True
    """Use complex-valued `logsumexp`. Necessary when parameters are real but some
    `characters` are negative."""

    def setup(self):
        self.n_symm = np.asarray(self.symmetries).shape[0]

        self.dense_symm = DenseSymmFFT(
            space_group=self.symmetries,
            shape=self.shape,
            features=self.features[0],
            param_dtype=self.param_dtype,
            use_bias=self.use_bias,
            kernel_init=self.kernel_init,
            bias_init=self.bias_init,
            precision=self.precision,
            mask=self.input_mask,
        )

        self.equivariant_layers = [
            DenseEquivariantFFT(
                product_table=self.product_table,
                shape=self.shape,
                features=self.features[layer + 1],
                use_bias=self.use_bias,
                param_dtype=self.param_dtype,
                precision=self.precision,
                kernel_init=self.kernel_init,
                bias_init=self.bias_init,
                mask=self.hidden_mask,
            )
            for layer in range(self.layers - 1)
        ]

        self.equivariant_layers_flip = [
            DenseEquivariantFFT(
                product_table=self.product_table,
                shape=self.shape,
                features=self.features[layer + 1],
                # this would bias the same outputs as self.equivariant
                use_bias=self.use_bias,
                param_dtype=self.param_dtype,
                precision=self.precision,
                kernel_init=self.kernel_init,
                bias_init=self.bias_init,
                mask=self.hidden_mask,
            )
            for layer in range(self.layers - 1)
        ]

    @nn.compact
    def __call__(self, x):
        if x.ndim < 3:
            x = jnp.expand_dims(x, -2)  # add a feature dimension

        x_flip = self.dense_symm(-1 * x)
        x = self.dense_symm(x)

        for layer in range(self.layers - 1):
            x = self.activation(x)
            x_flip = self.activation(x_flip)

            x_new = (
                self.equivariant_layers[layer](x)
                + self.equivariant_layers_flip[layer](x_flip)
            ) / np.sqrt(2)
            x_flip = (
                self.equivariant_layers[layer](x_flip)
                + self.equivariant_layers_flip[layer](x)
            ) / np.sqrt(2)
            x = jnp.array(x_new, copy=True)

        x = jnp.concatenate((x, x_flip), -1)

        x = self.output_activation(x)

        if self.parity == 1:
            par_chars = jnp.expand_dims(
                jnp.concatenate(
                    (jnp.array(self.characters), jnp.array(self.characters)), 0
                ),
                (0, 1),
            )
        else:
            par_chars = jnp.expand_dims(
                jnp.concatenate(
                    (jnp.array(self.characters), -1 * jnp.array(self.characters)), 0
                ),
                (0, 1),
            )

        if self.complex_output:
            x = logsumexp_cplx(x, axis=(-2, -1), b=par_chars)
        else:
            x = logsumexp(x, axis=(-2, -1), b=par_chars)

        if self.equal_amplitudes:
            return 1j * jnp.imag(x)
        else:
            return x


class GCNN_Parity_Irrep(nn.Module):
    r"""Implements a GCNN by projecting onto irreducible
    representations of the group. The projection onto
    the group is implemented with matrix multiplication

    Layers act on a feature maps of shape [batch_size, in_features, n_symm] and
    returns a feature map of shape [batch_size, features, n_symm].
    The input and the output are related by

    .. math ::

        y^{(i)}_g = \sum_{h,j} f^{(j)}_h W^{(ij)}_{h^{-1}g}.

    Note that this switches the convention of Cohen et al. to use an actual group
    convolution, but this doesn't affect equivariance.
    The convolution is implemented in terms of a group Fourier transform.
    Therefore, the group structure is represented internally as the set of its
    irrep matrices. After Fourier transforming, the convolution translates to

    .. math ::

        y^{(i)}_\rho = \sum_j f^{(j)}_\rho W^{(ij)}_\rho,

    where all terms are d x d matrices rather than numbers, and the juxtaposition
    stands for matrix multiplication.

    This model adds parity symmetry under the transformation x->-x

    """

    symmetries: HashableArray
    """A group of symmetry operations (or array of permutation indices) over which the network should be equivariant.
    Numpy/Jax arrays must be wrapped into an :class:`netket.utils.HashableArray`.
    """
    irreps: tuple[HashableArray]
    """List of irreducible representation matrices"""
    layers: int
    """Number of layers (not including sum layer over output)."""
    features: tuple
    """Number of features in each layer starting from the input. If a single number is given,
    all layers will have the same number of features."""
    characters: HashableArray
    """Array specifying the characters of the desired symmetry representation"""
    parity: int
    """Integer specifying the eigenvalue with respect to parity"""
    param_dtype: Any = np.float64
    """The dtype of the weights."""
    activation: Any = reim_selu
    """The nonlinear activation function between hidden layers."""
    output_activation: Any = identity
    """The nonlinear activation before the output."""
    input_mask: Array = None
    """Optional array of shape `(n_sites,)` used to restrict the convolutional
        kernel. Only parameters with mask :math:'\ne 0' are used. For best performance a
        boolean mask should be used."""
    hidden_mask: Array = None
    """Optional array of shape `(n_symm,)` where `(n_symm,)` = `len(graph.automorphisms())`
        used to restrict the convolutional kernel. Only parameters with mask :math:'\ne 0' are used.
        For best performance a boolean mask should be used"""
    equal_amplitudes: bool = False
    """If true forces all basis states to have the same amplitude by setting Re[psi] = 0"""
    use_bias: bool = True
    """if True uses a bias in all layers."""
    precision: Any = None
    """numerical precision of the computation see :class:`jax.lax.Precision` for details."""
    kernel_init: NNInitFunc = default_resgcnn_initializer
    """Initializer for the kernels of all layers."""
    bias_init: NNInitFunc = zeros
    """Initializer for the biases of all layers."""
    complex_output: bool = True
    """Use complex-valued `logsumexp`. Necessary when parameters are real but some
    `characters` are negative."""

    def setup(self):
        self.n_symm = np.asarray(self.symmetries).shape[0]

        self.dense_symm = DenseSymmMatrix(
            symmetries=self.symmetries,
            features=self.features[0],
            param_dtype=self.param_dtype,
            use_bias=self.use_bias,
            kernel_init=self.kernel_init,
            bias_init=self.bias_init,
            precision=self.precision,
            mask=self.input_mask,
        )

        self.equivariant_layers = [
            DenseEquivariantIrrep(
                irreps=self.irreps,
                features=self.features[layer + 1],
                use_bias=self.use_bias,
                param_dtype=self.param_dtype,
                precision=self.precision,
                kernel_init=self.kernel_init,
                bias_init=self.bias_init,
                mask=self.hidden_mask,
            )
            for layer in range(self.layers - 1)
        ]

        self.equivariant_layers_flip = [
            DenseEquivariantIrrep(
                irreps=self.irreps,
                features=self.features[layer + 1],
                # this would bias the same outputs as self.equivariant
                use_bias=self.use_bias,
                param_dtype=self.param_dtype,
                precision=self.precision,
                kernel_init=self.kernel_init,
                bias_init=self.bias_init,
                mask=self.hidden_mask,
            )
            for layer in range(self.layers - 1)
        ]

    @nn.compact
    def __call__(self, x):
        if x.ndim < 3:
            x = jnp.expand_dims(x, -2)  # add a feature dimension

        x_flip = self.dense_symm(-1 * x)
        x = self.dense_symm(x)

        for layer in range(self.layers - 1):
            x = self.activation(x)
            x_flip = self.activation(x_flip)

            x_new = (
                self.equivariant_layers[layer](x)
                + self.equivariant_layers_flip[layer](x_flip)
            ) / np.sqrt(2)
            x_flip = (
                self.equivariant_layers[layer](x_flip)
                + self.equivariant_layers_flip[layer](x)
            ) / np.sqrt(2)
            x = jnp.array(x_new, copy=True)

        x = jnp.concatenate((x, x_flip), -1)

        x = self.output_activation(x)

        if self.parity == 1:
            par_chars = jnp.expand_dims(
                jnp.concatenate(
                    (jnp.array(self.characters), jnp.array(self.characters)), 0
                ),
                (0, 1),
            )
        else:
            par_chars = jnp.expand_dims(
                jnp.concatenate(
                    (jnp.array(self.characters), -1 * jnp.array(self.characters)), 0
                ),
                (0, 1),
            )

        if self.complex_output:
            x = logsumexp_cplx(x, axis=(-2, -1), b=par_chars)
        else:
            x = logsumexp(x, axis=(-2, -1), b=par_chars)

        if self.equal_amplitudes:
            return 1j * jnp.imag(x)
        else:
            return x


def ResGCNN(
    symmetries=None,
    product_table=None,
    irreps=None,
    point_group=None,
    mode="auto",
    shape=None,
    layers=None,
    features=None,
    characters=None,
    parity=None,
    param_dtype=np.float64,
    complex_output=True,
    input_mask=None,
    hidden_mask=None,
    **kwargs,
):
    r"""Implements a Group Convolutional Neural Network (G-CNN) that outputs a wavefunction
    that is invariant over a specified symmetry group.

    The G-CNN is described in `Cohen et al. <http://proceedings.mlr.press/v48/cohenc16.pdf>`_
    and applied to quantum many-body problems in `Roth et al. <https://arxiv.org/pdf/2104.05085.pdf>`_ .

    The G-CNN alternates convolution operations with pointwise non-linearities. The first
    layer is symmetrized linear transform given by DenseSymm, while the other layers are
    G-convolutions given by DenseEquivariant. The hidden layers of the G-CNN are related by
    the following equation:

    .. math ::

        {\bf f}^{i+1}_h = \Gamma( \sum_h W_{g^{-1} h} {\bf f}^i_h).


    Args:
        symmetries: A specification of the symmetry group. Can be given by a
            :class:`netket.graph.Graph`, a :class:`netket.utils.group.PermutationGroup`, or an
            array :code:`[n_symm, n_sites]` specifying the permutations
            corresponding to symmetry transformations of the lattice.
        product_table: Product table describing the algebra of the symmetry group.
            Only needs to be specified if mode='fft' and symmetries is specified as an array.
        irreps: List of 3D tensors that project onto irreducible representations of the symmetry group.
            Only needs to be specified if mode='irreps' and symmetries is specified as an array.
        point_group: The point group, from which the space group is built. If symmetries is a
            graph the default point group is overwritten.
        mode: string "fft, irreps, matrix, auto" specifying whether to use a fast
            fourier transform over the translation group, a fourier transform using
            the irreducible representations or by constructing the full kernel matrix.
        shape: A tuple specifying the dimensions of the translation group.
        layers: Number of layers (not including sum layer over output).
        features: Number of features in each layer starting from the input. If a single
            number is given, all layers will have the same number of features.
        characters: Array specifying the characters of the desired symmetry representation.
        parity: Optional argument with value +/-1 that specifies the eigenvalue
            with respect to parity (only use on two level systems).
        param_dtype: The dtype of the weights.
        activation: The nonlinear activation function between hidden layers. Defaults to
            :class:`netket.nn.activation.reim_selu` .
        output_activation: The nonlinear activation before the output.
        equal_amplitudes: If True forces all basis states to have equal amplitude
            by setting :math:`\Re(\psi) = 0` .
        use_bias: If True uses a bias in all layers.
        precision: Numerical precision of the computation see :class:`jax.lax.Precision` for details.
        kernel_init: Initializer for the kernels of all layers. Defaults to
            :code:`lecun_normal(in_axis=1, out_axis=0)` which guarantees the correct variance of the
            output. See the documentation of :func:`flax.linen.initializers.lecun_normal`
            for more information.
        bias_init: Initializer for the biases of all layers.
        complex_output: If True, ensures that the network output is always complex.
            Necessary when network parameters are real but some `characters` are negative.
        input_mask: Optional array of shape :code:`(n_sites,)` used to restrict the convolutional
            kernel. Only parameters with mask :math:'\ne 0' are used. For best performance a
            boolean mask should be used.
        hidden_mask: Optional array of shape :code:`(n_symm,)` where
            :code:`(n_symm,) = len(graph.automorphisms())` used to restrict the convolutional
            kernel. Only parameters with mask :math:'\ne 0' are used.
            For best performance a boolean mask should be used.

    """

    if input_mask is not None:
        input_mask = HashableArray(input_mask)
    if hidden_mask is not None:
        hidden_mask = HashableArray(hidden_mask)

    # If the symmetries provided at instantiation is a Lattice object and a point group either is 
    # provided or exists, then we store the shape of the symmetries and store the space group in sg.
    # Instead of setting our mode to "auto" for automorphisms, we set mode to "fft" for fast-fourier 
    # transforms
    if isinstance(symmetries, Lattice) and (
        point_group is not None or symmetries._point_group is not None
    ):
        # With graph try to find point group, otherwise default to automorphisms
        shape = tuple(symmetries.extent)
        sg = symmetries.space_group(point_group)
        if mode == "auto":
            mode = "fft"
    
    # Otherwise, if we give a Graph object as our symmetries, we set the space group, sg, to 
    # the calculated automorphisms of the graph symmetries, set our mode from "auto" to "irreps" or, 
    # if it was previously set to "fft" we raise an error.
    elif isinstance(symmetries, Graph):
        sg = symmetries.automorphisms()
        if mode == "auto":
            mode = "irreps"
        if mode == "fft":
            raise ValueError(
                "When requesting 'mode=fft' a valid point group must be specified"
                "in order to construct the space group"
            )
    # If a permutation group was passed, then we store it straight into sg and set our mode
    # to "irreps"
    elif isinstance(symmetries, PermutationGroup):
        # If we get a group and default to irrep projection
        if mode == "auto":
            mode = "irreps"
        sg = symmetries
    else:
        if irreps is not None and (mode == "irreps" or mode == "auto"):
            mode = "irreps"
            sg = symmetries
            irreps = tuple(HashableArray(irrep) for irrep in irreps)
        elif product_table is not None and (mode == "fft" or mode == "auto"):
            mode = "fft"
            sg = symmetries
            product_table = HashableArray(product_table)
        else:
            raise ValueError(
                "Specification of symmetries is wrong or incompatible with selected mode"
            )

    # If "fft" is the mode and the shape of the translation group is not provided then raise an error,
    # otherwise we set our shape to the one provided. (shape must be an interable e.g. i for i in range(3))
    if mode == "fft":
        if shape is None:
            raise TypeError(
                "When requesting `mode=fft`, the shape of the translation group must be specified. "
                "Either supply the `shape` keyword argument or pass a `netket.graph.Graph` object to "
                "the symmetries keyword argument."
            )
        else:
            shape = tuple(shape)

    # If features is just an integer then we understand it to mean that each of the feature maps have width,
    # features. Example:
    # 
    # features = 3
    # layers = 3 
    # (3,) * 3 = (3, 3, 3)
    if isinstance(features, int):
        features = (features,) * (2*layers)


    if characters is None:
        characters = HashableArray(np.ones(len(np.asarray(sg))))
    else:
        if (
            not jnp.iscomplexobj(characters)
            and not is_complex_dtype(param_dtype)
            and not complex_output
            and jnp.any(characters < 0)
        ):
            raise ValueError(
                "`complex_output` must be used with real parameters and negative "
                "characters to avoid NaN errors."
            )
        characters = HashableArray(characters)

    if mode == "fft":
        sym = HashableArray(np.asarray(sg))
        if product_table is None:
            product_table = HashableArray(sg.product_table)
        if parity:
            return GCNN_Parity_FFT(
                symmetries=sym,
                product_table=product_table,
                layers=layers,
                features=features,
                characters=characters,
                shape=shape,
                parity=parity,
                param_dtype=param_dtype,
                complex_output=complex_output,
                hidden_mask=hidden_mask,
                input_mask=input_mask,
                **kwargs,
            )
        else:
            return ResGCNN_FFT(
                symmetries=sym,
                product_table=product_table,
                layers=layers,
                features=features,
                characters=characters,
                shape=shape,
                param_dtype=param_dtype,
                complex_output=complex_output,
                hidden_mask=hidden_mask,
                input_mask=input_mask,
                **kwargs,
            )
    elif mode in ["irreps", "auto"]:
        sym = HashableArray(np.asarray(sg))

        if irreps is None:
            irreps = tuple(HashableArray(irrep) for irrep in sg.irrep_matrices())

        if parity:
            return GCNN_Parity_Irrep(
                symmetries=sym,
                irreps=irreps,
                layers=layers,
                features=features,
                characters=characters,
                parity=parity,
                param_dtype=param_dtype,
                complex_output=complex_output,
                hidden_mask=hidden_mask,
                input_mask=input_mask,
                **kwargs,
            )
        else:
            return ResGCNN_Irrep(
                symmetries=sym,
                irreps=irreps,
                layers=layers,
                features=features,
                characters=characters,
                param_dtype=param_dtype,
                complex_output=complex_output,
                hidden_mask=hidden_mask,
                input_mask=input_mask,
                **kwargs,
            )
    else:
        raise ValueError(
            f"Unknown mode={mode}. Valid modes are 'fft',irreps' or 'auto'."
        )

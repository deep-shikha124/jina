from typing import TYPE_CHECKING, TypeVar, Tuple, Sequence, Iterator

import numpy as np

from ..mixin import ProtoTypeMixin
from ...proto import jina_pb2

if TYPE_CHECKING:
    # fix type-hint complain for sphinx and flake
    import scipy.sparse
    import tensorflow
    import torch

    ArrayType = TypeVar(
        'ArrayType',
        np.ndarray,
        scipy.sparse.spmatrix,
        tensorflow.SparseTensor,
        tensorflow.Tensor,
        torch.Tensor,
        jina_pb2.NdArrayProto,
    )

__all__ = ['NdArray']


class NdArray(ProtoTypeMixin):
    """
    A base class for containing the protobuf message of NdArray. It defines interfaces for easier get/set value.

    Do not use this class directly. Subclass should be used.

    :param proto: the protobuf message, when not given then create a new one via :meth:`get_null_proto`
    """

    def __init__(self, proto: jina_pb2.NdArrayProto):
        self._pb_body = proto

    @property
    def value(self) -> 'ArrayType':
        """Return the value in original framework type

        :return: the value of in numpy, scipy, tensorflow, pytorch type."""
        framework = self._pb_body.cls_name

        if self.is_sparse:
            if framework == 'scipy':
                idx, val, shape = self._get_raw_sparse_array()
                from scipy.sparse import coo_matrix

                x = coo_matrix((val, idx.T), shape=shape)
                sp_format = self._pb_body.parameters['sparse_format']
                if sp_format == 'bsr':
                    return x.tobsr()
                elif sp_format == 'csc':
                    return x.tocsc()
                elif sp_format == 'csr':
                    return x.tocsr()
                elif sp_format == 'coo':
                    return x
            elif framework == 'tensorflow':
                idx, val, shape = self._get_raw_sparse_array()
                from tensorflow import SparseTensor

                return SparseTensor(idx, val, shape)
            elif framework == 'torch':
                idx, val, shape = self._get_raw_sparse_array()
                from torch import sparse_coo_tensor

                return sparse_coo_tensor(idx, val, shape)
        else:
            if framework in {'numpy', 'torch', 'paddle', 'tensorflow'}:
                x = _get_dense_array(self._pb_body.dense)
                return _to_framework_array(x, framework)

    @staticmethod
    def unravel(protos: Sequence[jina_pb2.NdArrayProto]) -> 'ArrayType':
        """Unravel many ndarray-like proto in one-shot, by following the shape
        and dtype of the first proto.

        :param protos: a list of ndarray protos
        :return: a framework ndarray
        """
        first = NdArray(next(iter(protos)))
        framework, is_sparse = first._pb_body.cls_name, first.is_sparse

        if is_sparse:

            if framework in {'tensorflow'}:
                raise NotImplementedError(
                    f'fast ravel on sparse {framework} is not supported yet.'
                )

            if framework == 'scipy':
                import scipy.sparse

                n_examples = len(protos)

                n_features = first.proto.sparse.shape[1]
                indices_dtype = first.proto.sparse.indices.dtype
                values_dtype = first.proto.sparse.values.dtype

                indices_bytes = b''
                values_bytes = b''

                row_indices = []
                for k, pb_body_k in enumerate(protos):
                    indices_bytes += pb_body_k.sparse.indices.buffer
                    values_bytes += pb_body_k.sparse.values.buffer
                    row_indices += [k] * pb_body_k.sparse.values.shape[0]

                if first.proto.sparse.values.dtype:
                    indices_np = np.frombuffer(indices_bytes, dtype=indices_dtype)
                    # we can make this better if we refactor store sparse as 3 arrays (x_ind, y_ind, value)
                    cols_np = indices_np.reshape(2, -1)[1, :]
                    vals_np = np.frombuffer(values_bytes, dtype=values_dtype)
                    return scipy.sparse.csr_matrix(
                        (vals_np, (row_indices, cols_np)),
                        shape=(n_examples, n_features),
                    )

            if framework == 'torch':
                all_ds = []
                for j, p in enumerate(protos):
                    _d = _get_dense_array(p.sparse.indices)

                    _idx = np.array([j] * _d.shape[-1], dtype=np.int32)
                    if framework == 'torch':
                        _idx = _idx.reshape([1, -1])
                        _d = np.vstack([_idx, _d])
                    all_ds.append(_d)

                val = _unravel_dense_array(
                    (d.sparse.values.buffer for d in protos),
                    shape=[],
                    dtype=first.sparse.values.dtype,
                )

                idx = np.concatenate(all_ds, axis=-1)
                shape = [len(protos)] + list(first.sparse.shape)
                from torch import sparse_coo_tensor

                return sparse_coo_tensor(idx, val, shape)
        else:
            if framework in {'numpy', 'torch', 'paddle', 'tensorflow'}:
                x = _unravel_dense_array(
                    (d.dense.buffer for d in protos),
                    shape=list(first.dense.shape),
                    dtype=first.dense.dtype,
                )
                return _to_framework_array(x, framework)

    @value.setter
    def value(self, value: 'ArrayType'):
        """Set the value from numpy, scipy, tensorflow, pytorch type to protobuf.

        :param value: the framework ndarray to be set.
        """
        framework, is_sparse = _get_array_type(value)

        if framework == 'jina':
            # it is Jina's NdArray, simply copy it
            self._pb_body.cls_name = 'numpy'
            self._pb_body.CopyFrom(value._pb_body)
        elif framework == 'jina_proto':
            self._pb_body.cls_name = 'numpy'
            self._pb_body.CopyFrom(value)
        else:
            if is_sparse:
                if framework == 'scipy':
                    self._pb_body.parameters['sparse_format'] = value.getformat()
                    self._set_scipy_sparse(value)
                if framework == 'tensorflow':
                    self._set_tf_sparse(value)
                if framework == 'torch':
                    self._set_torch_sparse(value)
            else:
                if framework == 'numpy':
                    self._pb_body.cls_name = 'numpy'
                    _set_dense_array(value, self._pb_body.dense)
                if framework == 'tensorflow':
                    self._pb_body.cls_name = 'tensorflow'
                    _set_dense_array(value.numpy(), self._pb_body.dense)
                if framework == 'torch':
                    self._pb_body.cls_name = 'torch'
                    _set_dense_array(value.detach().cpu().numpy(), self._pb_body.dense)
                if framework == 'paddle':
                    self._pb_body.cls_name = 'paddle'
                    _set_dense_array(value.numpy(), self._pb_body.dense)

    @property
    def is_sparse(self) -> bool:
        """Check if the object represents a sparse ndarray.

        :return: True if the underlying ndarray is sparse
        """
        return self._pb_body.WhichOneof('content') == 'sparse'

    def _set_scipy_sparse(self, value: 'scipy.sparse.spmatrix'):
        v = value.tocoo(copy=True)
        indices = np.stack([v.row, v.col], axis=1)
        _set_dense_array(indices, self._pb_body.sparse.indices)
        _set_dense_array(v.data, self._pb_body.sparse.values)
        self._pb_body.sparse.ClearField('shape')
        self._pb_body.sparse.shape.extend(v.shape)
        self._pb_body.cls_name = 'scipy'

    def _set_tf_sparse(self, value: 'tensorflow.SparseTensor'):
        _set_dense_array(value.indices.numpy(), self._pb_body.sparse.indices)
        _set_dense_array(value.values.numpy(), self._pb_body.sparse.values)
        self._pb_body.sparse.ClearField('shape')
        self._pb_body.sparse.shape.extend(value.shape)
        self._pb_body.cls_name = 'tensorflow'

    def _set_torch_sparse(self, value):
        _set_dense_array(
            value.coalesce().indices().numpy(), self._pb_body.sparse.indices
        )
        _set_dense_array(value.coalesce().values().numpy(), self._pb_body.sparse.values)
        self._pb_body.sparse.ClearField('shape')
        self._pb_body.sparse.shape.extend(list(value.size()))
        self._pb_body.cls_name = 'torch'

    def _get_raw_sparse_array(self):
        idx = _get_dense_array(self._pb_body.sparse.indices)
        val = _get_dense_array(self._pb_body.sparse.values)
        shape = list(self._pb_body.sparse.shape)
        return idx, val, shape


def _get_dense_array(source):
    if source.buffer:
        x = np.frombuffer(source.buffer, dtype=source.dtype)
        return x.reshape(source.shape)
    elif len(source.shape) > 0:
        return np.zeros(source.shape)


def _set_dense_array(value, target):
    target.buffer = value.tobytes()
    target.ClearField('shape')
    target.shape.extend(list(value.shape))
    target.dtype = value.dtype.str


def _get_array_type(array) -> Tuple[str, bool]:
    """Get the type of ndarray without importing the framework

    :param array: any array, scipy, numpy, tf, torch, etc.
    :return: a tuple where the first element represents the framework, the second represents if it is sparse array
    """
    module_tags = array.__class__.__module__.split('.')
    class_name = array.__class__.__name__

    if 'numpy' in module_tags:
        return 'numpy', False

    if 'jina' in module_tags:
        if class_name == 'NdArray':
            return 'jina', False  # sparse or not is irrelevant

    if 'jina_pb2' in module_tags:
        if class_name == 'NdArrayProto':
            return 'jina_proto', False  # sparse or not is irrelevant

    if 'tensorflow' in module_tags:
        if class_name == 'SparseTensor':
            return 'tensorflow', True
        if class_name == 'Tensor' or class_name == 'EagerTensor':
            return 'tensorflow', False

    if 'torch' in module_tags and class_name == 'Tensor':
        return 'torch', array.is_sparse

    if 'paddle' in module_tags and class_name == 'Tensor':
        # Paddle does not support sparse tensor on 11/8/2021
        # https://github.com/PaddlePaddle/Paddle/issues/36697
        return 'paddle', False

    if 'scipy' in module_tags and 'sparse' in module_tags:
        return 'scipy', True

    raise TypeError(f'can not determine the array type: {module_tags}.{class_name}')


def _to_framework_array(x, framework):
    if framework == 'numpy':
        return x
    elif framework == 'tensorflow':
        from tensorflow import convert_to_tensor

        return convert_to_tensor(x)
    elif framework == 'torch':
        from torch import from_numpy

        return from_numpy(x)
    elif framework == 'paddle':
        from paddle import to_tensor

        return to_tensor(x)


def _unravel_dense_array(source, shape, dtype):
    x_mat = b''.join(source)
    shape = [-1] + shape
    return np.frombuffer(x_mat, dtype=dtype).reshape(shape)

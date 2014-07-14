import numpy
import theano
from theano import Apply, tensor

from theano.gradient import grad_undefined, grad_not_implemented

from theano.sandbox.cuda import cuda_available, GpuOp

if cuda_available:
    from theano.sandbox.cuda import (basic_ops, CudaNdarrayType,
                                     CudaNdarray, opt)

import theano.misc.pycuda_init
from theano.misc.pycuda_init import pycuda_available
if pycuda_available:
    import pycuda.gpuarray

try:
    import scikits.cuda
    from scikits.cuda import cublas
    import scikits.cuda.misc
    scikits.cuda.misc.init()
    scikits_cuda_available = True
except ImportError:
    scikits_cuda_available = False


def gemm_batched(Al, Bl, Cl, m, n, k, lda, ldb, ldc,
                 alpha=numpy.float32(1.0), beta=numpy.float32(1.0)):
    assert Al.shape[0] == Bl.shape[0]
    assert Al.shape[0] == Cl.shape[0]

    handle = scikits.cuda.misc._global_cublas_handle

    cublas.cublasSgemmBatched(handle, 'n', 'n', m, n, k, alpha,
                              Bl.gpudata, ldb, Al.gpudata, lda,
                              beta, Cl.gpuadata, ldc,
                              Cl.shape[0])


def gemv(alpha, A, x, beta, y):
    assert A.shape[0] == x.shape[0]
    assert A.shape[1] == y.shape[0]

    if A.strides[0] == 1:
        n, m = 0, 1
        trans = 't'
    else:
        n, m = 1, 0
        trans = 'n'

    handle = scikits.cuda.misc._global_cublas_handle

    cublas.cublasSgemv(handle, trans, A.shape[n], A.shape[m], alpha,
                       A.gpudata, A.strides[m],
                       x.gpudata, x.strides[0],
                       beta, y.gpudata, y.strides[0])


def ger(alpha, x, y, A):
    assert A.shape[1] == x.shape[0]
    assert A.shape[0] == y.shape[0]

    handle = scikits.cuda.misc._global_cublas_handle

    cublas.cublasSger(handle, A.shape[1], A.shape[0], alpha,
                      x.gpudata, x.strides[0],
                      y.gpudata, y.strides[0],
                      A.gpudata, A.strides[0])


def bptr(a):
    assert (a.ndim == 3 and a.strides[2] == 1)
    return pycuda.gpuarray.arange(a.ptr,
                                  a.ptr + a.shape[0] * a.strides[0] * 4,
                                  a.strides[0] * 4,
                                  dtype=cublas.ctypes.c_void_p)


class SparseBlockGemvSS(GpuOp):
    def __init__(self, inplace):
        self.inplace = inplace
        if self.inplace:
            self.destroy_map = {0: [0]}

    def __eq__(self, other):
        return type(self) == type(other) and self.inplace == other.inplace

    def __hash__(self):
        return hash(type(self)) ^ hash(self.inplace)

    def __str__(self):
        return "SparseBlockGemvSS%s" % ("{inplace}" if self.inplace else "")

    def make_node(self, o, W, h, inputIdx, outputIdx):
        o = basic_ops.as_cuda_ndarray_variable(o)
        W = basic_ops.as_cuda_ndarray_variable(W)
        h = basic_ops.as_cuda_ndarray_variable(h)
        assert o.ndim == 2
        assert W.ndim == 4
        assert h.ndim == 2
        assert inputIdx.ndim == 1
        assert outputIdx.ndim == 1

        assert 'int' in inputIdx.type.dtype
        assert 'int' in outputIdx.type.dtype

        return Apply(self, [o, W, h, inputIdx, outputIdx],
                     [o.type()])

    def perform(self, node, inputs, outputs):
        o, W, h, inputIdx, outputIdx = inputs
        out = outputs[0]

        if not self.inplace:
            o = o.copy()

        for j in range(o.shape[0]):
            out_id = outputIdx[j]
            for i in range(h.shape[0]):
                inp_id = inputIdx[i]
                gemv(numpy.float32(1.0), W[inp_id, out_id],
                     h[i], numpy.float32(1.0), o[j])

        out[0] = o

    def c_code(self, node, inputs, outputs, sub):
        o, W, h, inputIdx, outputIdx = inputs
        out = outputs[0]

        res = None

        if self.inplace:
            res = """
        Py_XDECREF(%(out)s);
        %(out)s = %(o);
        Py_INCREF(%(out)s);
        """ % dict(out=out, o=o)
        else:
            res = """
        if (CudaNdarray_prep_output(%(out)s, 2, CudaNdarray_HOST_DIMS(%(o)s)))
        {
          PyErr_SetString(PyExc_RuntimeError, "Cannot allocate output");
          %(fail)s
        }
        if (CudaNdarray_CopyFromCudaNdarray(%(out)s, %(o)s)) {
          PyErr_SetString(PyExc_RuntimeError, "Cannot copy data to output");
          %(fail)s
        }
        """ % dict(out=out, o=o, fail=sub['fail'])

        return res + """
        CudaNdarray *W_part = CudaNdarray_new_nd(2);
        CudaNdarray *h_part = CudaNdarray_new_nd(1);
        CudaNdarray *out_part = CudaNdarray_new_nd(1);
        if (W_part == NULL || h_part == NULL || o_part == NULL) {
          Py_XDECREF(W_part);
          Py_XDECREF(h_part);
          Py_XDECREF(out_part);
        }
        CudaNdarray_set_dim(W_part, 0, CudaNdarray_HOST_DIMS(%(W)s)[2]);
        CudaNdarray_set_stride(W_part, 0, CudaNdarray_HOST_STRIDES(%(W)s)[2]);
        CudaNdarray_set_dim(W_part, 1, CudaNdarray_HOST_DIMS(%(W)s)[3]);
        CudaNdarray_set_stride(W_part, 1, CudaNdarray_HOST_STRIDES(%(W)s)[3]);
        CudaNdarray_set_dim(h_part, 0, CudaNdarray_HOST_DIMS(%(h)s)[1]);
        CudaNdarray_set_stride(h_part, 0, CudaNdarray_HOST_STRIDES(%(h)s)[1]);
        CudaNdarray_set_dim(out_part, 0, CudaNdarray_HOST_DIMS(%(out)s)[1]);
        CudaNdarray_set_stride(out_part, 0, CudaNdarray_HOST_STRIDES(%(out)s)[1]);

        for (int j = 0; j < CudaNdarray_HOST_DIMS(%(o)s)[0]; j++) {
          npy_intp out_id = *(dtype_%(outputIdx)s *)PyArray_GETPTR1(%(outputIdx)s, j);
          CudaNdarray_set_device_data(out_part, CudaNdarray_DEV_DATA(%(out)s) +
                        CudaNdarray_HOST_STRIDES(%(out)s)[0] * j, %(out)s);
          for (int i = 0; i < CudaNdarray_HOST_DIMS(%(h)s)[0]; i++) {
            npy_intp inp_id = *(dtype_%(inputIdx)s *)PyArray_GETPTR1(%(inputIdx)s, i);
            CudaNdarray_set_device_data(h_part, CudaNdarray_DEV_DATA(%(h)s) +
                         CudaNdarray_HOST_STRIDES(%(h)s)[0] * i, %(h)s);
            CudaNdarray_set_device_data(W_part, CudaNdarray_DEV_DATA(%(W)s) +
                     (CudaNdarray_HOST_STRIDES(%(W)s)[0] * inp_id) +
                     (CudaNdarray_HOST_STRIDES(%(W)s)[1] * out_id), %(W)s);

            if (CudaNdarray_sgemv(1.0f, W_part, h_part, 1.0f, o_part)) {
               %(fail)s
            }
          }
        }
        """ % dict(out=out, h=h, o=o, inputIdx=inputIdx, outputIdx=outputIdx,
                   W=W, fail=sub['fail'])

    def grad(self, inputs, grads):
        o, W, h, inputIdx, outputIdx = inputs
        go = grads[0]

        # might revise that interface to not have a huge output
        Wgrad = sparse_block_outer_ss(W.zeros_like(),
                                      h, go, inputIdx, outputIdx)
        hgrad = sparse_block_gemv_ss(h.zeros_like(),
                                     W.dimshuffle((1, 0, 3, 2)),
                                     go,
                                     outputIdx, inputIdx)
        return [go, Wgrad, hgrad,
                grad_undefined(self, 3, inputIdx,
                               "grad of inputIdx makes no sense"),
                grad_undefined(self, 4, outputIdx,
                               "grad of outputIdx makes no sense")]


sparse_block_gemv_ss = SparseBlockGemvSS(False)
sparse_block_gemv_ss_inplace = SparseBlockGemvSS(True)


class SparseBlockOuterSS(GpuOp):
    def __init__(self, inplace=False):
        self.inplace = inplace
        if self.inplace:
            self.destroy_map = {0: [0]}

    def __eq__(self, other):
        return type(self) == type(other) and self.inplace == other.inplace

    def __hash__(self):
        return hash(type(self)) ^ hash(self.inplace)

    def __str__(self):
        return "SparseBlockOuterSS%s" % ("{inplace}" if self.inplace else "")

    def make_node(self, o, x, y, xIdx, yIdx):
        o = basic_ops.as_cuda_ndarray_variable(o)
        x = basic_ops.as_cuda_ndarray_variable(x)
        y = basic_ops.as_cuda_ndarray_variable(y)
        return Apply(self, [o, x, y, xIdx, yIdx],
                     [o.type()])

    def perform(self, node, inputs, outputs):
        o, x, y, xIdx, yIdx = inputs
        out = outputs[0]

        if not self.inplace:
            o = o.copy()

        for j in range(y.shape[0]):
            out_id = yIdx[j]
            for i in range(x.shape[0]):
                inp_id = xIdx[i]
                ger(numpy.float32(1.0), y[j],
                    x[i], o[inp_id, out_id])

        out[0] = o


sparse_block_outer_ss = SparseBlockOuterSS(False)
sparse_block_outer_ss_inplace = SparseBlockOuterSS(True)


if cuda_available:
    @opt.register_opt()
    @opt.local_optimizer([sparse_block_gemv_ss], inplace=True)
    def local_inplace_blocksparse_gemv(node):
        if node.op == sparse_block_gemv_ss:
            return [sparse_block_gemv_ss_inplace(*node.inputs)]

    @opt.register_opt()
    @opt.local_optimizer([sparse_block_outer_ss], inplace=True)
    def local_inplace_blocksparse_outer(node):
        if node.op == sparse_block_outer_ss:
            return [sparse_block_outer_ss_inplace(*node.inputs)]


def sparse_block_dot_SS(W, h, inputIdx, b, outputIdx):
    """
    var: shape, comment
    W: (iBlocks, oBlocks, iSize, oSize), weight matrix
    h: (iWin, iSize), input from lower layer (sparse)
    inputIdx: (iWin,), indexes of the input blocks
    b: (oBlocks, oSize), bias vector
    outputIdx: (oWin,), indexes of the output blocks

    returns (oBlocks, oSize), dot(W[i, j], h[i]) + b[j]
         but b[j] is only added once
    """
    return sparse_block_gemv_ss(b.take(outputIdx, axis=0), W, h,
                                inputIdx, outputIdx)

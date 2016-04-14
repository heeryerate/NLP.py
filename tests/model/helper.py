"""Helper module for nlp.model tests."""

import numpy as np

try:
    import adolc
    from adolc_helper import *
except:
    pass

try:
    import algopy
    from algopy_helper import *
except:
    pass

try:
    from nlp.model.amplmodel import AmplModel
except:
    pass

try:
    from nlp.model.pysparsemodel import PySparseAmplModel, PySparseSlackModel
except:
    pass

try:
    from nlp.model.cysparsemodel import CySparseAmplModel, CySparseSlackModel
except:
    pass

try:
    from nlp.model.scipymodel import SciPyAmplModel, SciPySlackModel
except:
    pass

try:
    import pycppad
    from cppad_helper import *
except:
    pass


class RosenbrockData(object):

    def __init__(self):
        self.expected_f = 1616.0
        self.expected_g = np.array([-804., -1204., -1204., -1204., -400.])
        self.expected_H = np.array([[1602.,  400.,    0.,    0.,   0.],
                                    [ 400., 1802.,  400.,    0.,   0.],
                                    [   0.,  400., 1802.,  400.,   0.],
                                    [   0.,    0.,  400., 1802., 400.],
                                    [   0.,    0.,    0.,  400., 200.]])

        v = np.arange(1, self.expected_H.shape[0] + 1, dtype=np.float)
        self.expected_Hv = np.dot(self.expected_H, v)


class Hs7Data(object):

    def __init__(self):
        self.expected_f = -0.39056208756589972
        self.expected_c = np.array([29.0])
        self.expected_l = -25.3905620876   # uses cons_pos().
        self.expected_g = np.array([0.8, -1.])
        self.expected_H = np.array([[-52.24, 0.],
                                    [0., -2.]])

        v = np.arange(1, self.expected_H.shape[0] + 1, dtype=np.float)
        self.expected_Hv = np.dot(self.expected_H, v)
        self.expected_J = np.array([[40., 4.]])
        self.expected_Jv = np.dot(self.expected_J, v)
        w = 2 * np.ones(self.expected_J.shape[0])
        self.expected_JTw = np.dot(self.expected_J.T, w)


class Hs7SlackData(Hs7Data):

    def __init__(self):
        super(Hs7SlackData, self).__init__()
        self.expected_c = np.array([25.0])


class Hs10Data(object):

    def __init__(self):
        self.expected_f = -1.0
        self.expected_c = np.array([-1.0])
        self.expected_l = 0.0   # uses cons_pos().
        self.expected_g = np.array([1., -1.])
        self.expected_H = np.array([[6.,  -2.],
                                    [-2., 2.]])
        v = np.arange(1, self.expected_H.shape[0] + 1, dtype=np.float)
        self.expected_Hv = np.dot(self.expected_H, v)
        self.expected_J = np.array([[2., -2.]])
        self.expected_Jv = np.dot(self.expected_J, v)
        w = 2 * np.ones(self.expected_J.shape[0])
        self.expected_JTw = np.dot(self.expected_J.T, w)


class Hs10SlackData(object):

    def __init__(self):
        self.expected_f = -1.0
        self.expected_c = np.array([-2.0])
        self.expected_l = -1.0   # uses cons_pos().
        self.expected_g = np.array([1., -1., 0.])
        self.expected_H = np.array([[6., -2., 0.],
                                    [-2., 2., 0.],
                                    [0., 0., 0.]])
        v = np.arange(1, self.expected_H.shape[0] + 1, dtype=np.float)
        self.expected_Hv = np.dot(self.expected_H, v)
        self.expected_J = np.array([[2., -2., -1]])
        self.expected_Jv = np.dot(self.expected_J, v)
        w = 2 * np.ones(self.expected_J.shape[0])
        self.expected_JTw = np.dot(self.expected_J.T, w)


class GenericTest(object):

    def get_expected(self):
        raise NotImplementedError('This method must be subclassed')

    def get_derivatives(self, model):
        return NotImplementedError('This method must be subclassed')

    def test_model(self):
        data = self.get_expected()
        if self.model.m > 0:
            (f, c, l) = get_values(self.model)
            assert(np.allclose(c, data.expected_c))
            assert(abs(l - data.expected_l) <= 1.0e-6 * abs(data.expected_l))
        else:
            f = get_values(self.model)

        assert(abs(f - data.expected_f) <= 1.0e-6 * abs(data.expected_f))

        if self.model.m > 0:
            (g, H, Hv, J, Jv, JTw) = self.get_derivatives(self.model)
            assert(np.allclose(J, data.expected_J))
            assert(np.allclose(Jv, data.expected_Jv))
            assert(np.allclose(JTw, data.expected_JTw))
        else:
            (g, H, Hv) = self.get_derivatives(self.model)

        assert(np.allclose(g, data.expected_g))
        assert(np.allclose(H, data.expected_H))
        assert(np.allclose(Hv, data.expected_Hv))


class Rosenbrock(GenericTest):

    def get_expected(self):
        return RosenbrockData()


class Hs7(GenericTest):

    def get_expected(self):
        return Hs7Data()


def get_values(model):
    f = model.obj(model.x0)
    if model.m > 0:
        c = model.cons(model.x0)
        l = model.lag(model.x0, model.pi0)
        return (f, c, l)
    else:
        return f


def get_derivatives_plain(model):
    g = model.grad(model.x0)
    H = model.hess(model.x0, model.pi0)
    v = np.arange(1, model.nvar + 1, dtype=np.float)
    Hv = model.hprod(model.x0, model.pi0, v)
    if model.m > 0:
        J = model.jac(model.x0)
        Jop = model.jop(model.x0)
        Jv = Jop * v
        w = 2 * np.ones(model.ncon)
        JTw = Jop.T * w
        return (g, H, Hv, J, Jv, JTw)
    else:
        return (g, H, Hv)


def get_derivatives_coord(model):
    g = model.grad(model.x0)

    H = ndarray_from_coord(model.nvar, model.nvar,
                           *model.hess(model.x0, model.pi0), symmetric=True)
    v = np.arange(1, model.nvar + 1, dtype=np.float)
    Hv = model.hprod(model.x0, model.pi0, v)
    if model.m > 0:
        J = ndarray_from_coord(model.ncon, model.nvar,
                               *model.jac(model.x0), symmetric=False)
        Jop = model.jop(model.x0)
        Jv = Jop * v
        w = 2 * np.ones(model.ncon)
        JTw = Jop.T * w
        return (g, H, Hv, J, Jv, JTw)
    else:
        return (g, H, Hv)


def get_derivatives_llmat(model):
    g = model.grad(model.x0)
    H = ndarray_from_ll_mat_sym(model.hess(model.x0, model.pi0))
    v = np.arange(1, model.nvar + 1, dtype=np.float)
    Hv = model.hprod(model.x0, model.pi0, v)
    if model.m > 0:
        J = ndarray_from_ll_mat(model.jac(model.x0))
        Jop = model.jop(model.x0)
        Jv = Jop * v
        w = 2 * np.ones(model.ncon)
        JTw = Jop.T * w
        return (g, H, Hv, J, Jv, JTw)
    else:
        return (g, H, Hv)


def get_derivatives_scipy(model):
    g = model.grad(model.x0)
    H = model.hess(model.x0, model.pi0).todense()
    v = np.arange(1, model.nvar + 1, dtype=np.float)
    Hv = model.hprod(model.x0, model.pi0, v)
    if model.m > 0:
        J = model.jac(model.x0).todense()
        Jop = model.jop(model.x0)
        Jv = Jop * v
        w = 2 * np.ones(model.ncon)
        JTw = Jop.T * w
        return (g, H, Hv, J, Jv, JTw)
    else:
        return (g, H, Hv)


def ndarray_from_ll_mat_sym(spA):
    n = spA.shape[0]
    A = np.zeros((n, n), dtype=np.float)
    for i in range(n):
        for j in range(i + 1):
            A[i, j] = spA[i, j]
            A[j, i] = A[i, j]
    return A


def ndarray_from_ll_mat(spA):
    m, n = spA.shape
    A = np.zeros((m, n), dtype=np.float)
    for i in range(m):
        for j in range(n):
            A[i, j] = spA[i, j]
    return A


def ndarray_from_coord(nrow, ncol, vals, rows, cols, symmetric=False):
    A = np.zeros((nrow, ncol), dtype=np.float)
    for (row, col, val) in zip(rows, cols, vals):
        A[row, col] += val
        if symmetric and row != col:
            A[col, row] = val
    return A

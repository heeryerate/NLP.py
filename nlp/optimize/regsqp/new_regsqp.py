# -*- coding: utf-8 -*-

from nlp.model.pysparsemodel import PySparseAmplModel
from nlp.model.augmented_lagrangian import AugmentedLagrangian
from nlp.model.linemodel import C1LineModel
from nlp.ls.linesearch import ArmijoLineSearch, ArmijoWolfeLineSearch, LineSearchFailure
from nlp.ls.wolfe import StrongWolfeLineSearch

from nlp.tools.exceptions import UserExitRequest
from nlp.tools.norms import norm2 as norm2
from nlp.tools.timing import cputime

try:
    from hsl.solvers.pyma57 import PyMa57Solver as LBLSolver
except ImportError:
    from hsl.solvers.pyma27 import PyMa27Solver as LBLSolver

import pysparse.sparse.pysparseMatrix as ps

import logging
import math
import numpy as np
import sys

np.set_printoptions(precision=16, formatter={'float': '{:0.8g}'.format})


class RegSQPSolver(object):
    """Regularized SQP method for equality-constrained optimization."""

    def __init__(self, model, **kwargs):
        u"""Regularized SQP framework for an equality-constrained problem.

        :keywords:
            :model: `NLPModel` instance.
            :abstol: Absolute stopping tolerance
            :reltol: relative required accuracy for ‖[g-J'y ; c]‖
            :theta: sufficient decrease condition for the inner iterations
            :prox: initial proximal parameter
            :penalty: initial penalty parameter
            :itermax: maximum number of iterations allowed
            :bump_max: Max number of times regularization parameters are
                       increased when a factorization fails (default 5).

        """
        self.model = model
        self.x = model.x0.copy()
        self.y = np.ones(model.m)

        self.abstol = kwargs.get('abstol', 1.0e-7)
        self.reltol = kwargs.get('reltol', 1.0e-6)
        self.theta = kwargs.get('theta', 0.99)
        self.itermax = kwargs.get('maxiter', max(100, 10 * model.n))

        # attributed related to quasi-Newton variants
        self.save_g = kwargs.get('save_g', False)
        self.x_old = None
        self.gL_old = None

        # Max number of times regularization parameters are increased.
        self.bump_max = kwargs.get('bump_max', 15)

        self.itn = 0
        self.K = None
        self.LBL = None
        self.short_status = "unknown"
        self.status = "unk"
        self.tsolve = 0.0
        self.f0 = None
        self.cnorm = None

        # Set regularization parameters.
        # This solver uses the proximal augmented Lagrangian formulation
        # ϕ(x;yₖ,ρ,δ) := f(x) - c(x)ᵀyₖ + ½ ρ ‖x-xₖ‖² + ½ δ⁻¹ ‖c(x)‖².
        # Note the δ⁻¹.
        self.prox_min = 1.0e-3  # used when increasing the prox parameter
        self.prox_max = 1.0e40  # used in inertia
        self.prox_last = 0.0
        self.penalty_min = 1.0e-8
        prox = max(0.0, kwargs.get('prox', 0.0))
        penalty = max(self.penalty_min, kwargs.get('penalty', 1.0))
        self.merit = AugmentedLagrangian(model,
                                         penalty=1. / penalty,
                                         prox=prox,
                                         xk=model.x0.copy())
        self.epsilon = 10. * penalty
        self.prox_factor = 10.  # increase factor during inertia correction
        self.penalty_factor = 10.  # increase factor during regularization

        # Initialize format strings for display
        self.hformat = "%-5s  %8s  %7s  %7s  %7s  %7s"
        self.header = self.hformat % ("iter", "f", u"‖c‖", u"‖∇L‖", u"ρ", u"δ")
        self.format = "%-5d  %8.1e  %7.1e  %7.1e  %7.1e  %7.1e"

        # Grab logger if one was configured.
        logger_name = kwargs.get('logger_name', 'nlp.regsqp')
        self.log = logging.getLogger(logger_name)
        self.log.addHandler(logging.NullHandler())
        self.log.propagate = False
        return

    def assemble_linear_system(self, x, y, dual_reg=True):
        u"""Assemble main saddle-point matrix.

        [ H+ρI      J' ] [∆x] = [ -g + J'y ]
        [    J     -δI ] [∆y]   [ -c       ]

        For now H is the exact Hessian of the Lagrangian.
        """
        self.log.debug('assembling linear system')

        # Some shortcuts for convenience
        model = self.model
        n = model.n
        m = model.m
        self.K = ps.PysparseMatrix(nrow=n + m, ncol=n + m,
                                   sizeHint=model.nnzh + model.nnzj + m,
                                   symmetric=True)

        # contribution of the Hessian
        H = model.hess(x, z=y)
        (val, irow, jcol) = H.find()
        self.K.put(val, irow.tolist(), jcol.tolist())

        # add primal regularization
        # if self.merit.prox > 0:
        #     self.K.addAt(self.merit.prox * np.ones(n), range(n), range(n))

        # contribution of the Jacobian
        J = model.jac(x)
        (val, irow, jcol) = J.find()
        self.K.put(val, (n + irow).tolist(), jcol.tolist())

        # dual regularization
        if dual_reg:
            self.K.put(-1. / self.merit.penalty * np.ones(m),
                       range(n, n + m), range(n, m + n))
        return

    # def initialize_rhs(self):
    #     """Allocate an empty vector to store the rhs of Newton systems."""
    #     return np.empty(self.model.n + self.model.m)
    #
    # def update_rhs(self, rhs, g, J, y, c):
    #     """Set the rhs of Newton system according to current information."""
    #     n = self.model.n
    #     rhs[:n] = -g + J.T * y
    #     rhs[n:] = -c
    #     return

    def assemble_rhs(self, g, J, y, c):
        """Set the rhs of Newton system according to current information."""
        return np.concatenate((-g + J.T * y, -c))

    def new_penalty(self, Fnorm):
        """Return updated penalty parameter value."""
        alpha = 0.1
        gamma = 1.8
        penalty = max(min(Fnorm,
                          min(alpha / self.merit.penalty,
                              1.0 / self.merit.penalty**gamma)),
                      self.penalty_min)
        # penalty = max(
        #     min(1.0 / self.merit.penalty / 10., 1.0 / self.merit.penalty**(1 +
        # 0.8)), self.penalty_min)
        return penalty

    def solve_linear_system(self, rhs, itref_thresh=1.0e-7, nitref=1):
        u"""Compute a step by solving Newton's equations.

        Use a direct method to solve the symmetric and indefinite system

        [ H+ρI      J' ] [∆x] = [ -g + J'y ]
        [    J     -δI ] [∆y]   [ -c       ].

        We increase ρ until the inertia indicates that H+ρI is positive
        definite on the nullspace of J and increase δ in case the matrix is
        singular because the rank deficiency in J.
        """
        self.log.debug('solving linear system')
        nvar = self.model.nvar
        ncon = self.model.ncon

        self.LBL = LBLSolver(self.K, factorize=True)
        second_order_sufficient = self.LBL.inertia == (nvar, ncon, 0)
        full_rank = self.LBL.isFullRank
        self.merit.prox = 0.0

        nb_bump = 0
        tired = nb_bump > self.bump_max
        while not (second_order_sufficient and full_rank) and not tired:

            if not second_order_sufficient:
                self.log.debug("further convexifying model")

                if self.merit.prox == 0.0:
                    if self.prox_last == 0.0:
                        self.merit.prox = self.prox_min
                    else:
                        self.merit.prox = max(self.prox_min,
                                              0.3 * self.prox_last)
                    self.K.addAt(self.merit.prox * np.ones(nvar),
                                 range(nvar), range(nvar))
                else:
                    if self.prox_last == 0.0:
                        factor = 100.
                    else:
                        factor = 8.
                    self.K.addAt(
                        factor * self.merit.prox * np.ones(nvar),
                        range(nvar), range(nvar))
                    self.merit.prox *= factor + 1

            if not full_rank:
                self.log.debug("further regularizing")
                # further regularize; this isn't quite supported by theory
                # the augmented Lagrangian uses 1/δ
                self.K.addAt(
                    -self.penalty_factor / self.merit.penalty * np.ones(ncon),
                    range(nvar, nvar + ncon), range(nvar, nvar + ncon))
                self.merit.penalty *= self.penalty_factor + 1

            self.LBL = LBLSolver(self.K, factorize=True)
            second_order_sufficient = self.LBL.inertia == (nvar, ncon, 0)
            inertia = self.LBL.inertia
            self.log.debug("Inertia is now (%d,%d,%d) (should be (%d,%d,%d))",
                           inertia[0], inertia[1], inertia[2],
                           self.model.nvar, self.model.ncon, 0)
            full_rank = self.LBL.isFullRank
            nb_bump += 1
            tired = nb_bump > self.bump_max

        if not second_order_sufficient:
            self.log.info("unable to convexify sufficiently")
            status = '    Unable to convexify sufficiently.'
            short_status = 'cnvx'
            solved = False
            dx = None
            dy = None
            return status, short_status, solved, dx, dy

        if not full_rank:
            self.log.info("unable to regularize sufficiently")
            status = '    Unable to regularize sufficiently.'
            short_status = 'degn'
            solved = False
            dx = None
            dy = None
            return status, short_status, solved, dx, dy

        self.prox_last = self.merit.prox
        self.LBL.solve(rhs)
        self.LBL.refine(rhs, nitref=nitref)
        (dx, dy) = self.get_dx_dy(self.LBL.x)
        self.log.debug("step accuracy: %3.2e", norm2(self.LBL.residual))
        status = None
        short_status = None
        solved = True
        return status, short_status, solved, dx, dy

    def get_dx_dy(self, step):
        """Split `step` into steps along x and y.

        Outputs are *references*, not copies.
        """
        return (step[:self.model.n], -step[self.model.n:])

    def solve_inner(self, x, y, f, g, J, c, gL,
                    Fnorm0, gLnorm0, cnorm0, Fnorm, gLnorm, cnorm):
        u"""Perform a sequence of inner iterations.

        The objective of the inner iterations is to identify an improved
        iterate w+ = (x+, y+) such that the optimality residual satisfies
        ‖F(w+)‖ ≤ Θ ‖F(w)‖ + ϵ.
        The improved iterate is identified by minimizing the proximal augmented
        Lagrangian.
        """
        self.log.debug('starting inner iterations with target %7.1e',
                       self.theta * Fnorm0 + self.epsilon)
        self.log.info(self.format, self.itn, f, cnorm, gLnorm,
                      self.merit.prox, self.merit.penalty)

        y_al = y - c * self.merit.penalty
        phi = f - np.dot(y, c) + 0.5 * self.merit.penalty * cnorm**2
        gphi = g - J.T * y_al

        model = self.model
        ls_fmt = "%7.1e  %8.1e"
        failure = False
        # finished = False

        # if infeasibility is large, immediately increase penalty
        if cnorm > self.theta * cnorm0 + 0.5 * self.epsilon:
            self.merit.penalty *= 10

        gphi_norm = norm2(gphi)
        if gphi_norm <= self.theta * gLnorm0 + 0.5 * self.epsilon:
            self.log.debug('optimality improved sufficiently')
            if cnorm <= self.theta * cnorm0 + 0.5 * self.epsilon:
                self.log.debug('feasibility improved sufficiently')
                y = y_al

        Fnorm = gphi_norm + cnorm
        finished = Fnorm <= self.theta * Fnorm0 + self.epsilon
        tired = self.itn > self.itermax

        while not (failure or finished or tired):

            self.x_old = x.copy()
            self.gL_old = gL.copy()

            self.merit.xk = x.copy()

            # compute step
            self.assemble_linear_system(x, y)
            # self.update_rhs(self.rhs, g, J, y, c)
            rhs = self.assemble_rhs(g, J, y, c)

            status, short_status, solved, dx, _ = self.solve_linear_system(rhs)

            # print "sufficient:", np.dot(self.K[:self.model.n, :self.model.n] *
            # dx, dx) + self.merit.penalty * np.dot(dx, J.T * (J * dx))
            assert solved

            if not solved:
                failure = True
                continue

            # Step 4: Armijo backtracking linesearch
            self.merit.pi = y
            line_model = C1LineModel(self.merit, x, dx)
            # TODO: pass ϕ(x) to ArmijoLineSearch
            slope = np.dot(gphi, dx)
            # ls = ArmijoLineSearch(line_model, bkmax=10,
            #                       decr=1.75, value=phi, slope=slope)
            ls = ArmijoWolfeLineSearch(line_model, step=1.0, bkmax=10,
                                       decr=1.75, value=phi, slope=slope)
            # ls = StrongWolfeLineSearch(
            #     line_model, value=phi, slope=slope, gtol=0.1)
            try:
                for step in ls:
                    # self.merit.xk = ls.iterate.copy()
                    self.log.debug(ls_fmt, step, ls.trial_value)

                print 'step norm: ', norm2(x - ls.iterate)
                x = ls.iterate
                f = model.obj(x)
                g = model.grad(x)
                J = model.jop(x)
                c = model.cons(x) - model.Lcon
                cnorm = norm2(c)
                gL = g - J.T * y
                y_al = y - c * self.merit.penalty
                phi = f - np.dot(y, c) + 0.5 * self.merit.penalty * cnorm**2
                gphi = g - J.T * y_al
                gphi_norm = norm2(gphi)

                if gphi_norm <= self.theta * gLnorm0 + 0.5 * self.epsilon:
                    self.log.debug('optimality improved sufficiently')
                    if cnorm <= self.theta * cnorm0 + 0.5 * self.epsilon:
                        self.log.debug('feasibility improved sufficiently')
                        y = y_al
                    else:
                        self.merit.penalty *= 10

                self.itn += 1
                self.inner_itn += 1
                Fnorm = gphi_norm + cnorm
                finished = Fnorm <= self.theta * Fnorm0 + self.epsilon
                tired = self.itn > self.itermax

                self.log.info(self.format, self.itn, f, cnorm, gphi_norm,
                              self.merit.prox, self.merit.penalty)

                try:
                    self.post_inner_iteration(x, gL)

                except UserExitRequest:
                    self.status = "User exit"
                    finished = True

            except LineSearchFailure:
                self.status = "Linesearch failure"
                failure = True

        solved = Fnorm <= self.theta * Fnorm0 + self.epsilon
        return x, y, f, g, J, c, gphi, gphi_norm, cnorm, Fnorm, solved

    def solve(self, **kwargs):

        # Transfer pointers for convenience.
        model = self.model
        x = self.x
        y = self.y
        self.short_status = "fail"
        self.status = "fail"
        self.tsolve = 0.0
        self.inner_itn = 0

        # Get initial objective value
        print 'x0: ', x
        self.f = self.f0 = f = model.obj(x)

        # Initialize right-hand side and coefficient matrix
        # of linear systems
        # rhs = self.initialize_rhs()

        g = model.grad(x)
        J = model.jop(x)
        c = model.cons(x) - model.Lcon
        self.cnorm = cnorm = cnorm0 = norm2(c)

        gL = g - J.T * y
        self.gLnorm = gLnorm = gLnorm0 = norm2(gL)

        Fnorm = Fnorm0 = gLnorm + cnorm

        self.log.info(self.header)
        self.log.info(self.format, self.itn, self.f0, cnorm0, gLnorm0,
                      self.merit.prox, self.merit.penalty)

        # Find a better initial point
        self.assemble_linear_system(x, y, dual_reg=False)
        rhs = self.assemble_rhs(g, J, y, c)

        self.LBL = LBLSolver(self.K, factorize=True)
        self.LBL.solve(rhs)
        (dx, dy) = self.get_dx_dy(self.LBL.x)
        self.log.debug("step accuracy: %3.2e", norm2(self.LBL.residual))

        xs = x + dx
        ys = y + dy
        gs = model.grad(xs)
        Js = model.jop(xs)
        cs = model.cons(xs) - model.Lcon
        gLs = gs - Js.T * ys
        Fnorms = norm2(gLs) + norm2(cs)
        if Fnorms < Fnorm0:
            self.log.debug("improved initial point accepted")
            x += dx
            y += dy
            Fnorm = Fnorm0 = Fnorms
            g = gs.copy()
            J = model.jop(x)
            c = cs.copy()
            self.f = f = model.obj(x)
            self.cnorm = cnorm = norm2(c)
            self.gLnorm = gLnorm = norm2(gLs)
            # self.merit.penalty = 1. / Fnorm

            self.log.info(self.format, self.itn,
                          self.f, self.cnorm, self.gLnorm,
                          self.merit.prox, self.merit.penalty)

        # Initialize penalty parameter
        # delta = min(0.1, Fnorm0)

        # set stopping tolerance
        tol = self.reltol * Fnorm0 + self.abstol

        self.tsolve = 0

        self.optimal = optimal = Fnorm <= tol
        if optimal:
            print cnorm, gLnorm
            status = 'Optimal solution found'
            short_status = 'opt'

        tired = self.itn > self.itermax
        finished = optimal or tired

        self.itn = 0
        tick = cputime()

        # Main loop.
        while not finished:

            self.x_old = x.copy()
            self.gL_old = gL.copy()

            # update penalty parameter
            self.merit.penalty = 1.0 / self.new_penalty(Fnorm)
            print 'penalty: ', self.merit.penalty

            # compute extrapolation step
            self.assemble_linear_system(x, y)
            # self.update_rhs(rhs, g, J, y, c)
            rhs = self.assemble_rhs(g, J, y, c)

            status, short_status, solved, dx, dy = \
                self.solve_linear_system(rhs, J)

            assert solved

            # check for acceptance of extrapolation step
            # if it is rejected, it will serve as a starting point in the
            # inner iterations
            self.epsilon = 10.0 / self.merit.penalty
            x += dx
            # y += dy
            yplus = y + dy
            f = model.obj(x)  # only necessary for printing
            g = model.grad(x)
            J = model.jop(x)
            c = model.cons(x) - model.Lcon

            gL_ext = g - J.T * yplus            # = ∇L(wk+)
            gLnorm_ext = norm2(gL_ext)
            cnorm_ext = norm2(c)                # = ‖c(xk+)‖
            Fnorm_ext = gLnorm_ext + cnorm_ext  # = ‖F(wk+)‖

            if Fnorm_ext <= self.theta * Fnorm + self.epsilon:
                self.log.debug("extrapolation step accepted")
                y = yplus
                gL = gL_ext
                gLnorm = gLnorm_ext
                cnorm = cnorm_ext
                Fnorm = Fnorm_ext

                try:
                    self.post_iteration(x, gL)
                except UserExitRequest:
                    self.status = "User exit"
                    short_status = 'user'

                self.itn += 1

            else:
                # perform a sequence of inner iterations
                # starting from the extrapolated step in x and the old y
                gL_inner = g - J.T * y
                gLnorm_inner = norm2(gL_inner)
                cnorm_inner = cnorm_ext
                Fnorm_inner = gLnorm_inner + cnorm_inner
                x, y, f, g, J, c, gL, gLnorm, cnorm, Fnorm, solved = \
                    self.solve_inner(x, y, f, g, J, c, gL_inner,
                                     Fnorm, gLnorm, cnorm,
                                     Fnorm_inner, gLnorm_inner, cnorm_inner)
                print "end inner iteration: ", solved

            optimal = Fnorm <= tol
            tired = self.itn > self.itermax
            finished = optimal or tired
            if self.itn % 20 == 0:
                self.log.info(self.header)

            self.log.info(self.format, self.itn, f, cnorm, gLnorm,
                          self.merit.prox, self.merit.penalty)

            if optimal:
                status = 'Optimal solution found'
                short_status = 'opt'
                finished = True
                continue

            if tired:
                status = 'Maximum number of iterations reached'
                short_status = 'iter'
                finished = True
                continue

        # Transfer final values to class members.
        self.tsolve = cputime() - tick
        self.x = x.copy()
        self.y = y.copy()
        self.f = f
        self.cnorm = cnorm
        self.gLnorm = gLnorm
        self.optimal = optimal
        self.status = status
        self.short_status = short_status
        return

    def post_iteration(self, x, g, **kwargs):
        """
        Override this method to perform additional work at the end of a
        major iteration. For example, use this method to restart an
        approximate Hessian.
        """
        return None

    def post_inner_iteration(self, x, g, **kwargs):
        """
        Override this method to perform additional work at the end of a
        minor iteration. For example, use this method to restart an
        approximate Hessian.
        """
        return None


# if __name__ == '__main__':
#
#     # Create root logger.
#     log = logging.getLogger('nlp.regsqp')
#     log.setLevel(logging.DEBUG)
#     fmt = logging.Formatter('%(name)-15s %(levelname)-8s %(message)s')
#     hndlr = logging.StreamHandler(sys.stdout)
#     hndlr.setFormatter(fmt)
#     log.addHandler(hndlr)
#
#     # Configure the solver logger.
#     sublogger = logging.getLogger('nlp.regsqp.solver')
#     sublogger.setLevel(logging.DEBUG)
#     sublogger.addHandler(hndlr)
#     sublogger.propagate = False
#
#     model = PySparseAmplModel("hs006.nl")         # Create a model
#     solver = RegSQPSolver(model)
#     solver.solve()
#     print 'x:', solver.x
#     print 'y:', solver.y
#     print solver.status

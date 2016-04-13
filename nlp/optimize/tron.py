# -*- coding: utf-8 -*-
u"""Trust-Region Method for Bound-Constrained Programming.

A pure Python/Numpy implementation of TRON as described in

Chih-Jen Lin and Jorge J. Moré, *Newton"s Method for Large Bound-
Constrained Optimization Problems*, SIAM J. Optim., 9(4), 1100–1127, 1999.
"""

import math
import logging
import numpy as np
from pykrylov.linop import SymmetricallyReducedLinearOperator as ReducedHessian

from nlp.model.nlpmodel import QPModel
from nlp.tr.trustregion import TrustRegionSolver
from nlp.tr.trustregion import GeneralizedTrustRegion
from nlp.tools import norms
from nlp.tools.utils import where, to_boundary
from nlp.tools.timing import cputime
from nlp.tools.exceptions import UserExitRequest


__docformat__ = "restructuredtext"


class TRON(object):
    u"""Trust-region Newton method for bound-constrained problems."""

    def __init__(self, model, tr_solver, **kwargs):
        u"""Instantiate a trust-region solver for a bound-constrained problem.

        The model should have the general form

            min f(x)  subject to l ≤ x ≤ u.

        :parameters:
            :model:        a :class:`NLPModel` instance.

        :keywords:
            :x0:           starting point                     (``model.x0``)
            :reltol:       relative stopping tolerance        (1.0e-5)
            :abstol:       absolute stopping tolerance        (1.0e-12)
            :maxiter:      maximum number of iterations       (max(1000,10n))
            :maxfuncall:   maximum number of objective function evaluations
                                                              (1000)
            :logger_name:  name of a logger object that can be used in the post
                           iteration                          (``None``)
        """
        self.model = model
        self.tr = GeneralizedTrustRegion()

        self.tr_solver = tr_solver
        self.solver = None

        self.iter = 0         # Iteration counter
        self.total_cgiter = 0
        self.x = kwargs.get("x0", self.model.x0.copy())
        self.f = None
        self.f0 = None
        self.g = None
        self.g_old = None
        self.save_g = False
        self.pgnorm = None
        self.pg0 = None
        self.tsolve = 0.0

        self.status = ""
        self.step_status = ""

        self.reltol = kwargs.get("reltol", 1e-12)
        self.abstol = kwargs.get("abstol", 1e-6)
        self.maxiter = kwargs.get("maxiter", 100 * self.model.n)
        self.maxfuncall = kwargs.get("maxfuncall", 1000)
        self.cgtol = 0.1
        self.alphac = 1

        self.hformat = "%-5s  %8s  %7s  %5s  %8s  %8s  %8s  %4s"
        self.header = self.hformat % ("iter", "f", u"‖P∇f‖", "inner",
                                      u"ρ", u"‖step‖", "radius", "stat")
        self.format = "%-5d  %8.1e  %7.1e  %5d  %8.1e  %8.1e  %8.1e  %4s"
        self.format0 = "%-5d  %8.1e  %7.1e  %5s  %8s  %8s  %8.1e  %4s"

        # Setup the logger. Install a NullHandler if no output needed.
        logger_name = kwargs.get("logger_name", "nlp.tron")
        self.log = logging.getLogger(logger_name)
        self.log.addHandler(logging.NullHandler())
        self.log.propagate = False

    def precon(self, v, **kwargs):
        """Generic preconditioning method---must be overridden."""
        return v

    def post_iteration(self, **kwargs):
        """Override this method to perform work at the end of an iteration.

        For example, use this method for preconditioners that need updating,
        e.g., a limited-memory BFGS preconditioner.
        """
        return None

    def project(self, x, l, u):
        """Project x into the bounds [l, u]."""
        return np.maximum(np.minimum(x, u), l)

    def projected_step(self, x, d, l, u):
        """Project the step d into the feasible box [l, u].

        The projected step is defined as s := P[x + d] - x.
        """
        return self.project(x + d, l, u) - x

    def breakpoints(self, x, d, l, u):
        """Find the smallest and largest breakpoints on the half line x + t d.

        We assume that x is feasible. Return the smallest and largest t such
        that x + t d lies on the boundary.
        """
        pos = where((d > 0) & (x < u))  # Hit the upper bound.
        neg = where((d < 0) & (x > l))  # Hit the lower bound.
        npos = len(pos)
        nneg = len(neg)

        nbrpt = npos + nneg
        # Handle the exceptional case.
        if nbrpt == 0:
            return (0, 0, 0)

        brptmin = np.inf
        brptmax = 0
        if npos > 0:
            steps = (u[pos] - x[pos]) / d[pos]
            brptmin = min(brptmin, np.min(steps))
            brptmax = max(brptmax, np.max(steps))
        if nneg > 0:
            steps = (l[neg] - x[neg]) / d[neg]
            brptmin = min(brptmin, np.min(steps))
            brptmax = max(brptmax, np.max(steps))

        self.log.debug("nearest  breakpoint: %7.1e", brptmin)
        self.log.debug("farthest breakpoint: %7.1e", brptmax)
        return (nbrpt, brptmin, brptmax)

    def cauchy(self, x, g, H, l, u, delta, alpha):
        u"""Compute a Cauchy step.

        This step must satisfy a trust region constraint and a sufficient
        decrease condition. The Cauchy step is computed for the quadratic

           q(s) = gᵀs + ½ sᵀHs,

        where H=Hᵀ and g is a vector. Given a parameter α, the Cauchy step is

           s[α] = P[x - α g] - x,

        with P the projection into the box [l, u].
        The Cauchy step satisfies the trust-region constraint and the
        sufficient decrease condition

           ‖s‖ ≤ Δ,      q(s) ≤ μ₀ gᵀs,

        where μ₀ ∈ (0, 1).
        """
        # Constant that defines sufficient decrease.
        mu0 = 0.01

        # Interpolation and extrapolation factors.
        interpf = 0.1
        extrapf = 10

        # Find the minimal and maximal breakpoints along x - α g.
        (_, _, brptmax) = self.breakpoints(x, -g, l, u)

        # Decide whether to interpolate or extrapolate.
        s = self.projected_step(x, -alpha * g, l, u)
        if norms.norm2(s) > delta:
            interp = True
        else:
            Hs = H * s
            gts = np.dot(g, s)
            interp = (.5 * np.dot(Hs, s) + gts >= mu0 * gts)

        # Either interpolate or extrapolate to find a successful step.
        if interp:
            # Reduce alpha until a successful step is found.
            search = True
            while search:
                alpha *= interpf
                s = self.projected_step(x, -alpha * g, l, u)
                if norms.norm2(s) <= delta:
                    Hs = H * s
                    gts = np.dot(g, s)
                    search = (.5 * np.dot(Hs, s) + gts >= mu0 * gts)
        else:
            # Increase alpha until a successful step is found.
            search = True
            alphas = alpha
            while search and alpha <= brptmax:
                alpha *= extrapf
                s = self.projected_step(x, -alpha * g, l, u)
                if norms.norm2(s) <= delta:
                    Hs = H * s
                    gts = np.dot(g, s)
                    if .5 * np.dot(Hs, s) + gts < mu0 * gts:
                        search = True
                        alphas = alpha
                else:
                    search = False

            # Recover the last successful step.
            alpha = alphas
            s = self.projected_step(x, -alpha * g, l, u)
        return (s, alpha)

    def projected_newton_step(self, x, g, H, delta, l, u, s, cgtol, itermax):
        u"""Generate a sequence of approximate minimizers to the QP subproblem.

            min q(x) subject to  l ≤ x ≤ u

        where q(x₀ + s) = gᵀs + ½ sᵀHs,

        x₀ is a base point provided by the user, H=Hᵀ and g is a vector.

        At each stage we have an approximate minimizer xₖ, and generate
        a direction pₖ by using a preconditioned conjugate gradient
        method on the subproblem

           min {q(xₖ + p) | ‖p‖ ≤ Δ, s(fixed)=0 },

        where fixed is the set of variables fixed at xₖ and Δ is the
        trust-region bound. Given pₖ, the next minimizer is generated by a
        projected search.

        The starting point for this subroutine is x₁ = x₀ + s, where
        s is the Cauchy step.

        Returned status is one of the following:
            info = 1  Convergence. The final step s satisfies
                      ‖(g + H s)[free]‖ ≤ cgtol ‖g[free]‖, and the
                      final x is an approximate minimizer in the face defined
                      by the free variables.

            info = 2  Termination. The trust region bound does not allow
                      further progress: ‖pₖ‖ = Δ.

            info = 3  Failure to converge within itermax iterations.
        """
        self.log.debug("Entering projected_newton_step")
        exitOptimal = False
        exitPCG = False
        exitIter = False

        w = H * s

        # Compute the Cauchy point.
        x = self.project(x + s, l, u)

        # Start the main iteration loop.
        # There are at most n iterations because at each iteration
        # at least one variable becomes active.
        iters = 0
        tol = 1.0

        while not (exitOptimal or exitPCG or exitIter):
            # Determine the free variables at the current minimizer.
            free_vars = where((x > l) & (x < u))
            nfree = len(free_vars)

            # Exit if there are no free constraints.
            if nfree == 0:
                exitOptimal = True
                info = 1
                continue

            # Obtain the submatrix of H for the free variables.
            ZHZ = ReducedHessian(H, free_vars)

            # Compute the norm of the reduced gradient Zᵀg
            gfree = g[free_vars] + w[free_vars]
            gfnorm = norms.norm2(g[free_vars])

            # Solve the trust region subproblem in the free variables
            # to generate a direction p[k]

            tol = cgtol * gfnorm  # note: gfnorm ≠ norm(gfree)
            qp = QPModel(gfree, ZHZ)
            self.solver = TrustRegionSolver(qp, self.tr_solver)
            self.solver.solve(prec=self.precon,
                              radius=self.tr.radius,
                              abstol=tol)

            step = self.solver.step
            iters += self.solver.niter

            # Use a projected search to obtain the next iterate
            xfree = x[free_vars]
            lfree = l[free_vars]
            ufree = u[free_vars]
            (xfree, proj_step) = self.projected_linesearch(xfree, lfree, ufree,
                                                           gfree, step, ZHZ,
                                                           alpha=1.0)

            # Update the minimizer and the step.
            # Note that s now contains x[k+1] - x[0]
            x[free_vars] = xfree
            s[free_vars] += proj_step

            # Compute the gradient grad q(x[k+1]) = g + H*(x[k+1] - x[0])
            # of q at x[k+1] for the free variables.
            Hs = H * s
            gfree = g[free_vars] + Hs[free_vars]
            gfnormf = norms.norm2(gfree)

            # Convergence and termination test.
            # We terminate if the preconditioned conjugate gradient method
            # encounters a direction of negative curvature, or
            # if the step is at the trust region bound.
            if gfnormf <= cgtol * gfnorm:
                exitOptimal = True
                info = 1
                continue
            elif self.solver.status == "trust-region boundary active":
                #  infotr == 3 or infotr == 4:
                exitPCG = True
                info = 2
                continue
            elif iters >= itermax:
                exitIter = True
                info = 3
                continue

        self.log.debug("Leaving projected_newton_step with info=%d", info)

        return (x, s, iters, info)

    def projected_linesearch(self, x, l, u, g, d, H, alpha=1.0):
        u"""Use a projected search to compute a satisfactory step.

        This step must satisfy a sufficient decrease condition for the
        quadratic

            q(s) = gᵀs + ½ sᵀHs,

        where H=Hᵀ and g is a vector. Given the parameter α, the step is

           s[α] = P[x + α d] - x,

        where d is the search direction and P the projection into the
        box [l, u]. The final step s = s[α] satisfies the sufficient decrease
        condition

           q(s) ≤ μ₀ gᵀs,

        where μ₀ ∈ (0, 1).

        The search direction d must be a descent direction for the quadratic q
        at x such that the quadratic is decreasing along the ray  x + α d
        for 0 ≤ α ≤ 1.
        """
        mu0 = 0.01
        interpf = 0.5
        nsteps = 0

        # Find the smallest break-point on the ray x + α d.
        (_, brptmin, _) = self.breakpoints(x, d, l, u)

        # Reduce alpha until the sufficient decrease condition is
        # satisfied or x + α w is feasible.

        search = True
        while search and alpha > brptmin:

            # Calculate P[x + alpha*w] - x and check the sufficient
            # decrease condition.
            nsteps += 1

            s = self.projected_step(x, alpha * d, l, u)
            Hs = H * s
            gts = np.dot(g, s)
            q = .5 * np.dot(Hs, s) + gts
            if q <= mu0 * gts:
                search = False
            else:
                alpha *= interpf

        # Force at least one more constraint to be added to the active
        # set if alpha < brptmin and the full step is not successful.
        # There is sufficient decrease because the quadratic function
        # is decreasing in the ray x + alpha*w for 0 <= alpha <= 1.
        if alpha < 1 and alpha < brptmin:
            alpha = brptmin

        # Compute the final iterate and step.
        s = self.projected_step(x, alpha * d, l, u)
        x = self.project(x + alpha * s, l, u)
        return (x, s)

    def projected_gradient_norm2(self, x, g, l, u):
        """Compute the Euclidean norm of the projected gradient at x."""
        lower = where(x == l)
        upper = where(x == u)

        pg = g.copy()
        pg[lower] = np.minimum(g[lower], 0)
        pg[upper] = np.maximum(g[upper], 0)

        return norms.norm2(pg[where(l != u)])

    def solve(self):
        """Solve method.

        All keyword arguments are passed directly to the constructor of the
        trust-region solver.
        """
        model = self.model

        # Project the initial point into [l,u].
        self.project(self.x, model.Lvar, model.Uvar)

        # Gather initial information.
        self.f = model.obj(self.x)
        self.f0 = self.f
        self.g = model.grad(self.x)  # Current  gradient
        self.g_old = self.g.copy()
        pgnorm = self.projected_gradient_norm2(self.x, self.g,
                                               model.Lvar, model.Uvar)
        self.pg0 = pgnorm
        cgtol = self.cgtol
        cg_iter = 0
        cgitermax = model.n

        # Initialize the trust region radius
        self.tr.radius = self.pg0

        # Test for convergence or termination
        # stoptol = max(self.abstol, self.reltol * self.pgnorm)
        stoptol = 1e-6 * pgnorm
        exitUser = False
        exitOptimal = pgnorm <= stoptol
        exitIter = self.iter >= self.maxiter
        exitFunCall = model.obj.ncalls >= self.maxfuncall
        status = ""

        # Wrap Hessian into an operator.
        H = model.hop(self.x, self.model.pi0)
        t = cputime()

        # Print out header and initial log.
        if self.iter % 20 == 0:
            self.log.info(self.header)
            self.log.info(self.format0, self.iter, self.f, pgnorm,
                          "", "", "", self.tr.radius, "")

        while not (exitUser or exitOptimal or exitIter or exitFunCall):
            self.iter += 1

            # Compute the Cauchy step and store in s.
            (s, self.alphac) = self.cauchy(self.x, self.g, H,
                                           model.Lvar, model.Uvar,
                                           self.tr.radius,
                                           self.alphac)

            # Compute the projected Newton step.
            (x, s, cg_iter, info) = self.projected_newton_step(self.x, self.g,
                                                               H,
                                                               self.tr.radius,
                                                               model.Lvar,
                                                               model.Uvar, s,
                                                               cgtol,
                                                               cgitermax)

            snorm = norms.norm2(s)
            self.total_cgiter += cg_iter

            # Compute the predicted reduction.
            m = np.dot(s, self.g) + .5 * np.dot(s, H * s)

            # Compute the function
            x_trial = self.x + s
            f_trial = model.obj(x_trial)

            # Evaluate the step and determine if the step is successful.

            # Compute the actual reduction.
            rho = self.tr.ratio(self.f, f_trial, m)
            ared = self.f - f_trial

            # On the first iteration, adjust the initial step bound.
            snorm = norms.norm2(s)
            if self.iter == 1:
                self.tr.radius = min(self.tr.radius, snorm)

            # Update the trust region bound
            slope = np.dot(self.g, s)
            if f_trial - self.f - slope <= 0:
                alpha = self.tr.gamma3
            else:
                alpha = max(self.tr.gamma1,
                            -0.5 * (slope / (f_trial - self.f - slope)))

            # Update the trust region bound according to the ratio
            # of actual to predicted reduction
            self.tr.update_radius(rho, snorm, alpha)

            # Update the iterate.
            if rho > self.tr.eta0:
                # Successful iterate
                # Trust-region step is accepted.
                self.x = x_trial
                self.f = f_trial
                self.g = model.grad(self.x)
                step_status = "Acc"
            else:
                # Unsuccessful iterate
                # Trust-region step is rejected.
                step_status = "Rej"

                # TODO: backtrack along step.

            self.step_status = step_status
            status = ""
            try:
                self.post_iteration()
            except UserExitRequest:
                status = "usr"

            # Print out header, say, every 20 iterations
            if self.iter % 20 == 0:
                self.log.info(self.header)

            pstatus = step_status if step_status != "Acc" else ""

            # Test for convergence. FATOL and FRTOL
            if abs(ared) <= self.abstol and -m <= self.abstol:
                exitOptimal = True
                status = "fatol"
            if abs(ared) <= self.reltol * abs(self.f) and \
               (-m <= self.reltol * abs(self.f)):
                exitOptimal = True
                status = "frtol"

            pgnorm = self.projected_gradient_norm2(self.x, self.g,
                                                   model.Lvar, model.Uvar)
            if pstatus == "":
                if pgnorm <= stoptol:
                    exitOptimal = True
                    status = "gtol"
            else:
                self.iter -= 1  # to match TRON iteration number

            exitIter = self.iter > self.maxiter
            exitFunCall = model.obj.ncalls >= self.maxfuncall
            exitUser = status == "usr"

            self.log.info(self.format, self.iter, self.f, pgnorm,
                          cg_iter, rho, snorm, self.tr.radius, pstatus)

        self.tsolve = cputime() - t    # Solve time
        self.pgnorm = pgnorm
        # Set final solver status.
        if status == "usr":
            pass
        elif self.iter > self.maxiter:
            status = "itr"
        self.status = status
        self.log.info("final status: %s", self.status)

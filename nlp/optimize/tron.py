"""Trust-Region Method for Unconstrained Programming."""

from pykrylov.linop import SymmetricallyReducedLinearOperator as ReducedHessian
from pykrylov.linop import LinearOperator
from nlp.tr.trustregion import TrustRegion
from nlp.tools import norms
from nlp.tools.timing import cputime
from nlp.tools.exceptions import UserExitRequest
import numpy as np
import logging
from math import sqrt

__docformat__ = 'restructuredtext'


class TRONTrustRegion(TrustRegion):
    """A trust-region management class specially taylored for TRON.

    Subclassed from `TrustRegion`.
    """

    def __init__(self, **kwargs):
        """Initialize an object allowing management of a trust region.

        :keywords:
            :radius: Initial trust-region radius (default: 1.0)
        """
        self.radius = self.radius0 = kwargs.get('radius', 1.0)
        self.radius_max = 1.0e+10
        self.eta0 = 1e-4
        self.eta1 = 0.25
        self.eta2 = 0.75
        self.gamma1 = 0.25
        self.gamma2 = 0.5
        self.gamma3 = 4.0
        self.eps = np.finfo(np.double).eps  # Machine epsilon.

    def update_radius(self, ratio, step_norm, alpha):
        """Update the trust-region radius.

        The rule implemented by this method is:

        radius = gamma1 * step_norm      if ared/pred <  eta1
        radius = gamma2 * radius         if ared/pred >= eta2
        radius unchanged otherwise,

        where ared/pred is the quotient computed by :meth:`ratio`.
        """
        if ratio <= self.eta0:
            self.radius = min(max(alpha, self.gamma1) * step_norm,
                              self.gamma2 * self.radius)
        elif ratio <= self.eta1:
            self.radius = max(self.gamma1 * self.radius,
                              min(alpha * step_norm,
                                  self.gamma2 * self.radius))
        elif ratio <= self.eta2:
            self.radius = max(self.gamma1 * self.radius,
                              min(alpha * step_norm,
                                  self.gamma3 * self.radius))
        else:
            self.radius = max(self.radius, min(alpha * step_norm,
                                               self.gamma3 * self.radius))


class TRONFramework(object):
    """Trust-region Newton method for bound-constrained optimization problems.

           min f(x)  subject to xl <= x <= xu

    where the Hessian matrix is sparse.
    """

    def __init__(self, model, **kwargs):
        """Instantiate a trust-region solver for ``model``.

        :parameters:
            :model:     a :class:`NLPModel` instance.

        :keywords:
            :x0:           starting point                     (``model.x0``)
            :reltol:       relative stopping tolerance        (1.0e-12)
            :abstol:       absolute stopping tolerance        (1.0e-6)
            :maxiter:      maximum number of iterations       (max(1000,10n))
            :maxfuncall:   maximum number of objective function evaluations
                                                              (1000)
            :logger_name:  name of a logger object that can be used in the post
                           iteration                          (``None``)
        """
        self.model = model
        self.TR = TRONTrustRegion()
        self.iter = 0         # Iteration counter
        self.total_cgiter = 0
        self.x = kwargs.get('x0', self.model.x0.copy())
        self.f = None
        self.f0 = None
        self.g = None
        self.g_old = None
        self.save_g = False
        self.gnorm = None
        self.g0 = None
        self.tsolve = 0.0

        self.reltol = kwargs.get('reltol', 1e-12)
        self.abstol = kwargs.get('abstol', 1e-6)
        self.maxiter = kwargs.get('maxiter', 100 * self.model.n)
        self.maxfuncall = kwargs.get('maxfuncall', 1000)
        self.cgtol = 0.1
        self.gtol = 1.0e-5
        self.alphac = 1
        self.feval = 0

        self.hformat = '%-5s  %8s  %7s  %5s  %8s  %8s  %8s  %4s'
        self.header = self.hformat % ('Iter', 'f(x)', '|g(x)|', 'cg',
                                      'rho', 'Step', 'Radius', 'Stat')
        self.hlen = len(self.header)
        self.hline = '-' * self.hlen
        self.format = '%-5d  %8.2e  %7.1e  %5d  %8.1e  %8.1e  %8.1e  %4s'
        self.format0 = '%-5d  %8.2e  %7.1e  %5s  %8s  %8s  %8.1e  %4s'
        self.radii = [self.TR.radius]

        # Setup the logger. Install a NullHandler if no output needed.
        logger_name = kwargs.get('logger_name', 'nlp.tron')
        self.log = logging.getLogger(logger_name)
        self.log.addHandler(logging.NullHandler())
        self.log.propagate = False

    def hprod(self, v, **kwargs):
        """Default hprod based on nlp's hprod.

        User should overload to provide a custom routine, e.g., a quasi-Newton
        approximation.
        """
        return self.model.hprod(self.x, self.model.pi0, v, **kwargs)

    def precon(self, v, **kwargs):
        """Generic preconditioning method---must be overridden."""
        return v

    def post_iteration(self, **kwargs):
        """Override this method to perform work at the end of an iteration.

        For example, use this method for preconditioners that need updating,
        e.g., a limited-memory BFGS preconditioner.
        """
        return None

    def project(self, x, xl, xu):
        """Project x into the bounds [xl, xu]."""
        return np.maximum(np.minimum(x, xu), xl)

    def gradient_projection_step(self, x, d, xl, xu):
        """Compute the projected gradient of f(x) into the feasible box.

        Feasible box is defined as

                   xl <= x <= xu

                   s = P[x + d] - x
        """
        s = np.zeros(len(x))
        for i in range(0, len(x)):
            if x[i] + d[i] < xl[i]:
                s[i] = xl[i] - x[i]
            elif x[i] + d[i] > xu[i]:
                s[i] = xu[i] - x[i]
            else:
                s[i] = d[i]

        return s

    def breakpoints(self, x, d, xl, xu):
        """Find the smallest and largest breakpoints on the half line x + t*d.

        We assume that x is feasible. Return the smallest and largest t such
        that x + t*d lies on the boundary.
        """
        nbrpt = 0
        for i in range(0, len(x)):
            if x[i] < xu[i] and d[i] > 0:
                nbrpt += 1
                brpt = (xu[i] - x[i]) / d[i]
                if nbrpt == 1:
                    brptmin = brpt
                    brptmax = brpt
                else:
                    brptmin = min(brpt, brptmin)
                    brptmax = max(brpt, brptmax)

            elif x[i] > xl[i] and d[i] < 0:
                nbrpt = nbrpt + 1
                brpt = (xl[i] - x[i]) / d[i]
                if nbrpt == 1:
                    brptmin = brpt
                    brptmax = brpt
                else:
                    brptmin = min(brpt, brptmin)
                    brptmax = max(brpt, brptmax)

        # Handle the exceptional case.
        if nbrpt == 0:
            brptmin = brptmax = 0

        self.log.debug('Nearest  breakpoint: %7.1e', brptmin)
        self.log.debug('Farthest breakpoint: %7.1e', brptmax)
        return (nbrpt, brptmin, brptmax)

    def cauchy(self, x, g, H, xl, xu, delta, alpha):
        """Compute a Cauchy step.

        This step must satisfy a trust region constraint and a sufficient
        decrease condition.

        The Cauchy step is computed for the quadratic

           q(s) = 0.5*s'*H*s + g'*s,

        where H is a symmetric matrix and g is a vector.
        Given a parameter alpha, the Cauchy step is

           s[alpha] = P[x - alpha*g] - x,

        with P the projection onto the n-dimensional interval [xl,xu].
        The Cauchy step satisfies the trust region constraint and the
        sufficient decrease condition

           || s || <= delta,      q(s) <= mu_0*(g'*s),

        where mu_0 is a constant in (0,1).
        """
        # Constant that defines sufficient decrease.
        mu0 = 0.01
        # Interpolation and extrapolation factors.
        interpf = 0.1
        extrapf = 10

        # Find the minimal and maximal break-point on x - alpha*g.
        (_, _, brptmax) = self.breakpoints(x, -g, xl, xu)

        # Evaluate the initial alpha and decide if the algorithm
        # must interpolate or extrapolate.
        s = self.gradient_projection_step(x, -alpha * g, xl, xu)
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
                alpha = interpf * alpha
                s = self.gradient_projection_step(x, -alpha * g, xl, xu)
                if norms.norm2(s) <= delta:
                    Hs = H * s
                    gts = np.dot(g, s)
                    search = (.5 * np.dot(Hs, s) + gts >= mu0 * gts)
        else:
            # Increase alpha until a successful step is found.
            search = True
            alphas = alpha
            while search and alpha <= brptmax:
                alpha = extrapf * alpha
                s = self.gradient_projection_step(x, -alpha * g, xl, xu)
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
            s = self.gradient_projection_step(x, -alpha * g, xl, xu)
        return (s, alpha)

    def trqsol(self, x, p, delta):
        """Compute a solution of the quadratic trust region equation.

        It returns the largest (non-negative) solution of
            ||x + sigma*p|| = delta.

        The code is only guaranteed to produce a non-negative solution
        if ||x|| <= delta, and p != 0.
        If the trust region equation has no solution, sigma is set to 0.
        """
        ptx = np.dot(p, x)
        ptp = np.dot(p, p)
        xtx = np.dot(x, x)
        dsq = delta**2

        # Guard against abnormal cases.
        rad = ptx**2 + ptp * (dsq - xtx)
        rad = np.sqrt(max(rad, 0))

        if ptx > 0:
            sigma = (dsq - xtx) / (ptx + rad)
        elif rad > 0:
            sigma = (rad - ptx) / ptp
        else:
            sigma = 0
        return sigma

    def truncatedcg(self, g, H, delta, tol, stol, itermax):
        """Preconditioned conjugate-gradient method.

        Given a sparse symmetric matrix H in compressed column storage,
        this subroutine uses a preconditioned conjugate gradient method
        to find an approximate minimizer of the trust region subproblem

           min { q(s) : || s || <= delta }.

        where q is the quadratic

           q(s) = 0.5 s'Hs + g's,

        and H is a symmetric matrix.

        Termination occurs if the conjugate gradient iterates leave
        the trust region, a negative curvature direction is generated,
        or one of the following two convergence tests is satisfied.

        Convergence in the original variables:

           || grad q(s) || <= tol

        Convergence in the scaled variables:

           || grad Q(w) || <= stol

        Note that if w = L'*s, then L*grad Q(w) = grad q(s).
        """
        # Initialize the iterate w and the residual r.
        w = np.zeros(len(g))

        # Initialize the residual t of grad q to -g.
        # Initialize the residual r of grad Q by solving L*r = -g.
        # Note that t = L*r.
        t = -g.copy()
        r = t.copy()

        # Initialize the direction p.
        p = r.copy()

        # Initialize rho and the norms of r and t.
        rho = np.dot(r, r)
        rnorm0 = np.sqrt(rho)

        # Exit if g = 0.
        if rnorm0 == 0:
            iters = 0
            info = 1
            return (w, iters, info)

        for iters in range(0, itermax):
            # Compute z by solving L'*z = p.
            z = p.copy()

            # Compute q by solving L*q = A*z and save L*q for
            # use in updating the residual t.
            q = H * z
            z = q.copy()

            # Compute alpha and determine sigma such that the trust region
            # constraint || w + sigma*p || = delta is satisfied.
            ptq = np.dot(p, q)
            if ptq > 0:
                alpha = rho / ptq
            else:
                alpha = 0

            sigma = self.trqsol(w, p, delta)

            # Exit if there is negative curvature or if the
            # iterates exit the trust region.

            if ptq <= 0 or alpha >= sigma:
                w = sigma * p + w
                if ptq <= 0:
                    info = 3
                else:
                    info = 4
                return (w, iters, info)

            # Update w and the residuals r and t.
            # Note that t = L*r.
            w = alpha * p + w
            r = -alpha * q + r
            t = -alpha * z + t

            # Exit if the residual convergence test is satisfied.
            rtr = np.dot(r, r)
            rnorm = np.sqrt(rtr)
            tnorm = np.sqrt(np.dot(t, t))

            if tnorm <= tol:
                info = 1
                return (w, iters, info)

            if rnorm <= stol:
                info = 2
                return (w, iters, info)

            # Compute p = r + beta*p and update rho.
            beta = rtr / rho
            p = r + p * beta
            rho = rtr

        # iters = itmax
        info = 5

        return (w, iters, info)

    def projected_newton_step(self, x, g, H, delta, xl, xu, s, cgtol, itermax):
        """Generate a sequence of approximate minimizers to the QP subprolem.

            min q(x) subject to  xl <= x <= xu

        where q(x0 + s) = 0.5 s'Hs + g's.

        Returned status is one of the following:
            info = 1  Convergence. The final step s satisfies
                      || (g + H * s)[free] || <= rtol * || g[free] ||, and the
                      final x is an approximate minimizer in the face defined
                      by the free variables.

            info = 2  Termination. The trust region bound does not allow
                      further progress.

            info = 3  Failure to converge within itermax iterations.
        """
        w = H * s

        # Compute the Cauchy point.
        x = self.project(x + s, xl, xu)

        # Start the main iteration loop.
        # There are at most n iterations because at each iteration
        # at least one variable becomes active.
        iters = 0
        for i in range(0, len(x)):
            # Determine the free variables at the current minimizer.
            nfree = 0
            free_vars = []
            for j in range(0, len(x)):
                if xl[j] < x[j] and x[j] < xu[j]:
                    nfree += 1
                    free_vars.append(j)

            nfree = len(free_vars)

            # Exit if there are no free constraints.
            if nfree == 0:
                info = 1
                return (x, s, iters, info)

            # Obtain the submatrix of H for the free variables.
            ZHZ = ReducedHessian(H, free_vars)

            # Compute the norm of the reduced gradient Z'*g
            gfree = g[free_vars] + w[free_vars]
            gfnorm = norms.norm2(g[free_vars])

            # Solve the trust region subproblem in the free variables
            # to generate a direction p[k]

            tol = cgtol * gfnorm
            stol = 0

            (w, trpcg_iters, infotr) = self.truncatedcg(gfree, ZHZ, delta,
                                                        tol, stol, 1000)
            iters += trpcg_iters

            # Use a projected search to obtain the next iterate
            xfree = x[free_vars]
            xlfree = xl[free_vars]
            xufree = xu[free_vars]
            (xfree, w) = self.projected_linesearch(xfree, xlfree, xufree,
                                                   gfree, w, ZHZ, alpha=1.0)

            # Update the minimizer and the step.
            # Note that s now contains x[k+1] - x[0]
            x[free_vars] = xfree
            s[free_vars] = s[free_vars] + w

            # Compute the gradient grad q(x[k+1]) = g + H*(x[k+1] - x[0])
            # of q at x[k+1] for the free variables.
            w = H * s
            gfree = g[free_vars] + w[free_vars]
            gfnormf = norms.norm2(gfree)

            # Convergence and termination test.
            # We terminate if the preconditioned conjugate gradient method
            # encounters a direction of negative curvature, or
            # if the step is at the trust region bound.
            if gfnormf <= cgtol * gfnorm:
                info = 1
                return (x, s, iters, info)
            elif infotr == 3 or infotr == 4:
                info = 2
                return (x, s, iters, info)
            elif iters >= itermax:
                info = 3
                return (x, s, iters, info)

        return (x, s, iters, info)

    def projected_linesearch(self, x, xl, xu, g, d, H, alpha=1.0):
        """Use a projected search to compute a satisfactory step.

        This step must satisfy a sufficient decrease condition for the
        quadratic

            q(s) = 0.5 s'Hs + g's,

        where H is a symmetric matrix and g is a vector.
        Given the parameter alpha, the step is

           s[alpha] = P[x + alpha*d] - x,

        where d is the search direction and P the projection onto the
        n-dimensional interval [xl,xu]. The final step s = s[alpha] satisfies
        the sufficient decrease condition

           q(s) <= mu_0*(g'*s),

        where mu_0 is a constant in (0,1).

        The search direction d must be a descent direction for the quadratic q
        at x such that the quadratic is decreasing in the ray  x + alpha*d
        for 0 <= alpha <= 1.
        """
        mu0 = 0.01
        interpf = 0.5
        nsteps = 0

        # Find the smallest break-point on the ray x + alpha*d.
        (_, brptmin, _) = self.breakpoints(x, d, xl, xu)

        # Reduce alpha until the sufficient decrease condition is
        # satisfied or x + alpha*w is feasible.

        search = True
        while search and alpha > brptmin:

            # Calculate P[x + alpha*w] - x and check the sufficient
            # decrease condition.
            nsteps += 1

            s = self.gradient_projection_step(x, alpha * d, xl, xu)
            Hs = H * s
            gts = np.dot(g, s)
            q = .5 * np.dot(Hs, s) + gts
            if q <= mu0 * gts:
                search = False
            else:
                alpha = interpf * alpha

        # Force at least one more constraint to be added to the active
        # set if alpha < brptmin and the full step is not successful.
        # There is sufficient decrease because the quadratic function
        # is decreasing in the ray x + alpha*w for 0 <= alpha <= 1.
        if alpha < 1 and alpha < brptmin:
            alpha = brptmin

        # Compute the final iterate and step.
        s = self.gradient_projection_step(x, alpha * d, xl, xu)
        x = self.project(x + alpha * s, xl, xu)
        return (x, s)

    def projected_gradient_norm2(self, x, g, xl, xu):
        """Compute the Euclidean norm of the projected gradient at x."""
        gpnrm2 = 0.
        for i in range(0, len(x)):
            if xl[i] != xu[i]:
                if x[i] == xl[i]:
                    gpnrm2 += min(g[i], 0)**2
                elif x[i] == xu[i]:
                    gpnrm2 += max(g[i], 0)**2
                else:
                    gpnrm2 += g[i]**2
        return sqrt(gpnrm2)

    def solve(self):
        """Solve method.

        :keywords:
            :maxiter:  maximum number of iterations.

        All other keyword arguments are passed directly to the constructor of
        the trust-region solver.
        """
        model = self.model

        # Project the initial point into [xl,xu].
        self.project(self.x, model.Lvar, model.Uvar)

        # Gather initial information.
        self.f = model.obj(self.x)
        self.feval += 1
        self.f0 = self.f
        self.g = model.grad(self.x)  # Current  gradient
        self.g_old = self.g.copy()
        self.gnorm = norms.norm2(self.g)
        self.g0 = self.gnorm
        cgtol = self.cgtol
        cgiter = 0
        cgitermax = model.n

        # Initialize the trust region radius
        self.TR.radius = self.g0

        # Test for convergence or termination
        stoptol = self.gtol * self.g0
        exitUser = False
        exitOptimal = False
        exitIter = self.iter >= self.maxiter
        exitFunCall = self.feval >= self.maxfuncall
        status = ''

        # Wrap Hessian into an operator.
        H = LinearOperator(model.n, model.n,
                           lambda v: self.hprod(v),
                           symmetric=True)

        t = cputime()

        # Print out header and initial log.
        if self.iter % 20 == 0:
            self.log.info(self.hline)
            self.log.info(self.header)
            self.log.info(self.hline)
            self.log.info(self.format0, self.iter, self.f, self.gnorm,
                          '', '', '', self.TR.radius, '')

        while not (exitUser or exitOptimal or exitIter or exitFunCall):
            self.iter += 1

            # Compute a step and evaluate the function at the trial point.

            # Save the best function value, iterate, and gradient.
            self.fc = self.f
            self.xc = self.x.copy()
            self.gc = self.g.copy()

            # Compute the Cauchy step and store in s.
            (s, self.alphac) = self.cauchy(self.x, self.g, H,
                                           model.Lvar, model.Uvar,
                                           self.TR.radius,
                                           self.alphac)

            # Compute the projected Newton step.
            (x, s, cg_iter, info) = self.projected_newton_step(self.x, self.g,
                                                               H,
                                                               self.TR.radius,
                                                               model.Lvar,
                                                               model.Uvar, s,
                                                               cgtol,
                                                               cgitermax)

            snorm = norms.norm2(s)
            self.total_cgiter += cg_iter

            # Compute the predicted reduction.
            m = np.dot(s, self.g) + .5 * np.dot(s, H * s)

            # compute the function
            x_trial = self.x + s
            f_trial = model.obj(x_trial)
            self.feval += 1

            # Evaluate the step and determine if the step is successful.

            # Compute the actual reduction.
            rho = self.TR.ratio(self.f, f_trial, m)
            ared = self.f - f_trial

            # On the first iteration, adjust the initial step bound.
            snorm = norms.norm2(s)
            if self.iter == 1:
                self.TR.radius = min(self.TR.radius, snorm)

            # Update the trust region bound
            slope = np.dot(self.g, s)
            if f_trial - self.f - slope <= 0:
                alpha = self.TR.gamma3
            else:
                alpha = max(self.TR.gamma1,
                            -0.5 * (slope / (f_trial - self.f - slope)))

            # Update the trust region bound according to the ratio
            # of actual to predicted reduction
            self.TR.update_radius(rho, snorm, alpha)

            # Update the iterate.
            if rho > self.TR.eta0:
                # Successful iterate
                # Trust-region step is accepted.
                self.x = x_trial
                self.f = f_trial
                self.g = model.grad(self.x)
                self.gnorm = norms.norm2(self.g)
                step_status = 'Acc'

            else:
                # Unsuccessful iterate
                # Trust-region step is rejected.
                step_status = 'Rej'

            self.step_status = step_status
            self.radii.append(self.TR.radius)
            status = ''
            try:
                self.post_iteration()
            except UserExitRequest:
                status = 'usr'

            # Print out header, say, every 20 iterations
            if self.iter % 20 == 0 and self.verbose:
                self.log.info(self.hline)
                self.log.info(self.header)
                self.log.info(self.hline)

            if self.verbose:
                pstatus = step_status if step_status != 'Acc' else ''
                self.log.info(self.format, self.iter, self.f, self.gnorm,
                              cg_iter, rho, snorm, self.TR.radius, pstatus)

            # Test for convergence. FATOL and FRTOL
            if abs(ared) <= self.abstol and -m <= self.abstol:
                exitOptimal = True
                status = 'fatol'
            if abs(ared) <= self.reltol * abs(self.f) and \
               (-m <= self.reltol * abs(self.f)):
                exitOptimal = True
                status = 'frtol'

            if pstatus == '':
                pgnorm2 = self.projected_gradient_norm2(self.x, self.g,
                                                        model.Lvar, model.Uvar)
                if pgnorm2 <= stoptol:
                    exitOptimal = True
                    status = 'gtol'
            else:
                self.iter -= 1  # to match TRON iteration number

            exitIter = self.iter > self.maxiter
            exitFunCall = self.feval >= self.maxfuncall
            exitUser = status == 'usr'

        self.tsolve = cputime() - t    # Solve time
        # Set final solver status.
        if status == 'usr':
            pass
        elif self.iter > self.maxiter:
            status = 'itr'
        self.status = status

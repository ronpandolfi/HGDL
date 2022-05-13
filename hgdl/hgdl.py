import numpy as np
import torch as t
import time

from loguru import logger
from functools import *
import hgdl.misc as misc
from hgdl.local_methods.local_optimizer import run_local_optimizer
from hgdl.global_methods.global_optimizer import run_global
from hgdl.local_methods.local_optimizer import run_local
import dask.distributed as distributed
import dask.multiprocessing
from dask.distributed import as_completed
from hgdl.optima  import optima
from hgdl.meta_data  import meta_data
import pickle


#####todo: Constraints: we have to update the new lamb and slack variables in the constraint class



class HGDL:
    """
    This is HGDL, a class for asynchronous HPC-customized optimization.
    H ... Hybrid
    G ... Global
    D ... Deflated
    L ... Local
    The algorithm places a number of walkers inside the domain (the number is determined by the dask client), all of which perform
    a local optimization in a distributed way in parallel. When the walkers have identified local optima, their positions are communicated back to the host
    who removes the found optima by deflation, and replaces the fittest walkers by a global optimization step. From here the next epoch
    begins with distributed local optimizations of the new walkers. The algorithm results in a sorted list of unique optima (only if optima
    are of the form f'(x) = 0)
    The method `hgdl.optimize` instantly returns a result object that can be queried for a growing, sorted list of
    optima. If a Hessian is provided, those optima are classified as minima, maxima or saddle points.

    Parameters
    ----------
    func : Callable
        The function to be MINIMIZED. A callable that accepts an np.ndarray and optional arguments, and returns a scalar.
    grad : Callable
        The gradient of the function to be MINIMIZED. A callable that accepts an np.ndarray and optional arguments, and returns a vector
        (np.ndarray) of shape (D), where D is the dimensionality of the space in which the
        optimization takes place.
    bounds : np.ndarray
        The bounds in which to optimize; an np.ndarray of shape (D x 2), where D is the dimensionality of the space in which the
        optimization takes place.
    hess : Callable, optional
        The Hessian of the function to be MINIMIZED. A callable that accepts an np.ndarray and optional arguments, and returns a
        np.ndarray of shape (D x D). The default value is no-op.
    num_epochs : int, optional
        The number of epochs the algorithm runs through before being terminated. One epoch is the convergence of all local walkers,
        the deflation of the identified optima, and the global replacement of the walkers. Note, the algorithm is running asynchronously, so a high number
        of epochs can be chosen without concern, it will not affect the run time to obtain the optima. Therefore, the default is
        100000.
    global_optimizer : Callable or str, optional
        The function (identified by a string or a Callable) that replaces the fittest walkers after their local convergence.
        The possible options are `genetic` (default), `gauss`, `random` or a callable that accepts an
        np.ndarray of shape (U x D) of positions, an np.ndarray of shape (U) of function values,
        and np.ndarray of shape (D x 2) of bounds, and an integer specifying the number of offspring
        individuals that should be returned. The callable should return the positions of the offspring
        individuals as an np.ndarray of shape (number_of_offspring x D).
    local_optimizer : Callable or str, optional
        The local optimizer that is used for the local-walker optimization. The options are
        `dNewton` (the default Newton method that need a Hessian), `L-BFGS-B`, `BFGS`, `CG`, `Newton-CG` and most other scipy.optimize.minimize
        local optimizers. The above methods have been tested, but most others should work. Visit the `scipy.optimize.minimize` docs for specifications
        and limitations of the local methods. The parameter also accepts a callable that accepts as input a function, gradient, Hessian,
        bounds (all as specified above), and args, and returns an object similar to the scipy.optimize.minimize methods.
    number_of_optima : int, optional
        The number of optima that will be stored in the optima list and deflated. The default is 1e6.
    radius: float, optional
        The radius of the deflation operator. The default is estimated from the size of the domain.
        This will be changed in future releases to be estimated from the curvature of the function
        at the optima.
    local_max_iter : int, optional
        The number of iterations before local optimizations are terminated. The default is 100.
    args : tuple, optional
        A tuple of arguments that will be communicated to the function, the gradient, and the Hessian callables.
        Default = ().
    constr : object, optional
        An optional constraint that is communicated to the local optimizers. The format follows from scipy.optimize.minimize.
        The default is no constraints. Make sure you use a local optimizer that allows for constraints. The recommended option is
        `local_optimizer = "SLSQP"`.

    Attributes
    ----------
    optima : object
        Contains the attribute optima.list in which the optima are stored. 
        However, the method 'get_latest(n)' should be used to access the optima.


    """

    def __init__(self, func, grad, bounds,
            hess = None, num_epochs=100000,
            global_optimizer = "genetic",
            local_optimizer = "L-BFGS-B",
            number_of_optima = 1000000,
            radius = None,
            local_max_iter = 100,
            args = (), constraints = ()):
        self.constr = constraints
        self.dim_orig = len(bounds)
        if self.constr:
            #option 1: func/grad/hass class methods, have self.constraints
            #option 2: use partial, constraints dont have to be attribute
            #option 3: func,... not class methods
            c_var = 0
            for c in self.constr:
                if c.ctype == "=": c_var += 1
                else: c_var += 2
                bounds = np.row_stack([bounds,c.bounds])
            self.L = func
            self.Lgrad = grad
            self.Lhess = hess
            self.func = self.lagrangian
            self.grad = self.lagrangian_grad
            self.hess = None #self.lagrangian_hess
        else:
            self.func = func
            self.grad = grad
            self.hess = hess
        self.bounds = np.asarray(bounds)
        if radius is None: self.radius = np.min(bounds[:,1]-bounds[:,0])/1000.0
        else: self.radius = radius
        self.dim = len(self.bounds)
        self.local_max_iter = local_max_iter
        self.num_epochs = num_epochs
        self.global_optimizer = global_optimizer
        self.local_optimizer = local_optimizer
        self.args = args
        self.optima = optima(self.dim, number_of_optima)
        logger.debug("HGDL successfully initiated {}")
        logger.debug("deflation radius set to {}", self.radius)
        if callable(self.hess): logger.debug("Hessian was provided by the user: {}", self.hess)
        logger.debug("========================")
    ###########################################################################
    ###########################################################################
    ############USER FUNCTIONS#################################################
    ###########################################################################
    ###########################################################################
    ###########################################################################
    def optimize(self, dask_client = None, x0 = None, tolerance = 1e-6):
        """
        Function to start the optimization. Note, this function will not return anything.
        Use the method hgdl.HGDL.get_latest() (non-blocking) or hgdl.HGDL.get_final() (blocking)
        to query results.

        Parameters
        ----------
        dask_client : distributed.client.Client, optional
            The client that will be used for the distibuted local optimizations. The default is a local client.
        x0 : np.ndarray, optional
            An np.ndarray of shape (V x D) of points used as starting positions.
            If V > number of walkers (specified by the dask client) the array will be truncated.
            If V < number of walkers, random points will be appended.
            The default is None, meaning only random points will be used.
        tolerance : float, optional
            The tolerance used by the local optimizers. The default is 1e-6
        """

        client = self._init_dask_client(dask_client)
        self.tolerance = tolerance
        logger.debug(client)
        if x0 is not None and len(x0[0]) != self.dim: raise Exception("The given starting locations do not have the right dimensionality.")
        self.x0 = self._prepare_starting_positions(x0)
        logger.debug("HGDL starts with: {}", self.x0)
        self.meta_data = meta_data(self)
        self._run_epochs(client)

    ###########################################################################
    def get_client_info(self):
        """
        Function to receive info about the workers.
        """
        return self.workers
    ###########################################################################
    def get_latest(self, n = None):
        """
        Function to request the current best n results.

        Parameters
        ----------
        n : int, optional
            Number of optima list entries to be returned.
            Default is the length of the current optima list.
        """
        try:
            data, frames = self.transfer_data.get()
            self.optima = distributed.protocol.deserialize(data,frames)
            logger.debug("HGDL called get_latest() successfully")
        except Exception as err:
            self.optima = self.optima
            logger.error("HGDL get_latest failed due to {} \n optima list unchanged", str(err))

        optima_list = self.optima.list
        if n is not None: n = min(n,len(optima_list["x"]))
        else: n = len(optima_list["x"])
        return {"x": optima_list["x"][0:n], \
                "func evals": optima_list["func evals"][0:n],
                "classifier": optima_list["classifier"][0:n],
                "eigen values": optima_list["eigen values"][0:n],
                "gradient norm":optima_list["gradient norm"][0:n],
                "success":optima_list["success"]}
    ###########################################################################
    def get_final(self,n = None):
        """
        Function to request the final result.
        CAUTION: This function will block the main thread until
        the end of all epochs is reached.

        Parameters
        ----------
        n : int, optional
            Number of optima list entries to be returned.
            Default is the length of the final optima list.
        """
        try:
            self.optima = self.main_future.result()
        except Exception as err:
            logger.error("HGDL get_final failed due to {}", str(err))
        optima_list = self.optima.list
        if n is not None: n = min(n,len(optima_list["x"]))
        else: n = len(optima_list["x"])
        return {"x": optima_list["x"][0:n], \
                "func evals": optima_list["func evals"][0:n],
                "classifier": optima_list["classifier"][0:n],
                "eigen values": optima_list["eigen values"][0:n],
                "gradient norm":optima_list["gradient norm"][0:n],
                "success":optima_list["success"]}
    ###########################################################################
    def cancel_tasks(self, n = None):
        """
        Function to cancel all tasks and therefore the execution.
        However, this function does not kill the client.

        Parameters
        ----------
        n : int, optional
            Number of optima list entries to be returned.
            Default is the length of the current optima list.
        """
        logger.debug("HGDL is cancelling all tasks...")
        res = self.get_latest(n)
        self.break_condition.set(True)
        self.client.cancel(self.main_future)
        logger.debug("Status of HGDL task: ", self.main_future.status)
        logger.debug("This leaves the client alive.")
        return res
    ###########################################################################
    def kill_client(self, n = None):
        """
        Function to cancel all tasks and kill the dask client, and therefore the execution.

        Parameters
        ----------
        n : int, optional
            Number of optima list entries to be returned.
            Default is the length of the current optima list.
        """
        logger.debug("HGDL kill client initialized ...")
        res = self.get_latest(n)
        try:
            self.break_condition.set(True)
            self.client.gather(self.main_future)
            self.client.cancel(self.main_future)
            del self.main_future
            self.client.shutdown()
            self.client.close()
            logger.debug("HGDL kill client successful")
        except Exception as err:
            raise RuntimeError("HGDL kill failed") from err
        return res
    ###########################################################################
    ############USER FUNCTIONS END#############################################
    ###########################################################################
    def _prepare_starting_positions(self,x0):
        if x0 is None: x0 = misc.random_population(self.bounds,self.number_of_walkers)
        elif x0.ndim == 1: x0 = np.array([x0])

        if len(x0) < self.number_of_walkers:
            x0_aux = np.zeros((self.number_of_walkers,len(x0[0])))
            x0_aux[0:len(x0)] = x0
            x0_aux[len(x0):] = misc.random_population(self.bounds,self.number_of_walkers - len(x0))
            x0 = x0_aux
        elif len(x0) > self.number_of_walkers:
            x0 = x0[0:self.number_of_walkers]
        else: x0 = x0
        return x0
    ###########################################################################
    def _init_dask_client(self,dask_client):
        if dask_client is None: 
            dask_client = dask.distributed.Client()
            logger.debug("No dask client provided to HGDL. Using the local client")
        else:
            logger.debug("dask client provided to HGDL")
        client = dask_client
        worker_info = list(client.scheduler_info()["workers"].keys())
        if not worker_info: raise Exception("No workers available")
        self.workers = {"host": worker_info[0],
                "walkers": worker_info[1:]}
        logger.debug(f"Host {self.workers['host']} has {len(self.workers['walkers'])} workers.")
        self.number_of_walkers = len(self.workers["walkers"])
        return client
    ###########################################################################
    def _run_epochs(self,client):
        self.break_condition = distributed.Variable("break_condition",client)
        self.transfer_data = distributed.Variable("transfer_data",client)
        a = distributed.protocol.serialize(self.optima)
        self.transfer_data.set(a)
        self.break_condition.set(False)
        data = {"transfer data":self.transfer_data,
                "break condition":self.break_condition,
                "optima":self.optima, "metadata":self.meta_data}
        bf = client.scatter(data, workers = self.workers["host"])
        self.main_future = client.submit(hgdl, bf, workers = self.workers["host"])
        self.client = client
    ###########################################################################
    def lagrangian(self,x, *args):
        L = self.L(x[0:self.dim_orig], *args)
        for c in self.constr:
            if   c.ctype == '=': L += c.lamb * (c.nlc(x[0:self.dim_orig],*args) - c.value)
            elif c.ctype == '<': L += c.lamb * (c.nlc(x[0:self.dim_orig],*args) - c.value + c.slack**2)
            elif c.ctype == '>': L += c.lamb * (c.nlc(x[0:self.dim_orig],*args) - c.value - c.slack**2)
            else: raise Exception("Wrong ctype in constraint")
        return L
    def lagrangian_grad(self,x, *args):
        Lgrad = np.zeros((len(x)))
        Lgrad[0:self.dim_orig] = self.Lgrad(x[0:self.dim_orig], *args) 
        index = self.dim_orig
        for c in self.constr:
            Lgrad[0:self.dim_orig] += c.lamb * c.nlc_grad(x[0:self.dim_orig], *args)
            if c.ctype == '=':
                Lgrad[index] = c.nlc(x[0:self.dim_orig],*args) - c.value
                index += 1
            elif c.ctype == '<':
                Lgrad[index] = c.nlc(x[0:self.dim_orig],*args) - c.value + c.slack**2
                index += 1
                Lgrad[index] = 2.0 * c.lamb * c.slack
                index += 1
            elif c.ctype == '>':
                Lgrad[index] = c.nlc(x[0:self.dim_orig],*args) - c.value - c.slack**2
                index += 1
                Lgrad[index] = -2.0 * c.lamb * c.slack
                index += 1
            else: raise Exception("Wrong ctype in constraint")
        return Lgrad

###########################################################################
###########################################################################
##################hgdl functions###########################################
###########################################################################
###########################################################################
def hgdl(data):
    metadata = data["metadata"]
    transfer_data = data["transfer data"]
    break_condition = data["break condition"]
    optima = data["optima"]
    logger.debug("HGDL computing epoch 1 of {}", metadata.num_epochs)
    optima = run_local(metadata,optima,metadata.x0)
    a = distributed.protocol.serialize(optima)
    transfer_data.set(a)

    for i in range(1,metadata.num_epochs):
        bc = break_condition.get()
        if bc is True:
            logger.debug(f"HGDL Epoch {i} was cancelled")
            break
        logger.debug(f"HGDL computing epoch {i+1} of ", metadata.num_epochs)
        optima = run_hgdl_epoch(metadata,optima)
        a = distributed.protocol.serialize(optima)
        transfer_data.set(a)
    logger.debug("HGDL finished all epochs!")
    return optima
###########################################################################
def run_hgdl_epoch(metadata,optima):
    optima_list = optima.list
    n = min(len(optima_list["x"]),metadata.number_of_walkers)
    x0 = run_global(\
            np.array(optima_list["x"][0:n,:]),
            np.array(optima_list["func evals"][0:n]),
            metadata.bounds, metadata.global_optimizer,metadata.number_of_walkers)
    x0 = np.array(x0)
    optima = run_local(metadata,optima,x0)
    return optima
